"""Even out the volume of shows and commercials.

Every video is measured once (EBU R128 integrated loudness and true peak, audio only) and the result is remembered. When a video
is encoded for a channel, one fixed gain is applied to bring it to the target, with a limiter behind it. A fixed gain, not a
compressor on the live stream: a 30 second commercial gives a live filter no time to settle, a join part way in has no look-ahead,
and a compressor flattens films. A file that has not been measured yet plays as it is, and is measured in the background.

What may be read is decided by the caller (tuner.py); nothing here takes a path from a browser.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque

DEFAULT_TARGET = -23.0          # LUFS: broadcast television (EBU R128); streaming sites use -14 to -16
TARGET_RANGE = (-30.0, -10.0)
MAX_BOOST = 12.0                # dB: a very quiet file is not raised further than this
MAX_CUT = -20.0
PEAK_ROOM = 3.0                 # dB over -1 dBTP the gain may push a peak to before the limiter has to work
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".ts", ".webm", ".wmv", ".flv"}
MEASURE_TIMEOUT = 3600


def parse_ebur128(text):
    """(integrated LUFS, true peak dBFS) from the end-of-run summary of ffmpeg's ebur128 filter; either may be None."""
    block = text.rsplit("Summary:", 1)[-1]
    lufs = re.search(r"I:\s+(-?\d+(?:\.\d+)?|-inf)\s+LUFS", block)
    peak = re.search(r"Peak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS", block)

    def num(m):
        if not m or m.group(1) == "-inf":
            return None
        return float(m.group(1))
    return num(lufs), num(peak)


def gain_db(lufs, peak, target=DEFAULT_TARGET):
    """The gain, in dB, that brings a file of this loudness to the target: never more than MAX_BOOST, never more than takes
    its peak PEAK_ROOM over -1 dBTP (the limiter is for a stray peak, not for squashing a dynamic film), None when it has no
    measurable audio."""
    if lufs is None:
        return None
    gain = target - lufs
    if peak is not None:
        gain = min(gain, (-1.0 - peak) + PEAK_ROOM)
    return round(max(MAX_CUT, min(MAX_BOOST, gain)), 2)


def filter_for(gain):
    """The ffmpeg audio filter for a gain, or None when there is nothing to do (within a quarter of a dB)."""
    if gain is None or abs(gain) < 0.25:
        return None
    return f"volume={gain:+.2f}dB,alimiter=limit=0.891:attack=5:release=60:level=disabled"


def measure(path, timeout=MEASURE_TIMEOUT, low_priority=True):
    """Measure one file: {"lufs", "peak"} (a value is None for a file with no audio), or raises OSError / RuntimeError."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", path, "-vn", "-sn", "-dn",
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    kwargs = {"preexec_fn": (lambda: os.nice(15))} if low_priority and hasattr(os, "nice") else {}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError("measuring took too long")
    if r.returncode != 0 and "Summary:" not in r.stderr:
        raise RuntimeError("could not read the audio")
    lufs, peak = parse_ebur128(r.stderr)
    return {"lufs": lufs, "peak": peak}


class Levels:
    """The remembered loudness of videos, the work of measuring the ones that are not, and the filter for a file."""

    def __init__(self, store_path, lists_dir=None, target_fn=None, measure_fn=measure, sweep_every=6 * 3600,
                 first_sweep=90.0, pause=0.5):
        self.path = store_path
        self.lists_dir = lists_dir
        self.target_fn = target_fn or (lambda: DEFAULT_TARGET)
        self.measure = measure_fn
        self.sweep_every, self.first_sweep, self.pause = sweep_every, first_sweep, pause
        self.lock = threading.Lock()
        self.known = {}                         # realpath -> {"size", "mtime", "lufs", "peak"}
        self.queue = deque()
        self.queued = set()
        self.failed = {}                        # realpath -> when it failed (not tried again for a day)
        self.current = None
        self.dirty = False
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self._load()

    # -- the store
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.known = {k: v for k, v in data.items() if isinstance(v, dict) and "size" in v and "mtime" in v}
        except (OSError, ValueError):
            self.known = {}

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps(self.known)
            self.dirty = False
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".loudness-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError:
            with self.lock:
                self.dirty = True

    @staticmethod
    def _key(path):
        return os.path.realpath(path)

    def _fresh(self, key):
        """The remembered answer for a file, if the file is still the one that was measured."""
        rec = self.known.get(key)
        if rec is None:
            return None
        try:
            st = os.stat(key)
        except OSError:
            return None
        if st.st_size != rec.get("size") or abs(st.st_mtime - rec.get("mtime", 0)) > 1.0:
            return None
        return rec

    # -- what the encoder asks
    def filter_for(self, path):
        """The audio filter for this file, or None (not measured yet, no audio, or already at the target). A file that is
        not measured is put at the front of the queue."""
        key = self._key(path)
        with self.lock:
            rec = self._fresh(key)
        if rec is None:
            self.submit(path, front=True)
            return None
        return filter_for(gain_db(rec.get("lufs"), rec.get("peak"), self.target_fn()))

    def submit(self, path, front=False):
        key = self._key(path)
        with self.lock:
            if key in self.queued or key == self.current:
                return
            if time.time() - self.failed.get(key, 0) < 86400:
                return
            if self._fresh(key) is not None:
                return
            if front:
                self.queue.appendleft(key)
            else:
                self.queue.append(key)
            self.queued.add(key)
        self.wake.set()

    def status(self):
        with self.lock:
            measured = sum(1 for k in self.known)
            return {"target": self.target_fn(), "measured": measured, "queued": len(self.queue),
                    "measuring": os.path.basename(self.current) if self.current else None, "failed": len(self.failed)}

    # -- the work
    def sweep(self, limit=50000):
        """Queue every video in the lists that has no fresh measurement."""
        if not self.lists_dir or not os.path.isdir(self.lists_dir):
            return 0
        n = 0
        for root, dirs, files in os.walk(self.lists_dir, followlinks=True):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith(".") or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                    continue
                self.submit(os.path.join(root, name))
                n += 1
                if n >= limit:
                    return n
        return n

    def work_once(self):
        """Measure the next queued file. Returns True when there was one."""
        with self.lock:
            if not self.queue:
                return False
            key = self.queue.popleft()
            self.queued.discard(key)
            self.current = key
        try:
            st = os.stat(key)
            got = self.measure(key)
            rec = {"size": st.st_size, "mtime": st.st_mtime, "lufs": got.get("lufs"), "peak": got.get("peak")}
            with self.lock:
                self.known[key] = rec
                self.dirty = True
        except (OSError, RuntimeError):
            with self.lock:
                self.failed[key] = time.time()
        finally:
            with self.lock:
                self.current = None
        return True

    def loop(self):
        next_sweep = time.time() + self.first_sweep
        last_save = time.time()
        while not self.stop_event.is_set():
            try:
                if time.time() >= next_sweep:
                    self.sweep()
                    next_sweep = time.time() + self.sweep_every
                worked = self.work_once()
                if time.time() - last_save > 30 or (not worked and self.dirty):
                    self.save()
                    last_save = time.time()
            except Exception as e:                          # nothing may stop the measuring for good
                print(f"[loudness] pass failed: {e}", flush=True)
                worked = False
            if worked:
                self.stop_event.wait(self.pause)
            else:
                self.wake.wait(30)
                self.wake.clear()
