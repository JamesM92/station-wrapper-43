"""Break points moved onto real black/silent gaps (breaksnap.py), against a real ffmpeg-made clip."""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="breaksnap-")
os.environ["FS42_BREAK_STORE"] = os.path.join(TMP, "store.json")
import breaksnap as bs
import breakstore


def chapters(*starts, end=200.0):
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end
        out.append({"chapter_start": float(s), "chapter_end": float(e), "segment_duration": float(e - s)})
    return out


class Choose(unittest.TestCase):
    def test_parse_detect(self):
        log = ("[blackdetect @ 0x1] black_start:60 black_end:61.1 black_duration:1.1\n"
               "[silencedetect @ 0x2] silence_start: 60.0004\n[silencedetect @ 0x2] silence_end: 61.022 | silence_duration: 1.02\n")
        self.assertEqual(bs.parse_detect(log), ([(60.0, 61.1)], [(60.0004, 61.022)]))

    def test_black_with_silence_beats_a_nearer_plain_pause(self):
        self.assertAlmostEqual(bs.choose(100, [(60, 61)], [(60, 61), (98, 99)]), 60.5)

    def test_a_gap_too_far_away_is_ignored(self):
        self.assertIsNone(bs.choose(300, [(60, 61)], [(60, 61)]))

    def test_nothing_found_moves_nothing(self):
        self.assertIsNone(bs.choose(100, [], []))

    def test_a_short_blip_of_silence_is_not_a_break(self):
        self.assertIsNone(bs.choose(100, [], [(99, 99.2)]))


class Snap(unittest.TestCase):
    def test_moves_a_mark_and_keeps_the_segments_consistent(self):
        new, n = bs.snap(chapters(0, 120), 200.0, lambda lo, hi: ([(60, 61)], [(60, 61)]))
        self.assertEqual(n, 1)
        self.assertAlmostEqual(new[1]["chapter_start"], 60.5)
        self.assertAlmostEqual(new[0]["chapter_end"], 60.5)
        self.assertAlmostEqual(new[0]["segment_duration"], 60.5)
        self.assertAlmostEqual(new[1]["segment_duration"], 139.5)

    def test_the_first_mark_and_a_mark_near_the_end_stay(self):
        new, n = bs.snap(chapters(0, 190), 200.0, lambda lo, hi: ([(185, 186)], [(185, 186)]))
        self.assertEqual(n, 0)

    def test_a_move_that_would_leave_a_sliver_is_refused(self):
        new, n = bs.snap(chapters(0, 40, 120), 200.0, lambda lo, hi: ([(45, 46)], [(45, 46)]) if hi < 130 else ([], []))
        self.assertEqual(new[1]["chapter_start"], 40.0 if n == 0 else new[1]["chapter_start"])
        self.assertGreaterEqual(new[1]["chapter_start"] - new[0]["chapter_start"], bs.MIN_SEGMENT)

    def test_a_file_without_marks_is_left_alone(self):
        self.assertEqual(bs.snap([], 200.0, lambda lo, hi: ([], [])), ([], 0))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Real(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clip = os.path.join(TMP, "gap.mp4")
        gap = "between(t,60,61)+between(t,150,150.5)"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=200",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=200",
                        "-vf", f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='{gap}'",
                        "-af", f"volume=enable='{gap}':volume=0", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        "-shortest", cls.clip], check=True)

    def test_finds_the_gap_in_the_real_file(self):
        black, silence = bs.ffmpeg_detect(self.clip, 30, 100)
        self.assertTrue(any(abs(a - 60) < 0.3 for a, b in black), black)
        self.assertTrue(any(abs(a - 60) < 0.3 for a, b in silence), silence)

    def test_snap_station_moves_the_stored_marks_once(self):
        db = os.path.join(TMP, "fs42.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE catalog_entries (station TEXT, realpath TEXT, duration REAL)")
        c.execute("CREATE TABLE chapter_points (path TEXT PRIMARY KEY, points TEXT, last_updated TEXT)")
        c.execute("INSERT INTO catalog_entries VALUES ('S', ?, 200.0)", (self.clip,))
        c.execute("INSERT INTO chapter_points VALUES (?, ?, 'x')", (self.clip, json.dumps(chapters(0, 120))))
        c.commit()
        self.assertEqual(bs.snap_station(db, "S"), (1, 1))
        pts = json.loads(c.execute("SELECT points FROM chapter_points").fetchone()[0])
        self.assertLess(abs(pts[1]["chapter_start"] - 150.3), 0.6)      # the nearer of the two gaps (30 s, not 60 s, away)
        seen = []
        self.assertEqual(bs.snap_station(db, "S", detect_for=lambda *a: seen.append(a) or ([], [])), (0, 0))   # done: not looked at again
        self.assertEqual(seen, [])
        self.assertEqual(bs.snap_station(db, "Other"), (0, 0))


class EdlTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.video = os.path.join(self.dir, "Show S01E01.mkv")
        open(self.video, "wb").write(b"x" * 10)
        self.db = os.path.join(self.dir, "fs42.db")
        c = self.conn = sqlite3.connect(self.db)
        c.execute("CREATE TABLE catalog_entries (station TEXT, realpath TEXT, duration REAL)")
        c.execute("CREATE TABLE chapter_points (path TEXT PRIMARY KEY, points TEXT, last_updated TEXT)")
        c.execute("INSERT INTO catalog_entries VALUES ('S', ?, 1500.0)", (self.video,))
        c.commit()
        breakstore.save({})

    def edl(self, text):
        open(os.path.join(self.dir, "Show S01E01.edl"), "w").write(text)

    def marks(self):
        row = self.conn.execute("SELECT points FROM chapter_points").fetchone()
        return [c["chapter_start"] for c in json.loads(row[0])] if row else None

    def test_reads_cuts_and_commercial_breaks_and_ignores_the_rest(self):
        self.edl("# made by comskip\n300.5 420.0 0\n610 700 3\n900 901 2\n0.5 3 0\nnonsense\n")
        self.assertEqual(breakstore.read_edl(self.video)[0], [0.5, 300.5, 610.0])
        self.assertEqual(breakstore.read_edl(os.path.join(self.dir, "none.mkv")), (None, None))

    def test_an_edl_beside_the_video_sets_the_breaks_even_with_no_chapters(self):
        self.edl("300.5 420.0 0\n610 700 0\n")
        self.assertEqual(bs.snap_station(self.db, "S", detect_for=lambda *a: self.fail("no scan when there is an edl")), (1, 2))
        self.assertEqual(self.marks(), [0.0, 300.5, 610.0])
        self.assertEqual(breakstore.load()[self.video]["source"], "edl")

    def test_an_edl_wins_over_chapter_marks_and_is_not_redone(self):
        self.conn.execute("INSERT INTO chapter_points VALUES (?, ?, 'x')", (self.video, json.dumps(chapters(0, 500, 900, end=1500.0))))
        self.conn.commit()
        self.edl("300 400 0\n")
        bs.snap_station(self.db, "S", detect_for=lambda *a: ([], []))
        self.assertEqual(self.marks(), [0.0, 300.0])
        self.assertEqual(bs.snap_station(self.db, "S"), (0, 0))

    def test_changing_the_edl_changes_the_breaks(self):
        self.edl("300 400 0\n")
        bs.snap_station(self.db, "S")
        self.edl("300 400 0\n800 900 0\n")
        self.assertEqual(bs.snap_station(self.db, "S")[0], 1)
        self.assertEqual(self.marks(), [0.0, 300.0, 800.0])

    def test_a_removed_edl_goes_back_to_the_chapter_marks(self):
        self.conn.execute("INSERT INTO chapter_points VALUES (?, ?, 'x')", (self.video, json.dumps(chapters(0, 500, end=1500.0))))
        self.conn.commit()
        self.edl("300 400 0\n")
        bs.snap_station(self.db, "S", detect_for=lambda *a: ([], []))
        os.remove(os.path.join(self.dir, "Show S01E01.edl"))
        bs.snap_station(self.db, "S", detect_for=lambda *a: ([], []))
        self.assertEqual(self.marks(), [0.0, 500.0])


if __name__ == "__main__":
    unittest.main(verbosity=0)
