"""Tests for supervisor.py, using real child processes.

Run: python3 test_supervisor.py   (from this folder)
"""
import io
import os
import sys
import tempfile
import threading
import time
import unittest

import supervisor as sv

PY = sys.executable


def svc(name, code, core=False, env=None):
    return sv.Service(name, [PY, "-u", "-c", code], core=core, env=env)


class Run:
    """A supervisor running in a thread, so a test can watch it and then stop it."""

    def __init__(self, services, **kw):
        self.out = io.StringIO()
        kw.setdefault("backoff_base", 0.05)
        kw.setdefault("backoff_max", 0.4)
        kw.setdefault("grace", 2.0)
        kw.setdefault("poll", 0.02)
        self.sup = sv.Supervisor(services, out=self.out, **kw)
        self.code = None
        self.thread = threading.Thread(target=lambda: setattr(self, "code", self.sup.run()))
        self.thread.start()

    def text(self):
        return self.out.getvalue()

    def wait_for(self, needle, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.text():
                return True
            time.sleep(0.02)
        return False

    def stop(self, timeout=10):
        self.sup.stop()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the supervisor did not stop"
        return self.code


FOREVER = "import time\nwhile True: time.sleep(1)"


class OutputTest(unittest.TestCase):
    def test_each_line_is_prefixed_with_its_service(self):
        r = Run([svc("console", "print('hello'); print('world'); import time; time.sleep(30)", core=True)])
        self.assertTrue(r.wait_for("[console] world"))
        r.stop()
        self.assertIn("[console] hello", r.text())
        self.assertIn("[supervisor] started console", r.text())

    def test_stderr_is_captured_too(self):
        r = Run([svc("tuner", "import sys,time; sys.stderr.write('oops\\n'); sys.stderr.flush(); time.sleep(30)", core=True)])
        self.assertTrue(r.wait_for("[tuner] oops"))
        r.stop()


class RestartTest(unittest.TestCase):
    def test_a_service_that_crashes_is_restarted(self):
        with tempfile.TemporaryDirectory() as d:
            counter = os.path.join(d, "n")
            code = (f"import os,sys,time\nn=int(open({counter!r}).read()) if os.path.exists({counter!r}) else 0\n"
                    f"open({counter!r},'w').write(str(n+1))\nprint('start', n+1, flush=True)\n"
                    f"sys.exit(1) if n < 2 else time.sleep(30)")
            r = Run([svc("console", code, core=True)])
            self.assertTrue(r.wait_for("[console] start 3"), r.text())
            r.stop()
            self.assertIn("exited with status 1", r.text())

    def test_the_wait_grows_after_each_quick_failure_and_is_capped(self):
        r = Run([svc("console", "import sys; sys.exit(3)", core=True)], backoff_base=0.05, backoff_max=0.2)
        end = time.time() + 8
        while r.text().count("will restart in") < 5 and time.time() < end:
            time.sleep(0.02)
        r.stop()
        delays = [float(line.split("restart in ")[1].rstrip("s")) for line in r.text().splitlines() if "restart in" in line]
        self.assertEqual(delays[:5], [0.05, 0.1, 0.2, 0.2, 0.2])      # doubling, then capped

    def test_a_service_that_ran_a_while_starts_its_backoff_over(self):
        # Each run lasts 0.4s, longer than stable_after, so no failure counts as "quick":
        # the wait stays at the base instead of doubling.
        r = Run([svc("console", "import time,sys; time.sleep(0.4); sys.exit(1)", core=True)],
                backoff_base=0.05, stable_after=0.2)
        end = time.time() + 8
        while r.text().count("will restart in") < 3 and time.time() < end:
            time.sleep(0.02)
        r.stop()
        delays = [line.split("restart in ")[1] for line in r.text().splitlines() if "restart in" in line]
        self.assertGreaterEqual(len(delays), 3)
        self.assertEqual(set(delays), {"0.05s"})

    def test_a_core_service_that_exits_cleanly_is_still_restarted(self):
        r = Run([svc("console", "print('ran')", core=True)])
        self.assertTrue(r.wait_for("will restart"))
        r.stop()

    def test_a_missing_program_is_retried_not_fatal(self):
        r = Run([sv.Service("sync", ["/no/such/program"])])
        self.assertTrue(r.wait_for("could not start sync"))
        self.assertTrue(r.wait_for("will restart"))
        r.stop()


class RestartRequestTest(unittest.TestCase):
    """SIGUSR1 restarts the tuner so it re-reads its config, without touching the console."""

    def test_a_requested_restart_is_immediate_even_after_failures(self):
        r = Run([svc("console", FOREVER, core=True),
                 svc("tuner", "import time; print('tuner up', flush=True); time.sleep(60)", core=True)],
                backoff_base=5.0)                               # a long wait that must NOT apply
        self.assertTrue(r.wait_for("[tuner] tuner up"))
        self.assertTrue(r.sup.request_restart("tuner"))
        end = time.time() + 5
        while r.text().count("[tuner] tuner up") < 2 and time.time() < end:
            time.sleep(0.02)
        r.stop()
        self.assertEqual(r.text().count("[tuner] tuner up"), 2)
        self.assertIn("stopped for a restart you asked for", r.text())
        self.assertNotIn("tuner will restart in 5", r.text())

    def test_the_console_is_not_touched_by_a_tuner_restart(self):
        r = Run([svc("console", "print('console up', flush=True)\nimport time\ntime.sleep(60)", core=True),
                 svc("tuner", FOREVER, core=True)])
        self.assertTrue(r.wait_for("[console] console up"))
        r.sup.request_restart("tuner")
        time.sleep(0.6)
        r.stop()
        self.assertEqual(r.text().count("started console"), 1)
        self.assertEqual(r.text().count("[console] console up"), 1)
        self.assertEqual(r.text().count("started tuner"), 2)

    def test_asking_for_a_service_that_is_not_running_does_nothing(self):
        r = Run([svc("console", FOREVER, core=True)])
        r.wait_for("started console")
        self.assertFalse(r.sup.request_restart("tuner"))
        self.assertFalse(r.sup.request_restart("nope"))
        r.stop()


class RefusalTest(unittest.TestCase):
    """Exit 78 means 'not here' (for example, the catalog is on SMB). Never retried."""

    def test_an_essential_service_refusing_stops_the_container_with_78(self):
        r = Run([svc("console", "import sys; print('on an SMB share'); sys.exit(78)", core=True),
                 svc("tuner", FOREVER, core=True)])
        r.thread.join(10)
        self.assertFalse(r.thread.is_alive(), "the container did not stop")
        self.assertEqual(r.code, 78)
        self.assertIn("[console] on an SMB share", r.text())
        self.assertIn("is essential, so the container is stopping", r.text())
        self.assertNotIn("console will restart", r.text())

    def test_an_optional_service_refusing_stops_only_itself(self):
        r = Run([svc("console", FOREVER, core=True), svc("sync", "import sys; sys.exit(78)")])
        self.assertTrue(r.wait_for("sync refused to run"))
        time.sleep(0.5)
        self.assertTrue(r.thread.is_alive())                         # the rest carries on
        self.assertEqual(r.stop(), 0)
        self.assertNotIn("sync will restart", r.text())

    def test_an_optional_service_that_finishes_is_left_finished(self):
        r = Run([svc("console", FOREVER, core=True), svc("ta-nfo", "print('done')")])
        self.assertTrue(r.wait_for("ta-nfo finished"))
        time.sleep(0.4)
        r.stop()
        self.assertEqual(r.text().count("started ta-nfo"), 1)


class ShutdownTest(unittest.TestCase):
    def test_stopping_ends_every_child_and_exits_zero(self):
        r = Run([svc("console", FOREVER, core=True), svc("tuner", FOREVER, core=True)])
        r.wait_for("started tuner")
        pids = [s.proc.pid for s in r.sup.services]
        self.assertEqual(r.stop(), 0)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        stubborn = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\nwhile True: time.sleep(1)"
        r = Run([svc("tuner", stubborn, core=True)], grace=0.5)
        self.assertTrue(r.wait_for("[tuner] ready"))
        pid = r.sup.services[0].proc.pid
        started = time.time()
        self.assertEqual(r.stop(), 0)
        self.assertLess(time.time() - started, 5)
        self.assertIn("did not stop", r.text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class StationWatchTest(unittest.TestCase):
    """New stations made in FieldStation42's console reach the dashboard: the tuner is restarted."""

    def test_a_new_station_restarts_the_tuner_and_the_console_is_untouched(self):
        seen = {"801": "Comedy"}
        r = Run([svc("console", FOREVER, core=True), svc("tuner", FOREVER, core=True)],
                watch_stations=lambda: dict(seen), watch_every=0.05)
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.3)
            self.assertNotIn("stations changed", r.text())            # the first look is the baseline
            seen["802"] = "NBC News"
            self.assertTrue(r.wait_for("stations changed (+802 NBC News)"))
            self.assertTrue(r.wait_for("tuner stopped for a restart you asked for"))
            end = time.time() + 8
            while r.text().count("started tuner") < 2 and time.time() < end:
                time.sleep(0.02)
            self.assertEqual(r.text().count("started tuner"), 2)       # the tuner came back
            self.assertEqual(r.text().count("started console"), 1)     # the console was never touched
        finally:
            r.stop()

    def test_a_removed_station_does_too_and_no_change_does_nothing(self):
        seen = {"801": "Comedy", "802": "Drama"}
        r = Run([svc("tuner", FOREVER, core=True)], watch_stations=lambda: dict(seen), watch_every=0.05)
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.4)
            self.assertEqual(r.text().count("started tuner"), 1)
            del seen["802"]
            self.assertTrue(r.wait_for("stations changed (-802 Drama)"))
        finally:
            r.stop()

    def test_an_unreadable_config_is_reported_and_does_not_stop_anything(self):
        calls = []

        def boom():
            calls.append(1)
            raise ValueError("half written")
        r = Run([svc("tuner", FOREVER, core=True)], watch_stations=boom, watch_every=0.05)
        try:
            self.assertTrue(r.wait_for("could not read the station configs: half written"))
            self.assertTrue(r.thread.is_alive())
        finally:
            r.stop()


class BuildWaitTest(unittest.TestCase):
    """A new station restarts the tuner, but never while a build is running: that would cut the build off."""

    def test_the_restart_waits_until_the_build_is_done(self):
        seen = {"801": "Comedy"}
        building = [True]
        r = Run([svc("console", FOREVER, core=True), svc("tuner", FOREVER, core=True)],
                watch_stations=lambda: dict(seen), watch_every=0.05, busy=lambda: building[0])
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.3)
            seen["802"] = "Weekly"
            self.assertTrue(r.wait_for("a build is running"))
            time.sleep(0.6)
            self.assertEqual(r.text().count("started tuner"), 1)            # still waiting: nothing was restarted
            self.assertEqual(r.text().count("a build is running"), 1)       # and it only says so once
            building[0] = False
            self.assertTrue(r.wait_for("stations changed (+802 Weekly)"))
            end = time.time() + 8
            while r.text().count("started tuner") < 2 and time.time() < end:
                time.sleep(0.02)
            self.assertEqual(r.text().count("started tuner"), 2)            # restarted once the build ended
        finally:
            r.stop()

    def test_without_a_build_it_restarts_at_once(self):
        seen = {"801": "Comedy"}
        r = Run([svc("tuner", FOREVER, core=True)], watch_stations=lambda: dict(seen), watch_every=0.05, busy=lambda: False)
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.2)
            seen["802"] = "X"
            self.assertTrue(r.wait_for("stations changed (+802 X)"))
            self.assertNotIn("a build is running", r.text())
        finally:
            r.stop()


class SettleTest(unittest.TestCase):
    """A change is acted on only after it has stood for a moment: the dashboard starts the new station's build right
    after saving it, and a restart in between would lose that build."""

    def test_a_change_that_is_followed_by_a_build_never_restarts_the_tuner_in_between(self):
        seen = {"801": "Comedy"}
        building = [False]
        r = Run([svc("tuner", FOREVER, core=True)], watch_stations=lambda: dict(seen), watch_every=0.05,
                busy=lambda: building[0], settle=0.6)
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.2)
            seen["802"] = "Weekly"                                           # the station is saved ...
            time.sleep(0.25)
            building[0] = True                                               # ... and its build starts a moment later
            time.sleep(1.0)
            self.assertEqual(r.text().count("started tuner"), 1)             # never restarted: the build was not cut off
            self.assertIn("a build is running", r.text())
        finally:
            r.stop()

    def test_a_change_that_stands_is_acted_on_after_the_wait(self):
        seen = {"801": "Comedy"}
        r = Run([svc("tuner", FOREVER, core=True)], watch_stations=lambda: dict(seen), watch_every=0.05,
                busy=lambda: False, settle=0.5)
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.2)
            began = time.time()
            seen["802"] = "X"
            self.assertTrue(r.wait_for("stations changed (+802 X)"))
            self.assertGreaterEqual(time.time() - began, 0.45)               # it waited
        finally:
            r.stop()

    def test_a_change_that_keeps_changing_waits_for_it_to_stop(self):
        seen = {"801": "Comedy"}
        r = Run([svc("tuner", FOREVER, core=True)], watch_stations=lambda: dict(seen), watch_every=0.05,
                busy=lambda: False, settle=0.6)
        try:
            self.assertTrue(r.wait_for("started tuner"))
            time.sleep(0.2)
            seen["802"] = "X"
            time.sleep(0.4)
            seen["803"] = "Y"                                                # another one before the first settled
            time.sleep(0.3)
            self.assertNotIn("stations changed", r.text())                   # the wait started over
            self.assertTrue(r.wait_for("stations changed"))
            self.assertIn("+802 X", r.text())
            self.assertIn("+803 Y", r.text())                                # both in one restart
        finally:
            r.stop()


class ConfigTest(unittest.TestCase):
    def names(self, env, exists):
        return [s.name for s in sv.services_from_env(env, exists=lambda p: exists)]

    FARM_OFF = {"FARM_ENABLED": "0", "FIRSTRUN_TEST_STATION": "0", "FIRSTRUN_GUIDE_STATION": "0"}       # both default-on jobs off, to test the rest

    def test_the_console_always_runs(self):
        self.assertEqual(self.names(self.FARM_OFF, False), ["console", "ta-nfo"])

    def test_the_tuner_runs_when_it_has_a_config(self):
        self.assertEqual(self.names(self.FARM_OFF, True), ["console", "tuner", "ta-nfo"])
        tuner = sv.services_from_env({"FS42_TUNER_CONF": "/x/c.json"}, exists=lambda p: p == "/x/c.json")[1]
        self.assertEqual(tuner.env, {"FS42_TUNER_CONF": "/x/c.json"})
        self.assertTrue(tuner.core)

    def test_the_farm_runs_by_default_and_can_be_switched_off(self):
        self.assertEqual(self.names({}, True), ["console", "tuner", "firstrun", "farm", "ta-nfo"])
        for off in ("0", "no", "false", "OFF"):
            self.assertNotIn("farm", self.names({"FARM_ENABLED": off}, True), off)
        for on in ("", "1", "yes"):
            self.assertIn("farm", self.names({"FARM_ENABLED": on}, True), on)

    def test_the_test_station_job_runs_by_default_once_and_can_be_switched_off(self):
        self.assertIn("firstrun", self.names({}, True))
        for off in ("0", "no", "false", "OFF"):
            self.assertNotIn("firstrun", self.names({"FIRSTRUN_TEST_STATION": off, "FIRSTRUN_GUIDE_STATION": off}, True), off)
            self.assertIn("firstrun", self.names({"FIRSTRUN_TEST_STATION": off}, True), off)         # the guide still needs it
            self.assertIn("firstrun", self.names({"FIRSTRUN_GUIDE_STATION": off}, True), off)
        job = [s for s in sv.services_from_env({}, exists=lambda p: True) if s.name == "firstrun"][0]
        self.assertFalse(job.core)                       # never essential: it finishes, and a failure must not stop the container
        self.assertIn("/opt/wrapper/firstrun.py", job.argv)

    def test_the_tubearchivist_job_always_runs_and_the_dashboard_switches_it_on(self):
        # TA_ENABLED is only a starting value now: the job idles until the dashboard (or the variable) enables it
        for env in ({}, {"TA_ENABLED": "1"}, {"TA_ENABLED": "0"}):
            self.assertIn("ta-nfo", self.names({**self.FARM_OFF, **env}, True), str(env))
        ta = [s for s in sv.services_from_env({}, exists=lambda p: True) if s.name == "ta-nfo"][0]
        self.assertFalse(ta.core)
        self.assertIn("/opt/ta-nfo/service.py", ta.argv)

    def test_sync_is_switched_on_by_the_environment(self):
        off = self.FARM_OFF
        self.assertEqual(self.names({**off, "SYNC_ENABLED": "1"}, True), ["console", "tuner", "sync", "ta-nfo"])
        self.assertEqual(self.names({**off, "SYNC_ENABLED": "YES"}, True), ["console", "tuner", "sync", "ta-nfo"])
        for v in ("", "0", "no", "false"):
            self.assertEqual(self.names({**off, "SYNC_ENABLED": v}, True), ["console", "tuner", "ta-nfo"], v)

    def test_the_log_says_what_is_off_and_why(self):
        notes = " ".join(sv.notes_from_env({}, exists=lambda p: False))
        self.assertIn("no config.json", notes)
        self.assertIn("SYNC_ENABLED=1", notes)
        self.assertNotIn("TA_ENABLED", notes)                    # the conversion is switched on in the dashboard
        self.assertNotIn("farm is off", notes)
        self.assertIn("farm is off", " ".join(sv.notes_from_env(self.FARM_OFF, exists=lambda p: True)))
        self.assertEqual(sv.notes_from_env({"SYNC_ENABLED": "1"}, exists=lambda p: True), [])

    def test_only_the_console_and_tuner_are_essential(self):
        s = {x.name: x.core for x in sv.services_from_env({"SYNC_ENABLED": "1"}, exists=lambda p: True)}
        self.assertEqual(s, {"console": True, "tuner": True, "firstrun": False, "farm": False, "sync": False,
                             "ta-nfo": False})


if __name__ == "__main__":
    unittest.main()
