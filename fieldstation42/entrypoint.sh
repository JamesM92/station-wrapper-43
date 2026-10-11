#!/bin/bash
# Replaces install.sh for a container: that script is interactive (reads a
# key press, sets up a venv) and neither applies here. This checks where the
# catalog and the config live, links runtime/ and confs/ into the config volume, clears the
# whole-folder links an earlier version put in the catalog, does the
# parts of install.sh that still matter for a fresh runtime/, seeds the tuner's config, and
# starts the supervisor.
set -euo pipefail

# 0. The catalog must NOT be an SMB/CIFS share; local disk is the place for it.
#    It is a folder of symlinks into your media. SMB shares refuse symlinks or
#    follow them on the server, so every link would be missing or dead, and the
#    schedule would quietly fill with nothing. Refuse to start rather than run
#    like that. Your media can stay on the share: only the catalog must be local.
#    FS42_ALLOW_NETWORK_CATALOG=1 overrides it, for a share you have tested.
mkdir -p /app/catalog
catalog_fs="$(stat -f -c %T /app/catalog 2>/dev/null || echo unknown)"
case "$catalog_fs" in
  cifs|smb|smb2|smb3|smbfs) catalog_is_smb=1 ;;
  *) catalog_is_smb=0 ;;
esac
# A real symlink to a file outside the folder, followed: what every catalog link is.
catalog_links_work=0
if probe_dir="$(mktemp -d -p /app/catalog .symlink-probe-XXXXXX 2>/dev/null)"; then
  probe_target="$(mktemp -d)"
  echo ok > "$probe_target/t"
  if ln -s "$probe_target/t" "$probe_dir/link" 2>/dev/null && [ "$(cat "$probe_dir/link" 2>/dev/null)" = ok ]; then
    catalog_links_work=1
  fi
  rm -rf "$probe_dir" "$probe_target"
fi
if [ "$catalog_is_smb" = 1 ] || [ "$catalog_links_work" = 0 ]; then
  if [ "$catalog_is_smb" = 1 ]; then
    echo "entrypoint: /app/catalog is on an SMB/CIFS share ($catalog_fs)." >&2
  else
    echo "entrypoint: /app/catalog cannot hold working symlinks ($catalog_fs)." >&2
  fi
  echo "entrypoint: The catalog is a folder of symlinks. On a share they are refused or resolve on the server." >&2
  echo "entrypoint: Put the catalog on LOCAL DISK: set FS42_LOCAL_ROOT in .env (the catalog is \$FS42_LOCAL_ROOT/fs42/catalog)." >&2
  echo "entrypoint: Your media can stay on the share. Only the catalog must be local." >&2
  if [ "${FS42_ALLOW_NETWORK_CATALOG:-}" = 1 ]; then
    echo "entrypoint: FS42_ALLOW_NETWORK_CATALOG=1 is set, so starting anyway." >&2
  else
    echo "entrypoint: REFUSING TO START. To override on a share you have tested: FS42_ALLOW_NETWORK_CATALOG=1." >&2
    exit 78
  fi
elif [ "$catalog_fs" = nfs ] || [ "$catalog_fs" = nfs4 ] || [ "$catalog_fs" = fuse.sshfs ] || [ "$catalog_fs" = 9p ]; then
  echo "entrypoint: note: /app/catalog is on a network filesystem ($catalog_fs). Symlinks work here, but" \
       "local disk is safer." >&2
fi

# 0b. The config volume (/config) holds everything FieldStation42 and the dashboard keep:
#     runtime/ (the sqlite schedule database and sockets), confs/ (stations), tuner/ (the
#     dashboard's config). It must be local disk too: sqlite misbehaves over SMB/NFS.
mkdir -p /config/runtime /config/confs /config/tuner
config_fs="$(stat -f -c %T /config 2>/dev/null || echo unknown)"
case "$config_fs" in
  cifs|smb|smb2|smb3|smbfs|nfs|nfs4|fuse.sshfs|9p)
    echo "entrypoint: /config is on a network share ($config_fs). It holds the sqlite schedule database," >&2
    echo "entrypoint: which misbehaves over SMB/NFS. Put the config volume on LOCAL disk (FS42_LOCAL_ROOT)." >&2
    if [ "${FS42_ALLOW_NETWORK_CATALOG:-}" = 1 ]; then
      echo "entrypoint: FS42_ALLOW_NETWORK_CATALOG=1 is set, so starting anyway." >&2
    else
      echo "entrypoint: REFUSING TO START. To override on a share you have tested: FS42_ALLOW_NETWORK_CATALOG=1." >&2
      exit 78
    fi ;;
esac
# FieldStation42 reads runtime/ and confs/ relative to /app, so link them into the config
# volume. A real directory already there (a volume mounted straight onto it, as an older
# compose file did) is left alone and used as it is.
for d in runtime confs; do
  if [ -L "/app/$d" ] || [ ! -e "/app/$d" ]; then
    ln -sfn "/config/$d" "/app/$d"
  else
    echo "entrypoint: /app/$d is a real directory (an older layout?); using it as it is, not /config/$d" >&2
  fi
done

# 1. Seed the runtime assets FS42 ships in docs/ and expects to find under
#    runtime/. Several of the example station confs reference these by a
#    hardcoded relative path (standby.png, brb.png, ...) and
#    fs42/station_io.py hard-crashes StationManager() -- which every
#    station_42.py code path instantiates -- if a referenced file is
#    missing. Only copied when absent, so a user's own customization is
#    never overwritten by a container restart.
mkdir -p /app/catalog

for f in static.mp4 standby.png brb.png off_air_pattern.mp4 signoff.mp4; do
  if [ ! -e "/app/runtime/$f" ]; then
    if [ -e "/app/docs/$f" ]; then
      cp "/app/docs/$f" "/app/runtime/$f"
    else
      # The sample videos are not committed to this repo (see .gitignore).
      echo "entrypoint: /app/docs/$f not in image, skipping" >&2
    fi
  fi
done
if [ ! -d /app/runtime/logo_images ] && [ -d /app/docs/logo_images ]; then
  cp -r /app/docs/logo_images /app/runtime/logo_images
fi
if [ ! -e /app/runtime/channel.socket ]; then
  touch /app/runtime/channel.socket
fi

# 2. The catalog holds REAL folders, with only the files inside them linked (by-genre/, tanfo/,
#    media-links/). An earlier version also linked each whole media folder into it
#    (catalog/anime -> /mnt/media/anime/). A link to a whole folder sends a file manager
#    straight into the real media instead of showing a catalog, so none are made now, and the ones
#    that version made are removed. Only a top-level link pointing at exactly /mnt/media/<its own
#    name>/ goes, which is precisely what it made; any other link is left alone.
for link in /app/catalog/*; do
  [ -L "$link" ] || continue
  name="$(basename "$link")"
  if [ "$(readlink "$link")" = "/mnt/media/$name/" ]; then
    rm -f "$link" && echo "entrypoint: removed the old whole-folder link /app/catalog/$name (the catalog holds real folders; only files are linked)"
  fi
done

# 2b. A starter structure, so a new catalog is not just raw media links: by-genre/ is where
#     FieldStation42 reads its tag folders from, and by-genre/bump/ holds the channel mid cards every
#     station needs. The bump folder gets one clip (the 5 second Station Wrapper 43 splash) the first time
#     it is created, so a station builds out of the box; replace it with your own, or add more.
#     Seeded only when the folder is first made, so deleting the clip later is respected. The
#     new folders take the owner of the catalog folder itself (yours), not root's, so you can
#     add clips from the host without sudo.
if [ ! -e /app/catalog/by-genre ]; then
  mkdir -p /app/catalog/by-genre && chown --reference=/app/catalog /app/catalog/by-genre 2>/dev/null
  echo "entrypoint: created /app/catalog/by-genre (the tag folders FieldStation42 schedules from)"
fi
if [ -d /app/catalog/by-genre ] && [ ! -e /app/catalog/by-genre/bump ]; then
  mkdir -p /app/catalog/by-genre/bump && chown --reference=/app/catalog /app/catalog/by-genre/bump 2>/dev/null
  # the clip is the Station Wrapper 43 splash card (5 seconds), pre-rendered in the image (/opt/assets); a test card
  # is made with ffmpeg if the image somehow lacks it
  if cp /opt/assets/sw43-splash.mp4 /app/catalog/by-genre/bump/sw43-splash.mp4 2>/dev/null \
     || ffmpeg -nostdin -loglevel error -y \
          -f lavfi -i "smptebars=size=640x480:rate=30" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
          -t 5 -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest -movflags +faststart \
          /app/catalog/by-genre/bump/sw43-splash.mp4 2>/dev/null; then
    chown --reference=/app/catalog /app/catalog/by-genre/bump/sw43-splash.mp4 2>/dev/null
    echo "entrypoint: created /app/catalog/by-genre/bump (the channel mid cards) with the Station Wrapper 43 splash (5 seconds; add your own clips)"
  else
    rm -f /app/catalog/by-genre/bump/sw43-splash.mp4
    echo "entrypoint: created /app/catalog/by-genre/bump (could not make a channel mid card; add your own)" >&2
  fi
fi

# 3. The tuner's config lives in the config volume, so the generator can edit it from the
#    host. Create it from the image's default the first time, never overwrite it.
if [ ! -e /config/tuner/config.json ]; then
  if cp /opt/tuner-config.default.json /config/tuner/config.json 2>/dev/null; then
    # The container runs as root, but scripts/build_channels.py runs on the host as you and
    # must be able to write this file. It holds no secrets.
    chmod 666 /config/tuner/config.json
    echo "entrypoint: created /config/tuner/config.json from the default"
  else
    echo "entrypoint: could not create /config/tuner/config.json (is /config read-only?); the tuner will not start" >&2
  fi
fi

# 4. Say which build this is, last of the startup messages and just above the services' own
#    output, so nobody has to scroll to find it. Then run everything (the console, the tuner,
#    and whichever of sync / ta-nfo are switched on) under a small supervisor that keeps them
#    up. tini, the real PID 1, reaps strays.
echo "entrypoint: Station Wrapper 43 ${FS42_WRAPPER_REF:-dev} (commit ${FS42_WRAPPER_SHA:-unknown}, built ${FS42_WRAPPER_BUILT:-unknown})"
echo "entrypoint: contains FieldStation42 $(cut -c1-7 /opt/FS42_REF 2>/dev/null || echo unknown) and TA_NFOWriter ${TA_NFOWRITER_VERSION:-unknown}"
exec python3 /opt/supervisor/supervisor.py
