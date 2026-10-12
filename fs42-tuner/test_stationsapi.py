"""Tests for stationsapi.py with a stand-in console. Run: python3 test_stationsapi.py"""
import copy
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta

import console_client
import legacy
import stationform as sf
import stationsapi
from stationsapi import NotFound, Stations


class FakeConsole:
    def __init__(self):
        self.confs, self.log = {}, []
        self.fail = None
        self.fail_update = None
        self.gate = None
        self.status = {"log": ["Starting catalog rebuild…"]}

    def stations(self):
        return copy.deepcopy(self.confs)

    def create(self, conf):
        self.log.append(("create", conf["network_name"]))
        self.confs[conf["network_name"]] = copy.deepcopy(conf)

    def update(self, conf, name=None):
        renamed = bool(name) and name != conf["network_name"]
        self.log.append(("rename", name, conf["network_name"]) if renamed else ("update", conf["network_name"]))
        if self.fail_update:
            raise console_client.ConsoleError(self.fail_update)
        if renamed:
            self.confs.pop(name)
        self.confs[conf["network_name"]] = copy.deepcopy(conf)

    def reset_schedule(self, name):
        self.log.append(("reset", name))

    def delete(self, name):
        self.log.append(("delete", name))
        self.confs.pop(name)

    def rebuild_catalog(self, name, progress=None, on_wait=None):
        if progress:
            progress(self.status)
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise console_client.ConsoleError(self.fail)
        self.log.append(("rebuild", name))

    def add_day(self, name, progress=None, on_wait=None):
        self.log.append(("day", name))

    def snap_breaks(self, name, progress=None, on_wait=None):
        self.log.append(("snap", name))

    def update_catalog(self, name, progress=None, on_wait=None):
        if progress:
            progress(self.status)
        if self.fail:
            raise console_client.ConsoleError(self.fail)
        self.log.append(("update", name))


def form(**kw):
    f = {"name": "Cartoons", "number": 12, "kind": "standard", "blocks": [{"from": 0, "to": 24, "lists": ["Cartoons"]}],
         "increment": 30, "bump": "bump", "commercials": ""}
    f.update(kw)
    return f


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.lists = os.path.join(self.root, "by-genre")
        for name in ("Cartoons", "bump", "commercials", "Movies"):
            os.makedirs(os.path.join(self.lists, name))
        open(os.path.join(self.lists, "Cartoons", "a.mp4"), "w").close()
        open(os.path.join(self.lists, "bump", "b.mp4"), "w").close()
        open(os.path.join(self.lists, "commercials", "c.mp4"), "w").close()
        self.db = os.path.join(self.root, "fluid.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        c.commit()
        c.close()
        self.console = FakeConsole()
        self.api = Stations(self.console, self.db, self.lists)
        # the old schedule a rebuild keeps goes in a file of its own: never the real one, which other tests would then read
        self._legacy_path, legacy.PATH = legacy.PATH, os.path.join(self.root, "legacy-schedule.db")

    def tearDown(self):
        if self.console.gate:
            self.console.gate.set()
        end = time.time() + 10
        while self.api.jobs._running() and time.time() < end:      # a job still running would write to the files being removed
            time.sleep(0.02)
        legacy.PATH = self._legacy_path
        shutil.rmtree(self.root)

    def block(self, station, start, end, title="Show", plan="[]"):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,?)",
                  (station, start.isoformat(), end.isoformat(), title, plan))
        c.commit()
        c.close()


class GuideApiTest(Base):
    """A guide channel: made through the console like any station, with music from inside the media."""

    def setUp(self):
        super().setUp()
        self.media = os.path.join(self.root, "media")
        os.makedirs(os.path.join(self.media, "music"))
        open(os.path.join(self.media, "music", "song.mp3"), "w").close()
        os.makedirs(os.path.join(self.media, "empty"))
        self.api.media_root = self.media

    def form(self, **kw):
        g = {"title": "Guide", "hours": 3, "scroll_speed": 1.0, "messages": ["Hi"], "music": os.path.join(self.media, "music")}
        g.update(kw)
        return {"name": "Guide", "number": 2, "kind": "guide", "guide": g}

    def test_it_saves_as_a_guide_station_and_needs_no_build(self):
        out = self.api.save(None, self.form())
        self.assertEqual((out["needs_build"], out["warnings"]), (False, []))
        conf = self.console.confs["Guide"]
        self.assertEqual((conf["network_type"], conf["play_sound"], conf["wrapper_guide"]["music"]), ("guide", False, os.path.join(self.media, "music")))
        row = self.api.listing()[0]
        self.assertEqual((row["type"], row["editable"], row["lists"]), ("guide", True, []))
        self.assertEqual(self.api.get("Guide")["form"]["guide"]["music"], os.path.join(self.media, "music"))

    def test_the_music_must_be_inside_the_media_exist_and_have_music_in_it(self):
        for music, text in ((os.path.join(self.root, "elsewhere"), "inside your media"), (os.path.join(self.media, "gone"), "not there"),
                            (os.path.join(self.media, "empty"), "no music")):
            with self.assertRaises(sf.FormError) as cm:
                self.api.save(None, self.form(music=music))
            self.assertIn(text, str(cm.exception))
        self.api.save(None, self.form(music=""))                          # no music is fine
        self.assertEqual(self.console.confs["Guide"]["wrapper_guide"]["music"], "")

    def test_there_is_nothing_to_build(self):
        self.api.save(None, self.form())
        with self.assertRaises(sf.FormError) as cm:
            self.api.build("Guide", 7)
        self.assertIn("drawn live", str(cm.exception))


class SeasonsApiTest(Base):
    """Seasons, special days and commercials by the hour: kept as a definition, written as the station's config."""

    def setUp(self):
        super().setUp()
        import schedules as sc
        self.sc = sc
        self._path = sc.PATH
        sc.PATH = os.path.join(self.root, "schedules.json")
        os.makedirs(os.path.join(self.lists, "Summer"))
        os.makedirs(os.path.join(self.lists, "Shark"))
        for n in ("Summer", "Shark"):
            open(os.path.join(self.lists, n, "x.mp4"), "w").close()

    def tearDown(self):
        self.sc.PATH = self._path
        super().tearDown()

    def day(self, *videos, ads=()):
        return {"videos": [{"from": a, "to": b, "lists": l} for a, b, l in videos],
                "commercials": [{"from": a, "to": b, "list": l} for a, b, l in ads]}

    def week(self, d):
        return {n: d for n in self.sc.DAYS}

    def form(self, **kw):
        defn = {"default": self.week(self.day((0, 24, ["Cartoons"]))),
                "seasons": {"dates": dict(self.sc.DEFAULT_DATES), "spring": None, "summer": self.week(self.day((0, 24, ["Summer"]))),
                          "fall": None, "winter": None},
                "events": [{"name": "Shark Week", "when": {"type": "range", "start": datetime.now().strftime("%m-%d"),
                                                            "end": datetime.now().strftime("%m-%d"), "year": None},
                            "replace": False, "mode": "same", "schedule": self.day((20, 22, ["Shark"]), ads=[(20, 22, "commercials")])}]}
        f = form(**kw)
        f.pop("blocks")
        f["schedule"] = defn
        return f

    def test_saving_keeps_the_definition_and_writes_the_layers_into_the_config(self):
        out = self.api.save(None, self.form())
        conf = self.console.confs["Cartoons"]
        self.assertIn("week_overrides", conf)
        self.assertEqual(len(conf["week_overrides"]), 4)
        self.assertIn("date_overrides", conf)                             # the event is today
        slot = list(conf["date_overrides"].values())[0]["20"]
        self.assertEqual(slot, {"tags": "Shark", "commercial_dir": "commercials"})
        self.assertEqual(self.sc.get("Cartoons")["events"][0]["name"], "Shark Week")
        self.assertEqual(out["warnings"], [])

    def test_the_lists_a_schedule_uses_must_exist(self):
        f = self.form()
        f["schedule"]["events"][0]["schedule"] = self.day((20, 22, ["Nope"]))
        with self.assertRaises(sf.FormError) as cm:
            self.api.save(None, f)
        self.assertIn("'Nope'", str(cm.exception))

    def test_it_reads_back_as_the_same_definition_and_a_hand_edit_makes_it_read_only(self):
        self.api.save(None, self.form())
        got = self.api.get("Cartoons")
        self.assertIsNone(got["why"])
        self.assertEqual(got["form"]["schedule"]["seasons"]["summer"]["monday"]["videos"][0]["lists"], ["Summer"])
        self.assertTrue(self.api.listing()[0]["editable"])
        self.console.confs["Cartoons"]["week_overrides"]["June 1 - August 31"]["monday"]["3"] = {"tags": "Hand"}
        self.assertFalse(self.api.listing()[0]["editable"])
        self.assertIn("console", self.api.get("Cartoons")["why"])
        with self.assertRaises(sf.FormError):
            self.api.save("Cartoons", self.form())                        # saving would replace the hand edit: refused

    def test_a_station_made_the_older_way_still_reads_and_gets_a_schedule_for_the_editor(self):
        self.api.save(None, form())                                       # the older form: blocks, no schedule
        got = self.api.get("Cartoons")
        self.assertEqual(got["form"]["blocks"], [{"from": 0, "to": 24, "lists": ["Cartoons"]}])
        self.assertEqual(got["form"]["schedule"]["default"]["monday"]["videos"][0]["lists"], ["Cartoons"])
        self.assertIsNone(self.sc.get("Cartoons"))

    def test_the_config_follows_the_dates(self):
        f = self.form()
        f["schedule"]["events"][0]["when"] = {"type": "range", "start": "07-21", "end": "07-27", "year": 2026}
        self.api.save(None, f)
        self.assertNotIn("date_overrides", self.console.confs["Cartoons"])           # far from July 2026 today
        self.assertEqual(self.api.sync_schedules(datetime(2026, 7, 14)), ["Cartoons"])
        self.assertIn("July 21", self.console.confs["Cartoons"]["date_overrides"])
        self.assertEqual(self.api.sync_schedules(datetime(2026, 7, 14)), [])             # already right
        self.assertEqual(self.api.sync_schedules(datetime(2026, 8, 5)), ["Cartoons"])
        self.assertNotIn("date_overrides", self.console.confs["Cartoons"])           # over: taken out again
        self.assertEqual(self.console.confs["Cartoons"]["channel_number"], 12)           # nothing else was touched

    def test_premiere_nights_make_their_folders_and_the_catalog_knows_them(self):
        from datetime import timedelta
        show = os.path.join(self.lists, "Series")
        for season, n in (("Season 1", 2), ("Season 2", 3)):
            os.makedirs(os.path.join(show, season))
            for i in range(1, n + 1):
                open(os.path.join(show, season, f"S0{season[-1]}E0{i}.mp4"), "w").close()
        today = datetime(2026, 10, 14)                                       # a Wednesday
        friday = self.sc.DAYS.index("friday")
        week = {n: self.day((0, 24, ["Cartoons"])) for n in self.sc.DAYS}
        for n in self.sc.DAYS[:5]:
            week[n] = self.day((0, 19, ["Cartoons"]), (19, 20, ["Series"]), (20, 24, ["Cartoons"]))
        week["friday"]["videos"][1]["premiere"] = True
        f = self.form()
        f["schedule"] = {"default": week, "seasons": {"dates": dict(self.sc.DEFAULT_DATES), **{s: None for s in self.sc.SEASONS}},
                         "events": [], "premieres": {"Series": {"from": "Season 2", "since": "2026-10-14"}}}
        self.api.save(None, f)
        conf = self.console.confs["Cartoons"]
        self.assertEqual(conf["day_templates"][conf["wednesday"]]["19"]["tags"], ".loop.Series")
        night = next(v for k, v in conf["date_overrides"].items() if v["19"]["tags"] == ".new.Series.0")
        self.assertEqual(night["19"]["tags"], ".new.Series.0")
        self.assertTrue(os.path.exists(os.path.join(self.lists, ".new.Series.0", "S02E01.mp4")))    # made by the save
        self.assertTrue(os.path.exists(os.path.join(self.lists, ".loop.Series", "Season 1", "S01E02.mp4")))
        self.assertEqual(self.api.get("Cartoons")["why"], None)                  # still an editable station
        # weeks later: the first three premieres have aired and joined the loop; the config and the catalog follow
        self.console.log.clear()
        later = datetime(2026, 11, 20)
        self.assertEqual(self.api.sync_schedules(later), ["Cartoons"])
        self.assertTrue(os.path.exists(os.path.join(self.lists, ".loop.Series", "Season 2", "S02E03.mp4")))
        self.assertIn(("update", "Cartoons"), self.console.log)
        self.console.log.clear()
        self.assertEqual(self.api.sync_schedules(later), [])                     # nothing new: no config change, no catalog update
        self.assertNotIn(("update", "Cartoons"), self.console.log)

    def test_rename_and_delete_take_the_definition_along(self):
        self.api.save(None, self.form())
        self.console.confs["Cartoons"]["_has_schedule"] = False
        self.api.rename("Cartoons", "Toons")
        self.assertIsNone(self.sc.get("Cartoons"))
        self.assertIsNotNone(self.sc.get("Toons"))
        self.api.delete("Toons")
        self.assertIsNone(self.sc.get("Toons"))

    def test_the_preview_says_which_layer_wins_each_day(self):
        out = self.api.preview(self.form()["schedule"], 3, now=datetime(2026, 7, 14))
        self.assertEqual([d["layer"] for d in out["days"]], ["season", "season", "season"])
        self.assertEqual(out["days"][0]["name"], "summer")
        with self.assertRaises(sf.FormError):
            self.api.preview({"default": {}})


class ChangeTest(Base):
    def test_creating_a_station_goes_through_the_console_and_says_it_needs_building(self):
        out = self.api.save(None, form())
        self.assertEqual(self.console.log, [("create", "Cartoons")])
        self.assertEqual((out["created"], out["needs_build"], out["warnings"]), (True, True, []))
        conf = self.console.confs["Cartoons"]
        self.assertEqual((conf["channel_number"], conf["content_dir"]), (12, "catalog/by-genre"))

    def test_editing_updates_and_keeps_what_the_form_does_not_own(self):
        self.api.save(None, form())
        self.console.confs["Cartoons"]["standby_image"] = "runtime/standby.png"
        out = self.api.save("Cartoons", form(number=13, blocks=[{"from": 0, "to": 24, "lists": ["Movies"]}]))
        self.assertEqual((out["created"], self.console.log[-1]), (False, ("update", "Cartoons")))
        self.assertEqual(self.console.confs["Cartoons"]["standby_image"], "runtime/standby.png")
        self.assertEqual(self.console.confs["Cartoons"]["channel_number"], 13)

    def test_a_bad_form_never_reaches_the_console(self):
        for bad in (form(name=""), form(number=-1), form(blocks=[]), form(kind="web")):
            with self.assertRaises(sf.FormError):
                self.api.save(None, bad)
        self.assertEqual(self.console.log, [])

    def test_a_clash_with_another_station_is_refused(self):
        self.api.save(None, form())
        with self.assertRaises(sf.FormError) as cm:
            self.api.save(None, form(name="Other"))                          # same number 12
        self.assertIn("channel 12", str(cm.exception))
        with self.assertRaises(sf.FormError):
            self.api.save(None, form(number=99))                             # same name

    def test_lists_that_do_not_exist_are_refused_with_their_names(self):
        with self.assertRaises(sf.FormError) as cm:
            self.api.save(None, form(blocks=[{"from": 0, "to": 24, "lists": ["Nope", "Cartoons"]}]))
        self.assertIn("'Nope'", str(cm.exception))
        with self.assertRaises(sf.FormError):
            self.api.save(None, form(commercials="missing"))
        self.assertEqual(self.console.log, [])

    def test_an_empty_list_is_allowed_but_warned_about(self):
        out = self.api.save(None, form(blocks=[{"from": 0, "to": 24, "lists": ["Movies"]}]))
        self.assertTrue(any("'Movies' has no videos" in w for w in out["warnings"]))

    def test_a_station_cannot_be_renamed_and_an_advanced_one_is_not_flattened(self):
        self.api.save(None, form())
        with self.assertRaises(sf.FormError) as cm:
            self.api.save("Cartoons", form(name="Renamed"))
        self.assertIn("use Rename", str(cm.exception))
        self.console.confs["Cartoons"]["date_overrides"] = {"2026-01-01": "x"}
        with self.assertRaises(sf.FormError) as cm:
            self.api.save("Cartoons", form())
        self.assertIn("console", str(cm.exception))
        with self.assertRaises(NotFound):
            self.api.save("Ghost", form(name="Ghost", number=77))

    def test_delete(self):
        self.api.save(None, form())
        self.api.delete("Cartoons")
        self.assertEqual(self.console.confs, {})
        with self.assertRaises(NotFound):
            self.api.delete("Cartoons")


class WeekApiTest(Base):
    def days_form(self):
        movies = [{"from": 0, "to": 24, "lists": ["Movies"]}]
        base = [{"from": 0, "to": 24, "lists": ["Cartoons"]}]
        f = form(blocks=None)
        f["days"] = {d: (movies if d in ("saturday", "sunday") else base) for d in sf.DAYS}
        return f

    def test_a_station_with_a_different_weekend_is_saved_listed_and_read_back(self):
        out = self.api.save(None, self.days_form())
        self.assertEqual(out["created"], True)
        conf = self.console.confs["Cartoons"]
        self.assertEqual((conf["monday"], conf["saturday"]), ("schedule1", "schedule2"))
        row = self.api.listing(datetime(2026, 10, 5, 12))[0]
        self.assertEqual((row["editable"], row["lists"]), (True, ["Cartoons", "Movies"]))
        got = self.api.get("Cartoons")["form"]
        self.assertEqual(got["days"]["sunday"][0]["lists"], ["Movies"])

    def test_every_list_in_every_day_must_exist(self):
        f = self.days_form()
        f["days"]["friday"] = [{"from": 0, "to": 24, "lists": ["Nope"]}]
        with self.assertRaises(sf.FormError) as cm:
            self.api.save(None, f)
        self.assertIn("'Nope'", str(cm.exception))

    def test_an_empty_list_on_just_the_weekend_is_still_warned_about(self):
        out = self.api.save(None, self.days_form())                      # Movies has no videos
        self.assertTrue(any("'Movies' has no videos" in w for w in out["warnings"]))


class ReadTest(Base):
    def test_listing_says_what_each_station_is_and_whether_it_is_on_air(self):
        self.api.save(None, form())
        self.console.confs["Web"] = {"network_name": "Web", "channel_number": 3, "network_type": "web", "web_url": "x"}
        now = datetime(2026, 10, 5, 12, 0)
        self.block("Cartoons", datetime(2026, 10, 5), datetime(2026, 10, 9))
        rows = {r["name"]: r for r in self.api.listing(now)}
        self.assertEqual(list(rows), ["Web", "Cartoons"])                     # by channel number
        c = rows["Cartoons"]
        self.assertEqual((c["editable"], c["lists"], c["on_air"], c["low"]), (True, ["Cartoons"], True, False))
        self.assertEqual(c["scheduled_until"], "2026-10-09T00:00:00")
        w = rows["Web"]
        self.assertEqual((w["editable"], w["on_air"], w["low"]), (False, False, True))
        self.assertIn("console", w["why"])

    def test_the_hidden_extra_day_is_not_shown(self):
        self.api.save(None, form())
        self.api.horizon = lambda: 7                                          # the keeper shows 7 days, builds 8
        now = datetime(2026, 10, 5, 12, 0)
        self.block("Cartoons", datetime(2026, 10, 5), datetime(2026, 10, 13))      # really runs to the end of the 12th
        row = self.api.listing(now)[0]
        self.assertEqual(row["scheduled_until"], "2026-10-12T00:00:00")          # shown: 7 days from the start of today
        self.assertTrue(row["on_air"])
        self.block("Cartoons", datetime(2026, 10, 12, 10), datetime(2026, 10, 12, 11), "Hidden day")
        self.assertEqual(self.api.schedule("Cartoons", date(2026, 10, 12), now)["blocks"], [])
        s = self.api.schedule("Cartoons", date(2026, 10, 12), now)
        self.assertTrue(s["beyond"])
        self.assertFalse(self.api.schedule("Cartoons", date(2026, 10, 11), now)["beyond"])
        self.api.horizon = lambda: None                                       # keeper off: everything is shown
        self.assertEqual(self.api.listing(now)[0]["scheduled_until"], "2026-10-13T00:00:00")

    def test_running_low_means_less_than_a_day_left(self):
        self.api.save(None, form())
        now = datetime(2026, 10, 5, 12, 0)
        self.block("Cartoons", datetime(2026, 10, 5), datetime(2026, 10, 6, 6, 0))     # 18 hours left
        self.assertTrue(self.api.listing(now)[0]["low"])

    def test_get_returns_the_form_or_the_reason_it_cannot(self):
        self.api.save(None, form())
        got = self.api.get("Cartoons")
        self.assertEqual((got["form"]["number"], got["why"]), (12, None))
        self.console.confs["Cartoons"]["network_type"] = "streaming"
        got = self.api.get("Cartoons")
        self.assertIsNone(got["form"])
        self.assertIn("console", got["why"])
        with self.assertRaises(NotFound):
            self.api.get("Ghost")

    def test_the_schedule_for_a_day_marks_what_is_on_now(self):
        self.api.save(None, form())
        now = datetime(2026, 10, 5, 10, 30)
        self.block("Cartoons", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 11), "Morning")
        self.block("Cartoons", datetime(2026, 10, 5, 11), datetime(2026, 10, 5, 12), "Late")
        self.block("Cartoons", datetime(2026, 10, 6, 10), datetime(2026, 10, 6, 11), "Tomorrow")
        s = self.api.schedule("Cartoons", date(2026, 10, 5), now)
        self.assertEqual([(b["title"], b["now"]) for b in s["blocks"]], [("Morning", True), ("Late", False)])
        self.assertEqual([b["title"] for b in self.api.schedule("Cartoons", date(2026, 10, 6), now)["blocks"]], ["Tomorrow"])
        self.assertEqual(self.api.schedule("Cartoons", date(2026, 10, 9), now)["blocks"], [])
        with self.assertRaises(NotFound):
            self.api.schedule("Ghost", date(2026, 10, 5), now)

    def test_a_huge_day_is_cut_and_says_so(self):
        self.api.save(None, form())
        saved, stationsapi.MAX_BLOCKS = stationsapi.MAX_BLOCKS, 3
        try:
            for i in range(6):
                self.block("Cartoons", datetime(2026, 10, 5, i), datetime(2026, 10, 5, i, 30))
            s = self.api.schedule("Cartoons", date(2026, 10, 5), datetime(2026, 10, 5, 12))
        finally:
            stationsapi.MAX_BLOCKS = saved
        self.assertEqual((len(s["blocks"]), s["truncated"]), (3, True))


class BlockTest(Base):
    PLAN = json.dumps([
        {"path": "catalog/by-genre/Cartoons/Show/S1/ep1.mp4", "skip": 0, "duration": 600.0, "is_stream": False, "content_type": "feature"},
        {"path": "bump/b.mp4", "skip": 0, "duration": 5.0, "is_stream": False, "content_type": "bump"},
        {"path": "commercials/ad.mp4", "skip": 0, "duration": 30.0, "is_stream": False, "content_type": "commercial"},
        {"path": "catalog/by-genre/Cartoons/Show/S1/ep1.mp4", "skip": 600.0, "duration": 600.0, "is_stream": False, "content_type": "feature"}])

    def setUp(self):
        super().setUp()
        self.api.save(None, form())
        self.start = datetime(2026, 10, 5, 10)
        self.block("Cartoons", self.start, self.start + timedelta(hours=1), "Show", self.PLAN)

    def test_a_file_shows_its_catalog_title_when_there_is_one(self):
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE catalog_entries (id INTEGER PRIMARY KEY, station TEXT, path TEXT, title TEXT)")
        c.execute("INSERT INTO catalog_entries (station,path,title) VALUES ('Cartoons','catalog/by-genre/Cartoons/Show/S1/ep1.mp4','Episode One')")
        c.commit()
        c.close()
        entries = self.api.block("Cartoons", self.start)["entries"]
        self.assertEqual([e["title"] for e in entries], ["Episode One", None, None, "Episode One"])

    def test_the_files_in_a_block_come_with_their_times_and_kinds(self):
        out = self.api.block("Cartoons", self.start)
        self.assertEqual((out["title"], out["total"], out["offset"]), ("Show", 4, 0))
        self.assertEqual([(e["file"], e["type"], e["duration"]) for e in out["entries"]],
                         [("ep1.mp4", "feature", 600.0), ("b.mp4", "bump", 5.0), ("ad.mp4", "commercial", 30.0), ("ep1.mp4", "feature", 600.0)])
        self.assertEqual([e["at"] for e in out["entries"]],
                         ["2026-10-05T10:00:00", "2026-10-05T10:10:00", "2026-10-05T10:10:05", "2026-10-05T10:10:35"])

    def test_it_pages_through_a_long_block(self):
        self.assertEqual([e["type"] for e in self.api.block("Cartoons", self.start, offset=1, limit=2)["entries"]], ["bump", "commercial"])
        self.assertEqual(self.api.block("Cartoons", self.start, offset=3, limit=2)["entries"][0]["type"], "feature")
        self.assertEqual(self.api.block("Cartoons", self.start, offset=99)["entries"], [])
        saved, stationsapi.MAX_ENTRIES = stationsapi.MAX_ENTRIES, 2
        try:
            self.assertEqual(len(self.api.block("Cartoons", self.start, limit=100)["entries"]), 2)       # a page is capped
        finally:
            stationsapi.MAX_ENTRIES = saved

    def test_a_missing_block_or_station_is_not_found(self):
        with self.assertRaises(NotFound):
            self.api.block("Cartoons", self.start + timedelta(hours=5))
        with self.assertRaises(NotFound):
            self.api.block("Ghost", self.start)

    def test_a_block_with_a_damaged_plan_does_not_crash(self):
        later = self.start + timedelta(hours=2)
        self.block("Cartoons", later, later + timedelta(hours=1), "Broken", "{not json")
        out = self.api.block("Cartoons", later)
        self.assertEqual((out["total"], out["entries"]), (0, []))


class RenameTest(Base):
    def setUp(self):
        super().setUp()
        self.api.save(None, form())
        now = datetime.now()
        self.block("Cartoons", now - timedelta(hours=1), now + timedelta(days=3))        # it has a schedule
        self.console.log.clear()

    def wait(self, job):
        end = time.time() + 5
        while time.time() < end and self.api.jobs.get(job["id"])["state"] == "running":
            time.sleep(0.02)

    def test_a_station_with_a_schedule_is_cleared_renamed_and_rebuilt_under_the_new_name(self):
        out = self.api.rename("Cartoons", "Toons")
        self.wait(out["job"])
        self.assertEqual((out["name"], out["old"]), ("Toons", "Cartoons"))
        self.assertEqual(self.console.log[:2], [("reset", "Cartoons"), ("rename", "Cartoons", "Toons")])   # old schedule first
        self.assertEqual(self.console.log[2], ("rebuild", "Toons"))
        self.assertEqual(self.console.log[3:], [("day", "Toons")] * 7)
        self.assertEqual(list(self.console.confs), ["Toons"])
        self.assertEqual(self.console.confs["Toons"]["channel_number"], 12)                      # the number is kept

    def test_a_station_that_never_had_a_schedule_is_just_renamed(self):
        self.api.save(None, form(name="Fresh", number=40))
        self.console.log.clear()
        out = self.api.rename("Fresh", "Newer")
        self.assertIsNone(out["job"])
        self.assertEqual(self.console.log, [("rename", "Fresh", "Newer")])                       # no reset, no build

    def test_everything_the_form_does_not_own_is_kept(self):
        self.console.confs["Cartoons"]["standby_image"] = "runtime/standby.png"
        self.console.confs["Cartoons"]["date_overrides"] = {"2026-01-01": "x"}                   # an advanced station
        out = self.api.rename("Cartoons", "Toons")
        self.wait(out["job"])
        self.assertEqual(self.console.confs["Toons"]["standby_image"], "runtime/standby.png")
        self.assertEqual(self.console.confs["Toons"]["date_overrides"], {"2026-01-01": "x"})

    def test_bad_names_and_clashes_are_refused_before_anything_changes(self):
        self.api.save(None, form(name="Other", number=50))
        self.console.log.clear()
        for bad, word in (("", "name"), ("a/b", "slashes"), (".x", "dot"), ("Cartoons", "already its name"),
                          ("other", "already a station"), ("OTHER", "already a station")):
            with self.assertRaises(sf.FormError, msg=bad) as cm:
                self.api.rename("Cartoons", bad)
            self.assertIn(word, str(cm.exception))
        with self.assertRaises(NotFound):
            self.api.rename("Ghost", "Whatever")
        self.assertEqual(self.console.log, [])

    def test_a_station_being_built_is_not_renamed_under_the_build(self):
        gate = threading.Event()
        self.console.gate = gate
        self.api.jobs.start("Cartoons", 1, rebuild=True)
        with self.assertRaises(sf.FormError) as cm:
            self.api.rename("Cartoons", "Toons")
        self.assertIn("being built", str(cm.exception))
        gate.set()

    def test_if_the_console_refuses_after_the_clear_the_message_says_so(self):
        self.console.fail_update = "name is not allowed"
        with self.assertRaises(console_client.ConsoleError) as cm:
            self.api.rename("Cartoons", "Toons")
        self.assertIn("name is not allowed", str(cm.exception))
        self.assertIn("schedule was already cleared", str(cm.exception))


class TitlesTest(Base):
    def test_titles_come_from_the_catalog_and_fall_back_to_nothing(self):
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE catalog_entries (id INTEGER PRIMARY KEY, station TEXT, path TEXT, title TEXT)")
        c.execute("INSERT INTO catalog_entries (station,path,title) VALUES ('S','a/x.mp4','A Real Title'), ('S','a/y.mp4',''), ('T','a/x.mp4','Other Station')")
        c.commit()
        c.close()
        import resolver
        self.assertEqual(resolver.titles(self.db, "S", ["a/x.mp4", "a/y.mp4", "a/none.mp4"]), {"a/x.mp4": "A Real Title"})
        self.assertEqual(resolver.titles(self.db, "S", []), {})
        self.assertEqual(resolver.titles(self.db, "Nobody", ["a/x.mp4"]), {})

    def test_no_catalog_table_is_fine(self):
        import resolver
        self.assertEqual(resolver.titles(self.db, "S", ["a/x.mp4"]), {})
        self.assertEqual(resolver.titles(os.path.join(self.root, "missing.db"), "S", ["a"]), {})

    def test_big_pages_are_looked_up_in_chunks(self):
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE catalog_entries (id INTEGER PRIMARY KEY, station TEXT, path TEXT, title TEXT)")
        c.executemany("INSERT INTO catalog_entries (station,path,title) VALUES ('S',?,?)", [(f"p/{i}.mp4", f"T{i}") for i in range(1000)])
        c.commit()
        c.close()
        import resolver
        got = resolver.titles(self.db, "S", [f"p/{i}.mp4" for i in range(1000)])
        self.assertEqual((len(got), got["p/999.mp4"]), (1000, "T999"))


class JobsTest(Base):
    def wait(self, job_id, state="running", timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            j = self.api.jobs.get(job_id)
            if j["state"] != state:
                return j
            time.sleep(0.02)
        self.fail("the job did not finish")

    def test_a_build_rebuilds_the_catalog_then_adds_the_days_in_order(self):
        self.api.save(None, form())
        self.console.log.clear()
        job = self.api.build("Cartoons", 3)
        done = self.wait(job["id"])
        self.assertEqual(done["state"], "done")
        self.assertEqual(self.console.log, [("rebuild", "Cartoons")] + [("day", "Cartoons")] * 3)

    def test_resuming_a_rebuild_whose_catalog_was_built_only_adds_the_days_that_are_missing(self):
        self.api.save(None, form())
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE catalog_entries (station TEXT, path TEXT)")
        c.execute("INSERT INTO catalog_entries VALUES ('Cartoons', 'a.mp4')")
        c.commit()
        c.close()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.block("Cartoons", datetime.now(), today + timedelta(days=3))              # three days were built before it stopped
        self.console.log.clear()
        done = self.wait(self.api.build("Cartoons", 7, resume=True)["id"])
        self.assertEqual(done["state"], "done")
        self.assertEqual(self.console.log, [("snap", "Cartoons")] + [("day", "Cartoons")] * 4)      # no catalog rebuild, 4 more days

    def test_resuming_with_no_catalog_starts_from_the_catalog(self):
        self.api.save(None, form())
        self.console.log.clear()
        done = self.wait(self.api.build("Cartoons", 2, resume=True)["id"])
        self.assertEqual(self.console.log, [("rebuild", "Cartoons")] + [("day", "Cartoons")] * 2)

    def test_updating_videos_refreshes_the_catalog_and_leaves_the_schedule_alone(self):
        self.api.save(None, form())
        self.console.log.clear()
        job = self.api.build("Cartoons", 7, rebuild=False, refresh=True)
        done = self.wait(job["id"])
        self.assertEqual(done["state"], "done")
        self.assertEqual(self.console.log, [("update", "Cartoons")])          # no rebuild, no reset, no days
        self.assertEqual(done["days"], 0)
        self.assertIn("next time days are added", done["message"])

    def test_a_failed_update_says_why_and_still_leaves_the_schedule_alone(self):
        self.api.save(None, form())
        self.console.log.clear()
        self.console.fail = "no space left"
        done = self.wait(self.api.build("Cartoons", 1, refresh=True)["id"])
        self.assertEqual((done["state"], self.console.log), ("error", []))
        self.assertIn("no space left", done["message"])

    def test_extending_adds_days_without_touching_the_catalog(self):
        self.api.save(None, form())
        self.console.log.clear()
        done = self.wait(self.api.build("Cartoons", 2, rebuild=False)["id"])
        self.assertEqual((done["state"], self.console.log), ("done", [("day", "Cartoons")] * 2))

    def test_a_failure_ends_the_job_with_the_reason(self):
        self.api.save(None, form())
        self.console.fail = "no bump clips"
        done = self.wait(self.api.build("Cartoons", 2)["id"])
        self.assertEqual(done["state"], "error")
        self.assertIn("no bump clips", done["message"])
        self.assertNotIn(("day", "Cartoons"), self.console.log)               # it stopped there

    def test_a_running_build_marks_the_system_busy_and_clears_it_when_done(self):
        import stations as stationlib
        saved, stationlib.JOBS_BUSY = stationlib.JOBS_BUSY, os.path.join(self.root, "busy")
        try:
            self.api.save(None, form())
            self.console.gate = threading.Event()
            job = self.api.build("Cartoons", 1)
            self.assertTrue(stationlib.is_busy())                        # the supervisor will wait
            self.console.gate.set()
            self.wait(job["id"])
            end = time.time() + 5
            while stationlib.is_busy() and time.time() < end:
                time.sleep(0.05)
            self.assertFalse(stationlib.is_busy())                       # and goes ahead once it is over
        finally:
            stationlib.JOBS_BUSY = saved

    def test_a_running_build_reports_elapsed_time_and_its_last_log_line(self):
        self.api.save(None, form())
        self.console.gate = threading.Event()
        job = self.api.build("Cartoons", 1)
        end = time.time() + 5
        while "so far" not in (self.api.jobs.get(job["id"]) or {}).get("message", "") and time.time() < end:
            time.sleep(0.02)
        j = self.api.jobs.get(job["id"])
        self.assertIn("m 0", j["message"])
        self.assertIn("you can leave this page", j["message"])
        self.assertEqual(j["detail"], "Starting catalog rebuild…")
        row = [r for r in self.api.listing() if r["name"] == "Cartoons"][0]       # a reloaded page sees it too
        self.assertEqual(row["job"]["id"], job["id"])
        self.assertEqual([x["id"] for x in self.api.jobs.list()], [job["id"]])
        self.console.gate.set()
        self.wait(job["id"])
        row = [r for r in self.api.listing() if r["name"] == "Cartoons"][0]
        self.assertIsNone(row["job"])

    def test_after_every_video_is_checked_it_says_the_chapter_pass_is_running(self):
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE file_meta (path TEXT)")
        c.execute("CREATE TABLE chapter_points (path TEXT)")
        c.executemany("INSERT INTO file_meta VALUES (?)", [(str(i),) for i in range(3)])
        c.commit()
        job = {"started": time.time()}
        progress = self.api.jobs._progress_for(job, 0, 3, 0)
        progress({})
        self.assertIn("all 3 videos checked; now reading chapter marks: 0 of 3", job["message"])
        c.execute("INSERT INTO chapter_points VALUES ('0')")
        c.commit()
        progress({})
        self.assertIn("chapter marks: 1 of 3", job["message"])
        c.executemany("INSERT INTO chapter_points VALUES (?)", [("1",), ("2",)])
        c.commit()
        c.close()
        progress({})
        self.assertNotIn("chapter marks", job["message"])                       # both passes done: just the time and count
        self.assertIn("3 of 3 videos checked", job["message"])

    def test_days_are_clamped_and_one_build_at_a_time_per_station(self):
        self.api.save(None, form())
        self.console.gate = threading.Event()
        job = self.api.build("Cartoons", 9999)
        self.assertEqual(job["days"], stationsapi.MAX_DAYS)
        with self.assertRaises(sf.FormError) as cm:
            self.api.build("Cartoons", 1)
        self.assertIn("already being built", str(cm.exception))
        self.console.gate.set()
        self.wait(job["id"])
        self.assertEqual(self.api.build("Cartoons", 0)["days"], 1)           # at least one day
        with self.assertRaises(NotFound):
            self.api.build("Ghost", 1)


class ShowSlotsTest(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.lists, "Sitcom"))
        open(os.path.join(self.lists, "Sitcom", "e1.mp4"), "w").close()
        open(os.path.join(self.lists, "Sitcom", ".show"), "w").write("tv/Sitcom")

    def test_a_show_in_a_slot_is_played_in_order_and_reads_back_as_the_same_form(self):
        self.api.save(None, form(blocks=[{"from": 0, "to": 20, "lists": ["Cartoons"]}, {"from": 20, "to": 21, "lists": ["Sitcom"]},
                                         {"from": 21, "to": 24, "lists": ["Cartoons"]}]))
        tmpl = next(iter(self.console.confs["Cartoons"]["day_templates"].values()))
        self.assertEqual(tmpl["20"], {"tags": "Sitcom", "sequence": "series"})
        self.assertEqual(tmpl["12"], {"tags": "Cartoons"})                      # a list is still random
        got = self.api.get("Cartoons")["form"]
        self.assertEqual([(b["from"], b["to"], b["lists"]) for b in got["schedule"]["default"]["monday"]["videos"]],
                         [(0, 20, ["Cartoons"]), (20, 21, ["Sitcom"]), (21, 24, ["Cartoons"])])


class InterruptedRebuildTest(Base):
    def test_a_rebuild_that_was_started_and_is_not_running_is_reported_as_interrupted(self):
        import legacy
        saved, legacy.PATH = legacy.PATH, os.path.join(self.root, "legacy.db")
        try:
            self.api.save(None, form())
            now = datetime.now()
            self.block("Cartoons", now - timedelta(hours=1), now + timedelta(hours=5), plan='[{"path": "a.mp4", "skip": 0, "duration": 60}]')
            self.assertTrue(legacy.snapshot(self.db, "Cartoons", now=now))                  # a rebuild began: the old schedule is kept
            row = next(r for r in self.api.listing(now) if r["name"] == "Cartoons")
            self.assertTrue(row["old_schedule"]["interrupted"])                             # no job is running (the server restarted)
            self.api.jobs.running = lambda name: {"id": "x", "station": name}
            row = next(r for r in self.api.listing(now) if r["name"] == "Cartoons")
            self.assertNotIn("interrupted", row["old_schedule"])                            # a running one is just in progress
        finally:
            legacy.PATH = saved


class ProgressTextTest(unittest.TestCase):
    def test_a_build_of_videos_all_read_before_says_why_it_shows_no_count(self):
        import time
        jobs = stationsapi.Jobs(None, None, None)
        job = {"started": time.time() - 30, "refresh": False}
        jobs._progress_for(job, 0, 1268)({})
        self.assertIn("going through 1268 videos", job["message"])
        self.assertIn("not read again", job["message"])


class BuiltCallbackTest(unittest.TestCase):
    def test_the_guide_is_told_when_a_build_has_finished(self):
        told = []
        jobs = stationsapi.Jobs(None, None, None)
        jobs.on_built = told.append
        jobs._run_steps = lambda job: job.update(state="done")
        jobs._run({"station": "Toons", "state": "running"})
        self.assertEqual(told, ["Toons"])
        failed = []
        jobs.on_built = failed.append
        jobs._run_steps = lambda job: job.update(state="error")
        jobs._run({"station": "Toons", "state": "running"})
        self.assertEqual(failed, [])                              # a failed build changes nothing in the guide


if __name__ == "__main__":
    unittest.main()
