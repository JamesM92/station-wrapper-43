"""Episodes of a show are played in natural order, and a show that is part way through keeps its place
(fieldstation42/console_launch.py: patch_episode_order, fs42-tuner/ordering.py).
Run: python3 scripts/test_episode_order.py (needs the vendored FieldStation42 under fieldstation42/src)"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "fieldstation42", "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(HERE, "..", "fs42-tuner"))
spec = importlib.util.spec_from_file_location("console_launch", os.path.join(HERE, "..", "fieldstation42", "console_launch.py"))
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)
ordering = importlib.import_module("ordering")

try:
    from fs42.sequence import NamedSequence
    from fs42.sequence_io import SequenceIO
except (Exception, SystemExit):
    NamedSequence = SequenceIO = None

EPS = [f"/lists/Show/Season 2/Show - Episode {n}.mkv" for n in (1, 2, 3, 10, 11)]


class Key(unittest.TestCase):
    def test_numbers_compare_as_numbers_and_folders_come_first(self):
        paths = ["Show/Season 10/E1.mkv", "Show/Season 2/E10.mkv", "Show/Season 2/E2.mkv", "Show/Season 2/E1.mkv"]
        self.assertEqual(sorted(paths, key=ordering.natural_key),
                         ["Show/Season 2/E1.mkv", "Show/Season 2/E2.mkv", "Show/Season 2/E10.mkv", "Show/Season 10/E1.mkv"])

    def test_zero_padded_names_are_unchanged_and_case_is_ignored(self):
        paths = ["b S01E02.mkv", "a S01E10.mkv", "A S01E02.mkv"]
        self.assertEqual(sorted(paths, key=ordering.natural_key), ["A S01E02.mkv", "a S01E10.mkv", "b S01E02.mkv"])


@unittest.skipUnless(NamedSequence, "FieldStation42 is not importable")
class Sequence(unittest.TestCase):
    def setUp(self):
        self.populate = NamedSequence.populate
        self.update = SequenceIO.update_sequence_entries

    def tearDown(self):
        NamedSequence.populate = self.populate
        SequenceIO.update_sequence_entries = self.update

    def names(self, seq):
        return [os.path.basename(e.fpath).replace("Show - Episode ", "").replace(".mkv", "") for e in seq.episodes]

    def test_upstream_alone_plays_episode_10_before_episode_2(self):
        seq = NamedSequence("S", "series", "Show", 0, 1, 0, list(EPS), False)
        self.assertEqual(self.names(seq), ["1", "10", "11", "2", "3"])

    def test_with_the_patch_a_new_sequence_is_in_natural_order(self):
        cl.patch_episode_order(NamedSequence, SequenceIO)
        seq = NamedSequence("S", "series", "Show", 0, 1, 0, list(EPS), False)
        self.assertEqual(self.names(seq), ["1", "2", "3", "10", "11"])
        self.assertEqual(seq.current_index, 0)

    def test_a_show_part_way_through_keeps_its_place_across_the_change_of_order(self):
        cl.patch_episode_order(NamedSequence, SequenceIO)
        stored = sorted(EPS)                                      # the order an earlier run stored: 1, 10, 11, 2, 3
        index = stored.index(EPS[3])                              # it was at Episode 10
        seq = NamedSequence("S", "series", "Show", 0, 1, index, stored, True)
        self.assertEqual(self.names(seq)[seq.current_index], "10")
        self.assertEqual(self.names(seq), ["1", "2", "3", "10", "11"])

    def test_the_patch_is_not_stacked(self):
        cl.patch_episode_order(NamedSequence, SequenceIO)
        first = NamedSequence.__dict__["populate"]
        cl.patch_episode_order(NamedSequence, SequenceIO)
        self.assertIs(NamedSequence.__dict__["populate"], first)

    def test_updating_the_files_stores_the_natural_order_and_follows_the_current_file(self):
        cl.patch_episode_order(NamedSequence, SequenceIO)
        db = os.path.join(tempfile.mkdtemp(), "s.db")
        io = object.__new__(SequenceIO)
        io.db_path = db
        io._init_sequence_table()
        with sqlite3.connect(db) as c:
            c.execute("INSERT INTO named_sequence (station, sequence_name, tag_path, start_perc, end_perc, current_index) VALUES ('S','series','Show',0,1,3)")
        io.update_sequence_entries("S", "series", "Show", list(reversed(EPS)), EPS[3], 3)
        with sqlite3.connect(db) as c:
            rows = [r[0] for r in c.execute("SELECT fpath FROM sequence_entries ORDER BY sequence_index")]
            idx = c.execute("SELECT current_index FROM named_sequence").fetchone()[0]
        self.assertEqual(rows, EPS)
        self.assertEqual(rows[idx], EPS[3])


if __name__ == "__main__":
    unittest.main()
