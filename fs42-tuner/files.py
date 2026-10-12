"""The file manager's backend: browse the sources, build the tag folders.

A tag folder (catalog/by-genre/<Tag>) is a list FieldStation42 schedules from. This lets you
build those lists: look through your media, add videos or whole folders to a list, take
them out again. Rules that never bend:

  * Only symlinks are made. A video is never copied, so adding it to ten lists costs nothing.
  * The media is never written. Sources are read-only here, and "remove" only ever removes a
    link, never a real file.
  * Everything written is under the tag folder root. Names and paths from the browser are
    checked, and a link that points at a folder is never written through (that would put
    links in somewhere else, such as your media).

Nothing here talks HTTP: tuner.py calls these functions. Errors are FileError, whose text is
safe to show and whose `code` is the HTTP status.
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid

import settings

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
SIDECAR_EXT = {".nfo"}          # travels with its video: FieldStation42 reads titles from it
MAX_ENTRIES = 3000              # one listing
MAX_LINKS = int(os.environ.get("FILES_MAX_LINKS", "200000"))      # one "add": it runs in the background, so it can be big
PEEK_SECONDS = 5.0              # most time one listing spends looking inside folders to find single movies
HIDDEN = {"@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found"}
SOURCES = ("media", "pool", "tanfo")


class FileError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def clean_rel(rel):
    """A relative path from the browser, normalised, or FileError. "" is the root itself."""
    if rel is None:
        rel = ""
    if not isinstance(rel, str) or "\0" in rel or "\\" in rel:
        raise FileError("that path is not valid")
    if rel.startswith("/"):
        raise FileError("that path is not valid")
    rel = rel.rstrip("/")
    if not rel:
        return ""
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise FileError("that path is not valid")
    return "/".join(parts)


def clean_tag(name):
    """A tag folder name, or FileError. One folder name: no slashes, not hidden."""
    if not isinstance(name, str):
        raise FileError("a list needs a name")
    name = name.strip()
    if not name:
        raise FileError("a list needs a name")
    if "/" in name or "\\" in name or "\0" in name or name in (".", "..") or name.startswith("."):
        raise FileError("a list name is one folder name: no slashes, and it can't start with a dot")
    if len(name.encode()) > 200:
        raise FileError("that name is too long")
    return name


_linking = {}                 # list name -> {"thread", "started"}: sources being linked in the background (see add_show)
_linking_lock = threading.Lock()
TYPE_LABELS = {"videos": "Videos", "shorts": "Shorts", "streams": "Streams"}   # a YouTube channel's kinds of video (TA_NFOWriter)
SHOW_MARK = ".show"           # inside a list made from a show (see add_show): the show's folder in the media view
SEASON = re.compile(r"^(season|series|saison|staffel|temporada|book|volume)\b[\s._-]*\d*|^s\d{1,2}$|^specials?$", re.I)
EXTRA = re.compile(r"(^|[\s._-])(trailer|sample|behindthescenes|featurette|deleted|interview|scene|short|other)$", re.I)
_class_cache = {}             # folder -> (its modified time, answer): looking inside a folder on a share is slow, so once


def classify(folder):
    """What a folder in the media is: ("movie", the video's name) when it holds exactly one video and no season
    folders (extras, subtitles and the like beside it don't matter), ("show", None) when it holds several videos or has
    season folders, else (None, None): an ordinary folder that holds other folders."""
    try:
        stamp = os.stat(folder).st_mtime_ns
    except OSError:
        return None, None
    hit = _class_cache.get(folder)
    if hit and hit[0] == stamp:
        return hit[1]
    videos, season = [], False
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") or e.name in HIDDEN:
                    continue
                if e.is_dir():
                    season = season or bool(SEASON.match(e.name))
                elif _is_video(e.name) and not EXTRA.search(os.path.splitext(e.name)[0]):    # a trailer or sample is not the movie
                    videos.append(e.name)
    except OSError:
        return None, None
    answer = ("show", None) if season or len(videos) >= 2 else ("movie", videos[0]) if videos else (None, None)
    if len(_class_cache) > 20000:
        _class_cache.clear()
    _class_cache[folder] = (stamp, answer)
    return answer


def is_show_dir(folder):
    """A folder that is a show on its own: not one of a show's season folders, and not inside another show."""
    if SEASON.match(os.path.basename(folder.rstrip("/"))) or classify(folder)[0] != "show":
        return False
    return classify(os.path.dirname(folder.rstrip("/")))[0] != "show"


def linking():
    """The names of the sources whose videos are still being linked in."""
    with _linking_lock:
        return sorted(n for n, x in _linking.items() if x["thread"].is_alive())


def wait_linked(timeout=3600):
    """Wait until every source being linked in is done (a build must not start on half of a series). True if all are."""
    end = time.time() + timeout
    while True:
        with _linking_lock:
            alive = [x["thread"] for x in _linking.values() if x["thread"].is_alive()]
        if not alive:
            return True
        if time.time() > end:
            return False
        alive[0].join(0.5)


def read_mark(path):
    """{"path", "kind"} from a show source's marker. A marker an earlier version made holds just the path (a series)."""
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        info = json.loads(raw)
        if isinstance(info, dict) and isinstance(info.get("path"), str):
            out = {"path": info["path"], "kind": info.get("kind") or "series"}
            if isinstance(info.get("members"), list):
                out["members"] = info["members"]
            return out
    except ValueError:
        pass
    return {"path": raw, "kind": "series"} if raw else None


def source_kind(full):
    """What a path in the media would be as a source: a "series", a "season", one "episode" (a video), or a plain
    "folder" of videos; None for anything else."""
    if os.path.isfile(full):
        return "episode" if _is_video(os.path.basename(full)) else None
    if not os.path.isdir(full):
        return None
    if is_show_dir(full):
        return "series"
    if SEASON.match(os.path.basename(full.rstrip("/"))) or classify(os.path.dirname(full.rstrip("/")))[0] == "show":
        return "season"
    return "folder"


def source_name(rel, full, kind):
    """The list name a source is made under: the series, "series - season", "series - episode", a movie by its own name."""
    parts = rel.split("/")
    if kind == "episode":
        stem = os.path.splitext(parts[-1])[0]
        parent = os.path.dirname(full)
        if classify(parent)[0] == "movie":
            return parts[-2]                                  # a movie in its own folder: the folder's name
        return " - ".join(parts[-3:-1] + [stem]) if len(parts) > 2 else stem
    if kind == "season":
        return " - ".join(parts[-2:])
    return parts[-1]


def shows_in(tags_root):
    """The names of the lists that are shows used as sources (see Files.add_show)."""
    try:
        names = os.listdir(tags_root)
    except OSError:
        return []
    return sorted((n for n in names if os.path.exists(os.path.join(tags_root, n, SHOW_MARK))), key=str.casefold)


def season_type(part):
    """"videos" (Season 2026), "shorts" (Season 12026) or "streams" (Season 22026) for a TA_NFOWriter season folder name,
    None for anything else."""
    m = re.match(r"^Season (\d{4,5})$", part.strip())
    if not m:
        return None
    n = int(m.group(1))
    return "streams" if n >= 20000 else "shorts" if n >= 10000 else "videos"


def _type_ok(rel, types):
    """Is a file (its path inside the followed folder) of one of these kinds? Files outside any season stay."""
    if not types:
        return True
    kind = season_type(rel.split("/")[0])
    return kind is None or kind in types


def _is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def _wanted(name):
    ext = os.path.splitext(name)[1].lower()
    return ext in VIDEO_EXT or ext in SIDECAR_EXT


def _within(base, rel=""):
    """`base` joined with an already cleaned relative path, normalised and checked to stay inside `base` (FileError when it
    does not). The one place a path from a page becomes a path on disk."""
    root = os.path.normpath(base)
    if not rel:
        return root
    full = os.path.normpath(os.path.join(root, rel))
    if not full.startswith(root.rstrip(os.sep) + os.sep):
        raise FileError("that path is not valid")
    return full


def _inside(path, base):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    return path == base or path.startswith(base.rstrip("/") + "/")


class Files:
    def __init__(self, media_root, catalog_dir, ta=None, follows_path=None):
        """media_root: where the media is mounted. catalog_dir: the catalog volume (/app/catalog).
        ta: optional, returns the TubeArchivist settings ({"enabled": bool, "library": path}); with it, the raw
        TubeArchivist folder is hidden once its readable version is on."""
        self.catalog = catalog_dir
        self.follows_path = follows_path            # where followed folders are remembered; None: not remembered
        self._follow_lock = threading.Lock()
        self._add_jobs = {}                         # adds running or just done in the background (add_in_background)
        self._jobs_lock = threading.Lock()
        self.follow_status = {}
        self.ta = ta or (lambda: {"enabled": False, "libraries": []})
        self.tags_root = os.path.join(catalog_dir, "by-genre")
        self.roots = {"media": media_root,
                      "pool": os.path.join(catalog_dir, "media-links"),
                      "tanfo": os.path.join(catalog_dir, "tanfo")}

    # ---- what exists
    def info(self):
        roots = {name: os.path.isdir(path) for name, path in self.roots.items()}
        pool = self._pool_in_use()
        # "media" is the one view the page shows: the pool when there is one, else the real media folders
        roots["media"] = pool or roots["media"] or roots["tanfo"]
        return {"roots": roots, "tags": os.path.isdir(self.tags_root), "media_source": "pool" if pool else "media"}

    # ---- the one media view: the pool (or the real folders) with the YouTube view as a folder in it
    def _pool_in_use(self):
        """The pool counts once it holds something: it is made by the sync job's first pass, and until then (or
        if that job is off) the folder can exist and be empty, which would hide all of your media."""
        return bool(self._top_names(self.roots["pool"]))

    def _media_base(self):
        return self.roots["pool"] if self._pool_in_use() else self.roots["media"]

    def _top_names(self, base):
        try:
            return [n for n in os.listdir(base) if not n.startswith(".") and n not in HIDDEN]
        except OSError:
            return []

    def _settings(self):
        try:
            cfg = self.ta() or {}
        except Exception:
            return [], []
        libs = cfg.get("libraries") or ([cfg["library"]] if cfg.get("library") else [])
        views = cfg.get("views") or [{"library": p, "name": n} for p, n in settings.view_names(libs).items()]
        return libs, (views if cfg.get("enabled") else [{**v, "off": True} for v in views])

    def _hidden_raw(self):
        """Names of the raw TubeArchivist folders at the top of the media, once their readable versions are on."""
        out = set()
        for v in self._settings()[1]:
            if v.get("off") or not os.path.isdir(os.path.join(self.roots["tanfo"], v["name"])):
                continue                      # off, or not converted yet: keep showing the original
            lib = os.path.realpath(v["library"])
            if os.path.dirname(lib) == os.path.realpath(self.roots["media"]):
                out.add(os.path.basename(lib))
        return out

    def _view_folders(self, base):
        """{name shown in the media view: real folder} for each converted library that has been built: the library's
        own folder name plus -nfo. A name something else at the top already has gets ' (converted)' added."""
        hidden = self._hidden_raw()
        taken = {n.casefold() for n in self._top_names(base) if n not in hidden}
        out = {}
        for v in self._settings()[1]:
            real = os.path.join(self.roots["tanfo"], v["name"])
            if not os.path.isdir(real):
                continue
            name = v["name"] if v["name"].casefold() not in taken else f"{v['name']} (converted)"
            taken.add(name.casefold())
            out[name] = real
        return out

    def _locate(self, root, rel):
        """(real folder, path inside it, name to give the top folder) for a place the page names."""
        rel = clean_rel(rel)
        if root == "media":
            base = self._media_base()
            first, _, rest = rel.partition("/")
            real = self._view_folders(base).get(first)
            if real:
                return real, rest, first
            return base, rel, None
        return self._base(root), rel, None

    def _base(self, root):
        if root == "tags":
            return self.tags_root
        if root not in self.roots:
            raise FileError("unknown place")
        return self.roots[root]

    def _own(self, path):
        """Give a new link or folder the catalog's owner: we run as root, the catalog is yours."""
        if os.geteuid() != 0:
            return
        try:
            ref = os.stat(self.catalog)
            os.lchown(path, ref.st_uid, ref.st_gid)
        except OSError:
            pass

    # ---- browsing
    def list(self, root, rel=""):
        rel_shown = clean_rel(rel)
        base, rel, _label = self._locate(root, rel)
        if not os.path.isdir(base):
            raise FileError("that place is not there (is it mounted?)", 404)
        full = _within(base, rel)
        if not os.path.isdir(full):
            raise FileError("that folder is not there", 404)
        if not _inside(full, base):
            raise FileError("that folder is outside this place", 403)
        entries, truncated = [], False
        peek_until = time.time() + PEEK_SECONDS     # looking inside every folder is slow on a network share: bounded
        markers = None
        inside_show = root in SOURCES and bool(rel_shown) and (SEASON.match(os.path.basename(full)) or classify(full)[0] == "show")
        in_playlists = os.path.basename(full) == "Playlists" and os.path.dirname(os.path.dirname(full)) == self.roots["tanfo"]
        try:
            names = sorted(os.listdir(full), key=str.casefold)
        except OSError as e:
            raise FileError(f"cannot read that folder: {e.strerror}", 403)
        for name in names:
            if name.startswith(".") or name in HIDDEN:
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            path = os.path.join(full, name)
            entry = {"name": name}
            try:
                if os.path.islink(path):
                    entry["link"] = os.readlink(path)
                    entry["ok"] = os.path.exists(path)
                    entry["kind"] = "dir" if os.path.isdir(path) else "file"
                    if entry["kind"] == "file":
                        entry["video"] = _is_video(name)
                elif os.path.isdir(path):
                    entry["kind"] = "dir"
                else:
                    entry["kind"] = "file"
                    entry["video"] = _is_video(name)
                    entry["size"] = os.path.getsize(path)
            except OSError:
                continue
            if entry["kind"] == "file" and name.endswith(".m3u8") and in_playlists:
                entry = {"name": name[:-5], "kind": "playlist", "videos": self._playlist_size(path)}
                entries.append(entry)
                continue
            if entry["kind"] == "file" and not entry["video"]:
                continue                       # only folders and videos are shown: no .nfo, images, subtitles
            if entry["kind"] == "dir" and root in SOURCES and not inside_show and time.time() < peek_until:   # one movie in a folder: show the movie
                what, video = classify(path)
                if what == "show" and not self._in_view(rel_shown or name):
                    entry["show"] = True
                    if markers is None:
                        markers = self._markers()
                    entry["source"] = markers.get(rel_shown + "/" + name if rel_shown else name, (None,))[0]     # the list's name, or None
                if what == "movie":
                    vpath = os.path.join(path, video)
                    try:
                        size = os.path.getsize(vpath)
                    except OSError:
                        size = 0
                    entry = {"name": f"{name}/{video}", "label": name, "kind": "file", "video": True, "size": size}
            entries.append(entry)
        if root == "media" and not rel_shown:           # the top of the media view (not inside the YouTube folder)
            raw = self._hidden_raw()
            entries = [e for e in entries if e["name"] not in raw]               # its readable version replaces it
            for name in self._view_folders(base):
                if not any(e["name"] == name for e in entries):
                    entries.append({"name": name, "kind": "dir"})
        if root == "tags" and not rel_shown:             # shows used as sources have their own place (shows()), not the lists
            entries = [e for e in entries if not (e["kind"] == "dir" and os.path.exists(os.path.join(full, e["name"], SHOW_MARK)))]
        if root == "media" and rel_shown and "/" not in rel_shown and rel_shown in self._view_folders(self._media_base()):
            for e in entries:                                 # a converted library's playlists come first, as "_Playlists"
                if e["kind"] == "dir" and e["name"] == "Playlists":
                    e["label"] = "_Playlists"
        entries.sort(key=lambda e: (e["kind"] != "dir", (e.get("label") or e["name"]) != "_Playlists",
                                    (e.get("label") or e["name"]).casefold()))
        return {"root": root, "path": rel_shown, "entries": entries, "truncated": truncated}

    # ---- shows as sources: a station can play a show directly
    def shows(self):
        """The names of the lists that are shows used as sources, sorted."""
        return shows_in(self.tags_root)

    def _markers(self):
        """{source path in the media: (list name, kind)} for every show source that has been made."""
        out = {}
        for name in shows_in(self.tags_root):
            info = read_mark(os.path.join(self.tags_root, name, SHOW_MARK))
            if info:
                out[info["path"]] = (name, info["kind"])
        return out

    def show_members(self):
        """{list name: [{"root", "path", "types"?}]}: what each source is made of, so a page can show and change it."""
        out = {}
        for name in shows_in(self.tags_root):
            info = read_mark(os.path.join(self.tags_root, name, SHOW_MARK))
            if not info:
                continue
            if info.get("members"):
                out[name] = info["members"]
            else:
                path, _, types = info["path"].partition("?")
                m = {"root": "media", "path": path}
                if types:
                    m["types"] = types.split(",")
                out[name] = [m]
        return out

    def show_kinds(self):
        """{list name: "series" | "season" | "episode" | "folder"} for each show source."""
        return {name: kind for name, kind in self._markers().values()}

    def media_path_of_list_item(self, rel):
        """The path in the media view of a video (or folder) inside a list ("List/Season 1/ep.mp4"), so it can be used as a
        source of its own. A list's files are links into the media: this follows the link back. FileError when it
        does not lead into the media view."""
        rel = clean_rel(rel)
        tag, _, inner = rel.partition("/")
        self._tag_dir(tag)
        if not inner:
            raise FileError("that is a whole list: use it as it is")
        full = _within(self.tags_root, rel)
        if not os.path.lexists(full):
            raise FileError("that is not in the list", 404)
        base = self._media_base()
        places = [("", os.path.realpath(base))] + [(name, os.path.realpath(r)) for name, r in self._view_folders(base).items()]

        def in_media(path):
            real = os.path.realpath(path)
            for name, root in places:
                if real.startswith(root.rstrip(os.sep) + os.sep):
                    return "/".join(x for x in (name, os.path.relpath(real, root).replace(os.sep, "/")) if x)
            return None
        if os.path.isdir(full) and not os.path.islink(full):
            # a folder in a list is a real folder of links: its place in the media is where one of its files comes from,
            # less the part of the path that is inside the folder
            for r, dirs, names in os.walk(full):
                dirs.sort()
                for n in sorted(names):
                    if _wanted(n) and not n.startswith("."):
                        got = in_media(os.path.join(r, n))
                        inner_rel = os.path.relpath(os.path.join(r, n), full).replace(os.sep, "/")
                        if got and got.endswith("/" + inner_rel):
                            return got[: -len(inner_rel) - 1]
                        raise FileError("that folder is not inside your media folders, so it can't be picked on its own here")
            raise FileError("there are no videos in that folder")
        got = in_media(full)
        if got:
            return got
        raise FileError("that one is not inside your media folders, so it can't be picked on its own here")

    def add_show(self, rel, wait=True, done=None, types=None):
        """Use a series, a season, or one episode or movie as a source of its own, so a station's slot can play it
        without a list being made first. It is made as a list (kept up to date for a folder) and marked, so it is
        offered in the Shows choice and not among the lists. A whole top-level folder is not allowed: choose a
        series inside it. `types`: only one kind of a YouTube channel's videos ("videos", "shorts" or "streams"; each
        has its own seasons), so no season has to be picked. A YouTube playlist (a .m3u8 in a library's Playlists
        folder) is a source too."""
        rel = clean_rel(rel)
        if not rel:
            raise FileError("choose a series")
        types = self._clean_types(types)
        base, inner, _label = self._locate("media", rel)
        full = _within(base, inner)
        playlist = (not os.path.lexists(full) and os.path.lexists(full + ".m3u8")
                    and os.path.basename(os.path.dirname(full)) == "Playlists")
        if not playlist and (not os.path.lexists(full) or not _inside(os.path.dirname(full) if os.path.isfile(full) else full, base)):
            raise FileError("that is not there", 404)
        if os.path.isdir(full) and "/" not in rel:
            raise FileError(f"'{rel}' is a whole folder of your media: choose a series inside it (and a season, if you want one)")
        kind = "playlist" if playlist else source_kind(full)
        if kind is None:
            raise FileError("only a series, a season, a playlist, or one video can be a source")
        key, label = rel, None
        if types:
            if kind != "series" or not self._in_view(rel):
                raise FileError("only a YouTube channel has shorts, streams and videos to choose between")
            key = f"{rel}?{','.join(types)}"
            label = " + ".join(TYPE_LABELS[x] for x in types)
        known = self._markers()
        if key in known:
            name = known[key][0]
        else:
            name = self._free_name(rel, f"{source_name(rel, full, kind)} - {label}" if label else source_name(rel, full, kind))
            self.create_tag(name)
        mark = {"path": key, "kind": kind}
        src = {"root": "media", "path": rel}
        if types:
            src["types"] = types
        return self._start_source(name, kind, mark, [src], wait, done)

    def add_set(self, members, wait=True, done=None):
        """Several sources as one: [{"root": "tags", "path": list} | {"root": "media", "path": rel, "types": [...]}]. It is
        a list of its own (kept up to date, and offered as a source, not among the lists), so a mid card or a commercial
        slot, which takes one list, can have a series, a season and a video together. The same members are the same set."""
        if not isinstance(members, list) or not members:
            raise FileError("choose something to add")
        clean = []
        for m in members:
            if not isinstance(m, dict) or m.get("root") not in ("tags", "media"):
                raise FileError("that selection is not valid")
            path = clean_rel(m.get("path"))
            if not path:
                raise FileError("that selection is not valid")
            item = {"root": m["root"], "path": path}
            types = self._clean_types(m.get("types")) if m["root"] == "media" else None
            if types:
                item["types"] = types
            if item not in clean:
                clean.append(item)
        if len(clean) > 50:
            raise FileError("that is a lot to put together: make a list of them on the Lists page")
        key = "set:" + hashlib.sha1(json.dumps(sorted(clean, key=json.dumps), sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:12]
        known = self._markers()
        if key in known:
            name = known[key][0]
        else:
            first = os.path.splitext(clean[0]["path"].rsplit("/", 1)[-1])[0]
            base = f"{first} + {len(clean) - 1} more" if len(clean) > 1 else first
            name, n = base, 1
            while os.path.lexists(_within(self.tags_root, clean_tag(name))):      # another set starts the same way: number it
                n += 1
                name = f"{base} ({n})"
            self.create_tag(name)
        return self._start_source(name, "set", {"path": key, "kind": "set", "members": clean}, clean, wait, done)

    @staticmethod
    def _clean_types(types):
        if types in (None, [], ""):
            return None
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not all(x in TYPE_LABELS for x in types):
            raise FileError("the kinds of video are videos, shorts and streams")
        return sorted(set(types), key=list(TYPE_LABELS).index)

    def _start_source(self, name, kind, mark, sources, wait, done):
        """Mark list `name` as a source and link `sources` into it: now, or in the background (a build waits for it)."""
        tag_dir = _within(self.tags_root, name)
        mark_file = _within(tag_dir, SHOW_MARK)
        with open(mark_file, "w") as f:
            json.dump(mark, f)
        self._own(mark_file)

        def link():
            out = {"added": 0, "already_there": 0, "notes": [], "more_notes": 0}
            for s in sources:
                got = self.add(name, [s], follow=True)
                out["added"] += got["added"]
                out["already_there"] += got["already_there"]
                out["notes"] += got.get("notes", [])
            return out
        if wait:
            out = link()
            return {"tag": name, "kind": kind, **out}
        # a big series on a slow share can take a minute to link: answer at once and finish in the background. The page
        # shows it as being linked, and a build waits for it (wait_linked)
        with _linking_lock:
            running = _linking.get(name)
            if not (running and running["thread"].is_alive()):
                def work():
                    began = time.time()
                    try:
                        out = link()
                        print(f"[files] linked '{name}': {out['added']} new links, {out['already_there']} already there, "
                              f"in {time.time() - began:.1f}s (from {self._media_base()})", flush=True)
                    except Exception as e:
                        print(f"[files] could not link '{name}': {e}", flush=True)
                    finally:
                        if done:
                            try:
                                done(name)
                            except Exception:
                                pass
                th = threading.Thread(target=work, daemon=True, name=f"link-{name}")
                _linking[name] = {"thread": th, "started": time.time()}
                th.start()
        return {"tag": name, "kind": kind, "linking": True}

    def _free_name(self, rel, name):
        """A list name nothing else has: with the media folder it is in added first if the plain name is taken."""
        for candidate in (name, f"{rel.split('/')[0]} - {name}"):
            candidate = clean_tag(candidate)
            if not os.path.lexists(_within(self.tags_root, candidate)):
                return candidate
        raise FileError(f"there is already a list called '{name}'. Rename it, or use that list", 409)

    def remove_show(self, name):
        name, path = self._tag_dir(name)
        if not os.path.exists(os.path.join(path, SHOW_MARK)):
            raise FileError(f"'{name}' is a list, not a show used as a source", 409)
        os.unlink(os.path.join(path, SHOW_MARK))
        return self.delete_tag(name)

    # ---- the lists
    def _tag_dir(self, name, must_exist=True):
        name = clean_tag(name)
        path = _within(self.tags_root, name)
        if must_exist:
            if os.path.islink(path):
                raise FileError(f"'{name}' is a link to a folder, not a list this can change")
            if not os.path.isdir(path):
                raise FileError(f"there is no list called '{name}'", 404)
        return name, path

    def create_tag(self, name):
        name, path = self._tag_dir(name, must_exist=False)
        if os.path.lexists(path):
            raise FileError(f"'{name}' already exists", 409)
        os.makedirs(path)
        self._own(self.tags_root)
        self._own(path)
        return {"tag": name}

    def rename_tag(self, old, new):
        old, old_path = self._tag_dir(old)
        new, new_path = self._tag_dir(new, must_exist=False)
        if os.path.lexists(new_path):
            raise FileError(f"'{new}' already exists", 409)
        os.rename(old_path, new_path)
        self._move_follows(old, new)
        return {"tag": new, "note": "A station that uses the old name must be changed to the new one"}

    def delete_tag(self, name):
        """Remove a list and the links in it. Refuses if it holds a real file: that is yours."""
        name, path = self._tag_dir(name)
        real = []
        for root, dirs, files in os.walk(path):
            real += [os.path.relpath(os.path.join(root, f), path) for f in files
                     if not os.path.islink(os.path.join(root, f)) and f != SHOW_MARK]
        if real:
            shown = ", ".join(real[:3]) + (f" and {len(real) - 3} more" if len(real) > 3 else "")
            raise FileError(f"'{name}' holds real files, not just links ({shown}); this only deletes lists "
                            f"made of links. Remove those yourself first.")
        removed = 0
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
                removed += 1
            for d in dirs:
                p = os.path.join(root, d)
                os.unlink(p) if os.path.islink(p) else os.rmdir(p)
        os.rmdir(path)
        self._move_follows(name, None)
        return {"tag": name, "removed": removed}

    # ---- adding
    @staticmethod
    def _playlist_size(m3u):
        try:
            with open(m3u, encoding="utf-8", errors="replace") as f:
                return sum(1 for line in f if line.strip() and not line.startswith("#"))
        except OSError:
            return 0

    @staticmethod
    def _playlist_files(m3u, view):
        """(list folder name, [(name, link)]) for the videos of one TubeArchivist playlist: the converted links in
        the view that its .m3u8 names, each with its episode .nfo, in the playlist's order."""
        out, seen = [], set()
        view = os.path.normpath(view)
        try:
            with open(m3u, encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for line in f]
        except OSError as e:
            raise FileError(f"cannot read that playlist: {e.strerror}", 403)
        for line in lines:
            if not line or line.startswith("#"):
                continue
            p = os.path.normpath(os.path.join(os.path.dirname(m3u), line))
            if not p.startswith(view + os.sep) or not os.path.lexists(p) or not _is_video(p):
                continue
            name = os.path.basename(p)
            if name in seen:
                continue
            seen.add(name)
            out.append((name, p))
            nfo = os.path.splitext(p)[0] + ".nfo"
            if os.path.lexists(nfo):
                out.append((os.path.basename(nfo), nfo))
        return os.path.basename(m3u)[:-5], out

    def _source_files(self, root, rel):
        """(path relative to the chosen folder, full path) for each video and nfo under a source."""
        if root not in SOURCES and root != "tags":
            raise FileError("you can only add from the media view or from another list")
        if root == "tags":
            rel = clean_rel(rel)
            if not rel or "/" in rel:
                raise FileError("choose a whole list to put inside this one")
            self._tag_dir(rel)                         # it must be a list that exists (and not a link)
        base, rel, label = self._locate(root, rel)
        full = _within(base, rel)
        if (root != "tags" and not os.path.lexists(full) and os.path.lexists(full + ".m3u8")
                and os.path.basename(os.path.dirname(full)) == "Playlists"
                and os.path.dirname(os.path.dirname(os.path.dirname(full))) == self.roots["tanfo"]):
            return self._playlist_files(full + ".m3u8", os.path.dirname(os.path.dirname(full)))
        if not os.path.lexists(full):
            raise FileError(f"{root}/{rel} is not there", 404)
        # A link to a FILE is fine (the pool is made of them, pointing into the media). A folder, or a link to a
        # folder, must really be inside this place: otherwise it is a way out of it.
        if not _inside(os.path.dirname(full) if os.path.islink(full) and not os.path.isdir(full) else full, base):
            raise FileError("that is outside this place", 403)
        if os.path.isdir(full):
            out = []
            for r, dirs, files in os.walk(full):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in HIDDEN)
                for f in sorted(files):
                    if not f.startswith(".") and _wanted(f):
                        p = os.path.join(r, f)
                        out.append((os.path.relpath(p, full).replace(os.sep, "/"), p))
                if len(out) > MAX_LINKS:
                    raise FileError(f"that is more than {MAX_LINKS} files: add a smaller folder")
            return (label if label and not rel else os.path.basename(full.rstrip("/"))) or root, out
        if not _wanted(os.path.basename(full)):
            raise FileError("only videos (and their .nfo files) can be added")
        out = [(os.path.basename(full), full)]
        stem = os.path.splitext(full)[0]
        if _is_video(full) and os.path.isfile(stem + ".nfo"):
            out.append((os.path.basename(stem) + ".nfo", stem + ".nfo"))
        return None, out

    @staticmethod
    def _target(path):
        """What a new link should point at: the file's own path, or where a link already points (one hop)."""
        if os.path.islink(path):
            t = os.readlink(path)
            return t if os.path.isabs(t) else os.path.normpath(os.path.join(os.path.dirname(path), t))
        return path

    def add_in_background(self, tag, sources, follow=False, done=None):
        """add(), run in a thread: a pick of many channels can be tens of thousands of links, and the page used to sit
        on one request that long. Returns {"id"} at once; add_status(id) says how far it is."""
        tag, _dir = self._tag_dir(tag)                 # a list that is not there is refused now, not later
        if not isinstance(sources, list) or not sources:
            raise FileError("choose something to add")
        job = {"id": uuid.uuid4().hex[:10], "tag": tag, "state": "running", "message": "Looking through what you picked…",
               "done": 0, "total": 0, "started": time.time()}
        with self._jobs_lock:
            self._add_jobs[job["id"]] = job
            for old in sorted(self._add_jobs.values(), key=lambda j: j["started"])[:-20]:
                if old["state"] != "running":
                    self._add_jobs.pop(old["id"], None)

        def progress(message, done_=None, total=None):
            job["message"] = message
            if done_ is not None:
                job["done"] = done_
            if total is not None:
                job["total"] = total

        def work():
            try:
                job["result"] = self.add(tag, sources, follow=follow, progress=progress)
                job["state"] = "done"
                job["message"] = "Done."
            except FileError as e:
                job["state"], job["message"] = "error", str(e)
            except Exception as e:                      # a job always ends in done or error, never hangs the page
                job["state"], job["message"] = "error", f"that did not work: {e}"
            finally:
                if done and job["state"] == "done":
                    try:
                        done(tag)
                    except Exception:
                        pass
        threading.Thread(target=work, daemon=True, name=f"add-{tag}").start()
        return {"id": job["id"], "tag": tag}

    def add_status(self, job_id):
        with self._jobs_lock:
            job = self._add_jobs.get(job_id)
        if job is None:
            raise FileError("that add is not known (the server was restarted?)", 404)
        return {k: v for k, v in job.items() if k != "started"} | {"elapsed": int(time.time() - job["started"])}

    def add(self, tag, sources, follow=False, progress=None):
        """Link the chosen videos and folders into a list. sources: [{"root": "...", "path": "..."}].
        With follow, each FOLDER is also remembered: refresh_follows() links the new videos that appear in it later."""
        tag, tag_dir = self._tag_dir(tag)
        if not isinstance(sources, list) or not sources:
            raise FileError("choose something to add")
        plan, followed = [], []
        for n, s in enumerate(sources, 1):
            if not isinstance(s, dict):
                raise FileError("that selection is not valid")
            if progress:
                progress(f"Looking through {n} of {len(sources)} folders… {len(plan)} files found so far", 0, 0)
            folder, files = self._source_files(s.get("root"), s.get("path"))
            if s.get("root") == "tags":
                self._check_no_loop(tag, clean_rel(s.get("path")), follow)
            types = self._clean_types(s.get("types"))
            for rel, path in files:
                if _type_ok(rel, types):
                    plan.append((os.path.join(folder, rel) if folder else rel, path))
            if folder:
                followed.append({"root": s.get("root"), "path": clean_rel(s.get("path")), "folder": folder, "types": types})
        if len(plan) > MAX_LINKS:
            raise FileError(f"that is more than {MAX_LINKS} files: add a smaller selection")
        out = self._link(tag, tag_dir, plan, progress)
        if follow and followed and self.follows_path:
            out["following"] = self._follow(tag, followed)
        return out

    def _in_view(self, rel):
        """Is this path in the media view inside a converted YouTube library (a "-nfo" folder)? A channel there is not a
        TV series: it can go into a list like any folder."""
        top = clean_rel(rel).split("/")[0]
        return bool(top) and top in self._view_folders(self._media_base())

    def _check_no_loop(self, tag, inside, follow):
        """A list can't contain itself, and (when it is kept up to date) lists can't contain each other in a circle."""
        if inside == tag:
            raise FileError("a list can't be put inside itself")
        if not follow or not self.follows_path:
            return
        edges = {}
        for x in self._load_follows():
            if x["root"] == "tags":
                edges.setdefault(x["tag"], set()).add(x["path"])
        todo, seen = [inside], set()
        while todo:
            cur = todo.pop()
            if cur == tag:
                raise FileError(f"“{inside}” already has “{tag}” inside it, so that would go round in a circle")
            if cur not in seen:
                seen.add(cur)
                todo += list(edges.get(cur, ()))

    def _link(self, tag, tag_dir, plan, progress=None):
        added = present = 0
        notes = []
        for n, (rel, path) in enumerate(plan):
            if progress and n % 200 == 0:
                progress(f"Linking {n} of {len(plan)} files…", n, len(plan))
            dest = os.path.join(tag_dir, rel)
            target = self._target(path)
            problem = self._blocked(tag_dir, rel)
            if problem:
                notes.append(f"{rel}: {problem}")
                continue
            if os.path.islink(dest):
                if os.readlink(dest) == target:
                    present += 1
                else:
                    notes.append(f"{rel}: kept the link that is already there (it points somewhere else)")
                continue
            if os.path.lexists(dest):
                notes.append(f"{rel}: kept the file that is already there")
                continue
            try:
                self._makedirs(tag_dir, os.path.dirname(rel))
                os.symlink(target, dest)
                self._own(dest)
            except OSError as e:
                notes.append(f"{rel}: could not link ({e.strerror})")
                continue
            added += 1
        return {"tag": tag, "added": added, "already_there": present, "notes": notes[:20],
                "more_notes": max(0, len(notes) - 20)}

    # ---- followed folders: a folder added to a list stays linked, and its new videos are pulled in later
    def _load_follows(self):
        try:
            with open(self.follows_path) as f:
                data = json.load(f).get("follows", [])
            return [x for x in data if isinstance(x, dict) and all(isinstance(x.get(k), str) for k in ("tag", "root", "path", "folder"))]
        except (OSError, ValueError, AttributeError):
            return []

    def _save_follows(self, items):
        folder = os.path.dirname(self.follows_path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".follows-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"follows": items}, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o666)  # nosec B103 - a file in the config volume, editable from the host by whoever owns the folder
            os.replace(tmp, self.follows_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if os.geteuid() == 0:
            try:
                ref = os.stat(folder)
                os.chown(self.follows_path, ref.st_uid, ref.st_gid)
            except OSError:
                pass

    def _follow(self, tag, folders):
        """Remember these folders for this list (a folder already followed keeps what was taken out of it)."""
        with self._follow_lock:
            items = self._load_follows()
            for f in folders:
                if not any(x["tag"] == tag and x["root"] == f["root"] and x["path"] == f["path"] for x in items):
                    items.append({"tag": tag, "root": f["root"], "path": f["path"], "folder": f["folder"], "excluded": [],
                                  **({"types": f["types"]} if f.get("types") else {})})
            self._save_follows(items)
        return [f["folder"] for f in folders]

    def follows(self, tag=None):
        """The followed folders (of one list, or all), with how the last refresh went."""
        if not self.follows_path:
            return []
        out = []
        for x in self._load_follows():
            if tag is not None and x["tag"] != tag:
                continue
            st = self.follow_status.get((x["tag"], x["root"], x["path"]), {})
            out.append({"tag": x["tag"], "root": x["root"], "path": x["path"], "folder": x["folder"],
                        "excluded": len(x.get("excluded", [])), "last_checked": st.get("at"),
                        "last_added": st.get("added"), "error": st.get("error")})
        return out

    def unfollow(self, tag, root, path):
        """Stop following a folder. The links already in the list stay."""
        path = clean_rel(path)
        with self._follow_lock:
            items = self._load_follows()
            keep = [x for x in items if not (x["tag"] == tag and x["root"] == root and x["path"] == path)]
            if len(keep) == len(items):
                raise FileError("that folder is not being followed", 404)
            self._save_follows(keep)
        return {"tag": tag, "path": path}

    @staticmethod
    def _skipped(rel, excluded):
        return any(rel == e or rel.startswith(e + "/") for e in excluded)

    def refresh_follows(self):
        """Link, into each list, the videos that have appeared in its followed folders. Add-only, and what you took
        out of a followed folder is not put back. Returns {"added": total, "checked": folders looked at}."""
        total = checked = 0
        if not self.follows_path:
            return {"added": 0, "checked": 0}
        for x in self._load_follows():
            key = (x["tag"], x["root"], x["path"])
            checked += 1
            try:
                tag, tag_dir = self._tag_dir(x["tag"])
                _label, files = self._source_files(x["root"], x["path"])
                folder = x["folder"]                       # the name it was given when it was added: it stays put
                excluded = x.get("excluded", [])
                types = x.get("types")
                plan = [(os.path.join(folder, rel), path) for rel, path in files
                        if not self._skipped(rel, excluded) and _type_ok(rel, types)]
                got = self._link(tag, tag_dir, plan)["added"]
                if x["root"] == "tags":                    # a list inside a list mirrors it: what left it leaves here too
                    self._prune_mirror(tag_dir, folder, {rel for rel, _p in plan})
                total += got
                self.follow_status[key] = {"at": time.time(), "added": got, "error": None}
            except FileError as e:
                self.follow_status[key] = {"at": time.time(), "added": 0, "error": str(e)}
            except OSError as e:
                self.follow_status[key] = {"at": time.time(), "added": 0, "error": e.strerror or "could not read it"}
        return {"added": total, "checked": checked}

    def _prune_mirror(self, tag_dir, folder, keep):
        """Take out the links under tag_dir/folder that are no longer in the list it mirrors. Links only."""
        top = os.path.join(tag_dir, folder)
        if not os.path.isdir(top) or os.path.islink(top):
            return
        for root, dirs, files in os.walk(top):
            for f in files:
                p = os.path.join(root, f)
                if os.path.islink(p) and os.path.relpath(p, tag_dir).replace(os.sep, "/") not in keep:
                    os.unlink(p)
        if os.path.isdir(top):
            self._tidy(top, tag_dir)

    def _forget_removed(self, tag, rel):
        """You took `rel` out of a list: stop following the folder it was (all of it), or keep that video out."""
        if not self.follows_path:
            return
        with self._follow_lock:
            items, changed = self._load_follows(), False
            for x in list(items):
                if x["tag"] != tag:
                    continue
                if rel == x["folder"]:
                    items.remove(x)
                    changed = True
                elif rel.startswith(x["folder"] + "/"):
                    inner = rel[len(x["folder"]) + 1:]
                    ex = x.setdefault("excluded", [])
                    for e in (inner, os.path.splitext(inner)[0] + ".nfo") if _is_video(inner) else (inner,):
                        if e not in ex:
                            ex.append(e)
                    changed = True
            if changed:
                self._save_follows(items)

    def _move_follows(self, old, new):
        """A list was renamed (new) or deleted (None): its followed folders go with it."""
        if not self.follows_path:
            return
        with self._follow_lock:
            items = self._load_follows()
            out, changed = [], False
            for x in items:
                if x["root"] == "tags" and x["path"] == old:      # another list has this one inside it
                    changed = True
                    if new is None:
                        continue                                  # the list is gone: stop following it
                    x["path"] = new
                if x["tag"] == old:
                    changed = True
                    if new is None:
                        continue
                    x["tag"] = new
                out.append(x)
            if changed:
                self._save_follows(out)

    @staticmethod
    def _blocked(tag_dir, rel):
        """Why a link can't go at tag_dir/rel: a folder on the way is a link (it would write through it)."""
        cur = tag_dir
        for part in os.path.dirname(rel).split("/") if os.path.dirname(rel) else []:
            cur = _within(cur, part)
            if os.path.islink(cur):
                return "skipped: a folder on the way is a link, and links are never written through"
            if os.path.lexists(cur) and not os.path.isdir(cur):
                return "skipped: a file is in the way"
        return None

    def _makedirs(self, tag_dir, rel_dir):
        cur = tag_dir
        for part in rel_dir.split("/") if rel_dir else []:
            cur = _within(cur, part)
            if not os.path.isdir(cur):
                os.mkdir(cur)
                self._own(cur)

    # ---- removing
    def remove(self, tag, paths):
        """Take links (or folders of links) out of a list. A real file is never touched."""
        tag, tag_dir = self._tag_dir(tag)
        if not isinstance(paths, list) or not paths:
            raise FileError("choose something to remove")
        removed, refused = 0, []
        for rel in paths:
            rel = clean_rel(rel)
            if not rel:
                raise FileError("choose something inside the list")
            full = _within(tag_dir, rel)
            if not os.path.lexists(full):
                continue
            self._forget_removed(tag, rel)
            blocked = self._blocked(tag_dir, rel)
            if blocked:
                refused.append(rel)
                continue
            if os.path.islink(full):
                os.unlink(full)
                removed += 1
                if _is_video(full):                           # its .nfo link is hidden from view: it goes with it
                    side = os.path.splitext(full)[0] + ".nfo"
                    if os.path.islink(side):
                        os.unlink(side)
            elif os.path.isdir(full):
                n, bad = self._remove_dir(full)
                removed += n
                refused += [os.path.join(rel, b) for b in bad]
            else:
                refused.append(rel)
            self._tidy(os.path.dirname(full), tag_dir)
        return {"tag": tag, "removed": removed, "refused": refused[:20]}

    @staticmethod
    def _remove_dir(path):
        removed, bad = 0, []
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                p = os.path.join(root, f)
                if os.path.islink(p):
                    os.unlink(p)
                    removed += 1
                else:
                    bad.append(os.path.relpath(p, path))
            for d in dirs:
                p = os.path.join(root, d)
                if os.path.islink(p):
                    os.unlink(p)
                    removed += 1
                else:
                    try:
                        os.rmdir(p)
                    except OSError:
                        pass
        try:
            os.rmdir(path)
        except OSError:
            pass
        return removed, bad

    @staticmethod
    def _tidy(path, stop):
        """After a removal: a folder left with no videos has nothing to keep its hidden .nfo links for, so those
        links go and the folder goes. Real files are never touched (a folder holding one stays). Never the list itself."""
        stop = os.path.realpath(stop)
        while os.path.realpath(path) != stop and os.path.realpath(path).startswith(stop + "/"):
            for root, _dirs, files in os.walk(path):
                if any(_is_video(f) for f in files):
                    return                                     # still has videos: leave it
            for root, dirs, files in os.walk(path, topdown=False):
                for f in files:
                    p = os.path.join(root, f)
                    if os.path.islink(p) and not _is_video(f):
                        os.unlink(p)                           # a sidecar link, and nothing left for it to go with
                for d in dirs:
                    try:
                        os.rmdir(os.path.join(root, d))
                    except OSError:
                        pass
            try:
                os.rmdir(path)
            except OSError:
                return                                         # something real is in there: leave it
            path = os.path.dirname(path)
