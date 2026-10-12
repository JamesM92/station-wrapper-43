#!/bin/sh
# Keeps catalog/by-genre filled, on its own: tag folders of links from your media (genres from the
# NFO files, one folder for each TubeArchivist channel, bump). Add-only: it never removes or rewrites
# a link, so what you arrange by hand stays. It does NOT create stations, number channels or touch
# schedules: that is FieldStation42's own console (:4242). You open it, add a station, and pick one
# of the tag folders this made.
#
# Runs at start, then every FARM_INTERVAL_MINUTES, and right after ta-nfo finishes a pass (it leaves
# farm.now in the private run folder), so a new YouTube channel gets its folder without waiting.
set -eu

INTERVAL="${FARM_INTERVAL_MINUTES:-30}"
ARGS="${FARM_ARGS:-}"
FLAG="${FS42_FARM_NOW:-${FS42_RUN_DIR:-${TMPDIR:-/tmp}/fs42-run-$(id -u)}/farm.now}"

stop() { echo "farm: stopping"; exit 0; }
trap stop TERM INT

last=0
while true; do
  now="$(date +%s)"
  if [ -e "$FLAG" ] || [ $((now - last)) -ge $((INTERVAL * 60)) ]; then
    rm -f "$FLAG"
    echo "farm: $(date '+%Y-%m-%d %H:%M:%S') preparing /app/catalog/by-genre"
    # shellcheck disable=SC2086   # ARGS is a list of options on purpose
    if python3 /opt/wrapper/build_channels.py --links-only $ARGS; then
      :
    else
      echo "farm: that pass reported an error (exit $?); will try again next time" >&2
    fi
    last="$(date +%s)"
  fi
  [ "${FARM_RUN_ONCE:-}" = 1 ] && exit 0
  sleep 10 &
  wait $!
done
