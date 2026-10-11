"""The TubeArchivist conversion, as the dashboard drives it: find the library, save settings, ask for a run.

The conversion itself is ta-nfo/service.py, which reads the same settings. Nothing here converts anything.
"""
import os
import re
import threading
import time

import files as filemgr
import settings

CHANNEL_DIR = re.compile(r"^UC[0-9A-Za-z_-]{22}$")      # how TubeArchivist names a channel folder
MAX_SCAN = 300                                           # folders looked at while detecting


def _inside(path, base):
    base, path = os.path.realpath(base), os.path.realpath(path)
    return path == base or path.startswith(base.rstrip("/") + "/")


def _channels_in(folder):
    """How many TubeArchivist channel folders (UC...) are directly inside, or 0."""
    n = 0
    try:
        for name in os.listdir(folder):
            if CHANNEL_DIR.match(name) and os.path.isdir(os.path.join(folder, name)):
                n += 1
    except OSError:
        return 0
    return n


_detect_cache = {"root": None, "at": 0.0, "data": None}
DETECT_FRESH = 600
_counts = {}                    # folder -> (when, how many channel folders)
COUNT_FRESH = 60


_probes = {}                    # library folder -> {"at", "exists", "channels", "busy"}: the last look at it
_probe_lock = threading.Lock()


def _look(folder):
    """Look at a library folder (slow on a share: a listing and a check of every entry) and keep the answer."""
    exists = channels = None
    try:
        exists = os.path.isdir(folder)
        channels = _channels_in(folder) if exists else 0
    finally:
        with _probe_lock:
            _probes[folder] = {"at": time.time(), "exists": exists, "channels": channels, "busy": False}


def library_probe(folder, wait=False):
    """(exists, channels) as last seen, or (None, None) before the first look. It never waits on the share: an old
    answer is returned at once and a fresh look runs in the background (the Files page used to sit blank for minutes
    while a share was listed). wait=True looks now, for the tests."""
    with _probe_lock:
        got = _probes.get(folder)
        start = not (got and (got["busy"] or time.time() - got["at"] < COUNT_FRESH))
        if start:
            _probes[folder] = {**(got or {"at": 0.0, "exists": None, "channels": None}), "busy": True}
    if start:
        if wait:
            _look(folder)
        else:
            threading.Thread(target=_look, args=(folder,), daemon=True, name="ta-look").start()
    with _probe_lock:
        got = _probes.get(folder) or {}
    return got.get("exists"), got.get("channels")


def warm():
    """Look at every library once at start-up, so the first page view finds the answers waiting."""
    try:
        for lib in settings.get_ta()["libraries"]:
            library_probe(lib)
    except Exception:
        pass


def _converted_cached(view):
    """How many channels the converted view has (a local folder, but it holds one folder per channel)."""
    now = time.time()
    got = _counts.get(("view", view))
    if got and now - got[0] < 10:
        return got[1]
    try:
        n = sum(1 for d in os.listdir(view) if os.path.isfile(os.path.join(view, d, "tvshow.nfo")))
    except OSError:
        n = 0
    _counts[("view", view)] = (now, n)
    return n


def detect(media_root):
    """Folders under the media (one or two levels down) that look like a TubeArchivist library. Remembered for two
    minutes: looking walks a few hundred folders."""
    now = time.time()
    c = _detect_cache
    if c["root"] == media_root and c["data"] is not None and now - c["at"] < DETECT_FRESH:
        return c["data"]
    data = _detect(media_root)
    _detect_cache.update(root=media_root, at=now, data=data)
    return data


def _detect(media_root):
    found, seen = [], 0
    try:
        top = sorted(d for d in os.listdir(media_root) if not d.startswith(".") and d not in filemgr.HIDDEN)
    except OSError:
        return found
    candidates = [media_root]
    for d in top:
        p = os.path.join(media_root, d)
        if os.path.isdir(p):
            candidates.append(p)
            try:
                candidates += [os.path.join(p, s) for s in sorted(os.listdir(p))
                               if not s.startswith(".") and os.path.isdir(os.path.join(p, s))][:40]
            except OSError:
                pass
    for p in candidates:
        seen += 1
        if seen > MAX_SCAN:
            break
        n = _channels_in(p)
        if n:
            found.append({"path": p, "channels": n})
    found.sort(key=lambda f: (-f["channels"], f["path"]))
    return found


def status(media_root, catalog_dir):
    cfg = settings.get_ta()
    libraries, total = [], 0
    for v in cfg["views"]:
        view, lib = os.path.join(catalog_dir, "tanfo", v["name"]), v["library"]
        converted = _converted_cached(view)
        total += converted
        exists, channels = library_probe(lib)
        libraries.append({"path": lib, "name": v["name"], "exists": True if exists is None else exists,      # not known yet: not "missing"
                          "channels": channels, "converted": converted})
    return {
        "settings": cfg,
        "job": settings.read_status(),
        "channels_in_view": total,
        "libraries": libraries,
        "media_root": media_root,
    }


def save(media_root, body, catalog_dir=None):
    """Save enabled / library / interval_minutes from the dashboard. Raises FileError for a bad value."""
    allowed = {k: body[k] for k in ("enabled", "libraries", "library", "interval_minutes") if k in body}
    if not allowed:
        raise filemgr.FileError("nothing to save")
    if "library" in allowed and "libraries" not in allowed:         # the old single-folder form
        one = allowed.pop("library")
        allowed["libraries"] = [one] if isinstance(one, str) and one.strip() else None
        if allowed["libraries"] is None:
            del allowed["libraries"]
    else:
        allowed.pop("library", None)
    libs = allowed.get("libraries")
    if isinstance(libs, list):
        # A folder already in the list is kept as it is even if it is missing now (its volume may be off for a
        # moment); only a folder being ADDED must exist. Otherwise one missing folder, such as the built-in default,
        # would block every change, including taking it out.
        current = settings.get_ta()
        known = set(current["libraries"])
        from_default = current["source"].get("libraries") == "default"
        clean, new_ok = [], False
        for lib in libs:
            if not isinstance(lib, str) or not lib.strip():
                raise filemgr.FileError("each library must be a folder path, like /mnt/media/youtube")
            lib = lib.strip()
            if not os.path.isabs(lib):
                raise filemgr.FileError("each library must be a folder path, like /mnt/media/youtube")
            norm = os.path.normpath(lib)
            if norm in known:
                clean.append(norm)
                continue
            if not _inside(lib, media_root):
                raise filemgr.FileError(f"{lib} must be inside your media ({media_root}), because that is what "
                                        f"the container can see")
            if not os.path.isdir(lib):
                raise filemgr.FileError(f"{lib} is not a folder the container can see: is its volume mounted?", 404)
            clean.append(norm)
            new_ok = True
        if from_default and new_ok:                      # nobody chose the built-in default: a real folder replaces it
            clean = [p for p in clean if p not in known or os.path.isdir(p)]
        allowed["libraries"] = clean
    if not allowed:
        raise filemgr.FileError("nothing to save")
    try:
        cfg = settings.set_ta(allowed)
    except settings.SettingsError as e:
        raise filemgr.FileError(str(e))
    except OSError as e:
        raise filemgr.FileError(f"could not save the settings: {e.strerror}", 500)
    return {"settings": cfg}


def run_now():
    """Ask the job to convert now. It does nothing if the conversion is switched off."""
    if not settings.get_ta()["enabled"]:
        raise filemgr.FileError("the conversion is switched off: turn it on first")
    try:
        open(settings.RUN_NOW, "w").close()
    except OSError as e:
        raise filemgr.FileError(f"could not ask for a run: {e.strerror}", 500)
    return {"asked": True}
