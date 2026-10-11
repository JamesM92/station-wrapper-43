"""External IPTV sources: other tuners, playlists of channels, and single streams such as cameras.

A source is something that already makes a live video stream. This keeps the list of them (config/tuner/sources.json),
finds each one's channels, gives every channel a number of its own that stays the same, and builds the ffmpeg command
that turns a channel into the stream the tuner serves. The tuner (tuner.py) puts the channels in its lineup, its guide,
its player and its dashboard like any other channel, marked as an external IPTV source.

Three kinds of source:
    hdhomerun   another HDHomeRun on the network: its channels come from <url>/lineup.json and keep their own numbers
    m3u         a playlist (an .m3u or .m3u8 address) with one channel per entry
    stream      one stream, such as a camera: an http, rtsp, rtmp, udp, rtp or srt address, one channel

Two ways to play a channel:
    convert     re-encode to what a web browser plays (H.264 and AAC, the same as every other channel)
    relay       copy the stream as it is, with no re-encode: cheap, but a browser may not be able to play it
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

PATH = os.environ.get("FS42_IPTV", "/config/tuner/sources.json")
TYPES = ("hdhomerun", "m3u", "stream")
MODES = ("convert", "relay", "guide")      # "guide": a tuner whose channels are NOT played here (its listings only go in the guide)
SCHEMES = {"hdhomerun": ("http", "https"), "m3u": ("http", "https"),
           "stream": ("http", "https", "rtsp", "rtsps", "rtmp", "rtmps", "udp", "rtp", "srt")}
CHANNEL_SCHEMES = SCHEMES["stream"]
MAX_SOURCES = 40
MAX_CHANNELS = 5000                     # per source
MAX_PLAYLIST = 8 * 1024 * 1024
WHITELIST = "http,https,tcp,tls,rtsp,rtp,udp,crypto,rtmp,rtmps,srt"       # what ffmpeg may open: never a local file


class SourceError(Exception):
    """Something the person can fix: the message is safe to show."""


# ---- addresses --------------------------------------------------------------------------------------------
def check_url(url, kind):
    if not isinstance(url, str) or not url.strip():
        raise SourceError("an address is needed")
    url = url.strip()
    if len(url) > 2000 or any(c.isspace() or ord(c) < 32 for c in url):
        raise SourceError("that address is not valid (spaces or control characters in it)")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in SCHEMES[kind] or not parts.hostname:
        raise SourceError(f"a {kind} source needs an address starting with " + ", ".join(s + "://" for s in SCHEMES[kind]))
    return url


def mask(url):
    """The address with any password hidden, for showing on a page (camera addresses often carry one)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parts.password:
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        user = urllib.parse.quote(urllib.parse.unquote(parts.username or ""), safe="")
        return urllib.parse.urlunsplit((parts.scheme, f"{user}:****@{host}{port}", parts.path, parts.query, parts.fragment))
    return url


# ---- finding a source's channels --------------------------------------------------------------------------
def _get(url, limit, timeout=15):
    if not str(url).lower().startswith(("http://", "https://")):
        raise SourceError("an address to read a list of channels from must be http or https")
    req = urllib.request.Request(url, headers={"User-Agent": "StationWrapper43"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - http and https only, checked above
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SourceError("that is bigger than a channel list should be")
    return data


class Channels(list):
    """A list of channels that also carries the guide addresses a playlist's header named."""
    guide_urls = ()


def playlist_guide_urls(text):
    """The XMLTV addresses in a playlist's first line (url-tvg="a,b" or x-tvg-url), http(s) only."""
    for raw in text.splitlines()[:5]:
        if raw.strip().upper().startswith("#EXTM3U"):
            found = re.search(r'(?:url-tvg|x-tvg-url)="([^"]*)"', raw, re.I)
            urls = [u.strip() for u in (found.group(1).split(",") if found else []) if u.strip().lower().startswith(("http://", "https://"))]
            return urls[:3]
    return []


def parse_m3u(text):
    """[{key, name, url, group, logo}] from an M3U/M3U8 playlist. Entries without a usable address are skipped."""
    out, seen, info = [], set(), None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            info = {"name": name or attrs.get("tvg-name") or "", "group": attrs.get("group-title", ""),
                    "logo": attrs.get("tvg-logo", ""), "id": attrs.get("tvg-id", "")}
        elif line and not line.startswith("#"):
            parts = urllib.parse.urlsplit(line)
            if info is not None and parts.scheme.lower() in CHANNEL_SCHEMES and parts.hostname and len(line) <= 2000:
                name = info["name"] or line
                key = info["id"] or name
                base, n = key, 1
                while key in seen:                           # the same id or name twice: keep both, apart
                    n += 1
                    key = f"{base}#{n}"
                seen.add(key)
                out.append({"key": key[:200], "name": name[:120], "url": line, "group": info["group"][:80], "logo": info["logo"][:500]})
                if len(out) >= MAX_CHANNELS:
                    break
            info = None
    return out


def fetch_channels(source):
    """The channels a source has right now: [{key, name, url, ...}]. Raises SourceError with a reason."""
    kind, url = source["type"], source["url"]
    if kind == "stream":
        return [{"key": "stream", "name": source["name"], "url": url, "group": "", "logo": ""}]
    try:
        if kind == "hdhomerun":
            rows = json.loads(_get(url.rstrip("/") + "/lineup.json", 2 * 1024 * 1024))
            if not isinstance(rows, list):
                raise SourceError("that device did not answer like an HDHomeRun")
            out = []
            for c in rows[:MAX_CHANNELS]:
                if isinstance(c, dict) and c.get("GuideNumber") and c.get("URL"):
                    out.append({"key": str(c["GuideNumber"]), "name": str(c.get("GuideName") or c["GuideNumber"])[:120],
                                "url": str(c["URL"]), "group": "", "logo": "", "number": str(c["GuideNumber"]),
                                "hd": bool(c.get("HD"))})
            return out
        text = _get(url, MAX_PLAYLIST).decode("utf-8", "replace")
        out = Channels(parse_m3u(text))
        out.guide_urls = playlist_guide_urls(text)
        return out
    except SourceError:
        raise
    except Exception as e:
        raise SourceError(f"could not read {mask(url)}: {e}") from e


def probe(url, timeout=15):
    """Look at a stream for a few seconds: {"ok", "video", "audio", "error"}. ffprobe is given no way to open a local file."""
    url = check_url(url, "stream")
    cmd = ["ffprobe", "-v", "error", "-protocol_whitelist", WHITELIST, "-rw_timeout", "8000000",
           *(["-rtsp_transport", "tcp"] if url.lower().startswith("rtsp") else []),
           "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "no answer within a few seconds"}
    except OSError as e:
        return {"ok": False, "error": f"could not run ffprobe: {e}"}
    try:
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except ValueError:
        streams = []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not streams:
        why = (r.stderr or "").strip().splitlines()
        return {"ok": False, "error": (why[-1] if why else "no video or audio found")[:300].replace(url, mask(url))}
    return {"ok": video is not None or audio is not None,
            "video": f"{video['codec_name']} {video.get('width', '?')}x{video.get('height', '?')}" if video else None,
            "audio": audio["codec_name"] if audio else None}


# ---- playing a channel ------------------------------------------------------------------------------------
def ffmpeg_command(channel, normalize, mux):
    """The ffmpeg that makes one external channel into the tuner's MPEG-TS on stdout."""
    url = channel["url"]
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-protocol_whitelist", WHITELIST,
           "-fflags", "+genpts+discardcorrupt", "-rw_timeout", "15000000"]
    if scheme in ("rtsp", "rtsps"):
        cmd += ["-rtsp_transport", "tcp"]
    elif scheme in ("http", "https"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += ["-i", url, "-map", "0:v:0", "-map", "0:a:0?"]
    if channel.get("mode") == "relay":
        return cmd + ["-c", "copy"] + mux + ["pipe:1"]
    return cmd + normalize + mux + ["pipe:1"]


# ---- the list of sources ----------------------------------------------------------------------------------
def _clean_name(value, fallback=""):
    name = (value if isinstance(value, str) else "").strip()[:80] or fallback
    if not name:
        raise SourceError("a source needs a name")
    return name


class Sources:
    """The sources and what has been chosen about their channels, kept in a file of our own."""

    def __init__(self, path=None):
        self.path = path or PATH
        self._lock = threading.RLock()

    # -- the file
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".sources-")
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, self.path)

    # -- the sources
    def all(self):
        with self._lock:
            return self._load()

    def get(self, source_id):
        for s in self.all():
            if s["id"] == source_id:
                return s
        raise SourceError("there is no such source")

    def save(self, fields, source_id=None):
        """Add a source, or change one (the address and kind can't change: make a new source for that)."""
        with self._lock:
            items = self._load()
            if source_id is None:
                if len(items) >= MAX_SOURCES:
                    raise SourceError(f"there can be at most {MAX_SOURCES} sources")
                kind = fields.get("type")
                if kind not in TYPES:
                    raise SourceError("choose what kind of source it is")
                item = {"id": uuid.uuid4().hex[:8], "type": kind, "url": check_url(fields.get("url"), kind), "enabled": True,
                        "mode": "convert", "base": None, "channels": [], "numbers": {}, "overrides": {},
                        "refreshed": None, "error": None}
                items.append(item)
            else:
                item = next((s for s in items if s["id"] == source_id), None)
                if item is None:
                    raise SourceError("there is no such source")
            item["name"] = _clean_name(fields.get("name"), item.get("name", ""))
            if "mode" in fields:
                if fields["mode"] not in MODES or (fields["mode"] == "guide" and item["type"] != "hdhomerun"):
                    raise SourceError("play it converted for a browser, or relayed as it is" + (", or (for a tuner) guide only" if item["type"] == "hdhomerun" else ""))
                item["mode"] = fields["mode"]
            if "enabled" in fields:
                item["enabled"] = bool(fields["enabled"])
            if "base" in fields:
                b = fields["base"]
                if b in (None, ""):
                    item["base"] = None
                elif isinstance(b, bool) or not isinstance(b, int) or not 1 <= b <= 99999:
                    raise SourceError("the first channel number is a whole number from 1 to 99999")
                else:
                    item["base"] = b
            if source_id is not None and "url" in fields and fields["url"] != mask(item["url"]):    # the masked one sent back is no change
                item["url"] = check_url(fields["url"], item["type"])
            self._save(items)
            return item

    def delete(self, source_id):
        with self._lock:
            items = self._load()
            kept = [s for s in items if s["id"] != source_id]
            if len(kept) == len(items):
                raise SourceError("there is no such source")
            self._save(kept)

    def set_channels(self, source_id, channels=None, error=None):
        """The result of looking at a source's channels: the list, or why it could not be read (the old list stays)."""
        with self._lock:
            items = self._load()
            item = next((s for s in items if s["id"] == source_id), None)
            if item is None:
                return
            if channels is not None:
                item["channels"] = list(channels)
                item["guide_urls"] = list(getattr(channels, "guide_urls", ()) or ())
            item["refreshed"] = time.time()
            item["error"] = error
            self._save(items)

    def override(self, source_id, key, fields):
        """What was chosen for one channel: switched off, another name, another number."""
        with self._lock:
            items = self._load()
            item = next((s for s in items if s["id"] == source_id), None)
            if item is None:
                raise SourceError("there is no such source")
            if not any(c["key"] == key for c in item["channels"]):
                raise SourceError("that channel is not in this source")
            ov = dict(item["overrides"].get(key, {}))
            if "enabled" in fields:
                ov["enabled"] = bool(fields["enabled"])
            if "name" in fields:
                name = (fields["name"] or "").strip()[:120] if isinstance(fields["name"], str) else ""
                ov["name"] = name or None
            if "number" in fields:
                num = (str(fields["number"]).strip() if fields["number"] is not None else "")
                if num and (not re.fullmatch(r"\d{1,5}(\.\d{1,3})?", num) or float(num) == 0):
                    raise SourceError("a channel number is digits above 0, like 105 or 20.1 (Jellyfin ignores channel 0)")
                ov["number"] = num or None
            item["overrides"][key] = {k: v for k, v in ov.items() if v is not None}
            if not item["overrides"][key]:
                item["overrides"].pop(key)
            self._save(items)
            return item["overrides"].get(key, {})

    # -- the channels that are live
    def plan(self, taken=()):
        """Every channel that should be on air: [{number, name, source_id, source, type, mode, url, key, group, logo}],
        numbered. A channel keeps the number it was first given, even when others come and go. `taken` are numbers
        something else already has. Returns (channels, clashes) where clashes are the channels that could not keep
        the number they asked for."""
        with self._lock:
            items = self._load()
            used = {str(n) for n in taken}
            out, clashes, changed = [], [], False
            for order, src in enumerate(items):
                if not src.get("enabled", True) or src.get("mode") == "guide":
                    continue                                      # off, or guide only: no channels of its own in the lineup
                base = src.get("base") or 100 + 100 * order
                numbers = src.setdefault("numbers", {})
                for c in src.get("channels", []):
                    ov = src.get("overrides", {}).get(c["key"], {})
                    if ov.get("enabled") is False:
                        continue
                    want = ov.get("number") or (c.get("number") if src["type"] == "hdhomerun" else None) or numbers.get(c["key"])
                    if want and want not in used:
                        num = want
                    else:
                        if want:                                  # it asked for a number something else has
                            clashes.append({"source": src["name"], "channel": c["name"], "wanted": want})
                        mine = {v for k, v in numbers.items() if k != c["key"]}
                        n = base
                        while str(n) in used or str(n) in mine:
                            n += 1
                        num = str(n)
                        numbers[c["key"]] = num                   # remembered, so it is this number next time too
                        changed = True
                    used.add(num)
                    out.append({"number": num, "name": ov.get("name") or c["name"], "source_id": src["id"], "source": src["name"],
                                "type": src["type"], "mode": src.get("mode", "convert"), "url": c["url"], "key": c["key"],
                                "group": c.get("group", ""), "logo": c.get("logo", "")})
            if changed:
                self._save(items)
            return out, clashes

    def describe(self):
        """The sources for a page: addresses masked, with the channels and what was chosen for each."""
        with self._lock:
            items = self._load()
        out = []
        for s in items:
            out.append({"id": s["id"], "name": s["name"], "type": s["type"], "url": mask(s["url"]), "enabled": s.get("enabled", True),
                        "mode": s.get("mode", "convert"), "base": s.get("base"), "refreshed": s.get("refreshed"), "error": s.get("error"),
                        "channels": [{**{k: c.get(k, "") for k in ("key", "name", "group", "logo")},
                                      "number": s.get("numbers", {}).get(c["key"]) or (c.get("number") if s["type"] == "hdhomerun" else None),
                                      "override": s.get("overrides", {}).get(c["key"], {})} for c in s.get("channels", [])]})
        return out
