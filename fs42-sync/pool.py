"""Keep a local "pool" of symlinks that mirrors the media share.

The pool is a tree of real folders holding one symlink per video (and its .nfo
sidecar), each pointing at the file on the share by the path THIS container
sees. It lives on local disk, so you can browse it, and copy or move links out
of it into FieldStation42's tag folders with ordinary tools, without touching
the share and without writing a container path by hand.

The scan only ever ADDS links. A removed or renamed file leaves its old link
behind until you prune (prune_pool), and a link you moved or deleted from the
pool is simply made again if the file is still there, so treat the pool as
managed and copy out of it rather than rearranging it.

Walking a big share is slow, so a directory whose own modified time hasn't
changed since the last scan is not listed again: one stat stands in for a
listing, and its known subfolders are reused. Two safeguards, because a folder's
time is only a hint: one modified within the last few seconds is not trusted
(see RACY_NS), and directory times are not reliable on every server, so a full
scan (full=True) lists everything and should run now and then.
"""
import os
import time

# A folder time this close to the moment we looked can't be trusted: a file added
# in the same timestamp tick leaves the time unchanged, and the folder would look
# untouched on the next pass. (The same trap Git guards against with its "racy"
# index entries.) Such a folder is simply listed again next time.
RACY_NS = 3_000_000_000

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
SIDECAR_EXT = {".nfo"}
# Folders NAS systems and operating systems drop into a share. Never media.
JUNK_DIRS = {"@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information", "lost+found", "__pycache__"}


def wanted(name):
    ext = os.path.splitext(name)[1].lower()
    return ext in VIDEO_EXT or ext in SIDECAR_EXT


def ensure_link(dst, target, stats):
    """Make dst a link to target unless something is already there. Never replaces."""
    if os.path.islink(dst):
        if os.readlink(dst) != target:
            stats["notes"].append(f"kept existing link that points elsewhere: {dst}")
        return
    if os.path.lexists(dst):
        stats["notes"].append(f"kept existing file: {dst}")
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(target, dst)
    except OSError as e:
        stats["notes"].append(f"could not link {dst}: {e}")
        return
    stats["links_added"] += 1


def new_state():
    return {"dirs": {}, "last_full": 0}


def scan_pool(media_root, pool_root, state, full=False, include=(), exclude=(), now=0, now_ns=None):
    """Add the missing links to the pool. Returns stats; updates state in place.

    media_root is the share as this process sees it; link targets are written
    with that path. include / exclude name top-level folders of the share.
    """
    media_root = media_root.rstrip("/")
    now_ns = time.time_ns() if now_ns is None else now_ns
    stats = {"dirs_listed": 0, "dirs_cached": 0, "links_added": 0, "notes": [], "full": bool(full)}
    dirs = state.setdefault("dirs", {})
    # The cached folder lists were made under one include/exclude; a different one
    # means they are wrong, so start over.
    chosen = [sorted(include), sorted(exclude)]
    if full or state.get("filter") != chosen:
        dirs.clear()
        stats["full"] = True
    state["filter"] = chosen
    seen = set()
    stack = [""]
    while stack:
        rel = stack.pop()
        src = os.path.join(media_root, rel) if rel else media_root
        try:
            mtime = os.stat(src).st_mtime_ns
        except OSError:
            dirs.pop(rel, None)                     # gone: forget it. Its links stay (add-only).
            continue
        seen.add(rel)
        cached = dirs.get(rel)
        if cached and not cached.get("racy") and cached["mtime"] == mtime:
            stats["dirs_cached"] += 1
            subdirs = cached["subdirs"]
        else:
            stats["dirs_listed"] += 1
            subdirs = []
            try:
                entries = sorted(os.scandir(src), key=lambda e: e.name)
            except OSError as e:
                stats["notes"].append(f"could not read {src}: {e}")
                continue
            for e in entries:
                name = e.name
                if name.startswith(".") or name in JUNK_DIRS:
                    continue
                if not rel and ((include and name not in include) or name in exclude):
                    continue
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(name)
                elif wanted(name) and rel:
                    # (files straight in the share's top level are not in any library)
                    ensure_link(os.path.join(pool_root, rel, name), f"{src}/{name}", stats)
            dirs[rel] = {"mtime": mtime, "subdirs": subdirs, "racy": abs(now_ns - mtime) < RACY_NS}
        for name in subdirs:
            stack.append(f"{rel}/{name}" if rel else name)
    for gone in [d for d in dirs if d not in seen]:
        dirs.pop(gone, None)                        # a subtree that disappeared from the share
    if full:
        state["last_full"] = now
    return stats


def prune_pool(media_root, pool_root):
    """Remove pool links whose file is gone from the share, and folders that empties.

    Only links that point under media_root are considered; a link you made to
    anywhere else is left alone. Returns how many links were removed.
    """
    media_root = media_root.rstrip("/")
    removed = 0
    for root, dirs, files in os.walk(pool_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            if target.startswith(media_root + "/") and not os.path.lexists(target):
                os.remove(path)
                removed += 1
                parent = os.path.dirname(path)
                while parent != pool_root and not os.listdir(parent):
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
    return removed
