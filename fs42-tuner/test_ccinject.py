"""Closed captions put into a transport stream (ccinject.py). Run: python3 test_ccinject.py"""
import os
import shutil
import subprocess
import tempfile
import unittest

import ccinject as cc

VTT = """WEBVTT

00:00:00.500 --> 00:00:03.000
ALPHA <i>first</i>

00:00:05.000 --> 00:00:08.000
BRAVO
second line
"""


class Parts(unittest.TestCase):
    def test_parity_makes_every_byte_odd(self):
        for b in range(0x20, 0x7F):
            self.assertEqual(bin(cc.parity(b)).count("1") % 2, 1)
        self.assertEqual(cc.pair(cc.RCL), (0x94, 0x20))                     # the textbook values
        self.assertEqual(cc.pair(cc.EOC), (0x94, 0x2F))

    def test_webvtt_is_read_into_plain_cues(self):
        self.assertEqual(cc.parse_vtt(VTT), [(0.5, 3.0, "ALPHA first"), (5.0, 8.0, "BRAVO second line")])
        self.assertEqual(cc.parse_vtt("1\n00:01:02,250 --> 00:01:03,000\nHi\n"), [(62.25, 63.0, "Hi")])

    def test_text_is_made_safe_for_608(self):
        self.assertEqual(cc.clean("Café “x” *a*"), 'Cafe "x" -a-')
        self.assertEqual(cc.clean("日本"), "")
        lines = cc.wrap("word " * 30)
        self.assertTrue(all(len(l) <= cc.COLUMNS for l in lines))

    def test_a_caption_is_loaded_then_flipped_and_cleared_later(self):
        line = cc.timeline([(1.0, 2.0, "Hello")], 30)
        frames = sorted(line)
        self.assertEqual(line[frames[0]], cc.pair(cc.RCL))                  # loading starts before the caption is due
        flip = [f for f in frames if line[f] == cc.pair(cc.EOC)]
        self.assertTrue(flip and flip[-1] <= 31)                            # shown by 1.0 s
        self.assertEqual(line[max(frames)], cc.pair(cc.EDM))                # and cleared at 2.0 s

    def test_a_long_caption_is_sent_again_while_it_is_on_screen_but_never_over_the_next_one(self):
        line = cc.timeline([(0.0, 20.0, "STILL HERE"), (20.0, 21.0, "NEXT")], 30)
        flips = [f for f in sorted(line) if line[f] == cc.pair(cc.EOC)]
        self.assertGreaterEqual(len([f for f in flips if f < 600]), 8)       # about every four seconds of the twenty
        both = cc.timeline([(0.0, 3.0, "A"), (3.1, 6.0, "B")], 30)
        self.assertEqual(sorted(both)[0], 0)
        self.assertEqual(cc.caption_pairs("A")[0], cc.pair(cc.RCL))

    def test_the_sei_has_no_emulation_hazard(self):
        n = cc.sei((0x00, 0x00))
        self.assertNotIn(b"\x00\x00\x00", n[4:])
        self.assertNotIn(b"\x00\x00\x01", n[4:])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Stream(unittest.TestCase):
    def test_a_player_decoding_the_stream_gets_the_captions_at_their_times(self):
        d = tempfile.mkdtemp()
        base = os.path.join(d, "base.ts")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                        "-t", "10", "-c:v", "libx264", "-bf", "0", "-g", "30", "-f", "mpegts", base], check=True, capture_output=True)
        raw = open(base, "rb").read()
        inj = cc.Injector(cc.timeline(cc.parse_vtt(VTT), 30), 0, 30)
        out = b"".join(inj.feed(raw[i:i + 1316]) for i in range(0, len(raw), 1316)) + inj.flush()
        self.assertEqual(len(out) % 188, 0)
        self.assertEqual(inj.pid, 256)
        path = os.path.join(d, "cc.ts")
        open(path, "wb").write(out)
        srt = os.path.join(d, "cc.srt")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"movie={path}[out0+subcc]",
                        "-map", "0:s:0", "-f", "srt", srt], check=True, capture_output=True)
        text = open(srt).read()
        self.assertIn("ALPHA first", text)
        self.assertIn("BRAVO", text)
        self.assertIn("second line", text)
        # video still decodes: same number of frames out as in
        n = lambda p: subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                      "stream=nb_read_frames", "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip()
        self.assertEqual(n(path), n(base))

    def test_a_segment_that_starts_part_way_in_picks_up_the_captions_there(self):
        d = tempfile.mkdtemp()
        base = os.path.join(d, "base.ts")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                        "-t", "4", "-c:v", "libx264", "-bf", "0", "-g", "30", "-f", "mpegts", base], check=True, capture_output=True)
        raw = open(base, "rb").read()
        inj = cc.Injector(cc.timeline(cc.parse_vtt(VTT), 30), 4.0, 30)          # the file's 4.0 s mark: BRAVO comes at 5.0 s
        out = inj.feed(raw) + inj.flush()
        path = os.path.join(d, "cc.ts")
        open(path, "wb").write(out)
        srt = os.path.join(d, "cc.srt")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"movie={path}[out0+subcc]",
                        "-map", "0:s:0", "-f", "srt", srt], check=True, capture_output=True)
        text = open(srt).read()
        self.assertIn("BRAVO", text)
        self.assertNotIn("ALPHA", text)

    def test_a_viewer_who_joins_mid_caption_gets_it_within_a_few_seconds(self):
        d = tempfile.mkdtemp()
        base = os.path.join(d, "base.ts")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                        "-t", "10", "-c:v", "libx264", "-bf", "0", "-g", "30", "-f", "mpegts", base], check=True, capture_output=True)
        raw = open(base, "rb").read()
        inj = cc.Injector(cc.timeline([(0.0, 600.0, "A LONG ONE")], 30), 300.0, 30)       # no resume: the join is simulated below
        out = inj.feed(raw) + inj.flush()
        path = os.path.join(d, "cc.ts")
        open(path, "wb").write(out)
        srt = os.path.join(d, "cc.srt")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"movie={path}[out0+subcc]",
                        "-map", "0:s:0", "-f", "srt", srt], check=True, capture_output=True)
        self.assertIn("A LONG ONE", open(srt).read())

    def test_a_caption_already_on_screen_when_a_segment_starts_is_loaded_again(self):
        d = tempfile.mkdtemp()
        base = os.path.join(d, "base.ts")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                        "-t", "3", "-c:v", "libx264", "-bf", "0", "-g", "30", "-f", "mpegts", base], check=True, capture_output=True)
        cues = [(0.0, 600.0, "A LONG ONE")]
        inj = cc.Injector(cc.timeline(cues, 30), 300.0, 30, resume=cc.active_at(cues, 300.0))
        out = inj.feed(open(base, "rb").read()) + inj.flush()
        path = os.path.join(d, "cc.ts")
        open(path, "wb").write(out)
        srt = os.path.join(d, "cc.srt")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"movie={path}[out0+subcc]",
                        "-map", "0:s:0", "-f", "srt", srt], check=True, capture_output=True)
        self.assertIn("A LONG ONE", open(srt).read())


if __name__ == "__main__":
    unittest.main()
