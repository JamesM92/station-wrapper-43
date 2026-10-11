"""External IPTV sources (iptv.py): addresses, playlists, numbering, and the ffmpeg command. Run: python3 test_iptv.py"""
import json
import os
import shutil
import tempfile
import unittest

import iptv

M3U = """#EXTM3U
#EXTINF:-1 tvg-id="news.a" tvg-logo="http://x/l.png" group-title="News",News A
http://example.com/a.ts
#EXTINF:-1 group-title="Sport",Sport 1
rtsp://cam.local/stream1
#EXTINF:-1,Local File
file:///etc/passwd
#EXTINF:-1,Same Name
http://example.com/s1.ts
#EXTINF:-1,Same Name
http://example.com/s2.ts
# a comment
http://example.com/orphan.ts
"""


class Addresses(unittest.TestCase):
    def test_only_the_right_kinds_of_address_are_accepted(self):
        self.assertEqual(iptv.check_url("  rtsp://u:p@10.0.0.5:554/live  ", "stream"), "rtsp://u:p@10.0.0.5:554/live")
        for bad, kind in (("file:///etc/passwd", "stream"), ("rtsp://cam/x", "m3u"), ("http://", "stream"), ("http://a b/c", "stream"),
                          ("", "stream"), (None, "m3u"), ("concat:a|b", "stream"), ("http://x/\nfile", "stream")):
            with self.assertRaises(iptv.SourceError, msg=repr(bad)):
                iptv.check_url(bad, kind)

    def test_a_password_in_an_address_is_hidden_when_shown(self):
        self.assertEqual(iptv.mask("rtsp://admin:s3cret@10.0.0.5:554/live"), "rtsp://admin:****@10.0.0.5:554/live")
        self.assertEqual(iptv.mask("http://10.0.0.9/lineup"), "http://10.0.0.9/lineup")


class Playlists(unittest.TestCase):
    def test_a_playlist_gives_channels_and_skips_what_cannot_be_played(self):
        ch = iptv.parse_m3u(M3U)
        self.assertEqual([c["name"] for c in ch], ["News A", "Sport 1", "Same Name", "Same Name"])      # the file:// one and the orphan are not
        self.assertEqual((ch[0]["key"], ch[0]["group"], ch[0]["logo"]), ("news.a", "News", "http://x/l.png"))
        self.assertEqual(len({c["key"] for c in ch}), 4)                                                # two with one name stay apart


class Numbering(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.s = iptv.Sources(os.path.join(self.root, "sources.json"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def add(self, name, kind="m3u", url="http://example.com/p.m3u", channels=None, **kw):
        src = self.s.save({"name": name, "type": kind, "url": url, **kw})
        self.s.set_channels(src["id"], channels if channels is not None else iptv.parse_m3u(M3U))
        return src["id"]

    def test_channels_are_numbered_from_a_base_and_keep_their_numbers(self):
        sid = self.add("Playlist")
        plan, _ = self.s.plan(taken={"100"})
        self.assertEqual([(c["number"], c["name"]) for c in plan], [("101", "News A"), ("102", "Sport 1"), ("103", "Same Name"), ("104", "Same Name")])
        self.s.set_channels(sid, [{"key": "brand.new", "name": "New", "url": "http://x/n.ts", "group": "", "logo": ""}] + iptv.parse_m3u(M3U))
        plan, _ = self.s.plan(taken={"100"})
        self.assertEqual({c["name"]: c["number"] for c in plan}["News A"], "101")                       # unchanged by one added in front
        self.assertEqual({c["name"]: c["number"] for c in plan}["New"], "105")

    def test_a_playlists_header_names_its_guide_and_it_is_kept_with_the_source(self):
        text = '#EXTM3U url-tvg="http://g.example/a.xml, ftp://no/x.xml,https://g.example/b.xml.gz"\n#EXTINF:-1 tvg-id="a.news",News A\nhttp://x/a.ts\n'
        self.assertEqual(iptv.playlist_guide_urls(text), ["http://g.example/a.xml", "https://g.example/b.xml.gz"])
        self.assertEqual(iptv.playlist_guide_urls("#EXTM3U\n#EXTINF:-1,A\nhttp://x/a.ts\n"), [])
        sid = self.add("P", channels=iptv.Channels(iptv.parse_m3u(text)))
        chans = iptv.Channels(iptv.parse_m3u(text))
        chans.guide_urls = iptv.playlist_guide_urls(text)
        self.s.set_channels(sid, chans)
        self.assertEqual(self.s.get(sid)["guide_urls"], ["http://g.example/a.xml", "https://g.example/b.xml.gz"])

    def test_a_guide_only_tuner_puts_no_channels_in_the_lineup_and_only_tuners_can_be_guide_only(self):
        ch = [{"key": "20.1", "name": "WXYZ", "number": "20.1", "url": "http://t/auto/v20.1", "group": "", "logo": ""}]
        self.add("Antenna", kind="hdhomerun", url="http://192.168.1.20", channels=ch, mode="guide")
        self.assertEqual(self.s.plan()[0], [])
        with self.assertRaises(iptv.SourceError):
            self.s.save({"name": "P", "type": "m3u", "url": "http://example.com/p.m3u", "mode": "guide"})

    def test_each_source_gets_its_own_hundred_and_a_clash_is_moved_and_reported(self):
        self.add("A")
        self.add("B", url="http://example.com/q.m3u")
        plan, _ = self.s.plan()
        self.assertEqual(sorted({c["number"][0] for c in plan}), ["1", "2"])                            # 1xx and 2xx
        self.s.override(self.s.all()[0]["id"], "news.a", {"number": "205"})
        plan, clashes = self.s.plan()
        self.assertEqual(len({c["number"] for c in plan}), len(plan))                                   # never two the same

    def test_a_tuner_keeps_its_own_numbers_and_moves_only_when_they_clash(self):
        lineup = [{"key": "20.1", "name": "WBXX", "url": "http://t/auto/v20.1", "number": "20.1", "group": "", "logo": ""},
                  {"key": "20.2", "name": "Story", "url": "http://t/auto/v20.2", "number": "20.2", "group": "", "logo": ""}]
        self.add("Antenna", kind="hdhomerun", url="http://192.168.1.20", channels=lineup)
        plan, clashes = self.s.plan(taken={"20.2"})
        self.assertEqual([(c["number"], c["name"]) for c in plan], [("20.1", "WBXX"), ("100", "Story")])
        self.assertEqual(clashes, [{"source": "Antenna", "channel": "Story", "wanted": "20.2"}])

    def test_a_channel_can_be_switched_off_renamed_or_renumbered(self):
        sid = self.add("Playlist")
        self.s.override(sid, "news.a", {"enabled": False})
        self.s.override(sid, "Sport 1", {"name": "Sports", "number": "77"})
        plan, _ = self.s.plan()
        self.assertEqual({c["name"]: c["number"] for c in plan if c["name"] in ("Sports", "News A")}, {"Sports": "77"})
        with self.assertRaises(iptv.SourceError):
            self.s.override(sid, "Sport 1", {"number": "abc"})
        with self.assertRaises(iptv.SourceError):
            self.s.override(sid, "Sport 1", {"number": "0"})                          # Jellyfin ignores channel 0
        with self.assertRaises(iptv.SourceError):
            self.s.override(sid, "nope", {"enabled": False})
        self.s.override(sid, "news.a", {"enabled": True})
        self.assertIn("News A", [c["name"] for c in self.s.plan()[0]])

    def test_a_source_that_is_switched_off_has_no_channels_and_the_page_never_sees_a_password(self):
        sid = self.add("Cam", kind="stream", url="rtsp://admin:s3cret@10.0.0.5/live", channels=[{"key": "stream", "name": "Cam", "url": "rtsp://admin:s3cret@10.0.0.5/live", "group": "", "logo": ""}])
        self.assertEqual(len(self.s.plan()[0]), 1)
        self.s.save({"name": "Cam", "enabled": False}, sid)
        self.assertEqual(self.s.plan()[0], [])
        shown = json.dumps(self.s.describe())
        self.assertNotIn("s3cret", shown)
        self.s.save({"name": "Cam 2", "url": self.s.describe()[0]["url"]}, sid)                        # the masked one sent back changes nothing
        self.assertEqual(self.s.get(sid)["url"], "rtsp://admin:s3cret@10.0.0.5/live")

    def test_saving_checks_what_it_is_given(self):
        for bad in ({"name": "", "type": "m3u", "url": "http://x/p.m3u"}, {"name": "x", "type": "nope", "url": "http://x"},
                    {"name": "x", "type": "stream", "url": "file:///x"}, {"name": "x", "type": "stream", "url": "rtsp://x/y", "mode": "fast"}):
            with self.assertRaises(iptv.SourceError, msg=bad):
                self.s.save(bad)
        self.s.delete(self.add("Gone"))
        self.assertEqual(self.s.all(), [])
        with self.assertRaises(iptv.SourceError):
            self.s.delete("nope")


class Commands(unittest.TestCase):
    def test_a_converted_channel_is_re_encoded_and_a_relayed_one_is_copied(self):
        ch = {"url": "rtsp://cam.local/live", "mode": "convert"}
        cmd = iptv.ffmpeg_command(ch, ["-c:v", "libx264"], ["-f", "mpegts"])
        self.assertIn("-rtsp_transport", cmd)
        self.assertIn("libx264", cmd)
        self.assertEqual(cmd[-1], "pipe:1")
        self.assertIn(iptv.WHITELIST, cmd)                                  # ffmpeg may not open a local file for it
        relay = iptv.ffmpeg_command({"url": "http://t/auto/v1", "mode": "relay"}, ["-c:v", "libx264"], ["-f", "mpegts"])
        self.assertEqual(relay[relay.index("-c") + 1], "copy")
        self.assertNotIn("libx264", relay)
        self.assertIn("-reconnect", relay)


if __name__ == "__main__":
    unittest.main()
