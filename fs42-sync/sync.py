#!/usr/bin/env python3
"""fs42-sync: keep the symlink pool current, and notice when a tag folder changed.

Every SYNC_INTERVAL_MINUTES this does two things.

1. POOL. Walk the media share and add a symlink to the local pool (media-links/)
   for every video and .nfo that has no link yet. See pool.py. Add-only.

2. CATALOGS. FieldStation42 only sees a video once the station's catalog is
   refreshed. So a link you copy into a tag folder is invisible until you do. This notices the tag folders
   that changed since the last refresh and logs which stations are affected.
   With SYNC_REBUILD=1 it also refreshes them, once a day, within SYNC_REBUILD_WINDOW_HOURS
   (default 2) of SYNC_REBUILD_HOUR (default 4). The refresh only updates the catalog: the schedule is NOT touched,
   so the new videos are used the next time days are added (the schedule keeper does that each morning).
   On by default; SYNC_REBUILD=0 turns it off.

The first run records a baseline and does not treat existing tags as changed.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import pool

try:
    import schedlock
    import console_client
except ImportError:         # run from the repo: the module sits in the dashboard's folder
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs42-tuner"))
    import schedlock
    import console_client

VIDEO_EXT = pool.VIDEO_EXT
SKIP_TAGS = set()                   # bump is watched too: it is a real folder of links, and every station uses it
STATE_NAME = ".sync_state.json"
NETWORK_FS = {"cifs", "smb", "smb2", "smb3", "smbfs"}


def config(env=None):
    e = os.environ if env is None else env
    def flag(name):
        return e.get(name, "") in ("1", "true", "yes")
    return {
        "media": e.get("SYNC_MEDIA", "/mnt/media").rstrip("/"),
        "pool": e.get("SYNC_POOL", "/app/catalog/media-links").rstrip("/"),
        "catalog": e.get("SYNC_CATALOG", "/app/catalog/by-genre").rstrip("/"),
        "console": e.get("SYNC_CONSOLE", "http://127.0.0.1:4242").rstrip("/"),
        "interval": max(1, int(e.get("SYNC_INTERVAL_MINUTES", "60"))),
        "full_every": float(e.get("SYNC_FULL_EVERY_HOURS", "24")),
        "include": [x.strip() for x in e.get("SYNC_INCLUDE", "").split(",") if x.strip()],
        "exclude": [x.strip() for x in e.get("SYNC_EXCLUDE", "").split(",") if x.strip()],
        "prune": flag("SYNC_PRUNE"),
        # On unless switched off: it only updates catalogs (no schedule is touched), so it is safe to do every night
        "rebuild": e.get("SYNC_REBUILD", "").strip().lower() not in ("0", "false", "no", "off"),
        "rebuild_hour": int(e.get("SYNC_REBUILD_HOUR", "4")) % 24,
        "rebuild_window": max(1, int(e.get("SYNC_REBUILD_WINDOW_HOURS", "2"))),
        "run_once": flag("SYNC_RUN_ONCE"),
        "allow_network": e.get("FS42_ALLOW_NETWORK_CATALOG", "") == "1",
    }


# ---- the pool must be on local disk

def filesystem_of(path, mountinfo=None):
    """Filesystem type holding path (cifs, ext4, ...), or None. mountinfo is /proc/self/mountinfo text."""
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo") as f:
                mountinfo = f.read()
        except OSError:
            return None
    real = os.path.realpath(path)
    best = None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount = fields[4].replace("\\040", " ")
        if real == mount or real.startswith(mount.rstrip("/") + "/"):
            if best is None or len(mount) > len(best[0]):
                best = (mount, right.split()[0])
    return best[1] if best else None


# ---- which tag folders changed

def tag_signatures(catalog):
    """{tag: hash of its videos}: names and where each link points. NFOs and other files are ignored.

    A tag is a top-level folder of the content folder. A video added, removed
    or re-pointed changes its tag's hash. A whole folder linked into a tag counts
    by where it points, not by what is inside it, so new files inside such a
    folder are not noticed here (rebuild that station by hand).
    """
    sigs = {}
    if not os.path.isdir(catalog):
        return sigs
    for tag in sorted(os.listdir(catalog)):
        path = os.path.join(catalog, tag)
        if tag.startswith(".") or tag in SKIP_TAGS or os.path.islink(path) or not os.path.isdir(path):
            continue
        h = hashlib.sha256()
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for d in dirs:
                full = os.path.join(root, d)
                if os.path.islink(full):            # a whole folder linked in: its target counts, not its contents
                    h.update(f"dir:{os.path.relpath(full, path)}\0{os.readlink(full)}\n".encode())
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                    continue
                full = os.path.join(root, name)
                target = os.readlink(full) if os.path.islink(full) else ""
                h.update(f"{os.path.relpath(full, path)}\0{target}\n".encode())
        sigs[tag] = h.hexdigest()
    return sigs


def strings_in(value):
    """Every string anywhere in a station config. Tags are plain strings in it, wherever FieldStation42 keeps them."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings_in(k)
            yield from strings_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from strings_in(v)


def stations_using(tag, stations):
    """Names of the stations whose config mentions this tag, or a folder inside it.

    Read as plain strings rather than by key, so it keeps working if FieldStation42
    renames where a station keeps its tags.
    """
    hit = []
    for name, conf in stations.items():
        if any(s == tag or s.startswith(tag + "/") for s in strings_in(conf)):
            hit.append(name)
    return sorted(hit)


# ---- FieldStation42's console

class Console:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _call(self, method, path):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=b"{}" if method == "POST" else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310 - the console's own address
            return json.loads(r.read() or b"{}")

    def stations(self):
        return {s["station_conf"]["network_name"]: s["station_conf"] for s in self._call("GET", "/stations")["stations"]}

    def _wait(self, path, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._call("GET", path)
            if st.get("status") == "done":
                return
            if st.get("status") == "error" or "error" in st:
                raise RuntimeError(st.get("log", st.get("error", ""))[-300:])
            time.sleep(3)
        raise RuntimeError("timed out")

    def update_catalog(self, name, timeout=4 * 3600):
        """Bring new videos into the station's catalog. The schedule is left alone (console_client.update_catalog)."""
        try:
            console_client.Console(self.base).update_catalog(name, timeout=timeout)
        except console_client.ConsoleError as e:
            raise RuntimeError(str(e))

    def add_day(self, name, timeout=300):
        with schedlock.held():
            task = self._call("POST", f"/build/schedule/add_time/day/{urllib.parse.quote(name, safe='')}")["task_id"]
            self._wait(f"/build/schedule/add_time/status/{task}", timeout)


# ---- state

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


# ---- one pass

def in_window(hour, start, width):
    """Is this hour within `width` hours from `start`? (Wraps past midnight.)

    A window rather than one exact hour: the passes are an hour apart and drift
    with how long each takes, so a single hour could be skipped altogether.
    """
    return (hour - start) % 24 < width


def watch_catalogs(cfg, state, console_factory, now, log=print):
    """Compare tag folders with the last baseline; rebuild the affected stations if allowed.

    Returns the stations still waiting for a rebuild.
    """
    cat = state.setdefault("catalogs", {"sigs": {}, "last_rebuild_day": ""})
    current = tag_signatures(cfg["catalog"])
    if not cat["sigs"] and "baselined" not in cat:
        cat["sigs"], cat["baselined"] = current, True       # first run: what is here now is the baseline
        log(f"catalogs: baseline recorded for {len(current)} tags")
        return []
    changed = sorted(t for t in current if current[t] != cat["sigs"].get(t))
    if not changed:
        return []
    try:
        console = console_factory(cfg["console"])
        stations = console.stations()
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        log(f"catalogs: tags changed ({', '.join(changed)}) but the console is not reachable ({e}); will retry")
        return []
    pending = {}
    for tag in changed:
        for name in stations_using(tag, stations):
            pending.setdefault(name, []).append(tag)
    unused = [t for t in changed if not any(t in tags for tags in pending.values())]
    for tag in unused:
        log(f"catalogs: tag '{tag}' changed, but no station uses it; recorded")
        cat["sigs"][tag] = current[tag]
    if not pending:
        return []
    for name, tags in sorted(pending.items()):
        log(f"catalogs: station '{name}' needs a catalog rebuild: {', '.join(tags)} changed")
    today = now.strftime("%Y-%m-%d")
    if not cfg["rebuild"]:
        log("catalogs: SYNC_REBUILD is off. Press Update videos on these stations (Stations page) to bring the "
            "changes in; their schedules are not touched.")
        return sorted(pending)
    if not in_window(now.hour, cfg["rebuild_hour"], cfg["rebuild_window"]) or cat.get("last_rebuild_day") == today:
        log(f"catalogs: will rebuild from {cfg['rebuild_hour']}:00, for {cfg['rebuild_window']} hours")
        return sorted(pending)
    failed = False
    for name, tags in sorted(pending.items()):
        try:
            console.update_catalog(name)
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            log(f"catalogs: updating the catalog of '{name}' failed: {e}")
            failed = True
            continue
        log(f"catalogs: updated the catalog of '{name}' (its schedule is untouched)")
        for tag in tags:
            cat["sigs"][tag] = current[tag]
    if not failed:
        cat["last_rebuild_day"] = today             # a failure leaves today open, so the next pass retries
    return sorted(n for n, tags in pending.items() if any(cat["sigs"].get(t) != current[t] for t in tags))


def run_once(cfg, console_factory=Console, now=None, log=print):
    now = now or datetime.now()
    state_path = os.path.join(cfg["pool"], STATE_NAME)
    state = load_state(state_path)
    pstate = state.setdefault("pool", pool.new_state())
    ts = time.time()
    full = (ts - pstate.get("last_full", 0)) >= cfg["full_every"] * 3600
    stats = pool.scan_pool(cfg["media"], cfg["pool"], pstate, full=full, include=cfg["include"],
                           exclude=cfg["exclude"], now=ts)
    log(f"pool: {stats['links_added']} links added; {stats['dirs_listed']} folders listed, "
        f"{stats['dirs_cached']} unchanged{' (full scan)' if stats['full'] else ''}")
    for note in stats["notes"][:10]:
        log(f"pool: note: {note}")
    if len(stats["notes"]) > 10:
        log(f"pool: ... and {len(stats['notes']) - 10} more notes")
    if cfg["prune"]:
        log(f"pool: pruned {pool.prune_pool(cfg['media'], cfg['pool'])} links whose file is gone")
    pending = watch_catalogs(cfg, state, console_factory, now, log)
    save_state(state_path, state)
    return {"pool": stats, "pending": pending}


def main():
    cfg = config()
    if not os.path.isdir(cfg["media"]):
        print(f"fs42-sync: no media at {cfg['media']}. Mount the share there (it is read-only here).", file=sys.stderr)
        return 78
    os.makedirs(cfg["pool"], exist_ok=True)
    fs = filesystem_of(cfg["pool"])
    if fs in NETWORK_FS and not cfg["allow_network"]:
        print(f"fs42-sync: the pool ({cfg['pool']}) is on an SMB share ({fs}). It is a folder of symlinks, "
              f"which do not work there. Use local disk (FS42_LOCAL_ROOT). REFUSING TO START.", file=sys.stderr)
        return 78
    print(f"fs42-sync: pool {cfg['pool']} <- {cfg['media']}, every {cfg['interval']} min"
          f"{', rebuilding changed catalogs from ' + str(cfg['rebuild_hour']) + ':00' if cfg['rebuild'] else ''}", flush=True)
    while True:
        try:
            run_once(cfg)
        except Exception as e:                      # one bad pass must not end the loop
            print(f"fs42-sync: that pass failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if cfg["run_once"]:
            return 0
        time.sleep(cfg["interval"] * 60)


if __name__ == "__main__":
    sys.exit(main())
