"""Even out the volume (loudness.py). Run: python3 test_loudness.py (the real-ffmpeg test needs ffmpeg)"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import loudness as L

SUMMARY = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -27.4 LUFS
    Threshold: -37.6 LUFS

  Loudness range:
    LRA:         6.1 LU

  True peak:
    Peak:       -3.2 dBFS
"""


class Math(unittest.TestCase):
    def test_the_summary_is_read(self):
        self.assertEqual(L.parse_ebur128("noise\n" + SUMMARY), (-27.4, -3.2))
        self.assertEqual(L.parse_ebur128("  I:   -inf LUFS\n  Peak:  -inf dBFS"), (None, None))

    def test_the_gain_reaches_the_target_within_limits(self):
        self.assertEqual(L.gain_db(-27.4, -3.2), 4.4)                  # quiet: raised
        self.assertEqual(L.gain_db(-16.0, -0.5), -7.0)                 # loud: lowered
        self.assertEqual(L.gain_db(-45.0, -30.0), L.MAX_BOOST)         # very quiet: not boosted without limit
        self.assertEqual(L.gain_db(-8.0, 0.0), -15.0)
        self.assertIsNone(L.gain_db(None, None))                       # no audio
        self.assertEqual(L.gain_db(-30.0, -1.0, target=-23.0), 3.0)    # its peak sets how far it may go: 3 dB over -1 dBTP

    def test_the_filter_is_left_out_when_there_is_nothing_to_do(self):
        self.assertIsNone(L.filter_for(None))
        self.assertIsNone(L.filter_for(0.1))
        f = L.filter_for(-6.5)
        self.assertTrue(f.startswith("volume=-6.50dB,alimiter="))


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.video = os.path.join(self.dir, "a.mp4")
        open(self.video, "wb").write(b"x" * 100)
        self.calls = []
        self.levels = L.Levels(os.path.join(self.dir, "loudness.json"), self.dir, target_fn=lambda: -23.0,
                               measure_fn=self.fake)

    def fake(self, path):
        self.calls.append(path)
        return {"lufs": -30.0, "peak": -5.0}

    def test_a_file_is_measured_once_and_then_has_a_filter(self):
        self.assertIsNone(self.levels.filter_for(self.video))         # not measured: plays as it is, and is queued
        self.assertTrue(self.levels.work_once())
        self.assertIn("volume=+7.00dB", self.levels.filter_for(self.video))
        self.levels.submit(self.video)
        self.assertFalse(self.levels.work_once())                     # fresh: not measured again
        self.assertEqual(len(self.calls), 1)

    def test_a_changed_file_is_measured_again_and_a_restart_remembers(self):
        self.levels.filter_for(self.video)
        self.levels.work_once()
        self.levels.save()
        again = L.Levels(os.path.join(self.dir, "loudness.json"), self.dir, measure_fn=self.fake)
        self.assertIsNotNone(again.filter_for(self.video))            # remembered across a restart
        open(self.video, "wb").write(b"y" * 5000)                     # the file was replaced
        os.utime(self.video, (time.time() + 10, time.time() + 10))
        self.assertIsNone(again.filter_for(self.video))               # no longer the one that was measured
        self.assertTrue(again.queue)

    def test_a_file_that_cannot_be_read_is_not_tried_again_at_once(self):
        def broken(path):
            raise RuntimeError("could not read the audio")
        lv = L.Levels(os.path.join(self.dir, "l2.json"), self.dir, measure_fn=broken)
        lv.submit(self.video)
        lv.work_once()
        lv.submit(self.video)
        self.assertFalse(lv.queue)
        self.assertEqual(lv.status()["failed"], 1)

    def test_the_sweep_queues_every_video_in_the_lists(self):
        os.makedirs(os.path.join(self.dir, "List", "Show"))
        for n in ("b.mkv", "c.mp4", "notes.txt", ".hidden.mp4"):
            open(os.path.join(self.dir, "List", "Show", n), "wb").write(b"z")
        self.levels.sweep()
        names = sorted(os.path.basename(k) for k in self.levels.queue)
        self.assertEqual(names, ["a.mp4", "b.mkv", "c.mp4"])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RealFfmpeg(unittest.TestCase):
    def tone(self, path, volume):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=440:sample_rate=48000,volume={volume}", "-t", "6", path], check=True, capture_output=True)

    def loudness_after(self, path, af):
        out = os.path.join(os.path.dirname(path), "out.wav")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path] + (["-af", af] if af else []) + [out]
        subprocess.run(cmd, check=True, capture_output=True)
        return L.measure(out, low_priority=False)["lufs"]

    def test_a_quiet_file_and_a_loud_one_end_up_at_the_same_level(self):
        d = tempfile.mkdtemp()
        quiet, loud = os.path.join(d, "quiet.wav"), os.path.join(d, "loud.wav")
        self.tone(quiet, 0.3)
        self.tone(loud, 1.6)
        results = []
        for f in (quiet, loud):
            m = L.measure(f, low_priority=False)
            self.assertIsNotNone(m["lufs"])
            af = L.filter_for(L.gain_db(m["lufs"], m["peak"], -23.0))
            results.append((m["lufs"], self.loudness_after(f, af)))
        (q_before, q_after), (l_before, l_after) = results
        self.assertGreater(l_before - q_before, 10)                    # they were far apart
        self.assertLess(abs(q_after - l_after), 1.5)                   # and are not now
        self.assertLess(abs(l_after - (-23.0)), 1.5)


if __name__ == "__main__":
    unittest.main()
