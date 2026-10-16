"""The words in the bottom right corner that say why a test pattern is on screen. Run: python3 test_overlay.py (needs ffmpeg)"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

os.environ.setdefault("FS42_TUNER_CONF", os.path.join(tempfile.mkdtemp(), "c.json"))
import json
json.dump({"db_path": "none.db", "catalog_root": tempfile.mkdtemp(), "channels": {}, "media_root": "/tmp", "port": 5099,
           "advertise_url": "http://127.0.0.1:5099"}, open(os.environ["FS42_TUNER_CONF"], "w"))
sys.argv = ["tuner.py"]
tuner = importlib.import_module("tuner")
ffcmd = importlib.import_module("ffcmd")


class OverlayTest(unittest.TestCase):
    def test_the_text_goes_in_the_picture_filter_of_the_command(self):
        cmd = tuner.filler_cmd(5, "BAD FILE: a.mp4")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("drawtext=text='BAD FILE\\: a.mp4'", vf)
        self.assertIn("x=w-tw-30:y=h-th-30", vf)                          # bottom right
        self.assertEqual(tuner.filler_cmd(5)[tuner.filler_cmd(5).index("-vf") + 1].count("drawtext"), 0)

    def test_nothing_in_the_name_can_break_the_filter(self):
        vf = ffcmd.with_text(["ffmpeg", "-vf", "scale=1:1"], "it's 100%: a\\b;c,d")[2]
        self.assertEqual(vf.count("drawtext"), 1)
        self.assertNotIn("\\b", vf.split("text=")[1].split(":font")[0].replace("\\\\", ""))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_ffmpeg_accepts_it_and_makes_a_stream(self):
        for cmd in (tuner.filler_cmd(1, "NO FILE: it's here.mp4"), tuner.filler_cmd(1, "QUEUEING")):
            out = subprocess.run(cmd, capture_output=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr.decode()[-300:])
            self.assertGreater(len(out.stdout), 4000)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_picture_is_held_on_screen_with_the_reason_over_it(self):
        d = tempfile.mkdtemp()
        png = os.path.join(d, "brb.png")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180", "-frames:v", "1", png], check=True)
        out = subprocess.run(tuner.still_cmd(png, 2, "NO COMMERCIAL"), capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode()[-300:])
        self.assertGreater(len(out.stdout), 4000)
        shutil.rmtree(d)


class CaptionSafetyTest(unittest.TestCase):
    def test_a_failing_caption_injector_is_dropped_and_the_stream_goes_on(self):
        class Boom:
            def feed(self, chunk):
                raise IndexError("a stream it did not expect")

        out, inject = tuner.caption_feed(Boom(), b"\x47" * 188, 3)
        self.assertEqual(out, b"\x47" * 188)                 # the chunk goes out untouched
        self.assertIsNone(inject)                            # and captions are off for the rest of the programme
        self.assertEqual(tuner.caption_feed(None, b"abc"), (b"abc", None))


class OffAirReasonTest(unittest.TestCase):
    class Entry:
        def __init__(self, path, kind): self.path, self.content_type = path, kind

    def test_a_programme_has_no_reason_and_the_off_air_video_always_has_one(self):
        self.assertIsNone(tuner.offair_reason("S", self.Entry("/m/Show.mp4", "feature"), "Show"))
        for e, title in ((self.Entry("/opt/assets/test-pattern.mp4", "feature"), "Show"), (self.Entry("/x/y.mp4", "off_air"), "Show"),
                         (self.Entry("/x/y.mp4", "feature"), "Offair")):
            self.assertEqual(tuner.offair_reason("S", e, title), "OFF AIR: NOTHING SCHEDULED")

    def test_lists_the_calendar_left_out_for_having_no_videos_are_named(self):
        import schedules
        d = tempfile.mkdtemp()
        old = schedules.PATH
        schedules.PATH = os.path.join(d, "schedules.json")
        try:
            import json
            json.dump({"stations": {}, "skipped": {"S": ["Toons", "Anime", "More"]}}, open(schedules.PATH, "w"))
            got = tuner.offair_reason("S", self.Entry("/x/y.mp4", "off_air"), "x")
            self.assertEqual(got, "NO VIDEOS IN: Anime, More +1")
        finally:
            schedules.PATH = old


class ChannelSurvivesTest(unittest.TestCase):
    def test_an_error_while_playing_puts_the_reason_on_screen_and_the_channel_goes_on(self):
        import threading
        ch = tuner.Channel.__new__(tuner.Channel)
        ch.stop, ch.station, ch.number, ch.now_playing = threading.Event(), "X", 3, ""
        ch.lock, ch.subs = threading.Lock(), set()
        piped = []
        calls = []

        def step():
            calls.append(1)
            if len(calls) == 1:
                raise KeyError("no such block")
            ch.stop.set()
        ch._step = step
        ch._pipe = lambda cmd, **k: piped.append(cmd)
        ch._run()
        self.assertEqual(len(calls), 2)                          # it carried on after the error
        text = " ".join(piped[0])
        self.assertIn("CHANNEL ERROR", text)
        self.assertIn("KeyError", text)


class PrimeTest(unittest.TestCase):
    def test_a_viewer_joining_a_channel_of_tiny_chunks_is_not_dropped(self):
        import threading
        ch = tuner.Channel(9, "X", tuner.EncoderPool(4))
        ch.thread = object()                                  # an encoder is "running": attach only has to replay
        for _ in range(5000):                                 # what a quiet picture looks like: thousands of small writes
            ch.prime.append(b"\x47" * 1316)
        sub = ch.attach()
        self.assertIsNotNone(sub)
        self.assertFalse(sub.dead.is_set())
        got = 0
        while not sub.q.empty():
            got += len(sub.q.get_nowait())
        self.assertEqual(got, 5000 * 1316)                    # all of it, nothing lost


class DashboardTileTest(unittest.TestCase):
    class Entry:
        def __init__(self, path, kind="feature", stream=False):
            self.path, self.content_type, self.is_stream = path, kind, stream

    class Cur:
        def __init__(self, entry, title="Show"):
            self.entry, self.block_title = entry, title

    class Chan:
        def __init__(self, now_playing="--", subs=()):
            self.station, self.now_playing, self.subs = "S", now_playing, set(subs)

    def test_what_makes_a_channel_show_the_test_pattern(self):
        c = self.Chan()
        self.assertEqual(tuner.pattern_reason(c, None), "NO PROGRAMMING")                     # nothing scheduled
        gone = self.Cur(self.Entry("/nowhere/at/all/x.mp4"))
        self.assertTrue(tuner.pattern_reason(c, gone).startswith("NO FILE: x.mp4"))
        here = tempfile.mkdtemp()
        open(os.path.join(here, "ok.mp4"), "w").close()
        self.assertIsNone(tuner.pattern_reason(c, self.Cur(self.Entry(os.path.join(here, "ok.mp4")))))
        self.assertEqual(tuner.pattern_reason(c, self.Cur(self.Entry(os.path.join(here, "ok.mp4"), "off_air"))),
                         "OFF AIR: NOTHING SCHEDULED")
        self.assertEqual(tuner.pattern_reason(self.Chan("Test pattern: bad file"), self.Cur(self.Entry(os.path.join(here, "ok.mp4")))),
                         "BAD FILE")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_converted_source_gets_a_still_from_its_own_output_and_a_passed_on_one_none(self):
        ts = os.path.join(tempfile.mkdtemp(), "x.ts")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                        "-t", "3", "-c:v", "libx264", "-g", "30", "-f", "mpegts", ts], check=True, capture_output=True)
        data = open(ts, "rb").read()
        ch = tuner.Channel("88", "iptv:88", tuner.EncoderPool(2))
        for i in range(0, len(data), 188 * 64):
            ch.prime.append(data[i:i + 188 * 64])
        ch.subs.add(tuner.Subscriber())
        ch.thread = object()                                          # its encoder is running
        tuner.CHANS["88"] = ch
        tuner.EXTERNAL["iptv:88"] = {"name": "Cam", "mode": "convert", "source": "S", "type": "stream"}
        try:
            got = tuner.thumbnail("88")
            self.assertTrue(got and got[:2] == b"\xff\xd8")                         # a JPEG
            tuner.THUMBS.clear()
            tuner.EXTERNAL["iptv:88"]["mode"] = "relay"
            self.assertIsNone(tuner.thumbnail("88"))
            ch.thread = None
            tuner.EXTERNAL["iptv:88"]["mode"] = "convert"
            self.assertIsNone(tuner.thumbnail("88"))                                  # its encoder is down: none
        finally:
            tuner.CHANS.pop("88", None)
            tuner.EXTERNAL.pop("iptv:88", None)
            tuner.THUMBS.clear()

    def test_no_video_is_opened_for_a_snapshot_and_none_is_made_while_the_encoder_is_down(self):
        tuner.CHANS["77"] = tuner.Channel("77", "NobodyWatches", tuner.EncoderPool(2))
        called, real = [], tuner._frame_from_prime
        tuner._frame_from_prime = lambda chan: called.append(chan.station) or b"jpg"
        tuner._grab_frame_real = tuner._grab_frame
        tuner._grab_frame = lambda station: called.append("FILE:" + station) or b"file"
        try:
            self.assertIsNone(tuner.thumbnail("77"))                  # no encoder running: idle
            self.assertEqual(called, [])
            tuner.CHANS["77"].thread = object()                       # an encoder is running (a viewer, or the tail after one)
            self.assertEqual(tuner.thumbnail("77"), b"jpg")           # taken from the output it keeps, not from a file
            self.assertEqual(called, ["NobodyWatches"])
        finally:
            tuner._frame_from_prime = real
            tuner._grab_frame = tuner._grab_frame_real
            tuner.THUMBS.clear()
            del tuner.CHANS["77"]


class WindDownTest(unittest.TestCase):
    def channel(self, station="Own"):
        return tuner.Channel("66", station, tuner.EncoderPool(2))

    def test_a_station_finishes_the_queued_break_and_stops_at_the_start_of_the_next(self):
        ch = self.channel()
        ch.tail = {"breaks": 1}                                       # the last viewer left during the programme
        self.assertFalse(ch.tail_stops_here(False))                   # the programme goes on
        self.assertFalse(ch.tail_stops_here(True))                    # the queued break starts
        self.assertFalse(ch.tail_stops_here(True))                    # ...its next commercial or card is part of it
        self.assertFalse(ch.tail_stops_here(False))                   # the programme again
        self.assertTrue(ch.tail_stops_here(True))                     # a break that was not queued: stop here

    def test_leaving_in_the_middle_of_a_break_means_that_break_was_the_next_one(self):
        ch = self.channel()
        ch.in_break = True
        ch.thread = object()
        sub = tuner.Subscriber()
        ch.subs.add(sub)
        ch.pool.acquire("66")
        ch.detach(sub)
        try:
            self.assertEqual(ch.tail, {"breaks": 0})
            self.assertFalse(ch.tail_stops_here(True))                # still inside it
            self.assertFalse(ch.tail_stops_here(False))
            self.assertTrue(ch.tail_stops_here(True))                 # the next one stops it
        finally:
            ch.reaper.cancel()

    def test_somebody_tuning_in_cancels_the_wind_down(self):
        ch = self.channel()
        ch.tail = {"breaks": 0}
        ch.thread = object()
        ch.attach()
        self.assertIsNone(ch.tail)
        self.assertFalse(ch.tail_stops_here(True))

    def test_how_long_a_channel_stays_up_after_the_last_viewer(self):
        for station, ext, want in (("Own", None, tuner.IDLE_TAIL_SECONDS),
                                   ("iptv:5", {"mode": "convert"}, tuner.EXTERNAL_LINGER),
                                   ("iptv:6", {"mode": "relay"}, tuner.LINGER)):
            if ext:
                tuner.EXTERNAL[station] = ext
            ch = self.channel(station)
            ch.thread = object()
            sub = tuner.Subscriber()
            ch.subs.add(sub)
            try:
                ch.detach(sub)
                self.assertEqual(ch.reaper.interval, want, station)
            finally:
                ch.reaper.cancel()
                tuner.EXTERNAL.pop(station, None)


class FreeAnEncoderTest(unittest.TestCase):
    def setUp(self):
        self.pool = tuner.EncoderPool(1)                           # one encoder: every tune-in competes
        self.made = {}
        self.real_guide = tuner.guide_conf

    def tearDown(self):
        tuner.guide_conf = self.real_guide
        for n in list(self.made):
            tuner.CHANS.pop(n, None)

    def chan(self, number, station, winding_since=None):
        ch = tuner.Channel(number, station, self.pool)
        tuner.CHANS[number] = ch
        self.made[number] = ch
        if winding_since is not None:                              # running its tail with nobody watching
            ch.thread = threading.Thread(target=lambda: None)
            ch.idle_since = winding_since
            self.pool.active.add(number)
        return ch

    def test_the_channel_idle_the_longest_gives_up_its_encoder_to_a_newcomer(self):
        old, newer = self.chan("71", "A", winding_since=100.0), self.chan("72", "B", winding_since=200.0)
        self.pool.n = 2
        self.pool.active = {"71", "72"}
        want = self.chan("73", "C")
        sub = want.attach()
        self.assertIsNotNone(sub)
        self.assertIsNone(old.thread)                              # the longest idle one let go
        self.assertIsNotNone(newer.thread)                         # the other keeps winding down
        self.assertIn("73", self.pool.active)

    def test_a_channel_with_a_viewer_is_never_taken_from(self):
        busy = self.chan("74", "A", winding_since=100.0)
        busy.subs.add(tuner.Subscriber())
        want = self.chan("75", "B")
        self.assertIsNone(want.attach())                           # nothing free, and nobody to take from
        self.assertIsNotNone(busy.thread)

    def test_the_tv_guide_is_the_exception(self):
        guide = self.chan("76", "Guide", winding_since=100.0)
        tuner.guide_conf = lambda station: {"x": 1} if station == "Guide" else None
        want = self.chan("77", "Other")
        self.assertIsNone(want.attach())
        self.assertIsNotNone(guide.thread)                         # it was not stopped for the newcomer


class VolumeFilterTest(unittest.TestCase):
    def test_the_gain_goes_into_the_encoder_command_only_when_there_is_one(self):
        plain = tuner.segment_cmd("/m/a.mp4", 0, 30, False)
        self.assertNotIn("-af", plain)
        leveled = tuner.segment_cmd("/m/a.mp4", 0, 30, False, audio="volume=+4.00dB,alimiter=limit=0.891")
        self.assertEqual(leveled[leveled.index("-af") + 1], "volume=+4.00dB,alimiter=limit=0.891")
        self.assertLess(leveled.index("-af"), leveled.index("pipe:1"))

    def test_a_live_stream_and_a_station_that_says_no_are_left_alone(self):
        class E:
            is_stream = True
        self.assertIsNone(tuner.audio_level_filter("S", E(), "/m/a.mp4"))


if __name__ == "__main__":
    unittest.main()
