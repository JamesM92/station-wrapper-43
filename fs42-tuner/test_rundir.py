import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rundir


class PrivateFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = os.environ.get("FS42_RUN_DIR")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("FS42_RUN_DIR", None)
        else:
            os.environ["FS42_RUN_DIR"] = self.saved

    def test_it_is_made_private(self):
        os.environ["FS42_RUN_DIR"] = os.path.join(self.tmp, "run")
        p = rundir.path("x.lock")
        self.assertEqual(p, os.path.join(self.tmp, "run", "x.lock"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode), 0o700)

    def test_a_folder_others_can_open_is_refused(self):
        d = os.path.join(self.tmp, "open")
        os.mkdir(d, 0o755)
        os.chmod(d, 0o755)
        os.environ["FS42_RUN_DIR"] = d
        with self.assertRaises(OSError):
            rundir.path("x")

    def test_a_link_in_its_place_is_refused(self):
        target = os.path.join(self.tmp, "t")
        os.mkdir(target, 0o700)
        link = os.path.join(self.tmp, "l")
        os.symlink(target, link)
        os.environ["FS42_RUN_DIR"] = link
        with self.assertRaises(OSError):
            rundir.path("x")

    def test_default_is_per_user_under_the_temp_folder(self):
        os.environ.pop("FS42_RUN_DIR", None)
        d = rundir.directory()
        self.assertTrue(d.startswith(tempfile.gettempdir()))
        self.assertTrue(d.endswith(str(os.getuid())))


if __name__ == "__main__":
    unittest.main()
