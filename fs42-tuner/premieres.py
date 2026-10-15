"""Premiere nights: the folders behind a show's "new episodes air here, older ones loop" (see schedules.py).

For each show with premiere settings (the show is a list of links, in season folders or not):

    <lists>/.loop.<Show>        links to the older episodes (before the new ones begin) and the ones already premiered,
                                in the same folders: the show's other slots play this, in order, looping
    <lists>/.new.<Show>.<k>     one link: new episode number k, made a few days before its night, so the slot that night
                                can name it as its only choice

Folders and links are only ever ADDED (a catalog update brings them in), so nothing a catalog knows of goes away. What
is premiered is decided by the date alone (schedules.premiere_index), never by state kept here.
"""
import os
import re
from datetime import timedelta

import ordering
import schedules

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}


_natural = ordering.natural_key


def episodes(show_dir):
    """The videos of a show as paths inside it ("Season 5/Show - S05E01.mkv"), in episode order."""
    out = []
    for root, dirs, names in os.walk(show_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            if not n.startswith(".") and os.path.splitext(n)[1].lower() in VIDEO_EXT:
                out.append(os.path.relpath(os.path.join(root, n), show_dir).replace(os.sep, "/"))
    out.sort(key=_natural)
    return out


def folders(eps):
    """[(folder path inside the show, how many episodes it holds)] for choosing where the new episodes start."""
    count = {}
    for e in eps:
        parts = e.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            count["/".join(parts[:i])] = count.get("/".join(parts[:i]), 0) + 1
    return sorted(count.items(), key=lambda kv: _natural(kv[0]))


def boundary(eps, start):
    """Where the new episodes begin: the first episode that is `start` (a file) or inside it (a folder). With none given,
    or one that is not there, there are no new episodes (len(eps)): everything is the loop."""
    if not start:
        return len(eps)
    for i, e in enumerate(eps):
        if e == start or e.startswith(start + "/"):
            return i
    return len(eps)


def _clean(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "x"


def loop_tag(show):
    return f".loop.{_clean(show)}"


def new_tag(show, k):
    return f".new.{_clean(show)}.{k}"


def plan(defn, lists_dir, today):
    """{show: {"loop": tag or None, "new": [tag for each new episode], "pool": [paths], "need": {tag: path}, "dir"}} for
    each show with premiere settings. `loop` is None while nothing is older and nothing has premiered: the show plays
    from its first episode as it is. `need` are the premiere folders to have by now (the nights within the lookahead)."""
    out = {}
    for show, cfg in (defn.get("premieres") or {}).items():
        show_dir = os.path.join(lists_dir, show)
        eps = episodes(show_dir) if os.path.isdir(show_dir) else []
        b = boundary(eps, cfg.get("from", ""))
        new = eps[b:]
        pool = eps[:b] + new[:schedules.premiere_index(defn, show, today)]
        need = {}
        for d in schedules.premiere_dates(defn, show, today, today + timedelta(days=schedules.LOOKAHEAD)):
            k = schedules.premiere_index(defn, show, d)
            if k < len(new):
                need[new_tag(show, k)] = new[k]
        out[show] = {"loop": loop_tag(show) if pool else None, "new": [new_tag(show, k) for k in range(len(new))],
                     "new_paths": new, "pool": pool, "need": need, "dir": show_dir}
    return out


def _link(target_dir, rel, source):
    """A link at target_dir/rel to what `source` (itself a link into the media) points at. Only the links are looked at,
    never the media: this runs often, and a share may be asleep."""
    where = os.readlink(source) if os.path.islink(source) else os.path.abspath(source)
    dest = os.path.join(target_dir, *rel.split("/"))
    if os.path.islink(dest) and os.readlink(dest) == where:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.lexists(dest):
        os.unlink(dest)
    os.symlink(where, dest)
    return True


def sync(lists_dir, plans):
    """Make the folders a plan names. Returns the names of the folders that got something new (a catalog update is due)."""
    changed = []
    for show, p in plans.items():
        wanted = [(p["loop"], rel) for rel in p["pool"]] if p["loop"] else []
        wanted += [(tag, rel) for tag, rel in p["need"].items()]
        touched = set()
        for tag, rel in wanted:
            source = os.path.join(p["dir"], *rel.split("/"))
            if not os.path.lexists(source):
                continue
            target = os.path.join(lists_dir, tag)
            if tag in p["need"]:
                rel_in = os.path.basename(rel)             # a premiere folder holds the episode at its top
            else:
                rel_in = rel
            if _link(target, rel_in, source):
                touched.add(tag)
        changed += sorted(touched)
    return changed
