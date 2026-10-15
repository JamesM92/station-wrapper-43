"""Warm start: the piece after the current one is opened and seeked while the current one plays, then joined on time.

Run: python3 test_warmstart.py (needs ffmpeg)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("FS42_TUNER_CONF", os.path.join(tempfile.mkdtemp(), "c.json"))
json.dump({"db_path": "none.db", "catalog_root": tempfile.mkdtemp(), "channels": {}, "media_root": "/tmp", "port": 5098,
           "advertise_url": "http://127.0.0.1:5098"}, open(os.environ["FS42_TUNER_CONF"], "w"))
sys.argv = ["tuner.py"]
import tuner  # noqa: E402

CLIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "test-pattern.mp4")


class Command(unittest.TestCase):
    def test_a_plain_segment_is_unchanged(self):
        cmd = tuner.segment_cmd("/m/a.mkv", 120.0, 300.0, False)
        i = cmd.index("-i")
        self.assertEqual(cmd[i - 2:i + 4], ["-ss", "120.000", "-i", "/m/a.mkv", "-t", "300.000"])
        self.assertEqual(cmd.count("-ss"), 1)

    def test_a_warm_segment_reads_from_earlier_and_drops_what_it_read_before_it_is_due(self):
        cmd = tuner.segment_cmd("/m/a.mkv", 120.0, 300.0, False, lead=4.0)
        i = cmd.index("-i")
        self.assertEqual(cmd[i - 2:i + 6], ["-ss", "116.000", "-i", "/m/a.mkv", "-ss", "4.000", "-t", "300.000"])

    def test_the_lead_never_reads_from_before_the_start_of_the_file(self):
        cmd = tuner.segment_cmd("/m/a.mkv", 2.0, 60.0, False, lead=4.0)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "0.000")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Timing(unittest.TestCase):
    def run_cmd(self, cmd):
        began = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        first, total = None, 0
        while True:
            c = p.stdout.read(188 * 64)
            if not c:
                break
            if first is None:
                first = time.time() - began
            total += len(c)
        p.wait()
        return first, time.time() - began, total

    def test_a_warm_segment_says_the_same_thing_but_a_lead_later(self):
        plain = self.run_cmd(tuner.segment_cmd(CLIP, 10.0, 3.0, False))
        warm = self.run_cmd(tuner.segment_cmd(CLIP, 10.0, 3.0, False, lead=2.0))
        self.assertAlmostEqual(warm[2], plain[2], delta=plain[2] * 0.05)           # the same picture and sound
        self.assertGreater(warm[0] - plain[0], 1.5)                               # but not before it is due
        self.assertLess(warm[0] - plain[0], 2.6)


class Handover(unittest.TestCase):
    def channel(self):
        ch = tuner.Channel(77, "Toons", tuner.POOL if hasattr(tuner, "POOL") else None)
        return ch

    def cursor(self, path, skip, offset=0.1):
        return SimpleNamespace(entry=SimpleNamespace(path=path, skip=skip), offset=offset)

    def test_the_piece_started_early_is_taken_when_it_is_the_one_due(self):
        ch = self.channel()
        proc = subprocess.Popen(["sleep", "30"])
        ch._warm = {"proc": proc, "path": "/m/a.mkv", "skip": 900.0, "local": "/m/a.mkv", "pinned": False, "began": time.time(), "lead": 4.0}
        got = ch._take_warm("/m/a.mkv", self.cursor("/m/a.mkv", 900.0))
        self.assertIs(got["proc"], proc)
        self.assertIsNone(ch._warm)
        proc.kill()
        proc.wait()

    def test_another_piece_is_dropped_and_its_process_stopped(self):
        ch = self.channel()
        proc = subprocess.Popen(["sleep", "30"])
        ch._warm = {"proc": proc, "path": "/m/a.mkv", "skip": 900.0, "local": "/m/a.mkv", "pinned": False, "began": time.time(), "lead": 4.0}
        self.assertIsNone(ch._take_warm("/m/b.mkv", self.cursor("/m/b.mkv", 0.0)))
        self.assertIsNotNone(proc.wait(timeout=5))                              # it was stopped, not left running

    def test_the_same_file_at_another_place_is_not_taken(self):
        ch = self.channel()
        proc = subprocess.Popen(["sleep", "30"])
        ch._warm = {"proc": proc, "path": "/m/a.mkv", "skip": 900.0, "local": "/m/a.mkv", "pinned": False, "began": time.time(), "lead": 4.0}
        self.assertIsNone(ch._take_warm("/m/a.mkv", self.cursor("/m/a.mkv", 1500.0)))
        proc.wait(timeout=5)

    def test_a_piece_from_the_start_of_its_file_is_started_just_early_enough(self):
        """A plain command has no lead: its first picture comes about WARM_STARTUP after it is started, so starting it that much
        early puts the first picture at the join."""
        cmd = tuner.segment_cmd("/m/ad.mp4", 0.0, 30.0, False, lead=0.0)
        self.assertEqual(cmd.count("-ss"), 1)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "0.000")

    def test_dropping_with_nothing_started_is_harmless(self):
        self.channel()._drop_warm()


if __name__ == "__main__":
    unittest.main()
