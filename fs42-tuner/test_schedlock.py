"""Tests for schedlock.py: one schedule change at a time, across processes. Run: python3 test_schedlock.py"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import console_client
import schedlock


class LockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "lock")
        self.saved = schedlock.PATH
        schedlock.PATH = self.path

    def tearDown(self):
        schedlock.PATH = self.saved

    def test_threads_take_turns(self):
        inside, worst = [0], [0]

        def work():
            with schedlock.held():
                inside[0] += 1
                worst[0] = max(worst[0], inside[0])
                time.sleep(0.05)
                inside[0] -= 1
        threads = [threading.Thread(target=work) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(worst[0], 1)                       # never two at once

    def holder(self, seconds):
        code = ("import os,sys,time; os.environ['FS42_SCHEDULE_LOCK']=%r; sys.path.insert(0,%r); import schedlock\n"
                "with schedlock.held():\n print('held', flush=True); time.sleep(%s)\n" % (self.path, os.path.dirname(os.path.abspath(__file__)), seconds))
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_another_process_waits_until_the_holder_is_done(self):
        p = self.holder(1.0)
        t0 = time.time()
        with schedlock.held(timeout=10):
            waited = time.time() - t0
        p.wait()
        self.assertGreater(waited, 0.5)                     # it really waited for the other process

    def test_a_crashed_holder_does_not_leave_it_stuck(self):
        p = self.holder(60)
        p.send_signal(signal.SIGKILL)
        p.wait()
        t0 = time.time()
        with schedlock.held(timeout=5):
            pass
        self.assertLess(time.time() - t0, 2)

    def test_it_gives_up_after_the_timeout_with_a_clear_error(self):
        p = self.holder(5)
        try:
            with self.assertRaises(schedlock.LockTimeout) as cm:
                with schedlock.held(timeout=0.5):
                    pass
            self.assertIn("too long", str(cm.exception))
        finally:
            p.kill()
            p.wait()


class ClientHoldsItTest(unittest.TestCase):
    """Two clients extending at once must not have their build windows overlap."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = schedlock.PATH
        schedlock.PATH = os.path.join(self.dir, "lock")

    def tearDown(self):
        schedlock.PATH = self.saved

    def test_builds_do_not_overlap(self):
        active, worst, lock = [0], [0], threading.Lock()

        def make():
            c = console_client.Console("http://x", sleep=lambda s: time.sleep(0.01))
            state = {"polls": 0}

            def fake(method, path, body=None, timeout=60):
                if method == "POST":
                    with lock:
                        active[0] += 1
                        worst[0] = max(worst[0], active[0])
                    state["polls"] = 0
                    return {"task_id": "t"}
                state["polls"] += 1
                if state["polls"] >= 3:
                    with lock:
                        active[0] -= 1
                    return {"status": "done"}
                return {"status": "running"}
            c._call = fake
            return c
        clients = [make() for _ in range(4)]
        threads = [threading.Thread(target=lambda c=c: [c.add_day("S") for _ in range(2)]) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(worst[0], 1)                       # a new build never started before the last one finished

    def test_a_lock_that_never_frees_is_a_clear_console_error(self):
        saved, schedlock.held = schedlock.held, lambda **kw: (_ for _ in ()).throw(schedlock.LockTimeout("too long"))
        try:
            c = console_client.Console("http://x")
            from contextlib import contextmanager

            @contextmanager
            def boom(**kw):
                raise schedlock.LockTimeout("another schedule change has been running for too long")
                yield
            schedlock.held = boom
            with self.assertRaises(console_client.ConsoleError) as cm:
                c.add_day("S")
            self.assertIn("too long", str(cm.exception))
        finally:
            schedlock.held = saved


if __name__ == "__main__":
    unittest.main()
