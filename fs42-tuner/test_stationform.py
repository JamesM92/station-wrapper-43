"""Tests for stationform.py. Run: python3 test_stationform.py"""
import json
import os
import unittest

import stationform as sf
from stationform import FormError

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fieldstation42", "src", "fs42",
                      "station_config_schema.json")


def standard(**kw):
    f = {"name": "Cartoons", "number": 12, "kind": "standard",
         "blocks": [{"from": 0, "to": 24, "lists": ["Cartoons"]}], "increment": 30, "bump": "bump", "commercials": ""}
    f.update(kw)
    return f


class ValidateTest(unittest.TestCase):
    def test_a_good_form_comes_back_clean(self):
        f = sf.validate(standard(name="  Cartoons  ", blocks=[{"from": 0, "to": 24, "lists": ["A", "A", " B "]}]))
        self.assertEqual(f["name"], "Cartoons")
        self.assertEqual(f["blocks"][0]["lists"], ["A", "B"])         # trimmed, no repeats

    def test_names_and_numbers(self):
        for bad in ("", "  ", None, 5, "a/b", "a\\b", ".hidden", "x" * 101):
            with self.assertRaises(FormError, msg=repr(bad)):
                sf.validate(standard(name=bad))
        for bad in (-1, 10000, "12", 1.5, True, None):
            with self.assertRaises(FormError, msg=repr(bad)):
                sf.validate(standard(number=bad))
        self.assertEqual(sf.validate(standard(number=1))["number"], 1)
        with self.assertRaises(FormError) as cm:
            sf.validate(standard(number=0))                                      # channel 0 is not: Jellyfin ignores it
        self.assertIn("channel 0", str(cm.exception))

    def test_it_refuses_a_name_or_number_another_station_has_but_not_its_own(self):
        with self.assertRaises(FormError) as cm:
            sf.validate(standard(), existing_names=["cartoons"])
        self.assertIn("already a station", str(cm.exception))
        with self.assertRaises(FormError) as cm:
            sf.validate(standard(), existing_numbers={12: "News"})
        self.assertIn("channel 12", str(cm.exception))
        sf.validate(standard(), existing_names=["Cartoons"], existing_numbers={12: "Cartoons"}, original="Cartoons")

    def test_time_blocks_must_cover_the_day_exactly(self):
        ok = [{"from": 0, "to": 6, "lists": ["A"]}, {"from": 6, "to": 24, "lists": ["B"]}]
        self.assertEqual(len(sf.validate(standard(blocks=ok))["blocks"]), 2)
        bad = ([], None, "x", [{"from": 1, "to": 24, "lists": ["A"]}],                       # starts late
               [{"from": 0, "to": 6, "lists": ["A"]}],                                         # stops early
               [{"from": 0, "to": 6, "lists": ["A"]}, {"from": 5, "to": 24, "lists": ["B"]}],  # overlap
               [{"from": 0, "to": 6, "lists": ["A"]}, {"from": 8, "to": 24, "lists": ["B"]}],  # gap
               [{"from": 0, "to": 25, "lists": ["A"]}], [{"from": 6, "to": 6, "lists": ["A"]}],
               [{"from": 0, "to": 24, "lists": []}], [{"from": 0, "to": 24}],
               [{"from": 0.5, "to": 24, "lists": ["A"]}], [{"from": 0, "to": 24, "lists": ["a/b"]}])
        for b in bad:
            with self.assertRaises(FormError, msg=str(b)):
                sf.validate(standard(blocks=b))

    def test_other_fields(self):
        for bad in (20, 0, None, "30"):
            with self.assertRaises(FormError, msg=repr(bad)):
                sf.validate(standard(increment=bad))
        with self.assertRaises(FormError):
            sf.validate(standard(kind="web"))
        with self.assertRaises(FormError):
            sf.validate({"name": "L", "number": 3, "kind": "loop"})
        self.assertEqual(sf.validate(standard(bump=""))["bump"], "bump")


class ConfTest(unittest.TestCase):
    def test_time_with_no_slot_shows_the_test_pattern_unless_a_station_has_its_own_off_air_clip(self):
        conf = sf.to_conf(sf.validate(standard()))
        self.assertEqual(conf["off_air_video"], sf.OFF_AIR_CLIP)
        own = sf.to_conf(sf.validate(standard()), {"network_name": "Cartoons", "channel_number": 12, "off_air_video": "/mnt/media/mine.mp4"})
        self.assertEqual(own["off_air_video"], "/mnt/media/mine.mp4")                     # set in the console: kept

    def test_short_videos_sharing_a_slot_is_on_unless_a_station_says_no(self):
        self.assertNotIn("wrapper_pack_short", sf.to_conf(sf.validate(standard())))                 # absent means on
        off = sf.to_conf(sf.validate(standard(pack=False)))
        self.assertIs(off["wrapper_pack_short"], False)
        self.assertNotIn("wrapper_pack_short", sf.to_conf(sf.validate(standard(pack=True)), off))   # saving it on removes the key
        self.assertTrue(sf.validate(standard())["pack"])
        self.assertFalse(sf.validate(standard(pack=False))["pack"])

    def test_moving_break_points_onto_scene_changes_is_a_station_setting(self):
        off = sf.to_conf(sf.validate(standard()))
        self.assertNotIn("wrapper_break_snap", off)
        on = sf.to_conf(sf.validate(standard(snap=True)))
        self.assertIs(on["wrapper_break_snap"], True)
        self.assertEqual(sf.to_conf(sf.validate(standard(snap="yes")))  .get("wrapper_break_snap"), None)    # only a real true
        self.assertNotIn("wrapper_break_snap", sf.to_conf(sf.validate(standard()), on))                       # saving it off turns it off

    def test_a_standard_station_has_a_slot_for_every_hour_and_the_same_week(self):
        f = sf.validate(standard(blocks=[{"from": 0, "to": 6, "lists": ["Night"]},
                                         {"from": 6, "to": 24, "lists": ["Day", "More"]}], commercials="commercials"))
        c = sf.to_conf(f)
        self.assertEqual((c["network_name"], c["channel_number"], c["network_type"]), ("Cartoons", 12, "standard"))
        self.assertEqual((c["content_dir"], c["bump_dir"], c["schedule_increment"]), ("catalog/by-genre", "bump", 30))
        self.assertEqual((c["commercial_free"], c["commercial_dir"]), (False, "commercials"))
        daily = c["day_templates"]["daily"]
        self.assertEqual(sorted(daily, key=int), [str(h) for h in range(24)])
        self.assertEqual(daily["5"], {"tags": "Night"})
        self.assertEqual(daily["6"], {"tags": ["Day", "More"]})                  # several lists: a list of tags
        self.assertTrue(all(c[d] == "daily" for d in sf.DAYS))

    def test_without_commercials_the_station_is_commercial_free(self):
        c = sf.to_conf(sf.validate(standard()))
        self.assertEqual((c["commercial_free"], c["commercial_dir"]), (False, sf.COM_OFF))   # the default is a name no choice can have
        self.assertIs(c["wrapper_no_breaks"], True)                              # and no breaks: videos play back to back
        form, _ = sf.parse_base({**c, "network_name": "Cartoons", "channel_number": 12, "network_type": "standard",
                                 "content_dir": sf.CONTENT_DIR, "schedule_increment": 30, "break_strategy": "end"})
        self.assertEqual(form["commercials"], "")                                # and it shows as off again
        with_ads = sf.to_conf(sf.validate({**standard(), "commercials": "commercials"}))
        self.assertNotIn("wrapper_no_breaks", with_ads)

    def test_start_and_end_of_break_cards_can_be_two_lists_or_the_same_one(self):
        same = sf.to_conf(sf.validate({**standard(), "bump": "cards", "bump_end": "cards"}))
        self.assertEqual(same["bump_dir"], "cards")
        self.assertNotIn("wrapper_start_cards", same)
        blank = sf.to_conf(sf.validate({**standard(), "bump": "cards"}))
        self.assertEqual(blank["bump_dir"], "cards")                                    # blank end list: the same one
        two = sf.to_conf(sf.validate({**standard(), "bump": "start", "bump_end": "end"}))
        self.assertEqual(two["bump_dir"], sf.schedules.cards_folder("start", "end"))
        self.assertEqual((two["wrapper_start_cards"], two["wrapper_end_cards"]), ("start", "end"))

    def test_mid_cards_are_opt_in_and_a_station_made_earlier_keeps_them(self):
        self.assertNotIn("wrapper_mid_cards", sf.to_conf(sf.validate(standard())))        # absent means on
        off = sf.to_conf(sf.validate({**standard(), "midcards": False}))
        self.assertIs(off["wrapper_mid_cards"], False)
        self.assertEqual(off["bump_dir"], sf.OFF_CARDS)                                  # the default is a name no choice can have
        self.assertEqual((off["wrapper_start_cards"], off["wrapper_end_cards"]), ("bump", "bump"))   # remembered for turning it back on
        form, _ = sf.parse_base({**off, "network_name": "Cartoons", "channel_number": 12, "network_type": "standard",
                                 "content_dir": sf.CONTENT_DIR, "schedule_increment": 30, "break_strategy": "end"})
        self.assertIs(form["midcards"], False)

    def test_a_show_or_movie_can_have_cards_of_its_own_and_the_station_still_opens_in_the_form(self):
        rows = [{"show": "Rugrats", "start": "rugrats-brb", "end": "rugrats-back"}, {"show": "Movies/Toy Story", "start": "ts"}]
        form = sf.validate({**standard(), "show_cards": rows})
        self.assertEqual(form["show_cards"][1], {"show": "Movies/Toy Story", "start": "ts", "end": "ts"})
        conf = sf.to_conf(form)
        self.assertEqual(conf["tag_overrides"], {"Rugrats": {"bump_dir": ".cards.rugrats-brb--rugrats-back"},
                                                 "Movies/Toy Story": {"bump_dir": "ts"}})
        again, why = sf.parse_base({**conf, "network_name": "Cartoons", "network_type": "standard", "content_dir": sf.CONTENT_DIR,
                                    "schedule_increment": 30, "break_strategy": "end"})
        self.assertIsNone(why)
        self.assertEqual(again["show_cards"], form["show_cards"])
        # an override somebody else made (in the console) is not ours: the form must not hold the station and lose it
        foreign, why = sf.parse_base({**conf, "tag_overrides": {"Other": {"bump_dir": "x"}}, "network_name": "Cartoons",
                                      "network_type": "standard", "content_dir": sf.CONTENT_DIR, "schedule_increment": 30,
                                      "break_strategy": "end"})
        self.assertIsNone(foreign)
        self.assertIn("tag_overrides", why)
        none = sf.to_conf(sf.validate(standard()))
        self.assertNotIn("tag_overrides", none)

    def test_a_show_cards_row_must_name_a_list_or_a_folder_inside_one_and_only_once(self):
        for bad in ([{"show": "../x", "start": "a"}], [{"show": "", "start": "a"}],
                    [{"show": "A", "start": "a"}, {"show": "A", "start": "b"}], [{"show": "A"}], "nope"):
            with self.assertRaises(sf.FormError):
                sf.validate({**standard(), "show_cards": bad})

    def test_the_two_card_lists_are_the_pre_and_post_folders_of_a_hidden_folder_shared_by_stations(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        for n in ("start", "end"):
            os.makedirs(os.path.join(d, n))
            open(os.path.join(d, n, n + ".mp4"), "w").close()
        name = sf.make_cards_dir(d, "start", "end")
        path = os.path.join(d, name)
        self.assertEqual(os.listdir(os.path.join(path, "pre")), ["start.mp4"])
        self.assertEqual(os.listdir(os.path.join(path, "post")), ["end.mp4"])
        self.assertEqual(sf.make_cards_dir(d, "start", "end"), name)                       # again: nothing breaks
        self.assertEqual(sf.make_cards_dir(d, "start", "start"), "start")                  # one list: no folder needed

    def test_a_channels_own_logo_is_kept_and_checked(self):
        conf = sf.to_conf(sf.validate({**standard(), "logo": " /logos/toons.png "}))
        self.assertEqual(conf["wrapper_logo"], "logos/toons.png")
        self.assertNotIn("wrapper_logo", sf.to_conf(sf.validate(standard())))
        for bad in ("../x.png", "logos/a.gif", 5):
            with self.assertRaises(sf.FormError):
                sf.validate({**standard(), "logo": bad})
        form, _ = sf.parse_base({**conf, "network_name": "Cartoons", "channel_number": 12, "network_type": "standard",
                                 "content_dir": sf.CONTENT_DIR, "schedule_increment": 30, "break_strategy": "end"})
        self.assertEqual(form["logo"], "logos/toons.png")

    def test_the_caption_track_is_on_unless_the_station_says_no_and_round_trips(self):
        on = sf.to_conf(sf.validate(standard()))
        self.assertNotIn("wrapper_caption_track", on)                                 # absent means on
        off = sf.to_conf(sf.validate({**standard(), "caption_track": False}))
        self.assertIs(off["wrapper_caption_track"], False)
        form, _ = sf.parse_base({**off, "network_name": "Cartoons", "channel_number": 12, "network_type": "standard",
                                 "content_dir": sf.CONTENT_DIR, "schedule_increment": 30, "break_strategy": "end"})
        self.assertIs(form["caption_track"], False)
        again = sf.to_conf(sf.validate({**standard(), "caption_track": True}), existing=off)
        self.assertNotIn("wrapper_caption_track", again)                              # turned back on: the key goes

    def test_the_volume_is_evened_out_unless_the_station_says_no_and_round_trips(self):
        self.assertNotIn("wrapper_level_audio", sf.to_conf(sf.validate(standard())))           # absent means on
        off = sf.to_conf(sf.validate({**standard(), "level_audio": False}))
        self.assertIs(off["wrapper_level_audio"], False)
        form, _ = sf.parse_base({**off, "network_name": "Cartoons", "channel_number": 12, "network_type": "standard",
                                 "content_dir": sf.CONTENT_DIR, "schedule_increment": 30, "break_strategy": "end"})
        self.assertIs(form["level_audio"], False)
        again = sf.to_conf(sf.validate({**standard(), "level_audio": True}), existing=off)
        self.assertNotIn("wrapper_level_audio", again)

    def test_a_loop_station_repeats_one_list(self):
        c = sf.to_conf(sf.validate({"name": "Loop", "number": 1, "kind": "loop", "loop": {"list": "Test Pattern", "shuffle": True}}))
        self.assertEqual((c["network_type"], c["content_dir"], c["shuffle_loop"]), ("loop", "catalog/by-genre/Test Pattern", True))
        self.assertEqual(c["channel_number"], 1)
        self.assertNotIn("day_templates", c)

    def test_editing_keeps_what_the_form_does_not_own(self):
        existing = {"network_name": "Old", "channel_number": 5, "network_type": "standard", "standby_image": "runtime/standby.png",
                    "logo_dir": "x", "_draft": True, "monday": "weird", "day_templates": {"weird": {}}, "commercial_dir": "old"}
        c = sf.to_conf(sf.validate(standard()), existing)
        self.assertEqual((c["standby_image"], c["logo_dir"]), ("runtime/standby.png", "x"))
        self.assertNotIn("_draft", c)
        self.assertEqual(list(c["day_templates"]), ["daily"])
        self.assertEqual(c["commercial_dir"], sf.COM_OFF)                  # commercials were switched off

    def test_what_we_write_passes_fieldstation42s_own_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("pip install jsonschema")
        schema = json.load(open(SCHEMA))
        for form in (standard(), standard(commercials="commercials", blocks=[{"from": 0, "to": 12, "lists": ["A"]},
                                                                             {"from": 12, "to": 24, "lists": ["B", "C"]}]),
                     {"name": "Loop", "number": 1, "kind": "loop", "loop": {"list": "Test Pattern", "shuffle": False}}):
            conf = sf.to_conf(sf.validate(form))
            jsonschema.validate({"station_conf": conf}, schema)


def week(**over):
    """Seven days of the same single block, with some days replaced."""
    base = [{"from": 0, "to": 24, "lists": ["Cartoons"]}]
    return {d: over.get(d, base) for d in sf.DAYS}


class WeekTest(unittest.TestCase):
    def weekend_form(self):
        wk = [{"from": 0, "to": 24, "lists": ["Movies"]}]
        return standard(blocks=None, days=week(saturday=wk, sunday=wk))

    def test_days_that_all_match_are_the_plain_form(self):
        f = sf.validate(standard(blocks=None, days=week()))
        self.assertIn("blocks", f)
        self.assertNotIn("days", f)                           # one representation for one schedule

    def test_days_that_differ_stay_per_day(self):
        f = sf.validate(self.weekend_form())
        self.assertNotIn("blocks", f)
        self.assertEqual(f["days"]["saturday"][0]["lists"], ["Movies"])
        self.assertEqual(f["days"]["monday"][0]["lists"], ["Cartoons"])

    def test_every_day_is_checked_and_a_bad_one_is_named(self):
        bad = week(wednesday=[{"from": 0, "to": 12, "lists": ["A"]}])                  # stops at noon
        with self.assertRaises(FormError) as cm:
            sf.validate(standard(blocks=None, days=bad))
        self.assertIn("Wednesday", str(cm.exception))
        for broken in ({}, {d: week()[d] for d in sf.DAYS[:6]}, {**week(), "funday": week()["monday"]}, "x", []):
            with self.assertRaises(FormError, msg=str(broken)[:40]):
                sf.validate(standard(blocks=None, days=broken))

    def test_the_conf_shares_a_template_between_days_that_match(self):
        conf = sf.to_conf(sf.validate(self.weekend_form()))
        self.assertEqual(sorted(conf["day_templates"]), ["schedule1", "schedule2"])
        self.assertEqual([conf[d] for d in sf.DAYS], ["schedule1"] * 5 + ["schedule2"] * 2)
        self.assertEqual(conf["day_templates"]["schedule2"]["7"], {"tags": "Movies"})
        self.assertEqual(sorted(conf["day_templates"]["schedule1"], key=int), [str(h) for h in range(24)])

    def test_seven_different_days_make_seven_templates(self):
        names = ["A", "B", "C", "D", "E", "F", "G"]
        days = {d: [{"from": 0, "to": 24, "lists": [n]}] for d, n in zip(sf.DAYS, names)}
        conf = sf.to_conf(sf.validate(standard(blocks=None, days=days)))
        self.assertEqual(len(conf["day_templates"]), 7)
        self.assertEqual(len({conf[d] for d in sf.DAYS}), 7)

    def test_a_per_day_station_reads_back_the_same(self):
        clean = sf.validate(self.weekend_form())
        back, why = sf.parse(sf.to_conf(clean))
        self.assertIsNone(why)
        self.assertEqual(back, clean)

    def test_a_split_day_that_differs_on_one_day_round_trips(self):
        split = [{"from": 0, "to": 6, "lists": ["Night"]}, {"from": 6, "to": 24, "lists": ["Day", "More"]}]
        clean = sf.validate(standard(blocks=None, days=week(friday=split)))
        back, why = sf.parse(sf.to_conf(clean))
        self.assertIsNone(why)
        self.assertEqual(back["days"]["friday"], split)
        self.assertEqual(back, clean)

    def test_going_back_to_one_schedule_collapses_the_templates(self):
        conf = sf.to_conf(sf.validate(standard(blocks=None, days=week())))
        self.assertEqual(list(conf["day_templates"]), ["daily"])

    def test_lists_used_covers_every_day(self):
        f = sf.validate(self.weekend_form())
        self.assertEqual(sf.lists_used(f), ["Cartoons", "Movies"])
        self.assertEqual(sf.lists_used(sf.validate(standard())), ["Cartoons"])

    def test_what_is_written_passes_fieldstation42s_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("pip install jsonschema")
        schema = json.load(open(SCHEMA))
        jsonschema.validate({"station_conf": sf.to_conf(sf.validate(self.weekend_form()))}, schema)

    def test_a_config_with_odd_templates_is_refused_not_flattened(self):
        conf = sf.to_conf(sf.validate(self.weekend_form()))
        unused = json.loads(json.dumps(conf))
        unused["day_templates"]["spare"] = unused["day_templates"]["schedule1"]
        self.assertIsNone(sf.parse(unused)[0])                                          # would be lost on save
        self.assertIn("no day uses", sf.parse(unused)[1])
        missing = json.loads(json.dumps(conf))
        missing["tuesday"] = "nowhere"
        self.assertIsNone(sf.parse(missing)[0])
        inline = json.loads(json.dumps(conf))
        inline["tuesday"] = {"0": {"tags": "X"}}
        self.assertIsNone(sf.parse(inline)[0])
        advanced = json.loads(json.dumps(conf))
        advanced["day_templates"]["schedule2"]["3"] = {"tags": "Movies", "sequence": "s"}
        self.assertIsNone(sf.parse(advanced)[0])


class ParseTest(unittest.TestCase):
    def roundtrip(self, form):
        clean = sf.validate(form)
        back, why = sf.parse(sf.to_conf(clean))
        self.assertIsNone(why)
        return clean, back

    def test_what_the_form_writes_it_reads_back(self):
        for form in (standard(), standard(commercials="commercials", increment=60, bump="bumps",
                                          blocks=[{"from": 0, "to": 6, "lists": ["A"]}, {"from": 6, "to": 7, "lists": ["B", "C"]},
                                                  {"from": 7, "to": 24, "lists": ["A"]}]),
                     {"name": "L", "number": 1, "kind": "loop", "loop": {"list": "Test Pattern", "shuffle": True}}):
            clean, back = self.roundtrip(form)
            self.assertEqual(clean, back)

    def test_break_style_and_five_minute_slots_round_trip(self):
        for br in ("end", "standard", "center"):
            clean, back = self.roundtrip(standard(breaks=br, increment=5))
            self.assertEqual((back["breaks"], back["increment"]), (br, 5))
        self.assertEqual(sf.to_conf(sf.validate(standard(breaks="standard")))["break_strategy"], "standard")
        self.assertEqual(sf.validate(standard())["breaks"], "end")                  # what it did before: at the end
        with self.assertRaises(FormError):
            sf.validate(standard(breaks="whenever"))

    def test_a_station_made_elsewhere_keeps_its_break_style_when_saved_here(self):
        conf = sf.to_conf(sf.validate(standard()))
        del conf["break_strategy"]                                                    # FieldStation42's default is standard
        form, why = sf.parse(conf)
        self.assertEqual(form["breaks"], "standard")
        self.assertEqual(sf.to_conf(sf.validate(form), conf)["break_strategy"], "standard")
        conf["break_strategy"] = "mystery"
        self.assertIn("mystery", sf.parse(conf)[1])

    def test_equal_neighbouring_hours_merge_into_one_block(self):
        conf = sf.to_conf(sf.validate(standard()))
        form, _ = sf.parse(conf)
        self.assertEqual(form["blocks"], [{"from": 0, "to": 24, "lists": ["Cartoons"]}])

    def test_a_station_the_generator_made_is_editable(self):
        conf = {"network_name": "Comedy", "channel_number": 801, "network_type": "standard", "schedule_increment": 60,
                "break_strategy": "end", "commercial_free": True, "content_dir": "catalog/by-genre", "bump_dir": "bump",
                "standby_image": "runtime/standby.png", "be_right_back_media": "runtime/brb.png",
                "day_templates": {"daily": {str(h): {"tags": "Comedy"} for h in range(24)}}, **{d: "daily" for d in sf.DAYS}}
        form, why = sf.parse(conf)
        self.assertIsNone(why)
        self.assertEqual((form["name"], form["increment"], form["blocks"]), ("Comedy", 60, [{"from": 0, "to": 24, "lists": ["Comedy"]}]))

    def base(self):
        return sf.to_conf(sf.validate(standard()))

    def test_anything_the_form_cannot_hold_is_refused_not_flattened(self):
        def with_(**kw):
            c = self.base()
            c.update(kw)
            return c
        cases = {
            "web": with_(network_type="web"),
            "other folder": with_(content_dir="catalog/elsewhere"),
            "overrides": with_(date_overrides={"2026-01-01": "x"}),
            "two templates": with_(day_templates={"a": {}, "b": {}}),
            "different days": with_(sunday="other"),
            "odd increment": with_(schedule_increment=20),
            "commercials without a folder": with_(commercial_free=False, commercial_dir=""),
        }
        slot = self.base()
        slot["day_templates"]["daily"]["3"] = {"tags": "A", "sequence": "show1"}
        cases["a slot with a sequence"] = slot
        short = self.base()
        del short["day_templates"]["daily"]["7"]
        cases["a missing hour"] = short
        for label, conf in cases.items():
            form, why = sf.parse(conf)
            self.assertIsNone(form, label)
            self.assertTrue(why, label)

    def test_a_loop_over_something_that_is_not_one_list_is_refused(self):
        for cd in ("catalog/by-genre", "catalog/by-genre/A/B", "somewhere/else", ""):
            form, why = sf.parse({"network_name": "L", "channel_number": 1, "network_type": "loop", "content_dir": cd})
            self.assertIsNone(form, cd)

    def test_no_odd_config_can_make_parse_raise(self):
        base = sf.to_conf(sf.validate(standard()))
        weird = [
            {**base, "monday": ["a"]}, {**base, "monday": 5}, {**base, "day_templates": []},
            {**base, "day_templates": {"daily": []}}, {**base, "day_templates": {"daily": {str(h): 5 for h in range(24)}}},
            {**base, "day_templates": {"daily": {str(h): {"tags": [["x"]]} for h in range(24)}}},
            {**base, "schedule_increment": [30]}, {**base, "schedule_increment": {}}, {**base, "channel_number": [1]},
            {**base, "commercial_free": None, "commercial_dir": 5}, {**base, "content_dir": 7},
            {**base, "network_type": ["loop"]}, {**base, "network_type": {"a": 1}, "content_dir": "x"},
            {**base, "bump_dir": {"a": 1}}, {**base, "date_overrides": 0, "hidden": []}, {"network_name": [], "channel_number": 1},
        ]
        for conf in weird:
            form, why = sf.parse(conf)                     # must not raise, whatever it decides
            self.assertTrue(form is None or isinstance(form, dict))
            if form is None:
                self.assertTrue(why)

    def test_junk_is_refused(self):
        for junk in (None, [], "x", {}, {"network_name": "A"}, {"network_name": "A", "channel_number": "5"}):
            self.assertIsNone(sf.parse(junk)[0])


class GuideKindTest(unittest.TestCase):
    def guide(self, **kw):
        g = {"title": "Penguin Guide", "hours": 3, "scroll_speed": 1.0, "messages": ["Hello", " ", "World"], "music": "/mnt/media/music"}
        g.update(kw)
        return {"name": "Guide", "number": 2, "kind": "guide", "guide": g}

    def test_a_guide_station_validates_and_writes_fieldstation42s_own_guide_keys(self):
        form = sf.validate(self.guide())
        self.assertEqual(form["guide"]["messages"], ["Hello", "World"])
        conf = sf.to_conf(form)
        self.assertEqual((conf["network_type"], conf["play_sound"], conf["scroll_speed"], conf["messages"]),
                         ("guide", False, 1.0, ["Hello", "World"]))
        self.assertNotIn("sound_to_play", conf)                       # FieldStation42 would refuse to start if the music were away
        self.assertEqual(conf["wrapper_guide"], {"title": "Penguin Guide", "hours": 3, "theme": "modern-blue", "music": "/mnt/media/music"})
        for key in ("content_dir", "day_templates", "monday", "bump_dir"):
            self.assertNotIn(key, conf)

    def test_the_guide_style_is_chosen_checked_and_read_back(self):
        conf = sf.to_conf(sf.validate(self.guide(theme="midnight")))
        self.assertEqual(conf["wrapper_guide"]["theme"], "midnight")
        self.assertEqual(sf.parse(conf)[0]["guide"]["theme"], "midnight")
        with self.assertRaises(FormError):
            sf.validate(self.guide(theme="neon"))
        self.assertEqual(sf.validate(self.guide())["guide"]["theme"], "modern-blue")                # none chosen: the default

    def test_it_reads_back_the_same_and_no_music_means_no_sound(self):
        form = sf.validate(self.guide(music=""))
        conf = sf.to_conf(form)
        self.assertFalse(conf["play_sound"])
        self.assertNotIn("sound_to_play", conf)
        self.assertEqual(conf["wrapper_guide"]["music"], "")
        back, why = sf.parse(sf.to_conf(sf.validate(self.guide())))
        self.assertIsNone(why)
        self.assertEqual(back, sf.validate(self.guide()))

    def test_it_keeps_what_the_form_does_not_own(self):
        conf = sf.to_conf(sf.validate(self.guide()), {"network_name": "Guide", "channel_number": 2, "hidden": True, "logo": "x.png"})
        self.assertEqual((conf["hidden"], conf["logo"]), (True, "x.png"))

    def test_bad_values_are_refused(self):
        for bad in ({"hours": 9}, {"hours": True}, {"scroll_speed": 0}, {"scroll_speed": 9}, {"title": ""}, {"title": "x" * 41},
                    {"messages": "no"}, {"messages": ["x" * 201]}, {"messages": ["a"] * 21}, {"music": "relative/folder"}, {"music": 5}):
            with self.assertRaises(FormError, msg=str(bad)):
                sf.validate(self.guide(**bad))

    def test_a_guide_station_uses_no_lists(self):
        self.assertEqual(sf.lists_used(sf.validate(self.guide())), [])


if __name__ == "__main__":
    unittest.main()
