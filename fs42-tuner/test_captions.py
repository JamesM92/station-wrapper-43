"""Captions for the web player: finding subtitle files and tracks, WebVTT out, and the tuner's two routes.

Run: python3 test_captions.py   (the ffmpeg parts are skipped when ffmpeg is not installed)
"""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import captions
from test_files_api import Server

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello <i>there</i>\n\n2\n00:00:05,000 --> 00:00:06,500\nBye\n"
VTT = "WEBVTT\n\n00:01.000 --> 00:03.000\nHi\n"


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_video(path, subs=()):
    """A one-second video, with each (srt text, language) in `subs` as a subtitle track inside it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=5"]
    for i, (text, _lang) in enumerate(subs):
        srt = os.path.join(tempfile.gettempdir(), f"cc-test-{os.getpid()}-{i}.srt")
        with open(srt, "w") as f:
            f.write(text)
        cmd += ["-i", srt]
    cmd += ["-map", "0:v"] + sum([["-map", f"{i + 1}:0"] for i in range(len(subs))], [])
    cmd += ["-c:v", "libx264", "-c:s", "srt"]
    for i, (_t, lang) in enumerate(subs):
        cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
    subprocess.run(cmd + [path], check=True)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.v = os.path.join(self.root, "Show - 01.mkv")
        touch(self.v)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_subtitle_files_beside_the_video_are_found_with_a_label_from_their_name(self):
        touch(os.path.join(self.root, "Show - 01.srt"), SRT)
        touch(os.path.join(self.root, "Show - 01.en.vtt"), VTT)
        touch(os.path.join(self.root, "Show - 01.jpn.forced.ass"), "")
        touch(os.path.join(self.root, "Show - 02.srt"), SRT)                       # another episode's
        touch(os.path.join(self.root, "Show - 01.nfo"), "")
        got = {os.path.basename(t["file"]): (t["lang"], t["label"]) for t in captions.sidecars(self.v)}
        touch(os.path.join(self.root, "Show - 01.mkv.fr.srt"), SRT)
        got = {os.path.basename(t["file"]): (t["lang"], t["label"]) for t in captions.sidecars(self.v)}
        self.assertEqual(got.pop("Show - 01.mkv.fr.srt"), ("fr", "French"))
        self.assertEqual(got, {"Show - 01.srt": ("", "Captions"), "Show - 01.en.vtt": ("en", "English"),
                               "Show - 01.jpn.forced.ass": ("jpn", "Japanese (forced)")})

    def test_a_link_finds_the_files_beside_the_real_video(self):
        real = os.path.join(self.root, "real", "Show - 01.mkv")
        touch(real)
        touch(os.path.join(self.root, "real", "Show - 01.en.srt"), SRT)
        link = os.path.join(self.root, "links", "Show - 01.mkv")
        os.makedirs(os.path.dirname(link))
        os.symlink(real, link)
        self.assertEqual([t["lang"] for t in captions.sidecars(link)], ["en"])

    def test_a_vtt_file_is_passed_through_and_the_video_id_is_stable(self):
        touch(os.path.join(self.root, "Show - 01.en.vtt"), VTT)
        self.assertEqual(captions.vtt(self.v, "f0"), VTT)
        self.assertIsNone(captions.vtt(self.v, "f9"))
        self.assertEqual(captions.eid(self.v), captions.eid(self.v))
        self.assertNotEqual(captions.eid(self.v), captions.eid(self.v + "x"))

    def test_a_missing_video_has_no_tracks(self):
        self.assertEqual(captions.tracks(os.path.join(self.root, "nope.mkv")), [])

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_an_srt_file_becomes_webvtt(self):
        touch(os.path.join(self.root, "Show - 01.en.srt"), SRT)
        text = captions.vtt(self.v, "f0")
        self.assertTrue(text.startswith("WEBVTT"))
        self.assertIn("00:01.000 --> 00:03.000", text)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_text_tracks_inside_the_video_are_found_and_read(self):
        v = os.path.join(self.root, "inside.mkv")
        make_video(v, [(SRT, "eng"), (SRT.replace("Hello", "Bonjour"), "fre")])
        found = captions.tracks(v)
        self.assertEqual([(t["lang"], t["label"]) for t in found], [("eng", "English"), ("fre", "French")])
        self.assertIn("Bonjour", captions.vtt(v, found[1]["id"]))
        self.assertEqual(captions.public(found)[0].keys(), {"id", "label", "lang"})        # no file paths go to the page


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.video = os.path.join(cls.root, "media", "Show - 01.mkv")
        make_video(cls.video, [(SRT, "eng")])
        cls.plain = os.path.join(cls.root, "media", "Plain.mkv")
        make_video(cls.plain)
        cls.db = os.path.join(cls.root, "f.db")
        c = sqlite3.connect(cls.db)
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, start_time TEXT, end_time TEXT, title TEXT, plan_json TEXT)")
        now = datetime.now()
        for station, path in (("Anime", cls.video), ("Plain", cls.plain)):
            plan = json.dumps([{"path": path, "skip": 0, "duration": 4000.0, "is_stream": False, "content_type": "feature"}])
            c.execute("INSERT INTO liquid_blocks (station,start_time,end_time,title,plan_json) VALUES (?,?,?,?,?)",
                      (station, (now - timedelta(seconds=60)).isoformat(), (now + timedelta(hours=1)).isoformat(), "Show", plan))
        c.commit()
        c.close()
        os.makedirs(os.path.join(cls.root, "catalog", "by-genre"))
        cls.srv = Server(cls.root, extra={"db_path": cls.db, "channels": {"7": "Anime", "8": "Plain"}})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.root)

    def test_the_page_is_told_where_the_file_is_and_what_captions_it_has(self):
        code, info = self.srv.get("/captions/7.json")
        self.assertEqual(code, 200)
        self.assertEqual([(t["lang"], t["label"]) for t in info["tracks"]], [("eng", "English")])
        self.assertTrue(55 < info["pos"] < 70, info)                       # about a minute into the file
        self.assertNotIn("file", json.dumps(info))
        self.assertEqual(self.srv.get("/captions/8.json")[1]["tracks"], [])

    def test_a_track_comes_back_as_webvtt_for_the_file_that_is_on(self):
        info = self.srv.get("/captions/7.json")[1]
        url = f"{self.srv.base}/captions/7/{info['eid']}/{info['tracks'][0]['id']}.vtt"
        with urllib.request.urlopen(url, timeout=20) as r:
            body = r.read().decode()
            self.assertIn("text/vtt", r.headers["Content-Type"])
        self.assertTrue(body.startswith("WEBVTT"))
        self.assertIn("Hello", body)

    def test_only_what_is_airing_can_be_asked_for(self):
        info = self.srv.get("/captions/7.json")[1]
        for path in ("/captions/99.json", "/captions/7/deadbeef0000/s2.vtt", f"/captions/7/{info['eid']}/s9.vtt",
                     f"/captions/7/{info['eid']}/../../etc/passwd", "/captions/7/x"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                urllib.request.urlopen(self.srv.base + path, timeout=10)
            self.assertIn(cm.exception.code, (404, 400), path)


if __name__ == "__main__":
    unittest.main()


class BurnIn(unittest.TestCase):
    def test_a_track_is_picked_by_language_and_never_another(self):
        d = tempfile.mkdtemp()
        v = os.path.join(d, "Show - 01.mkv")
        open(v, "w").close()
        for n in ("Show - 01.en.srt", "Show - 01.ja.srt"):
            open(os.path.join(d, n), "w").close()
        self.assertTrue(captions.pick(v, "en")["file"].endswith("Show - 01.en.srt"))
        self.assertTrue(captions.pick(v, "eng")["file"].endswith("Show - 01.en.srt"))      # a three-letter name finds the same one
        self.assertTrue(captions.pick(v, "ja")["file"].endswith("Show - 01.ja.srt"))
        self.assertIsNone(captions.pick(v, "fr"))                                          # not the first one that is there
        self.assertIsNone(captions.pick(v, ""))

    def test_editing_a_subtitle_file_is_noticed(self):
        d = tempfile.mkdtemp()
        v = os.path.join(d, "Show - 01.mkv")
        open(v, "w").close()
        sub = os.path.join(d, "Show - 01.en.srt")
        open(sub, "w").write("1\n00:00:01,000 --> 00:00:02,000\nFIRST\n")
        self.assertIn("FIRST", captions.vtt(v, "f0"))
        open(sub, "w").write("1\n00:00:01,000 --> 00:00:02,000\nSECOND\n")
        os.utime(sub, (os.stat(sub).st_atime, os.stat(sub).st_mtime + 5))
        self.assertIn("SECOND", captions.vtt(v, "f0"))

    def test_the_filter_moves_the_clock_to_the_file_and_back_and_refuses_a_name_it_cannot_quote(self):
        t = {"file": "/m/My Show: 01.en.srt", "lang": "en"}
        f = captions.burn_filter(t, "/m/My Show: 01.mkv", 12.5)
        self.assertEqual(f, "setpts=PTS+12.500/TB,subtitles=filename='/m/My Show\\: 01.en.srt',setpts=PTS-STARTPTS")
        emb = captions.burn_filter({"index": 3, "ordinal": 1, "lang": "en"}, "/cache/x.mkv", 0)
        self.assertIn("subtitles=filename='/cache/x.mkv':si=1", emb)
        self.assertIsNone(captions.burn_filter({"file": "/m/it's.srt"}, "/m/it's.mkv", 0))
        self.assertIsNone(captions.burn_filter(None, "/m/x.mkv", 0))
