"""Premiere nights: new episodes air in a marked slot, one per night, and the show's other slots loop the older ones.

Run: python3 test_premieres.py
"""
import copy
import os
import shutil
import tempfile
import unittest
from datetime import date

import premieres
import schedules as sc

FS42 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fieldstation42", "src")
import sys
sys.path.insert(0, FS42)
try:
    from fs42.config_processor import ConfigProcessor
    from fs42.slot_reader import SlotReader
except Exception:                                           # the engine's sources are not beside this copy
    ConfigProcessor = SlotReader = None


def day(*blocks):
    return {"videos": [dict(b) for b in blocks], "commercials": [], "cards": []}


def defn(frm="Season 2", since="2026-10-12", events=()):
    """Monday to Friday 20:00-21:00 plays 'Show'; Friday's is the premiere slot. Everything else plays 'Other'."""
    week = {}
    for d in sc.DAYS:
        blocks = [{"from": 0, "to": 20, "lists": ["Other"]}, {"from": 20, "to": 21, "lists": ["Show"]}, {"from": 21, "to": 24, "lists": ["Other"]}] \
            if d in sc.DAYS[:5] else [{"from": 0, "to": 24, "lists": ["Other"]}]
        if d == "friday":
            blocks[1]["premiere"] = True
        week[d] = day(*blocks)
    raw = {"default": week, "seasons": {"dates": dict(sc.DEFAULT_DATES), **{s: None for s in sc.SEASONS}}, "events": list(events),
           "premieres": {"Show": {"from": frm, "since": since}}}
    return sc.validate(raw)


class Conf(unittest.TestCase):
    def info(self, n_new=3, loop=".loop.Show"):
        return {"Show": {"loop": loop, "new": [premieres.new_tag("Show", k) for k in range(n_new)]}}

    def test_a_premiere_flag_needs_one_list(self):
        raw = copy.deepcopy(defn())
        raw["default"]["friday"]["videos"][1]["lists"] = ["Show", "Other"]
        with self.assertRaises(sc.ScheduleError):
            sc.validate(raw)

    def test_settings_for_a_show_nothing_premieres_are_dropped(self):
        raw = defn()
        raw["premieres"]["Ghost"] = {"from": "x", "since": "2026-01-01"}
        self.assertEqual(list(sc.validate(raw)["premieres"]), ["Show"])

    def test_other_airings_play_the_loop_in_order(self):
        parts = sc.conf_parts(defn(), date(2026, 10, 14), (), (), self.info())          # a Wednesday
        slot = parts["day_templates"][parts["wednesday"]]["20"]
        self.assertEqual(slot["tags"], ".loop.Show")
        self.assertEqual(slot["sequence"], sc.SERIES)

    def test_each_premiere_night_plays_its_own_new_episode(self):
        parts = sc.conf_parts(defn(), date(2026, 10, 14), (), (), self.info())
        nights = parts["date_overrides"]
        self.assertEqual(nights["October 16"]["20"]["tags"], ".new.Show.0")          # the first Friday since 12 October
        self.assertEqual(nights["October 23"]["20"]["tags"], ".new.Show.1")
        self.assertEqual(nights["October 30"]["20"]["tags"], ".new.Show.2")
        self.assertNotIn("sequence", nights["October 16"]["20"])
        self.assertEqual(list(nights["October 16"]), ["20"])                        # only the premiere hour is overridden

    def test_with_no_new_episode_left_the_night_continues_the_loop(self):
        parts = sc.conf_parts(defn(), date(2026, 10, 14), (), (), self.info(n_new=1))
        self.assertEqual(list(parts["date_overrides"]), ["October 16"])               # the other Fridays have nothing new

    def test_nothing_older_and_nothing_premiered_plays_the_show_itself(self):
        parts = sc.conf_parts(defn(frm="Season 1"), date(2026, 10, 12), ("Show",), (), {"Show": {"loop": None, "new": [".new.Show.0"]}})
        self.assertEqual(parts["day_templates"][parts["wednesday"]]["20"]["tags"], "Show")
        self.assertEqual(parts["day_templates"][parts["wednesday"]]["20"]["sequence"], sc.SERIES)

    def test_a_special_day_replacing_the_night_does_not_use_up_an_episode(self):
        ev = {"name": "Holiday", "when": {"type": "range", "start": "10-16", "end": "10-16", "year": 2026}, "replace": True,
              "mode": "same", "schedule": day({"from": 0, "to": 24, "lists": ["Other"]})}
        d = defn(events=[ev])
        parts = sc.conf_parts(d, date(2026, 10, 14), (), (), self.info())
        self.assertNotIn(".new.Show", str(parts["date_overrides"]["October 16"]))
        self.assertEqual(parts["date_overrides"]["October 23"]["20"]["tags"], ".new.Show.0")      # the first one is still the first

    def test_a_week_of_building_the_same_config_is_the_same(self):
        a = sc.conf_parts(defn(), date(2026, 10, 14), (), (), self.info())
        b = sc.conf_parts(defn(), date(2026, 10, 14), (), (), self.info())
        self.assertTrue(sc.conf_matches(a, b))

    def test_the_index_counts_nights_since_it_began(self):
        d = defn(since="2026-10-12")
        self.assertEqual([sc.premiere_index(d, "Show", date(2026, 10, n)) for n in (12, 16, 17, 23, 24)], [0, 0, 1, 1, 2])


@unittest.skipUnless(SlotReader, "needs the FieldStation42 sources")
class Engine(unittest.TestCase):
    """The config as FieldStation42's own slot logic reads it."""

    def tag(self, conf, when):
        from datetime import datetime
        conf = ConfigProcessor.preprocess({"network_name": "X", "channel_number": 5, **copy.deepcopy(conf)})
        slot, _ = SlotReader.get_slot(conf, when)
        return SlotReader.get_tag_from_slot(slot, when)[0] if slot else None

    def test_friday_has_the_new_episode_and_every_other_night_the_loop(self):
        from datetime import datetime
        info = {"Show": {"loop": ".loop.Show", "new": [premieres.new_tag("Show", k) for k in range(2)]}}
        conf = sc.conf_parts(defn(), date(2026, 10, 14), (), (), info)
        self.assertEqual(self.tag(conf, datetime(2026, 10, 16, 20, 5)), ".new.Show.0")      # the first Friday
        self.assertEqual(self.tag(conf, datetime(2026, 10, 16, 12)), "Other")               # the rest of that day is unchanged
        self.assertEqual(self.tag(conf, datetime(2026, 10, 15, 20, 5)), ".loop.Show")       # Thursday: the loop
        self.assertEqual(self.tag(conf, datetime(2026, 10, 23, 20, 5)), ".new.Show.1")      # the next Friday
        self.assertEqual(self.tag(conf, datetime(2026, 10, 30, 20, 5)), ".loop.Show")       # none left: the loop continues


class Folders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.media = os.path.join(self.tmp, "media")
        self.lists = os.path.join(self.tmp, "lists")
        for season, n in (("Season 1", 3), ("Season 2", 4)):
            os.makedirs(os.path.join(self.media, season))
            os.makedirs(os.path.join(self.lists, "Show", season))
            for i in range(1, n + 1):
                f = os.path.join(self.media, season, f"Show - S0{season[-1]}E0{i}.mkv")
                open(f, "w").close()
                os.symlink(f, os.path.join(self.lists, "Show", season, os.path.basename(f)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_older_episodes_are_the_loop_and_a_premiere_has_its_own_folder(self):
        d = defn()
        plans = premieres.plan(d, self.lists, date(2026, 10, 14))
        p = plans["Show"]
        self.assertEqual(len(p["pool"]), 3)                          # season 1; nothing has premiered before 14 October
        self.assertEqual(len(p["new"]), 4)
        self.assertEqual(sorted(p["need"]), [".new.Show.0", ".new.Show.1", ".new.Show.2"])    # the Fridays within the lookahead
        made = premieres.sync(self.lists, plans)
        self.assertIn(".loop.Show", made)
        loop = os.path.join(self.lists, ".loop.Show", "Season 1")
        self.assertEqual(len(os.listdir(loop)), 3)
        self.assertEqual(os.listdir(os.path.join(self.lists, ".new.Show.0")), ["Show - S02E01.mkv"])
        self.assertEqual(premieres.sync(self.lists, plans), [])      # nothing more to do the second time

    def test_premiered_episodes_join_the_loop_as_the_nights_pass(self):
        d = defn()
        later = premieres.plan(d, self.lists, date(2026, 10, 25))["Show"]       # two Fridays have aired
        self.assertEqual(len(later["pool"]), 3 + 2)
        self.assertTrue(later["pool"][-1].endswith("S02E02.mkv"))

    def test_nothing_older_means_no_loop_folder_until_something_premiered(self):
        p = premieres.plan(defn(frm="Season 1"), self.lists, date(2026, 10, 14))["Show"]
        self.assertIsNone(p["loop"])
        self.assertEqual(premieres.plan(defn(frm="Season 1"), self.lists, date(2026, 10, 17))["Show"]["loop"], ".loop.Show")

    def test_a_start_that_is_not_there_is_all_loop(self):
        p = premieres.plan(defn(frm="Season 9"), self.lists, date(2026, 10, 14))["Show"]
        self.assertEqual((len(p["pool"]), len(p["new"])), (7, 0))

    def test_episodes_and_folders(self):
        eps = premieres.episodes(os.path.join(self.lists, "Show"))
        self.assertEqual(eps[0], "Season 1/Show - S01E01.mkv")
        self.assertEqual(premieres.folders(eps), [("Season 1", 3), ("Season 2", 4)])
        self.assertEqual(premieres.boundary(eps, "Season 2"), 3)
        self.assertEqual(premieres.boundary(eps, "Season 2/Show - S02E03.mkv"), 5)


if __name__ == "__main__":
    unittest.main()
