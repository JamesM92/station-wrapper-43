"""The name people know a video by, read from the .nfo file beside it.

FieldStation42 names every scheduled video after its FILE (`20260101_[abc]_Some Video`, `Film.2019.1080p.x264`). The
guide, the now-playing banner and the schedule should say what the video is called. A Kodi-style .nfo beside the
video (the ones the TubeArchivist conversion writes, and the ones most media tools write) has that:

    an episode of a show  the show's name, and the episode's title apart      (<showtitle> and <title>)
    a YouTube video       the channel's name, and the video's title apart     (the same, with a youtube <uniqueid>)
    a movie               the movie's title                                   (<movie><title>)

Nothing is ever written. A video with no .nfo keeps its file name. Answers are remembered for a while, a missing .nfo
too, so a guide that lists thousands of videos reads each file once.
"""
import html
import os
import re
import threading
import time

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".mpg", ".mpeg", ".wmv", ".flv"}
EPISODE_NAMES = True                # the episode's name is shown too ("Show: Episode", or apart in the guide); off: just the show's
KEEP_FOUND = 3600
KEEP_MISSING = 600
MAX_REMEMBERED = 30000
READ_BYTES = 64 * 1024
_lock = threading.Lock()
_seen = {}                          # nfo path -> (when it expires, title or None)


def _tag(text, name, limit=200):
    m = re.search(rf"<{name}(?:\s[^>]*)?>\s*(.*?)\s*</{name}>", text, re.S | re.I)
    if not m:
        return None
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
    value = html.unescape(re.sub(r"\s+", " ", value)).strip()
    return value[:limit] or None


def configure(episode_names):
    """Set whether episodes carry their own name too. The answers remembered under the old setting are dropped."""
    global EPISODE_NAMES
    EPISODE_NAMES = bool(episode_names)


def parts_from_nfo(text):
    """(title, episode name) for one .nfo's text. The title is what the video belongs to (a show's name, a YouTube channel's
    name, a movie's title); the episode name is the title of an episode or of a YouTube video. (None, None) when the .nfo
    says nothing usable."""
    low = text[:2000].lower()
    if "<episodedetails" in low:
        show, episode = _tag(text, "showtitle"), _tag(text, "title")
        if show:
            return show, (episode if episode and episode != show else None)
        return episode, None
    if "<movie" in low:
        return _tag(text, "title"), None
    return None, None


def details_from_nfo(text):
    """What the guide can say about a video besides its names, from its .nfo: {"plot", "season", "episode", "aired", "year",
    "genres"} with only what the file has. {} when it says nothing usable."""
    low = text[:2000].lower()
    if "<episodedetails" not in low and "<movie" not in low:
        return {}
    out = {"kind": "episode" if "<episodedetails" in low else "movie"}
    plot = _tag(text, "plot", 600) or _tag(text, "outline", 600)
    if plot:
        out["plot"] = plot
    for key in ("season", "episode"):
        v = _tag(text, key)
        if v and re.fullmatch(r"\d{1,6}", v):
            out[key] = int(v)
    aired = _tag(text, "aired") or _tag(text, "premiered") or _tag(text, "releasedate")
    if aired and re.match(r"\d{4}-\d{2}-\d{2}", aired):
        out["aired"] = aired[:10]
    year = _tag(text, "year")
    if year and re.fullmatch(r"\d{4}", year):
        out["year"] = int(year)
    genres = [html.unescape(g).strip() for g in re.findall(r"<genre(?:\s[^>]*)?>\s*(.*?)\s*</genre>", text, re.S | re.I)]
    genres = [g[:60] for g in dict.fromkeys(genres) if g]
    # Jellyfin sorts the programmes into Movies, Sports, News and Kids by these words in a programme's categories
    if "<movie" in low and not any(g.lower() in ("movie", "film") for g in genres):
        genres.insert(0, "Movie")
    low_g = {g.lower() for g in genres}
    if low_g & {"animation", "cartoon", "cartoons", "family", "children", "kids"} and "kids" not in low_g:
        genres.append("Kids")
    if genres:
        out["genres"] = genres[:6]
    return out


def title_from_nfo(text):
    """The one name for a video (the show's name, with the episode's after it when that is switched on), or None."""
    title, episode = parts_from_nfo(text)
    return f"{title}: {episode}"[:200] if title and episode and EPISODE_NAMES else title


def parts_for(video_path):
    """(title, episode name) for this video from its .nfo, or (None, None) to keep its file name."""
    if os.path.splitext(video_path)[1].lower() not in VIDEO_EXT:
        return None, None
    nfo = os.path.splitext(video_path)[0] + ".nfo"
    now = time.time()
    with _lock:
        hit = _seen.get(nfo)
    if hit and hit[0] > now:
        return hit[1]
    parts = (None, None)
    try:
        with open(nfo, "r", encoding="utf-8", errors="replace") as f:
            parts = parts_from_nfo(f.read(READ_BYTES))
    except OSError:
        pass
    with _lock:
        if len(_seen) > MAX_REMEMBERED:
            _seen.clear()
        _seen[nfo] = (now + (KEEP_FOUND if parts[0] else KEEP_MISSING), parts)
    return parts


def title_for(video_path):
    """What to call this video in one line, or None to keep its file name."""
    title, episode = parts_for(video_path)
    return f"{title}: {episode}"[:200] if title and episode and EPISODE_NAMES else title


def names_for(video_path):
    """(title, episode name) for the guide, which shows them apart: the episode name only when that is switched on."""
    title, episode = parts_for(video_path)
    return title, (episode if EPISODE_NAMES else None)


_seen_details = {}                 # nfo path -> (when it expires, details)


def details_for(video_path):
    """{"plot", "season", "episode", "aired", "year", "genres"} for this video from its .nfo (what the file has), or {}."""
    if os.path.splitext(video_path)[1].lower() not in VIDEO_EXT:
        return {}
    nfo = os.path.splitext(video_path)[0] + ".nfo"
    now = time.time()
    with _lock:
        hit = _seen_details.get(nfo)
    if hit and hit[0] > now:
        return hit[1]
    got = {}
    try:
        with open(nfo, "r", encoding="utf-8", errors="replace") as f:
            got = details_from_nfo(f.read(READ_BYTES * 4))
    except OSError:
        pass
    with _lock:
        if len(_seen_details) > MAX_REMEMBERED:
            _seen_details.clear()
        _seen_details[nfo] = (now + (KEEP_FOUND if got else KEEP_MISSING), got)
    return got


ART_NAMES = ("-thumb.jpg", "-thumb.png", "-poster.jpg", ".jpg", ".png")
SHOW_ART = ("poster.jpg", "folder.jpg", "cover.jpg", "thumb.jpg", "fanart.jpg", "banner.jpg", "poster.png", "folder.png")


def show_art_for(video_path):
    """The picture of the SHOW a video belongs to (poster.jpg, folder.jpg... beside the video, in its season's folder or in the
    show's own), or None. A list holds links, so the folders the video really sits in are looked in too."""
    folders = [os.path.dirname(video_path)]
    try:
        if os.path.islink(video_path):
            target = os.readlink(video_path)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(video_path), target))
            folders.append(os.path.dirname(target))
    except OSError:
        pass
    for folder in folders:
        for _ in range(3):
            for name in SHOW_ART:
                if os.path.isfile(os.path.join(folder, name)):
                    return os.path.join(folder, name)
            parent = os.path.dirname(folder)
            if not parent or parent == folder:
                break
            folder = parent
    return None


def art_for(video_path, poster=True):
    """The picture of this video (a thumbnail beside it: `<name>-thumb.jpg`, as the TubeArchivist conversion writes), or None.
    A list holds links to the video, so the link is followed one step to the folder the video really sits in."""
    stems = [os.path.splitext(video_path)[0]]
    try:
        if os.path.islink(video_path):
            target = os.readlink(video_path)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(video_path), target))
            stems.append(os.path.splitext(target)[0])
    except OSError:
        pass
    for stem in stems:
        for suffix in ART_NAMES:
            if os.path.isfile(stem + suffix):
                return stem + suffix
    # no picture of its own: the show's (a poster beside the video, in its season's folder, or in the show's folder)
    for stem in (stems if poster else ()):
        folder = os.path.dirname(stem)
        for _ in range(3):
            for name in SHOW_ART:
                if os.path.isfile(os.path.join(folder, name)):
                    return os.path.join(folder, name)
            parent = os.path.dirname(folder)
            if not parent or parent == folder:
                break
            folder = parent
    return None


def hook(catalog_root, names=False):
    """A function from a plan entry's path (catalog-relative, or absolute) to its title (resolver.TITLE_HOOK), or with
    names=True to (title, episode name) (resolver.NAMES_HOOK)."""
    def find(path):
        full = path if os.path.isabs(path) else os.path.join(catalog_root, path)
        return names_for(full) if names else title_for(full)
    return find


def details_hook(catalog_root):
    """A function from a plan entry's path to its details (see details_for), for the guide."""
    def find(path):
        full = path if os.path.isabs(path) else os.path.join(catalog_root, path)
        return details_for(full)
    return find


def forget():
    with _lock:
        _seen.clear()
        _seen_details.clear()
