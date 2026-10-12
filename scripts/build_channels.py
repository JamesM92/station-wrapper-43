#!/usr/bin/env python3
"""Build one FieldStation42 channel per genre from a Sonarr/Radarr archive.

No AI: genres come from the Kodi .nfo files Sonarr and Radarr write.

  tvseries/ and anime/   each show folder's tvshow.nfo (<genre> tags)
  movies/                each movie's sidecar <video>.nfo, else movie.nfo
  --tv-library PATH      PATH itself is a TV library: each subfolder with a
                         tvshow.nfo and videos is a show (the layout
                         TA_NFOWriter builds). Repeatable. Labelled by the
                         folder name in the farm.

A show or movie goes under its primary genre, the first <genre> listed.
Items with no genre are reported and skipped unless --include-unclassified.

Groups. A group takes videos from a source and sends them somewhere other
than where their show's genre would put them. Three kinds of source, each
NAME[=DEST], all repeatable and mixable:
  --playlist NAME   a TA_NFOWriter playlist (<tv-library>/Playlists/NAME.m3u8,
                    written with its --playlists option)
  --show NAME       a whole show, series or TubeArchivist channel, by folder name
  --video PATTERN   individual videos: PATTERN matches "<show>/<path in show>",
                    e.g. "80s Ads/Season 1986/*[abc123]*". DEST is required.
NAME and PATTERN may be wildcards ("1980s Ads *"). DEST says where they go:
  (none)       its own channel, named after the playlist or show
  Genre        that genre's channel
  commercials  the commercials every station plays between programmes
               (left off the channels)
A group adds its videos to DEST and leaves them where they are, so a video can
live in several places; only "commercials" stays off the programme channels.
Several groups can share one DEST to build one set.

The generator writes a symlink farm, <catalog>/by-genre/<Genre>/<library>/
<item>/..., where each link points at the archive as the containers see it
(/mnt/media by default). FieldStation42 picks content by tag, a folder name
under content_dir, so one station per genre uses that genre as its tag.

The farm is a plain FieldStation42 content folder, and this script only ever
adds to it. Each run links the videos that are missing, and never removes or
rewrites a link, so links you add or change by hand survive, and one video can
live in several tag folders with no copies. A station is created when it is
missing and is never rewritten after that: its config is yours to edit in
FieldStation42. A station whose tag folder gained links gets its catalog
rebuilt and one day of schedule added back. Channel numbers are kept across
runs. Stations this script didn't create are never touched. --prune removes
links whose source video is gone, and nothing else.

Usage:
  build_channels.py --media-root /mnt/media --catalog /path/to/catalog \
      --tuner-config /path/to/config/tuner/config.json [--dry-run]

  # TubeArchivist channels via TA_NFOWriter, no Sonarr/Radarr folders:
  build_channels.py --media-root /mnt/media --no-default-libraries \
      --tv-library /mnt/media/tanfo --catalog ... --tuner-config ...
  #   ... plus groups, in any mix:
  #   --playlist "Saturday Cartoons" --show "80s Ads=commercials" \\
  #   --video "News/*[nnRYx_dfvoY]*=Retro Mix"
"""
import argparse
import fnmatch
import json
import os
import shutil
import glob
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import defusedxml.ElementTree as ET

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
TV_LIBRARIES = ("tvseries", "anime")
MOVIE_LIBRARY = "movies"
FARM = "by-genre"
BUMP = "bump"
COMMERCIALS = "commercials"
STATE_NAME = ".build_channels_state.json"
UNCLASSIFIED = "Unclassified"


def read_genres(nfo_path, root_tag):
    """All <genre> values from a Kodi NFO with the given root tag, in order."""
    try:
        root = ET.parse(nfo_path).getroot()
    except (OSError, ET.ParseError):
        return []
    if root.tag != root_tag:
        return []
    out = []
    for g in root.findall("genre"):
        if g.text and g.text.strip():
            out.append(g.text.strip())
    return out


def sanitize(name):
    return " ".join(name.replace("/", "-").replace("\\", "-").split())


def normalise_genre(raw, genre_map):
    key = raw.casefold()
    for k, v in genre_map.items():
        if k.casefold() == key:
            return sanitize(v)
    return sanitize(raw)


def media_files(item_dir):
    """Video files and .nfo sidecars under an item, as relative paths."""
    found = []
    for root, dirs, files in os.walk(item_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for f in files:
            if f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXT or ext == ".nfo":
                rel = os.path.relpath(os.path.join(root, f), item_dir)
                found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def tv_genres(item_dir):
    return read_genres(os.path.join(item_dir, "tvshow.nfo"), "tvshow")


def movie_genres(item_dir, videos):
    for rel in videos:
        if os.path.splitext(rel)[1].lower() in VIDEO_EXT:
            sidecar = os.path.splitext(os.path.join(item_dir, rel))[0] + ".nfo"
            genres = read_genres(sidecar, "movie")
            if genres:
                return genres
            break
    return read_genres(os.path.join(item_dir, "movie.nfo"), "movie")


def parse_tv_library(spec, reserved):
    """'HOST' or 'HOST=CONTAINER' -> (label, host dir, target dir).

    The target is the path symlinks point at, as the containers see it. It
    defaults to the host path, which is right when the folder is mounted at
    the same absolute path everywhere (the TA_NFOWriter deployment plan).
    """
    host, _, target = spec.partition("=")
    host = os.path.abspath(host)
    if not os.path.isdir(host):
        raise SystemExit(f"--tv-library is not a directory: {host}")
    label = os.path.basename(host.rstrip("/"))
    if not label or label in reserved:
        raise SystemExit(f"--tv-library {host}: the folder name '{label}' clashes with another library")
    target = (target or host).rstrip("/")
    if not os.path.isabs(target):
        raise SystemExit(f"--tv-library target must be an absolute path: {target}")
    return label, host, target


def discover(media_root, genre_map, tv_libraries=(), default_libraries=True):
    """Every show and movie, with its genre and the files that belong to it.

    tv_libraries: (label, host dir, target dir) for folders that are a TV
    library themselves. An item's "target" is where its farm links point:
    relative to --target-root for the default libraries, absolute otherwise.
    """
    items, warnings = [], []
    libraries = []
    if default_libraries:
        libraries += [(lib, os.path.join(media_root, lib), tv_genres, lib) for lib in TV_LIBRARIES]
        libraries.append((MOVIE_LIBRARY, os.path.join(media_root, MOVIE_LIBRARY), None, MOVIE_LIBRARY))
    libraries += [(label, host, tv_genres, target) for label, host, target in tv_libraries]
    for lib, lib_dir, genre_fn, target_prefix in libraries:
        if not os.path.isdir(lib_dir):
            warnings.append(f"library not found: {lib_dir}")
            continue
        for name in sorted(os.listdir(lib_dir)):
            item_dir = os.path.join(lib_dir, name)
            if name.startswith(".") or name == "__pycache__" or not os.path.isdir(item_dir):
                continue
            files = media_files(item_dir)
            videos = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXT]
            if not videos:
                continue
            if genre_fn is None:
                raw = movie_genres(item_dir, videos)
            else:
                raw = genre_fn(item_dir)
            genres = [g for g in (normalise_genre(r, genre_map) for r in raw) if g]
            items.append({
                "library": lib,
                "item": name,
                "genre": genres[0] if genres else None,
                "files": files,
                "dir": item_dir,
                "target": f"{target_prefix}/{name}",
            })
    return items, warnings


def playlist_names(tv_libraries):
    """Names of every playlist the --tv-library folders hold, sorted."""
    return sorted({
        os.path.splitext(f)[0]
        for _l, host, _t in tv_libraries
        if os.path.isdir(os.path.join(host, "Playlists"))
        for f in os.listdir(os.path.join(host, "Playlists")) if f.endswith(".m3u8")})


def expand_playlist(name, tv_libraries):
    """A playlist name, or every playlist matching a wildcard (* ? [..])."""
    if not any(c in name for c in "*?["):
        return [name]
    matches = fnmatch.filter(playlist_names(tv_libraries), name)
    if not matches:
        raise SystemExit(f"no playlist matches '{name}'. Found: "
                         f"{', '.join(playlist_names(tv_libraries)) or 'none'}")
    return matches


def read_playlist(name, tv_libraries):
    """Videos of one TA_NFOWriter playlist, in order.

    Returns [(host path, path as the containers see it)] and warnings. Entries
    are relative to the .m3u8's own folder; missing ones are skipped.
    """
    if os.sep in name or name.startswith("."):
        raise SystemExit(f"--playlist: not a playlist name: {name}")
    for _label, host, target in tv_libraries:
        m3u = os.path.join(host, "Playlists", name + ".m3u8")
        if not os.path.isfile(m3u):
            continue
        videos, warnings = [], []
        with open(m3u, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                path = os.path.normpath(os.path.join(os.path.dirname(m3u), line))
                rel = os.path.relpath(path, host)
                if rel.startswith(".."):
                    warnings.append(f"{name}: entry outside the library, skipped: {line}")
                elif not os.path.lexists(path):
                    # lexists, not exists: a view link may point at a path that only
                    # the containers have (/mnt/media/... inside, something else on
                    # this host). The link being there is what counts.
                    warnings.append(f"{name}: missing video, skipped: {line}")
                else:
                    videos.append((path, f"{target}/{rel.replace(os.sep, '/')}"))
        if not videos:
            warnings.append(f"{name}: no playable videos")
        return videos, warnings
    have = playlist_names(tv_libraries)
    raise SystemExit(f"playlist '{name}' not found in any --tv-library's Playlists folder "
                     f"(run ta_nfo.py --playlists). Found: {', '.join(have) or 'none'}")


def _is_video(rel):
    return os.path.splitext(rel)[1].lower() in VIDEO_EXT


def resolve_groups(groups, items, tv_libraries, genre_map):
    """Turn --playlist/--show/--video groups into imports.

    groups: [(kind, "NAME[=DEST]")]. A video can be claimed by any number of
    groups, so it can live in several places. Returns (commercials, routes,
    warnings):
      routes       video path without extension -> destinations (genres, and
                   COMMERCIALS), in order; the sidecars next to a video share
                   its key
      commercials  (link inside the farm, path as the containers see it)
    """
    by_path = {}                                    # host video path -> (item, rel)
    for it in items:
        for rel in it["files"]:
            if _is_video(rel):
                by_path[os.path.normpath(os.path.join(it["dir"], rel))] = (it, rel)
    routes, commercials, warnings = {}, [], []
    seen_commercials = set()

    def send(key, dest):
        if dest not in routes.setdefault(key, []):
            routes[key].append(dest)

    def claim(label, dest, paths, whole_item=None):
        genre = COMMERCIALS if dest == COMMERCIALS else normalise_genre(dest or label, genre_map)
        for path in paths:
            it, rel = by_path[path]
            send(os.path.splitext(path)[0], genre)
            if genre == COMMERCIALS and f"{it['target']}/{rel}" not in seen_commercials:
                # once per video, whichever groups name it: a second copy would
                # make FieldStation42 count it twice
                seen_commercials.add(f"{it['target']}/{rel}")
                commercials.append((f"{COMMERCIALS}/{sanitize(label)}/{os.path.basename(path)}",
                                    f"{it['target']}/{rel}"))
        if whole_item is not None:
            # the show's own NFOs go with it, so it arrives complete
            for rel in whole_item["files"]:
                send(os.path.splitext(os.path.normpath(os.path.join(whole_item["dir"], rel)))[0], genre)

    for kind, spec in groups:
        pattern, _, dest = spec.partition("=")
        if kind == "playlist":
            for name in expand_playlist(pattern, tv_libraries):
                videos, warns = read_playlist(name, tv_libraries)
                warnings += warns
                known = [p for p, _ in videos if p in by_path]
                warnings += [f"{name}: not in any library being read, skipped: {os.path.basename(p)}"
                             for p, _ in videos if p not in by_path]
                claim(name, dest, known)
        elif kind == "show":
            hit = [it for it in items if fnmatch.fnmatchcase(it["item"].casefold(), pattern.casefold())]
            if not hit:
                names = sorted({it["item"] for it in items})
                raise SystemExit(f"no show matches '{pattern}'. Found: "
                                 f"{', '.join(names[:20])}{' ...' if len(names) > 20 else ''}")
            for it in hit:
                paths = [os.path.normpath(os.path.join(it["dir"], rel)) for rel in it["files"] if _is_video(rel)]
                claim(it["item"], dest, paths, whole_item=it)
        else:                                       # video
            if not dest:
                raise SystemExit(f"--video '{spec}': a DEST is required (--video 'PATTERN=DEST')")
            hit = [path for path, (it, rel) in by_path.items()
                   if fnmatch.fnmatchcase(f"{it['item']}/{rel}".casefold(), pattern.casefold())]
            if not hit:
                raise SystemExit(f"--video '{pattern}' matches no video. The pattern is matched against "
                                 f"'<show>/<path in show>', e.g. 'News/Season 2026/*'")
            claim("videos", dest, sorted(hit))
    return sorted(set(commercials)), routes, warnings


def plan_farm(items, include_unclassified, routes=None):
    """genre -> sorted list of (link path inside the farm, archive path).

    Every video goes under its show's genre. routes (see resolve_groups) adds
    it to further places as well, so it can live in several tag folders. The
    one exception is COMMERCIALS: those videos are not scheduled as programmes,
    so they get no genre placement. The sidecars next to a video follow it.
    """
    routes = routes or {}
    plan, skipped = {}, set()

    def place(genre, it, rel):
        plan.setdefault(genre, []).append((f"{genre}/{it['library']}/{it['item']}/{rel}",
                                           f"{it['target']}/{rel}"))

    for it in items:
        for rel in it["files"]:
            dests = []
            if routes:
                dests = routes.get(os.path.splitext(os.path.normpath(os.path.join(it["dir"], rel)))[0], [])
            extra = [g for g in dests if g != COMMERCIALS]
            for g in extra:
                place(g, it, rel)
            if COMMERCIALS in dests:
                continue                    # played as commercials, not scheduled
            if it.get("route_only"):
                continue                    # a converted library: not listed by genre or channel, only by what you chose
            genre = it["genre"]
            if genre is None:
                if include_unclassified:
                    genre = UNCLASSIFIED
                else:
                    if not extra:
                        skipped.add(f"{it['library']}/{it['item']}")
                    continue
            place(genre, it, rel)
    # A genre left with only NFOs (its videos were commercials) has nothing to
    # play, and a station with nothing to play is worse than no station.
    for genre in [g for g, entries in plan.items()
                  if not any(os.path.splitext(link)[1].lower() in VIDEO_EXT for link, _ in entries)]:
        del plan[genre]
    for genre in plan:
        plan[genre] = sorted(set(plan[genre]))
    return plan, sorted(skipped)


def assign_numbers(genres, state, start, reserved=frozenset()):
    numbers = {g: state["genres"][g]["number"] for g in genres if g in state["genres"]}
    used = set(numbers.values()) | set(reserved)
    next_free = start
    for g in sorted(genres):
        if g in numbers:
            continue
        while next_free in used:
            next_free += 1
        numbers[g] = next_free
        used.add(next_free)
    return numbers


def station_conf(genre, number, bump_dir, commercial_dir=None):
    days = {str(h): {"tags": genre} for h in range(24)}
    conf = {
        "network_name": genre,
        "channel_number": number,
        "network_type": "standard",
        "schedule_increment": 60,
        "break_strategy": "end",
        "commercial_free": True,
        "content_dir": f"catalog/{FARM}",
        "bump_dir": bump_dir,
        "standby_image": "runtime/standby.png",
        "be_right_back_media": "runtime/brb.png",
        "day_templates": {"daily": days},
        **{d: "daily" for d in ("monday", "tuesday", "wednesday", "thursday",
                                "friday", "saturday", "sunday")},
    }
    if commercial_dir:
        conf["commercial_dir"] = commercial_dir
        conf["commercial_free"] = False
    return conf


try:
    import schedlock
    import webget
except ImportError:         # run from the repo: the module sits in the dashboard's folder
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs42-tuner"))
    import schedlock
    import webget


class Console:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with webget.open_url(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")

    def stations(self):
        """Every station's config, by name."""
        return {s["station_conf"]["network_name"]: s["station_conf"]
                for s in self._call("GET", "/stations")["stations"]}

    def station_channels(self):
        return {name: conf["channel_number"] for name, conf in self.stations().items()}

    def ensure_station(self, conf, existing):
        """Create the station if it is missing. Never rewrite one that exists.

        An existing station is yours to edit in FieldStation42. The one thing
        filled in is a commercial_dir it doesn't have yet, when commercials
        are in use. Returns what was done.
        """
        name = conf["network_name"]
        if name not in existing:
            self.save_station(conf)
            return "created"
        current = existing[name]
        if conf.get("commercial_dir") and not current.get("commercial_dir"):
            merged = {k: v for k, v in current.items() if not k.startswith("_")}
            merged["commercial_dir"] = conf["commercial_dir"]
            merged["commercial_free"] = False
            self.save_station(merged)
            return "commercials added"
        return "kept"

    def save_station(self, conf):
        name = urllib.parse.quote(conf["network_name"], safe="")
        if conf["network_name"] in self.station_channels():
            return self._call("PUT", f"/stations/{name}", {"station_conf": conf})
        return self._call("POST", "/stations", {"station_conf": conf})

    def rebuild_catalog(self, name, timeout=900):
        with schedlock.held():          # one build at a time across the container (see schedlock.py)
            task = self._call("POST", f"/build/catalog/{urllib.parse.quote(name, safe='')}")["task_id"]
            deadline = time.time() + timeout
            while time.time() < deadline:
                st = self._call("GET", f"/build/catalog/status/{task}")
                if st.get("status") == "done":
                    return
                if st.get("status") == "error":
                    raise RuntimeError(f"catalog rebuild failed for {name}: {st.get('log', '')[-300:]}")
                time.sleep(3)
        raise RuntimeError(f"catalog rebuild for {name} timed out")

    def add_day(self, name, timeout=900):
        """Add a day of schedule THROUGH the console. It reloads its own view afterwards, which a separate
        `station_42.py -d` process cannot make it do: the console kept showing the station as having no schedule."""
        with schedlock.held():          # one build at a time across the container (see schedlock.py)
            task = self._call("POST", f"/build/schedule/add_time/day/{urllib.parse.quote(name, safe='')}")["task_id"]
            deadline = time.time() + timeout
            while time.time() < deadline:
                st = self._call("GET", f"/build/schedule/add_time/status/{task}")
                if st.get("status") == "done":
                    return
                if st.get("status") == "error":
                    raise RuntimeError(f"adding a day to {name} failed: {st.get('log', '')[-300:]}")
                time.sleep(2)
        raise RuntimeError(f"adding a day to {name} timed out")


# Inside the Station Wrapper 43 container (the image sets FS42_IN_CONTAINER=1) there is no host: the paths in the
# symlinks, the catalog and the config are all the container's own, and there is no docker to call.
CONTAINER_CATALOG = "/app/catalog"
CONTAINER_CONFIG = "/config"
CONTAINER_MANIFEST = f"{CONTAINER_CONFIG}/groups.toml"
CONTAINER_TUNER_CONFIG = f"{CONTAINER_CONFIG}/tuner/config.json"


def in_container():
    return os.environ.get("FS42_IN_CONTAINER") == "1"


def adopt_owner(top, reference):
    """Give everything under `top` the owner of `reference` (links and folders, never following links).

    The container runs as root, but the catalog belongs to you, and you move links around in it from
    the host. What the generator creates in here must not end up root's. A no-op unless we are root.
    """
    if os.geteuid() != 0 or not os.path.isdir(top):
        return 0
    ref = os.stat(reference)
    want, changed = (ref.st_uid, ref.st_gid), 0
    for root, dirs, files in os.walk(top):
        for name in (*dirs, *files):
            path = os.path.join(root, name)
            st = os.lstat(path)
            if (st.st_uid, st.st_gid) != want:
                os.lchown(path, *want)
                changed += 1
    st = os.lstat(top)
    if (st.st_uid, st.st_gid) != want:
        os.lchown(top, *want)
        changed += 1
    return changed


def add_first_day(container, name, console=None):
    """Add a day of schedule to a new station. container=None: we are inside it, so ask the console itself
    (it then shows the schedule in its own pages); otherwise run station_42.py in the named container."""
    if container is None:
        (console or Console("http://127.0.0.1:4242")).add_day(name)
        return
    cmd = ["docker", "exec", container, "python3", "station_42.py", "-d", name]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0 or "added a day" not in out.stdout:
        raise RuntimeError(f"adding a day to {name} failed: {out.stdout[-300:]} {out.stderr[-300:]}")


# Filesystems where symlinks are unreliable, or resolve differently for the
# server and for us: creation may be refused, and an absolute target may be
# followed on the server's side instead of this machine's.
NETWORK_FS = {"cifs": "SMB", "smb3": "SMB", "smbfs": "SMB", "nfs": "NFS", "nfs4": "NFS",
              "fuse.sshfs": "SSHFS", "9p": "9p", "fuse.rclone": "rclone", "vboxsf": "VirtualBox share"}


def filesystem_of(path, mountinfo=None):
    """The filesystem type holding path (cifs, ext4, ...), or None if unknown.

    mountinfo is /proc/self/mountinfo text; passed in by tests.
    """
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


def probe_symlinks(folder):
    """Make a real symlink in folder and follow it. Returns None if it works, else why not.

    The link points at a file OUTSIDE the folder by absolute path, which is
    what every catalog link does, and the case that goes wrong on SMB shares.
    The probe files are removed.
    """
    outside = inside = None
    try:
        outside = tempfile.mkdtemp(prefix="symlink-probe-target-")
        inside = tempfile.mkdtemp(prefix=".symlink-probe-", dir=folder)
        target = os.path.join(outside, "target.txt")
        with open(target, "w") as f:
            f.write("ok")
        link = os.path.join(inside, "link")
        os.symlink(target, link)
        if not os.path.islink(link):
            return "the link was created but does not read back as a symlink"
        if os.readlink(link) != target:
            return f"the link reads back as {os.readlink(link)!r}, not the path it was given"
        with open(link) as f:
            if f.read() != "ok":
                return "the link does not lead to the file it points at"
        return None
    except OSError as e:
        return f"{e.strerror or e}"
    finally:
        for d in (inside, outside):
            if d:
                shutil.rmtree(d, ignore_errors=True)


def symlink_check(folder, label):
    """Can folder hold the catalog's symlinks? Returns (level, message).

    level: "ok", "note" (works here, but on a filesystem that is risky), "fail",
    or "skip" (cannot be tested, e.g. read-only).
    """
    probe_dir = folder
    while probe_dir and not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    fs = filesystem_of(probe_dir)
    kind = NETWORK_FS.get(fs or "")
    if not os.access(probe_dir, os.W_OK):
        return ("note" if kind else "skip",
                f"{label} ({folder}) is not writable here, so symlinks could not be tested"
                + (f"; it is on {kind} ({fs})" if kind else ""))
    why = probe_symlinks(probe_dir)
    if why:
        where = f" on {kind} ({fs})" if kind else (f" on {fs}" if fs else "")
        return ("fail", f"{label} ({folder}) cannot hold working symlinks{where}: {why}.\n"
                f"  Symlinks are what the catalog is made of. Put it on local disk (FS42_CATALOG_ROOT, or "
                f"--catalog), and keep only your media on the share. "
                + ("If the share must hold it, mount it with the cifs option mfsymlinks and test again."
                   if kind == "SMB" else ""))
    if kind:
        return ("note", f"{label} ({folder}) is on {kind} ({fs}). Symlinks worked here, but they can resolve "
                f"differently inside a container or after a remount; local disk is safer for the catalog.")
    return ("ok", f"{label} ({folder}): symlinks work" + (f" ({fs})" if fs else ""))


def restart_tuner(container):
    """Ask the container's supervisor to restart the tuner, so it re-reads its channels.

    SIGUSR1 goes to the supervisor (tini passes it on) and restarts the tuner only; the
    console is untouched. Returns True if the signal was sent.
    """
    if container is None:           # inside: PID 1 is tini, which hands the signal to the supervisor
        try:
            os.kill(1, signal.SIGUSR1)
            return True
        except OSError:
            return False
    out = subprocess.run(["docker", "kill", "--signal=USR1", container], capture_output=True, text=True)
    return out.returncode == 0


def farm_target(target, target_root):
    """Absolute targets come from --tv-library; the rest hang off target_root."""
    return target if os.path.isabs(target) else f"{target_root.rstrip('/')}/{target}"


def import_links(catalog, plan, target_root, commercials=(), dry=False, extra=()):
    """Add the symlinks that are missing from the farm. Never remove or change one.

    The farm is a plain FieldStation42 content folder: tag folders of links you
    can add to, remove or re-point by hand, and a later run leaves that alone.
    extra is more (link, target) pairs with absolute targets (the bump clips).
    Returns ({tag folder: links added}, notes about things left as they were).
    """
    farm = os.path.join(catalog, FARM)
    farm_real = os.path.realpath(farm)
    added, notes = {}, []

    def add(link, target):
        dst = os.path.join(farm, link)
        parent = os.path.dirname(dst)
        if os.path.lexists(parent):
            # A folder inside the farm that is itself a link (one you made, pointing somewhere else)
            # would make this write INTO that other place. Never do that.
            real = os.path.realpath(parent)
            if real != farm_real and not real.startswith(farm_real + os.sep):
                notes.append(f"left alone, it is inside a linked folder that points elsewhere: {link}")
                return
        if os.path.islink(dst):
            if os.readlink(dst) != target:
                notes.append(f"kept existing link that points elsewhere: {link}")
            return
        if os.path.lexists(dst):
            notes.append(f"kept existing file: {link}")
            return
        if not dry:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.symlink(target, dst)
            except OSError as e:
                notes.append(f"could not link {link}: {e}")
                return
        tag = link.split("/")[0]
        added[tag] = added.get(tag, 0) + 1

    for entries in plan.values():
        for link, target in entries:
            add(link, farm_target(target, target_root))
    for link, target in (*commercials, *extra):
        add(link, farm_target(target, target_root))
    return added, notes


def bump_entries(media_root, bump_target):
    """(link inside the farm, path as the container sees it) for each clip in the bump folder.

    The bump folder in the farm is a real folder. Clips you put in it yourself are used as they
    are. If the media also has a bump folder (optional), its clips are linked in too, laid out as
    they are there (FieldStation42 reads sub-folders of it for pre/post bumps).
    """
    src = os.path.join(media_root, BUMP)
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                continue
            rel = os.path.relpath(os.path.join(root, name), src).replace(os.sep, "/")
            out.append((f"{BUMP}/{rel}", f"{bump_target}/{rel}"))
    return out


def auto_tv_libraries(catalog):
    """The converted TubeArchivist libraries inside the catalog (catalog/tanfo/<name>-nfo), one per library, each
    that holds any channel. [] when there are none."""
    root = os.path.join(catalog, "tanfo")
    found = []
    try:
        for name in sorted(os.listdir(root)):
            view = os.path.join(root, name)
            if name.endswith("-nfo") and os.path.isdir(view) and any(
                    os.path.isfile(os.path.join(view, d, "tvshow.nfo")) for d in os.listdir(view)):
                found.append(view)
    except OSError:
        pass
    return found


def show_groups(library_dir):
    """A group per show in a TV library, so each becomes a tag folder named after it (a YouTube channel)."""
    try:
        names = sorted(d for d in os.listdir(library_dir)
                       if os.path.isfile(os.path.join(library_dir, d, "tvshow.nfo")))
    except OSError:
        return []
    return [("show", glob.escape(n)) for n in names]


def links_only(args, plan, commercials, bump_target, mapping):
    """Get the files ready and stop: add the missing links to catalog/by-genre. Add-only, no stations."""
    bump_note, bump_blocked = retire_bump_link(args.catalog, bump_target, dry=args.dry_run)
    if bump_note:
        print(f"note: {bump_note}")
    clips = [] if bump_blocked else bump_entries(args.media_root, bump_target)
    added, notes = import_links(args.catalog, plan, args.target_root, commercials, dry=args.dry_run, extra=clips)
    for n in notes:
        print(f"note: {n}")
    dead = find_dead_links(args.catalog, mapping) if args.prune else []
    if dead and not args.dry_run:
        prune_links(args.catalog, dead)
    if container_mode_owner(args) and not args.dry_run:
        adopt_owner(os.path.join(args.catalog, FARM), args.catalog)
    total = sum(added.values())
    verb = "would add" if args.dry_run else "added"
    print(f"farm: {verb} {total} links in {len([t for t, n in added.items() if n])} tag folders "
          f"({len(plan)} genres/channels planned)" + (f", {len(dead)} dead links "
          f"{'would be ' if args.dry_run else ''}removed" if dead else ""))
    if count_bump_clips(args.catalog) + len(clips) == 0:
        print(f"note: no channel mid cards in {os.path.join(args.catalog, FARM, BUMP)}; FieldStation42 wants some for every station")
    return 0


def container_mode_owner(args):
    return in_container()


def count_bump_clips(catalog):
    """Video files in the catalog's bump folder (links count; a link to a folder is followed)."""
    n = 0
    for _root, _dirs, files in os.walk(os.path.join(catalog, FARM, BUMP), followlinks=True):
        n += sum(1 for f in files if not f.startswith(".") and os.path.splitext(f)[1].lower() in VIDEO_EXT)
    return n


def retire_bump_link(catalog, bump_target, dry=False):
    """An earlier version linked the WHOLE bump folder into the farm. Make room for a real folder.

    Returns (note or None, blocked). Only a link to exactly the bump folder is replaced, which is what
    that version made. A link you made to somewhere else is left alone, and then nothing is added
    under it: that would write into the other place.
    """
    link = os.path.join(catalog, FARM, BUMP)
    if not os.path.islink(link):
        return None, False
    if os.readlink(link) == bump_target:
        if not dry:
            os.remove(link)
        return "replaced the old whole-folder bump link with a real folder of linked clips", False
    return f"kept {link}: it is a link to {os.readlink(link)}, so no channel mid cards were added under it", True


def find_duplicate_links(catalog):
    """Videos linked more than once inside one tag folder.

    FieldStation42 schedules every file it finds, so the same video under two
    names in one tag airs twice as often. Linking a video into two different
    tags is the point of the farm and is not reported. Returns
    {tag: [[link, link, ...], ...]}, judged on the file each link finally
    resolves to (through a TA_NFOWriter view, for example).
    """
    farm = os.path.join(catalog, FARM)
    found = {}
    if not os.path.isdir(farm):
        return found
    for tag in sorted(os.listdir(farm)):
        tag_dir = os.path.join(farm, tag)
        if tag == BUMP or tag.startswith(".") or not os.path.isdir(tag_dir) or os.path.islink(tag_dir):
            continue
        by_file = {}
        for root, _dirs, files in os.walk(tag_dir):
            for name in files:
                if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                    continue
                path = os.path.join(root, name)
                # a link whose target can't be followed here is compared by what it says
                real = os.path.realpath(path) if os.path.exists(path) else os.readlink(path) \
                    if os.path.islink(path) else path
                by_file.setdefault(real, []).append(os.path.relpath(path, farm))
        groups = [sorted(v) for v in by_file.values() if len(v) > 1]
        if groups:
            found[tag] = sorted(groups)
    return found


def to_host(target, mapping):
    """Host path for a link target, or None when it isn't one of ours.

    mapping: [(container path, host path)]. The longest prefix wins, so a
    library mounted inside the media root maps by its own entry.
    """
    for container, host in sorted(mapping, key=lambda m: -len(m[0])):
        container = container.rstrip("/")
        if target == container or target.startswith(container + "/"):
            return host.rstrip("/") + target[len(container):]
    return None


def find_dead_links(catalog, mapping):
    """Farm links this generator could have made whose source video is gone.

    A link pointing anywhere else is yours and is never listed. "Gone" means the
    first place the link leads is missing; see the note below on why it is one hop.
    """
    farm = os.path.join(catalog, FARM)
    dead = []
    for root, dirs, files in os.walk(farm):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                continue
            host = to_host(os.readlink(path), mapping)
            # One hop, and lexists, not exists: the host path is often itself a link
            # (a TubeArchivist view link) whose own target is a container path that
            # does not exist here. Following it would call healthy links dead.
            # The view's own cleanup removes a link when its video goes; a farm
            # link to that view link is then caught on the next prune.
            if host is not None and not os.path.lexists(host):
                dead.append(path)
    return sorted(dead)


def prune_links(catalog, dead):
    """Remove the given links, then any folder that leaves empty. Returns {tag: removed}."""
    farm = os.path.join(catalog, FARM)
    removed = {}
    for path in dead:
        os.remove(path)
        tag = os.path.relpath(path, farm).split(os.sep)[0]
        removed[tag] = removed.get(tag, 0) + 1
        # only folders these removals emptied; an empty folder you made stays
        parent = os.path.dirname(path)
        while parent != farm and not os.listdir(parent):
            os.rmdir(parent)
            parent = os.path.dirname(parent)
    return removed


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"genres": {}}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def write_tuner_channels(path, numbers, ours_before):
    """Replace only the channels this script manages; hand-made ones stay."""
    with open(path) as f:
        conf = json.load(f)
    channels = {n: name for n, name in conf.get("channels", {}).items() if name not in ours_before}
    for genre, number in numbers.items():
        channels[str(number)] = genre
    conf["channels"] = dict(sorted(channels.items(), key=lambda kv: float(kv[0])))
    with open(path, "w") as f:
        json.dump(conf, f, indent=2)
        f.write("\n")


def load_genre_map(path):
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


# Settings a manifest may hold. Everything the command line takes about
# libraries and groups is here, so a setup can live in one file beside the
# catalog instead of in shell history. Paths that name a place on this host are
# relative to the manifest; target_root is a container path and is not.
MANIFEST_SCALARS = {
    "media_root": str, "target_root": str, "catalog": str, "tuner_config": str, "console": str,
    "fs42_container": str, "start_channel": int, "include_unclassified": bool, "no_default_libraries": bool,
}
MANIFEST_HOST_PATHS = {"media_root", "catalog", "tuner_config"}
GROUP_KINDS = ("playlist", "show", "video")


def load_manifest(path):
    """Read a groups.toml. Returns {"defaults": argparse defaults, "genre_map": dict, "dir": manifest folder}."""
    try:
        import tomllib
    except ImportError:
        raise SystemExit("--manifest needs Python 3.11 or newer (it reads TOML with tomllib)")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise SystemExit(f"cannot read the manifest: {e}")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path}: not valid TOML: {e}")
    base = os.path.dirname(os.path.abspath(path))

    def host(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    allowed = set(MANIFEST_SCALARS) | {"tv_libraries", "genre_map", "group"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SystemExit(f"{path}: unknown setting(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")
    inside = in_container()
    defaults = {}
    for key, kind in MANIFEST_SCALARS.items():
        if key in data:
            if type(data[key]) is not kind:
                raise SystemExit(f"{path}: {key} must be {'a number' if kind is int else 'true/false' if kind is bool else 'text'}")
            if inside and key in MANIFEST_HOST_PATHS:
                continue            # a path on the host (written by a host-side init); here the container's own are used
            defaults[key] = host(data[key]) if key in MANIFEST_HOST_PATHS else data[key]
    libs = data.get("tv_libraries", [])
    if not isinstance(libs, list) or not all(isinstance(x, str) for x in libs):
        raise SystemExit(f'{path}: tv_libraries must be a list of "HOST" or "HOST=CONTAINER" text')
    if inside:      # HOST=CONTAINER: only the container side exists here
        defaults["tv_library"] = [spec.partition("=")[2] if "=" in spec else host(spec) for spec in libs]
    else:
        defaults["tv_library"] = [host(spec.partition("=")[0]) + (("=" + spec.partition("=")[2]) if "=" in spec else "")
                                  for spec in libs]
    genre_map = data.get("genre_map", {})
    if not isinstance(genre_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in genre_map.items()):
        raise SystemExit(f"{path}: genre_map must be a table of text = text")
    groups = []
    for n, entry in enumerate(data.get("group", []), 1):
        keys = set(entry) if isinstance(entry, dict) else set()
        kinds = [k for k in GROUP_KINDS if k in keys]
        if not isinstance(entry, dict) or len(kinds) != 1 or keys - set(GROUP_KINDS) - {"to"} \
                or not all(isinstance(v, str) for v in entry.values()):
            raise SystemExit(f"{path}: [[group]] #{n} needs exactly one of playlist / show / video, "
                             f"and may add `to` (all text)")
        kind = kinds[0]
        if kind == "video" and not entry.get("to"):
            raise SystemExit(f"{path}: [[group]] #{n}: a video group needs `to`")
        groups.append((kind, entry[kind] + (f"={entry['to']}" if entry.get("to") else "")))
    defaults["groups"] = groups
    return {"defaults": defaults, "genre_map": genre_map, "dir": base}


class GroupAction(argparse.Action):
    """Collect --playlist/--show/--video into one list, in command-line order."""
    def __call__(self, parser, namespace, values, option_string=None):
        groups = list(getattr(namespace, self.dest) or [])
        groups.append((self.const, values))
        setattr(namespace, self.dest, groups)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", metavar="groups.toml",
                    help="a TOML file holding these settings and your groups, so a setup lives in one file. "
                         "Flags given here override it; --tv-library and group flags add to it")
    ap.add_argument("--media-root", default="/mnt/media", help="archive root on this host")
    ap.add_argument("--target-root", default="/mnt/media",
                    help="the same archive as the containers see it; symlinks point here")
    ap.add_argument("--tv-library", action="append", default=[], metavar="HOST[=CONTAINER]",
                    help="a folder that is itself a TV library (each subfolder with a tvshow.nfo is a "
                         "show), e.g. the TA_NFOWriter view. Repeatable. CONTAINER is the path the "
                         "containers see it at; it defaults to HOST (same-path mount)")
    ap.add_argument("--playlist", action=GroupAction, const="playlist", dest="groups", metavar="NAME[=DEST]",
                    help="group: a TA_NFOWriter playlist (<tv-library>/Playlists/NAME.m3u8). Needs --tv-library")
    ap.add_argument("--show", action=GroupAction, const="show", dest="groups", metavar="NAME[=DEST]",
                    help="group: a whole show, series or TubeArchivist channel, by folder name")
    ap.add_argument("--video", action=GroupAction, const="video", dest="groups", metavar="PATTERN=DEST",
                    help="group: videos matching PATTERN against '<show>/<path in show>'")
    ap.add_argument("--no-default-libraries", action="store_true",
                    help="do not look for tvseries/, anime/ and movies/ under --media-root")
    ap.add_argument("--catalog", help="host path of the FieldStation42 catalog volume "
                                      "(default with a manifest: a catalog folder beside it)")
    ap.add_argument("--tuner-config", dest="tuner_config",
                    help="the tuner's config.json to update (default: config/tuner/config.json beside the catalog, "
                         "which the container creates on first start)")
    ap.add_argument("--console", default="http://127.0.0.1:4242", help="FieldStation42 console URL")
    ap.add_argument("--fs42-container", dest="fs42_container", default="station-wrapper-43",
                    help="the container (docker exec adds the first day; docker kill -s USR1 restarts its tuner)")
    ap.add_argument("--no-restart-tuner", action="store_true",
                    help="don't signal the container to restart its tuner after writing channels")
    ap.add_argument("--start-channel", type=int, default=801)
    ap.add_argument("--genre-map", help="JSON file mapping raw genre names to channel genres")
    ap.add_argument("--include-unclassified", action="store_true",
                    help="put items with no genre on an Unclassified channel")
    ap.add_argument("--prune", action="store_true",
                    help="also remove farm links whose source video is gone. Never touches a link that "
                         "points anywhere this script doesn't manage. Without it, nothing is ever removed")
    ap.add_argument("--links-only", dest="links_only", action="store_true",
                    help="only get the files ready: fill catalog/by-genre with tag folders of links (genres from the "
                         "NFOs, one folder per TubeArchivist channel, bump) and stop. No stations, no channel numbers, "
                         "no schedules, no console: those are FieldStation42's own console's job. This is what the "
                         "container's `farm` job runs on its own")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and write nothing")
    return ap


def parse_args(argv=None):
    """Command line plus an optional manifest. Flags beat the manifest."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--manifest")
    manifest_path = pre.parse_known_args(argv)[0].manifest
    if not manifest_path and in_container() and os.path.isfile(CONTAINER_MANIFEST):
        manifest_path = CONTAINER_MANIFEST      # inside the container the manifest is in the config volume
    manifest = load_manifest(manifest_path) if manifest_path else None
    ap = build_parser()
    if manifest:
        ap.set_defaults(**manifest["defaults"])
    args = ap.parse_args(argv)
    if in_container():
        args.catalog = args.catalog or CONTAINER_CATALOG
        args.tuner_config = args.tuner_config or CONTAINER_TUNER_CONFIG
    if not args.catalog and manifest:
        # groups.toml lives in the config folder, and the catalog is its sibling: <root>/config/groups.toml
        # and <root>/catalog. A catalog folder right beside the manifest works too.
        here = manifest["dir"]
        for candidate in (os.path.join(os.path.dirname(here), "catalog") if os.path.basename(here) == "config" else None,
                          os.path.join(here, "catalog")):
            if candidate and os.path.isdir(candidate):
                args.catalog = candidate
                break
    if not args.catalog:
        ap.error("--catalog is required (or `catalog` in the manifest, or put groups.toml in the config folder "
                 "beside the catalog folder)")
    if not args.tuner_config and not args.links_only:
        # The container creates its tuner config in config/tuner/, beside the catalog folder, the
        # first time it starts. (An earlier layout kept it in tuner/ beside the catalog.)
        parent = os.path.dirname(os.path.abspath(args.catalog))
        candidates = [os.path.join(parent, "config", "tuner", "config.json"),
                      os.path.join(parent, "tuner", "config.json")]
        found = [c for c in candidates if os.path.isfile(c)]
        if not found:
            ap.error(f"no tuner config at {candidates[0]}. Start the container once (it creates it), or pass "
                     f"--tuner-config (or `tuner_config` in the manifest)")
        args.tuner_config = found[0]
    args.manifest_genre_map = manifest["genre_map"] if manifest else {}
    return args


def main(argv=None):
    args = parse_args(argv)
    # None means "we are inside the container": no docker exec / docker kill, do it here.
    container = None if in_container() else args.fs42_container

    level, message = symlink_check(args.catalog, "catalog")
    if level == "fail":
        raise SystemExit(message)
    if level == "note":
        print(f"note: {message}")
    genre_map = {**args.manifest_genre_map, **load_genre_map(args.genre_map)}
    taken_labels = {BUMP, *TV_LIBRARIES, MOVIE_LIBRARY}
    if args.links_only:
        # On its own, with no groups file needed: shows with no genre are kept (under Unclassified) so nothing is
        # silently missing from FieldStation42. The converted TubeArchivist libraries (catalog/tanfo/<name>-nfo) are
        # NOT made into lists here: a channel or a playlist is chosen as a source where it is used (the calendar's
        # browser), or added to a list yourself on the Files page.
        views = [v for v in auto_tv_libraries(args.catalog)
                 if not any(os.path.abspath(s.partition("=")[0]) == os.path.abspath(v) for s in args.tv_library)]
        playlist_specs = [view if in_container() else f"{view}={CONTAINER_CATALOG}/tanfo/{os.path.basename(view)}"
                          for view in views]
        args.include_unclassified = True
    else:
        playlist_specs = []
    tv_libraries = []
    for spec in args.tv_library:
        lib = parse_tv_library(spec, taken_labels)
        taken_labels.add(lib[0])
        tv_libraries.append(lib)
    playlist_libs = []                       # read for their playlists only: their shows are not listed or tagged
    for spec in playlist_specs:
        lib = parse_tv_library(spec, taken_labels)
        taken_labels.add(lib[0])
        playlist_libs.append(lib)
    if args.no_default_libraries and not tv_libraries:
        raise SystemExit("--no-default-libraries needs at least one --tv-library")
    groups = args.groups or []
    if any(kind == "playlist" for kind, _ in groups) and not tv_libraries and not playlist_libs:
        raise SystemExit("--playlist needs a --tv-library that holds the playlist")
    items, warnings = discover(args.media_root, genre_map, [*tv_libraries, *playlist_libs],
                               default_libraries=not args.no_default_libraries)
    route_only = {lib[0] for lib in playlist_libs}
    for it in items:
        if it["library"] in route_only:
            it["route_only"] = True         # placed only where a chosen playlist sends it
    commercials, routes, group_warnings = resolve_groups(groups, items, [*tv_libraries, *playlist_libs], genre_map)
    warnings += group_warnings
    plan, skipped = plan_farm(items, args.include_unclassified, routes)
    for w in warnings:
        if args.links_only and w.startswith("library not found:"):
            continue        # the farm runs every few minutes; a media folder you don't have is not news each time
        print(f"warning: {w}")

    bump_target = f"{args.target_root.rstrip('/')}/{BUMP}"
    if args.links_only:
        mapping = [(tgt, host) for _label, host, tgt in [*tv_libraries, *playlist_libs]]
        if not args.no_default_libraries:
            mapping.append((args.target_root, args.media_root))
        return links_only(args, plan, commercials, bump_target, mapping)

    state_path = os.path.join(args.catalog, STATE_NAME)
    state = load_state(state_path)
    ours_before = set(state["genres"])
    console = Console(args.console)
    try:
        existing = console.stations()
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"cannot reach the FieldStation42 console at {args.console}: {e}")
    reserved = {conf["channel_number"] for name, conf in existing.items() if name not in ours_before}
    numbers = assign_numbers(plan.keys(), state, args.start_channel, reserved)
    if commercials and any(g.casefold() == COMMERCIALS for g in plan):
        raise SystemExit(f"a genre is named '{COMMERCIALS}', which is the commercials folder; "
                         f"rename it with --genre-map")
    for g in plan:
        if g not in state["genres"] and g in existing:
            raise SystemExit(f"a station named '{g}' already exists and this script didn't create it; "
                             f"rename it or pick another genre map entry")

    # What is in the farm already stays. Only missing links are added.
    bump_note, bump_blocked = retire_bump_link(args.catalog, bump_target, dry=True)
    clips = [] if bump_blocked else bump_entries(args.media_root, bump_target)
    added, notes = import_links(args.catalog, plan, args.target_root, commercials, dry=True, extra=clips)
    bump_folder = os.path.join(args.catalog, FARM, BUMP)
    if count_bump_clips(args.catalog) + len(clips) == 0:
        if not args.dry_run:
            os.makedirs(bump_folder, exist_ok=True)      # so there is a folder to drop clips into
        raise SystemExit(f"no channel mid cards (the short clips in the bump folder). FieldStation42 needs some for every station. Put a few short "
                         f"video files in {bump_folder} (the catalog volume; no media folder or extra "
                         f"volume is needed), then run this again.")
    mapping = [(tgt, host) for _label, host, tgt in tv_libraries]
    if not args.no_default_libraries:
        mapping.append((args.target_root, args.media_root))
    dead = find_dead_links(args.catalog, mapping) if args.prune else []
    pruned = {}
    for path in dead:
        tag = os.path.relpath(path, os.path.join(args.catalog, FARM)).split(os.sep)[0]
        pruned[tag] = pruned.get(tag, 0) + 1
    # New commercials mean every station's catalog needs another look.
    # New commercials or bump clips mean every station's catalog needs another look.
    commercials_changed = bool(added.get(COMMERCIALS) or pruned.get(COMMERCIALS)
                               or added.get(BUMP) or pruned.get(BUMP))
    changed = sorted(g for g in plan if g not in state["genres"] or added.get(g) or pruned.get(g)
                     or commercials_changed)
    removed = sorted(g for g in state["genres"] if g not in plan)

    print(f"{len(items)} items, {len(plan)} genres, {sum(len(v) for v in plan.values())} linked files")
    if routes:
        print(f"{len(routes)} videos also placed by groups")
    if commercials:
        print(f"{len(commercials)} commercials (left off the genre channels)")
    for g in sorted(plan, key=lambda g: numbers[g]):
        n = len({"/".join(link.split("/")[1:3]) for link, _ in plan[g]})
        if g not in state["genres"]:
            status = "new"
        elif g in changed:
            status = f"{added.get(g, 0)} links added" + (f", {pruned[g]} removed" if pruned.get(g) else "")
        else:
            status = "unchanged"
        print(f"  {numbers[g]:>5}  {g:<24} {n:>4} items  ({status})")
    if skipped:
        print(f"{len(skipped)} items with no genre, skipped (use --include-unclassified to keep them):")
        for sk in skipped:
            print(f"  - {sk}")
    if removed:
        print(f"genres no longer in the archive (stations left in place, remove by hand): {', '.join(removed)}")
    for note in notes[:10]:
        print(f"note: {note}")
    if len(notes) > 10:
        print(f"note: ... and {len(notes) - 10} more left as they were")
    if args.prune:
        print(f"{len(dead)} links whose source video is gone" + ("" if args.dry_run else ", removing"))

    def report_duplicates():
        dups = find_duplicate_links(args.catalog)
        shown = 0
        for tag, groups in dups.items():
            for group in groups:
                if shown < 10:
                    print(f"note: {tag}: the same video is linked {len(group)} times, so FieldStation42 "
                          f"would schedule it {len(group)} times: {', '.join(group)}")
                shown += 1
        if shown > 10:
            print(f"note: ... and {shown - 10} more videos linked more than once")

    if args.dry_run:
        report_duplicates()
        print("dry run: nothing written")
        return 0

    if bump_note:
        print(f"note: {bump_note}")
    retire_bump_link(args.catalog, bump_target)
    import_links(args.catalog, plan, args.target_root, commercials, extra=clips)
    if dead:
        prune_links(args.catalog, dead)
    if container is None:           # inside: we are root; the catalog is yours, so the new links should be too
        adopt_owner(os.path.join(args.catalog, FARM), args.catalog)
    report_duplicates()

    for g in changed:
        conf = station_conf(g, numbers[g], BUMP, COMMERCIALS if commercials else None)
        result = console.ensure_station(conf, existing)
        console.rebuild_catalog(g)
        if container is None or container:      # "" on the host means: don't touch docker
            add_first_day(container, g, console)
        state["genres"][g] = {"number": numbers[g]}
        save_state(state_path, state)
        print(f"built {g} on channel {numbers[g]} (station {result})")

    for g in plan:
        state["genres"][g] = {"number": numbers[g]}
    save_state(state_path, state)

    write_tuner_channels(args.tuner_config, {g: numbers[g] for g in plan}, ours_before)
    print(f"wrote {len(plan)} channels to {args.tuner_config}")
    where = "the container" if container is None else args.fs42_container
    hint = ("docker kill --signal=USR1 <this container's name>" if container is None
            else f"docker kill --signal=USR1 {args.fs42_container}")
    if (container is None or container) and not args.no_restart_tuner:
        if restart_tuner(container):
            print(f"asked {where} to restart its tuner, so the channels are live")
        else:
            print(f"could not signal {where}; restart its tuner yourself: {hint}")
    else:
        print(f"restart the tuner to pick them up: {hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
