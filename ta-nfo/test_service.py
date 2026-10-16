"""Tests for the TubeArchivist job (service.py), with a stand-in for TA_NFOWriter.

Run: python3 test_service.py
"""
import json
import importlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "fs42-tuner"))
settings = importlib.import_module("settings")

STUB = '''import os, sys
log = os.environ["STUB_LOG"]
open(log, "a").write(" ".join(sys.argv[1:]) + "\\n")
view = sys.argv[sys.argv.index("--view-dir") + 1]
os.makedirs(os.path.join(view, "Some Channel"), exist_ok=True)
open(os.path.join(view, "Some Channel", "tvshow.nfo"), "w").write("<tvshow/>")
print("done: 3 videos seen")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''


class Job:
    def __init__(self, root, env=None):
        self.root = root
        self.settings = os.path.join(root, "settings.json")
        self.status = os.path.join(root, "status.json")
        self.run_now = os.path.join(root, "ta.now")
        self.farm_now = os.path.join(root, "farm.now")
        self.view = os.path.join(root, "tanfo")
        self.log = os.path.join(root, "stub.log")
        stub = os.path.join(root, "stub.py")
        with open(stub, "w") as f:
            f.write(STUB)
        self.env = {**os.environ, "FS42_TA_NOW": self.run_now, "FS42_FARM_NOW": self.farm_now, "FS42_SETTINGS": self.settings, "FS42_TA_STATUS": self.status, "TA_VIEW": self.view,
                    "TA_NFO": stub, "STUB_LOG": self.log, "TA_POLL_SECONDS": "0.1", **(env or {})}
        # the module reads its paths at import, so run it as a real process with this environment
        code = ("import settings; settings.RUN_NOW=%r; settings.FARM_NOW=%r; import runpy, sys; sys.argv=['service.py'];"
                "runpy.run_path(%r, run_name='__main__')" % (self.run_now, self.farm_now, os.path.join(HERE, "service.py")))
        self.proc = subprocess.Popen([sys.executable, "-u", "-c", code], env={**self.env, "PYTHONPATH": os.path.join(HERE, "..", "fs42-tuner")},
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def runs(self):
        try:
            return open(self.log).read().splitlines()
        except OSError:
            return []

    def state(self):
        try:
            return json.load(open(self.status))
        except (OSError, ValueError):
            return {}

    def wait(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def save(self, **kw):
        settings.set_ta(kw, self.settings)

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.lib = os.path.join(self.root, "youtube")
        os.makedirs(self.lib)
        self.job = None

    def tearDown(self):
        if self.job and self.job.proc.poll() is None:
            self.job.proc.kill()
        shutil.rmtree(self.root)

    def start(self, env=None):
        self.job = Job(self.root, env)
        return self.job

    def test_it_does_nothing_until_it_is_switched_on(self):
        j = self.start()
        self.assertTrue(j.wait(lambda: j.state().get("state") == "disabled"))
        time.sleep(0.5)
        self.assertEqual(j.runs(), [])
        self.assertEqual(j.stop(), 0)

    def test_switching_it_on_runs_it_at_once_and_tells_the_farm(self):
        j = self.start()
        self.assertTrue(j.wait(lambda: j.state().get("state") == "disabled"))
        j.save(enabled=True, library=self.lib)
        self.assertTrue(j.wait(lambda: len(j.runs()) == 1))
        self.assertIn(self.lib, j.runs()[0])
        self.assertIn("--view-dir " + os.path.join(j.view, "youtube-nfo"), j.runs()[0])
        self.assertTrue(j.wait(lambda: j.state().get("state") == "idle" and j.state().get("last_ok")))
        self.assertEqual(j.state()["channels"], 1)
        self.assertTrue(os.path.exists(j.farm_now))                 # new channels: make their tag folders now
        j.stop()

    def test_convert_now_runs_it_again_and_switching_off_stops_the_runs(self):
        j = self.start()
        j.save(enabled=True, library=self.lib)
        self.assertTrue(j.wait(lambda: len(j.runs()) == 1))
        open(j.run_now, "w").close()
        self.assertTrue(j.wait(lambda: len(j.runs()) == 2))
        self.assertFalse(os.path.exists(j.run_now))                  # the request is consumed
        j.save(enabled=False)
        self.assertTrue(j.wait(lambda: j.state().get("state") == "disabled"))
        open(j.run_now, "w").close()
        time.sleep(0.5)
        self.assertEqual(len(j.runs()), 2)                           # off means off, even when asked
        self.assertFalse(os.path.exists(j.run_now))
        j.stop()

    def test_choosing_another_library_runs_it_for_the_new_one(self):
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        j = self.start()
        j.save(enabled=True, library=self.lib)
        self.assertTrue(j.wait(lambda: len(j.runs()) == 1))
        j.save(library=other)
        self.assertTrue(j.wait(lambda: len(j.runs()) == 2))
        self.assertIn(other, j.runs()[1])
        j.stop()

    def test_it_runs_again_when_the_interval_has_passed(self):
        j = self.start({"TA_INTERVAL_MINUTES": "15"})               # the shortest allowed: 15 minutes
        j.save(enabled=True, library=self.lib)
        self.assertTrue(j.wait(lambda: len(j.runs()) == 1))
        time.sleep(0.6)
        self.assertEqual(len(j.runs()), 1)                          # not yet
        j.stop()

    def test_a_missing_library_is_reported_not_crashed_on(self):
        j = self.start()
        j.save(enabled=True, library=os.path.join(self.root, "nope"))
        self.assertTrue(j.wait(lambda: j.state().get("state") == "error"))
        self.assertIn("no TubeArchivist library", j.state()["message"])
        self.assertEqual(j.runs(), [])
        self.assertIsNone(j.proc.poll())                             # still alive: fix the folder and it carries on
        j.save(library=self.lib)
        self.assertTrue(j.wait(lambda: len(j.runs()) == 1))
        self.assertTrue(j.wait(lambda: j.state().get("state") == "idle"))
        j.stop()

    def test_a_failing_run_is_reported_and_the_farm_is_not_told(self):
        j = self.start({"STUB_EXIT": "3"})
        j.save(enabled=True, library=self.lib)
        self.assertTrue(j.wait(lambda: j.state().get("state") == "error" and j.state().get("last_ok") is False))
        self.assertIn("exit 3", j.state()["message"])
        self.assertFalse(os.path.exists(j.farm_now))
        j.stop()

    def test_each_library_gets_its_own_view_and_a_failure_does_not_stop_the_others(self):
        second = os.path.join(self.root, "more", "YT Archive")
        os.makedirs(second)
        j = self.start()
        j.save(enabled=True, libraries=[self.lib, second, os.path.join(self.root, "gone")])
        self.assertTrue(j.wait(lambda: j.state().get("state") == "error"))
        runs = j.runs()
        self.assertEqual(len(runs), 2)
        self.assertIn("--view-dir " + os.path.join(j.view, "youtube-nfo"), runs[0])
        self.assertIn("--view-dir " + os.path.join(j.view, "YT Archive-nfo"), runs[1])
        st = j.state()
        self.assertEqual([d["ok"] for d in st["libraries"]], [True, True, False])
        self.assertEqual(st["channels"], 2)
        self.assertIn("gone-nfo", st["message"])
        j.stop()

    def test_a_library_taken_out_of_the_list_loses_its_view_and_the_old_flat_view_goes(self):
        for d in ("old-nfo", "Some Channel", "Playlists", "mine"):             # what an earlier run and an older version left
            os.makedirs(os.path.join(j_view := os.path.join(self.root, "tanfo"), d), exist_ok=True)
        open(os.path.join(j_view, "Some Channel", "tvshow.nfo"), "w").close()
        open(os.path.join(j_view, "mine", "keep.txt"), "w").close()
        j = self.start()
        j.save(enabled=True, library=self.lib)
        self.assertTrue(j.wait(lambda: j.state().get("last_ok")))
        left = sorted(os.listdir(j_view))
        self.assertEqual(left, ["mine", "youtube-nfo"])              # only what is not ours is left alone
        j.stop()

    def test_a_wrong_library_setting_never_wipes_an_existing_view(self):
        view = os.path.join(self.root, "tanfo")
        os.makedirs(os.path.join(view, "youtube-nfo", "Some Channel"))
        open(os.path.join(view, "youtube-nfo", "Some Channel", "tvshow.nfo"), "w").close()
        j = self.start()
        j.save(enabled=True, library=os.path.join(self.root, "not-there"))        # the settings now name another folder
        self.assertTrue(j.wait(lambda: j.state().get("state") == "error"))
        self.assertTrue(os.path.isfile(os.path.join(view, "youtube-nfo", "Some Channel", "tvshow.nfo")))   # still there
        j.save(library=self.lib)                                                   # a good run: now it is tidied
        self.assertTrue(j.wait(lambda: j.state().get("last_ok")))
        self.assertFalse(os.path.exists(os.path.join(view, "youtube-nfo", "Some Channel", "tvshow.nfo")) and
                         not os.path.isdir(os.path.join(view, "youtube-nfo")))
        j.stop()

    def test_the_old_environment_variables_still_switch_it_on(self):
        j = self.start({"TA_ENABLED": "1", "TA_LIBRARY": self.lib})
        self.assertTrue(j.wait(lambda: len(j.runs()) == 1))
        j.stop()


class ProgressLinesTest(unittest.TestCase):
    """TA_NFOWriter's --progress lines (0.5 and later), as the status the dashboard shows."""
    def setUp(self):
        sys.path.insert(0, HERE)
        import service
        self.p = service.parse_progress

    def test_the_total_up_front(self):
        self.assertEqual(self.p("480 channels, 12,345 videos to look at, 16 in parallel"), {"channel_total": 480, "videos_total": 12345})

    def test_a_channel_finished(self):
        self.assertEqual(self.p("[12/480] UCaaaaaaaaaaaaaaaaaaaaaa: 30 videos, 4 read, 2.1s; 14 videos/s"),
                         {"channels_done": 12, "channel_total": 480, "last_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"})

    def test_a_heartbeat_names_the_channel_it_is_inside(self):
        self.assertEqual(self.p("  ... UCbbbbbbbbbbbbbbbbbbbbbb: 1,200/3,400 videos, 37 videos/s, about 20m left"),
                         {"channel_id": "UCbbbbbbbbbbbbbbbbbbbbbb", "channel_videos_done": 1200, "channel_videos_total": 3400})

    def test_other_lines_are_not_progress(self):
        self.assertEqual(self.p("time: 5m in total = 1m listing"), {})               # a progress line with nothing to show
        for line in ("done: 3 videos seen, 2 files written, 0 removed", "error: cannot read x", "write /x/y.nfo", ""):
            self.assertIsNone(self.p(line), line)


class WhereItIsTest(unittest.TestCase):
    """Which channel the converter is on, seen from the file it has open (it prints nothing while it reads)."""
    A, B = "UC" + "a" * 22, "UC" + "b" * 22

    def setUp(self):
        sys.path.insert(0, HERE)
        import service
        self.service = service
        self.root = tempfile.mkdtemp()
        self.lib = os.path.join(self.root, "lib")
        self.view = os.path.join(self.root, "view")
        for cid in (self.A, self.B):
            os.makedirs(os.path.join(self.lib, cid))
            open(os.path.join(self.lib, cid, "v.mp4"), "w").write("x")
        os.makedirs(os.path.join(self.view, "Cool Channel"))
        open(os.path.join(self.view, "Cool Channel", "tvshow.nfo"), "w").write(
            f'<tvshow><uniqueid type="youtube" default="true">{self.B}</uniqueid></tvshow>')
        self._status, settings.STATUS_PATH = settings.STATUS_PATH, os.path.join(self.root, "status.json")
        self.children = []

    def tearDown(self):
        for c in self.children:
            c.kill()
            c.wait()
        settings.STATUS_PATH = self._status
        shutil.rmtree(self.root)

    def child(self, path=None):
        code = "import sys,time\nf=open(sys.argv[1],'rb') if len(sys.argv)>1 else None\nprint('ready',flush=True)\ntime.sleep(30)"
        p = subprocess.Popen([sys.executable, "-c", code] + ([path] if path else []), stdout=subprocess.PIPE, text=True)
        p.stdout.readline()
        self.children.append(p)
        return p

    def test_the_channel_whose_video_is_open_is_found(self):
        self.assertEqual(self.service.open_channel(self.child(os.path.join(self.lib, self.B, "v.mp4")).pid, self.lib), self.B)
        self.assertIsNone(self.service.open_channel(self.child().pid, self.lib))           # nothing of the library open

    def test_channel_names_come_from_the_views_own_files(self):
        self.assertEqual(self.service.channel_names(self.view), {self.B: "Cool Channel"})

    def test_the_status_says_which_channel_and_how_far_through(self):
        self.service.SAMPLE_SECONDS = 0.05
        proc = self.child(os.path.join(self.lib, self.B, "v.mp4"))
        stop = __import__("threading").Event()
        th = __import__("threading").Thread(target=self.service.watch_progress, args=(proc, self.lib, self.view, stop), daemon=True)
        th.start()
        try:
            for _ in range(100):
                st = settings.read_status()
                if st.get("channel"):
                    break
                time.sleep(0.05)
            self.assertEqual((st["channel"], st["channel_pos"], st["channel_total"]), ("Cool Channel", 2, 2))
            self.assertAlmostEqual(st["channel_seen"], time.time(), delta=5)
        finally:
            stop.set()
            th.join(2)


if __name__ == "__main__":
    unittest.main()
