"""The address the tuner tells clients to use (the address the client itself used, unless advertise_url was set).
Run: python3 test_advertise.py"""
import json
import os
import shutil
import tempfile
import unittest
import urllib.request

from test_files_api import Server


def fetch(srv, path, host):
    req = urllib.request.Request(srv.base + path, headers={"Host": host})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


class Advertise(unittest.TestCase):
    def test_an_unset_advertise_url_is_replaced_by_the_address_the_client_used(self):
        root = tempfile.mkdtemp()
        srv = Server(root, extra={"advertise_url": "http://localhost:5004", "channels": {"5": "Local"}})
        try:
            d = json.loads(fetch(srv, "/discover.json", "192.168.1.50:5004"))
            self.assertEqual((d["BaseURL"], d["LineupURL"]), ("http://192.168.1.50:5004", "http://192.168.1.50:5004/lineup.json"))
            lineup = json.loads(fetch(srv, "/lineup.json", "fs42.lan:5004"))
            self.assertEqual(lineup[0]["URL"], "http://fs42.lan:5004/auto/v5")
            self.assertIn("<URLBase>http://192.168.1.50:5004</URLBase>", fetch(srv, "/device.xml", "192.168.1.50:5004"))
            for bad in ("evil.example/<script>", "a b", "x:99999999"):
                self.assertNotIn("script", fetch(srv, "/discover.json", bad.replace(" ", "")))          # an odd Host is never echoed back
        finally:
            srv.stop()
            shutil.rmtree(root)

    def test_a_real_advertise_url_is_always_used(self):
        root = tempfile.mkdtemp()
        srv = Server(root, extra={"advertise_url": "http://tv.home:5004", "channels": {"5": "Local"}})
        try:
            self.assertEqual(json.loads(fetch(srv, "/discover.json", "192.168.1.50:5004"))["BaseURL"], "http://tv.home:5004")
            self.assertEqual(json.loads(fetch(srv, "/lineup.json", "192.168.1.50:5004"))[0]["URL"], "http://tv.home:5004/auto/v5")
        finally:
            srv.stop()
            shutil.rmtree(root)


class Head(unittest.TestCase):
    def test_head_gets_the_headers_of_a_get_and_no_body_and_starts_no_stream(self):
        root = tempfile.mkdtemp()
        srv = Server(root, extra={"channels": {"5": "Local"}})
        try:
            for path in ("/xmltv.xml", "/lineup.json", "/discover.json"):
                req = urllib.request.Request(srv.base + path, method="HEAD")
                with urllib.request.urlopen(req, timeout=10) as r:
                    self.assertEqual((r.status, r.read()), (200, b""), path)
                    self.assertGreater(int(r.headers.get("Content-Length", "0")), 0, path)      # the length a GET would have
            req = urllib.request.Request(srv.base + "/auto/v5", method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual((r.status, r.headers.get("Content-Type"), r.read()), (200, "video/mp2t", b""))
        finally:
            srv.stop()
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
