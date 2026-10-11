"""Tests for fs42_link.py: links go in, copies never do.

Run: python3 scripts/test_fs42_link.py
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest

import fs42_link


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.pool = os.path.join(self.root, "media-links")
        self.catalog = os.path.join(self.root, "fs42", "catalog")
        self.tag = os.path.join(self.catalog, "by-genre", "Comedy")
        # the pool: links with container-form targets that do not exist on this host
        for show in ("Show A", "Show B"):
            d = os.path.join(self.pool, "tvseries", show, "Season 1")
            os.makedirs(d)
            os.symlink(f"/mnt/media/tvseries/{show}/Season 1/S01E01.mp4", os.path.join(d, "S01E01.mp4"))
            os.symlink(f"/mnt/media/tvseries/{show}/Season 1/S01E01.nfo", os.path.join(d, "S01E01.nfo"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fs42_link.main(["--catalog", self.catalog, *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_a_link_is_recreated_with_the_same_target(self):
        src = os.path.join(self.pool, "tvseries/Show A/Season 1/S01E01.mp4")
        rc, out, _ = self.run_cli("Comedy", src)
        self.assertEqual(rc, 0)
        link = os.path.join(self.tag, "S01E01.mp4")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), "/mnt/media/tvseries/Show A/Season 1/S01E01.mp4")
        self.assertIn("added 1", out)
        self.assertIn("catalog must be rebuilt", out)

    def test_a_folder_keeps_its_layout_so_two_shows_do_not_collide(self):
        self.run_cli("Comedy", os.path.join(self.pool, "tvseries/Show A"), os.path.join(self.pool, "tvseries/Show B"))
        for show in ("Show A", "Show B"):
            link = os.path.join(self.tag, show, "Season 1", "S01E01.mp4")
            self.assertEqual(os.readlink(link), f"/mnt/media/tvseries/{show}/Season 1/S01E01.mp4")

    def test_only_videos_are_linked_not_nfos(self):
        self.run_cli("Comedy", os.path.join(self.pool, "tvseries/Show A"))
        names = [f for _r, _d, fs in os.walk(self.tag) for f in fs]
        self.assertEqual(names, ["S01E01.mp4"])

    def test_a_real_file_is_refused_so_nothing_is_ever_duplicated(self):
        real = os.path.join(self.root, "big video.mp4")
        touch(real, "pretend this is 4 GB")
        rc, out, err = self.run_cli("Comedy", real)
        self.assertEqual(rc, 1)
        self.assertIn("would duplicate the video", err)
        self.assertFalse(os.path.exists(self.tag))
        # and a real file inside a folder is skipped, while the links beside it still go in
        mixed = os.path.join(self.pool, "mixed")
        touch(os.path.join(mixed, "real.mp4"), "x")
        os.symlink("/mnt/media/a.mp4", os.path.join(mixed, "link.mp4"))
        rc, out, err = self.run_cli("Comedy", mixed)
        self.assertEqual(rc, 0)
        self.assertIn("real.mp4", err)
        self.assertTrue(os.path.islink(os.path.join(self.tag, "mixed", "link.mp4")))
        self.assertFalse(os.path.lexists(os.path.join(self.tag, "mixed", "real.mp4")))

    def test_running_it_twice_adds_nothing_the_second_time(self):
        src = os.path.join(self.pool, "tvseries/Show A")
        self.run_cli("Comedy", src)
        rc, out, _ = self.run_cli("Comedy", src)
        self.assertIn("added 0 links, 1 already there", out)

    def test_nothing_is_replaced(self):
        os.makedirs(self.tag)
        os.symlink("/mine/other.mp4", os.path.join(self.tag, "S01E01.mp4"))
        rc, _out, err = self.run_cli("Comedy", os.path.join(self.pool, "tvseries/Show A/Season 1/S01E01.mp4"))
        self.assertEqual(os.readlink(os.path.join(self.tag, "S01E01.mp4")), "/mine/other.mp4")
        self.assertIn("points elsewhere", err)

    def test_dry_run_changes_nothing(self):
        rc, out, _ = self.run_cli("Comedy", os.path.join(self.pool, "tvseries/Show A"), "--dry-run")
        self.assertIn("would add 1", out)
        self.assertFalse(os.path.exists(self.catalog))

    def test_a_missing_source_and_a_bad_tag_are_clear_errors(self):
        rc, _o, err = self.run_cli("Comedy", os.path.join(self.root, "nope"))
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        for bad in ("../x", "a/b", ".hidden", ".."):
            with self.assertRaises(SystemExit, msg=bad):
                self.run_cli(bad, os.path.join(self.pool, "tvseries"))

    def test_the_catalog_is_found_from_the_environment(self):
        self.assertEqual(fs42_link.catalog_dir("/x", {}), "/x")
        self.assertEqual(fs42_link.catalog_dir(None, {"FS42_CATALOG_ROOT": "/c", "FS42_LOCAL_ROOT": "/l"}), "/c")
        self.assertEqual(fs42_link.catalog_dir(None, {"FS42_LOCAL_ROOT": "/l"}), "/l/catalog")
        self.assertEqual(fs42_link.catalog_dir(None, {"FS42_SSD_ROOT": "/s"}), "/s/catalog")   # the older name
        self.assertIsNone(fs42_link.catalog_dir(None, {}))
        self.assertEqual(fs42_link.catalog_dir(None, {"FS42_IN_CONTAINER": "1"}), "/app/catalog")   # inside the container


if __name__ == "__main__":
    unittest.main()
