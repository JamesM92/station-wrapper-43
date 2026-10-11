"""The file manager over HTTP, against the real server. Run: python3 test_files_api.py"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Server:
    def __init__(self, root, env=None, extra=None):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        conf = {"db_path": os.path.join(root, "none.db"), "catalog_root": root, "channels": {},
                "media_root": os.path.join(root, "media"), "port": self.port, "advertise_url": self.base,
                "confs_dir": os.path.join(root, "confs")}
        conf.update(extra or {})
        self.conf_path = os.path.join(root, "tuner.json")
        with open(self.conf_path, "w") as f:
            json.dump(conf, f)
        self.proc = subprocess.Popen([sys.executable, "tuner.py"], cwd=os.path.dirname(os.path.abspath(__file__)),
                                     env={**os.environ, "FS42_TUNER_CONF": self.conf_path, "DASHBOARD_LOGIN": "0", **(env or {})},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(450):                      # up to 90 s: a busy CI runner can take a while to start a Python server
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("the tuner did not start")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def post(self, path, body, ctype="application/json", raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        touch(os.path.join(cls.root, "media", "anime", "Show", "e1.mp4"), "V")
        os.makedirs(os.path.join(cls.root, "catalog", "by-genre"))
        cls.srv = Server(os.path.join(cls.root), env={"FS42_FOLLOWS": os.path.join(cls.root, "follows.json")})
        # catalog_root is the parent of "catalog": the tuner looks in <catalog_root>/catalog/by-genre

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.root)

    def tags(self):
        return sorted(os.listdir(os.path.join(self.root, "catalog", "by-genre")))

    def test_the_whole_round_trip(self):
        code, info = self.srv.get("/api/files/info")
        self.assertEqual((code, info["edit"], info["roots"]["media"]), (200, True, True))
        code, out = self.srv.get("/api/files/list?root=media&path=anime")
        self.assertEqual([(e["label"], e["name"]) for e in out["entries"]], [("Show", "Show/e1.mp4")])   # one video: shown as itself
        self.assertEqual(self.srv.post("/api/files/create-tag", {"name": "Cartoons"})[0], 200)
        code, out = self.srv.post("/api/files/add", {"tag": "Cartoons", "sources": [{"root": "media", "path": "anime/Show"}]})
        self.assertEqual((code, out["added"]), (200, 1))
        link = os.path.join(self.root, "catalog", "by-genre", "Cartoons", "Show", "e1.mp4")
        self.assertTrue(os.path.islink(link))
        code, out = self.srv.get("/api/files/list?root=tags&path=Cartoons/Show")
        self.assertEqual(out["entries"][0]["link"], os.path.join(self.root, "media", "anime", "Show", "e1.mp4"))
        code, out = self.srv.post("/api/files/remove", {"tag": "Cartoons", "paths": ["Show/e1.mp4"]})
        self.assertEqual((code, out["removed"]), (200, 1))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "media", "anime", "Show", "e1.mp4")))
        self.assertEqual(self.srv.post("/api/files/rename-tag", {"tag": "Cartoons", "to": "Toons"})[0], 200)
        self.assertEqual(self.srv.post("/api/files/delete-tag", {"tag": "Toons"})[0], 200)
        self.assertEqual(self.tags(), [])

    def test_a_followed_folder_is_listed_refreshed_and_stopped(self):
        touch(os.path.join(self.root, "media", "tv", "Series", "e1.mp4"), "V")
        self.srv.post("/api/files/create-tag", {"name": "Follow"})
        code, out = self.srv.post("/api/files/add", {"tag": "Follow", "follow": True,
                                                      "sources": [{"root": "media", "path": "tv/Series"}]})
        self.assertEqual((code, out["following"]), (200, ["Series"]))
        f = self.srv.get("/api/files/follows?tag=Follow")[1]["follows"]
        self.assertEqual([(x["path"], x["root"]) for x in f], [("tv/Series", "media")])
        touch(os.path.join(self.root, "media", "tv", "Series", "e2.mp4"), "V")
        code, out = self.srv.post("/api/files/follows/refresh", {})
        self.assertEqual((code, out["added"]), (200, 1))
        self.assertEqual(self.srv.post("/api/files/unfollow", {"tag": "Follow", "root": "media", "path": "tv/Series"})[0], 200)
        self.assertEqual(self.srv.get("/api/files/follows?tag=Follow")[1]["follows"], [])
        self.srv.post("/api/files/delete-tag", {"tag": "Follow"})

    def test_errors_come_back_as_a_message_with_the_right_status(self):
        self.assertEqual(self.srv.post("/api/files/create-tag", {"name": "a/b"})[0], 400)
        code, out = self.srv.post("/api/files/add", {"tag": "Nope", "sources": [{"root": "media", "path": "anime"}]})
        self.assertEqual(code, 404)
        self.assertIn("no list called", out["error"])
        self.assertEqual(self.srv.get("/api/files/list?root=media&path=..")[0], 400)
        self.assertEqual(self.srv.get("/api/files/nothing")[0], 404)
        self.assertEqual(self.srv.post("/api/files/nothing", {})[0], 404)

    def test_only_json_posts_are_taken_so_another_website_cannot_drive_it(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            self.assertEqual(self.srv.post("/api/files/create-tag", None, ctype=ctype,
                                           raw=b'{"name": "Sneaky"}')[0], 415, ctype)
        self.assertNotIn("Sneaky", self.tags())

    def test_bad_bodies(self):
        self.assertEqual(self.srv.post("/api/files/create-tag", None, raw=b"not json")[0], 400)
        self.assertEqual(self.srv.post("/api/files/create-tag", None, raw=b"[1,2]")[0], 400)


class SwitchedOffTest(unittest.TestCase):
    def test_editing_can_be_switched_off_and_browsing_still_works(self):
        for how in ({"env": {"DASHBOARD_EDIT": "0"}}, {"extra": {"edit": False}}):
            root = tempfile.mkdtemp()
            try:
                touch(os.path.join(root, "media", "anime", "Show", "e1.mp4"))
                os.makedirs(os.path.join(root, "catalog", "by-genre"))
                srv = Server(root, **how)
                try:
                    self.assertFalse(srv.get("/api/files/info")[1]["edit"])
                    self.assertEqual(srv.get("/api/files/list?root=media")[0], 200)
                    code, out = srv.post("/api/files/create-tag", {"name": "X"})
                    self.assertEqual(code, 403)
                    self.assertIn("switched off", out["error"])
                    self.assertEqual(os.listdir(os.path.join(root, "catalog", "by-genre")), [])
                finally:
                    srv.stop()
            finally:
                shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
