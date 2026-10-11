#!/usr/bin/env python3
"""First start: put two stations on air, so a new install shows something and proves the whole chain.

"Test Pattern" on channel 2 plays a test card with a quiet tone, from a clip made here (ffmpeg, nothing
downloaded) into catalog/by-genre/Test Pattern/. "Channel Guide" on channel 1 is the scrolling guide of every
channel (drawn by the tuner, see guide_channel.py) with its default settings and no music; it needs no schedule.
Both are made through the console, like any station you would add yourself, so you can edit or delete them there.

It runs once. It does nothing if the install already has stations of its own (an existing setup is never
touched), and once it has run (or declined to), /config/.test-station-done stops it ever running again, so a
station you delete stays deleted. FIRSTRUN_TEST_STATION=0 and FIRSTRUN_GUIDE_STATION=0 turn each off.

Exit 0: done or not needed. 1: the console was not up in time (the supervisor tries again).
78: something is wrong that retrying will not fix (the message says what).
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.error

import build_channels as bc

NAME = "Test Pattern"
NUMBER = 2
GUIDE_NAME = "Channel Guide"
GUIDE_NUMBER = 1                 # not 0: Jellyfin and some other IPTV clients ignore channel 0
CLIP = "test-pattern.mp4"
CLIP_SECONDS = int(os.environ.get("FIRSTRUN_CLIP_SECONDS", "60"))     # how long the looped test card is
FLAG = os.path.join(bc.CONTAINER_CONFIG, ".test-station-done")
WAIT_SECONDS = 180
DAYS = 7                    # a day of schedule is only what is left of today; nothing here extends it later


def say(msg):
    print(f"firstrun: {msg}", flush=True)


ASSET = os.environ.get("FIRSTRUN_ASSET", "/opt/assets/test-pattern.mp4")      # the pre-rendered test card shipped in the image


def make_clip(path):
    """The test card at `path`: the pre-rendered clip from the image when it is there, else one made with ffmpeg."""
    if os.path.isfile(ASSET):
        shutil.copyfile(ASSET, path + ".part.mp4")
        os.replace(path + ".part.mp4", path)
        return
    render_clip(path)


def render_clip(path):
    """A one minute test card (SMPTE-style colour bars) with a quiet 1 kHz tone. The station loops it.

    Sixty seconds, not five: the dashboard starts an ffmpeg for every schedule entry and the schedule keeps
    growing, so a 5 s loop meant 17,280 entries (~2.7 MB) per day and an ffmpeg every 5 s; 60 s is 1,440."""
    tmp = path + ".part.mp4"
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "smptebars=size=640x480:rate=15",
           "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
           "-af", "volume=0.12", "-t", str(CLIP_SECONDS),
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-movflags", "+faststart", tmp]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise RuntimeError(f"ffmpeg could not make the test card: {out.stderr[-300:]}")
    os.replace(tmp, path)


def mark_done():
    try:
        with open(FLAG, "w") as f:
            f.write("The test station was made, or the install already had stations. Delete this file to allow it again.\n")
        if os.geteuid() == 0:           # the config folder is yours: give the file to whoever owns the folder
            ref = os.stat(bc.CONTAINER_CONFIG)
            os.chown(FLAG, ref.st_uid, ref.st_gid)
    except OSError as e:
        say(f"could not write {FLAG}: {e}")


def _on(var):
    return os.environ.get(var, "1").lower() not in ("0", "false", "no", "off")


def guide_conf():
    """The guide station with its default settings (the same keys the station form writes, see stationform.py)."""
    return {"network_name": GUIDE_NAME, "channel_number": GUIDE_NUMBER, "network_type": "guide",
            "messages": ["Welcome to the Channel Guide"], "play_sound": False, "scroll_speed": 1.0,
            "wrapper_guide": {"title": "Channel Guide", "hours": 3, "theme": "modern-blue", "music": ""}}


def main(argv=None):
    test_on, guide_on = _on("FIRSTRUN_TEST_STATION"), _on("FIRSTRUN_GUIDE_STATION")
    if not test_on and not guide_on:
        say("the first-start stations are switched off (FIRSTRUN_TEST_STATION=0, FIRSTRUN_GUIDE_STATION=0)")
        return 0
    if os.path.exists(FLAG):
        return 0
    console = bc.Console("http://127.0.0.1:4242")
    deadline = time.time() + WAIT_SECONDS
    while True:
        try:
            existing = console.stations()
            break
        except (urllib.error.URLError, OSError):
            if time.time() > deadline:
                say("the console did not come up in time; will try again")
                return 1
            time.sleep(3)
    others = [n for n in existing if n not in (NAME, GUIDE_NAME)]       # ours, left by an attempt that stopped halfway, don't count
    if others:
        say(f"this install already has {len(others)} station(s), so no first-start stations are added")
        mark_done()
        return 0

    try:
        if test_on and NAME not in existing:
            make_test_station(console)
        if guide_on and GUIDE_NAME not in existing:
            console.ensure_station(guide_conf(), existing)            # drawn live: no catalog or schedule to build
    except Exception as e:      # whatever it was, retrying on every start would not fix it
        say(f"could not set up the first-start stations: {e}")
        return 78
    mark_done()
    if test_on:
        say(f"channel {NUMBER} ({NAME}) is on air. Edit or delete it in the console; it will not come back.")
    if guide_on:
        say(f"channel {GUIDE_NUMBER} ({GUIDE_NAME}) is on air. Change its style, messages and music on the Stations page.")
    return 0


def make_test_station(console):
    tag_dir = os.path.join(bc.CONTAINER_CATALOG, bc.FARM, NAME)
    clip = os.path.join(tag_dir, CLIP)
    if not os.path.exists(clip):
        os.makedirs(tag_dir, exist_ok=True)
        make_clip(clip)
    # A "loop" station repeats the one clip all day: no bump clips, commercials or time slots to fit.
    conf = {"network_name": NAME, "channel_number": NUMBER, "network_type": "loop", "shuffle_loop": False,
            "content_dir": f"catalog/{bc.FARM}/{NAME}"}
    console.ensure_station(conf, {})
    console.rebuild_catalog(NAME)
    for _ in range(DAYS):
        bc.add_first_day(None, NAME, console)
    bc.adopt_owner(os.path.join(bc.CONTAINER_CATALOG, bc.FARM), bc.CONTAINER_CATALOG)


if __name__ == "__main__":
    sys.exit(main())
