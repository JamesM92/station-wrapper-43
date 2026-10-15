#!/bin/bash
# Run once on the Docker host before the first `docker compose up`.
#
#   ./setup-dirs.sh              create the folders
#   ./setup-dirs.sh --migrate    first move an older layout into this one, and clear Docker's
#                                leftovers from it (old containers, volumes with old paths)
#
# THREE volumes make it work. Two are local folders under FS42_LOCAL_ROOT:
#   config/    everything the container keeps: runtime/ (the schedule database and sockets),
#              confs/ (station configs), tuner/ (the dashboard's config), groups.toml
#   catalog/   everything made of symlinks: by-genre/ (tag folders), tanfo/ (the TubeArchivist
#              view), media-links/ (a symlink for every video on your media)
# and the third is your media (FS42_MEDIA_ROOT), which stays on the share and may hold
# several separate shares as subfolders.
#
# The bind-style named volumes in docker-compose.yml (driver_opts: o=bind)
# require their host-side directory to already exist -- unlike a plain
# short-syntax bind mount, Docker will not create it for you, and `up` fails
# with "no such file or directory" instead.
set -euo pipefail
cd "$(dirname "$0")"

MIGRATE=0
case "${1:-}" in
  "") ;;
  --migrate) MIGRATE=1 ;;
  *) echo "usage: $0 [--migrate]" >&2; exit 2 ;;
esac

# shellcheck disable=SC1091
[ -f .env ] && source .env

if [ -z "${FS42_LOCAL_ROOT:-}" ] && [ -n "${FS42_SSD_ROOT:-}" ]; then
  echo "! .env uses FS42_SSD_ROOT, which is now called FS42_LOCAL_ROOT. Rename it in .env:" >&2
  echo "    sed -i 's/^FS42_SSD_ROOT=/FS42_LOCAL_ROOT=/' .env" >&2
  FS42_LOCAL_ROOT="$FS42_SSD_ROOT"          # carry on with it for this run, so --migrate can find the old data
  RENAME_NEEDED=1
fi
: "${FS42_LOCAL_ROOT:?set FS42_LOCAL_ROOT in .env: a LOCAL disk path, not an SMB share}"
: "${FS42_MEDIA_ROOT:?set in .env}"

CONFIG_ROOT="${FS42_CONFIG_ROOT:-$FS42_LOCAL_ROOT/config}"
CATALOG_DIR="${FS42_CATALOG_ROOT:-$FS42_LOCAL_ROOT/catalog}"
RUNTIME_DIR="$CONFIG_ROOT/runtime"
CONFS_DIR="$CONFIG_ROOT/confs"
TUNER_DIR="$CONFIG_ROOT/tuner"
POOL_DIR="$CATALOG_DIR/media-links"

# --- an older layout? Two earlier ones are known:
#   first:   runtime/ and catalog/ directly under FS42_LOCAL_ROOT (then called FS42_SSD_ROOT), confs/ on the NAS
#   second:  everything under FS42_LOCAL_ROOT/fs42/ (runtime, catalog, confs, tuner) plus media-links/ beside it
# The schedule database is the thing that must not be lost, so its folder decides.
V1_RUNTIME="$FS42_LOCAL_ROOT/runtime"
V2_DIR="$FS42_LOCAL_ROOT/fs42"
OLD_NAS_CONFS="${FS42_NAS_DATA:-}/confs"
OLDER=""
if [ ! -e "$RUNTIME_DIR" ]; then
  if [ -d "$V2_DIR/runtime" ]; then OLDER="$V2_DIR/runtime"
  elif [ -d "$V1_RUNTIME" ]; then OLDER="$V1_RUNTIME"; fi
fi
if [ -n "$OLDER" ]; then
  if [ "$MIGRATE" = 0 ]; then
    echo "! Found an older layout: $OLDER (your schedule database)." >&2
    echo "  This layout keeps it in $RUNTIME_DIR. Starting now would use an EMPTY schedule." >&2
    echo "  Stop the stack (docker compose down), then run:  ./setup-dirs.sh --migrate" >&2
    echo "  That MOVES the folders on this disk (a rename, nothing is copied) and COPIES confs/ from the NAS if it is there." >&2
    exit 1
  fi
  move() {   # move OLD NEW: only if OLD exists and NEW doesn't
    if [ -e "$1" ] && [ ! -e "$2" ]; then
      mkdir -p "$(dirname "$2")"
      mv "$1" "$2" && echo "  moved   $1 -> $2"
    fi
  }
  echo "Migrating the older layout (stop the stack first: docker compose down)"
  if [ "$OLDER" = "$V2_DIR/runtime" ]; then
    move "$V2_DIR/runtime" "$RUNTIME_DIR"
    move "$V2_DIR/confs" "$CONFS_DIR"
    move "$V2_DIR/tuner" "$TUNER_DIR"
    [ -z "${FS42_CATALOG_ROOT:-}" ] && move "$V2_DIR/catalog" "$CATALOG_DIR"
    move "$FS42_LOCAL_ROOT/media-links" "$POOL_DIR"
    rmdir "$V2_DIR" 2>/dev/null && echo "  removed the empty $V2_DIR" || true
  else
    move "$V1_RUNTIME" "$RUNTIME_DIR"              # the catalog is already where it belongs
    if [ -n "${FS42_NAS_DATA:-}" ] && [ -d "$OLD_NAS_CONFS" ] && [ ! -e "$CONFS_DIR" ]; then
      mkdir -p "$CONFIG_ROOT"
      cp -a "$OLD_NAS_CONFS" "$CONFS_DIR" && echo "  copied  $OLD_NAS_CONFS -> $CONFS_DIR (the original is left in place)"
    fi
  fi
fi

# A real symlink to a file outside the folder, followed: what every link here is.
probe_symlinks() {
  local dir="$1" probe target
  probe="$(mktemp -d -p "$dir" .symlink-probe-XXXXXX)"
  target="$(mktemp -d)"
  echo ok > "$target/t"
  if ln -s "$target/t" "$probe/link" 2>/dev/null && [ "$(cat "$probe/link" 2>/dev/null)" = ok ]; then
    rm -rf "$probe" "$target"; return 0
  fi
  rm -rf "$probe" "$target"; return 1
}
network_fs() {
  case "$(stat -f -c %T "$1" 2>/dev/null)" in
    cifs|smb|smb2|smb3|smbfs|nfs|nfs4|fuse.sshfs|9p) return 0 ;;
    *) return 1 ;;
  esac
}

mkdir -p "$RUNTIME_DIR" "$CONFS_DIR" "$TUNER_DIR" "$CATALOG_DIR" "$POOL_DIR"

# Where the symlinks and the database live must be local disk. Say so now,
# not after a container loops.
if network_fs "$CONFIG_ROOT"; then
  echo "! $CONFIG_ROOT is on a network share ($(stat -f -c %T "$CONFIG_ROOT"))." >&2
  echo "  It holds the sqlite schedule database, which misbehaves over SMB/NFS. Use local disk." >&2
  exit 1
fi
if ! probe_symlinks "$CATALOG_DIR"; then
  echo "! The catalog ($CATALOG_DIR) cannot hold working symlinks ($(stat -f -c %T "$CATALOG_DIR"))." >&2
  echo "  It is folders of symlinks. Put it on local disk: FS42_LOCAL_ROOT in .env (FS42_CATALOG_ROOT to move just the catalog)." >&2
  echo "  Only your media belongs on the share." >&2
  exit 1
fi
if network_fs "$CATALOG_DIR"; then
  echo "note: the catalog ($CATALOG_DIR) is on a network share ($(stat -f -c %T "$CATALOG_DIR")). Symlinks worked here," >&2
  echo "  but they can resolve differently inside a container. Local disk is safer." >&2
fi

# --- Docker remembers a bind-style named volume's path and options when it first creates the
# volume. If either has since changed (a new layout, an edited .env), `docker compose up` keeps
# using the OLD ones, or fails with a confusing "no such file or directory" mount error. (The
# media volume is the sharp case: its path is unchanged but it is now a recursive bind, and
# the old one would silently leave out shares mounted inside it.) Volumes the compose file no
# longer has, and containers of services it no longer has, show up as leftovers too. Removing a
# volume deletes only Docker's note of the path: the folder it pointed at is not touched.
STALE_VOLUMES=""
OLD_CONTAINERS=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  if CFG="$(FS42_LOCAL_ROOT="$FS42_LOCAL_ROOT" docker compose config --format json 2>/dev/null)"; then
    PROJECT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("name",""))' "$CFG")"
    STALE_VOLUMES="$(python3 - "$CFG" "$PROJECT" <<'PY'
import json, subprocess, sys
cfg, project = json.loads(sys.argv[1]), sys.argv[2]
declared = set()
for v in cfg.get("volumes", {}).values():
    name, opts = v.get("name"), v.get("driver_opts") or {}
    if not name or not opts.get("device"):
        continue
    declared.add(name)
    fmt = '{{index .Options "device"}}|{{index .Options "o"}}'
    out = subprocess.run(["docker", "volume", "inspect", name, "--format", fmt], capture_output=True, text=True)
    if out.returncode != 0:
        continue
    have_dev, _, have_o = out.stdout.strip().partition("|")
    want_dev, want_o = opts["device"], opts.get("o", "")
    if (have_dev, have_o) != (want_dev, want_o):
        print(f"{name}\t{have_dev} ({have_o})\t{want_dev} ({want_o})")
if project:
    out = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                         capture_output=True, text=True)
    for name in out.stdout.split():
        if name not in declared:
            print(f"{name}\t\t")
PY
)"
    if [ -n "$PROJECT" ]; then
      OLD_CONTAINERS="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
        --format '{{.Names}} {{.Label "com.docker.compose.service"}}' 2>/dev/null | awk '$2 != "station-wrapper-43" {print $1}')"
    fi
  fi
fi
if [ -n "$STALE_VOLUMES" ] || [ -n "$OLD_CONTAINERS" ]; then
  if [ "$MIGRATE" = 1 ]; then
    echo "Clearing the leftovers of an earlier layout from Docker (your folders are not touched):"
    docker compose down --remove-orphans >/dev/null 2>&1 || true
    # Compose only calls a container an orphan if it made it with its own bookkeeping labels, so
    # remove the old ones by name too. They belong to this project and no longer match the file.
    for c in $OLD_CONTAINERS; do
      docker rm -f "$c" >/dev/null 2>&1 && echo "  removed the container $c"
    done
    if [ -n "$STALE_VOLUMES" ]; then
      while IFS=$'\t' read -r name _have _want; do
        docker volume rm "$name" >/dev/null && echo "  removed the volume $name"
      done <<< "$STALE_VOLUMES"
    fi
  else
    echo "! Docker still has parts of an earlier layout, and 'docker compose up' would trip over them:" >&2
    [ -n "$OLD_CONTAINERS" ] && echo "  containers no longer in the compose file: $(echo $OLD_CONTAINERS)" >&2
    if [ -n "$STALE_VOLUMES" ]; then
      while IFS=$'\t' read -r name have want; do
        if [ -z "$want" ]; then
          echo "  volume $name is no longer in the compose file" >&2
        else
          echo "  volume $name is still set up as $have (the compose file now says $want)" >&2
        fi
      done <<< "$STALE_VOLUMES"
    fi
    echo "  Fix it with:  docker compose down --remove-orphans" >&2
    [ -n "$STALE_VOLUMES" ] && echo "                docker volume rm $(echo "$STALE_VOLUMES" | cut -f1 | tr '\n' ' ')" >&2
    echo "  or let this script do exactly that:  ./setup-dirs.sh --migrate" >&2
    echo "  Removing a volume only deletes Docker's note of the path; the folders and their contents stay." >&2
    exit 1
  fi
fi

if [ ! -d "$FS42_MEDIA_ROOT" ]; then
  echo "! FS42_MEDIA_ROOT ($FS42_MEDIA_ROOT) doesn't exist or isn't mounted yet." >&2
  echo "  This one holds your actual media, so it's not auto-created -- check the mount." >&2
  exit 1
fi

echo "Host directories ready:"
echo "  $CONFIG_ROOT           (config: runtime/ confs/ tuner/)"
echo "  $CATALOG_DIR           (catalog: by-genre/ tanfo/ media-links/)"
echo "  $FS42_MEDIA_ROOT (existing; your media, may be one or several shares)"
echo "The services will run as $(stat -c %u:%g "$CONFIG_ROOT") (the owner of $CONFIG_ROOT), not root. To use another account"
echo "set PUID and PGID in .env; it must be able to read $FS42_MEDIA_ROOT."
if [ "${RENAME_NEEDED:-0}" = 1 ]; then
  echo "! Remember to rename FS42_SSD_ROOT to FS42_LOCAL_ROOT in .env: docker compose needs the new name." >&2
  exit 1
fi
