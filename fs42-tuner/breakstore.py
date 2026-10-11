"""Our own record of where each video's commercial breaks go.

FieldStation42 only knows the chapter marks it read out of a file, and keeps them in its own database. This is the
wrapper's copy of that knowledge, in the config volume (break-store.json), so it is ours to improve and survives a
catalog rebuild or a FieldStation42 update:

    one entry per video:  {"source": "edl" | "snap", "orig": the marks as FieldStation42 first read them (or None),
                           "points": the marks to use, "file": size:mtime of the video, "sig": what the entry was made from}

Where the breaks come from, best first:
    1. an EDL file beside the video (show.edl, as Comskip, Kodi and MythTV write them). The media folder is only
       ever read, so we read these but never write one;
    2. FieldStation42's chapter marks moved onto the nearest black frame or silence (breaksnap.py);
    3. the chapter marks as they are (nothing is stored: FieldStation42 has them already).

breaksnap.py puts the result into FieldStation42's chapter_points table, which is where its schedule builder looks,
until the wrapper builds schedules itself.
"""
import hashlib
import json
import os
import tempfile
import threading

STORE = os.environ.get("FS42_BREAK_STORE", "/config/break-store.json")
EDGE = 5.0                    # a break within this many seconds of the start or end is not a break
EDL_BREAKS = ("0", "3")       # EDL actions: 0 = cut, 3 = commercial break (Comskip writes 0)
_lock = threading.Lock()


def edl_path(video):
    return os.path.splitext(video)[0] + ".edl"


def read_edl(video):
    """(break times in seconds, a signature of the file) from the EDL beside `video`, or (None, None).
    Each line is "start end action". A cut or commercial break starts a break at `start`."""
    p = edl_path(video)
    try:
        with open(p, "rb") as f:
            raw = f.read(1_000_000)
    except OSError:
        return None, None
    points = []
    for line in raw.decode("utf-8", "replace").splitlines():
        parts = line.split("#")[0].split()
        if len(parts) >= 3 and parts[2] in EDL_BREAKS:
            try:
                points.append(float(parts[0]))
            except ValueError:
                continue
    return sorted(set(points)), hashlib.sha1(raw, usedforsecurity=False).hexdigest()


def chapters_from_points(points, duration):
    """Break times -> FieldStation42's chapter list (a segment between each pair of breaks)."""
    cuts = sorted({round(p, 3) for p in points if EDGE <= p <= duration - EDGE})
    starts = [0.0] + cuts
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else float(duration)
        out.append({"chapter_start": s, "chapter_end": e, "segment_duration": round(e - s, 3)})
    return out


def sig(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode(), usedforsecurity=False).hexdigest()


def load():
    with _lock:
        try:
            with open(STORE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


def save(store):
    with _lock:
        folder = os.path.dirname(STORE) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".break-store-")
        with os.fdopen(fd, "w") as f:
            json.dump(store, f)
        os.replace(tmp, STORE)
