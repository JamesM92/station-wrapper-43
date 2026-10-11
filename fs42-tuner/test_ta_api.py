"""The TubeArchivist panel's API, against the real server. Run: python3 test_ta_api.py"""
import json
import os
import shutil
import tempfile
import time
import unittest

from test_files_api import Server, touch

UC = "UC" + "a" * 22


class TaApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.settings = os.path.join(cls.root, "config", "settings.json")
        cls.status = os.path.join(cls.root, "status.json")
        cls.run_now = os.path.join(cls.root, "ta.now")
        os.environ.pop("FS42_TA_NOW", None)
        touch(os.path.join(cls.root, "media", "youtube", UC, "vid.mp4"))
        touch(os.path.join(cls.root, "media", "youtube", "UC" + "b" * 22, "vid.mp4"))
        touch(os.path.join(cls.root, "media", "movies", "Film", "Film.mkv"))
        for view in ("tubearchivist-nfo", "youtube-nfo"):          # whichever library the settings name at that point
            touch(os.path.join(cls.root, "catalog", "tanfo", view, "Playlists", "Picks.m3u8"),
                  "#EXTM3U\n#PLAYLIST:Picks\n#EXTINF:-1,One\n../Chan/a.mp4\n#EXTINF:-1,Two\n../Chan/b.mp4\n")
            touch(os.path.join(cls.root, "catalog", "tanfo", view, "Playlists", "Empty.m3u8"), "#EXTM3U\n")
        os.makedirs(os.path.join(cls.root, "catalog", "by-genre"))
        cls.srv = Server(cls.root, env={"FS42_SETTINGS": cls.settings, "FS42_TA_STATUS": cls.status,
                                        "FS42_TA_NOW": cls.run_now, "FS42_FARM_NOW": os.path.join(cls.root, "farm.now")})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.root)

    def test_it_finds_the_library_by_how_tubearchivist_lays_it_out(self):
        code, out = self.srv.get("/api/ta/detect")
        self.assertEqual(code, 200)
        self.assertEqual(out["libraries"], [{"path": os.path.join(self.root, "media", "youtube"), "channels": 2}])

    def test_status_starts_off_and_saving_turns_it_on(self):
        code, st = self.srv.get("/api/ta/status")
        self.assertEqual((code, st["settings"]["enabled"]), (200, False))
        lib = os.path.join(self.root, "media", "youtube")
        code, out = self.srv.post("/api/ta/settings", {"enabled": True, "library": lib, "interval_minutes": 60})
        self.assertEqual(code, 200)
        saved = json.load(open(self.settings))["ta"]
        self.assertEqual({k: saved[k] for k in ("enabled", "libraries", "interval_minutes")},
                         {"enabled": True, "libraries": [lib], "interval_minutes": 60})
        st = self.srv.get("/api/ta/status")[1]
        for _ in range(100):                      # the library is looked at in the background: the first answer may not have the count yet
            if st["libraries"][0]["channels"] is not None:
                break
            time.sleep(0.05)
            st = self.srv.get("/api/ta/status")[1]
        self.assertEqual(st["settings"]["enabled"], True)
        self.assertEqual(st["libraries"], [{"path": lib, "name": "youtube-nfo", "exists": True, "channels": 2, "converted": 0}])
        self.assertEqual(st["settings"]["source"]["libraries"], "saved")
        self.srv.post("/api/ta/settings", {"enabled": False})

    def test_several_libraries_can_be_saved_and_each_is_named_with_nfo_added(self):
        a = os.path.join(self.root, "media", "youtube")
        b = os.path.join(self.root, "media", "movies")
        code, out = self.srv.post("/api/ta/settings", {"libraries": [a, b, a]})
        self.assertEqual(code, 200)
        self.assertEqual([v["name"] for v in out["settings"]["views"]], ["youtube-nfo", "movies-nfo"])
        st = self.srv.get("/api/ta/status")[1]
        self.assertEqual([x["name"] for x in st["libraries"]], ["youtube-nfo", "movies-nfo"])
        for bad in ({"libraries": []}, {"libraries": ["relative"]}, {"libraries": [a, "/etc"]},
                    {"libraries": [a, os.path.join(self.root, "media", "nope")]}, {"libraries": "x"}):
            self.assertIn(self.srv.post("/api/ta/settings", bad)[0], (400, 404), str(bad))
        self.srv.post("/api/ta/settings", {"libraries": [a]})

    def test_a_missing_default_folder_does_not_block_choosing_the_real_one(self):
        with tempfile.TemporaryDirectory() as root:                       # a server whose settings were never saved
            touch(os.path.join(root, "media", "youtube", UC, "v.mp4"))
            os.makedirs(os.path.join(root, "catalog", "by-genre"))
            srv = Server(root, env={"FS42_SETTINGS": os.path.join(root, "fresh.json"),
                                    "FS42_TA_STATUS": os.path.join(root, "st.json"), "TA_LIBRARY": ""})
            try:
                st = srv.get("/api/ta/status")[1]
                self.assertEqual([x["path"] for x in st["libraries"]], ["/mnt/media/tubearchivist"])     # the default, not there
                real = os.path.join(root, "media", "youtube")
                code, out = srv.post("/api/ta/settings", {"libraries": ["/mnt/media/tubearchivist", real]})
                self.assertEqual(code, 200, out)
                self.assertEqual(out["settings"]["libraries"], [real])                # the default is replaced, not kept
                self.assertEqual(srv.post("/api/ta/settings", {"libraries": ["/etc"]})[0], 400)        # new folders are still checked
            finally:
                srv.stop()

    def test_a_folder_saved_earlier_and_missing_now_can_be_kept_or_taken_out(self):
        with tempfile.TemporaryDirectory() as root:
            touch(os.path.join(root, "media", "youtube", UC, "v.mp4"))
            os.makedirs(os.path.join(root, "media", "gone"))
            os.makedirs(os.path.join(root, "catalog", "by-genre"))
            srv = Server(root, env={"FS42_SETTINGS": os.path.join(root, "s.json"), "FS42_TA_STATUS": os.path.join(root, "st.json")})
            try:
                gone, real = os.path.join(root, "media", "gone"), os.path.join(root, "media", "youtube")
                self.assertEqual(srv.post("/api/ta/settings", {"libraries": [gone]})[0], 200)
                os.rmdir(gone)                                                    # its volume went away
                code, out = srv.post("/api/ta/settings", {"libraries": [gone, real]})        # adding another still works
                self.assertEqual((code, out["settings"]["libraries"]), (200, [gone, real]))
                code, out = srv.post("/api/ta/settings", {"libraries": [real]})              # and the missing one can be removed
                self.assertEqual((code, out["settings"]["libraries"]), (200, [real]))
            finally:
                srv.stop()

    def test_a_bad_library_is_refused_with_a_reason(self):
        for body, code in (({"library": "relative"}, 400),
                           ({"library": "/etc"}, 400),
                           ({"library": os.path.join(self.root, "media", "nope")}, 404),
                           ({"library": os.path.join(self.root, "media", "..", "..")}, 400),
                           ({"interval_minutes": 1}, 400), ({"enabled": "yes"}, 400), ({}, 400)):
            c, out = self.srv.post("/api/ta/settings", body)
            self.assertEqual(c, code, str(body))
            self.assertIn("error", out)

    def test_convert_now_needs_it_switched_on_and_then_asks_the_job(self):
        self.srv.post("/api/ta/settings", {"enabled": False})
        c, out = self.srv.post("/api/ta/run", {})
        self.assertEqual(c, 400)
        self.assertIn("switched off", out["error"])
        self.srv.post("/api/ta/settings", {"enabled": True, "library": os.path.join(self.root, "media", "youtube")})
        c, out = self.srv.post("/api/ta/run", {})
        self.assertEqual((c, out["asked"]), (200, True))
        self.assertTrue(os.path.exists(self.run_now))
        self.srv.post("/api/ta/settings", {"enabled": False})

    def test_playlists_are_no_longer_chosen_here(self):
        self.assertEqual(self.srv.get("/api/ta/playlists")[0], 404)                       # they are chosen as sources, where they are used
        code, out = self.srv.post("/api/ta/settings", {"playlists": ["Picks"]})
        self.assertEqual(code, 400)                                                       # nothing else to save: it says so
        self.assertNotIn("playlists", self.srv.get("/api/ta/status")[1]["settings"])

    def test_only_json_posts(self):
        self.assertEqual(self.srv.post("/api/ta/settings", None, ctype="text/plain", raw=b'{"enabled": true}')[0], 415)

    def test_the_job_s_status_is_passed_through(self):
        with open(self.status, "w") as f:
            json.dump({"state": "idle", "message": "done: 3 videos seen", "last_ok": True}, f)
        job = self.srv.get("/api/ta/status")[1]["job"]
        self.assertEqual((job["state"], job["last_ok"]), ("idle", True))


class SwitchedOffTest(unittest.TestCase):
    def test_the_settings_cannot_be_changed_when_editing_is_off(self):
        root = tempfile.mkdtemp()
        try:
            touch(os.path.join(root, "media", "youtube", UC, "v.mp4"))
            os.makedirs(os.path.join(root, "catalog", "by-genre"))
            srv = Server(root, env={"DASHBOARD_EDIT": "0", "FS42_SETTINGS": os.path.join(root, "s.json"),
                                    "FS42_TA_STATUS": os.path.join(root, "st.json"),
                                    "FS42_TA_NOW": os.path.join(root, "ta.now")})
            try:
                self.assertEqual(srv.get("/api/ta/status")[0], 200)
                self.assertEqual(srv.post("/api/ta/settings", {"enabled": True})[0], 403)
                self.assertEqual(srv.post("/api/ta/run", {})[0], 403)
                self.assertFalse(os.path.exists(os.path.join(root, "s.json")))
            finally:
                srv.stop()
        finally:
            shutil.rmtree(root)


class NeverWaitsOnTheShareTest(unittest.TestCase):
    def test_the_status_answers_at_once_even_when_listing_the_library_takes_ages(self):
        import tubearchivist as ta
        saved = ta._channels_in
        ta._probes.clear()
        ta._channels_in = lambda folder: (time.sleep(1.5), 7)[1]
        folder = tempfile.mkdtemp()
        try:
            began = time.time()
            first = ta.library_probe(folder)
            self.assertLess(time.time() - began, 0.5)                    # it did not wait for the listing
            self.assertEqual(first, (None, None))                        # nothing known yet
            for _ in range(100):
                if ta.library_probe(folder)[1] is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(ta.library_probe(folder), (True, 7))        # the background look finished
        finally:
            ta._channels_in = saved
            ta._probes.clear()
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
