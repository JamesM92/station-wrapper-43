"""Short videos share a time slot (fieldstation42/console_launch.py). Run: python3 scripts/test_pack_short.py"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("console_launch", os.path.join(HERE, "..", "fieldstation42", "console_launch.py"))
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)


class FakeSchedule:
    """Stands in for LiquidSchedule: its own rule is the slot, rounded up (the part the wrapper leaves as it is)."""
    def __init__(self, **conf):
        self.conf = {"schedule_increment": 30, **conf}

    def _calc_target_duration(self, duration, increment=None, break_info=None, break_strategy=None):
        import math
        if increment is None:
            increment = self.conf["schedule_increment"]
        required = duration + sum(break_info[k]["duration"] for k in ("start_bump", "end_bump") if break_info and break_info.get(k))
        multiple = increment * 60
        return multiple * math.ceil(required / multiple)


class ChunkTest(unittest.TestCase):
    def mins(self, minutes, slot=30, extra=0):
        return cl.chunk_minutes(minutes * 60, slot, extra)

    def test_how_many_fit_to_a_half_hour(self):
        for video, per_half_hour in ((16, 1), (15, 2), (11, 2), (10, 3), (9, 3), (7.5, 4), (7, 4), (6, 5), (5, 6), (4.9, 6),
                                     (3, 10), (2, 15), (1, 30)):
            chunk = self.mins(video)
            self.assertEqual(round(30 / chunk), per_half_hour, f"{video} min")
            self.assertGreaterEqual(chunk * 60, video * 60 - 1e-6, f"{video} min must fit in its block")

    def test_blocks_always_add_up_to_the_slot(self):
        for video in (0.5, 1, 1.7, 2.2, 4.2, 6.3, 8, 11, 14.9):
            chunk_s = self.mins(video) * 60
            self.assertEqual(chunk_s, int(chunk_s))                                  # whole seconds
            self.assertEqual(round(1800 / chunk_s) * chunk_s, 1800)                  # a whole number of them fill the half hour

    def test_a_long_video_and_other_slot_lengths(self):
        self.assertEqual(self.mins(22), 30)
        self.assertEqual(self.mins(45, slot=60), 60)
        self.assertEqual(self.mins(20, slot=60), 20)                                 # three to the hour
        self.assertEqual(self.mins(11, slot=15), 15)                                 # one fits a 15 minute slot
        self.assertEqual(self.mins(3, slot=15), 3)                                   # five to the quarter hour
        self.assertEqual(self.mins(0.2), 1)                                          # never under a minute

    def test_bumps_around_a_video_are_part_of_it(self):
        self.assertEqual(self.mins(9, extra=60), 10)                                 # 10 minutes with the bumps still makes three
        self.assertEqual(self.mins(10, extra=60), 15)                                # 11 minutes does not


class PatchTest(unittest.TestCase):
    def setUp(self):
        class Schedule(FakeSchedule):
            pass
        self.cls = Schedule
        cl.patch(Schedule)

    def test_an_eleven_minute_video_gets_fifteen_minutes_not_thirty(self):
        self.assertEqual(self.cls()._calc_target_duration(11 * 60, 30, None, "end"), 15 * 60)
        self.assertEqual(self.cls()._calc_target_duration(22 * 60, 30, None, "end"), 30 * 60)
        self.assertEqual(self.cls()._calc_target_duration(60, 30, None, "end"), 60)

    def test_it_uses_the_stations_slot_when_none_is_given_and_counts_bumps(self):
        self.assertEqual(self.cls(schedule_increment=60)._calc_target_duration(20 * 60), 20 * 60)
        bumps = {"start_bump": {"duration": 60}, "end_bump": None}
        self.assertEqual(self.cls()._calc_target_duration(9 * 60, 30, bumps, "end"), 10 * 60)

    def test_a_station_can_switch_it_off_and_patching_twice_does_nothing_more(self):
        self.assertEqual(self.cls(wrapper_pack_short=False)._calc_target_duration(11 * 60, 30, None, "end"), 30 * 60)
        before = self.cls._calc_target_duration
        cl.patch(self.cls)
        self.assertIs(self.cls._calc_target_duration, before)

    def test_a_station_with_no_breaks_gets_blocks_exactly_as_long_as_its_videos(self):
        s = self.cls(wrapper_no_breaks=True)
        self.assertEqual(s._calc_target_duration(22 * 60 + 17, 30, None, "end"), 22 * 60 + 17)
        self.assertEqual(s._calc_target_duration(11 * 60, 30, None, "end"), 11 * 60)              # no packing into halves either
        bumps = {"start_bump": {"duration": 5}, "end_bump": {"duration": 7}}
        self.assertEqual(s._calc_target_duration(600, 30, bumps, "end"), 612)
        # a slot that chose commercials of its own is opted in: a normal block (the slot, or the packed share of it)
        off = self.cls(wrapper_no_breaks=True, commercial_dir=".commercials.off")
        self.assertEqual(off._calc_target_duration(22 * 60, 30, {"commercial_dir": ".commercials.off"}, "end"), 22 * 60)
        self.assertEqual(off._calc_target_duration(22 * 60, 30, {"commercial_dir": "Ads"}, "end"), 30 * 60)
        self.assertEqual(off._calc_target_duration(11 * 60, 30, {"commercial_dir": "Ads"}, "end"), 15 * 60)

    def test_a_station_can_leave_out_the_start_and_end_cards_of_a_break(self):
        class Catalog:
            def __init__(self, **conf):
                self.config = conf

            def make_reel_fill(self, when, length, use_bumpers=True, commercial_dir=None, bump_dir=None, strict_count=None, lookahead=None):
                return use_bumpers
        cl.patch_mid_cards(Catalog)
        self.assertTrue(Catalog().make_reel_fill(None, 60))
        self.assertTrue(Catalog(wrapper_mid_cards=True).make_reel_fill(None, 60, True))
        self.assertFalse(Catalog(wrapper_mid_cards=False).make_reel_fill(None, 60))
        self.assertFalse(Catalog(wrapper_mid_cards=False).make_reel_fill(None, 60, use_bumpers=True, commercial_dir="x"))
        off = Catalog(wrapper_mid_cards=False, bump_dir=".cards.off")
        self.assertFalse(off.make_reel_fill(None, 60, bump_dir=".cards.off"))                      # only the default: none
        self.assertTrue(off.make_reel_fill(None, 60, bump_dir="Rugrats-cards"))                    # chosen for this time or show: used
        before = Catalog.make_reel_fill
        cl.patch_mid_cards(Catalog)
        self.assertIs(Catalog.make_reel_fill, before)

    def test_a_block_whose_video_left_the_catalog_does_not_stop_a_rebuild(self):
        class Block:
            def __init__(self, seq, content):
                self.sequence_key, self.content = seq, content

        class Content:
            path = "a.mp4"

        class Manager:
            def __init__(self, blocks):
                self.schedules = {"Heros": blocks}
                self.read = []

            def reset_sequences(self, station_config):
                for b in self.schedules["Heros"]:
                    if b.sequence_key:
                        self.read.append(b.content.path)          # the line FieldStation42 stops on when content is None
        cl.patch_reset_sequences(Manager)
        m = Manager([Block({"sequence_name": "s"}, Content()), Block({"sequence_name": "s"}, None), Block(None, None)])
        m.reset_sequences({"network_name": "Heros"})
        self.assertEqual(m.read, ["a.mp4"])                         # the block with no video is skipped, the others are read
        before = Manager.reset_sequences
        cl.patch_reset_sequences(Manager)
        self.assertIs(Manager.reset_sequences, before)

    def test_a_failure_in_the_rule_never_stops_a_schedule_being_built(self):
        real = cl.chunk_minutes
        cl.chunk_minutes = lambda *a, **k: 1 / 0
        try:
            self.assertEqual(self.cls()._calc_target_duration(11 * 60, 30, None, "end"), 30 * 60)       # FieldStation42's own answer
        finally:
            cl.chunk_minutes = real


if __name__ == "__main__":
    unittest.main()
