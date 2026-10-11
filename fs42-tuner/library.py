"""Read-only view of the FieldStation42 content folder (catalog/by-genre).

Each folder under it is a tag: FieldStation42 schedules by tag. This reports,
per tag, how many videos it holds, where they came from, and two things worth a
look: links whose target is gone ("dead") and the same file linked twice in one
tag (FieldStation42 would schedule it twice). Nothing is written.

"Dead" is judged from where this runs. Links hold container paths, so a path
this container doesn't mount reads as dead here even when FieldStation42's
container can see it.
"""
import json
import os
import threading
import time

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
SKIP_TAGS = {"bump"}          # bump clips are interstitials, not a tag you schedule
LOOSE = "(loose files)"
CACHE_SECONDS = 120

_cache = {"root": None, "at": 0.0, "data": None, "took": 0.0}
_lock = threading.Lock()



def source_name(rel_dirs):
    """Where a video came from, from the folders between its tag and the file.

    The generator lays genre tags out as <library>/<show>/..., so the first two
    folders name the show. Anything shallower (a commercials playlist, a flat
    folder) is named by its first folder, and files straight in the tag are loose.
    """
    return "/".join(rel_dirs[:2]) if rel_dirs else LOOSE


def scan_tag(tag_dir):
    videos = dead = 0
    sources, by_real = {}, {}
    stack = [(tag_dir, [])]
    while stack:
        path, rel_dirs = stack.pop()
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append((e.path, rel_dirs + [e.name]))
                continue
            if os.path.splitext(e.name)[1].lower() not in VIDEO_EXT:
                continue
            videos += 1
            name = source_name(rel_dirs)
            sources[name] = sources.get(name, 0) + 1
            if e.is_symlink():
                if not os.path.exists(e.path):
                    dead += 1
                    continue
                by_real.setdefault(os.path.realpath(e.path), []).append(e.path)
            else:
                by_real.setdefault(e.path, []).append(e.path)
    duplicates = sum(len(v) - 1 for v in by_real.values() if len(v) > 1)
    return {
        "videos": videos,
        "dead": dead,
        "duplicates": duplicates,
        "sources": [{"name": n, "videos": c} for n, c in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def scan(root):
    """Every tag under root, busiest first. A missing root is reported, not raised."""
    if not os.path.isdir(root):
        return {"found": False, "root": root, "tags": [], "totals": {"videos": 0, "dead": 0, "duplicates": 0}}
    tags = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if name.startswith(".") or name in SKIP_TAGS or not os.path.isdir(path):
            continue
        tags.append({"tag": name, **scan_tag(path)})
    tags.sort(key=lambda t: (-t["videos"], t["tag"]))
    totals = {k: sum(t[k] for t in tags) for k in ("videos", "dead", "duplicates")}
    return {"found": True, "root": root, "tags": tags, "totals": totals}


def _fresh_for():
    """How long status() reuses a scan. A scan that was slow (thousands of links into a network share, each one
    checked) is reused for longer."""
    return max(CACHE_SECONDS, 6 * _cache["took"])


def status(root, now=None):
    """scan(root), reused for a while. Scans on demand: the web server does NOT use this (see cached())."""
    now = time.time() if now is None else now
    with _lock:
        if _cache["root"] == root and _cache["data"] is not None and now - _cache["at"] < _fresh_for():
            return {**_cache["data"], "age_seconds": int(now - _cache["at"])}
    return _scan_and_store(root, now)


def _scan_and_store(root, now=None):
    began = time.time()
    data = scan(root)
    data["scanned_at"] = int(now if now is not None else began)
    with _lock:
        _cache.update(root=root, at=now if now is not None else began, data=data, took=time.time() - began)
    _save()
    return {**data, "age_seconds": 0}


# ---- the regular scan: what the pages show is whatever the last scheduled scan found. A page view never starts one.
SAVE_PATH = os.environ.get("FS42_LIBRARY_CACHE", "/config/library-scan.json")
_scanning = {"on": False}


def _save():
    """Keep the last scan on disk, so a restart still has something to show. Best effort."""
    try:
        with _lock:
            blob = {"root": _cache["root"], "at": _cache["at"], "took": _cache["took"], "data": _cache["data"]}
        tmp = SAVE_PATH + ".tmp"
        os.makedirs(os.path.dirname(SAVE_PATH) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, SAVE_PATH)
    except (OSError, TypeError, ValueError):
        pass


def load_saved(root):
    """Read the last scan from disk at start-up (only if it was of this folder)."""
    try:
        with open(SAVE_PATH) as f:
            blob = json.load(f)
        if blob.get("root") == root and isinstance(blob.get("data"), dict):
            with _lock:
                _cache.update(root=root, at=float(blob["at"]), data=blob["data"], took=float(blob.get("took", 0)))
            return True
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return False


def cached(root, now=None):
    """What the last scan found, at once, however old: never walks anything. Before the first scan has finished it
    says so ("never": true) instead of making anyone wait."""
    now = time.time() if now is None else now
    with _lock:
        have = _cache["root"] == root and _cache["data"] is not None
        data = {**_cache["data"]} if have else None
        at = _cache["at"]
        running = _scanning["on"]
    if data is None:
        return {"found": os.path.isdir(root), "root": root, "tags": [], "never": True, "scanning": running,
                "totals": {"videos": 0, "dead": 0, "duplicates": 0}, "age_seconds": None}
    return {**data, "age_seconds": int(now - at), "scanning": running}


def scan_now(root):
    """One full scan, stored and saved. Only one runs at a time (a second request is ignored). Returns True if it ran."""
    with _lock:
        if _scanning["on"]:
            return False
        _scanning["on"] = True
    try:
        _scan_and_store(root)
    finally:
        with _lock:
            _scanning["on"] = False
    return True


def request_scan(root):
    """Start a scan in the background now: for something you asked for (the Rescan button), never for a page view."""
    threading.Thread(target=scan_now, args=(root,), daemon=True, name="library-scan").start()


def loop(root, interval_minutes=60, first_delay=60, sleep=time.sleep, forever=True):
    """The regular scan: wait a little after start-up, then walk the lists every interval. A saved scan from before
    a restart is shown until the first of these finishes."""
    sleep(first_delay)
    while True:
        try:
            scan_now(root)
        except Exception as e:                      # nothing may stop it for good
            print(f"[library] scan failed: {e}", flush=True)
        if not forever:
            return
        sleep(interval_minutes * 60)


def refresh_tag(root, tag):
    """You changed one list: look at just that list again (in the background), so its count is right without a
    walk of every list."""
    def work():
        path = os.path.join(root, tag)
        with _lock:
            data = _cache["data"] if _cache["root"] == root else None
        if data is None:
            return
        entry = {"tag": tag, **scan_tag(path)} if os.path.isdir(path) else None
        with _lock:
            tags = [x for x in _cache["data"]["tags"] if x["tag"] != tag]
            if entry is not None:
                tags.append(entry)
            tags.sort(key=lambda x: (-x["videos"], x["tag"]))
            _cache["data"] = {**_cache["data"], "tags": tags,
                              "totals": {k: sum(x[k] for x in tags) for k in ("videos", "dead", "duplicates")}}
        _save()
    th = threading.Thread(target=work, daemon=True, name="library-tag")
    th.start()
    return th


def expire():
    """Mark the cached scan out of date but keep it to show until the new one is ready."""
    with _lock:
        _cache["at"] = 0.0


def forget():
    """Drop the cached scan (tests, and a way to force a fresh look)."""
    with _lock:
        _cache.update(root=None, at=0.0, data=None, took=0.0)
