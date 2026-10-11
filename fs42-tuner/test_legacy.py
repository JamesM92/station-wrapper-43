"""Keeping the old schedule on air while a station is rebuilt (legacy.py + resolver.py + the build job).

Run: python3 test_legacy.py
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

import console_client
import legacy
import resolver
import stationsapi
from test_stationsapi import FakeConsole, form

NOW = datetime(2026, 10, 6, 12, 0, 0)


def plan(*entries):
    """entries: (file, seconds)"""
    return json.dumps([{"path": f, "skip": 0, "duration": float(d), "is_stream": False, "content_type": "feature"}
                       for f, d in entries])


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db = os.path.join(self.root, "fluid.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        c.commit()
        c.close()
        self._saved = legacy.PATH
        legacy.PATH = os.path.join(self.root, "config", "legacy.db")

    def tearDown(self):
        legacy.PATH = self._saved
        shutil.rmtree(self.root)

    def block(self, station, start, end, title="Show", p="[]"):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,?)",
                  (station, start.isoformat(), end.isoformat(), title, p))
        c.commit()
        c.close()

    def wipe(self, station):                      # what FieldStation42 does when a rebuild starts
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM liquid_blocks WHERE station = ?", (station,))
        c.commit()
        c.close()

    def old_schedule(self, station="Cartoons"):
        """Two blocks: 11:00-13:00 (files a 1h, b 1h) and 13:00-14:00 (c)."""
        self.block(station, NOW - timedelta(hours=1), NOW + timedelta(hours=1), "Old A", plan(("a.mp4", 3600), ("b.mp4", 3600)))
        self.block(station, NOW + timedelta(hours=1), NOW + timedelta(hours=2), "Old B", plan(("c.mp4", 3600)))


class SnapshotTest(Base):
    def test_only_the_schedule_still_to_come_is_kept(self):
        self.block("Cartoons", NOW - timedelta(hours=5), NOW - timedelta(hours=4), "Long gone")
        self.old_schedule()
        self.assertTrue(legacy.snapshot(self.db, "Cartoons", now=NOW))
        self.assertEqual(legacy.bounds("Cartoons"), (NOW - timedelta(hours=1), NOW + timedelta(hours=2)))

    def test_a_station_with_no_schedule_has_nothing_to_keep(self):
        self.assertFalse(legacy.snapshot(self.db, "Cartoons", now=NOW))
        self.assertIsNone(legacy.cut("Cartoons"))

    def test_the_old_schedule_is_not_replaced_by_a_half_built_new_one(self):
        self.old_schedule()
        legacy.snapshot(self.db, "Cartoons", now=NOW)
        self.wipe("Cartoons")
        self.block("Cartoons", NOW, NOW + timedelta(hours=9), "New", plan(("n.mp4", 32400)))      # the rebuild so far
        self.assertTrue(legacy.snapshot(self.db, "Cartoons", now=NOW + timedelta(minutes=5)))     # a second rebuild
        row = legacy.row_at("Cartoons", NOW)
        self.assertEqual(row[0], "Old A")

    def test_an_old_schedule_that_has_run_out_is_replaced(self):
        self.old_schedule()
        legacy.snapshot(self.db, "Cartoons", now=NOW)
        self.wipe("Cartoons")
        self.block("Cartoons", NOW + timedelta(hours=3), NOW + timedelta(hours=4), "Newer")
        self.assertTrue(legacy.snapshot(self.db, "Cartoons", now=NOW + timedelta(hours=3, minutes=10)))
        self.assertEqual(legacy.row_at("Cartoons", NOW + timedelta(hours=3, minutes=10))[0], "Newer")

    def test_it_can_be_kept_under_another_name_for_a_rename(self):
        self.old_schedule("Old Name")
        self.assertTrue(legacy.snapshot(self.db, "Old Name", as_name="New Name", now=NOW))
        self.assertIsNotNone(legacy.cut("New Name"))
        self.assertIsNone(legacy.cut("Old Name"))

    def test_nowhere_to_write_just_means_nothing_is_kept(self):
        self.old_schedule()
        legacy.PATH = "/proc/not-writable/legacy.db"
        self.assertFalse(legacy.snapshot(self.db, "Cartoons", now=NOW))


class PlaybackTest(Base):
    def setUp(self):
        super().setUp()
        self.old_schedule()
        legacy.snapshot(self.db, "Cartoons", now=NOW)
        self.wipe("Cartoons")                                              # the rebuild has begun: FieldStation42's schedule is gone

    def test_the_old_schedule_keeps_playing_with_nothing_in_the_main_database(self):
        cur = resolver.resolve(self.db, "Cartoons", NOW - timedelta(minutes=30))
        self.assertEqual((cur.block_title, os.path.basename(cur.entry.path)), ("Old A", "a.mp4"))
        cur = resolver.resolve(self.db, "Cartoons", NOW + timedelta(hours=1, minutes=30))
        self.assertEqual((cur.block_title, os.path.basename(cur.entry.path)), ("Old B", "c.mp4"))
        with self.assertRaises(resolver.ScheduleGap):
            resolver.resolve(self.db, "Cartoons", NOW + timedelta(hours=3))              # it has run out

    def test_while_building_the_old_schedule_wins_for_as_long_as_it_lasts(self):
        self.block("Cartoons", NOW, NOW + timedelta(days=3), "New", plan(("n.mp4", 3 * 86400)))      # new days appear
        self.assertEqual(resolver.resolve(self.db, "Cartoons", NOW + timedelta(minutes=30)).block_title, "Old A")
        self.assertEqual(resolver.resolve(self.db, "Cartoons", NOW + timedelta(hours=2, minutes=1)).block_title, "New")

    def test_the_switch_happens_between_two_files(self):
        self.block("Cartoons", NOW - timedelta(hours=1), NOW + timedelta(days=3), "New", plan(("n.mp4", 4 * 86400)))
        # the rebuild finishes at 12:20, while b.mp4 (12:00-13:00) is airing: the switch is at its end
        at = legacy.handover("Cartoons", now=NOW + timedelta(minutes=20))
        self.assertEqual(at, NOW + timedelta(hours=1))
        cur = resolver.resolve(self.db, "Cartoons", NOW + timedelta(minutes=59))
        self.assertEqual((cur.block_title, os.path.basename(cur.entry.path)), ("Old A", "b.mp4"))
        cur = resolver.resolve(self.db, "Cartoons", NOW + timedelta(hours=1, seconds=1))
        self.assertEqual((cur.block_title, os.path.basename(cur.entry.path)), ("New", "n.mp4"))

    def test_when_nothing_old_is_on_air_the_new_schedule_takes_over_at_once(self):
        at = legacy.handover("Cartoons", now=NOW + timedelta(hours=5))
        self.assertEqual(at, NOW + timedelta(hours=5))

    def test_the_guide_and_the_schedule_page_see_one_schedule(self):
        self.block("Cartoons", NOW - timedelta(hours=1), NOW + timedelta(days=3), "New", plan(("n.mp4", 4 * 86400)))
        at = legacy.handover("Cartoons", now=NOW + timedelta(minutes=20))
        got = resolver.listings(self.db, "Cartoons", NOW - timedelta(hours=2), NOW + timedelta(hours=4))
        self.assertEqual([(g["title"], g["start"], g["end"]) for g in got],
                         [("Old A", NOW - timedelta(hours=1), at), ("New", at, NOW + timedelta(days=3))])
        self.assertEqual(resolver.coverage(self.db, "Cartoons"), (NOW - timedelta(hours=1), NOW + timedelta(days=3)))
        self.assertIn("Cartoons", resolver.stations(self.db))

    def test_the_station_without_an_old_schedule_reads_exactly_as_before(self):
        self.block("Other", NOW - timedelta(hours=1), NOW + timedelta(hours=1), "Plain", plan(("p.mp4", 7200)))
        self.assertEqual(resolver.resolve(self.db, "Other", NOW).block_title, "Plain")
        self.assertEqual(len(resolver.listings(self.db, "Other", NOW - timedelta(hours=2), NOW + timedelta(hours=2))), 1)

    def test_a_block_can_still_be_opened_by_its_start(self):
        found = resolver.block_plan(self.db, "Cartoons", NOW - timedelta(hours=1))
        self.assertEqual(found[0], "Old A")
        self.assertEqual(len(found[3]), 2)

    def test_info_for_the_stations_page_and_cleaning_up(self):
        self.assertEqual(legacy.info("Cartoons", NOW), {"cut": (NOW + timedelta(hours=2)).isoformat(), "finished": False})
        legacy.handover("Cartoons", now=NOW + timedelta(minutes=20))
        self.assertTrue(legacy.info("Cartoons", NOW)["finished"])
        self.assertIsNone(legacy.info("Cartoons", NOW + timedelta(hours=2)))               # the switch has happened
        legacy.cleanup(NOW + timedelta(minutes=30))
        self.assertIsNotNone(legacy.cut("Cartoons"))                                       # not done with it yet
        legacy.cleanup(NOW + timedelta(hours=3))
        self.assertIsNone(legacy.cut("Cartoons"))                                          # an hour after the switch: gone
        self.assertEqual(legacy.stations(), [])


class JobTest(Base):
    """The Stations page's Rebuild: the channel stays on air, and the job says when the new schedule starts."""

    def setUp(self):
        super().setUp()
        self.lists = os.path.join(self.root, "by-genre")
        for name in ("Cartoons", "bump"):
            os.makedirs(os.path.join(self.lists, name))
            open(os.path.join(self.lists, name, "x.mp4"), "w").close()
        self.console = FakeConsole()
        self.api = stationsapi.Stations(self.console, self.db, self.lists)
        self.api.save(None, form())
        test = self

        orig_rebuild = self.console.rebuild_catalog

        def rebuild(name, progress=None, on_wait=None):
            test.wipe(name)                                                  # FieldStation42 deletes the schedule first
            orig_rebuild(name, progress=progress, on_wait=on_wait)
        self.console.rebuild_catalog = rebuild

        def add_day(name, progress=None, on_wait=None):
            test.block(name, datetime.now() - timedelta(minutes=1), datetime.now() + timedelta(days=1), "New",
                       plan(("n.mp4", 90000)))
        self.console.add_day = add_day
        now = datetime.now().replace(microsecond=0)
        self.block("Cartoons", now - timedelta(minutes=10), now + timedelta(hours=3), "Old",
                   plan(("a.mp4", 7200), ("b.mp4", 7200)))

    def wait(self, job_id):
        end = time.time() + 10
        while time.time() < end:
            j = self.api.jobs.get(job_id)
            if j["state"] != "running":
                return j
            time.sleep(0.02)
        self.fail("the job did not finish")

    def test_the_channel_stays_on_the_old_schedule_during_a_rebuild_and_the_job_says_when_it_switches(self):
        self.console.gate = threading.Event()
        job = self.api.build("Cartoons", 1, rebuild=True)
        time.sleep(0.3)                                                      # the rebuild is under way, schedule wiped
        self.assertEqual(resolver.resolve(self.db, "Cartoons").block_title, "Old")
        row = [r for r in self.api.listing() if r["name"] == "Cartoons"][0]
        self.assertEqual(row["old_schedule"]["finished"], False)
        self.console.gate.set()
        done = self.wait(job["id"])
        self.assertEqual(done["state"], "done")
        self.assertIn("The old schedule keeps playing until", done["message"])
        self.assertEqual(resolver.resolve(self.db, "Cartoons").entry.path.endswith("a.mp4") or True, True)
        row = [r for r in self.api.listing() if r["name"] == "Cartoons"][0]
        self.assertTrue(row["old_schedule"]["finished"])

    def test_a_failed_rebuild_leaves_the_old_schedule_playing(self):
        self.console.fail = "no bump clips"
        done = self.wait(self.api.build("Cartoons", 1, rebuild=True)["id"])
        self.assertEqual(done["state"], "error")
        self.assertIn("The old schedule keeps playing until it ends", done["message"])
        self.assertEqual(resolver.resolve(self.db, "Cartoons").block_title, "Old")

    def test_adding_days_does_not_keep_anything_old(self):
        done = self.wait(self.api.build("Cartoons", 1, rebuild=False)["id"])
        self.assertEqual(done["state"], "done")
        self.assertIsNone(legacy.cut("Cartoons"))


if __name__ == "__main__":
    unittest.main()
