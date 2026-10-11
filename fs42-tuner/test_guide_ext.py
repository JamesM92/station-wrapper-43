"""A tuner on the Sources page gets its real listings into OUR guide, on the channels the lineup gave it.
Run: python3 test_guide_ext.py"""
import time
import unittest
import xml.etree.ElementTree as ET

from guide import GuideBuilder


class Resolver:
    @staticmethod
    def listings(db, station, a, b):
        return []


class ExternalGuide(unittest.TestCase):
    def build(self, renumber):
        now = int(time.time())
        rows = [{"GuideNumber": "20.1", "GuideName": "WBXX", "ImageURL": "http://x/l.png",
                 "Guide": [{"StartTime": now - 600, "EndTime": now + 3000, "Title": "Friends", "EpisodeTitle": "The Pilot"},
                           {"StartTime": now + 3000, "EndTime": now + 90000, "Title": "Seinfeld"}]}]
        gb = GuideBuilder({"100": "iptv:100", "5": "Local"}, Resolver, "none.db", guide_hours=4)
        gb._sd_get = lambda url: rows
        gb.static = lambda: {"iptv:100": "Live: WBXX"}                             # the placeholder an external channel gets
        gb.display = lambda s: "WBXX" if s == "iptv:100" else s
        gb.extra_sources = lambda: [{"type": "hdhomerun", "name": "tuner", "device_auth": "AUTH", "id_prefix": "ext1", "renumber": renumber}]
        return ET.fromstring(gb.xmltv(force=True))

    def test_the_tuners_listings_land_on_its_channel_in_our_lineup_and_replace_the_placeholder(self):
        root = self.build({"20.1": "100"})
        ids = [c.get("id") for c in root.findall("channel")]
        self.assertEqual(sorted(ids), ["fs42.100", "fs42.5"])                       # no second copy of the channel
        titles = [p.find("title").text for p in root.findall("programme") if p.get("channel") == "fs42.100"]
        self.assertEqual(titles, ["Friends", "Seinfeld"])                           # real listings, not "Live: WBXX"
        sub = [p.find("sub-title").text for p in root.findall("programme") if p.get("channel") == "fs42.100" and p.find("sub-title") is not None]
        self.assertEqual(sub, ["The Pilot"] if sub else [])

    def test_a_playlists_own_guide_lands_on_its_channels_and_other_feed_channels_are_left_out(self):
        gb = GuideBuilder({"100": "iptv:100"}, Resolver, "none.db", guide_hours=4)
        gb.static = lambda: {"iptv:100": "Live: News A"}
        gb.display = lambda s: "News A"
        stamp = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(time.time() - 600))
        later = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(time.time() + 3000))
        feed = (f'<tv><channel id="a.news"><display-name>A</display-name></channel><channel id="other"><display-name>O</display-name></channel>'
                f'<programme start="{stamp}" stop="{later}" channel="a.news"><title>Evening News</title></programme>'
                f'<programme start="{stamp}" stop="{later}" channel="other"><title>Not Ours</title></programme></tv>').encode()
        gb.extra_sources = lambda: [{"type": "xmltv", "name": "playlist", "url": "http://feed/x.xml", "retag": {"a.news": "100"}, "only_mapped": True}]
        from unittest import mock
        with mock.patch("guide._fetch", lambda url, *a, **k: feed):
            root = ET.fromstring(gb.xmltv(force=True))
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["fs42.100"])
        self.assertEqual([p.find("title").text for p in root.findall("programme")], ["Evening News"])

    def test_off_air_time_is_a_gap_in_the_guide_not_a_programme(self):
        class R:
            @staticmethod
            def listings(db, station, a, b):
                mid = a + (b - a) / 2
                return [{"title": "Offair", "start": a, "end": mid}, {"title": "Mystery Theater", "start": mid, "end": b}]
        gb = GuideBuilder({"5": "Mystery"}, R, "none.db", guide_hours=4)
        root = ET.fromstring(gb.xmltv(force=True))
        self.assertEqual([p.find("title").text for p in root.findall("programme")], ["Mystery Theater"])

    def test_a_scheduled_video_carries_its_synopsis_episode_date_kinds_and_picture(self):
        class R:
            @staticmethod
            def listings(db, station, a, b):
                return [{"title": "Mystery Theater", "subtitle": "The Locked Room", "start": a, "end": b, "path": "by-genre/X/ep.mp4"}]
        gb = GuideBuilder({"5": "Mystery"}, R, "none.db", guide_hours=4)
        gb.details = lambda p: {"plot": "A door that locks itself.", "season": 3, "episode": 7, "aired": "1994-05-06", "genres": ["Mystery"]}
        gb.art_url = lambda p: "@@BASE@@/art/" + p
        root = ET.fromstring(gb.xmltv(force=True))
        pr = root.find("programme")
        self.assertEqual(pr.find("desc").text, "A door that locks itself.")
        self.assertEqual(pr.find("date").text, "19940506")
        self.assertEqual(pr.find("category").text, "Mystery")
        self.assertEqual(pr.find("icon").get("src"), "@@BASE@@/art/by-genre/X/ep.mp4")
        ns = {e.get("system"): e.text for e in pr.findall("episode-num")}
        self.assertEqual(ns, {"xmltv_ns": "2.6.", "onscreen": "S03E07"})
        self.assertEqual([e.tag for e in pr][:4], ["title", "sub-title", "desc", "date"])         # in the order XMLTV asks for

    def test_a_tuner_whose_channel_is_not_in_the_lineup_adds_nothing_and_breaks_nothing(self):
        root = self.build({"99.9": "777"})                                           # that number is not a channel here
        self.assertIn("fs42.100", [c.get("id") for c in root.findall("channel")])
        self.assertEqual([p.find("title").text for p in root.findall("programme") if p.get("channel") == "fs42.100"], ["Live: WBXX"])


class FirstRequest(unittest.TestCase):
    def test_the_very_first_request_gets_our_own_channels_at_once_not_after_the_slow_listings(self):
        import threading
        gate = threading.Event()
        gb = GuideBuilder({"5": "Five"}, Resolver, "none.db", guide_hours=1)
        real = gb._build

        def build(quick=False):
            if not quick:
                gate.wait(10)                                    # the antenna's listings from another server: slow
            return real(quick=quick)
        gb._build = build
        began = time.time()
        first = gb.xmltv()
        self.assertLess(time.time() - began, 1.0)
        self.assertIn(b'id="fs42.5"', first)                      # our own channels are there straight away
        gate.set()
        for _ in range(100):                                       # and the full guide replaces it when it is done
            gb._lock.acquire()
            built = gb._building
            gb._lock.release()
            if not built:
                break
            time.sleep(0.05)
        self.assertIn(b'id="fs42.5"', gb.xmltv())


class StrictXml(unittest.TestCase):
    def test_characters_xml_cannot_hold_never_reach_a_strict_parser(self):
        class R:
            @staticmethod
            def listings(db, station, a, b):
                return [{"title": "Bad\x0bTitle\x00 & more", "subtitle": "ok\x1f", "start": a, "end": b}]
        gb = GuideBuilder({"5": "Five"}, R, "x.db", guide_hours=2)
        doc = gb.xmltv(force=True)
        root = ET.fromstring(doc)                                 # a strict parse (a browser's, Jellyfin's) would refuse the guide otherwise
        self.assertEqual(root.find("programme/title").text, "BadTitle & more")


class NeverStalls(unittest.TestCase):
    def test_an_old_guide_is_served_at_once_while_a_new_one_is_made_in_the_background(self):
        import threading
        gb = GuideBuilder({}, Resolver, "none.db", guide_hours=1, refresh_minutes=1)
        gate, made = threading.Event(), []

        def build(quick=False):
            made.append(1)
            if len(made) > 1:
                gate.wait(10)                                    # the second build is slow (other servers are asked)
            return b"<tv>%d</tv>" % len(made)
        gb._build = build
        self.assertEqual(gb.xmltv(force=True), b"<tv>1</tv>")     # a forced build waits for the result
        gb._at -= 3600                                             # it is out of date now
        began = time.time()
        self.assertEqual(gb.xmltv(), b"<tv>1</tv>")               # the old one comes back straight away ...
        self.assertLess(time.time() - began, 0.5)
        self.assertEqual(gb.xmltv(), b"<tv>1</tv>")               # ... and asking again does not start a second build
        time.sleep(0.2)
        self.assertEqual(len(made), 2)
        gate.set()
        for _ in range(100):
            if gb.xmltv() == b"<tv>2</tv>":
                break
            time.sleep(0.05)
        self.assertEqual(gb.xmltv(), b"<tv>2</tv>")               # then the new one is used

    def test_a_refresh_that_fails_keeps_the_old_guide(self):
        gb = GuideBuilder({}, Resolver, "none.db", guide_hours=1, refresh_minutes=1)
        gb._build = lambda quick=False: b"<tv>ok</tv>"
        gb.xmltv(force=True)
        gb._build = lambda quick=False: 1 / 0
        gb._at -= 3600
        self.assertEqual(gb.xmltv(), b"<tv>ok</tv>")
        time.sleep(0.3)
        self.assertEqual(gb.xmltv(), b"<tv>ok</tv>")


class SiliconDustIsAskedRarely(unittest.TestCase):
    ROWS = [{"GuideNumber": "20.1", "GuideName": "WBXX",
             "Guide": [{"StartTime": 1, "EndTime": 2000000000, "Title": "Friends"}]}]

    def builder(self, tmp=None):
        gb = GuideBuilder({}, Resolver, "none.db", guide_hours=4)
        gb.cache_dir = tmp
        return gb

    def test_a_second_build_reuses_the_answer_and_a_refusal_keeps_the_old_listings(self):
        import urllib.error
        calls = []
        gb = self.builder()

        def get(url):
            calls.append(url)
            return self.ROWS
        gb._sd_get = get
        src = {"type": "hdhomerun", "device_auth": "A", "id_prefix": "x"}
        c, p = gb._hdhomerun_elements(src)
        self.assertEqual(len(p), 1)
        n = len(calls)
        gb._hdhomerun_elements(src)
        self.assertEqual(len(calls), n)                                  # not asked again
        src["cache_minutes"] = 0                                          # due again, and now it refuses
        def refuse(url):
            calls.append(url)
            raise urllib.error.HTTPError(url, 429, "slow down", {}, None)
        gb._sd_get = refuse
        c, p = gb._hdhomerun_elements(src)
        self.assertEqual(len(p), 1)                                      # the old listings, not none
        n = len(calls)
        gb._hdhomerun_elements(src)
        self.assertEqual(len(calls), n)                                  # and left alone while it is backing off

    def test_a_restart_uses_the_answer_kept_on_disk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            gb = self.builder(d)
            gb._sd_get = lambda url: self.ROWS
            src = {"type": "hdhomerun", "device_auth": "A", "id_prefix": "x"}
            gb._hdhomerun_elements(src)
            again = self.builder(d)
            again._sd_get = lambda url: (_ for _ in ()).throw(AssertionError("asked again after a restart"))
            c, p = again._hdhomerun_elements(src)
            self.assertEqual(len(p), 1)


if __name__ == "__main__":
    unittest.main()
