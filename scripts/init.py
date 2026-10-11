#!/usr/bin/env python3
"""First run: look at your media, check the containers can see it, preview the
channels, and write a starter groups.toml. Nothing else is written.

  scripts/init.py --media-root /mnt/media [--catalog DIR] [--out groups.toml]

What it does:
  1. Finds the libraries under --media-root: tvseries/, anime/, movies/ and
     bump/, plus any other folder that is itself a TV library (a
     TubeArchivist view built by TA_NFOWriter, for instance) and its playlists.
  2. Checks the FieldStation42 container can see those paths. Links only work
     when the containers mount the media at the same paths as this host. Also
     tests that the catalog and any TV-library folder can hold working
     symlinks: SMB/CIFS shares often can't.
  3. Previews the channels the generator would make: a dry run, no links.
  4. Writes a commented groups.toml (it won't overwrite one without --force),
     and prints the command to run next.
"""
import argparse
import json
import os
import subprocess
import sys

import build_channels as bc


def candidate_libraries(media_root):
    """Folders under media_root that look like a TV library of their own.

    A folder qualifies if it holds a Playlists folder of .m3u8 files, or at
    least one subfolder with a tvshow.nfo. The standard libraries are skipped.
    """
    standard = {bc.BUMP, bc.MOVIE_LIBRARY, *bc.TV_LIBRARIES}
    found = []
    for name in sorted(os.listdir(media_root)):
        path = os.path.join(media_root, name)
        if name.startswith(".") or name in standard or name == "__pycache__" or not os.path.isdir(path):
            continue
        playlists = sorted(os.path.splitext(f)[0] for f in os.listdir(os.path.join(path, "Playlists"))
                           if f.endswith(".m3u8")) if os.path.isdir(os.path.join(path, "Playlists")) else []
        shows = [d for d in os.listdir(path)
                 if os.path.isfile(os.path.join(path, d, "tvshow.nfo"))]
        if playlists or shows:
            found.append({"path": path, "label": name, "shows": len(shows), "playlists": playlists})
    return found


CATALOG_VIEW = "tanfo"
CONTAINER_CATALOG = "/app/catalog"


def catalog_view(catalog):
    """The TubeArchivist view the ta-nfo service builds inside the catalog, if it's there.

    It is written by a container, so its links use container paths; the generator is
    told both the host folder and the container path (HOST=CONTAINER).
    """
    if not catalog:
        return None
    path = os.path.join(os.path.abspath(catalog), CATALOG_VIEW)
    if not os.path.isdir(path):
        return None
    playlists = sorted(os.path.splitext(f)[0] for f in os.listdir(os.path.join(path, "Playlists"))
                       if f.endswith(".m3u8")) if os.path.isdir(os.path.join(path, "Playlists")) else []
    shows = [d for d in os.listdir(path) if os.path.isfile(os.path.join(path, d, "tvshow.nfo"))]
    if not shows and not playlists:
        return None
    return {"path": path, "label": CATALOG_VIEW, "shows": len(shows), "playlists": playlists,
            "container": f"{CONTAINER_CATALOG}/{CATALOG_VIEW}"}


def uncovered(paths, destinations):
    """Paths that no container mount destination covers."""
    return [p for p in paths
            if not any(p == d or p.startswith(d.rstrip("/") + "/") for d in destinations)]


def container_destinations(container):
    """Mount destinations inside a container, or None when it can't be read."""
    try:
        out = subprocess.run(["docker", "inspect", container, "--format", "{{json .Mounts}}"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return [m["Destination"] for m in json.loads(out.stdout)]
    except (ValueError, KeyError, TypeError):
        return None


def render_manifest(media_root, libraries, catalog=None, target_root=None):
    lines = [
        "# groups.toml -- your FieldStation42 wrapper setup. Keep it in the config folder (beside catalog/).",
        "# Run:  scripts/build_channels.py --manifest groups.toml --dry-run",
        "# Command-line flags override anything here. See the README, \"Groups\".",
        "",
        f'media_root = "{media_root}"',
    ]
    if catalog:
        lines.append(f'catalog = "{catalog}"')
    if target_root and target_root != media_root:
        lines.append(f'target_root = "{target_root}"   # where the container sees your media (its mount is always /mnt/media)')
    else:
        lines.append("# target_root = \"/mnt/media\"   # where the container sees media_root, if it differs")
    if libraries:
        lines += ["", "# Folders that are themselves a TV library (each subfolder with a tvshow.nfo is a show).",
                  "tv_libraries = ["]
        lines += [f'  "{lib["path"]}{"=" + lib["container"] if lib.get("container") else ""}",' for lib in libraries]
        lines.append("]")
    else:
        lines += ["", '# tv_libraries = ["/mnt/media/tanfo"]   # a TA_NFOWriter view, for example']
    lines += [
        "",
        "# Groups add videos to another place as symlinks; nothing is moved or copied, and the",
        "# videos stay in their own genre channel. A group names ONE source and, optionally, `to`:",
        "#   to omitted      its own channel, named after the playlist or show",
        "#   to = \"Comedy\"   that genre's channel as well",
        "#   to = \"commercials\"   played between programmes (kept off the genre channels)",
    ]
    for lib in libraries:
        for name in lib["playlists"][:3]:
            lines += ["", "# [[group]]", f'# playlist = "{name}"', "# to = \"Some Channel\""]
    if not any(lib["playlists"] for lib in libraries):
        lines += ["", "# [[group]]", '# playlist = "1980s Ads *"       # wildcards work', '# to = "commercials"']
    lines += ["", "# [[group]]", '# show = "Some Show"', "# [[group]]", '# video = "Show/Season 1/*intro*"',
              '# to = "Retro Mix"']
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--media-root", default="/mnt/media", help="your media on THIS host")
    ap.add_argument("--target-root", default="/mnt/media",
                    help="where the container sees it. The compose files always mount it at /mnt/media, "
                         "whatever the host path is")
    ap.add_argument("--catalog", help="the catalog folder; groups.toml goes in the config folder beside it")
    ap.add_argument("--out", help="where to write the manifest (default: config/groups.toml beside --catalog, "
                                  "else ./groups.toml)")
    ap.add_argument("--fs42-container", default="station-wrapper-43", help="the container to check the mounts of")
    ap.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    args = ap.parse_args(argv)

    inside = bc.in_container()
    if inside:      # running in the Station Wrapper 43 container: its own paths, its own config volume
        args.catalog = args.catalog or bc.CONTAINER_CATALOG
        args.out = args.out or bc.CONTAINER_MANIFEST
    media_root = os.path.abspath(args.media_root)
    if not os.path.isdir(media_root):
        raise SystemExit(f"{media_root} is not a folder. Pass --media-root with the folder holding your media.")
    out = args.out or (os.path.join(os.path.dirname(os.path.abspath(args.catalog)), "config", "groups.toml")
                       if args.catalog else "groups.toml")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if os.path.exists(out) and not args.force:
        raise SystemExit(f"{out} already exists. Edit it, or pass --force to replace it.")

    print(f"Media root: {media_root}")
    present = [lib for lib in (*bc.TV_LIBRARIES, bc.MOVIE_LIBRARY, bc.BUMP)
               if os.path.isdir(os.path.join(media_root, lib))]
    print("  standard folders found: " + (", ".join(present) or "none"))
    bump_folder = os.path.join(args.catalog, bc.FARM, bc.BUMP) if args.catalog else None
    have_clips = bool(bump_folder and bc.count_bump_clips(args.catalog))
    if bc.BUMP not in present and not have_clips:
        where = bump_folder or "<catalog>/by-genre/bump"
        print(f"  NOTE: no channel mid cards (the short clips in the bump folder). FieldStation42 needs some for every station, and the generator "
              f"stops without them. Put a few short video files in {where} (inside the catalog volume; "
              f"no media folder or extra volume needed).")
    libraries = candidate_libraries(media_root)
    target_root = args.target_root.rstrip("/") or "/"
    if target_root != media_root:
        for lib in libraries:       # the container sees them under its own mount, not under the host's path
            lib["container"] = f"{target_root}/{os.path.relpath(lib['path'], media_root)}"
    view = catalog_view(args.catalog)
    if view:
        libraries.append(view)
    for lib in libraries:
        pl = f", {len(lib['playlists'])} playlists" if lib["playlists"] else ""
        where = f"  (the containers see it as {lib['container']})" if lib.get("container") else ""
        print(f"  TV library: {lib['path']}  ({lib['shows']} shows{pl}){where}")
    if not present and not libraries:
        print("  Nothing that looks like a library. Expected tvseries/, anime/, movies/ or a folder of shows "
              "with tvshow.nfo files.")

    # What the container must be able to see, as the container names it: each standard folder
    # that exists on the host (the generator links them), and each TV library. Checked one by
    # one against the container's mounts, so it works whether you share in the whole media
    # folder or one volume per folder, and it names the folder that is missing.
    needed = [f"{target_root}/{name}" for name in (*bc.TV_LIBRARIES, bc.MOVIE_LIBRARY, bc.BUMP)
              if os.path.isdir(os.path.join(media_root, name))]
    for lib in libraries:
        where = lib.get("container") or lib["path"]
        if not where.startswith(CONTAINER_CATALOG):          # the catalog volume is always there
            needed.append(where)
    destinations = None if inside else container_destinations(args.fs42_container)
    print(f"\nContainer check ({'this container' if inside else args.fs42_container}):")
    if inside:
        print("  skipped: this is running inside the container, so everything it found is visible to it.")
    elif destinations is None:
        print("  skipped: the container isn't running or Docker isn't reachable. Run this again after "
              "`docker compose up`, or check by hand that each folder above is mounted in it.")
    else:
        missing = uncovered(needed, destinations)
        if missing:
            for path in missing:
                print(f"  NOT VISIBLE: {path}\n    The container has no mount that covers it, so links to it would be dead. "
                      f"Add a read-only volume that mounts it there, then recreate the container.")
        else:
            print("  every folder above is mounted in the container")

    print("\nSymlink check (the catalog is made of symlinks, which SMB/CIFS shares handle badly):")
    checks = []
    if args.catalog:
        checks.append(bc.symlink_check(args.catalog, "catalog"))
    checks += [bc.symlink_check(lib["path"], "TV library") for lib in libraries]
    if not args.catalog:
        print("  catalog: pass --catalog to test it. It defaults to local disk, which is the safe place.")
    for level, message in checks:
        print(f"  {'PROBLEM: ' if level == 'fail' else 'note: ' if level == 'note' else ''}{message}")
    if any(level == "fail" for level, _ in checks):
        print("  Fix this first: links made on a folder that can't hold them will not work.")

    if args.catalog:
        tuner = bc.CONTAINER_TUNER_CONFIG if inside else \
            os.path.join(os.path.dirname(os.path.abspath(args.catalog)), "config", "tuner", "config.json")
        print(f"\nDashboard config: " + (f"found ({tuner})" if os.path.isfile(tuner) else
              f"not there yet ({tuner}). The container creates it the first time it starts: "
              f"start it once before running the generator."))

    print("\nChannel preview (no links are made):")
    tv = [bc.parse_tv_library(lib["path"] + (f"={lib['container']}" if lib.get("container") else ""),
                              {bc.BUMP, *bc.TV_LIBRARIES, bc.MOVIE_LIBRARY}) for lib in libraries]
    items, warnings = bc.discover(media_root, {}, tv)
    plan, skipped = bc.plan_farm(items, include_unclassified=False)
    if plan:
        for genre in sorted(plan):
            shows = len({"/".join(link.split("/")[1:3]) for link, _ in plan[genre]})
            print(f"  {genre:<28} {shows:>4} shows/movies")
    else:
        print("  no channels yet")
    if skipped:
        print(f"  {len(skipped)} items have no genre in their NFO and would be skipped "
              f"(--include-unclassified keeps them)")
    for w in warnings:
        if not w.startswith("library not found:"):      # the standard-folders line above says it
            print(f"  warning: {w}")

    # With groups.toml in a config/ folder, a catalog/ folder beside config/ is found without being
    # named (a catalog folder right beside the manifest works too).
    here = os.path.dirname(os.path.abspath(out))
    beside = os.path.abspath(os.path.join(os.path.dirname(here) if os.path.basename(here) == "config" else here,
                                          "catalog"))
    named = args.catalog and os.path.abspath(args.catalog) != beside and not inside
    with open(out, "w") as f:
        f.write(render_manifest(media_root, libraries, os.path.abspath(args.catalog) if named else None,
                                target_root))
    if inside and os.geteuid() == 0:        # written as root: hand it to whoever owns the config folder
        ref = os.stat(os.path.dirname(os.path.abspath(out)))
        os.chown(out, ref.st_uid, ref.st_gid)
    if inside:
        print(f"\nWrote {out} (in the config volume). Edit it, then:\n"
              f"  docker exec <container> fs42-channels build --dry-run     # preview\n"
              f"  docker exec <container> fs42-channels build               # build")
    else:
        print(f"\nWrote {out}. Next:\n"
              f"  scripts/build_channels.py --manifest {out} --dry-run     # preview\n"
              f"  scripts/build_channels.py --manifest {out}               # build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
