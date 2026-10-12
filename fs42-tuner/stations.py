"""The stations FieldStation42 knows about, read from its own station configs.

A station you add in FieldStation42's console is saved as confs/<name>.json with a
`station_conf` holding its `network_name` and `channel_number`. The dashboard and the
supervisor read those, so a new station shows up without anyone editing a channel list.
"""
import glob
import json
import os
import tempfile
import time


def discover(confs_dir):
    """{channel number (text): station name} for every station config in confs_dir.

    Files that are not station configs (main_config.json), or that can't be read, are skipped.
    """
    found = {}
    for path in sorted(glob.glob(os.path.join(confs_dir, "*.json"))):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        conf = data.get("station_conf") if isinstance(data, dict) else None
        if not isinstance(conf, dict):
            continue
        name, number = conf.get("network_name"), conf.get("channel_number")
        if isinstance(name, str) and name and isinstance(number, (int, str)) and not isinstance(number, bool) \
                and str(number).strip():
            found[str(number).strip()] = name
    return found


def station_confs(confs_dir):
    """{station name: its config} for every station config in confs_dir (the same files discover() reads)."""
    found = {}
    for path in sorted(glob.glob(os.path.join(confs_dir, "*.json"))):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        conf = data.get("station_conf") if isinstance(data, dict) else None
        if isinstance(conf, dict) and isinstance(conf.get("network_name"), str):
            found[conf["network_name"]] = conf
    return found


# ---- "a build is running": the dashboard marks it, the supervisor waits for it before restarting the dashboard.
# Restarting the dashboard wipes the builds it is running (they live in its memory), and a new station makes the
# supervisor restart it, so without this a station built right after being created would be cut off halfway.
JOBS_BUSY = os.environ.get("FS42_JOBS_BUSY", os.path.join(tempfile.gettempdir(), "fs42-jobs.busy"))
BUSY_MAX_AGE = 60                      # a mark not refreshed for this long is left over from a crash: ignore it


def mark_busy(path=None):
    try:
        with open(path or JOBS_BUSY, "w"):
            pass
    except OSError:
        pass


def clear_busy(path=None):
    try:
        os.unlink(path or JOBS_BUSY)
    except OSError:
        pass


def is_busy(path=None, max_age=BUSY_MAX_AGE, now=time.time):
    try:
        return now() - os.stat(path or JOBS_BUSY).st_mtime < max_age
    except OSError:
        return False
