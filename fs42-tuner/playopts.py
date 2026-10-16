"""What a piece of a channel carries besides its picture: the volume brought to the target, captions put in as a track, and
captions drawn on the picture. tuner.py hands over the folders and objects these need through `configure`."""
import os
import time
from datetime import datetime, timedelta

import captions
import ccinject
import schedules
import settings
import stations

CONFS_DIR = "/app/confs"
FPS = 30
LEVELS = None


def configure(confs_dir, fps, levels):
    global CONFS_DIR, FPS, LEVELS
    CONFS_DIR, FPS, LEVELS = confs_dir, fps, levels


_CAPTION_PREFS = {"at": 0.0, "track": {}, "level": {}}


def level_audio(station):
    """Is the volume of this station's videos evened out? On unless the station says no."""
    caption_track(station)                                  # keeps the shared station-settings cache fresh
    return _CAPTION_PREFS["level"].get(station, True)


def audio_level_filter(station, entry, original):
    """The audio filter that brings a video to the volume target, or None (switched off for the station, a live stream, or the
    video is not measured yet: it is queued, and plays as it is this time)."""
    if entry.is_stream or os.environ.get("LOUDNESS", "1").lower() in ("0", "false", "no", "off") or not level_audio(station):
        return None
    try:
        return LEVELS.filter_for(original)
    except Exception as e:                                  # an even volume is a nicety: never the reason a channel goes quiet
        print(f"! volume of {os.path.basename(original)}: {e}", flush=True)
        return None


def caption_track(station):
    """Does the station carry captions as a track in its stream? On unless the station says no."""
    if time.time() - _CAPTION_PREFS["at"] > 5:
        try:
            confs = stations.station_confs(CONFS_DIR)
        except OSError:
            confs = {}
        _CAPTION_PREFS["track"] = {n: c.get("wrapper_caption_track") is not False for n, c in confs.items()}
        _CAPTION_PREFS["level"] = {n: c.get("wrapper_level_audio") is not False for n, c in confs.items()}
        _CAPTION_PREFS["at"] = time.time()
    return _CAPTION_PREFS["track"].get(station, True)


def burn_language(station, seek):
    """The language of the captions the station's calendar draws on the picture right now ("" for none): the video
    block that covers the moment the programme started has the toggle on; the language is the one setting."""
    try:
        defn = schedules.get(station)
        if not defn or not schedules.burn_at(defn, datetime.now() - timedelta(seconds=max(seek, 0))):
            return ""
        return settings.get_captions()["language"]
    except Exception as e:                          # a calendar that can't be read must not take the channel off
        print(f"! caption setting of {station}: {e}", flush=True)
        return ""


_CC_LINES = {}


def caption_injector(station, entry, original, seek):
    """A ccinject.Injector for a programme whose station carries captions as a track and whose video has them in the
    language set on the Stations page, or None."""
    if os.environ.get("CAPTION_TRACK", "1").lower() in ("0", "false", "no", "off"):      # switched off for the whole container
        return None
    if entry.is_stream or entry.content_type in ("bump", "commercial") or not caption_track(station):
        return None
    try:
        lang = settings.get_captions()["language"]
        track = captions.pick(original, lang)
        if track is None:
            return None
        key = (original, track["id"], captions.version(original, track))
        got = _CC_LINES.get(key)
        if got is None:
            text = captions.vtt(original, track["id"])
            cues = ccinject.parse_vtt(text or "")
            got = (ccinject.timeline(cues, FPS), cues)
            if len(_CC_LINES) >= 8:
                _CC_LINES.pop(next(iter(_CC_LINES)))
            _CC_LINES[key] = got
        line, cues = got
        return ccinject.Injector(line, seek, FPS, resume=ccinject.active_at(cues, seek)) if line else None
    except Exception as e:                       # captions are a nicety: never the reason a channel goes quiet
        print(f"! caption track for {os.path.basename(original)}: {e}", flush=True)
        return None


def caption_feed(inject, chunk, number=0):
    """Run a chunk of the stream through the caption injector, if there is one. Whatever goes wrong with the captions, the
    picture goes on: the injector is dropped and the stream passes through untouched. Returns (chunk, injector or None)."""
    if inject is None:
        return chunk, None
    try:
        return inject.feed(chunk), inject
    except Exception as e:
        print(f"[ch{number}] captions switched off for this programme: {e}", flush=True)
        return chunk, None


def caption_filter(station, entry, original, local, seek):
    """The subtitle filter for a programme in a block that burns captions in, or None (the block does not, the video
    has none in that language, or it is a commercial or a card)."""
    if entry.is_stream or entry.content_type in ("bump", "commercial"):
        return None
    lang = burn_language(station, seek)
    if not lang:
        return None
    try:
        return captions.burn_filter(captions.pick(original, lang), local, seek)
    except Exception as e:                       # captions are a nicety: never the reason a channel goes quiet
        print(f"! captions for {os.path.basename(original)}: {e}", flush=True)
        return None
