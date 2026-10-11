"""A picture for the guide, per SERIES or per MOVIE (not per episode). In the order that gives the best one:

    a series   its poster (poster.jpg, folder.jpg... in the show's folder), else the cover embedded in its first video seen, else
               a frame of that video; one picture for the whole series, whichever episode asks
    a movie    a picture beside it, else the cover embedded in the file, else a frame of it, else a poster in its folder

The embedded cover or the frame is made once with ffmpeg and kept in a cache folder of our own (never beside your media), a few
at a time, so a guide of hundreds of programmes does not read hundreds of videos at once. Nothing is written to the media.
"""
import hashlib
import json
import os
import subprocess
import threading
import time

import titles

CACHE = os.environ.get("FS42_ART_CACHE", "/config/art-cache")
KEEP = 500                                   # pictures kept (one per series or movie); the least recently used go first
WIDTH = 640
_slots = threading.Semaphore(2)              # at most two videos are opened for a picture at a time
_lock = threading.Lock()
_none = {}                                   # key -> when a video was found to have no picture (tried again later)
RETRY_SECONDS = 1800


def _identity(video_path):
    """("series", title) when the video is an episode of a show, else ("movie", its file's identity)."""
    try:
        d = titles.details_for(video_path)
        if d.get("kind") == "episode":
            title, _ep = titles.parts_for(video_path)
            if title:
                return "s", title
    except Exception:
        pass
    try:
        st = os.stat(video_path)
        return "m", f"{os.path.realpath(video_path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return "m", video_path


def _key(kind, ident):
    return f"{kind}-{hashlib.sha1(ident.encode(), usedforsecurity=False).hexdigest()}"


def _embedded_stream(path):
    """The index of the video stream that is a cover picture (attached_pic), or None."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=index:stream_disposition=attached_pic",
                              "-of", "json", path], capture_output=True, text=True, timeout=30).stdout
        for s in json.loads(out or "{}").get("streams", []):
            if (s.get("disposition") or {}).get("attached_pic") == 1:
                return s["index"]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _run(cmd, out):
    try:
        subprocess.run(cmd, capture_output=True, timeout=40, check=True)
        return os.path.isfile(out) and os.path.getsize(out) > 200
    except (OSError, subprocess.SubprocessError):
        return False


def make(path, key):
    """Make and keep a picture from this video (its embedded cover, else a frame) under `key`, and return the file, or None."""
    out = os.path.join(CACHE, key + ".jpg")
    if os.path.isfile(out):
        os.utime(out, None)
        return out
    with _lock:
        failed_at = _none.get(key)
    if failed_at and time.time() - failed_at < RETRY_SECONDS:
        return None
    try:
        os.makedirs(CACHE, exist_ok=True)
    except OSError:
        return None
    tmp = out + f".{os.getpid()}.part.jpg"
    scale = f"scale={WIDTH}:-2"
    with _slots:
        if os.path.isfile(out):                  # another request made it while this one waited
            return out
        got = False
        idx = _embedded_stream(path)
        if idx is not None:
            got = _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-map", f"0:{idx}", "-frames:v", "1", "-vf", scale,
                        "-q:v", "4", tmp], tmp)
        for ss in ("10", "0") if not got else ():        # a frame ten seconds in, or the first one of a short video
            got = _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", ss, "-i", path, "-an", "-frames:v", "1", "-vf", scale,
                        "-q:v", "4", tmp], tmp)
            if got:
                break
    if got:
        os.replace(tmp, out)
        _prune()
        return out
    try:
        os.unlink(tmp)
    except OSError:
        pass
    with _lock:
        _none[key] = time.time()
    return None


def _prune():
    """Keep the pictures in use: the oldest go past KEEP, and so does anything an earlier version made (one per episode)."""
    try:
        names = [f for f in os.listdir(CACHE) if f.endswith(".jpg") and ".part" not in f]
        for f in names:
            if not f.startswith(("s-", "m-")):
                os.unlink(os.path.join(CACHE, f))
        files = [os.path.join(CACHE, f) for f in names if f.startswith(("s-", "m-"))]
        if len(files) > KEEP:
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[: len(files) - KEEP]:
                os.unlink(p)
    except OSError:
        pass


def _type(path):
    return "image/png" if path.endswith(".png") else "image/jpeg"


def get(video_path):
    """(path of the picture, content type) for what this video belongs to (its series, or itself if a movie), or None."""
    kind, ident = _identity(video_path)
    key = _key(kind, ident)
    cached = os.path.join(CACHE, key + ".jpg")
    if kind == "s":
        poster = titles.show_art_for(video_path)             # the show's own picture comes first
        if poster:
            return poster, _type(poster)
        if os.path.isfile(cached):
            os.utime(cached, None)
            return cached, "image/jpeg"
    else:
        side = titles.art_for(video_path, poster=False)       # a movie: the picture beside it
        if side:
            return side, _type(side)
        if os.path.isfile(cached):
            os.utime(cached, None)
            return cached, "image/jpeg"
    if os.path.isfile(video_path):
        made = make(video_path, key)
        if made:
            return made, "image/jpeg"
    poster = titles.art_for(video_path)
    if poster:
        return poster, _type(poster)
    return None
