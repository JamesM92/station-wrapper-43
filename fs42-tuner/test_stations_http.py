"""The Stations and Schedule routes over HTTP, against the real tuner server and a stand-in console.

Run: python3 test_stations_http.py
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from test_files_api import Server, touch


class FakeConsoleServer:
    """Just enough of FieldStation42's console: stations, build tasks, and a schedule table it fills."""

    def __init__(self, db):
        self.stations, self.tasks, self.db = {}, {}, db
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                p = urlparse(self.path).path
                if p == "/stations":
                    return self._send({"count": len(outer.stations), "stations": [{"station_conf": c} for c in outer.stations.values()]})
                if p.startswith("/build/") and "/status/" in p:
                    return self._send({"status": "done", "log": ""})
                self._send({"detail": "Not Found"}, 404)

            def do_POST(self):
                p = unquote(urlparse(self.path).path)
                if p == "/stations":
                    conf = self._body()["station_conf"]
                    if conf["network_name"] in outer.stations:
                        return self._send({"detail": "already exists"}, 409)
                    outer.stations[conf["network_name"]] = conf
                    return self._send({"success": True}, 201)
                if p.startswith("/build/catalog/"):
                    outer.tasks["t"] = "x"
                    outer.clear(p.rsplit("/", 1)[1])
                    return self._send({"task_id": "t"})
                if p.startswith("/build/schedule/reset/"):
                    outer.clear(p.rsplit("/", 1)[1])
                    return self._send({"task_id": "t"})
                if p.startswith("/build/schedule/add_time/day/"):
                    outer.add_day(p.rsplit("/", 1)[1])
                    return self._send({"task_id": "t"})
                self._send({"detail": "Not Found"}, 404)

            def do_PUT(self):
                name = unquote(urlparse(self.path).path.rsplit("/", 1)[1])
                if name not in outer.stations:
                    return self._send({"detail": "no such station"}, 404)
                conf = self._body()["station_conf"]
                outer.stations.pop(name)
                outer.stations[conf["network_name"]] = conf             # a changed network_name is a rename
                self._send({"success": True})

            def do_DELETE(self):
                name = unquote(urlparse(self.path).path.rsplit("/", 1)[1])
                if outer.stations.pop(name, None) is None:
                    return self._send({"detail": "no such station"}, 404)
                self._send({"success": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def clear(self, name):
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM liquid_blocks WHERE station = ?", (name,))
        c.commit()
        c.close()

    def add_day(self, name, progress=None, on_wait=None):
        c = sqlite3.connect(self.db)
        last = c.execute("SELECT MAX(end_time) FROM liquid_blocks WHERE station = ?", (name,)).fetchone()[0]
        start = datetime.fromisoformat(last) if last else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for h in range(24):
            s = start + timedelta(hours=h)
            plan = json.dumps([{"path": f"catalog/by-genre/x/ep{h}.mp4", "skip": 0, "duration": 3000.0, "is_stream": False, "content_type": "feature"},
                               {"path": "bump/b.mp4", "skip": 0, "duration": 5.0, "is_stream": False, "content_type": "bump"},
                               {"path": "commercials/ad.mp4", "skip": 0, "duration": 30.0, "is_stream": False, "content_type": "commercial"}])
            c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,?)",
                      (name, s.isoformat(), (s + timedelta(hours=1)).isoformat(), f"Show {h}", plan))
        c.commit()
        c.close()

    def stop(self):
        self.httpd.shutdown()


class StationsHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.db = os.path.join(cls.root, "fluid.db")
        c = sqlite3.connect(cls.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        c.commit()
        c.close()
        for name in ("Cartoons", "bump"):
            touch(os.path.join(cls.root, "catalog", "by-genre", name, "a.mp4"))
        cls.console = FakeConsoleServer(cls.db)
        cls.srv = Server(cls.root, extra={"console_url": cls.console.base, "db_path": cls.db},
                         env={"FS42_SCHEDULES": os.path.join(cls.root, "schedules.json")})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.console.stop()
        shutil.rmtree(cls.root)

    def form(self, **kw):
        f = {"name": "Toons", "number": 12, "kind": "standard", "blocks": [{"from": 0, "to": 24, "lists": ["Cartoons"]}],
             "increment": 30, "bump": "bump", "commercials": ""}
        f.update(kw)
        return f

    def job(self, job_id):
        for _ in range(100):
            code, j = self.srv.get(f"/api/stations/job?id={job_id}")
            if j["state"] != "running":
                return j
            time.sleep(0.05)
        self.fail("the job did not finish")

    def test_make_build_look_at_the_schedule_edit_and_delete(self):
        code, out = self.srv.post("/api/stations/save", {"original": None, "form": self.form()})
        self.assertEqual((code, out["created"]), (200, True))
        self.assertEqual(self.console.stations["Toons"]["channel_number"], 12)
        code, listing = self.srv.get("/api/stations")
        row = listing["stations"][0]
        self.assertEqual((row["name"], row["number"], row["editable"], row["on_air"]), ("Toons", 12, True, False))
        code, job = self.srv.post("/api/stations/build", {"name": "Toons", "days": 2})
        self.assertEqual(code, 200)
        done = self.job(job["id"])
        self.assertEqual(done["state"], "done")
        row = self.srv.get("/api/stations")[1]["stations"][0]
        self.assertTrue(row["on_air"])
        today = datetime.now().strftime("%Y-%m-%d")
        code, sched = self.srv.get(f"/api/schedule?name=Toons&date={today}")
        self.assertEqual((code, len(sched["blocks"])), (200, 24))
        self.assertEqual(sched["blocks"][0]["title"], "Show 0")
        self.assertEqual(sum(1 for b in sched["blocks"] if b["now"]), 1)
        first = sched["blocks"][3]["start"]                                   # the files inside one block
        code, blk = self.srv.get(f"/api/schedule/block?name=Toons&start={first}")
        self.assertEqual((code, blk["title"], blk["total"]), (200, "Show 3", 3))
        self.assertEqual([(e["file"], e["type"]) for e in blk["entries"]], [("ep3.mp4", "feature"), ("b.mp4", "bump"), ("ad.mp4", "commercial")])
        self.assertEqual(self.srv.get(f"/api/schedule/block?name=Toons&start={first}&offset=2&limit=5")[1]["entries"][0]["file"], "ad.mp4")
        code, got = self.srv.get("/api/stations/get?name=Toons")
        self.assertEqual(got["form"]["blocks"], [{"from": 0, "to": 24, "lists": ["Cartoons"]}])
        code, out = self.srv.post("/api/stations/save", {"original": "Toons", "form": self.form(number=13)})
        self.assertEqual((code, out["created"]), (200, False))
        self.assertEqual(self.console.stations["Toons"]["channel_number"], 13)
        self.assertEqual(self.srv.post("/api/stations/delete", {"name": "Toons"})[0], 200)
        self.assertEqual(self.srv.get("/api/stations")[1]["stations"], [])

    def test_a_slot_whose_list_has_no_videos_yet_plays_the_test_pattern_and_the_station_still_opens(self):
        import schedules as sc
        os.makedirs(os.path.join(self.root, "catalog", "by-genre", "Kids - Morning"), exist_ok=True)       # a list made for a template, empty
        form = self.form(name="Templ", number=41, blocks=[{"from": 0, "to": 12, "lists": ["Cartoons"]},
                                                          {"from": 12, "to": 24, "lists": ["Kids - Morning"]}])
        form["schedule"] = sc.from_legacy(form)
        code, out = self.srv.post("/api/stations/save", {"original": None, "form": form})
        self.assertEqual(code, 200, out)
        self.assertTrue(any("no videos" in w for w in out["warnings"]))
        conf = self.console.stations["Templ"]
        hours = conf["day_templates"][conf["monday"]]
        self.assertIn("5", hours)
        self.assertNotIn("15", hours)                                          # the empty list's hours are unassigned
        got = self.srv.get("/api/stations/get?name=Templ")[1]
        self.assertIsNotNone(got["form"])                                      # and the form still holds it
        notes = self.srv.get("/api/schedule?name=Templ")[1]["notes"]            # and the Schedule page says why the hours are bare
        self.assertEqual(len(notes), 1)
        self.assertIn("12:00 to 24:00", notes[0])
        self.assertIn("Kids - Morning", notes[0])
        self.assertEqual(got["form"]["schedule"]["default"]["monday"]["videos"][1]["lists"], ["Kids - Morning"])
        touch(os.path.join(self.root, "catalog", "by-genre", "Kids - Morning", "m.mp4"))                      # it gets a video
        self.assertEqual(self.srv.post("/api/stations/save", {"original": "Templ", "form": got["form"]})[0], 200)
        conf = self.console.stations["Templ"]
        self.assertIn("15", conf["day_templates"][conf["monday"]])             # now it is in the schedule
        self.srv.post("/api/stations/delete", {"name": "Templ"})

    def test_empty_lists_that_no_station_uses_are_offered_for_cleaning_up(self):
        os.makedirs(os.path.join(self.root, "catalog", "by-genre", "Idle Template List"), exist_ok=True)
        got = self.srv.get("/api/files/sources-in-use")[1]
        self.assertIn("Idle Template List", got["empty_unused"])
        self.assertNotIn("Cartoons", got["empty_unused"])                                   # it has a video
        self.assertNotIn("bump", got["empty_unused"])                                       # the mid cards are never offered
        self.assertEqual(self.srv.post("/api/files/delete-tag", {"tag": "Idle Template List"})[0], 200)

    def test_sources_made_for_stations_say_which_stations_use_them(self):
        touch(os.path.join(self.root, "media", "tv", "Sitcom", "Season 1", "e1.mp4"))
        code, made = self.srv.post("/api/files/add-show", {"path": "tv/Sitcom"})
        self.assertEqual(code, 200)
        tag = made["tag"]
        for _ in range(100):                                   # it is linked in the background
            if not self.srv.get("/api/files/shows")[1]["linking"]:
                break
            time.sleep(0.05)
        rows = {r["tag"]: r for r in self.srv.get("/api/files/sources-in-use")[1]["sources"]}
        self.assertEqual(rows[tag]["used_by"], [])
        form = self.form(name="UsesIt", number=31, blocks=[{"from": 0, "to": 24, "lists": [tag]}])
        self.assertEqual(self.srv.post("/api/stations/save", {"original": None, "form": form})[0], 200)
        rows = {r["tag"]: r for r in self.srv.get("/api/files/sources-in-use")[1]["sources"]}
        self.assertEqual(rows[tag]["used_by"], ["UsesIt"])
        self.srv.post("/api/stations/delete", {"name": "UsesIt"})

    def test_one_click_makes_a_channel_from_the_biggest_list_and_builds_it(self):
        touch(os.path.join(self.root, "catalog", "by-genre", "Big", "a.mp4"))
        touch(os.path.join(self.root, "catalog", "by-genre", "Big", "b.mp4"))
        code, out = self.srv.post("/api/stations/quickstart", {})
        self.assertEqual(code, 200)
        self.assertEqual((out["name"], out["source"]), ("My Channel", "Big"))
        self.assertGreaterEqual(out["number"], 3)
        conf = self.console.stations["My Channel"]
        self.assertEqual(conf["channel_number"], out["number"])
        self.assertIs(conf["commercial_free"], False)                                  # commercials off: no breaks
        self.assertEqual(conf["commercial_dir"], ".commercials.off")
        self.assertEqual(self.job(out["job"]["id"])["state"], "done")
        again = self.srv.post("/api/stations/quickstart", {})[1]
        self.assertEqual((again["name"], again["number"] != out["number"]), ("My Channel 2", True))   # the next one never clashes
        for n in ("My Channel", "My Channel 2"):
            self.srv.post("/api/stations/delete", {"name": n})

    def test_one_click_with_nothing_to_play_says_so(self):
        root = tempfile.mkdtemp()
        db = os.path.join(root, "f.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        c.commit()
        c.close()
        console = FakeConsoleServer(db)
        srv = Server(root, extra={"console_url": console.base, "db_path": db})
        try:
            os.makedirs(os.path.join(root, "catalog", "by-genre"), exist_ok=True)
            code, out = srv.post("/api/stations/quickstart", {})
            self.assertEqual(code, 400)
            self.assertIn("nothing to make a channel from", out["error"])
        finally:
            srv.stop()
            console.stop()
            shutil.rmtree(root)

    def test_renaming_clears_the_old_schedule_and_builds_it_again_under_the_new_name(self):
        self.srv.post("/api/stations/save", {"original": None, "form": self.form(name="Before", number=31)})
        self.job(self.srv.post("/api/stations/build", {"name": "Before", "days": 1})[1]["id"])
        self.assertTrue(self.srv.get("/api/stations")[1]["stations"][-1]["on_air"])
        code, out = self.srv.post("/api/stations/rename", {"name": "Before", "to": "After"})
        self.assertEqual((code, out["name"], out["old"]), (200, "After", "Before"))
        self.assertEqual(self.job(out["job"]["id"])["state"], "done")
        names = [s["name"] for s in self.srv.get("/api/stations")[1]["stations"]]
        self.assertIn("After", names)
        self.assertNotIn("Before", names)
        c = sqlite3.connect(self.db)
        left = c.execute("SELECT COUNT(*) FROM liquid_blocks WHERE station = 'Before'").fetchone()[0]
        c.close()
        self.assertEqual(left, 0)                                           # nothing orphaned under the old name
        self.assertTrue([s for s in self.srv.get("/api/stations")[1]["stations"] if s["name"] == "After"][0]["on_air"])
        self.srv.post("/api/stations/delete", {"name": "After"})

    def test_rename_errors(self):
        self.srv.post("/api/stations/save", {"original": None, "form": self.form(name="Solo", number=32)})
        self.assertEqual(self.srv.post("/api/stations/rename", {"name": "Ghost", "to": "x"})[0], 404)
        for bad in ("", "a/b", "Solo"):
            self.assertEqual(self.srv.post("/api/stations/rename", {"name": "Solo", "to": bad})[0], 400, repr(bad))
        self.assertEqual(self.srv.post("/api/stations/rename", {"name": "Solo"})[0], 400)
        self.srv.post("/api/stations/delete", {"name": "Solo"})

    def test_errors_have_the_right_status_and_a_message(self):
        code, out = self.srv.post("/api/stations/save", {"original": None, "form": self.form(name="")})
        self.assertEqual(code, 400)
        self.assertIn("name", out["error"])
        code, out = self.srv.post("/api/stations/save", {"original": None, "form": self.form(blocks=[{"from": 0, "to": 24, "lists": ["Nope"]}])})
        self.assertEqual(code, 400)
        self.assertIn("'Nope'", out["error"])
        self.assertEqual(self.srv.get("/api/stations/get?name=Ghost")[0], 404)
        self.assertEqual(self.srv.post("/api/stations/delete", {"name": "Ghost"})[0], 404)
        self.assertEqual(self.srv.post("/api/stations/build", {"name": "Ghost", "days": 1})[0], 404)
        self.assertEqual(self.srv.post("/api/stations/build", {"name": "Ghost", "days": "x"})[0], 400)
        self.assertEqual(self.srv.get("/api/schedule?name=Ghost")[0], 404)
        self.assertEqual(self.srv.get("/api/schedule?name=Ghost&date=junk")[0], 400)
        self.assertEqual(self.srv.get("/api/schedule/block?name=Ghost&start=2026-10-05T10:00:00")[0], 404)
        self.assertEqual(self.srv.get("/api/schedule/block?name=Ghost&start=junk")[0], 400)
        self.assertEqual(self.srv.get("/api/schedule/block?name=Ghost&start=2026-10-05T10:00:00&offset=x")[0], 400)
        self.assertEqual(self.srv.get("/api/stations/job?id=nope")[0], 404)
        self.assertEqual(self.srv.get("/api/stations/nothing")[0], 404)

    def test_only_json_posts_are_taken(self):
        self.assertEqual(self.srv.post("/api/stations/save", None, ctype="text/plain", raw=b"{}")[0], 415)


class ConsoleDownTest(unittest.TestCase):
    def test_a_console_that_is_not_there_is_a_clear_502_not_a_crash(self):
        root = tempfile.mkdtemp()
        try:
            touch(os.path.join(root, "catalog", "by-genre", "x", "a.mp4"))
            srv = Server(root, extra={"console_url": "http://127.0.0.1:9"})        # nothing listens on port 9
            try:
                code, out = srv.get("/api/stations")
                self.assertEqual(code, 502)
                self.assertIn("not reachable", out["error"])
                self.assertEqual(srv.get("/api/files/info")[0], 200)               # the rest of the page server is fine
            finally:
                srv.stop()
        finally:
            shutil.rmtree(root)


class SwitchedOffTest(unittest.TestCase):
    def test_changes_are_refused_when_editing_is_off_but_looking_still_works(self):
        root = tempfile.mkdtemp()
        try:
            db = os.path.join(root, "f.db")
            sqlite3.connect(db).execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)").connection.commit()
            console = FakeConsoleServer(db)
            touch(os.path.join(root, "catalog", "by-genre", "x", "a.mp4"))
            srv = Server(root, env={"DASHBOARD_EDIT": "0"}, extra={"console_url": console.base, "db_path": db})
            try:
                self.assertEqual(srv.get("/api/stations")[0], 200)
                self.assertEqual(srv.post("/api/stations/save", {"form": {}})[0], 403)
                self.assertEqual(srv.post("/api/stations/delete", {"name": "x"})[0], 403)
                self.assertEqual(srv.post("/api/stations/rename", {"name": "x", "to": "y"})[0], 403)
                self.assertEqual(srv.post("/api/stations/build", {"name": "x"})[0], 403)
            finally:
                srv.stop()
                console.stop()
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
