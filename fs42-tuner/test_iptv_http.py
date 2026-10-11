"""The Sources page's API and the channels it adds, against a real tuner process and a local stream.
Run: python3 test_iptv_http.py   (the playing part needs ffmpeg and is skipped without it)"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from test_files_api import Server


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class SourcesHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.www = os.path.join(cls.root, "www")
        os.makedirs(cls.www)
        cls.have_ffmpeg = bool(shutil.which("ffmpeg"))
        if cls.have_ffmpeg:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=500",
                            "-t", "20", "-c:v", "mpeg2video", "-b:v", "1M", "-c:a", "ac3", "-f", "mpegts", os.path.join(cls.www, "cam.ts")], check=True)
        cls.web = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=cls.www))
        cls.port = cls.web.server_address[1]
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        with open(os.path.join(cls.www, "list.m3u"), "w") as f:
            f.write(f'#EXTM3U\n#EXTINF:-1 tvg-id="a" group-title="Cams",Camera A\nhttp://127.0.0.1:{cls.port}/cam.ts\n'
                    f'#EXTINF:-1,Camera B\nhttp://127.0.0.1:{cls.port}/cam.ts\n')
        cls.srv = Server(cls.root, env={"FS42_IPTV": os.path.join(cls.root, "sources.json")})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.web.shutdown()
        shutil.rmtree(cls.root)

    def sources(self):
        return self.srv.get("/api/iptv/sources")[1]

    def lineup(self):
        with urllib.request.urlopen(self.srv.base + "/lineup.json", timeout=10) as r:
            return {c["GuideNumber"]: c for c in json.load(r)}

    def test_a_stream_and_a_playlist_become_channels_marked_as_external_sources(self):
        code, out = self.srv.post("/api/iptv/save", {"name": "Front door", "type": "stream", "url": f"http://127.0.0.1:{self.port}/cam.ts"})
        self.assertEqual(code, 200)
        code, pl = self.srv.post("/api/iptv/save", {"name": "Playlist", "type": "m3u", "url": f"http://127.0.0.1:{self.port}/list.m3u"})
        self.assertEqual(code, 200)
        lineup = self.lineup()
        self.assertEqual(lineup["100"]["GuideName"], "Front door")
        self.assertEqual((lineup["200"]["GuideName"], lineup["201"]["GuideName"]), ("Camera A", "Camera B"))
        self.assertFalse(lineup["100"]["Favorite"])                                  # not one of the FieldStation42 channels
        with urllib.request.urlopen(self.srv.base + "/channels.json", timeout=10) as r:
            rows = {x["channel"]: x for x in json.load(r)}
        self.assertEqual(rows["200"]["external"], {"source": "Playlist", "type": "m3u", "mode": "convert"})
        self.assertTrue(rows["200"]["on_air"])
        for page in ("/iptv.html", "/iptv.js", "/iptv.css"):
            self.assertEqual(urllib.request.urlopen(self.srv.base + page, timeout=10).status, 200)
        # a channel switched off leaves the lineup; switched on it comes back with the same number
        self.assertEqual(self.srv.post("/api/iptv/channel", {"id": pl["id"], "key": "a", "enabled": False})[0], 200)
        self.assertNotIn("200", self.lineup())
        self.assertEqual(self.srv.post("/api/iptv/channel", {"id": pl["id"], "key": "a", "enabled": True})[0], 200)
        self.assertEqual(self.lineup()["200"]["GuideName"], "Camera A")
        # taking a source away takes its channels away
        self.assertEqual(self.srv.post("/api/iptv/delete", {"id": pl["id"]})[0], 200)
        self.assertNotIn("200", self.lineup())
        self.assertIn("100", self.lineup())
        self.srv.post("/api/iptv/delete", {"id": out["id"]})
        self.assertNotIn("100", self.lineup())

    def test_a_bad_address_is_refused_and_a_password_is_never_shown_again(self):
        for url in ("file:///etc/passwd", "rtsp://x@", "javascript:alert(1)", ""):
            code, out = self.srv.post("/api/iptv/save", {"name": "Bad", "type": "stream", "url": url})
            self.assertEqual(code, 400, url)
            self.assertIn("error", out)
        code, out = self.srv.post("/api/iptv/save", {"name": "Cam", "type": "stream", "url": "rtsp://admin:s3cret@127.0.0.1:9/live"})
        self.assertEqual(code, 200)
        self.assertNotIn("s3cret", json.dumps(self.sources()))
        self.srv.post("/api/iptv/delete", {"id": out["id"]})

    def test_a_source_that_cannot_be_read_says_why_and_keeps_its_place(self):
        code, out = self.srv.post("/api/iptv/save", {"name": "Dead tuner", "type": "hdhomerun", "url": "http://127.0.0.1:9"})
        self.assertEqual(code, 200)
        src = next(s for s in self.sources()["sources"] if s["id"] == out["id"])
        self.assertIn("could not read", src["error"])
        self.assertEqual(src["channels"], [])
        self.srv.post("/api/iptv/delete", {"id": out["id"]})

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_stream_is_played_as_something_a_browser_can_play(self):
        code, out = self.srv.post("/api/iptv/save", {"name": "Front door", "type": "stream", "url": f"http://127.0.0.1:{self.port}/cam.ts"})
        try:
            got = os.path.join(self.root, "got.ts")
            with urllib.request.urlopen(self.srv.base + "/auto/v100", timeout=20) as r, open(got, "wb") as f:
                n = 0
                while n < 400_000:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", got], capture_output=True, text=True).stdout
            self.assertIn("h264", probe)
            self.assertIn("aac", probe)
        finally:
            self.srv.post("/api/iptv/delete", {"id": out["id"]})


class EditOffTest(unittest.TestCase):
    def test_with_editing_switched_off_the_sources_can_be_seen_but_not_changed(self):
        root = tempfile.mkdtemp()
        srv = Server(root, env={"FS42_IPTV": os.path.join(root, "sources.json"), "DASHBOARD_EDIT": "0"})
        try:
            self.assertEqual(srv.get("/api/iptv/sources")[1]["edit"], False)
            self.assertEqual(srv.post("/api/iptv/save", {"name": "x", "type": "stream", "url": "http://127.0.0.1/x"})[0], 403)
        finally:
            srv.stop()
            shutil.rmtree(root)




class LegacyImportTest(unittest.TestCase):
    def test_the_old_passthrough_setting_is_moved_onto_the_sources_page_at_start(self):
        root = tempfile.mkdtemp()
        srv = Server(root, extra={"passthrough": {"url": "http://127.0.0.1:9", "mode": "direct"}},
                     env={"FS42_IPTV": os.path.join(root, "sources.json")})
        try:
            out = srv.get("/api/iptv/sources")[1]
            self.assertEqual([s["type"] for s in out["sources"]], ["hdhomerun"])
            self.assertIsNone(out["legacy"])
            with open(srv.conf_path) as f:
                self.assertNotIn("passthrough", json.load(f))
        finally:
            srv.stop()
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
