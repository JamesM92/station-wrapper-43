"""Read ahead for the channels being watched, so a slow network share does not make a viewer wait.

The tuner starts an ffmpeg for each file the moment it is due, and each one has to open its file over the share first.
On a slow or busy SMB share that takes seconds, and a viewer has about three seconds of buffer. This copies the files
that are about to be needed into a local cache BEFORE they are due, and playback then reads the copy (a seekable
local file, so joining a file part way works as before).

What is kept ready, for each channel that has a viewer:
    the file playing now, if it is needed again after the break (an episode is split around its breaks), and
    the next group: the commercial break while a show plays, or the next episode while a break plays.
So the break gives the next episode time to load, and the show gives the next break time to load.

How early to start is learned. For each share (a folder under the media root) it keeps the time the first bytes take
and the sustained speed of its own copies, as the pessimistic end of recent samples, in the config volume. A file's
expected time is delay + size / speed, times a safety margin. Until it has measured a share it assumes a slow one.

The cache lives in RAM by default (a tmpfs mount, `/cache`): nothing is written to a drive. PREFETCH_RAM=0 lets it use
a folder on disk instead; PREFETCH=0 turns the whole thing off. The share is only read.
"""
import hashlib
import json
import os
import threading
import time
from collections import deque

import resolver

CHUNK = 4 * 1024 * 1024
SAMPLES = 20                                 # recent measurements kept per share
SMALL = 8 * 1024 * 1024                      # a file this small says nothing reliable about speed: only its delay counts
ASSUMED_DELAY = 3.0                          # seconds, for a share not measured yet
ASSUMED_BPS = 5 * 1024 * 1024                # bytes per second, for a share not measured yet
MARGIN = 1.5                                 # start this many times earlier than the expected time
CUSHION = 3.0                                # and this many seconds more
BREAK_TYPES = ("bump", "commercial")
MAX_PLAN_ENTRIES = 80


def env_config(env=None):
    e = os.environ if env is None else env

    def num(name, default):
        try:
            return float(e.get(name, "") or default)
        except ValueError:
            return float(default)
    off = ("0", "false", "no", "off")
    return {
        "enabled": e.get("PREFETCH", "1").strip().lower() not in off,
        "dir": e.get("PREFETCH_DIR", "/cache"),
        "ram_only": e.get("PREFETCH_RAM", "1").strip().lower() not in off,
        "max_bytes": int(num("PREFETCH_MAX_MB", 6144) * 1024 * 1024),
        "depth": max(1, int(num("PREFETCH_DEPTH", 1))),
        "stats_path": e.get("PREFETCH_STATS", "/config/prefetch-stats.json"),
        "copies_per_share": max(1, int(num("PREFETCH_COPIES", 1))),
    }


def mount_type(path):
    """The filesystem type of the mount that holds `path` ('tmpfs', 'ext4', ...), or None."""
    try:
        real, best, kind = os.path.realpath(path), "", None
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mp = parts[1].replace("\\040", " ")
                    if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                        best, kind = mp, parts[2]
        return kind
    except OSError:
        return None


def share_key(path, media_root="/mnt/media"):
    """The share a file is on: its folder just under the media root (/mnt/media/anime), else its first folders."""
    path = os.path.normpath(path)
    root = os.path.normpath(media_root)
    if path.startswith(root + os.sep):
        first = path[len(root) + 1:].split(os.sep)[0]
        return os.path.join(root, first)
    return os.sep.join(path.split(os.sep)[:3])


class Stats:
    """What is known of each share's speed. Pessimistic on purpose: a slow sample counts, a lucky fast one does not."""

    def __init__(self, path=None, now=time.time):
        self.path, self.now = path, now
        self.lock = threading.Lock()
        self.delay = {}                       # share -> deque of seconds to the first bytes
        self.speed = {}                       # share -> deque of bytes per second (large files only)
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            for key, v in data.get("shares", {}).items():
                self.delay[key] = deque(v.get("delay", [])[-SAMPLES:], maxlen=SAMPLES)
                self.speed[key] = deque(v.get("speed", [])[-SAMPLES:], maxlen=SAMPLES)
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def save(self):
        if not self.path:
            return
        with self.lock:
            blob = {"shares": {k: {"delay": list(self.delay.get(k, [])), "speed": list(self.speed.get(k, []))}
                               for k in set(self.delay) | set(self.speed)}}
        try:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(blob, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def record(self, share, size, delay, seconds):
        """One finished copy: `delay` to the first bytes, `seconds` for all of it (from the first bytes)."""
        with self.lock:
            self.delay.setdefault(share, deque(maxlen=SAMPLES)).append(max(0.0, delay))
            if size >= SMALL and seconds > 0:
                self.speed.setdefault(share, deque(maxlen=SAMPLES)).append(size / seconds)

    @staticmethod
    def _pick(samples, quantile):
        s = sorted(samples)
        return s[min(len(s) - 1, int(quantile * len(s)))]

    def expected(self, share, size):
        """Seconds a file of `size` bytes is expected to take to load, on the pessimistic side."""
        with self.lock:
            d, v = self.delay.get(share), self.speed.get(share)
            delay = self._pick(d, 0.75) if d else ASSUMED_DELAY
            bps = self._pick(v, 0.25) if v else ASSUMED_BPS
        return delay + size / max(bps, 1.0)

    def summary(self):
        with self.lock:
            out = {}
            for k in sorted(set(self.delay) | set(self.speed)):
                d, v = self.delay.get(k), self.speed.get(k)
                out[k] = {"delay_s": round(self._pick(d, 0.75), 2) if d else None,
                          "mb_per_s": round(self._pick(v, 0.25) / 1048576, 1) if v else None,
                          "samples": len(d or [])}
            return out


class Cache:
    """Copies of files, in one folder, within a size budget. Only copies that are wanted or playing are kept safe."""

    def __init__(self, folder, max_bytes, ram_only=True, now=time.time):
        self.folder, self.max_bytes, self.ram_only, self.now = folder, max_bytes, ram_only, now
        self.lock = threading.Lock()
        self.items = {}                       # source path -> {"file", "size", "used", "pins"}
        self.reserved = 0                     # bytes promised to copies in progress
        os.makedirs(folder, exist_ok=True)
        for name in os.listdir(folder):       # whatever an earlier run left is not trusted: start empty
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass

    def _name(self, src):
        return hashlib.sha1(src.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:20] + os.path.splitext(src)[1][:8]

    def used_bytes(self):
        with self.lock:
            return sum(i["size"] for i in self.items.values()) + self.reserved

    def lookup(self, src):
        """The local copy of src, if there is a complete one."""
        with self.lock:
            item = self.items.get(src)
            if item is None:
                return None
            item["used"] = self.now()
            return item["file"]

    def pin(self, src):
        """The copy of src for playback, kept safe until unpin(). (path, True) for a copy, (src, False) otherwise."""
        with self.lock:
            item = self.items.get(src)
            if item is None:
                return src, False
            item["pins"] += 1
            item["used"] = self.now()
            return item["file"], True

    def unpin(self, src):
        with self.lock:
            item = self.items.get(src)
            if item is not None and item["pins"] > 0:
                item["pins"] -= 1

    def make_room(self, size, keep):
        """Free space for `size` more bytes by dropping the oldest copies that are not playing and not in `keep`.
        True if it now fits (the room is reserved), False if it can't."""
        with self.lock:
            used = sum(i["size"] for i in self.items.values()) + self.reserved
            if used + size > self.max_bytes:
                for src, item in sorted(self.items.items(), key=lambda kv: kv[1]["used"]):
                    if used + size <= self.max_bytes:
                        break
                    if item["pins"] or src in keep:
                        continue
                    self._drop(src)
                    used -= item["size"]
            if used + size > self.max_bytes:
                return False
            self.reserved += size
            return True

    def _drop(self, src):
        item = self.items.pop(src, None)
        if item:
            try:
                os.unlink(item["file"])
            except OSError:
                pass

    def finish(self, src, tmp, size, reserved):
        """A copy is complete: make it the real one."""
        final = os.path.join(self.folder, self._name(src))
        with self.lock:
            self.reserved = max(0, self.reserved - reserved)
            os.replace(tmp, final)
            self.items[src] = {"file": final, "size": size, "used": self.now(), "pins": 0}

    def abandon(self, tmp, reserved):
        with self.lock:
            self.reserved = max(0, self.reserved - reserved)
        try:
            os.unlink(tmp)
        except OSError:
            pass

    def temp_name(self, src):
        return os.path.join(self.folder, self._name(src) + ".part")

    def summary(self):
        with self.lock:
            return {"files": len(self.items), "used_mb": round(sum(i["size"] for i in self.items.values()) / 1048576),
                    "playing": sum(1 for i in self.items.values() if i["pins"]), "max_mb": round(self.max_bytes / 1048576)}


def copy_file(src, dst, should_stop, chunk=CHUNK):
    """Copy src to dst in chunks. Returns (bytes, delay to the first chunk, seconds for the rest), or None if stopped."""
    began = time.time()
    first_at = None
    total = 0
    with open(src, "rb", buffering=0) as fin, open(dst, "wb") as fout:
        while True:
            if should_stop():
                return None
            data = fin.read(chunk)
            if first_at is None:
                first_at = time.time()
            if not data:
                break
            fout.write(data)
            total += len(data)
    end = time.time()
    return total, (first_at - began), max(1e-6, end - first_at)


def upcoming(db_path, station, now_dt, limit=MAX_PLAN_ENTRIES, horizon_s=4 * 3600):
    """The entries after the one airing now, in order: [{"path", "start" (datetime), "seconds", "type", "stream", "seek"}],
    with the one airing now first. Reads the schedule the way the tuner does."""
    from datetime import timedelta
    out, when = [], now_dt
    for _ in range(limit):
        try:
            cur = resolver.resolve(db_path, station, when)
        except Exception:
            break
        e = cur.entry
        out.append({"path": e.path, "start": when, "seconds": cur.remaining, "type": e.content_type,
                    "stream": e.is_stream, "seek": cur.seek})
        when = when + timedelta(seconds=max(cur.remaining, 0.5) + 0.01)       # just past the end: the next entry
        if (when - now_dt).total_seconds() > horizon_s:
            break
    return out


def groups(entries):
    """Entries -> consecutive groups of the same kind: [("break" or "program", [entries])]."""
    out = []
    for e in entries:
        kind = "break" if e["type"] in BREAK_TYPES else "program"
        if out and out[-1][0] == kind:
            out[-1][1].append(e)
        else:
            out.append((kind, [e]))
    return out


def wanted(entries, depth=1):
    """What to have ready, in order of need: [{"path", "start", "why", "gid"}] for the next `depth` groups, plus the
    file playing now if it is needed again later (an episode split around a break). Every file of a group is due when
    the GROUP starts, so a whole commercial break is ready before the break begins."""
    g = groups([e for e in entries if not e["stream"]])
    if not g:
        return []
    out, seen = [], set()
    later = {e["path"] for _k, es in g[1:depth + 2] for e in es}
    for e in g[0][1]:
        if e["path"] in later and e["path"] not in seen:
            seen.add(e["path"])
            out.append({"path": e["path"], "start": e["start"], "why": "needed again", "gid": 0})
    for gid, (_k, es) in enumerate(g[1:depth + 1], 1):
        for e in es:
            if e["path"] not in seen:
                seen.add(e["path"])
                out.append({"path": e["path"], "start": es[0]["start"], "why": "next", "gid": gid})
    return out


class Prefetcher:
    """The read-ahead for every watched channel. `watched()` -> {channel number: station}; `db()` -> the schedule path."""

    def __init__(self, cfg, db, watched, media_root="/mnt/media", log=print, now=time.time, stats=None, path_of=None):
        self.cfg, self.db, self.watched, self.media_root, self.log, self.now = cfg, db, watched, media_root, log, now
        self.path_of = path_of or (lambda p: p)       # a plan's path -> the file's real path (the tuner's media_path)
        self.enabled, self.why_not, self.cache = False, "", None
        self.stats = stats or Stats(cfg.get("stats_path"), now)
        self.lock = threading.Lock()
        self.copying = {}                    # share -> number of copies running
        self.current = {}                    # source path -> True while being copied
        self.stop_event = threading.Event()
        self.plans = {}                      # channel -> the last wanted() list, for the status page
        self._start_cache()

    def _start_cache(self):
        cfg = self.cfg
        if not cfg["enabled"]:
            self.why_not = "switched off (PREFETCH=0)"
            return
        folder = cfg["dir"]
        if cfg["ram_only"]:
            kind = mount_type(folder) if os.path.isdir(folder) else None
            if kind not in ("tmpfs", "ramfs"):
                self.why_not = (f"{folder} is not a RAM disk. Add a tmpfs mount for it to the service "
                                f"(tmpfs: - {folder}:size=6g), or set PREFETCH_RAM=0 to use a folder on disk, or PREFETCH=0 to turn this off")
                return
        try:
            self.cache = Cache(folder, self._budget(), cfg["ram_only"], self.now)
        except OSError as e:
            self.why_not = f"cannot use {folder}: {e.strerror or e}"
            return
        self.enabled = True

    def _budget(self):
        want = self.cfg["max_bytes"]
        try:
            st = os.statvfs(self.cfg["dir"])
            return int(min(want, st.f_blocks * st.f_frsize * 0.9))      # never more than 90% of the folder's own disk
        except OSError:
            return want

    # ---- used by playback
    def pin(self, path):
        """The path to play for `path`: its local copy when there is one (and it is kept safe), else `path` itself."""
        return self.cache.pin(path) if self.enabled else (path, False)

    def unpin(self, path):
        if self.enabled:
            self.cache.unpin(path)

    # ---- the planning
    def plan_once(self):
        """Look at every watched channel once and start the copies that are due. Returns the paths started."""
        from datetime import datetime
        started, keep, batches = [], set(), []
        for number, station in dict(self.watched()).items():
            try:
                entries = upcoming(self.db, station, datetime.now())
                for e in entries:
                    e["path"] = self.path_of(e["path"])
            except Exception:
                continue
            want = wanted(entries, self.cfg["depth"])
            self.plans[number] = [{"path": os.path.basename(w["path"]), "why": w["why"]} for w in want]
            keep.update(w["path"] for w in want)
            by_group = {}
            for w in want:
                by_group.setdefault(w["gid"], []).append(w)
            for gid, ws in by_group.items():
                batches.append((ws[0]["start"], ws))
        batches.sort(key=lambda b: b[0])
        queued = {}                                  # share -> seconds of copying already ahead of the next group
        for start, ws in batches:
            missing = []
            for w in ws:
                path = w["path"]
                if self.cache.lookup(path) or path in self.current:
                    continue
                try:
                    size = os.stat(path).st_size
                except OSError:
                    continue
                missing.append((path, size, share_key(path, self.media_root)))
            if not missing:
                continue
            ahead, mine = dict(queued), {}
            for path, size, share in missing:        # the group's own files, one after the other on each share
                mine[share] = mine.get(share, 0.0) + self.stats.expected(share, size)
            finish = max(ahead.get(s, 0.0) + secs for s, secs in mine.items())
            seconds_left = (start - datetime.now()).total_seconds()
            for s, secs in mine.items():
                queued[s] = ahead.get(s, 0.0) + secs
            if seconds_left > finish * MARGIN + CUSHION:
                continue                              # not yet: it would sit in RAM for no reason
            for path, size, share in missing:        # the whole group, in order (the shares' limits decide how many at once)
                if self._start_copy(path, size, share, keep):
                    started.append(path)
        return started

    def _start_copy(self, path, size, share, keep):
        with self.lock:
            if self.copying.get(share, 0) >= self.cfg["copies_per_share"] or path in self.current:
                return False
            if not self.cache.make_room(size, keep):
                return False
            self.copying[share] = self.copying.get(share, 0) + 1
            self.current[path] = True
        threading.Thread(target=self._copy, args=(path, size, share), daemon=True, name="prefetch-copy").start()
        return True

    def _copy(self, path, size, share):
        tmp = self.cache.temp_name(path)
        done = False
        try:
            got = copy_file(path, tmp, self.stop_event.is_set)
            if got is not None:
                total, delay, seconds = got
                self.cache.finish(path, tmp, total, size)
                self.stats.record(share, total, delay, seconds)
                self.stats.save()
                done = True
        except OSError as e:
            self.log(f"[prefetch] could not copy {os.path.basename(path)}: {e.strerror or e}")
        finally:
            if not done:
                self.cache.abandon(tmp, size)
            with self.lock:
                self.copying[share] = max(0, self.copying.get(share, 1) - 1)
                self.current.pop(path, None)

    def loop(self, every=2.0, sleep=time.sleep):
        while not self.stop_event.is_set():
            try:
                self.plan_once()
            except Exception as e:                  # nothing may stop the read-ahead for good
                self.log(f"[prefetch] pass failed: {e}")
            sleep(every)

    def status(self):
        out = {"enabled": self.enabled, "ram": bool(self.cfg["ram_only"]), "dir": self.cfg["dir"]}
        if not self.enabled:
            out["why_not"] = self.why_not
            return out
        out.update(cache=self.cache.summary(), shares=self.stats.summary(), copying=sum(self.copying.values()),
                   channels={str(k): v for k, v in self.plans.items()})
        return out
