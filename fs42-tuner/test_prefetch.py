"""The read-ahead (prefetch.py): learned load times, the cache, what is wanted when, and the copies it starts.

Run: python3 test_prefetch.py
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import prefetch as pf

MB = 1024 * 1024


def entry(path, kind="feature", start=0, stream=False):
    return {"path": path, "type": kind, "stream": stream, "start": datetime(2026, 1, 1) + timedelta(seconds=start), "seconds": 10, "seek": 0}


class StatsTest(unittest.TestCase):
    def test_an_unknown_share_is_assumed_to_be_slow(self):
        s = pf.Stats(None)
        self.assertAlmostEqual(s.expected("/mnt/media/x", 100 * MB), pf.ASSUMED_DELAY + 100 / 5, places=1)

    def test_expected_time_is_delay_plus_size_over_the_pessimistic_speed(self):
        s = pf.Stats(None)
        for bps in (50, 40, 45, 2, 48, 46, 47, 44):                         # one slow sample among fast ones
            s.record("/mnt/media/x", 200 * MB, 0.5, 200 / bps)
        exp = s.expected("/mnt/media/x", 100 * MB)
        self.assertGreater(exp, 0.5 + 100 / 45)                              # the slow sample counts: slower than the typical speed
        self.assertLess(exp, 0.5 + 100 / 1.9)                                # but one bad sample does not set the whole figure

    def test_a_small_file_teaches_only_the_delay(self):
        s = pf.Stats(None)
        s.record("/mnt/media/x", 1 * MB, 2.0, 0.001)                         # would look like 1000 MB/s
        self.assertIsNone(s.summary()["/mnt/media/x"]["mb_per_s"])
        self.assertEqual(s.summary()["/mnt/media/x"]["delay_s"], 2.0)

    def test_what_it_learned_is_kept_across_restarts(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "stats.json")
            a = pf.Stats(path)
            a.record("/mnt/media/anime", 100 * MB, 1.0, 10.0)
            a.save()
            b = pf.Stats(path)
            self.assertEqual(b.summary()["/mnt/media/anime"]["mb_per_s"], 10.0)
            open(path, "w").write("{broken")
            self.assertEqual(pf.Stats(path).summary(), {})                    # a bad file is just empty
        finally:
            shutil.rmtree(d)

    def test_shares_are_the_folders_under_the_media_root(self):
        self.assertEqual(pf.share_key("/mnt/media/anime/Show/e1.mkv"), "/mnt/media/anime")
        self.assertEqual(pf.share_key("/other/place/deep/file.mp4"), "/other/place")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = [0.0]
        self.cache = pf.Cache(os.path.join(self.d, "c"), 100, now=lambda: self.t[0])

    def tearDown(self):
        shutil.rmtree(self.d)

    def put(self, name, size):
        self.assertTrue(self.cache.make_room(size, set()))
        tmp = self.cache.temp_name(name)
        open(tmp, "wb").write(b"x" * size)
        self.cache.finish(name, tmp, size, size)

    def test_the_oldest_unused_copy_goes_first(self):
        self.put("a", 40); self.t[0] = 1; self.put("b", 40); self.t[0] = 2
        self.cache.lookup("a")                                             # a was used more recently than b
        self.assertTrue(self.cache.make_room(40, set()))
        self.assertIsNone(self.cache.lookup("b"))
        self.assertIsNotNone(self.cache.lookup("a"))

    def test_a_copy_that_is_playing_or_wanted_is_never_dropped(self):
        self.put("a", 40); self.put("b", 40)
        self.cache.pin("a")
        self.assertFalse(self.cache.make_room(60, {"b"}))                   # a plays, b is wanted: no room
        self.assertIsNotNone(self.cache.lookup("a")); self.assertIsNotNone(self.cache.lookup("b"))
        self.cache.unpin("a")
        self.assertTrue(self.cache.make_room(60, {"b"}))                    # now a can go
        self.assertIsNone(self.cache.lookup("a"))

    def test_the_budget_counts_copies_in_progress(self):
        self.assertTrue(self.cache.make_room(70, set()))
        self.assertFalse(self.cache.make_room(40, set()))                   # 70 already promised
        self.cache.abandon(self.cache.temp_name("z"), 70)
        self.assertTrue(self.cache.make_room(40, set()))

    def test_pin_gives_the_copy_or_the_original(self):
        self.put("a", 10)
        path, copied = self.cache.pin("a")
        self.assertTrue(copied and os.path.isfile(path))
        self.assertEqual(self.cache.pin("never-copied"), ("never-copied", False))

    def test_leftovers_from_an_earlier_run_are_cleared(self):
        folder = os.path.join(self.d, "again")
        os.makedirs(folder)
        open(os.path.join(folder, "old.mp4"), "w").write("x")
        pf.Cache(folder, 100)
        self.assertEqual(os.listdir(folder), [])


class CopyTest(unittest.TestCase):
    def test_a_file_is_copied_and_timed(self):
        d = tempfile.mkdtemp()
        try:
            src, dst = os.path.join(d, "a.bin"), os.path.join(d, "b.bin")
            open(src, "wb").write(os.urandom(300000))
            total, delay, seconds = pf.copy_file(src, dst, lambda: False, chunk=65536)
            self.assertEqual(total, 300000)
            self.assertEqual(open(src, "rb").read(), open(dst, "rb").read())
            self.assertGreaterEqual(delay, 0)
            self.assertIsNone(pf.copy_file(src, dst, lambda: True, chunk=65536))        # stopped
        finally:
            shutil.rmtree(d)


class PlanTest(unittest.TestCase):
    def test_groups(self):
        es = [entry("ep", "feature"), entry("b", "bump"), entry("c1", "commercial"), entry("ep", "feature")]
        self.assertEqual([(k, [e["path"] for e in g]) for k, g in pf.groups(es)],
                         [("program", ["ep"]), ("break", ["b", "c1"]), ("program", ["ep"])])

    def test_while_a_show_plays_the_next_break_and_the_show_itself_if_it_comes_back(self):
        es = [entry("ep1.mp4", "feature", 0), entry("b.mp4", "bump", 600), entry("c1.mp4", "commercial", 605),
              entry("c2.mp4", "commercial", 635), entry("ep1.mp4", "feature", 665), entry("ep2.mp4", "feature", 1300)]
        w = pf.wanted(es)
        self.assertEqual([(x["path"], x["why"]) for x in w],
                         [("ep1.mp4", "needed again"), ("b.mp4", "next"), ("c1.mp4", "next"), ("c2.mp4", "next")])

    def test_while_a_break_plays_the_next_episode(self):
        es = [entry("c1.mp4", "commercial", 0), entry("c2.mp4", "commercial", 30), entry("ep2.mp4", "feature", 60), entry("b.mp4", "bump", 700)]
        self.assertEqual([x["path"] for x in pf.wanted(es)], ["ep2.mp4"])

    def test_a_show_that_does_not_come_back_is_not_kept(self):
        es = [entry("ep1.mp4", "feature", 0), entry("c1.mp4", "commercial", 600), entry("ep2.mp4", "feature", 630)]
        self.assertEqual([x["path"] for x in pf.wanted(es)], ["c1.mp4"])

    def test_live_streams_are_ignored(self):
        self.assertEqual(pf.wanted([entry("s", "feature", 0, stream=True)]), [])


class PrefetcherTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.media = os.path.join(self.d, "media", "anime")
        os.makedirs(self.media)
        for n, size in (("ep1.mp4", 2 * MB), ("ep2.mp4", 3 * MB), ("c1.mp4", MB), ("b.mp4", MB // 2)):
            open(os.path.join(self.media, n), "wb").write(os.urandom(size))
        self.db = os.path.join(self.d, "f.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        self.c = c

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.d)

    def schedule(self, first_seconds):
        """A schedule where the episode now airing has `first_seconds` left, then a break, then the next episode."""
        p = lambda n: os.path.join(self.media, n)
        now = datetime.now()
        start = now - timedelta(seconds=100 - first_seconds)
        plan = [("ep1.mp4", 100, "feature"), ("b.mp4", 5, "bump"), ("c1.mp4", 30, "commercial"), ("ep2.mp4", 100, "feature")]
        entries = [{"path": p(n), "skip": 0, "duration": float(d), "is_stream": False, "content_type": t} for n, d, t in plan]
        self.c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,?)",
                       ("S", start.isoformat(), (start + timedelta(seconds=1000)).isoformat(), "Show", json.dumps(entries)))
        self.c.commit()

    def make(self, **cfg):
        c = {**pf.env_config({}), "dir": os.path.join(self.d, "cache"), "ram_only": False, "stats_path": None, "max_bytes": 50 * MB, **cfg}
        return pf.Prefetcher(c, self.db, lambda: {"1": "S"}, media_root=os.path.join(self.d, "media"), log=lambda *_: None)

    def settle(self, p):
        end = time.time() + 10
        while (p.current or any(p.copying.values())) and time.time() < end:
            time.sleep(0.02)

    def drain(self, p, passes=5):
        """plan_once until nothing more starts (one copy per share runs at a time), returning everything started."""
        out = []
        for _ in range(passes):
            got = p.plan_once()
            self.settle(p)
            if not got:
                break
            out += got
        return out

    def test_the_next_break_is_copied_when_it_is_nearly_due_and_playback_uses_the_copy(self):
        self.schedule(first_seconds=2)                                       # the break starts in 2 seconds
        p = self.make()
        started = self.drain(p)
        self.assertEqual(sorted(os.path.basename(s) for s in started), ["b.mp4", "c1.mp4"])
        path = os.path.join(self.media, "c1.mp4")
        local, copied = p.pin(path)
        self.assertTrue(copied)
        self.assertEqual(open(local, "rb").read(), open(path, "rb").read())
        p.unpin(path)
        self.assertEqual(p.stats.summary()[self.media]["samples"], 2)         # and it learned from the copies

    def test_nothing_is_copied_while_there_is_plenty_of_time(self):
        self.schedule(first_seconds=90)
        p = self.make()
        self.assertEqual(p.plan_once(), [])

    def test_a_slow_share_starts_earlier(self):
        self.schedule(first_seconds=15)
        p = self.make()
        self.assertEqual(p.plan_once(), [])                                   # 15 s is enough for these small files
        for _ in range(5):
            p.stats.record(self.media, 100 * MB, 4.0, 100.0)                   # 1 MB/s and 4 s to open: a slow share
        started = p.plan_once()
        self.settle(p)
        self.assertTrue(started)                                              # the same files now need a head start

    def test_nothing_runs_for_a_channel_nobody_watches(self):
        self.schedule(first_seconds=4)
        p = self.make()
        p.watched = lambda: {}
        self.assertEqual(p.plan_once(), [])

    def test_it_never_goes_over_the_budget(self):
        self.schedule(first_seconds=4)
        p = self.make(max_bytes=int(1.2 * MB))                                # room for the bump but not the commercial too
        p.plan_once()
        self.settle(p)
        self.assertLessEqual(p.cache.used_bytes(), int(1.2 * MB))

    def test_off_and_not_ram_are_explained(self):
        off = pf.Prefetcher({**pf.env_config({"PREFETCH": "0"}), "stats_path": None}, self.db, lambda: {})
        self.assertFalse(off.enabled)
        self.assertIn("PREFETCH=0", off.status()["why_not"])
        notram = pf.Prefetcher({**pf.env_config({}), "dir": os.path.join(self.d, "plain"), "stats_path": None}, self.db, lambda: {})
        self.assertFalse(notram.enabled)
        self.assertIn("tmpfs", notram.status()["why_not"])
        self.assertIn("PREFETCH_RAM=0", notram.status()["why_not"])
        self.assertEqual(notram.pin("x"), ("x", False))                       # playback is unaffected

    def test_the_status_says_what_is_going_on(self):
        self.schedule(first_seconds=2)
        p = self.make()
        self.drain(p)
        st = p.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["cache"]["files"], 2)
        self.assertIn("1", st["channels"])

    def test_the_mount_type_of_a_path(self):
        self.assertEqual(pf.mount_type("/proc"), "proc")
        self.assertNotIn(pf.mount_type("/definitely/not/here"), ("tmpfs", "ramfs"))    # whatever holds the closest folder


if __name__ == "__main__":
    unittest.main()
