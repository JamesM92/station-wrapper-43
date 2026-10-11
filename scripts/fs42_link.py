#!/usr/bin/env python3
"""Put videos into a FieldStation42 tag folder as links, never as copies.

  fs42_link.py TAG SOURCE [SOURCE ...] [--dry-run]

SOURCE is a link in the pool (media-links/), or a folder of them. Each video
link is recreated in catalog/by-genre/TAG pointing at the same file on the
share, so the video exists once and appears in as many tags as you like.

Why a tool when you could copy by hand: copying a link with a plain `cp`, or
dragging it in a file manager or over an SMB re-share, can follow the link and
copy the whole video instead. This only ever makes links, and refuses a real
file, so it cannot duplicate media.

  a link    ->  TAG/<name>
  a folder  ->  TAG/<folder name>/<the same layout>   (so two shows' S01E01.mp4 don't collide)

Nothing is replaced. Run it again and only what is missing is added.
Afterwards the station needs its catalog rebuilt to see the new videos: the
fs42-sync service notices and can do that for you, or use the console.
"""
import argparse
import os
import sys

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
FARM = "by-genre"


def catalog_dir(arg=None, env=None):
    env = os.environ if env is None else env
    if arg:
        return arg
    if env.get("FS42_IN_CONTAINER") == "1":       # inside the Station Wrapper 43 container
        return "/app/catalog"
    if env.get("FS42_CATALOG_ROOT"):
        return env["FS42_CATALOG_ROOT"]
    root = env.get("FS42_LOCAL_ROOT") or env.get("FS42_SSD_ROOT")
    if root:
        return os.path.join(root, "catalog")
    return None


def plan_links(sources, tag_dir):
    """[(new link path, target)] and a list of problems, without touching anything."""
    plan, problems = [], []
    for src in sources:
        src = src.rstrip("/") or src
        if os.path.islink(src) and not os.path.isdir(src):
            if os.path.splitext(src)[1].lower() not in VIDEO_EXT:
                problems.append(f"skipped (not a video): {src}")
                continue
            plan.append((os.path.join(tag_dir, os.path.basename(src)), os.readlink(src)))
        elif os.path.isdir(src):
            base = os.path.basename(os.path.abspath(src))
            found = 0
            for root, dirs, files in os.walk(src):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    path = os.path.join(root, name)
                    if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                        continue
                    if not os.path.islink(path):
                        problems.append(f"skipped, a real file and not a link (copying it would duplicate the "
                                        f"video): {path}")
                        continue
                    rel = os.path.relpath(path, src)
                    plan.append((os.path.join(tag_dir, base, rel), os.readlink(path)))
                    found += 1
            if not found:
                problems.append(f"no video links found in {src}")
        elif os.path.exists(src):
            problems.append(f"refused, a real file and not a link (copying it would duplicate the video): {src}")
        else:
            problems.append(f"not found: {src}")
    return plan, problems


def apply_links(plan, dry=False):
    """Create the missing links. Returns (added, already there, problems)."""
    added = present = 0
    problems = []
    for link, target in plan:
        if os.path.islink(link):
            if os.readlink(link) == target:
                present += 1
            else:
                problems.append(f"kept existing link that points elsewhere: {link}")
            continue
        if os.path.lexists(link):
            problems.append(f"kept existing file: {link}")
            continue
        if not dry:
            try:
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(target, link)
            except OSError as e:
                problems.append(f"could not link {link}: {e}")
                continue
        added += 1
    return added, present, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", help="the tag folder to add to (created if missing), e.g. Comedy or commercials")
    ap.add_argument("sources", nargs="+", help="links in the pool, or folders of them")
    ap.add_argument("--catalog", help="the catalog folder (default: $FS42_CATALOG_ROOT, or "
                                      "$FS42_LOCAL_ROOT/catalog)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be linked and change nothing")
    args = ap.parse_args(argv)

    catalog = catalog_dir(args.catalog)
    if not catalog:
        raise SystemExit("where is the catalog? Pass --catalog, or set FS42_LOCAL_ROOT (or FS42_CATALOG_ROOT).")
    if os.sep in args.tag or args.tag in (".", "..") or args.tag.startswith("."):
        raise SystemExit(f"'{args.tag}' is not a tag name. A tag is one folder name, like Comedy.")
    tag_dir = os.path.join(catalog, FARM, args.tag)
    plan, problems = plan_links(args.sources, tag_dir)
    added, present, more = apply_links(plan, dry=args.dry_run)
    problems += more
    for p in problems:
        print(f"note: {p}", file=sys.stderr)
    verb = "would add" if args.dry_run else "added"
    print(f"{args.tag}: {verb} {added} links, {present} already there ({tag_dir})")
    if added and not args.dry_run:
        print("The station's catalog must be rebuilt to see them (the fs42-sync service can do it, "
              "or use the FieldStation42 console).")
    return 1 if problems and not added and not present else 0


if __name__ == "__main__":
    sys.exit(main())
