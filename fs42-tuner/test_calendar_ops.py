"""The calendar's editing rules (static/calendar.js, CalendarOps), run under Node. Skipped when Node is not installed.

Run: python3 test_calendar_ops.py
"""
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = shutil.which("node")


def ops(expr):
    """Evaluate a JS expression that uses CalendarOps and return its JSON value."""
    script = f"require({json.dumps(os.path.join(HERE, 'static', 'calendar.js'))}); const O = CalendarOps; " \
             f"console.log(JSON.stringify({expr}));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


V = lambda a, b, *l: {"from": a, "to": b, "lists": list(l)}


@unittest.skipUnless(NODE, "needs Node")
class OpsTest(unittest.TestCase):
    def test_painting_a_slot_splits_the_block_under_it(self):
        out = ops(f"O.paint({json.dumps([V(0, 24, 'A')])}, 20, 22, {{lists: ['B']}})")
        self.assertEqual(out, [V(0, 20, "A"), V(20, 22, "B"), V(22, 24, "A")])

    def test_painting_over_several_blocks_trims_them(self):
        base = [V(0, 8, "A"), V(8, 16, "B"), V(16, 24, "C")]
        out = ops(f"O.paint({json.dumps(base)}, 6, 18, {{lists: ['X']}})")
        self.assertEqual(out, [V(0, 6, "A"), V(6, 18, "X"), V(18, 24, "C")])

    def test_a_slot_is_moved_by_taking_it_out_and_putting_it_down_again(self):
        base = [V(0, 8, "A"), V(8, 10, "B"), V(10, 24, "C")]
        lifted = f"O.remove({json.dumps(base)}, 1, true)"                   # a full day: the hole is closed by the block before it
        out = ops(f"O.place({lifted}, {json.dumps(V(8, 10, 'B'))}, 18, 0.5)")
        self.assertEqual(out, [V(0, 10, "A"), V(10, 18, "C"), V(18, 20, "B"), V(20, 24, "C")])

    def test_a_moved_slot_snaps_to_the_unit_and_stays_inside_the_day(self):
        one = json.dumps(V(8, 10, "B"))
        self.assertEqual(ops(f"O.place([], {one}, 23, 0.5)"), [V(22, 24, "B")])           # too late: pushed back to fit
        self.assertEqual(ops(f"O.place([], {one}, -3, 0.5)"), [V(0, 2, "B")])
        self.assertEqual(ops(f"O.place([], {one}, 5.3, 0.5)"), [V(5.5, 7.5, "B")])
        self.assertEqual(ops(f"O.place([], {json.dumps({'from': 8, 'to': 10, 'list': 'ads'})}, 5.4, 1)"),
                         [{"from": 5, "to": 7, "list": "ads"}])                             # the commercials layer moves by whole hours

    def test_a_slot_moved_next_to_one_that_says_the_same_joins_it(self):
        out = ops(f"O.place({json.dumps([V(0, 6, 'A')])}, {json.dumps(V(10, 12, 'A'))}, 6, 0.5)")
        self.assertEqual(out, [V(0, 8, "A")])

    def test_neighbours_with_the_same_lists_become_one(self):
        out = ops(f"O.paint({json.dumps([V(0, 12, 'A'), V(12, 24, 'B')])}, 12, 24, {{lists: ['A']}})")
        self.assertEqual(out, [V(0, 24, "A")])

    def test_half_hours(self):
        out = ops(f"O.paint({json.dumps([V(0, 24, 'A')])}, 20.5, 21.5, {{lists: ['B']}})")
        self.assertEqual(out, [V(0, 20.5, "A"), V(20.5, 21.5, "B"), V(21.5, 24, "A")])

    def test_removing_in_a_full_day_closes_the_hole(self):
        base = [V(0, 8, "A"), V(8, 16, "B"), V(16, 24, "C")]
        self.assertEqual(ops(f"O.remove({json.dumps(base)}, 1, true)"), [V(0, 16, "A"), V(16, 24, "C")])
        self.assertEqual(ops(f"O.remove({json.dumps(base)}, 0, true)"), [V(0, 16, "B"), V(16, 24, "C")])
        self.assertEqual(ops(f"O.remove({json.dumps(base)}, 1, false)"), [V(0, 8, "A"), V(16, 24, "C")])

    def test_resizing_a_full_day_moves_the_neighbours_edge(self):
        base = [V(0, 8, "A"), V(8, 16, "B"), V(16, 24, "C")]
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'to', 20, true, 0.5)"), [V(0, 8, "A"), V(8, 20, "B"), V(20, 24, "C")])
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'from', 4, true, 0.5)"), [V(0, 4, "A"), V(4, 16, "B"), V(16, 24, "C")])
        # it can't squash the neighbour out of existence
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'to', 24, true, 0.5)")[1:], [V(8, 23.5, "B"), V(23.5, 24, "C")])
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'to', 8, true, 0.5)")[1], V(8, 8.5, "B"))              # at least one unit

    def test_making_a_slot_smaller_does_not_stretch_its_neighbour(self):
        base = [V(0, 8, "A"), V(8, 16, "B"), V(16, 24, "C")]
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'to', 12, true, 0.5)"), [V(0, 8, "A"), V(8, 12, "B"), V(16, 24, "C")])
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'from', 12, true, 0.5)"), [V(0, 8, "A"), V(12, 16, "B"), V(16, 24, "C")])
        self.assertIn("Nothing is scheduled from 12:00", ops(f"O.problem({json.dumps([V(0, 8, 'A'), V(8, 12, 'B'), V(16, 24, 'C')])}, true, true)"))

    def test_growing_again_into_the_space_it_left_takes_nothing_from_the_neighbour(self):
        gap = [V(0, 8, "A"), V(8, 12, "B"), V(16, 24, "C")]
        self.assertEqual(ops(f"O.resize({json.dumps(gap)}, 1, 'to', 14, true, 0.5)"), [V(0, 8, "A"), V(8, 14, "B"), V(16, 24, "C")])
        self.assertEqual(ops(f"O.resize({json.dumps(gap)}, 1, 'to', 18, true, 0.5)"), [V(0, 8, "A"), V(8, 18, "B"), V(18, 24, "C")])      # past the gap it does take it

    def test_resizing_a_partial_day_stops_at_the_neighbour(self):
        base = [V(8, 10, "A"), V(12, 14, "B")]
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 0, 'to', 20, false, 0.5)")[0], V(8, 12, "A"))
        self.assertEqual(ops(f"O.resize({json.dumps(base)}, 1, 'from', 0, false, 0.5)")[1], V(10, 14, "B"))

    def test_problems_are_described(self):
        self.assertIsNone(ops(f"O.problem({json.dumps([V(0, 24, 'A')])}, true, true)"))
        self.assertIn("Nothing is scheduled from 12:00", ops(f"O.problem({json.dumps([V(0, 12, 'A')])}, true, true)"))
        self.assertIsNone(ops(f"O.problem({json.dumps([V(20, 22, 'A')])}, false, true)"))              # an overlay may have gaps
        self.assertIn("only half filled", ops(f"O.problem({json.dumps([V(20, 21.5, 'A')])}, false, true)"))
        self.assertIn("only one list", ops(f"O.problem({json.dumps([V(0, 20.5, 'A', 'C'), V(20.5, 24, 'B')])}, true, true)"))
        self.assertIn("no list yet", ops(f"O.problem({json.dumps([V(0, 24)])}, true, true)"))

    def test_commercial_blocks_carry_their_own_payload(self):
        out = ops("O.paint([{from: 8, to: 12, list: 'Ads'}], 10, 14, {list: 'Ads2'})")
        self.assertEqual(out, [{"from": 8, "to": 10, "list": "Ads"}, {"from": 10, "to": 14, "list": "Ads2"}])


@unittest.skipUnless(NODE, "needs Node")
class SpanTest(unittest.TestCase):
    """A slot on several days: the same time on each, one id, edited together."""
    ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    def week(self):
        return {d: [V(0, 24, "A")] for d in self.ORDER}

    def three(self):
        return ops(f"O.paintSame({json.dumps(self.week())}, {json.dumps(self.ORDER)}, 0, 2, 15, 16, {{lists: ['B']}}, 's1')")

    def test_a_slot_on_several_days_is_the_same_time_on_each(self):
        out = self.three()
        for d in ("monday", "tuesday", "wednesday"):
            self.assertEqual(out[d], [V(0, 15, "A"), {**V(15, 16, "B"), "span": "s1"}, V(16, 24, "A")])
        self.assertEqual(out["thursday"], [V(0, 24, "A")])

    def test_either_direction_gives_the_same_days(self):
        out = ops(f"O.paintSame({json.dumps(self.week())}, {json.dumps(self.ORDER)}, 2, 0, 15, 16, {{lists: ['B']}}, 's1')")
        self.assertEqual(out, self.three())

    def test_one_day_is_an_ordinary_slot(self):
        out = ops(f"O.paintSame({json.dumps(self.week())}, {json.dumps(self.ORDER)}, 2, 2, 8, 10, {{lists: ['B']}}, 's1')")
        self.assertTrue(all("span" not in b for b in out["wednesday"]))

    def test_settings_change_on_every_piece(self):
        out = ops(f"O.updateSpan({json.dumps(self.three())}, 's1', (b) => {{ b.lists = ['C']; b.burn = true; }})")
        pieces = [b for d in out.values() for b in d if b.get("span") == "s1"]
        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(b["lists"] == ["C"] and b["burn"] for b in pieces))

    def test_removing_it_takes_every_day_and_closes_the_holes(self):
        out = ops(f"O.removeSpan({json.dumps(self.three())}, 's1', true)")
        for d in ("monday", "tuesday", "wednesday"):
            self.assertEqual(out[d], [V(0, 24, "A")])

    def test_a_piece_left_alone_is_an_ordinary_slot_again(self):
        w = self.three()
        w["tuesday"] = [V(0, 24, "A")]
        w["wednesday"] = [V(0, 24, "A")]
        out = ops(f"O.cleanSpans({json.dumps(w)})")
        self.assertTrue(all("span" not in b for b in out["monday"]))

    def test_unlink_keeps_the_pieces(self):
        out = ops(f"O.unlinkSpan({json.dumps(self.three())}, 's1')")
        self.assertEqual(out["tuesday"][1], V(15, 16, "B"))

    def test_bounds(self):
        self.assertEqual(ops(f"O.spanBounds({json.dumps(self.three())}, {json.dumps(self.ORDER)}, 's1')"),
                         {"keys": ["monday", "tuesday", "wednesday"], "from": 15, "to": 16, "days": 3})

    def test_plain_drops_the_link(self):
        self.assertEqual(ops("O.plain({from: 1, to: 2, lists: ['A'], span: 'x'})"), V(1, 2, "A"))


if __name__ == "__main__":
    unittest.main()
