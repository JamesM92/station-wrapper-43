"""Tests for firstrun.py: the test station on a new install. Run: python3 test_firstrun.py"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error

import build_channels as bc
import firstrun


class FakeConsole:
    stations_now = {}
    log = []
    unreachable = False

    def __init__(self, base):
        pass

    def stations(self):
        if FakeConsole.unreachable:
            raise urllib.error.URLError("refused")
        return dict(FakeConsole.stations_now)

    def ensure_station(self, conf, existing):
        FakeConsole.log.append(("save", conf))
        FakeConsole.stations_now[conf["network_name"]] = conf
        return "station created"

    def rebuild_catalog(self, name):
        FakeConsole.log.append(("rebuild", name))

    def add_day(self, name):
        FakeConsole.log.append(("day", name))


class FirstRunTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.config = os.path.join(self.root, "config")
        self.catalog = os.path.join(self.root, "catalog")
        os.makedirs(self.config)
        os.makedirs(self.catalog)
        FakeConsole.stations_now, FakeConsole.log, FakeConsole.unreachable = {}, [], False
        self.saved = (bc.Console, bc.add_first_day, bc.CONTAINER_CATALOG, firstrun.FLAG, firstrun.make_clip,
                      firstrun.WAIT_SECONDS, os.environ.get("FIRSTRUN_TEST_STATION"))
        bc.Console = FakeConsole
        bc.CONTAINER_CATALOG = self.catalog
        firstrun.FLAG = os.path.join(self.config, ".test-station-done")
        self.clips = []
        firstrun.make_clip = lambda path: (self.clips.append(path), open(path, "w").write("clip"))
        firstrun.WAIT_SECONDS = 0
        os.environ.pop("FIRSTRUN_TEST_STATION", None)

    def tearDown(self):
        (bc.Console, bc.add_first_day, bc.CONTAINER_CATALOG, firstrun.FLAG, firstrun.make_clip,
         firstrun.WAIT_SECONDS, env) = self.saved
        if env is None:
            os.environ.pop("FIRSTRUN_TEST_STATION", None)
        else:
            os.environ["FIRSTRUN_TEST_STATION"] = env
        shutil.rmtree(self.root)

    def run_it(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return firstrun.main(), buf.getvalue()

    def test_a_new_install_gets_channel_0_playing_a_test_card(self):
        code, out = self.run_it()
        self.assertEqual(code, 0)
        (op, conf), rebuild = FakeConsole.log[0], FakeConsole.log[1]
        self.assertEqual((op, conf["network_name"], conf["channel_number"]), ("save", "Test Pattern", 2))
        self.assertEqual(conf["network_type"], "loop")                           # one clip, repeated all day
        self.assertEqual(conf["content_dir"], "catalog/by-genre/Test Pattern")   # just the folder holding the clip
        self.assertNotIn("day_templates", conf)                                  # no slots, bumps or commercials to fit
        self.assertEqual(rebuild, ("rebuild", "Test Pattern"))
        # the days are added THROUGH the console (so its own pages show the schedule), several so it lasts a while
        self.assertEqual([e for e in FakeConsole.log if e[0] == "day"], [("day", "Test Pattern")] * firstrun.DAYS)
        self.assertGreaterEqual(firstrun.DAYS, 7)
        self.assertEqual(self.clips, [os.path.join(self.catalog, "by-genre", "Test Pattern", "test-pattern.mp4")])
        self.assertTrue(os.path.exists(firstrun.FLAG))
        self.assertIn("channel 2 (Test Pattern)", out)
        guide = FakeConsole.stations_now["Channel Guide"]                        # and the guide, with its default settings
        self.assertEqual((guide["channel_number"], guide["network_type"], guide["play_sound"]), (1, "guide", False))
        self.assertEqual(guide["wrapper_guide"], {"title": "Channel Guide", "hours": 3, "theme": "modern-blue", "music": ""})
        self.assertNotIn(("rebuild", "Channel Guide"), FakeConsole.log)          # drawn live: nothing to build
        self.assertIn("channel 1 (Channel Guide)", out)

    def test_each_first_start_station_has_its_own_switch(self):
        os.environ["FIRSTRUN_GUIDE_STATION"] = "0"
        try:
            self.run_it()
            self.assertEqual(list(FakeConsole.stations_now), ["Test Pattern"])
            FakeConsole.stations_now.clear()
            os.remove(firstrun.FLAG)
            os.environ["FIRSTRUN_GUIDE_STATION"] = "1"
            os.environ["FIRSTRUN_TEST_STATION"] = "0"
            self.run_it()
            self.assertEqual(list(FakeConsole.stations_now), ["Channel Guide"])
            self.assertEqual(self.clips, [os.path.join(self.catalog, "by-genre", "Test Pattern", "test-pattern.mp4")])    # only the first run made the clip
        finally:
            os.environ.pop("FIRSTRUN_GUIDE_STATION", None)

    def test_a_retry_after_a_half_made_start_still_finishes(self):
        FakeConsole.stations_now = {"Test Pattern": {"network_name": "Test Pattern", "channel_number": 0}}
        code, _ = self.run_it()
        self.assertEqual(code, 0)
        self.assertIn("Channel Guide", FakeConsole.stations_now)

    def test_it_runs_once_and_a_deleted_station_stays_deleted(self):
        self.run_it()
        FakeConsole.stations_now.clear()                    # the user deletes it in the console
        FakeConsole.log.clear()
        code, _ = self.run_it()
        self.assertEqual((code, FakeConsole.log), (0, []))

    def test_an_install_that_already_has_stations_is_left_alone(self):
        FakeConsole.stations_now = {"Mine": {"network_name": "Mine", "channel_number": 5}}
        code, out = self.run_it()
        self.assertEqual((code, FakeConsole.log, self.clips), (0, [], []))
        self.assertIn("already has 1 station", out)
        self.assertTrue(os.path.exists(firstrun.FLAG))      # and it never asks again, even if they delete theirs

    def test_it_can_be_switched_off(self):
        os.environ["FIRSTRUN_TEST_STATION"] = "0"
        os.environ["FIRSTRUN_GUIDE_STATION"] = "0"
        try:
            code, out = self.run_it()
        finally:
            os.environ.pop("FIRSTRUN_GUIDE_STATION", None)
        self.assertEqual((code, FakeConsole.log), (0, []))
        self.assertFalse(os.path.exists(firstrun.FLAG))     # switching it back on later still works

    def test_a_console_that_is_not_up_yet_is_retried_not_given_up_on(self):
        FakeConsole.unreachable = True
        code, out = self.run_it()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(firstrun.FLAG))
        self.assertIn("try again", out)

    def test_a_failure_is_reported_and_does_not_leave_the_flag_so_it_can_be_fixed(self):
        def boom(path):
            raise RuntimeError("ffmpeg could not make the test card")
        firstrun.make_clip = boom
        code, out = self.run_it()
        self.assertEqual(code, 78)
        self.assertIn("could not set up the first-start stations", out)
        self.assertFalse(os.path.exists(firstrun.FLAG))
        self.assertEqual(FakeConsole.log, [])                # no half-made station

    def test_an_existing_clip_is_kept_not_made_again(self):
        d = os.path.join(self.catalog, "by-genre", "Test Pattern")
        os.makedirs(d)
        open(os.path.join(d, "test-pattern.mp4"), "w").write("mine")
        self.run_it()
        self.assertEqual(self.clips, [])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RealClipTest(unittest.TestCase):
    def test_the_pre_rendered_clip_in_the_image_is_used_when_it_is_there(self):
        d = tempfile.mkdtemp()
        asset = os.path.join(d, "asset.mp4")
        open(asset, "wb").write(b"PRERENDERED")
        saved, firstrun.ASSET = firstrun.ASSET, asset
        try:
            out = os.path.join(d, "t.mp4")
            firstrun.make_clip(out)
            self.assertEqual(open(out, "rb").read(), b"PRERENDERED")                       # copied, nothing rendered
        finally:
            firstrun.ASSET = saved
            shutil.rmtree(d)

    def test_the_clip_is_a_real_playable_video_with_sound(self):
        d = tempfile.mkdtemp()
        saved, firstrun.CLIP_SECONDS = firstrun.CLIP_SECONDS, 2
        try:
            path = os.path.join(d, "t.mp4")
            firstrun.make_clip(path)
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height:format=duration",
                                  "-of", "default=nw=1", path], capture_output=True, text=True).stdout
            self.assertIn("codec_name=h264", out)
            self.assertIn("codec_name=aac", out)
            self.assertIn("width=640", out)
            self.assertAlmostEqual(float(out.split("duration=")[1].split()[0]), 2.0, delta=0.3)
            self.assertEqual(os.listdir(d), ["t.mp4"])         # no temp file left behind
        finally:
            firstrun.CLIP_SECONDS = saved
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
