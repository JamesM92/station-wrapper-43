"""
fs42-tuner -- presents FieldStation42 to Jellyfin as an HDHomeRun tuner with
its own XMLTV guide, optionally with a real HDHomeRun passed through behind it
so Jellyfin sees a single device.

Why one device rather than two: Jellyfin builds an HDHomeRun channel's id as
"hdhr_" + GuideNumber, with nothing identifying the device, and its base tuner
host then merges every hdhomerun-type tuner with

    channels.Where(i => !list.Any(l => i.Id == l.Id))

so a channel whose number already exists is dropped SILENTLY. Two devices that
share a number quietly lose one. Putting the real tuner behind this wrapper
collapses that to one lineup in one namespace, which the wrapper validates.

Real-device channels pass through with GuideNumber and GuideName untouched, so
whatever guide provider already maps them keeps matching. Only FieldStation42's
channels are new, and they live in a number range of their own.

Endpoints:
  /                   dashboard -- every channel, what's on, system stats
  /watch.html         player -- mpegts.js, channel controls, guide grid
  /channels.json      per-channel now-playing, viewers, encoding state
  /system.json        uptime, load, memory, encoder/viewer counters, schedule
  /library.json       read-only view of the content folder: tags, sources, dead links,
                      duplicates (cached 2 min; ?refresh to rescan)
  /thumb/{n}.jpg      one still frame of what's on channel n (cached 30s)
  /discover.json      HDHR identity + TunerCount      (Jellyfin reads first)
  /lineup_status.json scan state
  /lineup.json        merged channel list
  /lineup.post        scan trigger (refreshes the upstream lineup)
  /device.xml         UPnP description
  /xmltv.xml          ONE guide for every channel in the lineup -- FieldStation42's
                      own schedule plus the real tuner's broadcast listings
  /guide_status.json  per-source guide counts and cache age
  /auto/v{channel}    MPEG-TS -- encoded from FS42, or proxied from the real tuner
  /status.json        encoder + viewer + passthrough occupancy

The portal is plain static files (static/index.html, app.js, style.css, and a
vendored mpegts.min.js) served straight off disk -- no template engine, no
build step. /discover.json, /lineup.json and friends are HDHomeRun emulation
for a Jellyfin/NextPVR/etc. front end; they cost nothing to leave in and the
portal doesn't use them (it reads /lineup.json for the channel list and
/xmltv.xml for the guide directly, same as any other client would).
"""

import http.cookies
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape

import auth as authmod
import webget
import captions
import ccinject
import loudness
import console_client
import files as filemgr
import keeper as keepermod
import guide_channel
import diag
import iptv
import legacy
import prefetch
import streaming
import schedules
import library
import resolver
import settings
import stationform
import stations
import stationsapi
import art
import channel_logo
import titles
import tubearchivist
diag.RING.install()                  # the Status page shows what this process last said
from resolver import ScheduleGap
from guide import GuideBuilder
from upstream import TunerBudget, Upstream, UpstreamError

CONF = json.load(open(os.environ.get("FS42_TUNER_CONF", "config.json")))

DB_PATH      = CONF["db_path"]
CATALOG_ROOT = CONF.get("catalog_root", ".")
# The content folder the Lists & files page's counts read (read-only). The
# generator builds it as catalog/by-genre.
LIBRARY_ROOT = CONF.get("library_root") or os.path.join(CATALOG_ROOT, "catalog", "by-genre")
# Channel numbers are strings throughout: a real HDHomeRun reports ATSC virtual
# channels like "7.1", which are not integers.
CHANNELS     = {str(k): v for k, v in CONF["channels"].items()}   # number -> station
# Stations added in FieldStation42's console are found in its station configs, so nobody has to list them
# here. A channel named in this config wins over a station that claims the same number.
# The file manager (the Files page): build the tag folders by hand. It writes only inside the catalog's
# by-genre folder, and only ever makes or removes links. The dashboard has no login yet, so
# `"edit": false` in this config, or DASHBOARD_EDIT=0, turns the changing actions off (browsing stays).
EDIT_FILES   = bool(CONF.get("edit", True)) and os.environ.get("DASHBOARD_EDIT", "1").lower() not in ("0", "false", "no", "off")
FILES        = filemgr.Files(CONF.get("media_root", "/mnt/media"), os.path.join(CATALOG_ROOT, "catalog"),
                             ta=lambda: settings.get_ta(),
                             follows_path=os.environ.get("FS42_FOLLOWS") or os.path.join(os.path.dirname(settings.PATH), "follows.json"))


# the old schedule kept on air while a station is rebuilt (legacy.py) lives beside the other saved settings
legacy.PATH = os.environ.get("FS42_LEGACY") or os.path.join(os.path.dirname(settings.PATH), "legacy-schedule.db")
schedules.PATH = os.environ.get("FS42_SCHEDULES") or os.path.join(os.path.dirname(settings.PATH), "schedules.json")


def follow_loop(interval_minutes, first_delay):
    """Pull the new videos of every followed folder into its list, now and then. Add-only."""
    time.sleep(first_delay)
    while True:
        try:
            got = FILES.refresh_follows()
            if got["added"]:
                print(f"[follow] linked {got['added']} new video file(s) into followed lists")
        except Exception as e:        # nothing may stop it for good
            print(f"[follow] pass failed: {e}")
        time.sleep(interval_minutes * 60)
# The login. One password protects the management pages and every /api/ call; the player, the channel list and
# the HDHomeRun / guide endpoints other clients use stay open (they cannot log in). Secure by default: until a
# password is set, the management pages send you to a "set a password" page. DASHBOARD_LOGIN=0 (or "login":
# false in this config) turns it off, for anyone who puts their own login in front. See auth.py.
LOGIN_ON     = bool(CONF.get("login", True)) and os.environ.get("DASHBOARD_LOGIN", "1").lower() not in ("0", "false", "no", "off")
AUTH         = authmod.Auth(os.environ.get("FS42_AUTH") or os.path.join(os.path.dirname(settings.PATH), "auth.json"), enabled=LOGIN_ON)
PROTECTED_PAGES = {"/stations.html", "/station.html", "/schedule.html", "/files.html", "/iptv.html", "/status.html"}
SESSION_COOKIE = "sw43_session"
if LOGIN_ON and os.environ.get("DASHBOARD_PASSWORD") and not AUTH.has_password():
    try:
        AUTH.set_password(os.environ["DASHBOARD_PASSWORD"])
        print("  login: the password from DASHBOARD_PASSWORD was set")
    except (authmod.AuthError, OSError) as e:
        print(f"  ! DASHBOARD_PASSWORD was not used: {e}")

# The Stations and Schedule pages: stations are made, changed and deleted THROUGH FieldStation42's console
# (the one place a station's settings live); what is scheduled is read from the schedule database.
CONSOLE      = console_client.Console(CONF.get("console_url") or os.environ.get("FS42_CONSOLE", "http://127.0.0.1:4242"))
STATIONS     = stationsapi.Stations(CONSOLE, DB_PATH, FILES.tags_root, media_root=CONF.get("media_root", "/mnt/media"),
                                    horizon=lambda: (lambda k: k["days_ahead"] if k["enabled"] else None)(settings.get_keeper()))
# FieldStation42 only extends schedules from its on-screen player, which this container never runs, so the
# keeper does it: every station that has a schedule is kept a few days ahead. See keeper.py.
KEEPER       = keepermod.Keeper(STATIONS)
CONFS_DIR    = CONF.get("confs_dir") or os.environ.get("FS42_CONFS", "/app/confs")
DISCOVERED   = {n: s for n, s in stations.discover(CONFS_DIR).items() if n not in CHANNELS}


_GUIDE_CONFS = {"at": 0.0, "confs": {}}


def apply_guide_now():
    """Make every running guide channel start over with its saved settings, keeping its viewers connected.
    Returns how many were running."""
    _GUIDE_CONFS["at"] = 0.0                            # read the configs afresh
    n = 0
    for chan in CHANS.values():
        if guide_conf(chan.station) is not None and chan.thread is not None:
            chan.reload.set()
            n += 1
    return n


def guide_conf(station):
    """The config of a guide channel (network_type "guide"), or None for any other station. Read from the station
    configs, at most every few seconds."""
    if time.time() - _GUIDE_CONFS["at"] > 5:
        _GUIDE_CONFS["confs"] = {n: c for n, c in stations.station_confs(CONFS_DIR).items() if c.get("network_type") == "guide"}
        _GUIDE_CONFS["at"] = time.time()
    return _GUIDE_CONFS["confs"].get(station)
CHANNELS.update(DISCOVERED)
MAX_ENCODERS = int(CONF.get("max_encoders", 4))
TUNER_COUNT  = int(CONF.get("tuner_count", 12))
GUIDE_HOURS  = int(CONF.get("guide_hours", 48))
LINGER       = float(CONF.get("linger_seconds", 10))
# After the last viewer leaves a channel keeps running a while, so the dashboard keeps its picture and a person who comes
# straight back finds it alive. A source converted here: this long. A station of ours: through what is already queued (the
# programme, the next break and its mid cards) and no further, or this long, whichever is first.
EXTERNAL_LINGER = float(CONF.get("external_linger_seconds", 300))
IDLE_TAIL_SECONDS = float(os.environ.get("IDLE_TAIL_MINUTES") or CONF.get("idle_tail_minutes", 15)) * 60
ADVERTISE    = CONF["advertise_url"].rstrip("/")
DEVICE_ID    = CONF.get("device_id", "FS42" + uuid.uuid5(uuid.NAMESPACE_DNS, ADVERTISE).hex[:8].upper())

# Mark the FS42 channels favourite. Jellyfin maps a lineup's Favorite flag
# straight onto ChannelInfo.IsFavorite, and IsFavorite is one of the few real
# filters in LiveTvChannelQuery -- so this is what gives the viewer a one-click
# "just the FS42 channels" view. Live TV itself is a single view that cannot be
# split, and ChannelGroup / M3U group-title is a dead field in Jellyfin (the
# M3U parser writes it and nothing ever reads it), so this is the lever that
# actually works.
FAVOR_FS42 = bool(CONF.get("favorite_fs42_channels", True))

GUIDE_CONF = CONF.get("guide") or {}

PT_CONF   = CONF.get("passthrough") or {}
PT_URL    = PT_CONF.get("url")
PT_MODE   = "proxy"                                    # the old "direct" mode is retired: clients always come through this container
PT_DIRECT_ASKED = PT_CONF.get("mode") == "direct"
UPSTREAM  = Upstream(PT_URL) if PT_URL else None

V = CONF.get("video", {})
FPS = int(V.get("fps", 30))
W, H = int(V.get("width", 1280)), int(V.get("height", 720))
GOP = FPS * 2                       # keyframe every 2s -- also the join latency
NORMALIZE = [
    "-c:v", V.get("vcodec", "libx264"),
    "-preset", V.get("preset", "veryfast"),
    "-b:v", V.get("vbitrate", "4000k"),
    "-maxrate", V.get("vbitrate", "4000k"),
    "-bufsize", V.get("bufsize", "8000k"),
    "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
           f"pad={W}:{H}:-1:-1:color=black,fps={FPS},format=yuv420p",
    "-g", str(GOP), "-keyint_min", str(GOP), "-sc_threshold", "0",
    "-c:a", "aac", "-b:a", V.get("abitrate", "192k"), "-ar", "48000", "-ac", "2",
]
MUX = [
    "-f", "mpegts",
    "-mpegts_flags", "+resend_headers+initial_discontinuity",
    "-mpegts_service_id", "1", "-mpegts_pmt_start_pid", "4096", "-mpegts_start_pid", "256",
    "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
]

CHUNK = 188 * 64                    # whole TS packets
PRIME_SECONDS = 3                   # rolling buffer handed to a late joiner

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript",
    ".css": "text/css", ".json": "application/json", ".png": "image/png",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
}

STARTED = time.time()
STATS = {"viewers_served": 0, "refused": 0, "encoder_starts": 0}
STATS_LOCK = threading.Lock()
THUMB_TTL = 30                      # seconds a snapshot is reused
THUMB_TTL_GUIDE = 8                 # the guide channel is drawn here, not read from a file: cheap to keep fresh
THUMBS = {}                         # number -> (taken_at, jpeg bytes or None)
THUMBS_LOCK = threading.Lock()


def _count(key, n=1):
    with STATS_LOCK:
        STATS[key] += n


def media_path(rel):
    return rel if os.path.isabs(rel) else os.path.join(CATALOG_ROOT, rel)


_CONT_ENV = os.environ.get("CONTINUOUS_TS", "").strip().lower()


def continuous_ts():
    """Do timestamps carry straight on across pieces (one continuous stream)? The CONTINUOUS_TS environment variable decides
    when it is set; else the dashboard's switch (settings.json), off unless turned on. Read at each piece, so a change is used
    from the next one."""
    if _CONT_ENV:
        return _CONT_ENV in ("1", "true", "yes", "on")
    try:
        return settings.get_playback()["continuous_ts"]
    except Exception:
        return False


def _continuous(mux):
    """The mux options without the flag that marks each new ffmpeg as a break in the stream."""
    return [("+resend_headers" if m == "+resend_headers+initial_discontinuity" else m) for m in mux]


def segment_cmd(path, seek, duration, is_stream, ts_offset=None, text=None, subs=None, ordered=False, audio=None, lead=0.0):
    """ffmpeg for one plan entry. -ss before -i so the seek is fast, -t bounds
    it to the scheduled duration so a file that runs long can't push the
    channel off its own clock. -re paces output at realtime: without it ffmpeg
    would encode a 20-minute segment as fast as the CPU allows and the fanout
    would race ahead of the wall clock it is supposed to track.

    `lead` (a warm start, see WARM_LEAD): it is started that many seconds before it is due, reading from `lead` seconds
    earlier in the file, and the first `lead` seconds of what comes out are dropped before they are encoded. The file is
    opened and seeked during that time, and the picture the viewer gets starts at `seek`, on time."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re"]
    if is_stream:
        cmd += ["-i", path]
    elif lead > 0:
        cmd += ["-ss", f"{max(seek - lead, 0):.3f}", "-i", path, "-ss", f"{lead:.3f}", "-t", f"{duration:.3f}"]
    else:
        cmd += ["-ss", f"{seek:.3f}", "-i", path, "-t", f"{duration:.3f}"]
    enc = with_subtitles(NORMALIZE, subs)
    if audio:                                    # the volume brought to the target: one fixed gain, then a limiter
        enc = enc + ["-af", audio]
    if ordered:                                  # captions are put in frame by frame: the frames must be in the order they are shown
        enc = enc + ["-bf", "0"]
    if ts_offset is not None:                    # timestamps carry on from the last segment: one continuous stream
        return with_text(cmd + enc + ["-output_ts_offset", f"{ts_offset:.3f}"] + _continuous(MUX) + ["pipe:1"], text)
    return with_text(cmd + enc + MUX + ["pipe:1"], text)


def with_subtitles(enc, subs):
    """The encoder options with `subs` (a filter from captions.burn_filter) drawn on the picture, or unchanged."""
    if not subs or "-vf" not in enc:
        return enc
    out = list(enc)
    i = out.index("-vf")
    out[i + 1] = f"{out[i + 1]},{subs}"
    return out


TEXT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def _drawtext(text):
    """An ffmpeg drawtext filter that puts `text` in the bottom right corner on a dark box."""
    t = re.sub(r"[^\w .,:!?'()/#&+@-]", "", str(text))[:70]
    t = t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")
    font = f":fontfile={TEXT_FONT}" if os.path.isfile(TEXT_FONT) else ""
    return (f"drawtext=text='{t}'{font}:fontcolor=white:fontsize=h/26:box=1:boxcolor=black@0.65:boxborderw=10:"
            "x=w-tw-30:y=h-th-30")


def with_text(cmd, text):
    """The same ffmpeg command with `text` drawn in the bottom right of the picture (why a test pattern is on screen)."""
    if not text or "-vf" not in cmd:
        return cmd
    out = list(cmd)
    i = out.index("-vf")
    out[i + 1] = f"{out[i + 1]},{_drawtext(text)}"
    return out


def filler_cmd(seconds, text=None):
    return with_text(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                      "-f", "lavfi", "-i", f"smptebars=size={W}x{H}:rate={FPS}",
                      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                      "-t", f"{max(seconds, 1):.1f}"] + NORMALIZE + MUX + ["pipe:1"], text)


def still_cmd(path, seconds, text=None):
    """A picture held on screen (a be-right-back card) for `seconds`, with silence under it."""
    return with_text(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                      "-loop", "1", "-framerate", str(FPS), "-i", path,
                      "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                      "-t", f"{max(seconds, 1):.1f}"] + NORMALIZE + MUX + ["pipe:1"], text)


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


class Subscriber:
    """One viewer attached to a channel. Bounded queue: a client too slow to
    drain gets dropped rather than back-pressuring the shared encoder and
    stuttering the stream for everyone else on that channel."""

    def __init__(self, depth=512):
        self.q = queue.Queue(maxsize=depth)
        self.dead = threading.Event()
        self.slow = False                      # dropped because it could not keep up (not because it left)

    def put(self, chunk):
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            self.slow = True
            self.dead.set()

    def pump(self, write):
        while not self.dead.is_set():
            try:
                chunk = self.q.get(timeout=5)
            except queue.Empty:
                continue
            if chunk is None:
                return
            write(chunk)


PRIME_BLOCK = 64 * 1024        # bytes in one piece of the replay a new viewer is given


class Channel:
    """One encoder feeding N viewers. Started on the first viewer, stopped
    (after a linger) when the last one leaves."""

    _warm = None                              # the next piece, already started and held (see _prepare_next)
    _warm_timer = None
    _last_chunk_at = None                     # when the last bytes were sent into the channel (to measure the gap at a join)
    _prev_kind = None                         # what kind of piece that was (commercial, bump, feature)
    _join_meta = None                         # what the piece about to play is, for the join report

    @property
    def _warm_lock(self):
        return self.__dict__.setdefault("_warm_lock_obj", threading.Lock())

    def __init__(self, number, station, pool):
        self.number, self.station, self.pool = number, station, pool
        self.subs = set()
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()
        self.prime = deque()
        self.prime_bytes = 0
        self.prime_cap = int(float(V.get("vbitrate", "4000k").rstrip("k")) * 1000 / 8 * PRIME_SECONDS)
        self.reaper = None
        self.now_playing = "--"
        self.reload = threading.Event()       # set to make a running guide channel start over with its new settings
        self.idle_since = None                # when its last viewer left (a channel winding down)
        self.tail = None                      # set when the last viewer left a station of ours: how many breaks may still start
        self.in_break = False                 # the segment being played is a commercial or a mid card
        self.clock = 0.0                      # seconds of stream sent so far, for continuous timestamps (CONTINUOUS_TS)

    def attach(self):
        """Returns a Subscriber, or None if no encoder slot is free. When the encoders are all busy, the channel that has been
        winding down the longest (nobody watching it, only its tail left) gives its slot up to the one being tuned to."""
        sub = self._attach()
        if sub is None and free_longest_idle(self):
            sub = self._attach()
        return sub

    def _attach(self):
        with self.lock:
            if self.reaper:
                self.reaper.cancel()
                self.reaper = None
            self.tail = None                                       # somebody is here again: nothing is winding down
            if self.thread is None:
                if not self.pool.acquire(self.number):
                    return None
                self.stop.clear()
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
                _count("encoder_starts")
                print(f"[ch{self.number}] encoder up -> {self.station}")
            sub = Subscriber()
            # The replay is handed over in blocks, not as the many small pieces ffmpeg wrote it in: a quiet picture (a test
            # pattern for a missing file) is thousands of tiny pieces, which overflowed the viewer's queue at once and dropped it.
            blob = b"".join(self.prime)
            for at in range(0, len(blob), PRIME_BLOCK):
                sub.put(blob[at:at + PRIME_BLOCK])
            self.subs.add(sub)
            print(f"[ch{self.number}] viewer joined ({len(self.subs)} on this channel)")
            return sub

    def detach(self, sub):
        with self.lock:
            self.subs.discard(sub)
            sub.dead.set()
            n = len(self.subs)
            if n == 0:
                self.idle_since = time.time()
            print(f"[ch{self.number}] viewer left ({n} remaining)")
            if n == 0 and self.thread is not None and self.reaper is None:
                ext = EXTERNAL.get(self.station)
                if ext is not None and ext.get("mode") == "convert":
                    delay = EXTERNAL_LINGER                         # a converted source: its picture keeps coming for a while
                elif ext is None and IDLE_TAIL_SECONDS > 0 and guide_conf(self.station) is None:
                    delay = IDLE_TAIL_SECONDS                       # a station of ours: what is queued, then it stops (_step)
                    self.tail = {"breaks": 0 if self.in_break else 1}
                else:
                    delay = LINGER
                self.reaper = threading.Timer(delay, self._shutdown, args=(delay,))
                self.reaper.daemon = True
                self.reaper.start()

    def _shutdown(self, idle=None, why=None):
        with self.lock:
            if self.subs:
                self.reaper = None
                return
            if self.reaper:
                self.reaper.cancel()
            self.stop.set()
            self.thread = None
            self.reaper = None
            self.tail = None
            self.prime.clear()
            self.prime_bytes = 0
            self.now_playing = "--"
        self.pool.release(self.number)
        print(f"[ch{self.number}] encoder down ({why or 'idle %.0fs' % (LINGER if idle is None else idle)})")

    def tail_stops_here(self, is_break):
        """Called as a segment is about to start. After the last viewer left a station of ours it may finish what is queued: the
        programme and the next whole break (with its mid cards). True when this segment would start a break that was not part of
        that, so the channel should stop instead (no file is opened for nobody)."""
        starts = is_break and not self.in_break
        self.in_break = bool(is_break)
        if self.tail is None or self.subs or not starts:
            return False
        if self.tail["breaks"] > 0:
            self.tail["breaks"] -= 1
            return False
        return True

    def _broadcast(self, chunk):
        self.prime.append(chunk)
        self.prime_bytes += len(chunk)
        while self.prime_bytes > self.prime_cap and len(self.prime) > 1:
            self.prime_bytes -= len(self.prime.popleft())
        with self.lock:
            targets = list(self.subs)
        for sub in targets:
            if sub.dead.is_set():
                if sub.slow:
                    print(f"[ch{self.number}] a viewer was dropped: it could not keep up with the stream", flush=True)
                    sub.slow = False
                self.detach(sub)
            else:
                sub.put(chunk)

    def retire(self):
        """This channel is going away (its source was removed or switched off): stop it and let its viewers go."""
        with self.lock:
            self.stop.set()
            had = self.thread is not None
            self.thread = None
            subs, self.subs = list(self.subs), set()
            if self.reaper:
                self.reaper.cancel()
                self.reaper = None
        for sub in subs:
            sub.put(None)
        if had:
            self.pool.release(self.number)

    def _run_external(self, info):
        """A channel of an external IPTV source (iptv.py): ffmpeg reads the stream and makes it this tuner's MPEG-TS, until the
        last viewer leaves. A stream that drops is picked up again a few seconds later."""
        import select
        self.reload.clear()
        self.now_playing = info["name"]
        cmd = iptv.ffmpeg_command(info, NORMALIZE, MUX)
        began = time.time()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            print(f"[ch{self.number}] could not start ffmpeg for {info['name']}: {e}", flush=True)
            self.stop.wait(5)
            return
        sent = 0
        try:
            fd = proc.stdout.fileno()
            while not self.stop.is_set() and not self.reload.is_set():
                ready, _w, _x = select.select([fd], [], [], 1.0)       # looks up every second, so a stalled stream can't hold a stop
                if not ready:
                    continue
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                self._broadcast(chunk)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            why = (proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
            if not self.stop.is_set() and not self.reload.is_set():
                print(f"[ch{self.number}] {info['name']} ({info['source']}) ended after {time.time() - began:.0f}s, {sent // 1024} KB"
                      + (f": {iptv.mask(why[-1])[:200]}" if why else "") + "; trying again in 5s", flush=True)
        if not self.stop.is_set() and not self.reload.is_set():
            self.stop.wait(5)

    def _run(self):
        """Walk the schedule from 'now' forward. Nothing is pre-rendered: the
        schedule is re-read at every segment boundary, so an FS42 rebuild
        mid-stream is picked up and a viewer tuning in at 8:17 joins whatever
        is genuinely airing at 8:17."""
        while not self.stop.is_set():
            try:
                self._step()
            except Exception as e:                    # whatever went wrong, the channel stays up and says what it was
                import traceback
                print(f"[ch{self.number}] an error while playing {self.station}: {e!r}\n{traceback.format_exc()}", flush=True)
                self.now_playing = "Test pattern: error"
                try:
                    self._pipe(filler_cmd(10, ("CHANNEL ERROR: " + type(e).__name__ + ": " + str(e))[:70]))
                except Exception:
                    self.stop.wait(5)
        self._drop_warm()
        with self.lock:
            for sub in list(self.subs):
                sub.put(None)

    def _step(self):
        """One piece of the schedule: the guide or an external stream, or the next segment of what is airing."""
        ext = EXTERNAL.get(self.station)
        if ext is not None:                        # an external IPTV source's channel: read from its stream, not scheduled
            self._run_external(ext)
            return
        gconf = guide_conf(self.station)
        if gconf is not None:                      # the guide channel: drawn here, not scheduled
            self._run_guide(gconf)
            return
        try:
            cur = resolver.resolve(DB_PATH, self.station)
        except ScheduleGap as e:
            print(f"[ch{self.number}] {e} -- filling")
            self.now_playing = "Off air"
            self._pipe(filler_cmd(15, "QUEUEING"))              # the next part of the schedule is not there yet
            return

        entry = cur.entry
        path = entry.path if entry.is_stream else media_path(entry.path)
        name = os.path.basename(entry.path)
        if not entry.is_stream and not os.path.exists(path):
            print(f"[ch{self.number}] missing media: {path}")
            self.now_playing = "Test pattern: no file"
            self._pipe(filler_cmd(min(cur.remaining, 15), f"NO FILE: {name}"))
            return
        if not entry.is_stream and os.path.splitext(path)[1].lower() in IMAGE_EXT:
            # a picture is what the schedule has when a break could not be filled (FieldStation42's be-right-back card)
            why = break_reason(self.station)
            print(f"[ch{self.number}] {why.lower()}: showing {name} for {cur.remaining:.0f}s", flush=True)
            self.now_playing = f"Test pattern: {why.lower()}"
            self._pipe(still_cmd(path, min(cur.remaining, 30), why))
            self.clock += min(cur.remaining, 30)
            return
        offair = offair_reason(self.station, entry, cur.block_title)
        if self.tail_stops_here(entry.content_type in ("commercial", "bump")):
            self._shutdown(why="nobody watching: the queued break has run")
            return

        waiting = self._warm
        if cur.remaining < 1.5 and waiting is not None and (waiting["path"], waiting["skip"]) != (path, entry.skip):
            # What is left of this piece is a moment, and the piece after it is already started and waiting (a piece started
            # early finishes a little before its time). Starting this one again would cost more than the moment is worth and
            # would throw the waiting piece away: wait the moment out and go straight on to it.
            self.stop.wait(max(cur.remaining, 0.0))
            return
        self.now_playing = cur.block_title
        warm = self._take_warm(path, cur)                       # this very piece, started a few seconds early, or None
        if warm is not None:
            local, pinned = warm["local"], warm["pinned"]
            stream_url = warm.get("stream_url")
        else:
            local, pinned = (path, False) if entry.is_stream else PREFETCH.pin(path)     # a copy made ahead of time, if there is one
            stream_url = None if (pinned or entry.is_stream) else stream_open(path, cur.seek)    # else a big file on a share: through the server
        play = stream_url or local
        print(f"[ch{self.number}] {cur.block_title} :: {os.path.basename(entry.path)} "
              f"@{cur.seek:.0f}s for {cur.remaining:.0f}s ({entry.content_type})" + (" [from the read-ahead copy]" if pinned else "")
              + (" [read ahead]" if stream_url else "") + (" [warm start]" if warm is not None else ""))
        try:
            subs = caption_filter(self.station, entry, path, local, cur.seek)
            inject = caption_injector(self.station, entry, path, cur.seek)
            began = time.time()
            self._join_meta = {"to": entry.content_type, "file": name, "due": time.time() - cur.offset,
                               "flags": [f for f, on in (("warm", warm is not None), ("read-ahead", bool(stream_url)),
                                                         ("copy", bool(pinned))) if on]}
            self._arm_warm(entry, cur, began)                    # and the one after it, a few seconds before this one ends
            if warm is not None:
                sent = self._pipe(None, label=name, cached=pinned, inject=inject, proc=warm["proc"])
            else:
                sent = self._pipe(segment_cmd(play, cur.seek, cur.remaining, entry.is_stream, self.clock if continuous_ts() else None,
                                              text=offair, subs=subs,
                                              ordered=inject is not None,
                                              audio=audio_level_filter(self.station, entry, path)),
                                  label=name, cached=pinned, inject=inject)
            if sent < 1024 and stream_url and not self.stop.is_set() and not self.reload.is_set():
                # the server could not give it: read the file as it is, now, and leave it out of the server for a while
                print(f"[ch{self.number}] the read-ahead server gave nothing for {name}: reading it directly", flush=True)
                STREAMER.mark_failed(stream_url)
                sent = self._pipe(segment_cmd(local, cur.seek, cur.remaining, entry.is_stream, None, text=offair, subs=subs,
                                              ordered=inject is not None, audio=audio_level_filter(self.station, entry, path)),
                                  label=name, cached=pinned, inject=inject)
            self.clock += cur.remaining
            self._prev_kind = entry.content_type
            if sent < 1024 and not entry.is_stream and not self.stop.is_set() and not self.reload.is_set():
                # ffmpeg gave nothing for a file that is there: it can't be read. Say so, and don't spin on it.
                print(f"[ch{self.number}] bad file (nothing came out of it): {path}", flush=True)
                self.now_playing = "Test pattern: bad file"
                self._pipe(filler_cmd(min(max(cur.remaining, 1), 10), f"BAD FILE: {name}"))
        finally:
            if pinned:
                PREFETCH.unpin(path)
            if stream_url:
                STREAMER.close(stream_url)

    def _run_guide(self, conf):
        """The guide channel: draw frames (guide_channel.py) into one ffmpeg with the music underneath, until the
        last viewer leaves. It starts over each hour, so the music gets a new shuffle."""
        self.reload.clear()
        opts = guide_channel.options(conf)
        files, ready = [], True
        if opts["music"]:                              # a big music folder on a slow share must not hold the picture up
            began = time.time()
            music_path = media_path(opts["music"])
            files, ready = GUIDE_MUSIC.get(music_path, budget=3.0)
            if not ready:
                print(f"[ch{self.number}] guide channel: still looking through {opts['music']} for music; starting without it", flush=True)

                def restart_with_music():
                    got = GUIDE_MUSIC.wait(music_path)
                    print(f"[ch{self.number}] guide channel: found {len(got)} music file(s) after {time.time() - began:.0f}s", flush=True)
                    if got and not self.stop.is_set():
                        self.reload.set()               # start over, this time with the music
                threading.Thread(target=restart_with_music, daemon=True, name="guide-music-wait").start()
        self.now_playing = opts["title"]
        playlist = None
        try:
            guide = guide_channel.Guide(opts, (W, H), rows_fn=lambda a, b: guide_channel.rows_from_xmltv(
                GUIDE.xmltv(), a, b, skip_names=tuple(GUIDE_STATIONS())))
            if files:
                fd, playlist = tempfile.mkstemp(prefix="guide-", suffix=".ffconcat")
                with os.fdopen(fd, "w") as f:
                    f.write(guide_channel.playlist_text(files))
            print(f"[ch{self.number}] guide channel: {len(files)} music file(s)" if opts["music"] else f"[ch{self.number}] guide channel: no music", flush=True)
            cmd = guide_channel.ffmpeg_command((W, H), GUIDE_FPS, playlist, NORMALIZE, MUX)
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except Exception as e:                        # no Pillow, no ffmpeg...: say so, and don't spin
            print(f"[ch{self.number}] the guide channel could not start: {e}", flush=True)
            if playlist:
                os.unlink(playlist)
            self._pipe(filler_cmd(15))
            return
        ends = time.time() + 3600
        writer = threading.Thread(target=guide_channel.feed, args=(proc.stdin, guide, GUIDE_FPS, self.stop), daemon=True)
        writer.start()
        try:
            while not self.stop.is_set() and not self.reload.is_set() and time.time() < ends:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                self._broadcast(chunk)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if playlist:
                try:
                    os.unlink(playlist)
                except OSError:
                    pass

    # ---- warm start: the piece after this one is opened and seeked while this one is still playing
    def _arm_warm(self, entry, cur, began):
        """Set a timer to start the next piece early, if this is a kind of piece that is followed by one worth starting early."""
        if WARM_LEAD <= 0 or entry.is_stream or self.stop.is_set():
            return
        boundary = began + cur.remaining
        delay = boundary - WARM_STARTUP - WARM_LEAD - time.time()
        if cur.remaining < 1:
            return
        timer = threading.Timer(max(delay, 0.0), self._prepare_next, args=(boundary, self.clock + cur.remaining))
        timer.daemon = True
        with self._warm_lock:
            if self._warm_timer is not None:
                self._warm_timer.cancel()
            self._warm_timer = timer
        timer.start()

    def _prepare_next(self, boundary, clock_next):
        """Start the next piece's ffmpeg a little before `boundary` (when this piece ends) so that its first picture comes out
        when it is due. Nothing is read from it until then. Skipped (and so played the ordinary way) for a piece that is not a
        plain file."""
        try:
            if self.stop.is_set() or self.reload.is_set():
                return
            wait = max(boundary - time.time(), 0.0)
            when = datetime.now() + timedelta(seconds=wait + 0.05)
            nxt = resolver.resolve(DB_PATH, self.station, when)
            e = nxt.entry
            if e.is_stream or nxt.offset > 1.0:
                return
            path = media_path(e.path)
            if os.path.splitext(path)[1].lower() in IMAGE_EXT or not os.path.exists(path):
                return
            if e.content_type in ("commercial", "bump") and self.tail is not None:
                return                          # a channel winding down: the tail rules decide on a break when it is due
            # A piece that joins part way into its file is started `lead` seconds early, reading from `lead` earlier (the open
            # and the seek happen in that time). One that starts at the beginning of its file has nothing earlier to read, so it is
            # started just early enough that its first picture comes out when it is due (WARM_STARTUP before).
            lead = min(WARM_LEAD, e.skip, max(wait - WARM_STARTUP, 0.0)) if e.skip >= 1.0 else 0.0
            if lead < 0.5:
                lead = 0.0
            pause = boundary - WARM_STARTUP - lead - time.time()
            if pause > 0 and self.stop.wait(pause):
                return
            if self.stop.is_set() or self.reload.is_set():
                return
            local, pinned = PREFETCH.pin(path)
            stream_url = None if pinned else stream_open(path, e.skip)
            offair = offair_reason(self.station, e, nxt.block_title)
            began = time.time()
            subs = caption_filter(self.station, e, path, local, e.skip)
            inject_ok = caption_injector(self.station, e, path, e.skip) is not None
            cmd = segment_cmd(stream_url or local, e.skip, nxt.remaining + nxt.offset, False, clock_next if continuous_ts() else None, text=offair,
                              subs=subs, ordered=inject_ok, audio=audio_level_filter(self.station, e, path), lead=lead)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            with self._warm_lock:
                old, self._warm = self._warm, {"proc": proc, "path": path, "skip": e.skip, "local": local, "pinned": pinned,
                                               "began": began, "lead": lead, "stream_url": stream_url}
            if old:
                self._release(old)
        except resolver.ScheduleGap:
            pass                                 # nothing follows (the end of what is built): the ordinary way will say so
        except Exception as ex:                  # a warm start is only an optimisation: never a reason to lose the channel
            print(f"[ch{self.number}] warm start skipped: {ex!r}", flush=True)

    def _take_warm(self, path, cur):
        """The piece started early, if it is the one now due (the same file, joining where it was told to); else it is
        dropped and None is returned."""
        with self._warm_lock:
            warm, self._warm = self._warm, None
        if warm is None:
            return None
        ok = (warm["path"] == path and abs(cur.entry.skip - warm["skip"]) < 0.5 and cur.offset < 4.0 + warm["lead"]
              and warm["proc"].poll() is None)
        if not ok:
            self._release(warm)
            return None
        return warm

    def _release(self, warm):
        try:
            if warm["proc"].poll() is None:
                warm["proc"].terminate()
                try:
                    warm["proc"].wait(timeout=3)
                except subprocess.TimeoutExpired:
                    warm["proc"].kill()
        except Exception:
            pass
        if warm.get("pinned"):
            PREFETCH.unpin(warm["path"])
        if warm.get("stream_url"):
            STREAMER.close(warm["stream_url"])

    def _drop_warm(self):
        with self._warm_lock:
            if self._warm_timer is not None:
                self._warm_timer.cancel()
                self._warm_timer = None
            warm, self._warm = self._warm, None
        if warm:
            self._release(warm)

    def _pipe(self, cmd, label=None, cached=False, inject=None, proc=None, began=None):
        """Run one ffmpeg into the channel (or carry on with `proc`, one started a little early). Returns how many bytes came
        out of it."""
        began = began or time.time()
        if proc is None:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        first = True
        sent = 0
        try:
            while not self.stop.is_set():
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                if first:                  # how long a file took to start: slow ones are what a viewer sees as buffering
                    first = False
                    took = time.time() - began
                    meta, self._join_meta = self._join_meta, None
                    if meta is not None:
                        # how late the picture was against when the schedule had it due; for a tune-in (the piece was
                        # already part way through) only the time to the first picture means anything
                        tune_in = self._prev_kind is None
                        late = None if tune_in else time.time() - meta["due"]
                        record_join({"ts": time.time(), "at": datetime.now().strftime("%H:%M:%S"), "channel": self.number,
                                     "station": self.station, "from": self._prev_kind or "tune-in", "to": meta["to"],
                                     "flags": meta["flags"], "first_picture_s": round(took, 2),
                                     "late_s": round(late, 2) if late is not None else None, "file": meta["file"]})
                    if label and took > SLOW_START_SECONDS:
                        print(f"[ch{self.number}] {label} took {took:.1f}s to start" + (" (from the read-ahead copy)" if cached else " (from the share)"), flush=True)
                sent += len(chunk)
                self._last_chunk_at = time.time()
                chunk, inject = caption_feed(inject, chunk, self.number)
                if chunk:
                    self._broadcast(chunk)
            if inject is not None:
                try:
                    tail = inject.flush()
                except Exception:
                    tail = b""
                if tail:
                    self._broadcast(tail)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return sent


def offair_reason(station, entry, block_title):
    """The words for a test pattern that is the schedule's own off-air video, or None when this is a programme. It is off
    air when the block is named so, FieldStation42 says the content is off air, or the file is the shipped test pattern.
    The reason is the lists the station's calendar left out because they had no videos, else that nothing is scheduled."""
    name = os.path.basename(str(getattr(entry, "path", "") or "")).lower()
    shipped = os.path.basename(stationform.OFF_AIR_CLIP).lower()
    if not (str(block_title).strip().lower() in ("offair", "off air", "off-air") or getattr(entry, "content_type", "") == "off_air"
            or (name and name == shipped)):
        return None
    try:
        empty = sorted(schedules.skipped_of(station), key=str.casefold)
    except Exception:
        empty = []
    if empty:
        return "NO VIDEOS IN: " + ", ".join(empty[:2]) + (" +%d" % (len(empty) - 2) if len(empty) > 2 else "")
    return "OFF AIR: NOTHING SCHEDULED"


def break_reason(station):
    """Why a be-right-back card is on a station's screen: no commercial to fill the break, or no bump to go around it. Looked
    up from what the catalog holds (and what the station asks for), because the schedule only says "a picture here"."""
    try:
        conf = CONSOLE.stations().get(station) or {}
    except Exception:
        conf = {}
    counts = {}
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2) as conn:
            for kind, n in conn.execute("SELECT content_type, COUNT(*) FROM catalog_entries WHERE station = ? GROUP BY content_type", (station,)):
                counts[kind] = n
    except sqlite3.Error:
        pass
    wants_ads = not conf.get("commercial_free", True) and conf.get("commercial_dir") not in (None, "", ".commercials.off")
    if wants_ads and not counts.get("commercial"):
        return "NO COMMERCIAL"
    if conf.get("wrapper_mid_cards") is not False and not counts.get("bump"):
        return "NO BUMP"
    return "NO COMMERCIAL" if wants_ads else "NO BUMP"


def free_longest_idle(wanting):
    """All the encoders are busy and somebody tunes to `wanting`: stop the channel that has been idle the longest (no viewers, its
    encoder only running out its wind-down) and let it have the slot. The TV guide channel is never the one chosen. Returns True
    when a slot was freed."""
    best = None
    for chan in list(CHANS.values()):
        if chan is wanting or chan.thread is None or chan.subs or chan.idle_since is None:
            continue
        if guide_conf(chan.station) is not None:
            continue
        if best is None or chan.idle_since < best.idle_since:
            best = chan
    if best is None:
        return False
    print(f"[ch{best.number}] giving its encoder to channel {wanting.number}: it has been idle the longest", flush=True)
    best._shutdown(why=f"freed for channel {wanting.number}")
    return best.thread is None


class EncoderPool:
    """Caps how many distinct FS42 channels encode at once -- the real CPU limit."""

    def __init__(self, n):
        self.n = n
        self.active = set()
        self.lock = threading.Lock()

    def acquire(self, channel):
        with self.lock:
            if channel in self.active:
                return True
            if len(self.active) >= self.n:
                return False
            self.active.add(channel)
            return True

    def release(self, channel):
        with self.lock:
            self.active.discard(channel)

    def count(self):
        with self.lock:
            return len(self.active)


titles.configure(settings.get_titles()["episode_names"])
resolver.NAMES_HOOK = titles.hook(CATALOG_ROOT, names=True)
resolver.TITLE_HOOK = titles.hook(CATALOG_ROOT)     # the guide, banner and schedule say what a video is called (its .nfo), not its file name
GUIDE_MUSIC = guide_channel.MusicIndex(os.path.join(os.path.dirname(os.environ.get("FS42_TUNER_CONF", "/config/tuner/config.json")), "guide-music.json"))
POOL = EncoderPool(MAX_ENCODERS)
CHANS = {n: Channel(n, s, POOL) for n, s in CHANNELS.items()}
EXTERNAL = {}                       # "iptv:<number>" -> the channel of an external IPTV source (iptv.py) that has that number
SOURCES = iptv.Sources(os.environ.get("FS42_IPTV") or os.path.join(os.path.dirname(os.environ.get("FS42_TUNER_CONF", "/config/tuner/config.json")), "sources.json"))
SOURCES_LOCK = threading.Lock()
EXTERNAL_CLASHES = []               # channels whose number was taken, from the last sync
SLOW_START_SECONDS = float(os.environ.get("SLOW_START_SECONDS", "2"))
# Warm start: the piece that follows is started this many seconds BEFORE it is due (reading from this far ahead of where it
# joins), so its file is already open, seeked and decoding when the one before it ends. Only for a piece that joins part way
# into its file (a show resuming after a break), where the open and seek are the slow part. 0 switches it off.
def _seconds(name, default):
    """A number of seconds from the environment; blank (a compose file passes one) is the default, 0 is zero."""
    try:
        text = os.environ.get(name, "").strip()
        return max(float(text), 0.0) if text else default
    except ValueError:
        return default


# The joins between pieces on every channel, newest last: what a viewer's player lives with. Kept in memory (the last 200).
JOINS = deque(maxlen=200)
JOIN_SLOW = 1.0                      # seconds: a join slower than this is written to the log


def record_join(row):
    JOINS.append(row)
    waited = row["late_s"] if row.get("late_s") is not None else row["first_picture_s"]
    if waited > JOIN_SLOW:
        print(f"[ch{row['channel']}] slow join: {row['from']} -> {row['to']} ({row['file']}): "
              + (f"its picture was {row['late_s']:.1f}s later than due" if row.get("late_s") is not None
                 else f"{row['first_picture_s']:.1f}s to the first picture")
              + (f" [{', '.join(row['flags'])}]" if row["flags"] else ""), flush=True)


def joins_report(now=None, recent=20):
    """The last joins and a summary of the last hour: how late each piece's picture came against when the schedule had it due
    (or, for a tune-in, the time to the first picture), by median, 90th percentile and worst, with warm starts against plain."""
    now = now or time.time()
    rows = [r for r in list(JOINS)]
    hour = [r for r in rows if r["ts"] >= now - 3600]

    def wait(r):
        return max(0.0, r["late_s"]) if r.get("late_s") is not None else r["first_picture_s"]
    vals = sorted(wait(r) for r in hour)
    summary = {"count": len(vals)}
    if vals:
        summary.update(median_s=round(vals[len(vals) // 2], 2), p90_s=round(vals[min(len(vals) - 1, int(0.9 * len(vals)))], 2),
                       worst_s=round(vals[-1], 2),
                       slow=sum(1 for v in vals if v > JOIN_SLOW))
        by = {}
        for r in hour:
            key = "warm" if "warm" in r["flags"] else ("tune-in" if r["from"] == "tune-in" else "plain")
            by.setdefault(key, []).append(wait(r))
        summary["by"] = {k: {"count": len(v), "median_s": round(sorted(v)[len(v) // 2], 2)} for k, v in by.items()}
    last = [{k: r.get(k) for k in ("at", "channel", "station", "from", "to", "flags", "first_picture_s", "late_s", "file")}
            for r in rows[-recent:]]
    return {"summary": summary, "recent": list(reversed(last))}


WARM_LEAD = _seconds("WARM_START_SECONDS", 4.0)
WARM_STARTUP = _seconds("WARM_START_STARTUP_SECONDS", 0.8)     # what an ffmpeg takes to put out a first picture
GUIDE_FPS = max(5, min(30, int(os.environ.get("GUIDE_FPS", "15"))))     # frames per second the guide channel is drawn at
# Read ahead for the channels being watched (prefetch.py): the next break or episode is copied into RAM before it is due
_PREFETCH_CFG = prefetch.env_config()
if not os.environ.get("PREFETCH_STATS"):                    # what it has learned about each share, beside the other saved settings
    _PREFETCH_CFG["stats_path"] = os.path.join(os.path.dirname(settings.PATH), "prefetch-stats.json")
PREFETCH = prefetch.Prefetcher(_PREFETCH_CFG, DB_PATH,
                               watched=lambda: {n: c.station for n, c in CHANS.items() if c.subs},
                               media_root=CONF.get("media_root", "/mnt/media"), path_of=lambda p: media_path(p))
# Big files on a network share are read through a small local server that holds a head start in RAM (then a local disk
# folder) and drops what has been played (streaming.py). Small files and files on local disk are read as before.
def _file_rate(real):
    """The bytes per second a video plays at (its size over the catalog's duration for it), or None when not known."""
    try:
        size = os.stat(real).st_size
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute("SELECT duration FROM catalog_entries WHERE realpath = ? OR path = ? ORDER BY duration DESC LIMIT 1",
                               (real, real)).fetchone()
        return size / row[0] if row and row[0] and row[0] > 30 else None
    except Exception:
        return None


_STREAM_CFG = streaming.env_config()
STREAMER = streaming.Streamer(
    _STREAM_CFG if PREFETCH.enabled else {**_STREAM_CFG, "enabled": False}, PREFETCH.stats, CONF.get("media_root", "/mnt/media"),
    os.path.join(PREFETCH.cfg["dir"], "stream") if PREFETCH.enabled else None,
    (lambda: max(0, PREFETCH.cache.max_bytes - PREFETCH.cache.held_bytes())) if PREFETCH.enabled else (lambda: 0),
    (_STREAM_CFG["disk_dir"] or os.path.join(CATALOG_ROOT, "catalog", ".prefetch")) if PREFETCH.enabled and _STREAM_CFG["disk_bytes"] > 0 else None,
    rate_of=_file_rate)
if PREFETCH.enabled:
    PREFETCH.cache.external = lambda: STREAMER.used["ram"]       # what the server holds in the RAM disk counts against it
    PREFETCH.streamer = STREAMER


def stream_open(path, seek):
    """The URL to read a file through the read-ahead server, or None (read the file as it is): it is not a big file on a share,
    or the server is off or cannot take it."""
    try:
        real = os.path.realpath(path)
        if STREAMER.qualifies(real):
            return STREAMER.open(real, seek)
    except Exception as e:
        print(f"[stream] {os.path.basename(path)}: {e}", flush=True)
    return None


LEVELS = loudness.Levels(os.path.join(os.path.dirname(settings.PATH), "loudness.json"), LIBRARY_ROOT,
                         target_fn=lambda: settings.get_loudness()["target"])
VIEWERS = threading.BoundedSemaphore(TUNER_COUNT)
PT_BUDGET = None            # set at startup once the real device is queried


def upstream_map():
    """number -> upstream channel dict, or {} when passthrough is off/unreachable."""
    if UPSTREAM is None:
        return {}
    try:
        return {c["GuideNumber"]: c for c in UPSTREAM.lineup()}
    except UpstreamError as e:
        print(f"! {e}")
        return {}


def display_name(station):
    """What a channel is called to people: a station's own name, or an external channel's name (not its internal key)."""
    ext = EXTERNAL.get(station)
    return ext["name"] if ext else station


def sync_external():
    """Make the channels of the external IPTV sources the channels the tuner has: new ones are added (to the lineup, the
    guide, the player and the dashboard), changed ones restart, and ones that are gone or switched off are stopped."""
    global EXTERNAL_CLASHES
    with SOURCES_LOCK:
        taken = {n for n, s in CHANNELS.items() if s not in EXTERNAL}
        try:
            taken |= set(upstream_map())                         # the single passthrough device's numbers too
        except Exception:
            pass
        plan, clashes = SOURCES.plan(taken)
        want = {c["number"]: c for c in plan}
        for num, c in want.items():
            station = f"iptv:{num}"
            changed = EXTERNAL.get(station) != c
            EXTERNAL[station] = c
            CHANNELS[num] = station
            if num not in CHANS:
                CHANS[num] = Channel(num, station, POOL)
            elif changed:
                CHANS[num].reload.set()                        # a different stream now: start it again
        for station in [s for s in EXTERNAL if s[5:] not in want]:
            num = station[5:]
            EXTERNAL.pop(station, None)
            CHANNELS.pop(num, None)
            chan = CHANS.pop(num, None)
            if chan is not None:
                chan.retire()
        EXTERNAL_CLASHES = clashes
    if GUIDE is not None:
        threading.Thread(target=lambda: GUIDE.xmltv(force=True), daemon=True, name="guide-after-sources").start()
    return len(want)


def external_guide_sources():
    """Guide sources for the tuners on the Sources page, so their channels have real listings in OUR guide (the one place
    Jellyfin and the guide channel look). Each tuner's own channels keep the numbers the lineup gave them."""
    out = []
    with SOURCES_LOCK:
        wanted = [(s["id"], s["name"], s["url"], {e["key"]: e["number"] for e in EXTERNAL.values() if e["source_id"] == s["id"]})
                  for s in SOURCES.all() if s["type"] == "hdhomerun" and s.get("enabled", True)]
    guide_only = {s["id"] for s in SOURCES.all() if s.get("mode") == "guide"}
    for s in SOURCES.all():                                       # a playlist's own guide (url-tvg), put on its channels
        if s["type"] != "m3u" or not s.get("enabled", True) or not s.get("guide_urls"):
            continue
        with SOURCES_LOCK:
            mapped = {e["key"]: e["number"] for e in EXTERNAL.values() if e["source_id"] == s["id"]}
        if mapped:
            for i, u in enumerate(s["guide_urls"]):
                out.append({"type": "xmltv", "name": f"playlist {s['name']} guide {i + 1}", "url": u, "retag": mapped, "only_mapped": True})
    for sid, name, url, numbers in wanted:
        try:
            auth = json.loads(iptv._get(url.rstrip("/") + "/discover.json", 65536)).get("DeviceAuth")
        except Exception as e:
            print(f"! guide for tuner {name}: {e}", flush=True)
            continue
        if auth:
            out.append({"type": "hdhomerun", "name": f"tuner {name}", "device_auth": auth, "id_prefix": f"ext{sid}",
                        "renumber": {} if sid in guide_only else numbers})
    return out


def refresh_source(source_id):
    """Look at a source's channels again. A source that can't be read keeps the channels it had, with the reason."""
    src = SOURCES.get(source_id)
    try:
        channels = iptv.fetch_channels(src)
        SOURCES.set_channels(source_id, channels, None)
    except iptv.SourceError as e:
        SOURCES.set_channels(source_id, None, str(e))
    sync_external()


def migrate_legacy():
    """The single `passthrough` device in the config file becomes a source on the Sources page, and leaves the config file."""
    global UPSTREAM, PT_URL
    if not PT_URL:
        raise iptv.SourceError("there is no passthrough device set in the config file")
    src = next((s for s in SOURCES.describe() if s["type"] == "hdhomerun" and s["url"].rstrip("/") == PT_URL.rstrip("/")), None)
    if src is None:
        src = SOURCES.save({"name": "HDHomeRun", "type": "hdhomerun", "url": PT_URL, "mode": "convert"})
    refresh_source(src["id"])
    conf_path = os.environ.get("FS42_TUNER_CONF", "")
    with open(conf_path) as f:
        data = json.load(f)
    data.pop("passthrough", None)
    tmp = conf_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, conf_path)
    UPSTREAM, PT_URL = None, None                        # this run already serves it as a source
    return src["id"]


def refresh_sources_loop(hours=6):
    """Keep the channel lists of the playlist and tuner sources fresh, and read any source that has none yet."""
    time.sleep(10)
    while True:
        for src in SOURCES.all():
            due = not src.get("refreshed") or time.time() - src["refreshed"] > hours * 3600
            if src.get("enabled", True) and src["type"] != "stream" and due:
                try:
                    refresh_source(src["id"])
                except Exception as e:
                    print(f"! source {src.get('name')}: {e}", flush=True)
        time.sleep(900)


def merged_lineup(base=None):
    base = base or ADVERTISE
    rows = []
    for num, station in sorted(CHANNELS.items(), key=lambda kv: _sortkey(kv[0])):
        rows.append({"GuideNumber": num, "GuideName": display_name(station),
                     "URL": f"{base}/auto/v{num}", "Favorite": FAVOR_FS42 and station not in EXTERNAL})
    for num, c in sorted(upstream_map().items(), key=lambda kv: _sortkey(kv[0])):
        # Passed through verbatim so existing guide mappings keep matching.
        # In "direct" mode the lineup carries the real device's own URL and
        # Jellyfin talks to it straight, bypassing this process entirely.
        url = c["URL"] if PT_MODE == "direct" else f"{base}/auto/v{num}"
        rows.append({"GuideNumber": num, "GuideName": c["GuideName"],
                     "URL": url, "HD": c.get("HD", 0), "Favorite": False})
    return rows


def _sortkey(num):
    try:
        return (0, float(num))
    except ValueError:
        return (1, 0.0)


GUIDE = None        # built at startup, once the upstream is known


def build_guide():
    """One XMLTV for everything in the lineup.

    Defaults to FieldStation42 plus, when a real tuner is passed through,
    SiliconDust's guide for it -- so a single provider URL in Jellyfin covers
    both sets of channels and series recordings work across the lot. Add
    "sources" to the guide config to merge your own XMLTV instead or as well.
    """
    sources = list(GUIDE_CONF.get("sources") or [])
    if not sources and UPSTREAM is not None:
        sources = [{"type": "hdhomerun"}]
    gb = GuideBuilder(
        fs42_channels=CHANNELS,
        resolver=resolver,
        db_path=DB_PATH,
        guide_hours=GUIDE_HOURS,
        sources=sources,
        refresh_minutes=int(GUIDE_CONF.get("refresh_minutes", 60)),
        upstream=UPSTREAM,
    )
    gb.cache_dir = os.path.dirname(os.environ.get("FS42_TUNER_CONF", "/config/tuner/config.json"))
    return gb


def GUIDE_STATIONS():
    """Names of the guide channels, which are left out of their own listing."""
    guide_conf("")                                  # refresh the cache
    return list(_GUIDE_CONFS["confs"])


_GUIDE_LOGOS = {"doc": None, "map": {}}


def guide_logos():
    """{channel number: icon address} from the channels of our guide (an antenna's logos come with its listings), read
    again only when the guide itself has been made again."""
    try:
        doc = GUIDE.xmltv() if GUIDE is not None else None
    except Exception:
        return _GUIDE_LOGOS["map"]
    if doc is None or _GUIDE_LOGOS["doc"] is doc:
        return _GUIDE_LOGOS["map"]
    found = {}
    try:
        from defusedxml import ElementTree as SafeET
        for ch in SafeET.fromstring(doc).findall("channel"):
            icon = ch.find("icon")
            src = icon.get("src") if icon is not None else ""
            if not str(src).lower().startswith(("http://", "https://")):
                continue
            for name in ch.findall("display-name"):
                text = (name.text or "").strip()
                if re.fullmatch(r"\d+(\.\d+)?", text):
                    found[text] = src
    except Exception:
        found = _GUIDE_LOGOS["map"]
    _GUIDE_LOGOS["doc"], _GUIDE_LOGOS["map"] = doc, found
    return found


def _external_logo(ext, num):
    """The logo of an external source's channel for the dashboard: the one its source gave it, else the one the guide has
    for that channel number, else none. Only an http or https address."""
    for src in (ext.get("logo"), guide_logos().get(str(num))):
        if str(src or "").lower().startswith(("http://", "https://")):
            return src
    return ""


_FILE_GONE = {}
_BREAK_WHY = {}


def _file_missing(path):
    """Is this video's file gone? One stat, remembered for a minute (the dashboard asks every few seconds)."""
    now = time.time()
    hit = _FILE_GONE.get(path)
    if hit and now - hit[0] < 60:
        return hit[1]
    gone = not os.path.exists(path)
    if len(_FILE_GONE) > 500:
        _FILE_GONE.clear()
    _FILE_GONE[path] = (now, gone)
    return gone


def pattern_reason(chan, cur):
    """The words for a channel that shows the test pattern, or None when it is playing a programme: nothing scheduled, the
    schedule's own off-air video, a file that is gone, or what the encoder itself fell back to. Looks at no video's data."""
    if cur is None:
        return "NO PROGRAMMING"
    entry = cur.entry
    why = offair_reason(chan.station, entry, cur.block_title)
    if why:
        deliberate = (getattr(entry, "content_type", "") != "off_air"
                      and str(cur.block_title).strip().lower() not in ("offair", "off air", "off-air"))
        return "TEST PATTERN" if deliberate else why        # a station that plays the test card as its programme, or time nothing fills
    if not entry.is_stream:
        path = media_path(entry.path)
        if os.path.splitext(path)[1].lower() in IMAGE_EXT:         # a picture is what an empty break shows
            now = time.time()
            hit = _BREAK_WHY.get(chan.station)
            if not hit or now - hit[0] > 30:
                hit = (now, break_reason(chan.station))
                _BREAK_WHY[chan.station] = hit
            return hit[1]
        if _file_missing(path):
            return ("NO FILE: " + os.path.basename(entry.path))[:60]
    now_playing = str(chan.now_playing or "")
    if now_playing.startswith("Test pattern: "):                    # while it is encoding, what it fell back to and why
        return now_playing[len("Test pattern: "):].upper()[:60]
    return None


def channels_status():
    rows = []
    for num, chan in sorted(CHANS.items(), key=lambda kv: _sortkey(kv[0])):
        ext = EXTERNAL.get(chan.station)
        if ext is not None:                           # an external IPTV channel: always offered, and marked as one
            rows.append({"channel": num, "station": ext["name"], "on_air": True, "viewers": len(chan.subs),
                         "encoding": chan.thread is not None,
                         "now": {"title": ext["name"], "file": ext["source"], "content_type": "iptv", "is_stream": True,
                                 "elapsed": 0, "duration": 0, "block_end": ""},
                         "external": {"source": ext["source"], "type": ext["type"], "mode": ext["mode"],
                                      "logo": _external_logo(ext, num)}})
            continue
        gc_ = guide_conf(chan.station)
        if gc_ is not None:                         # a guide channel is always on air: it is drawn live
            rows.append({"channel": num, "station": chan.station, "on_air": True, "viewers": len(chan.subs),
                         "encoding": chan.thread is not None,
                         "now": {"title": guide_channel.options(gc_)["title"], "file": "", "content_type": "guide", "is_stream": True,
                                 "elapsed": 0, "duration": 0, "block_end": ""}})
            continue
        try:
            cur = resolver.resolve(DB_PATH, chan.station)
        except (ScheduleGap, sqlite3.Error):
            cur = None
        row = {"channel": num, "station": chan.station, "on_air": cur is not None,
               "viewers": len(chan.subs), "encoding": chan.thread is not None, "now": None}
        row["pattern"] = pattern_reason(chan, cur)
        if cur is not None:
            row["now"] = {
                "title": cur.block_title,
                "file": os.path.basename(cur.entry.path),
                "content_type": cur.entry.content_type,
                "is_stream": cur.entry.is_stream,
                "elapsed": round(cur.offset, 1),
                "duration": round(cur.entry.duration, 1),
                "block_end": cur.block_end.isoformat(),
            }
        rows.append(row)
    return rows


def _meminfo():
    try:
        vals = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0])
        return {"total_mb": vals["MemTotal"] // 1024, "available_mb": vals["MemAvailable"] // 1024}
    except (OSError, KeyError, ValueError):
        return None


def _ffmpeg_info():
    exe = shutil.which("ffmpeg")
    if not exe:
        return {"found": False}
    try:
        first = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=5).stdout.splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        first = ""
    return {"found": True, "version": first[:80]}


def _fs42_ref():
    try:
        with open("/opt/FS42_REF") as f:
            return f.read().split()[0][:12]
    except (OSError, IndexError):
        return None


def status_report(recent=60):
    """Everything the Status page and the diagnostics download show: versions, the console, each station's schedule, the guide,
    the sources, disk space, what was last printed, and a short list of plain-language checks."""
    try:
        console = {"reachable": True, "stations": len(CONSOLE.stations())}
    except Exception as e:
        console = {"reachable": False, "error": str(e)}
    try:
        stations_rows = [{k: s.get(k) for k in ("name", "number", "type", "on_air", "scheduled_until", "low", "editable")}
                         for s in STATIONS.listing()] if console["reachable"] else []
    except Exception as e:
        stations_rows = []
        console = {**console, "error": f"could not read the stations: {e}"}
    media_root = CONF.get("media_root", "/mnt/media")
    try:
        media = {"path": media_root, "present": bool(os.listdir(media_root))}
    except OSError:
        media = {"path": media_root, "present": False}
    sysinfo = system_status()
    report = {
        "version": {"wrapper": os.environ.get("FS42_WRAPPER_REF"), "commit": os.environ.get("FS42_WRAPPER_SHA"),
                    "built": os.environ.get("FS42_WRAPPER_BUILT"), "ta_nfowriter": os.environ.get("TA_NFOWRITER_VERSION"),
                    "fieldstation42": _fs42_ref()},
        "uptime_seconds": sysinfo["uptime_seconds"], "console": console, "stations": stations_rows,
        "guide": sysinfo["guide"], "sources": [{"name": s["name"], "type": s["type"], "enabled": s["enabled"], "channels": len(s["channels"]),
                                                 "error": s["error"], "refreshed": s["refreshed"]} for s in SOURCES.describe()],
        "media": media, "ffmpeg": _ffmpeg_info(),
        "disk": diag.disk_free({"config": os.path.dirname(os.environ.get("FS42_TUNER_CONF", "/config/tuner/config.json")) or "/",
                                "catalog": os.path.join(CATALOG_ROOT, "catalog")}),
        "encoders": sysinfo["encoders"], "viewers": sysinfo["viewers"], "memory": sysinfo["memory"], "cpu": sysinfo["cpu"],
        "advertise": {"configured": ADVERTISE, "used_for_clients": "the address each client itself used" if ADVERTISE.split("//")[-1].startswith(("localhost", "127.0.0.1")) else ADVERTISE},
        "recent": diag.RING.recent(recent),
        "recent_problems": diag.RING.recent(15, problems_only=True),
    }
    report["checks"] = diag.checks(report)
    return report


def lineup_m3u(base):
    """The lineup as an M3U playlist (for TVHeadend and other IPTV software). tvg-id is the channel's id in /xmltv.xml, so the
    guide lines up with the channel; tvg-chno is its number."""
    out = ["#EXTM3U"]
    for row in merged_lineup(base):
        num = row["GuideNumber"]
        cid = f"fs42.{num}" if num in CHANNELS else f"hdhr.{num}"
        group = "Station Wrapper 43"
        station = CHANNELS.get(num)
        ext = EXTERNAL.get(station) if station else None
        if ext:
            group = f"IPTV: {ext['source']}"
        elif num not in CHANNELS:
            group = "Antenna"
        name = str(row["GuideName"]).replace(",", " ").replace("\n", " ")
        logo = f' tvg-logo="{ext["logo"]}"' if ext and ext.get("logo") and '"' not in ext["logo"] else ""
        out.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-chno="{num}" tvg-name="{name}"{logo} group-title="{group}",{name}')
        out.append(row["URL"])
    return ("\n".join(out) + "\n").encode("utf-8")


def system_status():
    load1, load5, load15 = os.getloadavg()
    prefetch_status = PREFETCH.status()
    with STATS_LOCK:
        counters = dict(STATS)
    return {
        "uptime_seconds": round(time.time() - STARTED),
        "cpu": {"count": os.cpu_count(), "load1": round(load1, 2),
                "load5": round(load5, 2), "load15": round(load15, 2)},
        "memory": _meminfo(),
        "encoders": {"in_use": POOL.count(), "max": MAX_ENCODERS,
                     "started_total": counters["encoder_starts"]},
        "viewers": {"in_use": sum(len(c.subs) for c in CHANS.values()), "max": TUNER_COUNT,
                    "served_total": counters["viewers_served"],
                    "refused_total": counters["refused"]},
        "schedule": {"db": DB_PATH,
                     "db_modified": (datetime.fromtimestamp(os.path.getmtime(DB_PATH)).isoformat()
                                     if os.path.exists(DB_PATH) else None),
                     "channels_on_air": sum(1 for r in channels_status() if r["on_air"])},
        "guide": GUIDE.stats,
        "prefetch": prefetch_status,
        "joins": joins_report(),
    }


def _grab_frame(station):
    """One still from whatever the schedule is playing right now. ffmpeg
    decodes a single frame after a seek, so this costs a fraction of a
    core for a fraction of a second -- nothing like a live encode."""
    gconf = guide_conf(station)
    if gconf is not None:                            # the guide channel: draw one picture of it
        try:
            from io import BytesIO
            from PIL import Image
            opts = guide_channel.options(gconf)
            g = guide_channel.Guide(opts, (640, 360), rows_fn=lambda a, b: guide_channel.rows_from_xmltv(
                GUIDE.xmltv(), a, b, skip_names=tuple(GUIDE_STATIONS())))
            out = BytesIO()
            Image.frombytes("RGB", (640, 360), g.frame(0)).save(out, "JPEG", quality=70)
            return out.getvalue()
        except Exception:
            return None
    try:
        cur = resolver.resolve(DB_PATH, station)
    except (ScheduleGap, sqlite3.Error):
        return None
    if cur.entry.is_stream:
        return None
    path = media_path(cur.entry.path)
    if not os.path.exists(path):
        return None
    best = None
    for ahead in THUMB_LOOKAHEAD:       # a programme's first seconds are often a black fade: look a little further in
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{cur.seek + ahead:.3f}", "-i", path,
               "-frames:v", "1", "-vf", "scale=320:-2",
               "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=15, check=True).stdout
        except (subprocess.SubprocessError, OSError):
            break
        if out and (best is None or len(out) > len(best)):
            best = out
        if best and len(best) >= THUMB_NOT_BLACK:
            break
    return best or None


THUMB_LOOKAHEAD = (0, 4, 10, 20)      # seconds past "now" to try when the frame is black
THUMB_NOT_BLACK = 3000                # a 320px JPEG smaller than this is (nearly) one flat colour


def _frame_from_prime(chan):
    """One still of a converted external channel, from the few seconds of its own output the channel already keeps for a viewer
    who joins. Nothing more is asked of the source: a second connection to a tuner would take another of its tuners."""
    data = b"".join(list(chan.prime))
    if len(data) < 188 * 20:
        return None
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0", "-frames:v", "1",
           "-vf", "scale=320:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"]
    try:
        out = subprocess.run(cmd, input=data, capture_output=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return out or None


def thumbnail(num):
    """JPEG snapshot for a channel, or None if there isn't one to show (off
    air, a live stream, or a missing file). Cached per channel for THUMB_TTL
    so a page full of tiles doesn't turn into a page full of ffmpegs."""
    chan = CHANS.get(num)
    if chan is None:
        return None
    guide = guide_conf(chan.station) is not None
    if not guide and chan.thread is None:            # its encoder is not running: no video is opened just to paint a dashboard tile
        return None
    ext = EXTERNAL.get(chan.station)
    if ext is not None and ext.get("mode") == "relay":
        return None                                  # passed on untouched: nothing here looks at it
    now = time.time()
    with THUMBS_LOCK:
        hit = THUMBS.get(num)
        if hit and now - hit[0] < (THUMB_TTL_GUIDE if guide else THUMB_TTL):
            return hit[1]
    # the picture is taken from the few seconds of output the channel already keeps (no file is read for it)
    body = _grab_frame(chan.station) if guide else _frame_from_prime(chan)
    if body is not None:                              # no frame yet is not remembered: the next ask may have one
        with THUMBS_LOCK:
            THUMBS[num] = (now, body)
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ---- the login
    def _session_ok(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        morsel = jar.get(SESSION_COOKIE)
        return bool(morsel) and AUTH.check_session(morsel.value)

    def _cookie(self, value, max_age):
        flags = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if self.headers.get("X-Forwarded-Proto", "") == "https":
            flags += "; Secure"
        return ("Set-Cookie", flags)

    def _gate(self, path, query=""):
        """True when the request was refused because it needs a login the caller does not have."""
        if not AUTH.enabled:
            return False
        api = path.startswith("/api/") and not path.startswith("/api/auth/")
        if not (api or path in PROTECTED_PAGES) or self._session_ok():
            return False
        if self.command == "POST":                    # do not leave an unread body on a kept-alive connection
            try:
                self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 1_000_000))
            except (ValueError, OSError):
                pass
        if api:
            self._json({"error": "log in first", "login": "/login.html", "setup": not AUTH.has_password()}, 401)
        else:
            self.send_response(302)
            where = "/login.html?next=" + quote(path + (f"?{query}" if query else ""), safe="")
            where = where.replace("\r", "").replace("\n", "")              # quote() leaves none; a header is never built from a line break
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()
        return True

    def _auth_api(self, path, body=None):
        action = path[len("/api/auth/"):]
        who = self.client_address[0]
        if body is None:
            if action == "status":
                return self._json({"login": AUTH.enabled, "has_password": AUTH.has_password(),
                                   "setup_needed": AUTH.enabled and not AUTH.has_password(),
                                   "authenticated": (not AUTH.enabled) or self._session_ok()})
            return self._json({"error": "no such call"}, 404)
        if not AUTH.enabled:
            return self._json({"error": "the login is switched off on this server (DASHBOARD_LOGIN=0)"}, 400)
        try:
            if action == "setup":
                if AUTH.has_password():
                    return self._json({"error": "a password is already set: log in instead"}, 409)
                AUTH.set_password(body.get("password"))
                return self._json({"ok": True}, headers=[self._cookie(AUTH.make_session(), authmod.SESSION_SECONDS)])
            if action == "login":
                if not AUTH.has_password():
                    return self._json({"error": "no password is set yet", "setup": True}, 409)
                wait = AUTH.blocked_for(who)
                if wait:
                    return self._json({"error": f"too many wrong passwords: wait {wait} seconds and try again"}, 429,
                                      headers=[("Retry-After", str(wait))])
                token = AUTH.login(body.get("password"), who)
                return self._json({"ok": True}, headers=[self._cookie(token, authmod.SESSION_SECONDS)])
            if action == "logout":
                jar = http.cookies.SimpleCookie()
                try:
                    jar.load(self.headers.get("Cookie", ""))
                except http.cookies.CookieError:
                    pass
                if jar.get(SESSION_COOKIE):
                    AUTH.revoke(jar[SESSION_COOKIE].value)      # dead everywhere, not only cleared in this browser
                return self._json({"ok": True}, headers=[self._cookie("", 0)])
            if action == "password":
                if not self._session_ok():
                    return self._json({"error": "log in first", "login": "/login.html"}, 401)
                if not AUTH.verify(body.get("current")):
                    return self._json({"error": "the current password is not right"}, 403)
                AUTH.set_password(body.get("new"))          # a new signing key: every other session is signed out
                return self._json({"ok": True}, headers=[self._cookie(AUTH.make_session(), authmod.SESSION_SECONDS)])
            return self._json({"error": "no such call"}, 404)
        except authmod.AuthError as e:
            return self._json({"error": str(e)}, 401 if action == "login" else 400)
        except OSError as e:
            return self._json({"error": f"could not save the password: {e.strerror}"}, 500)

    def _bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _files_api(self, path, query, body=None):
        """/api/files/*: the file manager. Errors the user can act on come back as {"error": ...}."""
        action = path[len("/api/files/"):]
        try:
            if body is None:                                     # GET
                if action == "info":
                    return self._json({"edit": EDIT_FILES, **FILES.info()})
                if action == "list":
                    return self._json(FILES.list(query.get("root", [""])[0], query.get("path", [""])[0]))
                if action == "add-status":
                    return self._json(FILES.add_status(query.get("id", [""])[0]))
                if action == "shows":
                    return self._json({"shows": FILES.shows(), "kinds": FILES.show_kinds(), "members": FILES.show_members(),
                                      "linking": filemgr.linking()})
                if action == "sources-in-use":
                    # every source made for a station (a series, a season, a video, a playlist, or several together) and the
                    # stations that name it, so one nobody uses any more can be taken away
                    try:
                        confs = CONSOLE.stations()
                    except Exception:
                        confs = {}
                    texts = {n: json.dumps(c, sort_keys=True) + json.dumps(schedules.get(n) or {}, sort_keys=True) for n, c in confs.items()}
                    members = FILES.show_members()
                    rows = []
                    for tag in FILES.shows():
                        used = sorted(n for n, t in texts.items() if json.dumps(tag) in t)
                        rows.append({"tag": tag, "kind": FILES.show_kinds().get(tag), "members": members.get(tag, []), "used_by": used})
                    # lists with nothing in them that no station names (a template makes one for each part of the day)
                    sourced = set(FILES.shows())
                    idle = []
                    for e in FILES.list("tags", "")["entries"]:
                        if e["kind"] != "dir" or e["name"] in sourced or e["name"] in ("bump", "commercials"):
                            continue
                        if any(json.dumps(e["name"]) in t for t in texts.values()):
                            continue
                        if not stationsapi._has_video(os.path.join(FILES.tags_root, e["name"])):
                            idle.append(e["name"])
                    return self._json({"sources": rows, "known_stations": len(confs), "empty_unused": idle[:100]})
                if action == "media-path":
                    return self._json({"path": FILES.media_path_of_list_item(query.get("list_path", [""])[0])})
                if action == "follows":
                    return self._json({"follows": FILES.follows(query.get("tag", [None])[0])})
                raise filemgr.FileError("no such call", 404)
            if not EDIT_FILES:
                raise filemgr.FileError("changing files is switched off (DASHBOARD_EDIT=0)", 403)
            if action == "create-tag":
                return self._json(FILES.create_tag(body.get("name")))
            if action == "rename-tag":
                out = FILES.rename_tag(body.get("tag"), body.get("to"))
                library.refresh_tag(LIBRARY_ROOT, body.get("tag"))          # gone under the old name ...
                library.refresh_tag(LIBRARY_ROOT, out.get("tag", body.get("to")))      # ... and counted under the new
                return self._json(out)
            if action == "delete-tag":
                out = FILES.delete_tag(body.get("tag"))
                library.refresh_tag(LIBRARY_ROOT, body.get("tag"))
                return self._json(out)
            if action == "add":
                if body.get("background") is True:       # a big pick: answer at once, the page watches add-status
                    return self._json(FILES.add_in_background(body.get("tag"), body.get("sources"), follow=body.get("follow") is True,
                                                              done=lambda tag: library.refresh_tag(LIBRARY_ROOT, tag)))
                out = FILES.add(body.get("tag"), body.get("sources"), follow=body.get("follow") is True)
                library.refresh_tag(LIBRARY_ROOT, out["tag"])               # just this list is counted again
                return self._json(out)
            if action == "add-show":
                src = body.get("path")
                if body.get("list_path"):                   # a video or folder inside a list: its place in the media is what is used
                    src = FILES.media_path_of_list_item(body.get("list_path"))
                if body.get("members") is not None:         # several sources as one (a mid card or a commercial takes one list)
                    out = FILES.add_set(body.get("members"), wait=False, done=lambda tag: library.refresh_tag(LIBRARY_ROOT, tag))
                else:
                    out = FILES.add_show(src, wait=False, done=lambda tag: library.refresh_tag(LIBRARY_ROOT, tag),
                                         types=body.get("types"))
                return self._json(out)
            if action == "remove-show":
                out = FILES.remove_show(body.get("tag"))
                library.refresh_tag(LIBRARY_ROOT, out["tag"])
                return self._json(out)
            if action == "unfollow":
                return self._json(FILES.unfollow(body.get("tag"), body.get("root"), body.get("path")))
            if action == "follows/refresh":
                return self._json(FILES.refresh_follows())
            if action == "remove":
                out = FILES.remove(body.get("tag"), body.get("paths"))
                library.refresh_tag(LIBRARY_ROOT, out["tag"])
                return self._json(out)
            raise filemgr.FileError("no such call", 404)
        except filemgr.FileError as e:
            return self._json({"error": str(e)}, e.code)
        except OSError as e:
            print(f"! files {action}: {e}")
            return self._json({"error": f"that did not work: {e.strerror or e}"}, 500)

    def _ta_api(self, path, body=None):
        """/api/ta/*: the TubeArchivist conversion's settings and status, shown on the Files page."""
        action = path[len("/api/ta/"):]
        media = CONF.get("media_root", "/mnt/media")
        try:
            if body is None:
                if action == "status":
                    return self._json(tubearchivist.status(media, os.path.join(CATALOG_ROOT, "catalog")))
                if action == "detect":
                    return self._json({"libraries": tubearchivist.detect(media)})
                raise filemgr.FileError("no such call", 404)
            if not EDIT_FILES:
                raise filemgr.FileError("changing settings is switched off (DASHBOARD_EDIT=0)", 403)
            if action == "settings":
                return self._json(tubearchivist.save(media, body, os.path.join(CATALOG_ROOT, "catalog")))
            if action == "run":
                return self._json(tubearchivist.run_now())
            raise filemgr.FileError("no such call", 404)
        except filemgr.FileError as e:
            return self._json({"error": str(e)}, e.code)

    def _base(self):
        """The address clients are told to use for this tuner's streams and lineup. It is `advertise_url`, unless that was
        never set (it is still localhost, which no other machine can reach): then it is the address the client itself used
        to reach us, taken from its Host header, so Jellyfin or anything else on the network just works."""
        from urllib.parse import urlparse as _up
        host = self.headers.get("Host", "") if hasattr(self, "headers") else ""
        if host and re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9a-fA-F:]+\](:\d{1,5})?", host) \
                and (_up(ADVERTISE).hostname or "") in ("localhost", "127.0.0.1", "::1"):
            return f"http://{host}"
        return ADVERTISE

    def _iptv_api(self, path, query=None, body=None):
        """/api/iptv/*: the Sources page (external IPTV sources: other tuners, playlists, cameras)."""
        action = path[len("/api/iptv/"):]
        try:
            if body is None:                                         # GET
                if action == "sources":
                    watching = {e["number"]: len(CHANS[e["number"]].subs) for e in EXTERNAL.values() if e["number"] in CHANS}
                    return self._json({"sources": SOURCES.describe(), "edit": EDIT_FILES, "clashes": EXTERNAL_CLASHES,
                                       "on_air": len(EXTERNAL), "watching": watching,
                                       "legacy": {"url": PT_URL, "mode": PT_MODE} if UPSTREAM is not None else None})
                raise iptv.SourceError("no such call")
            if not EDIT_FILES:
                return self._json({"error": "changing sources is switched off (DASHBOARD_EDIT=0)"}, 403)
            if action == "save":
                fields = {k: body[k] for k in ("name", "type", "url", "mode", "enabled", "base") if k in body}
                existing = body.get("id")
                src = SOURCES.save(fields, existing if isinstance(existing, str) and existing else None)
                if not existing and src["type"] != "stream":
                    refresh_source(src["id"])                        # a new tuner or playlist: read its channels now
                elif not existing:
                    SOURCES.set_channels(src["id"], iptv.fetch_channels(src), None)
                    sync_external()
                else:
                    if "url" in fields and src["type"] != "stream":
                        refresh_source(src["id"])
                    elif "url" in fields:
                        SOURCES.set_channels(src["id"], iptv.fetch_channels(src), None)
                    sync_external()
                return self._json({"id": src["id"]})
            if action == "import-legacy":
                return self._json({"id": migrate_legacy(), "restart": True})
            if action == "delete":
                SOURCES.delete(body.get("id"))
                sync_external()
                return self._json({"ok": True})
            if action == "refresh":
                refresh_source(body.get("id"))
                return self._json({"ok": True})
            if action == "channel":
                ov = SOURCES.override(body.get("id"), body.get("key"), {k: body[k] for k in ("enabled", "name", "number") if k in body})
                sync_external()
                return self._json({"override": ov})
            if action == "test":
                return self._json(iptv.probe(body.get("url")))
            raise iptv.SourceError("no such call")
        except iptv.SourceError as e:
            return self._json({"error": str(e)}, 400)

    def _stations_api(self, path, query=None, body=None):
        """/api/stations/* and /api/schedule: the Stations and Schedule pages."""
        action = path[len("/api/"):]
        q = lambda key: (query or {}).get(key, [""])[0]
        try:
            if body is None:                                         # GET
                if action == "stations":
                    return self._json({"stations": STATIONS.listing(), "edit": EDIT_FILES})
                if action == "stations/get":
                    return self._json(STATIONS.get(q("name")))
                if action == "stations/show-episodes":
                    return self._json(STATIONS.show_episodes(q("list")))
                if action == "stations/jobs":
                    return self._json({"jobs": STATIONS.jobs.list()})
                if action == "stations/job":
                    job = STATIONS.jobs.get(q("id"))
                    return self._json(job) if job else self._json({"error": "no such job"}, 404)
                if action == "schedule":
                    day = None
                    if q("date"):
                        try:
                            day = datetime.strptime(q("date"), "%Y-%m-%d").date()
                        except ValueError:
                            raise stationform.FormError("that date is not valid")
                    return self._json(STATIONS.schedule(q("name"), day))
                if action == "schedule/block":
                    try:
                        start = datetime.fromisoformat(q("start"))
                        offset, limit = int(q("offset") or 0), int(q("limit") or 200)
                    except ValueError:
                        raise stationform.FormError("that is not a valid part of the schedule")
                    return self._json(STATIONS.block(q("name"), start, offset, limit))
                raise filemgr.FileError("no such call", 404)
            if not EDIT_FILES:
                raise filemgr.FileError("changing stations is switched off (DASHBOARD_EDIT=0)", 403)
            if action == "stations/premiere-preview":
                return self._json({"shows": STATIONS.premiere_preview(body.get("schedule"))})
            if action == "stations/schedule-preview":
                return self._json(STATIONS.preview(body.get("schedule")))
            if action == "stations/quickstart":
                return self._json(STATIONS.quickstart(FILES, taken_numbers=list(CHANNELS),
                                                      done=lambda tag: library.refresh_tag(LIBRARY_ROOT, tag)))
            if action == "stations/save":
                return self._json(STATIONS.save(body.get("original"), body.get("form")))
            if action == "stations/rename":
                return self._json(STATIONS.rename(body.get("name"), body.get("to")))
            if action == "stations/guide-apply":
                return self._json({"restarted": apply_guide_now()})
            if action == "stations/delete":
                return self._json(STATIONS.delete(body.get("name")))
            if action == "stations/build":
                days = body.get("days", 7)
                if isinstance(days, bool) or not isinstance(days, int):
                    raise stationform.FormError("days must be a whole number")
                return self._json(STATIONS.build(body.get("name"), days, bool(body.get("rebuild", True)),
                                                 refresh=body.get("refresh") is True,
                                                 resume=body.get("resume") is True))
            raise filemgr.FileError("no such call", 404)
        except stationform.FormError as e:
            return self._json({"error": str(e)}, 400)
        except stationsapi.NotFound as e:
            return self._json({"error": str(e)}, 404)
        except filemgr.FileError as e:
            return self._json({"error": str(e)}, e.code)
        except console_client.ConsoleError as e:
            return self._json({"error": str(e)}, 502)

    def _json_body(self):
        """The JSON object posted to /api/*, or None after answering with the error. JSON only: a web page
        on another site can send a form post but not this (the browser asks first)."""
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._json({"error": "send JSON"}, 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= 1_000_000:
            self._json({"error": "that request is too big"}, 413)
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json({"error": "that is not valid JSON"}, 400)
            return None
        return body

    def do_POST(self):
        p = urlparse(self.path).path
        if self._gate(p):
            return
        if p.startswith("/api/auth/"):
            body = self._json_body()
            return None if body is None else self._auth_api(p, body)
        if p.startswith("/api/stations/"):
            body = self._json_body()
            return None if body is None else self._stations_api(p, None, body)
        if p.startswith("/api/iptv/"):
            body = self._json_body()
            return None if body is None else self._iptv_api(p, None, body)
        if p == "/api/system/speedtest":
            body = self._json_body()
            if body is None:
                return None
            if not EDIT_FILES:
                return self._json({"error": "this is switched off (DASHBOARD_EDIT=0)"}, 403)
            return self._json(diag.speed_test(CONF.get("media_root", "/mnt/media")))
        if p == "/api/loudness/settings":
            body = self._json_body()
            if body is None:
                return None
            if not EDIT_FILES:
                return self._json({"error": "changing settings is switched off (DASHBOARD_EDIT=0)"}, 403)
            try:
                got = settings.set_loudness(body)
            except settings.SettingsError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"could not save the settings: {e.strerror}"}, 500)
            return self._json({"settings": got, "status": LEVELS.status()})
        if p == "/api/playback/settings":
            body = self._json_body()
            if body is None:
                return None
            if not EDIT_FILES:
                return self._json({"error": "changing settings is switched off (DASHBOARD_EDIT=0)"}, 403)
            try:
                got = settings.set_playback(body)
            except settings.SettingsError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"could not save the settings: {e.strerror}"}, 500)
            return self._json({"settings": got, "env": bool(_CONT_ENV)})
        if p == "/api/captions/settings":
            body = self._json_body()
            if body is None:
                return None
            if not EDIT_FILES:
                return self._json({"error": "changing settings is switched off (DASHBOARD_EDIT=0)"}, 403)
            try:
                got = settings.set_captions(body)
            except settings.SettingsError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"could not save the settings: {e.strerror}"}, 500)
            return self._json({"settings": got})
        if p == "/api/titles/settings":
            body = self._json_body()
            if body is None:
                return None
            if not EDIT_FILES:
                return self._json({"error": "changing settings is switched off (DASHBOARD_EDIT=0)"}, 403)
            try:
                got = settings.set_titles({k: body[k] for k in ("episode_names",) if k in body})
            except settings.SettingsError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"could not save the settings: {e.strerror}"}, 500)
            titles.configure(got["episode_names"])
            if GUIDE is not None:                                   # the guide is made again with the new names
                threading.Thread(target=lambda: GUIDE.xmltv(force=True), daemon=True, name="guide-after-titles").start()
            return self._json({"settings": got})
        if p == "/api/keeper/settings":
            body = self._json_body()
            if body is None:
                return None
            if not EDIT_FILES:
                return self._json({"error": "changing settings is switched off (DASHBOARD_EDIT=0)"}, 403)
            try:
                return self._json({"settings": settings.set_keeper({k: body[k] for k in ("enabled", "days_ahead") if k in body})})
            except settings.SettingsError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"could not save the settings: {e.strerror}"}, 500)
        if p.startswith("/api/ta/"):
            body = self._json_body()
            return None if body is None else self._ta_api(p, body)
        if p.startswith("/api/files/"):
            body = self._json_body()
            return None if body is None else self._files_api(p, {}, body)
        if urlparse(self.path).path == "/lineup.post":
            if UPSTREAM is not None:
                try:
                    UPSTREAM.lineup(force=True)
                except UpstreamError as e:
                    print(f"! rescan: {e}")
            return self._json({"ScanInProgress": 0})
        self.send_error(404)

    def do_HEAD(self):
        """Some clients ask with HEAD before they fetch (a guide address, a stream address): answer with the headers a GET
        would give and no body. A stream is not started for it."""
        p = urlparse(self.path).path
        if p.startswith("/auto/"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        real = self.wfile

        class HeadersOnly:                     # the first write is the headers; the body is dropped
            sent = False

            def write(self, data):
                if not self.sent:
                    self.sent = True
                    return real.write(data)
                return len(data)

            def flush(self):
                real.flush()
        self.wfile = HeadersOnly()
        try:
            self.do_GET()
        finally:
            self.wfile = real

    def do_GET(self):
        p = urlparse(self.path).path
        if self._gate(p, urlparse(self.path).query):
            return
        if p == "/api/auth/status":
            return self._auth_api(p)

        if p == "/discover.json":
            # TunerCount covers both pools: FS42 viewer slots plus however many
            # physical tuners the real device has, since one lineup fronts both.
            total = TUNER_COUNT + (PT_BUDGET.n if PT_BUDGET else 0)
            return self._json({
                "FriendlyName": CONF.get("friendly_name", "Station Wrapper 43"),
                "Manufacturer": "Station Wrapper 43",
                # MUST NOT contain "hdtc". Jellyfin infers hardware transcoding
                # purely from the model string (DiscoverResponse.SupportsTranscoding:
                # model.Contains("hdtc", OrdinalIgnoreCase)). Claim an HDTC and it
                # offers heavy/internet540/480/360/240/mobile profiles and requests
                # them as "?transcode=<profile>" -- which this service does not
                # implement, so every profile would silently serve full-rate video
                # under whatever label the viewer picked. HDHR4 reports false, so
                # there is one "native" source and no phantom quality menu.
                "ModelNumber": "HDHR4-2US",
                "FirmwareName": "hdhomerun4_atsc",
                "FirmwareVersion": "20200101",
                "DeviceID": DEVICE_ID,
                "DeviceAuth": "fs42",
                "BaseURL": self._base(),
                "LineupURL": f"{self._base()}/lineup.json",
                "TunerCount": total,
            })

        if p == "/lineup_status.json":
            return self._json({"ScanInProgress": 0, "ScanPossible": 0,
                               "Source": "Cable", "SourceList": ["Cable"]})

        if p == "/lineup.json":
            return self._json(merged_lineup(self._base()))

        if p == "/device.xml":
            xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>{self._base()}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{escape(CONF.get("friendly_name", "Station Wrapper 43"))}</friendlyName>
    <manufacturer>fs42-tuner</manufacturer>
    <modelName>HDHR4-2US</modelName>
    <modelNumber>HDHR4-2US</modelNumber>
    <serialNumber></serialNumber>
    <UDN>uuid:{DEVICE_ID}</UDN>
  </device>
</root>"""
            return self._bytes(xml.encode(), "application/xml")

        if p == "/xmltv.xml":
            force = "refresh" in (urlparse(self.path).query or "")
            body = GUIDE.xmltv(force=force)
            if b"@@BASE@@" in body:                       # pictures are addressed from where this client reached us
                body = body.replace(b"@@BASE@@", self._base().encode())
            return self._bytes(body, "application/xml")

        if p.startswith("/logo/") and p.endswith(".png"):        # a channel's logo, for the guide: its own picture, else one drawn from its name
            num = p[len("/logo/"):-len(".png")]
            station = CHANNELS.get(num)
            if station is None or station in EXTERNAL:
                return self.send_error(404)
            data, ctype = None, "image/png"
            try:
                rel = (CONSOLE.stations().get(station) or {}).get("wrapper_logo")
                got = channel_logo.custom(CONF.get("media_root", "/mnt/media"), rel) if rel else None
                if got:
                    data, ctype = got
            except Exception:
                data = None
            if data is None:
                data = channel_logo.render(display_name(station), num)
            if data is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if p.startswith("/art/"):                          # a video's picture, for the guide: only the picture beside a scheduled video
            from urllib.parse import unquote
            rel = unquote(p[len("/art/"):])
            real_root = os.path.realpath(CATALOG_ROOT)
            if os.path.isabs(rel) or ".." in rel.split("/") or "\0" in rel:
                return self.send_error(404)
            full = os.path.normpath(os.path.join(real_root, rel))
            if not full.startswith(real_root.rstrip(os.sep) + os.sep):
                return self.send_error(404)
            folder = os.path.realpath(os.path.dirname(full))             # the file may be a link out to the media: its folder must not leave
            if not (folder + os.sep).startswith(real_root.rstrip(os.sep) + os.sep):
                return self.send_error(404)
            got = art.get(full)
            if not got or os.path.getsize(got[0]) > 4 * 1024 * 1024:
                return self.send_error(404)
            with open(got[0], "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", got[1])
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/guide_status.json":
            return self._json(GUIDE.stats)

        if p == "/status.json":
            st = {
                "encoders": {"in_use": POOL.count(), "max": MAX_ENCODERS},
                "viewers": {"max": TUNER_COUNT},
                "channels": [
                    {"channel": n, "station": c.station, "viewers": len(c.subs),
                     "encoding": c.thread is not None, "now_playing": c.now_playing}
                    for n, c in sorted(CHANS.items(), key=lambda kv: _sortkey(kv[0]))
                    if c.subs or c.thread
                ],
            }
            st["guide"] = GUIDE.stats
            if PT_BUDGET:
                st["passthrough"] = {"device": UPSTREAM.url, "mode": PT_MODE,
                                     "tuners_in_use": PT_BUDGET.active,
                                     "tuners": PT_BUDGET.n}
            return self._json(st)

        if p.startswith("/auto/v"):
            return self._stream(p[len("/auto/v"):])

        if p.startswith("/captions/"):
            return self._captions(p[len("/captions/"):])

        if p == "/channels.json":
            return self._json(channels_status())

        if p == "/system.json":
            return self._json(system_status())

        if p == "/api/system/status":
            return self._json(status_report())

        if p == "/api/system/diagnostics":
            data = diag.as_text(status_report(recent=300)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="station-wrapper-43-diagnostics.txt"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/lineup.m3u":
            return self._bytes(lineup_m3u(self._base()), "audio/x-mpegurl; charset=utf-8")

        if p.startswith("/api/files/"):
            from urllib.parse import parse_qs
            return self._files_api(p, parse_qs(urlparse(self.path).query))

        if p.startswith("/api/ta/"):
            return self._ta_api(p)

        if p == "/api/keeper":
            return self._json({"settings": settings.get_keeper(), "status": KEEPER.snapshot()})

        if p == "/api/titles":
            return self._json({"settings": settings.get_titles(), "edit": EDIT_FILES})
        if p == "/api/loudness":
            return self._json({"settings": settings.get_loudness(), "status": LEVELS.status(), "edit": EDIT_FILES})
        if p == "/api/captions":
            return self._json({"settings": settings.get_captions(), "edit": EDIT_FILES})
        if p == "/api/playback":
            return self._json({"settings": settings.get_playback(), "env": bool(_CONT_ENV), "on": continuous_ts(), "edit": EDIT_FILES})

        if p.startswith("/api/iptv/"):
            return self._iptv_api(p)

        if p == "/api/stations" or p.startswith("/api/stations/") or p in ("/api/schedule", "/api/schedule/block"):
            from urllib.parse import parse_qs
            return self._stations_api(p, parse_qs(urlparse(self.path).query))

        if p == "/library.json":
            if "refresh" in urlparse(self.path).query:        # the Rescan button: asked for, so it is done
                library.request_scan(LIBRARY_ROOT)
            return self._json(library.cached(LIBRARY_ROOT))      # a page view never starts a scan: it shows the last one

        if p.startswith("/thumb/") and p.endswith(".jpg"):
            body = thumbnail(p[len("/thumb/"):-len(".jpg")])
            if body is None:
                return self.send_error(404)
            return self._bytes(body, "image/jpeg")

        if self._static(p):
            return

        self.send_error(404)

    def _static(self, path):
        """Serve a file under STATIC_DIR if one matches; "/" means index.html.
        Returns False (never a 404 itself) for anything that doesn't resolve
        to a real file under STATIC_DIR -- including a path walked outside it
        with "../" -- so the caller's own 404 fires for those.
        """
        rel = "index.html" if path == "/" else path.lstrip("/")
        root = os.path.normpath(STATIC_DIR)
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(root.rstrip(os.sep) + os.sep):          # walked outside the folder with ../
            return False
        if not os.path.isfile(full):
            return False
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            self._bytes(f.read(), ctype)
        return True

    def _captions(self, rest):
        """Captions for what a channel is airing right now, for the web player. Only that one file can be asked for:
        the page names a channel and a track, never a path.
          /captions/<channel>.json                  where the file is now, and which caption tracks it has
          /captions/<channel>/<file id>/<track>.vtt  one track as WebVTT"""
        parts = rest.split("/")
        if len(parts) not in (1, 3) or ".." in parts:
            return self.send_error(404)
        num = parts[0][:-5] if len(parts) == 1 and parts[0].endswith(".json") else parts[0]
        chan = CHANS.get(num)
        if chan is None:
            return self.send_error(404)
        try:
            cur = resolver.resolve(DB_PATH, chan.station)
        except (ScheduleGap, sqlite3.Error):
            return self._json({"now": time.time(), "eid": None, "tracks": []})
        entry = cur.entry
        path = None if entry.is_stream else media_path(entry.path)
        if path is None or not os.path.exists(path):
            return self._json({"now": time.time(), "eid": None, "tracks": []})
        if len(parts) == 1:
            burned = bool(burn_language(chan.station, cur.seek))      # already in the picture: a second copy in the player would double them
            found = [] if burned or entry.content_type in ("bump", "commercial") else captions.tracks(path)
            return self._json({"now": time.time(), "eid": captions.eid(path), "pos": round(cur.seek, 2),
                               "remaining": round(cur.remaining, 1), "kind": entry.content_type, "tracks": captions.public(found),
                               "burned": burned})
        if len(parts) != 3 or not parts[2].endswith(".vtt") or parts[1] != captions.eid(path):
            return self.send_error(404, "that file is no longer on air")
        try:
            text = captions.vtt(path, parts[2][:-4])
        except (OSError, RuntimeError) as e:
            return self._json({"error": str(e)}, 502)
        if text is None:
            return self.send_error(404)
        self._bytes(text.encode("utf-8"), "text/vtt; charset=utf-8")

    def _stream(self, num):
        if num in CHANS:
            return self._stream_fs42(CHANS[num])
        up = upstream_map().get(num)
        if up is not None:
            return self._stream_passthrough(num, up)
        self.send_error(404)

    def _stream_fs42(self, chan):
        if not VIEWERS.acquire(blocking=False):
            print(f"[ch{chan.number}] refused: {TUNER_COUNT} viewer slots all taken")
            _count("refused")
            return self.send_error(503, "All tuners in use")
        sub = None
        try:
            sub = chan.attach()
            if sub is None:
                print(f"[ch{chan.number}] refused: {MAX_ENCODERS} encoders busy elsewhere")
                _count("refused")
                return self.send_error(503, "All tuners in use")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            _count("viewers_served")
            sub.pump(self.wfile.write)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if sub is not None:
                chan.detach(sub)
            VIEWERS.release()
        self.close_connection = True

    def _stream_passthrough(self, num, up):
        """Relay the real tuner's bytes untouched -- no decode, no re-encode.

        Unlike an FS42 channel, two viewers on one OTA channel really do occupy
        two physical tuners, so these are counted one-for-one against the
        device's own TunerCount rather than being fanned out.
        """
        if PT_BUDGET is not None and not PT_BUDGET.acquire():
            print(f"[ch{num}] refused: real tuner has all {PT_BUDGET.n} tuners busy")
            return self.send_error(503, "All tuners in use")
        try:
            print(f"[ch{num}] passthrough -> {up['URL']}")
            with webget.open_url(up["URL"], timeout=15) as r:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            # The local budget is a ceiling, not the truth. Anything else on
            # the network -- NextPVR, Channels DVR, another Jellyfin, the
            # HDHomeRun app on a phone -- takes physical tuners without this
            # process ever seeing it, so the device is the only real authority
            # on availability. When it turns us away, say "tuners busy" rather
            # than reporting a generic upstream fault, because that is almost
            # always what it means and it is what a client knows how to
            # handle.
            busy = isinstance(e, urllib.error.HTTPError) and e.code in (403, 503)
            print(f"[ch{num}] passthrough failed ({'device busy' if busy else e})")
            try:
                if busy:
                    self.send_error(503, "All tuners in use")
                else:
                    self.send_error(502, "Upstream tuner error")
            except Exception:
                pass
        finally:
            if PT_BUDGET is not None:
                PT_BUDGET.release()
            print(f"[ch{num}] passthrough closed")
        self.close_connection = True


def check_duplicates():
    """One lineup means one namespace, so a duplicate number here is fatal.

    Jellyfin keys an HDHomeRun channel as "hdhr_" + GuideNumber and drops any
    id it has already seen, without logging anything a user would notice.
    Better to refuse at startup than to have someone hunt a missing channel.
    """
    ours = set(CHANNELS)
    theirs = set(upstream_map())
    clash = sorted(ours & theirs, key=_sortkey)
    if clash:
        raise SystemExit(
            "\nREFUSING TO START -- duplicate channel numbers.\n"
            f"  FieldStation42 and {UPSTREAM.url} both claim: {clash}\n\n"
            "Jellyfin keys HDHomeRun channels by number alone, so one of each\n"
            "pair would silently vanish. Renumber the FieldStation42 channels\n"
            "in config.json (800-899 is usually clear of both OTA virtual\n"
            "channels and cable lineups).\n")
    return theirs


SLOW_REQUEST_SECONDS = float(os.environ.get("SLOW_REQUEST_SECONDS", "3"))


def _timed(fn):
    """Say so in the log when a page request is slow, with its address: the first thing to look at when a page
    feels laggy (`docker logs <container> | grep '\\[slow\\]'`). Streams are long by nature and left out."""
    def wrapper(self, *a, **k):
        began = time.time()
        try:
            return fn(self, *a, **k)
        finally:
            took = time.time() - began
            if took > SLOW_REQUEST_SECONDS and not self.path.startswith("/auto/"):
                print(f"[slow] {self.command} {self.path.split('?')[0]} took {took:.1f}s", flush=True)
    return wrapper


Handler.do_GET = _timed(Handler.do_GET)
Handler.do_POST = _timed(Handler.do_POST)


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH")
    if not os.path.exists(DB_PATH):
        # FieldStation42 only creates it once a station has a schedule. Starting
        # anyway lets the dashboard and Library work on a new install; channels
        # show "off air" until the schedule exists.
        print(f"  note: no schedule database yet ({DB_PATH}). Build a station and add a day in the "
              f"FieldStation42 console; channels are off air until then.")

    port = int(CONF.get("port", 5004))
    print(f"Station Wrapper 43 tuner on :{port}")
    print(f"  advertising {ADVERTISE}")

    if PT_DIRECT_ASKED:
        print("  ! passthrough mode 'direct' is retired: the device's channels now always play through this container")
    if UPSTREAM is not None and not os.environ.get("FS42_KEEP_PASSTHROUGH"):   # the env var is for the tests of the old path
        try:
            migrate_legacy()
            print("  passthrough: moved onto the Sources page (the setting is gone from the config file)")
        except Exception as e:                           # leave it as it was and keep working
            print(f"  ! could not move the passthrough device onto the Sources page: {e}")
    upstream_numbers = set()
    if UPSTREAM is not None:
        try:
            d = UPSTREAM.discover()
            PT_BUDGET = TunerBudget(UPSTREAM.tuner_count)
            upstream_numbers = check_duplicates()
            print(f"  passthrough: {d.get('FriendlyName','?')} "
                  f"({d.get('ModelNumber','?')}) at {UPSTREAM.url}")
            print(f"    {len(upstream_numbers)} channels, {PT_BUDGET.n} physical tuners, "
                  f"mode={PT_MODE}")
        except UpstreamError as e:
            print(f"  ! passthrough unavailable: {e}")
            print("    serving FieldStation42 channels only; /lineup.post retries")

    GUIDE = build_guide()
    GUIDE.static = lambda: (guide_conf(""), {**{n: guide_channel.options(c)["title"] for n, c in _GUIDE_CONFS["confs"].items()},
                                             **{s: f"Live: {e['name']}" for s, e in EXTERNAL.items()}})[1]
    GUIDE.display = display_name
    STATIONS.jobs.on_built = lambda name: threading.Thread(target=lambda: GUIDE.xmltv(force=True), daemon=True, name="guide-after-build").start()
    GUIDE.logo_url = lambda num, station: ((EXTERNAL[station].get("logo") or None) if station in EXTERNAL else f"@@BASE@@/logo/{num}.png")
    GUIDE.details = titles.details_hook(CATALOG_ROOT)
    GUIDE.art_url = lambda path: f"@@BASE@@/art/{quote(path)}"        # made when a client asks (art.py): its embedded cover, a frame, or the show's poster
    GUIDE.extra_sources = external_guide_sources
    sync_external()                                              # the channels of the external IPTV sources, from what was last read
    threading.Thread(target=refresh_sources_loop, daemon=True, name="iptv-sources").start()
    threading.Thread(target=GUIDE.xmltv, daemon=True).start()   # warm it

    print(f"  FieldStation42: {len(CHANNELS)} channels"
          + (f" ({len(DISCOVERED)} found in the station configs)" if DISCOVERED else "")
          + f" | {MAX_ENCODERS} encoders "
          f"| {TUNER_COUNT} viewer slots | favourite={FAVOR_FS42}")
    for n, s in sorted(CHANNELS.items(), key=lambda kv: _sortkey(kv[0])):
        print(f"  {n:>6}  {s}")
    threading.Thread(
        target=KEEPER.loop, daemon=True, name="schedule-keeper",
        kwargs={"interval_minutes": int(os.environ.get("KEEPER_INTERVAL_MINUTES", "30")),
                "first_delay": float(os.environ.get("KEEPER_FIRST_DELAY_SECONDS", "45"))}).start()
    # looking for TubeArchivist library folders walks the media (12 seconds over a network share): done once at start-up,
    # so opening the Files page finds the answer already waiting
    threading.Thread(target=lambda: (time.sleep(20), tubearchivist.detect(CONF.get("media_root", "/mnt/media"))),
                     daemon=True, name="ta-detect").start()
    threading.Thread(target=tubearchivist.warm, daemon=True, name="ta-warm").start()

    def warm_media():          # the Shows choice lists each media folder: looking inside them once now makes the first use quick
        time.sleep(25)
        try:
            for e in FILES.list("media", "")["entries"]:
                if e["kind"] == "dir":
                    FILES.list("media", e["name"])
        except Exception:
            pass
    threading.Thread(target=warm_media, daemon=True, name="media-warm").start()
    if os.environ.get("LOUDNESS", "1").lower() not in ("0", "false", "no", "off"):
        threading.Thread(target=LEVELS.loop, daemon=True, name="loudness").start()
    if PREFETCH.enabled:
        print(f"[prefetch] on: next-break and next-episode copies in {PREFETCH.cfg['dir']} "
              f"({'RAM' if PREFETCH.cfg['ram_only'] else 'disk'}), up to {PREFETCH.cache.max_bytes // 1048576} MB", flush=True)
        threading.Thread(target=PREFETCH.loop, daemon=True, name="prefetch").start()
        STREAMER.start()
    else:
        print(f"[prefetch] off: {PREFETCH.why_not}", flush=True)
    library.load_saved(LIBRARY_ROOT)                  # the pages show the last scan straight away, even after a restart
    threading.Thread(target=library.loop, daemon=True, name="library-scan",
                     args=(LIBRARY_ROOT, int(os.environ.get("LIBRARY_SCAN_MINUTES", "60")),
                           float(os.environ.get("LIBRARY_FIRST_DELAY_SECONDS", "60")))).start()
    if EDIT_FILES:
        threading.Thread(target=follow_loop, daemon=True, name="follow-folders",
                         args=(int(os.environ.get("FOLLOW_INTERVAL_MINUTES", "30")),
                               float(os.environ.get("FOLLOW_FIRST_DELAY_SECONDS", "120")))).start()
    # In the container the published port is reached through the container's own address, so the default is every
    # interface; FS42_BIND narrows it (127.0.0.1 for a run on the host itself).
    ThreadingHTTPServer((os.environ.get("FS42_BIND", ""), port), Handler).serve_forever()
