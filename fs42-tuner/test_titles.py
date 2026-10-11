"""The name a video is called by, from its .nfo (titles.py), and where it shows up. Run: python3 test_titles.py"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import resolver
import titles

YOUTUBE = '''<episodedetails><title>How &amp; Why Bridges Fall</title><showtitle>Some Channel</showtitle>
<uniqueid type="youtube" default="true">abc123</uniqueid></episodedetails>'''
EPISODE = "<episodedetails><title>The One With The Pilot</title><showtitle>Friends</showtitle><season>1</season></episodedetails>"
MOVIE = "<movie><title><![CDATA[Parasite]]></title><year>2019</year></movie>"


class Titles(unittest.TestCase):
    def setUp(self):
        titles.forget()
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)
        titles.forget()

    def video(self, name, nfo=None):
        p = os.path.join(self.root, name)
        open(p, "w").close()
        if nfo is not None:
            with open(os.path.splitext(p)[0] + ".nfo", "w") as f:
                f.write(nfo)
        return p

    def test_a_show_and_a_youtube_channel_are_named_by_their_name_a_movie_by_its_title(self):
        titles.configure(False)                                                  # the show's or channel's name alone
        try:
            self.assertEqual(titles.title_for(self.video("20260101_[abc123]_How.mp4", YOUTUBE)), "Some Channel")
            self.assertEqual(titles.title_for(self.video("Friends.S01E01.mkv", EPISODE)), "Friends")
            self.assertEqual(titles.title_for(self.video("Parasite.2019.1080p.mkv", MOVIE)), "Parasite")
        finally:
            titles.configure(True)

    def test_the_episode_or_the_video_title_goes_after_the_name_by_default_and_can_be_switched_off(self):
        yt, ep = self.video("yt.mp4", YOUTUBE), self.video("Friends.S01E01.mkv", EPISODE)
        self.assertEqual(titles.title_for(ep), "Friends: The One With The Pilot")         # on unless it is switched off
        self.assertEqual(titles.title_for(yt), "Some Channel: How & Why Bridges Fall")
        self.assertEqual(titles.title_for(self.video("m.mkv", MOVIE)), "Parasite")
        same = self.video("same.mkv", "<episodedetails><title>Show</title><showtitle>Show</showtitle></episodedetails>")
        self.assertEqual(titles.title_for(same), "Show")                                  # not "Show: Show"
        titles.configure(False)
        try:
            self.assertEqual(titles.title_for(ep), "Friends")
            self.assertEqual(titles.title_for(yt), "Some Channel")
        finally:
            titles.configure(True)

    def test_without_an_nfo_or_a_usable_one_the_file_name_stays(self):
        self.assertIsNone(titles.title_for(self.video("plain.mp4")))
        self.assertIsNone(titles.title_for(self.video("empty.mp4", "<movie></movie>")))
        self.assertIsNone(titles.title_for(self.video("junk.mp4", "not xml at all")))
        self.assertIsNone(titles.title_for(self.video("show.mp4", "<tvshow><title>A Show</title></tvshow>")))      # a show's own file is not an episode's
        self.assertIsNone(titles.title_for(os.path.join(self.root, "notes.txt")))

    def test_an_episode_with_no_show_name_uses_its_own_title_and_the_name_is_kept_short(self):
        self.assertEqual(titles.title_for(self.video("e.mkv", "<episodedetails><title>Only Title</title></episodedetails>")), "Only Title")
        self.assertEqual(len(titles.title_for(self.video("long.mkv", f"<movie><title>{'x' * 500}</title></movie>"))), 200)

    def test_answers_are_remembered_and_a_missing_nfo_too(self):
        p = self.video("a.mkv", MOVIE)
        self.assertEqual(titles.title_for(p), "Parasite")
        os.remove(os.path.splitext(p)[0] + ".nfo")
        self.assertEqual(titles.title_for(p), "Parasite")                       # not read again within the hour
        q = self.video("b.mkv")
        self.assertIsNone(titles.title_for(q))
        with open(os.path.splitext(q)[0] + ".nfo", "w") as f:
            f.write(MOVIE)
        self.assertIsNone(titles.title_for(q))                                   # a missing one is remembered for a few minutes

    def test_the_guide_gets_the_name_and_the_episode_apart(self):
        p, yt = self.video("Friends.S01E01.mkv", EPISODE), self.video("yt.mp4", YOUTUBE)
        self.assertEqual(titles.names_for(p), ("Friends", "The One With The Pilot"))
        self.assertEqual(titles.names_for(yt), ("Some Channel", "How & Why Bridges Fall"))             # the channel big, the video's title small
        self.assertEqual(titles.names_for(self.video("m.mkv", MOVIE)), ("Parasite", None))
        self.assertEqual(titles.names_for(os.path.join(self.root, "none.mkv")), (None, None))
        self.assertEqual(titles.hook(self.root, names=True)("Friends.S01E01.mkv"), ("Friends", "The One With The Pilot"))
        titles.configure(False)
        try:
            self.assertEqual(titles.names_for(p), ("Friends", None))                                    # switched off: the name alone
        finally:
            titles.configure(True)

    def test_the_hook_takes_paths_relative_to_the_catalog_too(self):
        self.video("m.mkv", MOVIE)
        self.assertEqual(titles.hook(self.root)("m.mkv"), "Parasite")


class Where(unittest.TestCase):
    """The guide, the banner and the schedule use the name."""
    def setUp(self):
        titles.forget()
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "catalog"))
        self.db = os.path.join(self.root, "fluid.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        c.execute("CREATE TABLE catalog_entries (station TEXT, path TEXT, title TEXT)")
        self.film = "catalog/Parasite.2019.1080p.mkv"
        open(os.path.join(self.root, self.film), "w").close()
        with open(os.path.join(self.root, "catalog", "Parasite.2019.1080p.nfo"), "w") as f:
            f.write(MOVIE)
        self.now = datetime.now().replace(microsecond=0)
        plan = [{"path": "catalog/bump.mp4", "skip": 0, "duration": 5, "is_stream": False, "content_type": "bump"},
                {"path": self.film, "skip": 0, "duration": 3000, "is_stream": False, "content_type": "feature"},
                {"path": "catalog/ad.mp4", "skip": 0, "duration": 100, "is_stream": False, "content_type": "commercial"}]
        c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,?)",
                  ("S", (self.now - timedelta(minutes=5)).isoformat(), (self.now + timedelta(hours=1)).isoformat(), "Parasite.2019.1080p", json.dumps(plan)))
        c.execute("INSERT INTO catalog_entries VALUES ('S', ?, 'Parasite.2019.1080p')", (self.film,))
        c.commit()
        c.close()
        self.saved, self.saved_names = resolver.TITLE_HOOK, resolver.NAMES_HOOK

    def tearDown(self):
        resolver.TITLE_HOOK, resolver.NAMES_HOOK = self.saved, self.saved_names
        shutil.rmtree(self.root)
        titles.forget()

    def test_the_old_schedule_being_kept_during_a_rebuild_gets_the_names_too(self):
        import legacy
        saved, legacy.PATH = legacy.PATH, os.path.join(self.root, "legacy.db")
        try:
            self.assertTrue(legacy.snapshot(self.db, "S", now=self.now))              # a rebuild began: the schedule is kept
            c = sqlite3.connect(self.db)
            c.execute("DELETE FROM liquid_blocks")                                   # and FieldStation42's own is gone
            c.commit()
            c.close()
            resolver.TITLE_HOOK = titles.hook(self.root)
            self.assertEqual(resolver.listings(self.db, "S", self.now, self.now + timedelta(minutes=30))[0]["title"], "Parasite")
            self.assertEqual(resolver.resolve(self.db, "S", self.now).block_title, "Parasite")
            resolver.TITLE_HOOK = None
            self.assertEqual(resolver.listings(self.db, "S", self.now, self.now + timedelta(minutes=30))[0]["title"], "Parasite.2019.1080p")
        finally:
            legacy.PATH = saved

    def test_without_the_hook_it_is_the_file_name_as_before(self):
        resolver.TITLE_HOOK = None
        self.assertEqual(resolver.resolve(self.db, "S", self.now).block_title, "Parasite.2019.1080p")
        self.assertEqual(resolver.listings(self.db, "S", self.now, self.now + timedelta(hours=1))[0]["title"], "Parasite.2019.1080p")

    def test_the_listings_carry_the_episode_name_apart_for_the_guide(self):
        resolver.TITLE_HOOK, resolver.NAMES_HOOK = titles.hook(self.root), titles.hook(self.root, names=True)
        titles.configure(True)
        try:
            with open(os.path.join(self.root, "catalog", "Parasite.2019.1080p.nfo"), "w") as f:
                f.write(EPISODE)
            titles.forget()
            got = resolver.listings(self.db, "S", self.now, self.now + timedelta(hours=1))[0]
            self.assertEqual((got["title"], got["subtitle"]), ("Friends", "The One With The Pilot"))
            self.assertEqual(resolver.resolve(self.db, "S", self.now).block_title, "Friends: The One With The Pilot")   # one line elsewhere
        finally:
            titles.configure(True)

    def test_with_the_hook_the_banner_the_guide_and_the_file_list_say_the_name(self):
        resolver.TITLE_HOOK = titles.hook(self.root)
        self.assertEqual(resolver.resolve(self.db, "S", self.now).block_title, "Parasite")
        self.assertEqual(resolver.listings(self.db, "S", self.now, self.now + timedelta(hours=1))[0]["title"], "Parasite")
        self.assertEqual(resolver.block_plan(self.db, "S", self.now - timedelta(minutes=5))[0], "Parasite")
        self.assertEqual(resolver.titles(self.db, "S", [self.film, "catalog/ad.mp4"]), {self.film: "Parasite"})


class Setting(unittest.TestCase):
    def test_the_episode_name_setting_is_saved_checked_and_read_back(self):
        import settings
        root = tempfile.mkdtemp()
        path = os.path.join(root, "settings.json")
        try:
            self.assertEqual(settings.get_titles(path), {"episode_names": True})                       # on unless switched off
            self.assertEqual(settings.set_titles({"episode_names": False}, path), {"episode_names": False})
            self.assertEqual(settings.get_titles(path), {"episode_names": False})
            with self.assertRaises(settings.SettingsError):
                settings.set_titles({"episode_names": "yes"}, path)
        finally:
            shutil.rmtree(root)

    def test_the_tick_box_is_on_the_guide_channels_edit_page_and_not_on_the_stations_overview(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        with open(os.path.join(here, "station.html")) as f:
            self.assertIn('id="g-episodes"', f.read())
        with open(os.path.join(here, "stations.html")) as f:
            self.assertNotIn("episode", f.read().lower())
        with open(os.path.join(here, "stations.js")) as f:
            self.assertNotIn("n-episodes", f.read())

    def test_through_the_api_it_changes_the_names_the_guide_uses(self):
        from test_files_api import Server
        import urllib.request
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "catalog"))
        srv = Server(root, env={"FS42_SETTINGS": os.path.join(root, "settings.json")})
        try:
            self.assertEqual(srv.get("/api/titles")[1]["settings"], {"episode_names": True})
            self.assertEqual(srv.post("/api/titles/settings", {"episode_names": False})[1], {"settings": {"episode_names": False}})
            self.assertEqual(srv.get("/api/titles")[1]["settings"], {"episode_names": False})
            self.assertEqual(srv.post("/api/titles/settings", {"episode_names": "maybe"})[0], 400)
        finally:
            srv.stop()
            shutil.rmtree(root)


class DetailsTest(unittest.TestCase):
    NFO = ("<episodedetails><title>The Locked Room</title><showtitle>Mystery Theater</showtitle><season>3</season><episode>7</episode>"
           "<plot><![CDATA[A door that\n  locks itself.]]></plot><aired>1994-05-06</aired><genre>Mystery</genre><genre>Drama</genre>"
           "</episodedetails>")

    def test_the_synopsis_season_episode_date_and_kinds_come_from_the_nfo(self):
        d = titles.details_from_nfo(self.NFO)
        self.assertEqual((d["season"], d["episode"], d["aired"]), (3, 7, "1994-05-06"))
        self.assertEqual(d["plot"], "A door that locks itself.")
        self.assertEqual(d["genres"], ["Mystery", "Drama"])
        self.assertEqual(titles.details_from_nfo("<tvshow><title>x</title></tvshow>"), {})
        self.assertEqual(titles.details_from_nfo("<movie><title>Film</title><year>2019</year></movie>"),
                         {"kind": "movie", "year": 2019, "genres": ["Movie"]})                              # Jellyfin files it under Movies
        kids = titles.details_from_nfo("<episodedetails><title>x</title><genre>Animation</genre></episodedetails>")
        self.assertEqual(kids["genres"], ["Animation", "Kids"])                            # and a cartoon under Kids

    def test_the_picture_beside_a_video_is_found_through_the_link_a_list_holds(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        view, lst = os.path.join(d, "view"), os.path.join(d, "list")
        os.makedirs(view)
        os.makedirs(lst)
        open(os.path.join(view, "ep.mp4"), "w").close()
        open(os.path.join(view, "ep-thumb.jpg"), "w").close()
        os.symlink(os.path.join(view, "ep.mp4"), os.path.join(lst, "ep.mp4"))
        self.assertEqual(titles.art_for(os.path.join(lst, "ep.mp4")), os.path.join(view, "ep-thumb.jpg"))
        self.assertIsNone(titles.art_for(os.path.join(lst, "nothing.mp4")))

    def test_with_no_picture_of_its_own_the_shows_poster_is_used(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        show, season, lst = os.path.join(d, "Show"), os.path.join(d, "Show", "Season 1"), os.path.join(d, "list")
        os.makedirs(season)
        os.makedirs(lst)
        open(os.path.join(season, "e1.mp4"), "w").close()
        open(os.path.join(show, "poster.jpg"), "w").close()
        os.symlink(os.path.join(season, "e1.mp4"), os.path.join(lst, "e1.mp4"))
        self.assertEqual(titles.art_for(os.path.join(lst, "e1.mp4")), os.path.join(show, "poster.jpg"))

if __name__ == "__main__":
    unittest.main()
