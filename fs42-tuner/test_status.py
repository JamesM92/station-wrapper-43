"""The Status page's API, the diagnostics download and /lineup.m3u, against a real tuner process.
Run: python3 test_status.py"""
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

import diag
from test_files_api import Server


class DiagUnitTest(unittest.TestCase):
    def test_redact_hides_secrets_but_keeps_the_rest(self):
        out = diag.redact({"url": "x", "password": "p", "nested": {"api_token": "t", "n": 1}, "list": [{"secret": "s"}]})
        self.assertEqual(out["url"], "x")
        self.assertNotEqual(out["password"], "p")
        self.assertNotEqual(out["nested"]["api_token"], "t")
        self.assertNotEqual(out["list"][0]["secret"], "s")
        self.assertEqual(out["nested"]["n"], 1)

    def test_checks_call_out_a_dead_console_and_an_empty_report_is_clean(self):
        bad = diag.checks({"console": {"reachable": False, "error": "refused"}, "stations": [], "sources": [], "media": {"present": True},
                           "ffmpeg": {"found": True}, "disk": {}, "guide": {}})
        self.assertEqual(bad[0]["level"], "error")
        self.assertIn("refused", bad[0]["text"])

    def test_a_check_that_has_a_fix_says_where_to_go(self):
        rows = diag.checks({"console": {"reachable": True}, "stations": [{"name": "A", "type": "standard", "scheduled_until": None}],
                            "sources": [], "media": {"present": False, "path": "/m"}, "ffmpeg": {"found": True}, "disk": {}, "guide": {}})
        fixes = {r["text"].split(":")[0][:20]: r.get("href") for r in rows}
        self.assertTrue(any(r.get("href") == "/stations.html" for r in rows))
        self.assertTrue(any(r.get("href") == "/files.html" for r in rows))
        self.assertTrue(all(r.get("fix") for r in rows if r.get("href")), fixes)

    def test_the_log_ring_keeps_what_was_printed(self):
        ring = diag.LogRing()
        ring.add("hello"); ring.add("Traceback: boom")
        self.assertEqual(len(ring.recent(10)), 2)
        self.assertEqual([r["line"] for r in ring.recent(10, problems_only=True)], ["Traceback: boom"])

    def test_speed_test_reports_an_error_without_media(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIn("error", diag.speed_test(d))
        finally:
            shutil.rmtree(d)


class StatusHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.srv = Server(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.root)

    def test_status_has_the_sections_the_page_draws(self):
        code, r = self.srv.get("/api/system/status")
        self.assertEqual(code, 200)
        for key in ("version", "uptime_seconds", "console", "stations", "guide", "sources", "media", "ffmpeg", "disk",
                    "encoders", "viewers", "cpu", "recent_problems", "checks"):
            self.assertIn(key, r)
        self.assertTrue(all(c["level"] in ("error", "warn", "ok") for c in r["checks"]))

    def test_diagnostics_is_a_text_attachment(self):
        with urllib.request.urlopen(self.srv.base + "/api/system/diagnostics", timeout=20) as resp:
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            self.assertTrue(resp.read().decode("utf-8").strip())

    def test_m3u_lists_each_lineup_channel_with_the_id_the_guide_uses(self):
        with urllib.request.urlopen(self.srv.base + "/lineup.m3u", timeout=20) as resp:
            self.assertIn("mpegurl", resp.headers["Content-Type"])
            text = resp.read().decode("utf-8")
        self.assertTrue(text.startswith("#EXTM3U\n"))
        with urllib.request.urlopen(self.srv.base + "/lineup.json", timeout=20) as resp:
            lineup = json.load(resp)
        ids = re.findall(r'tvg-id="([^"]+)"', text)
        self.assertEqual(len(ids), len(lineup))
        with urllib.request.urlopen(self.srv.base + "/xmltv.xml", timeout=60) as resp:
            xml = resp.read().decode("utf-8")
        for cid in ids:
            self.assertIn(f'<channel id="{cid}"', xml)

    def test_a_videos_picture_is_served_for_the_guide_and_only_from_the_catalog(self):
        import os
        view = os.path.join(self.root, "view")
        lst = os.path.join(self.root, "catalog", "by-genre", "List")
        os.makedirs(view)
        os.makedirs(lst)
        open(os.path.join(view, "ep.mp4"), "w").close()
        with open(os.path.join(view, "ep-thumb.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff fake jpeg")
        os.symlink(os.path.join(view, "ep.mp4"), os.path.join(lst, "ep.mp4"))
        with urllib.request.urlopen(self.srv.base + "/art/catalog/by-genre/List/ep.mp4", timeout=10) as r:
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, "image/jpeg"))
            self.assertTrue(r.read().startswith(b"\xff\xd8"))
        for bad in ("/art/catalog/by-genre/List/missing.mp4", "/art/../etc/passwd", "/art//etc/hosts"):
            try:
                urllib.request.urlopen(self.srv.base + bad, timeout=10)
                self.fail(bad)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404, bad)

    def test_a_channel_has_a_logo_drawn_from_its_name_unless_it_names_one(self):
        import os
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "media", "logos"))
        with open(os.path.join(root, "media", "logos", "mine.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n custom")
        srv = Server(root, extra={"channels": {"7": "Cartoons", "8": "Movies"}})
        try:
            with urllib.request.urlopen(srv.base + "/xmltv.xml", timeout=60) as r:
                xml = r.read().decode()
            self.assertNotIn("@@BASE@@", xml)
            self.assertIn(f'src="{srv.base}/logo/7.png"', xml)                 # on the address it was reached on
            with urllib.request.urlopen(srv.base + "/logo/7.png", timeout=10) as r:
                self.assertEqual(r.headers["Content-Type"], "image/png")
                self.assertTrue(r.read().startswith(b"\x89PNG"))
            for bad in ("/logo/99.png", "/logo/x.png"):
                try:
                    urllib.request.urlopen(srv.base + bad, timeout=10)
                    self.fail(bad)
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404)
        finally:
            srv.stop()
            shutil.rmtree(root)

    def test_the_status_page_is_served(self):
        with urllib.request.urlopen(self.srv.base + "/status.html", timeout=10) as resp:
            self.assertIn(b"Status", resp.read())


if __name__ == "__main__":
    unittest.main()
