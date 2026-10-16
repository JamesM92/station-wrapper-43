"""The schedule keeper running inside the real dashboard server, and its settings API.

Run: python3 test_keeper_api.py
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime

from test_files_api import Server, touch
from test_stations_http import FakeConsoleServer


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
    c.commit()
    c.close()


class KeeperHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.db = os.path.join(cls.root, "f.db")
        make_db(cls.db)
        cls.console = FakeConsoleServer(cls.db)
        for name, n in (("Low", 1), ("Full", 9)):          # Low has ~1 day of schedule, Full has plenty
            cls.console.stations[name] = {"network_name": name, "channel_number": len(cls.console.stations) + 1, "network_type": "standard"}
            for _ in range(n):
                cls.console.add_day(name)
        cls.console.stations["Never"] = {"network_name": "Never", "channel_number": 9, "network_type": "standard"}   # no schedule
        touch(os.path.join(cls.root, "catalog", "by-genre", "x", "a.mp4"))
        cls.settings = os.path.join(cls.root, "settings.json")
        cls.srv = Server(cls.root, env={"FS42_SETTINGS": cls.settings, "KEEPER_FIRST_DELAY_SECONDS": "0",
                                        "KEEPER_INTERVAL_MINUTES": "1",
                                        "KEEPER_TOPUP_HOUR": "0"},         # not held back until the morning, whatever the time is
                         extra={"console_url": cls.console.base, "db_path": cls.db})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.console.stop()
        shutil.rmtree(cls.root)

    def coverage_days(self, name):
        c = sqlite3.connect(self.db)
        end = c.execute("SELECT MAX(end_time) FROM liquid_blocks WHERE station = ?", (name,)).fetchone()[0]
        c.close()
        return (datetime.fromisoformat(end) - datetime.now()).total_seconds() / 86400 if end else None

    def test_it_tops_up_the_station_that_is_running_low_and_leaves_the_others(self):
        end = time.time() + 30
        while time.time() < end and (self.coverage_days("Low") or 0) < 6.5:
            time.sleep(0.3)
        self.assertGreaterEqual(self.coverage_days("Low"), 6.5)             # topped up to about a week
        self.assertLess(self.coverage_days("Low"), 10)                      # and not beyond what it needed (a week plus the hidden day)
        self.assertAlmostEqual(self.coverage_days("Full"), 8.4, delta=1.2)  # untouched (it already had 9 days from midnight)
        self.assertIsNone(self.coverage_days("Never"))                      # never started for you
        status = self.srv.get("/api/keeper")[1]["status"]
        self.assertTrue(status["last_pass"])
        self.assertIn("Low", [a["station"] for a in status["last_action"]["added"]])
        self.assertNotIn("Full", [a["station"] for a in status["last_action"]["added"]])
        self.assertIsNone(status["error"])


class SettingsApiTest(unittest.TestCase):
    def test_read_change_and_refuse(self):
        root = tempfile.mkdtemp()
        try:
            db = os.path.join(root, "f.db")
            make_db(db)
            console = FakeConsoleServer(db)
            touch(os.path.join(root, "catalog", "by-genre", "x", "a.mp4"))
            path = os.path.join(root, "s.json")
            srv = Server(root, env={"FS42_SETTINGS": path, "KEEPER_FIRST_DELAY_SECONDS": "3600"},
                         extra={"console_url": console.base, "db_path": db})
            try:
                code, out = srv.get("/api/keeper")
                self.assertEqual((code, out["settings"]["enabled"], out["settings"]["days_ahead"]), (200, True, 7))
                code, out = srv.post("/api/keeper/settings", {"enabled": False, "days_ahead": 14})
                self.assertEqual((code, out["settings"]["enabled"], out["settings"]["days_ahead"]), (200, False, 14))
                self.assertEqual(json.load(open(path))["keeper"], {"enabled": False, "days_ahead": 14})
                self.assertFalse(srv.get("/api/keeper")[1]["settings"]["enabled"])
                for bad in ({"days_ahead": 0}, {"days_ahead": 99}, {"enabled": "yes"}):
                    self.assertEqual(srv.post("/api/keeper/settings", bad)[0], 400, str(bad))
                self.assertEqual(srv.post("/api/keeper/settings", None, ctype="text/plain", raw=b"{}")[0], 415)
            finally:
                srv.stop()
                console.stop()
        finally:
            shutil.rmtree(root)

    def test_editing_off_blocks_the_change_but_not_the_look(self):
        root = tempfile.mkdtemp()
        try:
            db = os.path.join(root, "f.db")
            make_db(db)
            console = FakeConsoleServer(db)
            touch(os.path.join(root, "catalog", "by-genre", "x", "a.mp4"))
            srv = Server(root, env={"DASHBOARD_EDIT": "0", "FS42_SETTINGS": os.path.join(root, "s.json"),
                                    "KEEPER_FIRST_DELAY_SECONDS": "3600"},
                         extra={"console_url": console.base, "db_path": db})
            try:
                self.assertEqual(srv.get("/api/keeper")[0], 200)
                self.assertEqual(srv.post("/api/keeper/settings", {"enabled": False})[0], 403)
                self.assertFalse(os.path.exists(os.path.join(root, "s.json")))
            finally:
                srv.stop()
                console.stop()
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
