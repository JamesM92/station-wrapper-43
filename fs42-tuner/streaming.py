"""A read-ahead server for big files on a network share: play while it arrives, with a head start worked out from the
share's own speed.

A big video (a film) that is read straight from a share depends on the share the whole time it plays, and the open and the
seek at a join are the slowest things a busy share does. Copying the whole file first needs room for all of it and, for a
file that is bigger than the cache, is not possible. This sits between ffmpeg and the file instead:

    ffmpeg --http, ranges--> [this server, 127.0.0.1] --reads in 4 MB blocks--> the share
                                  |
                                  +-- blocks kept in RAM (the /cache tmpfs) first, then a local disk folder; what has been
                                      played is dropped at once

  * a read WAITS for its bytes, so ffmpeg can never get ahead of what has arrived (nothing is ever returned short or as zeros);
  * it holds only as much ahead of the playing position as the share can't deliver in time: with the file's playback rate P
    and the share's current speed D, `need = bytes still to play x (1 - D/P)`, never less than a floor of about a minute.
    That starts large and falls to nothing at the end of the file, so several films can be held at once;
  * what to fetch, and in what order, follows the join that is due soonest; the rest get what room is left (and the
    shortfall is reported);
  * pulling ahead takes at most half of the share's measured speed, so the streams already playing are not starved. A read
    ffmpeg is waiting for is never held back.

Correctness never depends on the estimates: a block that was not fetched ahead is fetched when it is asked for.
"""
import heapq
import http.server
import itertools
import math
import os
import re
import secrets
import socketserver
import threading
import time

BLOCK = 4 * 1024 * 1024
INDEX_BYTES = 8 * 1024 * 1024            # the start and the end of a file (headers, the index) are kept for the whole play
FLOOR_SECONDS = 75.0                     # never hold less than this much ahead of the playing position
WAIT_SECONDS = 90.0                      # a read that waits longer than this is given up (the caller falls back to the share)
MARGIN = 1.5
CUSHION = 5.0
NETWORK_FS = ("cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p", "fuse.rclone", "fuse.mergerfs", "ceph", "glusterfs")
DEFAULT_RATE = 8_000_000 / 8             # bytes/s assumed for a file whose duration is not known (8 Mbit/s)


def env_config(env=None):
    e = os.environ if env is None else env

    def num(name, default):
        try:
            return float(e.get(name, "") or default)
        except ValueError:
            return float(default)
    off = ("0", "false", "no", "off")
    return {
        "enabled": e.get("PREFETCH_STREAM", "1").strip().lower() not in off,
        "min_bytes": int(num("PREFETCH_STREAM_MIN_MB", 256) * 1024 * 1024),
        "disk_dir": e.get("PREFETCH_DISK_DIR", ""),
        "disk_bytes": int(num("PREFETCH_DISK_GB", 30) * 1024 ** 3),
        "share_fraction": min(max(num("PREFETCH_STREAM_SHARE", 0.5), 0.1), 1.0),
        "workers": max(1, int(num("PREFETCH_STREAM_WORKERS", 2))),
        "any_fs": e.get("PREFETCH_STREAM_ANY", "0").strip().lower() not in off,      # treat every file as being on a share
    }


def need_bytes(remaining, rate, speed, floor_seconds=FLOOR_SECONDS):
    """How much of `remaining` bytes to still play must be held ahead of the playing position so that playing can go on
    without waiting: what the share can't deliver in time, `remaining x (1 - speed/rate)`, and never less than the floor."""
    remaining = max(0, int(remaining))
    if remaining == 0 or rate <= 0:
        return 0
    short = remaining * max(0.0, 1.0 - speed / rate)
    return int(min(remaining, max(short, rate * floor_seconds)))


class _Bucket:
    """A pace for the pulling ahead of one share: `rate` bytes/s, with up to a second in hand."""

    def __init__(self, now=time.time):
        self.now, self.at, self.credit = now, None, 0.0

    def wait_time(self, nbytes, rate):
        t = self.now()
        if self.at is None:
            self.at, self.credit = t, rate                       # the first second is free
        self.credit = min(self.credit + (t - self.at) * rate, rate)
        self.at = t
        self.credit -= nbytes
        return max(0.0, -self.credit / rate) if self.credit < 0 else 0.0


class Source:
    """One big file being played or made ready."""

    def __init__(self, sid, path, size, rate, share, ext):
        self.id, self.path, self.size, self.rate, self.share, self.ext = sid, path, size, rate, share, ext
        self.nblocks = max(1, math.ceil(size / BLOCK))
        self.blocks = {}                  # block number -> (tier, file name)
        self.inflight = set()
        self.cond = threading.Condition()
        self.refs = 0                     # readers (pieces being played)
        self.playhead = 0                 # byte the last read asked for
        self.due = None                   # when a piece that will play from `seek` is due, or None
        self.seek_byte = 0
        self.last_used = time.time()
        self.failed = None                # why reads cannot be served (the caller reads the file directly)
        self.short = 0                    # bytes wanted that there was no room for
        self.need = 0

    def block_of(self, offset):
        return min(offset // BLOCK, self.nblocks - 1)


class Streamer:
    def __init__(self, cfg, stats, media_root, ram_dir, ram_free, disk_dir=None, log=print, now=time.time, opener=open,
                 rate_of=None):
        """cfg: env_config(). stats: prefetch.Stats. ram_free(): bytes the RAM tier can hold for streams (what the
        whole-file copies leave of it).
        rate_of(path) -> playback bytes/s or None. `opener` reads the files (a test can slow it down)."""
        self.cfg, self.stats, self.media_root, self.log, self.now, self.opener = cfg, stats, media_root, log, now, opener
        self.ram_dir, self.ram_free, self.disk_dir = ram_dir, ram_free, disk_dir
        self.rate_of = rate_of or (lambda p: None)
        self.lock = threading.RLock()
        self.sources = {}                 # id -> Source
        self.by_path = {}
        self.queue = []                   # heap of (priority, seq, source id, block)
        self.seq = itertools.count()
        self.qcond = threading.Condition()
        self.used = {"ram": 0, "disk": 0}
        self.buckets = {}
        self.stop_event = threading.Event()
        self.port = None
        self.enabled = False
        self.started = False
        self._tier_dirs()

    # ---- set up
    def _tier_dirs(self):
        for d in (self.ram_dir, self.disk_dir):
            if d:
                try:
                    os.makedirs(d, exist_ok=True)
                    for n in os.listdir(d):
                        if n.endswith(".blk"):                # whatever an earlier run left is not trusted
                            try:
                                os.unlink(os.path.join(d, n))
                            except OSError:
                                pass
                except OSError:
                    if d == self.disk_dir:
                        self.disk_dir = None
        self.enabled = bool(self.cfg["enabled"] and (self.ram_dir or self.disk_dir))

    def start(self):
        """Start the server and the workers. Returns the port."""
        if not self.enabled or self.started:
            return self.port
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _target(self):
                m = re.match(r"^/s/([A-Za-z0-9_-]+)(\.[A-Za-z0-9]{1,8})?$", self.path.split("?", 1)[0])
                with outer.lock:
                    src = outer.sources.get(m.group(1)) if m else None
                if src is None or src.failed:
                    self.send_error(404)
                    return None
                return src

            def do_HEAD(self):
                src = self._target()
                if src is None:
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(src.size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()

            def do_GET(self):
                src = self._target()
                if src is None:
                    return
                start, end, partial = 0, src.size - 1, False
                m = re.match(r"^bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), src.size - 1)
                    else:                                      # the last N bytes
                        start = max(0, src.size - int(m.group(2)))
                    partial = True
                if start >= src.size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{src.size}")
                    self.end_headers()
                    return
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{src.size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                pos = start
                try:
                    while pos <= end and not outer.stop_event.is_set():
                        data = outer.read(src, pos, min(1024 * 1024, end - pos + 1))
                        if not data:
                            break
                        self.wfile.write(data)
                        pos += len(data)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="stream-http").start()
        for i in range(self.cfg["workers"]):
            threading.Thread(target=self._worker, daemon=True, name=f"stream-fetch-{i}").start()
        threading.Thread(target=self._policy_loop, daemon=True, name="stream-policy").start()
        self.started = True
        self.log(f"[stream] read-ahead server on 127.0.0.1:{self.port}; files of {self.cfg['min_bytes'] // 1048576} MB or more on a "
                 f"share are read through it (RAM {self.ram_dir}" + (f", disk {self.disk_dir} up to {self.cfg['disk_bytes'] // 1024 ** 3} GB" if self.disk_dir else "") + ")")
        return self.port

    def stop(self):
        self.stop_event.set()
        with self.qcond:
            self.qcond.notify_all()
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    # ---- which files
    def qualifies(self, path, size=None, mount=None):
        """Is this file one to read through the server: big enough, and on a network share."""
        if not self.enabled or not self.started:
            return False
        try:
            size = os.stat(path).st_size if size is None else size
        except OSError:
            return False
        if size < self.cfg["min_bytes"]:
            return False
        if self.cfg.get("any_fs"):
            return True
        if mount is None:
            from prefetch import mount_type
            mount = mount_type(path)
        return mount in NETWORK_FS or (mount or "").startswith("fuse.")

    # ---- sources
    def _source(self, path):
        with self.lock:
            sid = self.by_path.get(path)
            if sid and sid in self.sources and not self.sources[sid].failed:
                return self.sources[sid]
            size = os.stat(path).st_size
            rate = self.rate_of(path) or DEFAULT_RATE
            from prefetch import share_key
            src = Source(secrets.token_urlsafe(12), path, size, float(rate), share_key(path, self.media_root),
                         os.path.splitext(path)[1][:8])
            self.sources[src.id] = src
            self.by_path[path] = src.id
            return src

    def open(self, path, seek_seconds=0.0):
        """Start playing `path` from `seek_seconds`: a URL for ffmpeg, or None (read the file directly)."""
        if not self.enabled or not self.started:
            return None
        try:
            src = self._source(path)
        except OSError:
            return None
        with src.cond:
            src.refs += 1
            src.last_used = self.now()
            src.seek_byte = self._byte_at(src, seek_seconds)
            src.playhead = src.seek_byte
        self._want_around(src, high=True)
        return f"http://127.0.0.1:{self.port}/s/{src.id}{src.ext}"

    def close(self, url):
        sid = (url or "").rsplit("/", 1)[-1].split(".", 1)[0]
        with self.lock:
            src = self.sources.get(sid)
        if src is None:
            return
        with src.cond:
            src.refs = max(0, src.refs - 1)
            src.last_used = self.now()

    def prepare(self, path, seek_seconds, due_at):
        """A piece that will play `path` from `seek_seconds` is due at `due_at` (a time.time() value): have its head ready."""
        if not self.enabled or not self.started:
            return
        try:
            src = self._source(path)
        except OSError:
            return
        with src.cond:
            src.due = due_at
            src.seek_byte = self._byte_at(src, seek_seconds)
            if src.refs == 0:
                src.playhead = src.seek_byte
            src.last_used = self.now()

    def _byte_at(self, src, seconds):
        return max(0, min(src.size - 1, int(seconds * src.rate)))

    def mark_failed(self, url):
        sid = (url or "").rsplit("/", 1)[-1].split(".", 1)[0]
        with self.lock:
            src = self.sources.get(sid)
        if src is not None:
            src.failed = "reads failed"
            self._drop_all(src)

    # ---- reading (the server's side)
    def read(self, src, offset, length):
        """`length` bytes of the file at `offset` (fewer at the end of the file), waiting for them to arrive."""
        if offset >= src.size:
            return b""
        length = min(length, src.size - offset)
        out = []
        deadline = self.now() + WAIT_SECONDS
        pos = offset
        while length > 0:
            b = pos // BLOCK
            with src.cond:
                src.playhead = pos
                src.last_used = self.now()
                while b not in src.blocks:
                    if src.failed:
                        raise OSError(src.failed)
                    self._queue(src, b, priority=(0, 0, b))          # a reader is waiting: before anything pulled ahead
                    if self.now() > deadline:
                        raise TimeoutError(f"{os.path.basename(src.path)}: block {b} did not arrive in {WAIT_SECONDS:.0f}s")
                    src.cond.wait(0.5)
                tier, name = src.blocks[b]
            data = self._read_block(tier, name, pos - b * BLOCK, min(length, BLOCK - (pos - b * BLOCK)))
            out.append(data)
            pos += len(data)
            length -= len(data)
            if not data:
                break
        return b"".join(out)

    def _read_block(self, tier, name, inner, n):
        folder = self.ram_dir if tier == "ram" else self.disk_dir
        with open(os.path.join(folder, name), "rb") as f:
            f.seek(inner)
            return f.read(n)

    # ---- fetching (the share's side)
    def _queue(self, src, block, priority):
        with self.qcond:
            heapq.heappush(self.queue, (priority, next(self.seq), src.id, block))
            self.qcond.notify()

    def _worker(self):
        while not self.stop_event.is_set():
            with self.qcond:
                while not self.queue and not self.stop_event.is_set():
                    self.qcond.wait(1.0)
                if self.stop_event.is_set():
                    return
                priority, _n, sid, block = heapq.heappop(self.queue)
            with self.lock:
                src = self.sources.get(sid)
            if src is None or src.failed:
                continue
            with src.cond:
                if block in src.blocks or block in src.inflight:
                    continue
                src.inflight.add(block)
            try:
                if priority[0] != 0:                             # pulling ahead is paced; a read someone waits for is not
                    self._pace(src.share, BLOCK)
                self._fetch(src, block)
            except Exception as e:                               # a failed fetch is retried by the next ask
                self.log(f"[stream] {os.path.basename(src.path)}: block {block}: {e}")
                time.sleep(0.5)
            finally:
                with src.cond:
                    src.inflight.discard(block)
                    src.cond.notify_all()

    def _pace(self, share, nbytes):
        speed = self.stats.speed_of(share)
        rate = max(speed * self.cfg["share_fraction"], 1024 * 1024)
        with self.lock:
            bucket = self.buckets.setdefault(share, _Bucket(self.now))
            wait = bucket.wait_time(nbytes, rate)
        if wait > 0:
            self.stop_event.wait(min(wait, 5.0))

    def _fetch(self, src, block):
        began = self.now()
        with self.opener(src.path, "rb") as f:
            f.seek(block * BLOCK)
            data = f.read(min(BLOCK, src.size - block * BLOCK))
        took = max(self.now() - began, 1e-6)
        if len(data) >= 1024 * 1024:
            self.stats.record_speed(src.share, len(data) / took)
        tier = self._place(len(data))
        name = f"{src.id}-{block}.blk"
        folder = self.ram_dir if tier == "ram" else self.disk_dir
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)
        with src.cond:
            src.blocks[block] = (tier, name)
            self.used[tier] += len(data)
            src.cond.notify_all()

    def _place(self, nbytes):
        """RAM first, then the disk folder. A block that has to be stored when neither has room (a reader is waiting for it)
        goes to the disk folder, or to RAM, over budget: the policy drops what is farthest the next time round."""
        with self.lock:
            if self.ram_dir and self.used["ram"] + nbytes <= self.ram_free():
                return "ram"
            if self.disk_dir and self.used["disk"] + nbytes <= self.cfg["disk_bytes"]:
                return "disk"
            return "disk" if self.disk_dir else "ram"

    # ---- the policy
    def _want_around(self, src, high=False):
        """Queue what the playing position needs next, at most `need` bytes ahead of it."""
        speed = self.stats.speed_of(src.share)
        with src.cond:
            ahead = need_bytes(src.size - src.playhead, src.rate, speed)
            first = src.block_of(src.playhead)
            last = src.block_of(min(src.size - 1, src.playhead + ahead))
            todo = [b for b in range(first, last + 1) if b not in src.blocks and b not in src.inflight]
            todo += [b for b in self._index_blocks(src) if b not in src.blocks and b not in src.inflight]
        for i, b in enumerate(todo):
            self._queue(src, b, priority=(1 if not high else 0.5, 0, i))

    @staticmethod
    def _index_blocks(src):
        n = max(1, INDEX_BYTES // BLOCK)
        return sorted(set(range(0, min(n, src.nblocks))) | set(range(max(0, src.nblocks - n), src.nblocks)))

    def policy_once(self):
        """Work out, for every source in the order they are due, what to hold; queue it, drop what is behind or not wanted."""
        now = self.now()
        with self.lock:
            sources = list(self.sources.values())
        budget_total = (self.ram_free() if self.ram_dir else 0) + (self.cfg["disk_bytes"] if self.disk_dir else 0)
        allotted = 0

        def due_in(s):
            if s.refs:
                return -1.0
            return (s.due - now) if s.due is not None else 1e9
        for src in sorted(sources, key=due_in):
            if src.failed:
                continue
            speed = self.stats.speed_of(src.share)
            delay = self.stats.delay_of(src.share)
            with src.cond:
                playing = src.refs > 0
                ahead = need_bytes(src.size - src.playhead, src.rate, speed)
                src.need = ahead
                pinned_bytes = sum(1 for _ in self._index_blocks(src)) * BLOCK
                grant = max(0, min(ahead + pinned_bytes, budget_total - allotted))
                src.short = max(0, ahead + pinned_bytes - grant)
                allotted += grant
                if not playing:
                    if src.due is None or src.due < now - 30:
                        # nobody plays it and nothing is waiting for it: let go of it after a while
                        if now - src.last_used > 60:
                            self._drop_all(src)
                        continue
                    lead = delay + ahead / max(speed * self.cfg["share_fraction"], 1.0)
                    if (src.due - now) > lead * MARGIN + CUSHION:
                        continue                         # not yet: it would sit in memory for nothing
                first = src.block_of(src.playhead)
                window = max(1, math.ceil(grant / BLOCK))
                last = min(src.nblocks - 1, first + window - 1)
                keep = set(range(first, last + 1)) | set(self._index_blocks(src))
                if playing:                              # a big file being played: what is behind the playing position goes
                    keep_behind = max(0, first - 1)
                    for b in [b for b in src.blocks if b < keep_behind and b not in self._index_blocks(src)]:
                        self._drop_block(src, b)
                todo = [b for b in sorted(keep) if b not in src.blocks and b not in src.inflight]
            for b in todo[:64]:
                self._queue(src, b, priority=(1, max(due_in(src), 0.0), b))
        # too much held for the budget (a block that had to be stored over it): drop the farthest of what is not playing
        over = sum(self.used.values()) - budget_total
        if over > 0:
            for src in sorted(sources, key=due_in, reverse=True):
                for b in sorted(src.blocks, reverse=True):
                    if over <= 0:
                        break
                    if src.refs and b <= src.block_of(src.playhead) + 1:
                        continue
                    over -= BLOCK
                    self._drop_block(src, b)

    def _policy_loop(self):
        while not self.stop_event.is_set():
            try:
                self.policy_once()
            except Exception as e:
                self.log(f"[stream] policy pass failed: {e}")
            self.stop_event.wait(1.0)

    # ---- dropping
    def _drop_block(self, src, block):
        with src.cond:
            got = src.blocks.pop(block, None)
        if got is None:
            return
        tier, name = got
        folder = self.ram_dir if tier == "ram" else self.disk_dir
        try:
            size = os.path.getsize(os.path.join(folder, name))
            os.unlink(os.path.join(folder, name))
        except OSError:
            size = 0
        with self.lock:
            self.used[tier] = max(0, self.used[tier] - size)

    def _drop_all(self, src):
        for b in list(src.blocks):
            self._drop_block(src, b)
        with self.lock:
            if src.id in self.sources and not src.refs:
                self.sources.pop(src.id, None)
                if self.by_path.get(src.path) == src.id:
                    self.by_path.pop(src.path, None)

    # ---- what is going on
    def status(self):
        out = {"enabled": self.enabled, "min_mb": self.cfg["min_bytes"] // 1048576,
               "ram_mb": round(self.used["ram"] / 1048576), "disk_mb": round(self.used["disk"] / 1048576),
               "disk_cap_gb": round(self.cfg["disk_bytes"] / 1024 ** 3) if self.disk_dir else 0, "files": []}
        with self.lock:
            sources = list(self.sources.values())
        for s in sources:
            speed = self.stats.speed_of(s.share)
            have = sum(BLOCK for b in s.blocks if b >= s.block_of(s.playhead))
            out["files"].append({
                "file": os.path.basename(s.path), "state": "playing" if s.refs else ("preparing" if s.due else "idle"),
                "size_mb": round(s.size / 1048576), "rate_mbps": round(s.rate * 8 / 1e6, 1),
                "share_mb_s": round(speed / 1048576, 1), "wanted_mb": round(s.need / 1048576),
                "have_mb": round(min(have, s.size) / 1048576), "short_mb": round(s.short / 1048576),
                "streams_live": speed >= s.rate})
        return out
