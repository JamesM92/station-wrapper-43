"""Tests for keeper.py. Run: python3 test_keeper.py"""
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

import console_client
import keeper
import settings
import stationform as sf
import stationsapi

NOW = datetime(2026, 10, 5, 12, 0)


class FakeConsole:
    def __init__(self):
        self.confs, self.days, self.down = {}, [], False
        self.gate = None

    def stations(self):
        if self.down:
            raise console_client.ConsoleError("the FieldStation42 console is not reachable right now")
        return dict(self.confs)

    def add_day(self, name, progress=None, on_wait=None):
        if self.gate:
            self.gate.wait(5)
        self.days.append(name)


class KeeperBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db = os.path.join(self.root, "f.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        c.commit()
        c.close()
        self.console = FakeConsole()
        self.api = stationsapi.Stations(self.console, self.db, os.path.join(self.root, "lists"))
        self.logs = []
        self.keeper = keeper.Keeper(self.api, log=self.logs.append)

    def tearDown(self):
        shutil.rmtree(self.root)

    def station(self, name, ends=None, **conf):
        self.console.confs[name] = {"network_name": name, "channel_number": len(self.console.confs) + 1, **conf}
        if ends is not None:
            c = sqlite3.connect(self.db)
            c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,'[]')",
                      (name, (ends - timedelta(days=1)).isoformat(), ends.isoformat(), "x"))
            c.commit()
            c.close()

    def wait_jobs(self):
        end = time.time() + 5
        while time.time() < end and any(j["state"] == "running" for j in self.api.jobs._jobs.values()):
            time.sleep(0.02)


class DaysNeededTest(unittest.TestCase):
    # NOW is 5 Oct 12:00: the schedule is kept through the end of 12 Oct (7 days shown) plus one hidden day: 13 Oct 00:00
    def test_zero_when_there_is_enough_and_whole_days_when_not(self):
        midnight = datetime(2026, 10, 13)
        self.assertEqual(keeper.days_needed(midnight, NOW, 7), 0)
        self.assertEqual(keeper.days_needed(midnight + timedelta(days=2), NOW, 7), 0)
        self.assertEqual(keeper.days_needed(midnight - timedelta(hours=1), NOW, 7), 1)          # an hour short: one more day
        self.assertEqual(keeper.days_needed(NOW + timedelta(days=5), NOW, 7), 3)
        self.assertEqual(keeper.days_needed(NOW + timedelta(hours=12), NOW, 7), 7)
        # a schedule that ran out three days ago has to catch up: one call per missing day
        self.assertEqual(keeper.days_needed(NOW - timedelta(days=3), NOW, 7), 11)

    def test_the_answer_only_changes_at_midnight(self):
        end = datetime(2026, 10, 13)
        self.assertEqual(keeper.days_needed(end, datetime(2026, 10, 5, 0, 1), 7), 0)
        self.assertEqual(keeper.days_needed(end, datetime(2026, 10, 5, 23, 59), 7), 0)
        self.assertEqual(keeper.days_needed(end, datetime(2026, 10, 6, 0, 1), 7), 1)           # a new day: one more


class PassTest(KeeperBase):
    def test_a_station_running_low_gets_the_missing_days(self):
        self.station("Cartoons", NOW + timedelta(days=2))
        actions = self.keeper.run_once(7, NOW)
        self.wait_jobs()
        self.assertEqual(actions, [("Cartoons", 6)])
        self.assertEqual(self.console.days, ["Cartoons"] * 6)
        self.assertTrue(any("Cartoons" in line and "6 day" in line for line in self.logs))

    def test_a_station_with_enough_schedule_is_left_alone(self):
        self.station("Full", datetime(2026, 10, 13))
        self.assertEqual(self.keeper.run_once(7, NOW), [])
        self.assertEqual(self.console.days, [])

    def test_a_station_that_ran_out_catches_up(self):
        self.station("Late", NOW - timedelta(days=3))
        self.assertEqual(self.keeper.run_once(7, NOW), [("Late", 11)])
        self.wait_jobs()
        self.assertEqual(len(self.console.days), 11)

    def test_the_extra_day_waits_for_the_morning_unless_the_station_is_about_to_run_out(self):
        night = datetime(2026, 10, 6, 1, 30)                              # a new day has started, but it is 1:30 am
        self.station("Fine", datetime(2026, 10, 13))                       # 6.9 days left: plenty
        self.station("Almost", night + timedelta(hours=6))                 # 6 hours left: not plenty
        got = dict(self.keeper.run_once(7, night))
        self.wait_jobs()
        self.assertEqual(list(got), ["Almost"])                            # topped up at once, at any hour
        morning = dict(self.keeper.run_once(7, datetime(2026, 10, 6, 4, 5)))
        self.assertEqual(morning["Fine"], 1)                               # the morning pass adds the hidden day

    def test_a_station_that_was_never_given_a_schedule_is_not_started_for_you(self):
        self.station("New")                                      # no blocks at all
        self.assertEqual(self.keeper.run_once(7, NOW), [])

    def test_hidden_stations_are_left_alone(self):
        self.station("Hidden", NOW, hidden=True)
        self.assertEqual(self.keeper.run_once(7, NOW), [])

    def test_a_station_being_built_by_hand_is_skipped_then_picked_up(self):
        self.station("Busy", NOW + timedelta(days=1))
        self.console.gate = threading.Event()
        self.api.jobs.start("Busy", 2, rebuild=False)            # you pressed Add days on the Stations page
        self.assertEqual(self.keeper.run_once(7, NOW), [])       # the keeper does not pile on
        self.console.gate.set()
        self.wait_jobs()
        self.console.days.clear()
        self.assertEqual(len(self.keeper.run_once(7, NOW)), 1)   # next pass, it is free

    def test_a_big_gap_is_done_in_steps_and_says_more_is_coming(self):
        self.station("Ancient", NOW - timedelta(days=60))
        actions = self.keeper.run_once(7, NOW)
        self.assertEqual(actions, [("Ancient", keeper.MAX_PER_PASS)])
        self.assertTrue(self.keeper.snapshot()["more"])
        self.assertTrue(any("more to catch up" in line for line in self.logs))

    def test_a_console_that_is_down_is_reported_not_fatal(self):
        self.station("Cartoons", NOW)
        self.console.down = True
        self.assertEqual(self.keeper.run_once(7, NOW), [])
        snap = self.keeper.snapshot()
        self.assertIn("not reachable", snap["error"])
        self.assertEqual(snap["last_pass"], NOW.isoformat())
        self.console.down = False
        self.assertEqual(len(self.keeper.run_once(7, NOW)), 1)
        self.assertIsNone(self.keeper.snapshot()["error"])

    def test_it_remembers_the_last_time_it_did_something(self):
        self.station("A", NOW + timedelta(days=1))
        self.keeper.run_once(7, NOW)
        self.wait_jobs()
        later = NOW + timedelta(hours=1)
        self.keeper.run_once(0, later)                           # asked to keep 0 days ahead: nothing to do this time
        snap = self.keeper.snapshot()
        self.assertEqual(snap["last_pass"], later.isoformat())
        self.assertEqual(snap["last_action"]["added"], [{"station": "A", "days": 7}])


class LoopTest(KeeperBase):
    def run_loop(self, **kw):
        self.slept = []
        self.keeper.loop(sleep=self.slept.append, now=lambda: NOW, forever=False, **kw)

    def test_the_loop_waits_a_moment_first_then_checks_and_schedules_the_next_pass(self):
        self.station("A", NOW + timedelta(days=1))
        saved = settings.PATH
        settings.PATH = os.path.join(self.root, "none.json")
        try:
            self.run_loop(interval_minutes=30, first_delay=45)
        finally:
            settings.PATH = saved
        self.assertEqual(self.slept, [45])                       # one pass then (forever=False) it stops
        self.assertTrue(self.keeper.snapshot()["last_pass"])
        self.assertEqual(self.keeper.snapshot()["next_pass"], (NOW + timedelta(minutes=30)).isoformat())

    def test_switched_off_means_no_pass(self):
        self.station("A", NOW + timedelta(days=1))
        path = os.path.join(self.root, "s.json")
        saved = settings.PATH
        settings.PATH = path
        try:
            settings.set_keeper({"enabled": False}, path)
            self.run_loop()
        finally:
            settings.PATH = saved
        self.assertEqual(self.console.days, [])
        self.assertIsNone(self.keeper.snapshot()["last_pass"])
        self.assertIsNone(self.keeper.snapshot()["next_pass"])

    def test_a_pass_that_blows_up_does_not_kill_the_loop(self):
        def boom(*a, **k):
            raise RuntimeError("surprise")
        self.keeper.run_once = boom
        saved = settings.PATH
        settings.PATH = os.path.join(self.root, "none.json")
        try:
            self.run_loop()
        finally:
            settings.PATH = saved
        self.assertTrue(any("pass failed: surprise" in line for line in self.logs))

    def test_while_catching_up_it_comes_back_in_a_minute_not_half_an_hour(self):
        self.station("Ancient", NOW - timedelta(days=60))
        saved = settings.PATH
        settings.PATH = os.path.join(self.root, "none.json")
        try:
            self.slept = []
            self.keeper.loop(sleep=self.slept.append, now=lambda: NOW, forever=False)
        finally:
            settings.PATH = saved
        self.assertEqual(self.keeper.snapshot()["next_pass"], (NOW + timedelta(seconds=60)).isoformat())


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_defaults_env_and_saved_in_that_order(self):
        k = settings.get_keeper({}, self.path)
        self.assertEqual((k["enabled"], k["days_ahead"]), (True, 7))
        k = settings.get_keeper({"SCHEDULE_KEEPER": "0", "KEEPER_DAYS_AHEAD": "14"}, self.path)
        self.assertEqual((k["enabled"], k["days_ahead"], k["source"]["enabled"]), (False, 14, "env"))
        settings.set_keeper({"enabled": True, "days_ahead": 3}, self.path)
        k = settings.get_keeper({"SCHEDULE_KEEPER": "0", "KEEPER_DAYS_AHEAD": "14"}, self.path)
        self.assertEqual((k["enabled"], k["days_ahead"], k["source"]["days_ahead"]), (True, 3, "saved"))

    def test_saving_validates_and_keeps_the_other_settings(self):
        for bad in ({"enabled": "yes"}, {"enabled": 1}, {"days_ahead": 0}, {"days_ahead": 31}, {"days_ahead": "7"}, {"days_ahead": True}):
            with self.assertRaises(settings.SettingsError, msg=str(bad)):
                settings.set_keeper(bad, self.path)
        self.assertFalse(os.path.exists(self.path))
        settings.set_ta({"enabled": True}, self.path)
        settings.set_keeper({"days_ahead": 10}, self.path)
        self.assertTrue(settings.get_ta(path=self.path)["enabled"])               # the TubeArchivist setting survived
        self.assertEqual(settings.get_keeper({}, self.path)["days_ahead"], 10)

    def test_odd_environment_values_never_raise(self):
        k = settings.get_keeper({"KEEPER_DAYS_AHEAD": "junk"}, self.path)
        self.assertEqual(k["days_ahead"], 7)
        self.assertEqual(settings.get_keeper({"KEEPER_DAYS_AHEAD": "999"}, self.path)["days_ahead"], 30)


if __name__ == "__main__":
    unittest.main()
