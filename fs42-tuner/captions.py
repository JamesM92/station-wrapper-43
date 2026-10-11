"""Captions for the web player, from the subtitle files that belong to a video.

Two places a video can keep its captions, and both are read here:
  * a file beside it with the same name: Show - 01.srt, Show - 01.en.vtt, Show - 01.eng.ass ...
  * a text subtitle track inside the video itself (most anime releases: an MKV with ASS tracks).

Either way the player is handed WebVTT. Only TEXT subtitles are used: picture subtitles (DVD, Blu-ray PGS) can't be
turned into text without OCR. The video itself is never changed; a track inside a video is read with ffmpeg and
the result kept in memory.

What may be read is decided by the caller (tuner.py): only the file a channel is airing right now. Nothing here
takes a path from a browser.
"""
import hashlib
import json
import os
import subprocess
import threading
import time

SIDE_EXT = (".vtt", ".srt", ".ass", ".ssa")
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd", "subviewer"}
MAX_VTT = 4 * 1024 * 1024
FFMPEG_TIMEOUT = 180
CACHE_SIZE = 24
TRACKS_TTL = 60                 # seconds a video's list of tracks is kept

LANGS = {"en": "English", "eng": "English", "ja": "Japanese", "jpn": "Japanese", "jp": "Japanese", "es": "Spanish",
         "spa": "Spanish", "fr": "French", "fre": "French", "fra": "French", "de": "German", "ger": "German",
         "deu": "German", "pt": "Portuguese", "por": "Portuguese", "it": "Italian", "ita": "Italian",
         "ko": "Korean", "kor": "Korean", "zh": "Chinese", "chi": "Chinese", "zho": "Chinese", "ru": "Russian",
         "rus": "Russian"}

_lock = threading.Lock()
_tracks_cache = {}              # (path, mtime) -> [track]
_vtt_cache = {}                 # (path, mtime, track id) -> text
_order = []                     # oldest first, for both caches


def _remember(cache, key, value):
    with _lock:
        cache[key] = value
        _order.append((cache is _tracks_cache, key))
        while len(_order) > CACHE_SIZE * 2:
            is_tracks, old = _order.pop(0)
            (_tracks_cache if is_tracks else _vtt_cache).pop(old, None)


def eid(path):
    """A short stable name for one video, so a page can tell when the channel moved on to another."""
    return hashlib.sha1(path.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:12]


def _label(lang, extra):
    name = LANGS.get((lang or "").lower())
    base = name or (lang.upper() if lang else "Captions")
    return f"{base} ({extra})" if extra else base


def sidecars(video):
    """Subtitle files beside the video (and beside the file a link points to): [{id, label, lang, file}]."""
    name = os.path.basename(video)
    stem = os.path.splitext(name)[0]
    folders = []
    for d in (os.path.dirname(video), os.path.dirname(os.path.realpath(video))):
        if d and d not in folders:
            folders.append(d)
    out, seen = [], set()
    for d in folders:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            low = n.lower()
            ext = os.path.splitext(low)[1]
            prefix = name if n.startswith(name + ".") else stem      # Show.mkv.srt is named after the whole file
            if ext not in SIDE_EXT or not n.startswith(prefix + ".") or n in seen:
                continue
            seen.add(n)
            middle = n[len(prefix) + 1:-len(ext)]                   # "en", "eng.forced", "" ...
            parts = [p for p in middle.split(".") if p]
            lang = parts[0] if parts and (parts[0].lower() in LANGS or (parts[0].isalpha() and len(parts[0]) in (2, 3))) else ""
            extra = ".".join(p for p in parts if p != lang)
            out.append({"id": f"f{len(out)}", "lang": lang.lower(), "label": _label(lang, extra), "file": os.path.join(d, n)})
    return out


def embedded(video):
    """Text subtitle tracks inside the video: [{id, label, lang, index}]. [] when there are none or it can't tell."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                            "stream=index,codec_name:stream_tags=language,title", "-of", "json", video],
                           capture_output=True, text=True, timeout=60)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    out = []
    for ordinal, s in enumerate(streams):                        # ordinal: its place among the subtitle streams (ffmpeg's si=)
        if s.get("codec_name") not in TEXT_CODECS:
            continue
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        lang = "" if lang == "und" else lang
        out.append({"id": f"s{s['index']}", "lang": lang, "label": _label(lang, (tags.get("title") or "")[:40]),
                    "index": s["index"], "ordinal": ordinal})
    return out


def tracks(video):
    """Every caption track this video has, files first. Remembered, so asking again is free."""
    try:
        key = (video, os.stat(video).st_mtime)
    except OSError:
        return []
    with _lock:
        got = _tracks_cache.get(key)
    if got is not None and time.time() - got[0] < TRACKS_TTL:
        return got[1]
    found = sidecars(video) + embedded(video)           # looked at again every so often: a subtitle file may have been added
    _remember(_tracks_cache, key, (time.time(), found))
    return found


def version(video, track):
    """What a caption track was last changed: its own file when it is one (editing a .srt is noticed), else the video's."""
    try:
        return os.stat(track["file"] if "file" in track else video).st_mtime
    except OSError:
        return 0


def public(track_list):
    return [{"id": t["id"], "label": t["label"], "lang": t["lang"]} for t in track_list]


def vtt(video, track_id):
    """The WebVTT text of one track, or None if the video has no such track. Raises OSError/RuntimeError when it
    exists but can't be read."""
    try:
        os.stat(video)
    except OSError:
        return None
    track = next((t for t in tracks(video) if t["id"] == track_id), None)
    if track is None:
        return None
    key = (video, track_id, version(video, track))
    with _lock:
        got = _vtt_cache.get(key)
    if got is not None:
        return got
    if "file" in track:
        if track["file"].lower().endswith(".vtt"):
            with open(track["file"], encoding="utf-8-sig", errors="replace") as f:
                text = f.read(MAX_VTT + 1)
        else:
            text = _ffmpeg_vtt(["-i", track["file"]])
    else:
        text = _ffmpeg_vtt(["-i", video, "-map", f"0:{track['index']}"])
    if len(text) > MAX_VTT:
        raise RuntimeError("those captions are too large")
    _remember(_vtt_cache, key, text)
    return text


def _ffmpeg_vtt(inputs):
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *inputs, "-f", "webvtt", "pipe:1"],
                           capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("reading the captions took too long")
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("could not read those captions")
    return r.stdout.decode("utf-8", errors="replace")


def pick(video, lang):
    """The caption track to burn into a channel's picture: the first one in the language asked for ("en" matches
    "eng" and a track labelled English), or None. Never another language: a wrong one is worse than none."""
    want = (lang or "").lower()
    if not want:
        return None
    name = LANGS.get(want, want)
    for t in tracks(video):
        have = (t.get("lang") or "").lower()
        if have and (have == want or LANGS.get(have, have) == name):
            return t
    return None


def _quote(path):
    """A file name as an ffmpeg filter option, or None when it can't be written safely (it has a quote in it)."""
    if "'" in path or "\n" in path:
        return None
    return "'" + path.replace("\\", "\\\\").replace(":", "\\:") + "'"


def burn_filter(track, video, seek):
    """The ffmpeg filters that draw `track` onto the picture of `video`, playing from `seek` seconds (it is seeked
    before it is opened, so the clock the subtitles read is moved to the file's own time and back). None if it can't."""
    if track is None:
        return None
    src = _quote(track["file"] if "file" in track else video)
    if src is None:
        return None
    si = "" if "file" in track else f":si={track['ordinal']}"
    return f"setpts=PTS+{max(seek, 0):.3f}/TB,subtitles=filename={src}{si},setpts=PTS-STARTPTS"
