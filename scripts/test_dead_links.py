"""A link to a video that is gone must not stop a catalog rebuild (fieldstation42/console_launch.py).
Run: python3 scripts/test_dead_links.py (needs the vendored FieldStation42 under fieldstation42/src)"""
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "fieldstation42", "src")
sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("console_launch", os.path.join(HERE, "..", "fieldstation42", "console_launch.py"))
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)
try:
    from fs42.media_processor import MediaProcessor
except (Exception, SystemExit):                             # the engine's sources, or its ffmpeg-python, are not here
    MediaProcessor = None


@unittest.skipUnless(MediaProcessor, "FieldStation42 is not importable")
class DeadLinkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        season = os.path.join(self.dir, "Show", "Season04[2022]")
        os.makedirs(season)
        for n in ("a.mkv", "c.mkv"):
            open(os.path.join(season, n), "w").close()
        os.symlink(os.path.join(self.dir, "gone", "b.mkv"), os.path.join(season, "b.mkv"))      # leads nowhere
        self.original = MediaProcessor.__dict__["rich_find_media"]

    def tearDown(self):
        MediaProcessor.rich_find_media = self.original

    def test_upstream_alone_stops_on_a_dead_link(self):
        with self.assertRaises(FileNotFoundError):
            MediaProcessor.rich_find_media(self.dir)

    def test_with_the_guard_the_rest_of_the_list_is_scanned(self):
        self.assertTrue(cl.patch_dead_links(MediaProcessor))
        got = MediaProcessor.rich_find_media(self.dir)
        names = sorted(os.path.basename(e.path) for e in got)
        self.assertEqual(names, ["a.mkv", "c.mkv"])
        self.assertTrue(all(e.size == 0 and e.last_mod > 0 for e in got))

    def test_a_clean_list_goes_through_unchanged_and_the_guard_is_not_stacked(self):
        os.unlink(os.path.join(self.dir, "Show", "Season04[2022]", "b.mkv"))
        cl.patch_dead_links(MediaProcessor)
        first = MediaProcessor.__dict__["rich_find_media"]
        cl.patch_dead_links(MediaProcessor)
        self.assertIs(MediaProcessor.__dict__["rich_find_media"].__func__ if hasattr(MediaProcessor.__dict__["rich_find_media"], "__func__") else MediaProcessor.__dict__["rich_find_media"],
                      first.__func__ if hasattr(first, "__func__") else first)
        self.assertEqual(len(MediaProcessor.rich_find_media(self.dir)), 2)


if __name__ == "__main__":
    unittest.main()
