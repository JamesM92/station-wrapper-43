"""
Passthrough for a real HDHomeRun sitting behind this wrapper.

The wrapper presents ONE tuner device to Jellyfin whose lineup is the real
device's channels plus FieldStation42's virtual ones. That removes the reason
to run two tuner devices at all, and with it the channel-id collision: Jellyfin
keys an HDHomeRun channel as "hdhr_" + GuideNumber with nothing identifying the
device, so two devices sharing a number silently lose one. One lineup, one
namespace, and the wrapper can check its own numbering for duplicates.

Real-device channels are passed through with their GuideNumber and GuideName
untouched, so whatever guide provider already maps them (Schedules Direct, an
existing XMLTV feed) keeps matching. Only FieldStation42's channels are new,
and they live in a range of their own.
"""

import json
import threading
import time
import urllib.request


class UpstreamError(Exception):
    pass


class Upstream:
    """A real HDHomeRun, read over HTTP.

    Nothing here speaks HDHomeRun's native UDP control protocol. It doesn't
    need to: Jellyfin only falls back to that for what it calls a legacy tuner,
    decided by whether the channel's URL in lineup.json starts with
    "hdhomerun" rather than "http" (HdHomerunHost.cs). Any device reporting
    http:// URLs -- which is every model that streams over HTTP -- is driven
    entirely through plain GETs, and so is this.
    """

    def __init__(self, url, cache_seconds=300):
        self.url = url.rstrip("/")
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._discover = None
        self._discover_at = 0
        self._lineup = None
        self._lineup_at = 0

    def _get_json(self, url, timeout=8):
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310 - the tuner's own address from our config
            return json.load(r)

    def discover(self, force=False):
        with self._lock:
            fresh = time.time() - self._discover_at < self.cache_seconds
            if self._discover is not None and fresh and not force:
                return self._discover
        try:
            d = self._get_json(f"{self.url}/discover.json")
        except Exception as e:
            raise UpstreamError(f"cannot reach HDHomeRun at {self.url}: {e}") from e
        with self._lock:
            self._discover, self._discover_at = d, time.time()
        return d

    def lineup(self, force=False):
        with self._lock:
            fresh = time.time() - self._lineup_at < self.cache_seconds
            if self._lineup is not None and fresh and not force:
                return self._lineup
        d = self.discover()
        lineup_url = d.get("LineupURL") or f"{d.get('BaseURL', self.url)}/lineup.json"
        try:
            rows = self._get_json(lineup_url)
        except Exception as e:
            raise UpstreamError(f"cannot read lineup from {lineup_url}: {e}") from e
        # Keep only what we need, and keep GuideNumber/GuideName verbatim so
        # existing guide mappings survive the move behind the wrapper.
        out = [
            {"GuideNumber": str(c["GuideNumber"]),
             "GuideName": c.get("GuideName", str(c["GuideNumber"])),
             "URL": c["URL"],
             "HD": c.get("HD", 0)}
            for c in rows if c.get("GuideNumber") and c.get("URL")
        ]
        with self._lock:
            self._lineup, self._lineup_at = out, time.time()
        return out

    @property
    def tuner_count(self):
        """Physical tuners on the real device. This is a hard ceiling we must
        not oversubscribe -- unlike a FieldStation42 channel, two viewers on
        one OTA channel genuinely occupy two tuners unless the device itself
        shares them, which we cannot assume."""
        try:
            return int(self.discover().get("TunerCount", 2))
        except Exception:
            return 2

    def friendly_name(self):
        try:
            return self.discover().get("FriendlyName", "HDHomeRun")
        except Exception:
            return "HDHomeRun"


class TunerBudget:
    """Counts concurrent passthrough streams against the real device's tuners."""

    def __init__(self, n):
        self.n = n
        self._sem = threading.BoundedSemaphore(n) if n > 0 else None
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self):
        if self._sem is None:
            return True
        if not self._sem.acquire(blocking=False):
            return False
        with self._lock:
            self._active += 1
        return True

    def release(self):
        if self._sem is None:
            return
        with self._lock:
            if self._active == 0:
                return
            self._active -= 1
        self._sem.release()

    @property
    def active(self):
        with self._lock:
            return self._active
