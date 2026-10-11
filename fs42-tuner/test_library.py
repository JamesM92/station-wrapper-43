"""The Library view: tag scan, dead links, duplicates, and the /library.json route.

Run: python3 test_library.py   (from this folder)
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

import library


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


def build_farm(root):
    """A farm shaped like the generator's, plus commercials, loose files and a bump folder."""
    archive = os.path.join(root, "archive")
    farm = os.path.join(root, "farm")
    for show, n in (("Show A", 2), ("Show B", 1)):
        for i in range(n):
            real = os.path.join(archive, "tvseries", show, "Season 01", f"e{i}.mp4")
            touch(real)
            link = os.path.join(farm, "Comedy", "tvseries", show, "Season 01", f"e{i}.mp4")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(real, link)
        nfo = os.path.join(farm, "Comedy", "tvseries", show, "tvshow.nfo")
        touch(nfo)                                              # not a video: not counted
    ad = os.path.join(archive, "ads", "ad1.mp4")
    touch(ad)
    os.makedirs(os.path.join(farm, "commercials", "80s Ads"))
    os.symlink(ad, os.path.join(farm, "commercials", "80s Ads", "ad1.mp4"))
    touch(os.path.join(farm, "Mine", "loose.mp4"))              # a real file straight in a tag
    touch(os.path.join(farm, "bump", "b.mp4"))                  # bump is not a tag
    return farm, archive


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.farm, self.archive = build_farm(self.root)
        library.forget()

    def tearDown(self):
        shutil.rmtree(self.root)
        library.forget()

    def tags(self):
        return {t["tag"]: t for t in library.scan(self.farm)["tags"]}

    def test_counts_videos_per_tag_and_ignores_nfos_and_bump(self):
        tags = self.tags()
        self.assertEqual({k: v["videos"] for k, v in tags.items()},
                         {"Comedy": 3, "commercials": 1, "Mine": 1})
        self.assertNotIn("bump", tags)

    def test_sources_name_the_show_the_playlist_or_loose_files(self):
        tags = self.tags()
        self.assertEqual(tags["Comedy"]["sources"], [{"name": "tvseries/Show A", "videos": 2},
                                                     {"name": "tvseries/Show B", "videos": 1}])
        self.assertEqual(tags["commercials"]["sources"], [{"name": "80s Ads", "videos": 1}])
        self.assertEqual(tags["Mine"]["sources"], [{"name": library.LOOSE, "videos": 1}])

    def test_a_link_to_a_missing_file_is_dead(self):
        os.remove(os.path.join(self.archive, "tvseries", "Show A", "Season 01", "e0.mp4"))
        self.assertEqual(self.tags()["Comedy"]["dead"], 1)
        self.assertEqual(library.scan(self.farm)["totals"]["dead"], 1)

    def test_the_same_file_linked_twice_in_one_tag_is_a_duplicate(self):
        real = os.path.join(self.archive, "tvseries", "Show B", "Season 01", "e0.mp4")
        os.symlink(real, os.path.join(self.farm, "Comedy", "second name.mp4"))
        self.assertEqual(self.tags()["Comedy"]["duplicates"], 1)

    def test_the_same_file_in_two_tags_is_not_a_duplicate(self):
        real = os.path.join(self.archive, "tvseries", "Show B", "Season 01", "e0.mp4")
        os.symlink(real, os.path.join(self.farm, "Mine", "also.mp4"))
        self.assertEqual(self.tags()["Comedy"]["duplicates"], 0)
        self.assertEqual(self.tags()["Mine"]["duplicates"], 0)

    def test_a_duplicate_through_a_chain_of_links_is_still_found(self):
        # farm link -> view link -> archive file, the way TA_NFOWriter's view works
        real = os.path.join(self.archive, "tvseries", "Show B", "Season 01", "e0.mp4")
        view = os.path.join(self.root, "view.mp4")
        os.symlink(real, view)
        os.symlink(view, os.path.join(self.farm, "Comedy", "via view.mp4"))
        self.assertEqual(self.tags()["Comedy"]["duplicates"], 1)

    def test_busiest_tag_first_and_totals(self):
        scan = library.scan(self.farm)
        self.assertEqual([t["tag"] for t in scan["tags"]], ["Comedy", "Mine", "commercials"])
        self.assertEqual(scan["totals"], {"videos": 5, "dead": 0, "duplicates": 0})

    def test_a_missing_folder_is_reported_not_raised(self):
        scan = library.scan(os.path.join(self.root, "nope"))
        self.assertFalse(scan["found"])
        self.assertEqual(scan["tags"], [])

    def test_status_is_cached_until_it_expires_or_is_forgotten(self):
        first = library.status(self.farm, now=1000)
        touch(os.path.join(self.farm, "Comedy", "new.mp4"))
        again = library.status(self.farm, now=1000 + library.CACHE_SECONDS - 1)
        self.assertEqual(again["totals"]["videos"], first["totals"]["videos"])
        self.assertEqual(again["age_seconds"], library.CACHE_SECONDS - 1)
        later = library.status(self.farm, now=1000 + library.CACHE_SECONDS + 1)
        self.assertEqual(later["totals"]["videos"], first["totals"]["videos"] + 1)
        touch(os.path.join(self.farm, "Comedy", "newer.mp4"))
        library.forget()
        self.assertEqual(library.status(self.farm)["totals"]["videos"], first["totals"]["videos"] + 2)


class RouteTest(unittest.TestCase):
    """/library.json on a running tuner, and the dashboard page that reads it."""
    PORT = 5011
    DELAY = "0.2"                       # seconds after start-up before the regular scan

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.farm, _ = build_farm(self.root)
        # the tuner won't start without a schedule database; an empty one will do
        db = os.path.join(self.root, "fs42.db")
        c = sqlite3.connect(db)
        c.execute("""CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT,
 station TEXT NOT NULL, liquid_type TEXT NOT NULL, start_time TIMESTAMP, end_time TIMESTAMP,
 break_strategy TEXT NOT NULL, title TEXT NOT NULL, sequence_key TEXT, break_info TEXT,
 content_json TEXT, plan_json TEXT NOT NULL)""")
        c.commit()
        c.close()
        conf = {"db_path": db, "catalog_root": self.root,
                "library_root": self.farm, "channels": {}, "port": self.PORT,
                "advertise_url": f"http://127.0.0.1:{self.PORT}"}
        path = os.path.join(self.root, "config.json")
        with open(path, "w") as f:
            json.dump(conf, f)
        env = dict(os.environ, FS42_TUNER_CONF=path, LIBRARY_FIRST_DELAY_SECONDS=self.DELAY,
                   FS42_LIBRARY_CACHE=os.path.join(self.root, "library-scan.json"))
        self.proc = subprocess.Popen([sys.executable, "tuner.py"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.2)

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        shutil.rmtree(self.root)

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}{path}", timeout=10).read()

    def library(self, query=""):
        """/library.json once the regular scan has run."""
        end = time.time() + 20
        while True:
            data = json.loads(self.get("/library.json" + query))
            if not data.get("never") or time.time() > end:
                return data
            query = ""
            time.sleep(0.1)

    def test_the_first_answer_is_what_the_regular_scan_found(self):
        data = self.library()
        self.assertTrue(data["found"])
        self.assertEqual(data["totals"]["videos"], 5)
        self.assertEqual({t["tag"] for t in data["tags"]}, {"Comedy", "commercials", "Mine"})
        self.assertIsNotNone(data["age_seconds"])

    def test_a_change_waits_for_the_next_scan_until_you_ask_for_one(self):
        self.assertEqual(self.library()["totals"]["videos"], 5)
        touch(os.path.join(self.farm, "Mine", "added.mp4"))
        for _ in range(3):                                                           # visiting the page again and again
            self.assertEqual(json.loads(self.get("/library.json"))["totals"]["videos"], 5)
        json.loads(self.get("/library.json?refresh"))                                # the Rescan button: asked for
        end = time.time() + 20
        while json.loads(self.get("/library.json"))["totals"]["videos"] != 6 and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(json.loads(self.get("/library.json"))["totals"]["videos"], 6)

    def test_a_scan_is_saved_so_a_restart_has_something_to_show(self):
        self.library()
        saved = json.load(open(os.path.join(self.root, "library-scan.json")))
        self.assertEqual(saved["data"]["totals"]["videos"], 5)

    def test_the_dashboard_page_has_the_library_section(self):
        page = self.get("/").decode()
        self.assertIn('id="library"', page)
        self.assertIn("/library.json", self.get("/dashboard.js").decode())


class NoScanYetTest(RouteTest):
    """Before the first scheduled scan: visiting must not start one."""
    PORT = 5016
    DELAY = "3600"

    def test_the_first_answer_is_what_the_regular_scan_found(self):
        pass

    test_a_change_waits_for_the_next_scan_until_you_ask_for_one = test_the_first_answer_is_what_the_regular_scan_found
    test_a_scan_is_saved_so_a_restart_has_something_to_show = test_the_first_answer_is_what_the_regular_scan_found
    test_the_dashboard_page_has_the_library_section = test_the_first_answer_is_what_the_regular_scan_found

    def test_opening_the_page_does_not_start_a_scan(self):
        for _ in range(3):
            data = json.loads(self.get("/library.json"))
            self.assertTrue(data["never"])
            self.assertFalse(data["scanning"])
            self.assertEqual(data["tags"], [])
        time.sleep(1.0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "library-scan.json")))      # still nothing walked


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.farm, _ = build_farm(self.root)
        self._saved = (library.SAVE_PATH,)
        library.SAVE_PATH = os.path.join(self.root, "scan.json")
        library.forget()

    def tearDown(self):
        library.SAVE_PATH, = self._saved
        library.forget()
        shutil.rmtree(self.root)

    def test_cached_never_walks_anything(self):
        got = library.cached(self.farm)
        self.assertTrue(got["never"])
        self.assertEqual(got["tags"], [])

    def test_the_loop_waits_then_scans_then_waits_the_interval(self):
        slept = []
        library.loop(self.farm, interval_minutes=30, first_delay=45, sleep=slept.append, forever=False)
        self.assertEqual(slept, [45])
        got = library.cached(self.farm)
        self.assertEqual(got["totals"]["videos"], 5)
        self.assertNotIn("never", got)

    def test_a_saved_scan_is_loaded_after_a_restart_but_only_for_the_same_folder(self):
        library.scan_now(self.farm)
        library.forget()
        self.assertTrue(library.cached(self.farm)["never"])
        self.assertTrue(library.load_saved(self.farm))
        self.assertEqual(library.cached(self.farm)["totals"]["videos"], 5)
        library.forget()
        self.assertFalse(library.load_saved(self.farm + "-other"))

    def test_changing_one_list_recounts_only_that_list(self):
        library.scan_now(self.farm)
        touch(os.path.join(self.farm, "Mine", "added.mp4"))
        library.refresh_tag(self.farm, "Mine").join(10)
        end = time.time() + 10
        while library.cached(self.farm)["totals"]["videos"] != 6 and time.time() < end:
            time.sleep(0.05)
        got = library.cached(self.farm)
        self.assertEqual(got["totals"]["videos"], 6)
        self.assertEqual({t["tag"] for t in got["tags"]}, {"Comedy", "commercials", "Mine"})
        shutil.rmtree(os.path.join(self.farm, "Mine"))                              # a list that was deleted drops out
        library.refresh_tag(self.farm, "Mine").join(10)
        end = time.time() + 10
        while "Mine" in {t["tag"] for t in library.cached(self.farm)["tags"]} and time.time() < end:
            time.sleep(0.05)
        self.assertNotIn("Mine", {t["tag"] for t in library.cached(self.farm)["tags"]})


class NoDatabaseTest(unittest.TestCase):
    """A brand-new install: no schedule database yet. The tuner must start and answer."""
    PORT = 5014

    def setUp(self):
        self.root = tempfile.mkdtemp()
        conf = {"db_path": os.path.join(self.root, "not-yet.db"), "catalog_root": self.root,
                "channels": {"801": "Comedy"}, "port": self.PORT,
                "advertise_url": f"http://127.0.0.1:{self.PORT}"}
        path = os.path.join(self.root, "config.json")
        with open(path, "w") as f:
            json.dump(conf, f)
        self.proc = subprocess.Popen([sys.executable, "tuner.py"], env=dict(os.environ, FS42_TUNER_CONF=path),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(150):
            if self.proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", self.PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.2)

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        shutil.rmtree(self.root)

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}{path}", timeout=10).read()

    def test_it_starts_instead_of_exiting(self):
        self.assertIsNone(self.proc.poll(), "the tuner exited because there is no schedule database yet")

    def test_the_dashboard_and_the_channel_list_work_with_every_channel_off_air(self):
        self.assertIn(b"Station Wrapper 43", self.get("/"))
        chans = json.loads(self.get("/channels.json"))
        self.assertEqual([(c["channel"], c["on_air"]) for c in chans], [("801", False)])
        self.assertIn("uptime_seconds", json.loads(self.get("/system.json")))
        self.assertEqual(json.loads(self.get("/library.json"))["found"], False)

    def test_the_guide_and_the_lineup_are_empty_but_valid(self):
        self.assertIn(b"<tv", self.get("/xmltv.xml"))
        self.assertEqual(json.loads(self.get("/lineup.json"))[0]["GuideNumber"], "801")
        self.assertFalse(os.path.exists(os.path.join(self.root, "not-yet.db")))   # looking never creates it


if __name__ == "__main__":
    unittest.main()
