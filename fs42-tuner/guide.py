"""
One XMLTV document covering every channel in the merged lineup.

Jellyfin binds a guide channel to a tuner channel by trying, in order:
channel Id, TunerChannelId, **Number**, then normalized Name
(ListingsManager.GetEpgChannelFromTunerChannel). The number fallback is the
reliable one here, so every <channel> this builds carries its tuner channel
number as a <display-name>. That is what lets a single guide feed cover both
FieldStation42's invented channels and the real tuner's broadcast ones.

Sources:
  fs42        FieldStation42's schedule, read from its sqlite db
  hdhomerun   SiliconDust's guide service for the passed-through device,
              fetched with the DeviceAuth token out of its discover.json
  xmltv       any existing XMLTV feed, by url or local path
"""

import gzip
import io
import os
import re
import json
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as SafeET
from datetime import datetime, timedelta, timezone

# Overridable so the endpoint can be pointed at a stub in tests.
SD_GUIDE = os.environ.get("FS42_SD_GUIDE", "https://api.hdhomerun.com/api/guide.php")      # my.hdhomerun.com now answers 403


def _tz_offset():
    return datetime.now().astimezone().strftime("%z")


def _fmt_local(dt):
    return dt.strftime("%Y%m%d%H%M%S ") + _tz_offset()


def _fmt_epoch(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _fetch(url, timeout=30):
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("a guide address must be http or https")
    req = urllib.request.Request(url, headers={"User-Agent": "fs42-tuner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - http and https only, checked above
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":            # some EPG feeds are served gzipped
        raw = gzip.decompress(raw)
    return raw


OFF_AIR_TITLES = {"offair", "off air", "off-air"}      # what FieldStation42 calls the blocks it fills with the test pattern


class GuideBuilder:
    """Assembles and caches the merged XMLTV document."""

    def __init__(self, fs42_channels, resolver, db_path, guide_hours=48,
                 sources=None, refresh_minutes=60, upstream=None):
        self.fs42 = fs42_channels          # {number: station}
        self.resolver = resolver
        self.db_path = db_path
        self.hours = guide_hours
        self.sources = sources or []
        self.refresh = refresh_minutes * 60
        self.upstream = upstream
        self.extra_sources = None          # optional: () -> more guide sources, worked out at each build (the Sources page's tuners)
        self.display = None                # optional: station key -> the name people see
        self.static = None                 # optional: () -> {station name: title} for stations with no schedule (a guide channel)
        self.details = None                # optional: a video's path -> {"plot", "season", "episode", "aired", "year", "genres"} (its .nfo)
        self.art_url = None                # optional: a video's path -> the address of its picture, or None
        self.logo_url = None               # optional: (channel number, station key) -> the address of the channel's logo, or None
        self._lock = threading.Lock()
        self._doc = None
        self._building = False
        self._last_build = None
        self._at = 0
        self._stats = {}
        self.cache_dir = None              # optional: a folder that keeps SiliconDust's answers across restarts
        self._sd_cache = {}                # source id prefix -> (fetched at, [channel xml], [programme xml])
        self._sd_blocked_until = 0         # SiliconDust said slow down: leave it alone until then

    # -- public ----------------------------------------------------------
    def xmltv(self, force=False):
        """The guide document. A guide that is out of date is still handed out at once while a new one is made in the
        background (making it asks other servers for listings and can take many seconds: the guide channel and the player
        must never stall on that). Only the very first call, or force=True, waits for a build."""
        with self._lock:
            if self._doc is not None and not force:
                if time.time() - self._at >= self.refresh and not self._building:
                    self._building = True
                    threading.Thread(target=self._rebuild, daemon=True, name="guide-refresh").start()
                return self._doc
            first = self._doc is None and not force
            if first and not self._building:
                self._building = True
                threading.Thread(target=self._rebuild, daemon=True, name="guide-refresh").start()
        if first:
            # the very first request does not wait for the listings from other servers (that can take a minute): it gets the
            # guide of our own channels now, and the full one replaces it as soon as it is built
            quick = self._build(quick=True)
            with self._lock:
                if self._doc is None:
                    self._doc, self._at = quick, 0.0
                return self._doc
        doc = self._build()
        with self._lock:
            self._doc, self._at = doc, time.time()
        return doc

    def _rebuild(self):
        try:
            doc = self._build()
            with self._lock:
                self._doc, self._at = doc, time.time()
        except Exception as e:                       # keep the old one: a guide that fails to build must not blank the listings
            print(f"! guide refresh failed: {e}", flush=True)
        finally:
            with self._lock:
                self._building = False

    @property
    def stats(self):
        with self._lock:
            return dict(self._stats, age_seconds=round(time.time() - self._at) if self._at else None, last_build=self._last_build)

    # -- assembly --------------------------------------------------------
    def _build(self, quick=False):
        """The whole guide document. quick=True leaves out every source that is asked for over the network (the antenna's
        listings, an XMLTV feed): it is made in a moment and used until the full one is ready."""
        began = time.time()
        tv = ET.Element("tv", {"generator-info-name": "station-wrapper-43"})
        channels, programmes = [], []
        stats = {}

        c, p = self._fs42_elements()
        channels += c
        programmes += p
        stats["fs42"] = {"channels": len(c), "programmes": len(p)}
        fs42_programmes = p

        retag = {}                          # (source's channel id) -> our lineup channel id, for a Sources-page tuner's channels
        extra = []
        if self.extra_sources and not quick:
            try:
                extra = list(self.extra_sources() or [])
            except Exception as e:
                stats["sources"] = {"error": str(e)}
        for src in ([] if quick else list(self.sources) + extra):
            kind = src.get("type")
            if not src.get("enabled", True):
                continue
            try:
                if kind == "hdhomerun":
                    c, p = self._hdhomerun_elements(src)
                    for guide_number, lineup_number in (src.get("renumber") or {}).items():
                        retag[f"{src.get('id_prefix', 'hdhr')}.{guide_number}"] = f"fs42.{lineup_number}"
                elif kind == "xmltv":
                    c, p = self._xmltv_elements(src)
                    if src.get("retag"):                       # a playlist's guide: its ids are our channels' playlist ids
                        if src.get("only_mapped"):
                            c = [el for el in c if el.get("id") in src["retag"]]
                            p = [el for el in p if el.get("channel") in src["retag"]]
                        for feed_id, lineup_number in src["retag"].items():
                            retag[feed_id] = f"fs42.{lineup_number}"
                else:
                    stats[kind or "?"] = {"error": f"unknown source type {kind!r}"}
                    continue
                channels += c
                programmes += p
                stats[src.get("name", kind)] = {"channels": len(c), "programmes": len(p)}
            except Exception as e:
                # A guide source failing must never take the guide down --
                # Jellyfin would wipe the listings it already has.
                stats[src.get("name", kind)] = {"error": str(e)}
                print(f"! guide source {kind}: {e}")

        # A Sources-page tuner's channels are already in the lineup under their own numbers: its real listings are put on
        # those channels (replacing the one "Live" programme they had) and its own copy of the channel is left out.
        if retag:
            lineup_ids = {el.get("id") for el in channels}
            have = set()
            for el in programmes:
                target = retag.get(el.get("channel"))
                if target and target in lineup_ids:
                    el.set("channel", target)
                    have.add(target)
            channels = [el for el in channels if el.get("id") not in retag]
            programmes = [el for el in programmes if not (el in fs42_programmes and el.get("channel") in have)]

        # A duplicate channel id makes some parsers drop listings; keep first.
        seen = set()
        for el in channels:
            cid = el.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            tv.append(el)
        for el in programmes:
            if el.get("channel") in seen:
                tv.append(el)

        with self._lock:
            self._stats = stats
        if not quick:
            self._last_build = {"seconds": round(time.time() - began, 1), "at": time.time()}
        print(f"[guide] built in {time.time() - began:.1f}s" + (" (quick: without the sources asked over the network)" if quick else "")
              + ": " + ", ".join(f"{k} {v.get('channels', '?')} ch / {v.get('programmes', '?')} programmes" if "error" not in v else f"{k} FAILED ({v['error']})"
                                 for k, v in stats.items()), flush=True)
        doc = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8")
        return re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", doc)       # characters XML cannot hold would make a strict parser (a browser, Jellyfin) refuse the whole guide

    # -- FieldStation42 ---------------------------------------------------
    def _fs42_elements(self):
        now = datetime.now()
        end = now + timedelta(hours=self.hours)
        chans, progs = [], []
        for num, station in self.fs42.items():
            ch = ET.Element("channel", {"id": f"fs42.{num}"})
            ET.SubElement(ch, "display-name").text = self.display(station) if self.display else station
            # The number MUST be here: it is how Jellyfin matches when the id
            # does not line up.
            ET.SubElement(ch, "display-name").text = str(num)
            if self.logo_url:
                try:
                    logo = self.logo_url(num, station)
                except Exception:
                    logo = None
                if logo:
                    ET.SubElement(ch, "icon", {"src": logo})
            chans.append(ch)

            listings = self.resolver.listings(self.db_path, station, now - timedelta(hours=1), end)
            title = (self.static() if self.static else {}).get(station)
            if title and not listings:               # a channel with no schedule of its own (the guide channel): one long programme
                listings = [{"title": title, "start": now - timedelta(hours=1), "end": end}]
            for b in listings:
                if str(b.get("title", "")).strip().lower() in OFF_AIR_TITLES:
                    continue                          # time with nothing scheduled is a gap in the guide, not a programme called Offair
                pr = ET.Element("programme", {
                    "start": _fmt_local(b["start"]),
                    "stop": _fmt_local(b["end"]),
                    "channel": f"fs42.{num}"})
                t = ET.SubElement(pr, "title")
                t.set("lang", "en")
                t.text = b["title"]
                if b.get("subtitle"):                    # the episode's name: the guide channel shows it apart, small
                    st = ET.SubElement(pr, "sub-title")
                    st.set("lang", "en")
                    st.text = b["subtitle"]
                self._describe(pr, b)
                progs.append(pr)
        return chans, progs

    def _describe(self, pr, b):
        """The rest of what is known of the video a block plays, as a tuner's own guide would carry it: the synopsis, the date
        it aired, its kinds, its picture and its season and episode (from its .nfo and the thumbnail beside it). Added in
        the order XMLTV asks for. Never allowed to break the guide."""
        path = b.get("path")
        if not path or self.details is None:
            return
        try:
            d = self.details(path) or {}
            if d.get("plot"):
                e = ET.SubElement(pr, "desc")
                e.set("lang", "en")
                e.text = d["plot"]
            if d.get("aired"):
                ET.SubElement(pr, "date").text = d["aired"].replace("-", "")
            elif d.get("year"):
                ET.SubElement(pr, "date").text = str(d["year"])
            for g in d.get("genres", []):
                c = ET.SubElement(pr, "category")
                c.set("lang", "en")
                c.text = g
            art = self.art_url(path) if self.art_url else None
            if art:
                ET.SubElement(pr, "icon", {"src": art})
            if d.get("season") is not None and d.get("episode") is not None:
                s, e = d["season"], d["episode"]
                ET.SubElement(pr, "episode-num", {"system": "xmltv_ns"}).text = f"{max(s - 1, 0)}.{max(e - 1, 0)}."
                ET.SubElement(pr, "episode-num", {"system": "onscreen"}).text = f"S{s:02d}E{e:02d}"
        except Exception as e:
            print(f"! guide: details of {path}: {e}", flush=True)

    # -- SiliconDust guide for the real tuner ------------------------------
    SD_CACHE_MINUTES = 180                 # how long SiliconDust's listings are used before asking again
    SD_BACKOFF_MINUTES = 30                # how long to leave it alone after it refuses us

    def _hdhomerun_elements(self, src):
        """SiliconDust's listings for the antenna, asked for as rarely as the listings change.

        A fetch is one request for the channel list and then a page per channel per few hours, which is dozens of
        requests; the guide is built every hour and again after every station build, so asking each time gets the
        address rate limited. The answer is kept (in memory and, when there is a folder for it, on disk, so a restart
        does not ask again), used until it is SD_CACHE_MINUTES old, and still used when the service refuses or fails.
        """
        prefix = src.get("id_prefix", "hdhr")
        ttl = float(src.get("cache_minutes", self.SD_CACHE_MINUTES)) * 60
        now = time.time()
        hit = self._sd_cache.get(prefix) or self._sd_load(prefix)
        if hit and now - hit[0] < ttl:
            return self._sd_parse(hit)
        if hit and now < self._sd_blocked_until:
            return self._sd_parse(hit)
        if not hit and now < self._sd_blocked_until:
            raise RuntimeError("SiliconDust asked us to slow down; trying again later")
        try:
            chans, progs = self._hdhomerun_fetch(src)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                self._sd_blocked_until = now + self.SD_BACKOFF_MINUTES * 60
                print(f"! guide: SiliconDust answered {e.code} (rate limited?); leaving it alone for "
                      f"{self.SD_BACKOFF_MINUTES} minutes", flush=True)
            if hit:
                return self._sd_parse(hit)
            raise
        except Exception:
            if hit:
                return self._sd_parse(hit)
            raise
        if chans:
            hit = (now, [ET.tostring(c, encoding="unicode") for c in chans], [ET.tostring(q, encoding="unicode") for q in progs])
            self._sd_cache[prefix] = hit
            self._sd_save(prefix, hit)
        return chans, progs

    @staticmethod
    def _sd_parse(hit):
        return [SafeET.fromstring(x) for x in hit[1]], [SafeET.fromstring(x) for x in hit[2]]

    def _sd_file(self, prefix):
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, "sd-guide-" + re.sub(r"[^A-Za-z0-9_.-]", "_", prefix) + ".json")

    def _sd_load(self, prefix):
        path = self._sd_file(prefix)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            hit = (float(d["at"]), list(d["channels"]), list(d["programmes"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self._sd_cache[prefix] = hit
        return hit

    def _sd_save(self, prefix, hit):
        path = self._sd_file(prefix)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": hit[0], "channels": hit[1], "programmes": hit[2]}, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def _hdhomerun_fetch(self, src):
        """SiliconDust serve a guide for channels the device can actually see,
        keyed by the DeviceAuth token in its discover.json. Depth depends on
        the device's own entitlement -- a unit without an HDHomeRun DVR
        subscription typically returns a short window rather than days -- so
        treat whatever comes back as the ceiling, not a bug here.
        """
        auth = src.get("device_auth")
        if not auth:
            if self.upstream is None:
                raise RuntimeError("no passthrough device to take DeviceAuth from")
            auth = self.upstream.discover().get("DeviceAuth")
        if not auth:
            raise RuntimeError("device reported no DeviceAuth")

        prefix = src.get("id_prefix", "hdhr")
        want_hours = src.get("hours", self.hours)
        chans, progs = [], []

        rows = [r for r in (self._sd_get(f"{SD_GUIDE}?DeviceAuth={auth}") or [])]
        from concurrent.futures import ThreadPoolExecutor

        def one(row):
            num = str(row.get("GuideNumber"))
            cid = f"{prefix}.{num}"
            ch = ET.Element("channel", {"id": cid})
            ET.SubElement(ch, "display-name").text = row.get("GuideName", num)
            ET.SubElement(ch, "display-name").text = num          # the matcher
            if row.get("ImageURL"):
                ET.SubElement(ch, "icon", {"src": row["ImageURL"]})
            try:
                got = self._sd_channel_programmes(auth, num, cid, row.get("Guide") or [], want_hours, src)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429, 503):                     # refused, not broken: stop asking, keep what we had
                    raise
                print(f"! guide: channel {num}: {e}", flush=True)
                got = []
            except Exception as e:                                # one channel failing must not lose the others
                print(f"! guide: channel {num}: {e}", flush=True)
                got = []
            return ch, got
        with ThreadPoolExecutor(max_workers=3) as pool:           # a page per channel per few hours: a few at once, not a burst
            for ch, got in pool.map(one, rows):
                chans.append(ch)
                progs += got
        return chans, progs

    def _sd_channel_programmes(self, auth, num, cid, first_page, want_hours, src):
        """The bare call returns one page per channel; walk forward with Start
        until we have the window we want or the service stops advancing."""
        horizon = time.time() + want_hours * 3600
        synopsis = int(src.get("synopsis_length", 160))
        seen, out = set(), []
        page, guard = first_page, 0

        while page and guard < 24:
            latest = 0
            for pr in page:
                key = (pr.get("StartTime"), pr.get("Title"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(self._sd_programme(pr, cid))
                latest = max(latest, int(pr.get("EndTime", 0)))
            if latest == 0 or latest >= horizon:
                break
            guard += 1
            nxt = self._sd_get(
                f"{SD_GUIDE}?DeviceAuth={auth}&Channel={num}"
                f"&Start={latest}&SynopsisLength={synopsis}")
            if not nxt:
                break
            # Match on GuideNumber rather than taking nxt[0]. The service
            # returns just the requested channel, but a stray extra row would
            # otherwise graft another channel's listings onto this one.
            row = next((r for r in nxt if str(r.get("GuideNumber")) == num), None)
            if row is None:
                break
            page = row.get("Guide") or []
        return out

    @staticmethod
    def _sd_programme(pr, cid):
        el = ET.Element("programme", {
            "start": _fmt_epoch(int(pr["StartTime"])),
            "stop": _fmt_epoch(int(pr["EndTime"])),
            "channel": cid})
        t = ET.SubElement(el, "title")
        t.set("lang", "en")
        t.text = pr.get("Title", "Unknown")
        if pr.get("EpisodeTitle"):
            st = ET.SubElement(el, "sub-title")
            st.set("lang", "en")
            st.text = pr["EpisodeTitle"]
        if pr.get("Synopsis"):
            d = ET.SubElement(el, "desc")
            d.set("lang", "en")
            d.text = pr["Synopsis"]
        if pr.get("EpisodeNumber"):
            # "S01E05" -> xmltv_ns wants 0-based "0.4."
            en = ET.SubElement(el, "episode-num")
            en.set("system", "onscreen")
            en.text = pr["EpisodeNumber"]
        if pr.get("OriginalAirdate"):
            ET.SubElement(el, "date").text = datetime.fromtimestamp(
                int(pr["OriginalAirdate"]), timezone.utc).strftime("%Y%m%d")
        if pr.get("ImageURL"):
            ET.SubElement(el, "icon", {"src": pr["ImageURL"]})
        for f in pr.get("Filter") or []:
            c = ET.SubElement(el, "category")
            c.set("lang", "en")
            c.text = f
        return el

    @staticmethod
    def _sd_get(url):
        import json
        return json.loads(_fetch(url))

    # -- an existing XMLTV feed -------------------------------------------
    def _xmltv_elements(self, src):
        """Merge someone else's XMLTV in as-is.

        Channel ids are kept, because the feed's <programme channel="..."> refer
        to them. If the feed's channels carry no number as a display-name,
        `number_map` can add one so Jellyfin's number fallback still matches.
        """
        if src.get("url"):
            raw = _fetch(src["url"])
        elif src.get("path"):
            with open(src["path"], "rb") as f:
                raw = f.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
        else:
            raise RuntimeError("xmltv source needs a url or a path")

        root = SafeET.parse(io.BytesIO(raw)).getroot()          # a feed from outside: no entity tricks
        number_map = {str(k): str(v) for k, v in (src.get("number_map") or {}).items()}

        chans = []
        for ch in root.findall("channel"):
            cid = ch.get("id")
            if cid in number_map:
                names = [d.text for d in ch.findall("display-name")]
                if number_map[cid] not in names:
                    ET.SubElement(ch, "display-name").text = number_map[cid]
            chans.append(ch)
        return chans, root.findall("programme")
