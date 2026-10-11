#!/usr/bin/env python3
"""Refresh one station's catalog (new videos in, missing ones out) WITHOUT touching its schedule.

FieldStation42's own rebuild deletes the station's schedule first, because it rebuilds the catalog from nothing.
Rebuilding the catalog is safe to do on its own: the catalog is replaced in one step, and the days already
scheduled keep the files they were built with. The new videos are simply there the next time days are added to
the schedule (by the schedule keeper, or the Add days button), which is what you want when the schedule is a week
long and you only added some videos.

Runs inside the container, from FieldStation42's own folder (it reads its configs and database relative to it):
    python3 update_catalog.py "<station name>"
Prints one line when it is done; exits non-zero with the reason otherwise. The schedule database is not written.
"""
import os
import sys


def main(argv):
    if len(argv) != 2 or not argv[1].strip():
        print('usage: update_catalog.py "<station name>"', file=sys.stderr)
        return 2
    name = argv[1]
    app = os.environ.get("FS42_APP", "/app")
    try:
        os.chdir(app)
        sys.path.insert(0, app)
        from fs42.catalog import ShowCatalog
        from fs42.station_manager import StationManager
        station = StationManager().station_by_name(name)
        if not station:
            print(f"there is no station called '{name}'", file=sys.stderr)
            return 3
        ShowCatalog.clear_fluid_cache()                  # look at the folders again, not at what was seen earlier
        ShowCatalog(station, rebuild_catalog=True)       # replaces the catalog in one step; no schedule is touched
    except Exception as e:                               # the reason goes back to the page
        print(f"could not update the catalog: {e}", file=sys.stderr)
        return 1
    print(f"Catalog updated for {name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
