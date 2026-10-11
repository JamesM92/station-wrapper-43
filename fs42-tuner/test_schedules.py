"""Seasons and special days: validation, the config they make, and (as an oracle) FieldStation42's own slot logic.

Run: python3 test_schedules.py
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime

import schedules as sc

FS42 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fieldstation42", "src")
sys.path.insert(0, FS42)
try:
    from fs42.config_processor import ConfigProcessor
    from fs42.slot_reader import SlotReader
except Exception:                                           # the engine's sources are not beside this copy
    ConfigProcessor = SlotReader = None


def day(*videos, ads=()):
    return {"videos": [{"from": a, "to": b, "lists": list(l)} for a, b, l in videos],
            "commercials": [{"from": a, "to": b, "list": l} for a, b, l in ads]}


def week(d):
    return {n: copy.deepcopy(d) for n in sc.DAYS}


def plain(lst="Base"):
    return {"default": week(day((0, 24, [lst]))), "seasons": {"dates": dict(sc.DEFAULT_DATES), **{s: None for s in sc.SEASONS}}, "events": []}


def with_season(defn, name, lst):
    defn["seasons"][name] = week(day((0, 24, [lst])))
    return sc.validate(defn)


def event(name, when, mode="same", replace=False, sched=None):
    return {"name": name, "when": when, "replace": replace, "mode": mode,
            "schedule": sched if sched is not None else day((20, 22, ["Shark"]))}


def rng(a, b, year=None):
    return {"type": "range", "start": a, "end": b, "year": year}


class ValidateTest(unittest.TestCase):
    def bad(self, defn, text):
        with self.assertRaises(sc.ScheduleError) as cm:
            sc.validate(defn)
        self.assertIn(text, str(cm.exception))

    def test_a_plain_schedule_is_accepted_and_cleaned(self):
        c = sc.validate(plain())
        self.assertEqual(c["default"]["monday"]["videos"], [{"from": 0.0, "to": 24.0, "lists": ["Base"]}])
        self.assertEqual(c["events"], [])
        self.assertTrue(sc.is_plain(c))

    def test_a_week_may_leave_time_unassigned_it_plays_the_test_pattern(self):
        d = plain()
        d["default"]["tuesday"] = day((0, 12, ["A"]))                  # nothing after noon: off air, the test pattern
        d["default"]["wednesday"] = day((1, 24, ["A"]))
        d["default"]["thursday"] = day()                               # a whole empty day
        c = sc.validate(d)
        self.assertEqual(c["default"]["tuesday"]["videos"], [{"from": 0.0, "to": 12.0, "lists": ["A"]}])
        parts = sc.conf_parts(c, date(2026, 7, 14))
        tue = parts["day_templates"][parts["tuesday"]]
        self.assertEqual(sorted(tue, key=int), [str(h) for h in range(12)])    # no slot for the unassigned hours: FieldStation42 shows off_air_video
        self.assertEqual(parts["day_templates"][parts["thursday"]], {})

    def test_blocks_must_not_overlap(self):
        d = plain()
        d["default"]["monday"] = day((0, 13, ["A"]), (12, 24, ["B"]))
        self.bad(d, "overlap")

    def test_half_hours_are_allowed_with_one_list_each_side(self):
        d = plain()
        d["default"]["monday"] = day((0, 20.5, ["A"]), (20.5, 24, ["B"]))
        c = sc.validate(d)
        hours = sc.hours_of(c["default"]["monday"])
        self.assertEqual(hours["20"], {"tags": ["A", "B"]})                  # first half A, second half B
        d["default"]["monday"] = day((0, 20.5, ["A", "C"]), (20.5, 24, ["B"]))
        self.bad(d, "can hold only one list")
        d["default"]["monday"] = day((0, 20.25, ["A"]), (20.25, 24, ["B"]))
        self.bad(d, "half hours")

    def test_an_overlay_may_leave_gaps_but_not_a_lone_half_hour(self):
        d = plain()
        d["events"] = [event("Shark Week", rng("07-21", "07-27"), sched=day((20, 22, ["Shark"])))]
        sc.validate(d)                                                       # whole hours: fine
        d["events"] = [event("Shark Week", rng("07-21", "07-27"), sched=day((20, 21.5, ["Shark"])))]
        self.bad(d, "half filled")
        d["events"] = [event("X", rng("07-21", "07-27"), replace=True, sched=day((20, 22, ["Shark"])))]
        self.bad(d, "needs to be covered")                                    # replacing a day means filling it

    def test_commercials_change_on_the_hour_and_do_not_overlap(self):
        d = plain()
        d["default"]["monday"] = day((0, 24, ["A"]), ads=[(8, 12, "Ads")])
        self.assertEqual(sc.validate(d)["default"]["monday"]["commercials"], [{"from": 8, "to": 12, "list": "Ads"}])
        d["default"]["monday"]["commercials"] = [{"from": 8.5, "to": 12, "list": "Ads"}]
        self.bad(d, "on the hour")
        d["default"]["monday"]["commercials"] = [{"from": 8, "to": 12, "list": "Ads"}, {"from": 11, "to": 13, "list": "Ads2"}]
        self.bad(d, "overlap")

    def test_season_dates_are_checked(self):
        d = plain()
        d["seasons"]["dates"]["spring"] = "02-29"
        self.bad(d, "29 February")
        d["seasons"]["dates"]["spring"] = "06-01"
        self.bad(d, "own start date")

    def test_events_are_checked(self):
        d = plain()
        for ev, text in ((event("", rng("07-21", "07-27")), "needs a name"),
                         (event("X", {"type": "range", "start": "13-01", "end": "07-27", "year": None}), "not valid"),
                         (event("X", rng("07-27", "07-21", 2026)), "ends before it starts"),
                         (event("X", {"type": "nth", "month": 11, "weekday": "thursday", "nth": 6, "days": 1}), "1st to 5th"),
                         (event("X", rng("07-21", "07-27"), sched=day()), "draw at least one slot")):
            d["events"] = [ev]
            self.bad(d, text)

    def test_the_older_form_converts_to_a_default_week(self):
        f = {"blocks": [{"from": 0, "to": 6, "lists": ["A"]}, {"from": 6, "to": 24, "lists": ["B", "C"]}]}
        c = sc.validate(sc.from_legacy(f))
        self.assertEqual(c["default"]["sunday"]["videos"][1], {"from": 6.0, "to": 24.0, "lists": ["B", "C"]})


class HoursTest(unittest.TestCase):
    def test_one_list_several_lists_halves_and_commercials(self):
        c = sc.validate({**plain(), "default": week(day((0, 6, ["A"]), (6, 12, ["B", "C"]), (12, 12.5, ["D"]), (12.5, 24, ["E"]),
                                                          ads=[(6, 9, "Ads")]))})
        h = sc.hours_of(c["default"]["monday"])
        self.assertEqual(h["0"], {"tags": "A"})
        self.assertEqual(h["6"], {"tags": ["B", "C"], "random_tags": True, "commercial_dir": "Ads"})
        self.assertEqual(h["9"], {"tags": ["B", "C"], "random_tags": True})
        self.assertEqual(h["12"], {"tags": ["D", "E"]})
        self.assertEqual(len(h), 24)

    def test_mid_cards_by_the_hour_set_a_slots_bump_folder_and_a_pair_of_lists_makes_a_hidden_one(self):
        d = {**plain(), "default": week(day((0, 24, ["A"])))}
        d["default"]["monday"]["cards"] = [{"from": 6, "to": 9, "start": "Open", "end": "Close"}, {"from": 9, "to": 10, "start": "Same"}]
        c = sc.validate(d)
        self.assertEqual(c["default"]["monday"]["cards"][1], {"from": 9, "to": 10, "start": "Same", "end": "Same"})
        h = sc.hours_of(c["default"]["monday"])
        self.assertEqual(h["6"], {"tags": "A", "bump_dir": ".cards.Open--Close"})
        self.assertEqual(h["9"], {"tags": "A", "bump_dir": "Same"})                    # one list: the list itself
        self.assertNotIn("bump_dir", h["10"])                                           # no block: the station's own default
        self.assertEqual(sc.card_pairs(c), [("Open", "Close")])
        self.assertEqual(sc.lists_of(c), ["A", "Open", "Close", "Same"])
        self.assertFalse(sc.is_plain(c))
        d["default"]["monday"]["cards"] = [{"from": 6, "to": 9, "start": "A"}, {"from": 8, "to": 10, "start": "B"}]
        with self.assertRaises(sc.ScheduleError):
            sc.validate(d)

    def test_a_commercial_or_mid_card_block_with_nothing_chosen_says_which_one_to_fill(self):
        d = {**plain(), "default": week(day((0, 24, ["A"])))}
        d["default"]["monday"]["commercials"] = [{"from": 8, "to": 12, "list": ""}]
        with self.assertRaises(sc.ScheduleError) as e:
            sc.validate(d)
        self.assertIn("commercial block at 08:00", str(e.exception))
        d["default"]["monday"]["commercials"] = []
        d["default"]["monday"]["cards"] = [{"from": 6, "to": 9, "start": "", "end": ""}]
        with self.assertRaises(sc.ScheduleError) as e:
            sc.validate(d)
        self.assertIn("mid cards for the block at 06:00", str(e.exception))

    def test_a_video_slots_own_cards_beat_the_time_periods_which_beat_the_stations(self):
        d = {**plain(), "default": week(day((0, 18, ["A"]), (18, 19, ["Rugrats"]), (19, 24, ["A"])))}
        d["default"]["monday"]["videos"][1].update({"start": "RugratsBrb", "end": "RugratsBack"})
        d["default"]["monday"]["cards"] = [{"from": 17, "to": 21, "start": "Evening", "end": "Evening"}]
        c = sc.validate(d)
        self.assertEqual(c["default"]["monday"]["videos"][1]["start"], "RugratsBrb")
        self.assertNotIn("start", c["default"]["monday"]["videos"][0])
        h = sc.hours_of(c["default"]["monday"])
        self.assertEqual(h["18"]["bump_dir"], ".cards.RugratsBrb--RugratsBack")          # the show's slot
        self.assertEqual(h["17"]["bump_dir"], "Evening")                                  # the time period
        self.assertEqual(h["20"]["bump_dir"], "Evening")
        self.assertNotIn("bump_dir", h["12"])                                             # the channel's default
        self.assertIn(("RugratsBrb", "RugratsBack"), sc.card_pairs(c))
        self.assertIn("RugratsBack", sc.lists_of(c))
        self.assertFalse(sc.is_plain(c))
        one = {**plain(), "default": week(day((0, 24, ["A"])))}
        one["default"]["monday"]["videos"][0]["end"] = "Only"                             # one of the two: the same list for both
        self.assertEqual(sc.validate(one)["default"]["monday"]["videos"][0]["start"], "Only")

    def test_hours_with_no_video_are_left_out(self):
        h = sc.hours_of(sc.validate({**plain(), "events": [event("E", rng("01-01", "01-02"))]})["events"][0]["schedule"])
        self.assertEqual(sorted(h, key=int), ["20", "21"])


class DatesTest(unittest.TestCase):
    def test_nth_weekdays(self):
        self.assertEqual(sc.nth_weekday(2026, 11, "thursday", 4), date(2026, 11, 26))        # US Thanksgiving
        self.assertEqual(sc.nth_weekday(2026, 5, "monday", -1), date(2026, 5, 25))           # Memorial Day
        self.assertEqual(sc.nth_weekday(2026, 12, "wednesday", -1), date(2026, 12, 30))
        self.assertIsNone(sc.nth_weekday(2026, 2, "monday", 5))

    def test_occurrences_repeat_yearly_or_once_and_wrap_new_year(self):
        ev = sc._event(event("NY", rng("12-30", "01-02")), 1)
        self.assertEqual(sc.occurrences(ev, date(2027, 1, 1), date(2027, 1, 5)), [(date(2026, 12, 30), date(2027, 1, 2))])
        once = sc._event(event("Once", rng("07-21", "07-27", 2026)), 1)
        self.assertEqual(sc.occurrences(once, date(2026, 7, 20), date(2026, 7, 30)), [(date(2026, 7, 21), date(2026, 7, 27))])
        self.assertEqual(sc.occurrences(once, date(2027, 7, 20), date(2027, 7, 30)), [])      # not next year
        yearly = sc._event(event("Y", rng("07-21", "07-27")), 1)
        self.assertEqual(len(sc.occurrences(yearly, date(2027, 7, 20), date(2027, 7, 30))), 1)


class ConfTest(unittest.TestCase):
    TODAY = date(2026, 7, 14)

    def test_a_plain_schedule_makes_a_plain_config(self):
        parts = sc.conf_parts(sc.validate(plain()), self.TODAY)
        self.assertEqual(sorted(parts), sorted(["day_templates", *sc.DAYS]))
        self.assertEqual(parts["day_templates"], {"daily": {str(h): {"tags": "Base"} for h in range(24)}})
        self.assertEqual(set(parts[d] for d in sc.DAYS), {"daily"})

    def test_a_slot_that_plays_a_show_is_played_in_order(self):
        d = plain()
        d["default"]["monday"] = day((0, 20, ["Base"]), (20, 21, ["Sitcom"]), (21, 24, ["Base"]))
        parts = sc.conf_parts(sc.validate(d), self.TODAY, shows=("Sitcom",))
        mon = parts["day_templates"][parts["monday"]]
        self.assertEqual(mon["20"], {"tags": "Sitcom", "sequence": sc.SERIES})         # tracked: one episode further each time it airs
        self.assertEqual(mon["19"], {"tags": "Base"})                                     # a list stays random
        plain_parts = sc.conf_parts(sc.validate(d), self.TODAY)
        self.assertNotIn("sequence", plain_parts["day_templates"][plain_parts["monday"]]["20"])

    def test_every_season_and_special_day_carries_it_too(self):
        d = with_season(plain(), "summer", "Sitcom")
        parts = sc.conf_parts(d, self.TODAY, shows=("Sitcom",))
        self.assertEqual(parts["week_overrides"]["June 1 - August 31"]["monday"]["12"], {"tags": "Sitcom", "sequence": sc.SERIES})

    def test_different_days_share_templates(self):
        d = plain()
        d["default"]["saturday"] = day((0, 24, ["Weekend"]))
        d["default"]["sunday"] = day((0, 24, ["Weekend"]))
        parts = sc.conf_parts(sc.validate(d), self.TODAY)
        self.assertEqual(len(parts["day_templates"]), 2)
        self.assertEqual(parts["saturday"], parts["sunday"])

    def test_seasons_become_week_overrides_covering_the_year(self):
        d = with_season(plain(), "summer", "Summer")
        parts = sc.conf_parts(d, self.TODAY)
        keys = list(parts["week_overrides"])
        self.assertEqual(len(keys), 4)
        self.assertIn("June 1 - August 31", keys)
        self.assertIn("March 1 - May 31", keys)
        self.assertIn("September 1 - November 30", keys)
        self.assertEqual(keys[-1], "December 1 - March 1")                 # stretched to cover 29 February; the next season is first
        self.assertEqual(parts["week_overrides"]["June 1 - August 31"]["monday"]["12"], {"tags": "Summer"})
        self.assertEqual(parts["week_overrides"]["March 1 - May 31"]["monday"]["12"], {"tags": "Base"})   # none of its own: the default

    def test_special_days_are_in_the_config_only_while_near(self):
        d = plain()
        d["events"] = [event("Shark Week", rng("07-21", "07-27", 2026))]
        d = sc.validate(d)
        near = sc.conf_parts(d, date(2026, 7, 14))["date_overrides"]
        self.assertEqual(sorted(near, key=lambda k: int(k.split()[1])), ["July 21", "July 22", "July 23", "July 24", "July 25", "July 26", "July 27"][:len(near)])
        self.assertIn("July 21", near)
        self.assertNotIn("date_overrides", sc.conf_parts(d, date(2026, 3, 1)))        # far away: nothing
        self.assertNotIn("date_overrides", sc.conf_parts(d, date(2026, 8, 1)))        # over
        self.assertNotIn("date_overrides", sc.conf_parts(d, date(2027, 7, 14)))       # a one-off is not next year's

    def test_the_later_starting_event_wins_a_day(self):
        d = plain()
        d["events"] = [event("Long", rng("07-20", "07-30", 2026), sched=day((20, 21, ["Long"]))),
                       event("Short", rng("07-24", "07-25", 2026), sched=day((20, 21, ["Short"])))]
        o = sc.conf_parts(sc.validate(d), date(2026, 7, 14))["date_overrides"]
        self.assertEqual(o["July 23"]["20"], {"tags": "Long"})
        self.assertEqual(o["July 24"]["20"], {"tags": "Short"})
        self.assertEqual(o["July 26"]["20"], {"tags": "Long"})

    def test_a_weekday_event_gives_each_date_its_weekdays_lineup(self):
        w = {n: day() for n in sc.DAYS}
        w["friday"] = day((20, 22, ["Fri"]))
        w["saturday"] = day((12, 14, ["Sat"]))
        d = plain()
        d["events"] = [event("Fest", rng("07-24", "07-26", 2026), mode="weekday", sched=w)]
        o = sc.conf_parts(sc.validate(d), date(2026, 7, 14))["date_overrides"]
        self.assertEqual(sorted(o), ["July 24", "July 25"])                    # the Sunday (26th) has nothing: no entry
        self.assertEqual(o["July 24"]["20"], {"tags": "Fri"})
        self.assertEqual(o["July 25"]["12"], {"tags": "Sat"})

    def test_conf_matches_tells_a_hand_edit_from_ours(self):
        d = with_season(plain(), "winter", "Snow")
        parts = sc.conf_parts(d, self.TODAY)
        conf = {**{k: copy.deepcopy(v) for k, v in parts.items()}, "network_name": "X"}
        self.assertTrue(sc.conf_matches(conf, parts))
        conf["week_overrides"]["December 1 - March 1"]["monday"]["3"] = {"tags": "Hand"}
        self.assertFalse(sc.conf_matches(conf, parts))

    def test_sources_say_which_layer_wins(self):
        d = with_season(plain(), "summer", "Summer")
        d["events"] = [sc._event(event("Shark Week", rng("07-21", "07-22", 2026), replace=False), 1)]
        got = dict(sc.sources(d, date(2026, 7, 20), 4))
        self.assertEqual(got[date(2026, 7, 20)], {"layer": "season", "name": "summer"})
        self.assertEqual(got[date(2026, 7, 21)]["layer"], "event")
        self.assertEqual(got[date(2026, 7, 23)], {"layer": "season", "name": "summer"})
        got = dict(sc.sources(d, date(2026, 4, 10), 1))
        self.assertEqual(got[date(2026, 4, 10)], {"layer": "default", "name": "spring"})         # a season with no schedule of its own


@unittest.skipIf(ConfigProcessor is None, "FieldStation42's sources are not available")
class OracleTest(unittest.TestCase):
    """Run the generated config through FieldStation42's OWN config processing and slot lookup."""

    def tags_at(self, conf, when):
        conf = {"network_name": "X", "channel_number": 5, **copy.deepcopy(conf)}
        conf = ConfigProcessor.preprocess(conf)
        slot, _n = SlotReader.get_slot(conf, when)
        return SlotReader.get_tag_from_slot(slot, when)[0] if slot else None

    def build(self, defn, today):
        return sc.conf_parts(sc.validate(defn), today)

    def test_seasons_event_and_default_layers_as_the_engine_sees_them(self):
        d = with_season(plain(), "summer", "Summer")
        d = with_season(d, "winter", "Snow")
        d["events"] = [event("Shark Week", rng("07-21", "07-27", 2026), sched=day((20, 22, ["Shark"])))]
        conf = self.build(d, date(2026, 7, 14))
        self.assertEqual(self.tags_at(conf, datetime(2026, 7, 14, 12)), "Summer")             # summer
        self.assertEqual(self.tags_at(conf, datetime(2026, 7, 22, 12)), "Summer")             # event day, an hour it does not touch
        self.assertEqual(self.tags_at(conf, datetime(2026, 7, 22, 20)), "Shark")              # the special slot
        self.assertEqual(self.tags_at(conf, datetime(2026, 4, 10, 12)), "Base")               # spring: the default
        self.assertEqual(self.tags_at(conf, datetime(2026, 12, 25, 12)), "Snow")              # winter
        self.assertEqual(self.tags_at(conf, datetime(2027, 2, 14, 12)), "Snow")               # winter, after New Year
        self.assertEqual(self.tags_at(conf, datetime(2028, 2, 29, 12)), "Snow")               # a leap day is still winter
        self.assertEqual(self.tags_at(conf, datetime(2028, 3, 1, 12)), "Base")                # and 1 March is spring

    def test_half_hours_random_lists_and_replace(self):
        d = plain()
        d["default"]["monday"] = day((0, 20.5, ["A"]), (20.5, 24, ["B"]))
        d["events"] = [event("Day", rng("07-20", "07-20", 2026), replace=True, sched=day((0, 24, ["X", "Y"])))]
        conf = self.build(d, date(2026, 7, 14))
        self.assertEqual(self.tags_at(conf, datetime(2026, 7, 13, 20, 10)), "A")             # Monday, first half
        self.assertEqual(self.tags_at(conf, datetime(2026, 7, 13, 20, 40)), "B")             # Monday, second half
        self.assertIn(self.tags_at(conf, datetime(2026, 7, 20, 20, 10)), ("X", "Y"))          # the special Monday replaces all of it
        self.assertIn(self.tags_at(conf, datetime(2026, 7, 20, 3)), ("X", "Y"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config", "schedules.json")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_put_get_move_drop(self):
        d = sc.validate(plain())
        self.assertIsNone(sc.get("A", self.path))
        sc.put("A", d, self.path)
        self.assertEqual(sc.get("A", self.path), d)
        sc.move("A", "B", self.path)
        self.assertEqual((sc.get("A", self.path), sc.names(self.path)), (None, ["B"]))
        sc.drop("B", self.path)
        self.assertEqual(sc.names(self.path), [])

    def test_a_broken_file_reads_as_nothing(self):
        os.makedirs(os.path.dirname(self.path))
        open(self.path, "w").write("{broken")
        self.assertIsNone(sc.get("A", self.path))


class BurnTest(unittest.TestCase):
    def burning(self, on, lo=18, hi=20):
        d = plain()
        blk = {"from": lo, "to": hi, "lists": ["Anime"]}
        if on is not None:
            blk["burn"] = on
        d["default"]["monday"] = {"videos": [{"from": 0, "to": lo, "lists": ["Base"]}, blk, {"from": hi, "to": 24, "lists": ["Base"]}],
                                  "commercials": []}
        return sc.validate(d)

    def test_a_block_can_draw_the_captions_on_the_picture(self):
        on = self.burning(True)
        self.assertIs(on["default"]["monday"]["videos"][1]["burn"], True)
        self.assertNotIn("burn", on["default"]["monday"]["videos"][0])
        for off in (None, False, ""):
            self.assertNotIn("burn", self.burning(off)["default"]["monday"]["videos"][1])

    def test_it_is_the_block_that_covers_the_moment(self):
        d = self.burning(True)
        self.assertTrue(sc.burn_at(d, datetime(2026, 10, 12, 18, 30)))           # a Monday
        self.assertFalse(sc.burn_at(d, datetime(2026, 10, 12, 17, 59)))
        self.assertFalse(sc.burn_at(d, datetime(2026, 10, 12, 20, 0)))
        self.assertFalse(sc.burn_at(d, datetime(2026, 10, 13, 18, 30)))          # Tuesday has none

    def test_a_special_day_wins_and_a_season_replaces_the_default(self):
        d = self.burning(True)
        d["seasons"]["summer"] = week(day((0, 24, ["Summer"])))
        d = sc.validate(d)
        self.assertFalse(sc.burn_at(d, datetime(2026, 7, 6, 18, 30)))            # a summer Monday: the season's week has none
        self.assertTrue(sc.burn_at(d, datetime(2026, 10, 12, 18, 30)))           # autumn: the default week
        ev = day((0, 24, ["Special"]))
        d["events"] = [event("Day", rng("10-12", "10-12"), sched=ev)]
        d = sc.validate(d)
        self.assertFalse(sc.burn_at(d, datetime(2026, 10, 12, 18, 30)))          # the special day has no toggle on
        ev["videos"][0]["burn"] = True
        d["events"] = [event("Day", rng("10-12", "10-12"), sched=ev)]
        self.assertTrue(sc.burn_at(sc.validate(d), datetime(2026, 10, 12, 3, 0)))


if __name__ == "__main__":
    unittest.main()
