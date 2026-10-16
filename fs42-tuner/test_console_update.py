"""Console.update_catalog: runs the catalog-only update script, one build at a time, and reports why it failed.

Run: python3 test_console_update.py
"""
import os
import shutil
import tempfile
import unittest

import console_client
import schedlock


class UpdateCatalogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = dict(os.environ)
        self._lock = schedlock.PATH
        schedlock.PATH = os.path.join(self.dir, "lock")
        os.environ["FS42_APP"] = self.dir                     # an existing folder to run in

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        schedlock.PATH = self._lock
        shutil.rmtree(self.dir)

    def script(self, body):
        path = os.path.join(self.dir, "update.py")
        with open(path, "w") as f:
            f.write(body)
        os.environ["FS42_UPDATE_CATALOG"] = path
        return console_client.Console("http://127.0.0.1:1")

    def test_it_runs_the_script_with_the_station_name_and_returns_what_it_printed(self):
        c = self.script('import sys\nprint("Catalog updated for " + sys.argv[1])\n')
        self.assertEqual(c.update_catalog("Odd Name/Here"), "Catalog updated for Odd Name/Here")

    def test_a_failure_comes_back_as_a_message_for_the_page(self):
        c = self.script('import sys\nprint("could not update the catalog: disk full", file=sys.stderr)\nsys.exit(1)\n')
        with self.assertRaises(console_client.ConsoleError) as cm:
            c.update_catalog("X")
        self.assertIn("disk full", str(cm.exception))

    def test_a_script_that_cannot_start_is_reported(self):
        c = console_client.Console("http://127.0.0.1:1")
        os.environ["FS42_UPDATE_CATALOG"] = os.path.join(self.dir, "missing.py")
        with self.assertRaises(console_client.ConsoleError):
            c.update_catalog("X")

    def test_progress_is_reported_while_it_runs_and_a_slow_one_is_cut_off(self):
        c = self.script("import time\ntime.sleep(30)\n")
        calls = []
        with self.assertRaises(console_client.ConsoleError) as cm:
            c.update_catalog("X", timeout=3, progress=calls.append)
        self.assertIn("took too long", str(cm.exception))
        self.assertTrue(calls)

    def test_it_waits_its_turn_behind_another_build(self):
        c = self.script('print("done")\n')
        waited = []
        with schedlock.held():                                # another build holds the lock ...
            import threading
            t = threading.Timer(0.5, lambda: None)
            out = []
            runner = threading.Thread(target=lambda: out.append(c.update_catalog("X", on_wait=lambda: waited.append(1))))
            runner.start()
            t.start()
            t.join()
            self.assertEqual(out, [])                         # ... so it has not run yet
        runner.join(10)
        self.assertEqual((waited, out), ([1], ["done"]))


if __name__ == "__main__":
    unittest.main()
