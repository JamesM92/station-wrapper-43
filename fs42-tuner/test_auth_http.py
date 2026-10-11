"""The login, end to end against the real dashboard server. Run: python3 test_auth_http.py"""
import http.client
import json
import os
import shutil
import tempfile
import unittest

from test_files_api import Server, touch


class Client:
    """A raw HTTP client: no redirects followed, cookies handled by hand, one connection reused if asked."""

    def __init__(self, srv):
        self.host, self.port = "127.0.0.1", srv.port

    def request(self, method, path, body=None, cookie=None, headers=None, raw=None, conn=None):
        c = conn or http.client.HTTPConnection(self.host, self.port, timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = f"sw43_session={cookie}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode(errors="replace")
        if conn is None:
            c.close()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        return r.status, r, parsed, text

    @staticmethod
    def session(resp):
        sc = resp.getheader("Set-Cookie") or ""
        return sc.split(";")[0].split("=", 1)[1] if sc.startswith("sw43_session=") else None


def server(env=None):
    root = tempfile.mkdtemp()
    touch(os.path.join(root, "media", "m", "a.mp4"))
    os.makedirs(os.path.join(root, "catalog", "by-genre"))
    return root, Server(root, env={"DASHBOARD_LOGIN": "1", "FS42_SETTINGS": os.path.join(root, "settings.json"), **(env or {})})


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.root, self.srv = server()
        self.c = Client(self.srv)

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.root)

    def setup_password(self, pw="correct horse"):
        code, resp, body, _ = self.c.request("POST", "/api/auth/setup", {"password": pw})
        self.assertEqual(code, 200, body)
        return Client.session(resp)

    def test_before_a_password_exists_management_is_locked_and_leads_to_setup(self):
        code, _, st, _ = self.c.request("GET", "/api/auth/status")
        self.assertEqual((code, st["login"], st["has_password"], st["setup_needed"], st["authenticated"]),
                         (200, True, False, True, False))
        code, _, body, _ = self.c.request("GET", "/api/stations")
        self.assertEqual((code, body["setup"], body["login"]), (401, True, "/login.html"))
        for page in ("/stations.html", "/station.html", "/schedule.html", "/files.html"):
            code, resp, _, _ = self.c.request("GET", page)
            self.assertEqual(code, 302, page)
            self.assertEqual(resp.getheader("Location"), "/login.html?next=" + page.replace("/", "%2F"))
        self.assertEqual(self.c.request("GET", "/stations.html?name=A%20B")[1].getheader("Location"),
                         "/login.html?next=%2Fstations.html%3Fname%3DA%2520B")             # the whole address, encoded

    def test_every_api_call_needs_the_login(self):
        for path in ("/api/files/info", "/api/files/list?root=media", "/api/ta/status", "/api/keeper", "/api/schedule?name=x",
                     "/api/stations", "/api/stations/get?name=x"):
            self.assertEqual(self.c.request("GET", path)[0], 401, path)
        for path in ("/api/files/create-tag", "/api/ta/settings", "/api/keeper/settings", "/api/stations/save",
                     "/api/stations/delete", "/api/stations/build"):
            self.assertEqual(self.c.request("POST", path, {"x": 1})[0], 401, path)
        self.assertFalse(os.path.exists(os.path.join(self.root, "catalog", "by-genre", "x")))

    def test_what_clients_and_viewers_need_stays_open(self):
        for path in ("/", "/watch.html", "/login.html", "/login.js", "/channels.json", "/lineup.json", "/discover.json",
                     "/lineup_status.json", "/status.json", "/library.json", "/style.css", "/common.js"):
            self.assertEqual(self.c.request("GET", path)[0], 200, path)

    def test_a_blocked_post_does_not_poison_the_connection(self):
        conn = http.client.HTTPConnection(self.c.host, self.c.port, timeout=10)
        code, *_ = self.c.request("POST", "/api/files/create-tag", {"name": "x" * 2000}, conn=conn)
        self.assertEqual(code, 401)
        code, _, st, _ = self.c.request("GET", "/api/auth/status", conn=conn)       # the same connection still works
        self.assertEqual(code, 200)
        conn.close()

    def test_setting_the_password_logs_you_in_with_a_safe_cookie_and_can_only_happen_once(self):
        code, resp, _, _ = self.c.request("POST", "/api/auth/setup", {"password": "short"})
        self.assertEqual(code, 400)
        self.assertIsNone(Client.session(resp))
        code, resp, _, _ = self.c.request("POST", "/api/auth/setup", {"password": "correct horse"})
        self.assertEqual(code, 200)
        cookie = resp.getheader("Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age="):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)                                   # plain http here
        token = Client.session(resp)
        self.assertEqual(self.c.request("GET", "/api/files/info", cookie=token)[0], 200)
        code, _, body, _ = self.c.request("POST", "/api/auth/setup", {"password": "someone else"})
        self.assertEqual(code, 409)                                          # a second visitor cannot take over
        self.assertIn("already set", body["error"])
        self.assertEqual(self.c.request("GET", "/stations.html", cookie=token)[0], 200)

    def test_behind_https_the_cookie_is_marked_secure(self):
        _, resp, _, _ = self.c.request("POST", "/api/auth/setup", {"password": "correct horse"},
                                       headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", resp.getheader("Set-Cookie"))

    def test_login_and_wrong_passwords(self):
        self.setup_password()
        code, resp, body, _ = self.c.request("POST", "/api/auth/login", {"password": "wrong one"})
        self.assertEqual((code, Client.session(resp)), (401, None))
        self.assertIn("not right", body["error"])
        code, resp, _, _ = self.c.request("POST", "/api/auth/login", {"password": "correct horse"})
        token = Client.session(resp)
        self.assertEqual((code, bool(token)), (200, True))
        self.assertEqual(self.c.request("GET", "/api/files/info", cookie=token)[0], 200)
        self.assertTrue(self.c.request("GET", "/api/auth/status", cookie=token)[2]["authenticated"])

    def test_a_forged_or_empty_cookie_gets_nowhere(self):
        token = self.setup_password()
        for bad in ("", "x", token + "x", "1.2.3", token[:-4] + "0000"):
            self.assertEqual(self.c.request("GET", "/api/files/info", cookie=bad)[0], 401, repr(bad))
        code, _, st, _ = self.c.request("GET", "/api/auth/status", cookie="junk")
        self.assertFalse(st["authenticated"])

    def test_guessing_is_slowed_down(self):
        self.setup_password()
        for _ in range(5):
            self.assertEqual(self.c.request("POST", "/api/auth/login", {"password": "nope nope"})[0], 401)
        code, resp, body, _ = self.c.request("POST", "/api/auth/login", {"password": "correct horse"})
        self.assertEqual(code, 429)                                          # even the right one while it is blocked
        self.assertTrue(int(resp.getheader("Retry-After")) > 0)
        self.assertIn("too many", body["error"])

    def test_logout_ends_the_session_everywhere(self):
        token = self.setup_password()
        code, resp, _, _ = self.c.request("POST", "/api/auth/logout", {}, cookie=token)
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", resp.getheader("Set-Cookie"))
        self.assertEqual(self.c.request("GET", "/api/files/info", cookie=token)[0], 401)     # a copied cookie is dead too

    def test_changing_the_password(self):
        token = self.setup_password()
        self.assertEqual(self.c.request("POST", "/api/auth/password", {"current": "correct horse", "new": "a new one!!"})[0], 401)
        code, _, body, _ = self.c.request("POST", "/api/auth/password", {"current": "not it", "new": "a new one!!"}, cookie=token)
        self.assertEqual(code, 403)
        code, _, body, _ = self.c.request("POST", "/api/auth/password", {"current": "correct horse", "new": "short"}, cookie=token)
        self.assertEqual(code, 400)
        code, resp, _, _ = self.c.request("POST", "/api/auth/password", {"current": "correct horse", "new": "a new one!!"}, cookie=token)
        self.assertEqual(code, 200)
        fresh = Client.session(resp)
        self.assertEqual(self.c.request("GET", "/api/files/info", cookie=token)[0], 401)     # the old session is gone
        self.assertEqual(self.c.request("GET", "/api/files/info", cookie=fresh)[0], 200)
        self.assertEqual(self.c.request("POST", "/api/auth/login", {"password": "correct horse"})[0], 401)
        self.assertEqual(self.c.request("POST", "/api/auth/login", {"password": "a new one!!"})[0], 200)

    def test_the_auth_calls_take_json_only(self):
        for path in ("/api/auth/setup", "/api/auth/login"):
            code, *_ = self.c.request("POST", path, raw=b'{"password": "correct horse"}', headers={"Content-Type": "text/plain"})
            self.assertEqual(code, 415, path)
        self.assertFalse(Client.session(self.c.request("POST", "/api/auth/login", raw=b"not json")[1]))

    def test_the_password_survives_a_restart_of_the_server(self):
        self.setup_password()
        self.srv.stop()
        self.srv = Server(self.root, env={"DASHBOARD_LOGIN": "1", "FS42_SETTINGS": os.path.join(self.root, "settings.json")})
        self.c = Client(self.srv)
        self.assertFalse(self.c.request("GET", "/api/auth/status")[2]["setup_needed"])
        self.assertEqual(self.c.request("POST", "/api/auth/login", {"password": "correct horse"})[0], 200)
        self.assertEqual(oct(os.stat(os.path.join(self.root, "auth.json")).st_mode & 0o777), "0o600")


class OtherModesTest(unittest.TestCase):
    def test_login_switched_off_leaves_everything_open_and_says_so(self):
        root, srv = server({"DASHBOARD_LOGIN": "0"})
        try:
            c = Client(srv)
            self.assertEqual(c.request("GET", "/api/files/info")[0], 200)
            self.assertEqual(c.request("GET", "/stations.html")[0], 200)
            st = c.request("GET", "/api/auth/status")[2]
            self.assertEqual((st["login"], st["authenticated"], st["setup_needed"]), (False, True, False))
            code, _, body, _ = c.request("POST", "/api/auth/setup", {"password": "correct horse"})
            self.assertEqual(code, 400)
            self.assertIn("switched off", body["error"])
        finally:
            srv.stop()
            shutil.rmtree(root)

    def test_a_password_can_be_given_in_the_environment(self):
        root, srv = server({"DASHBOARD_PASSWORD": "from the environment"})
        try:
            c = Client(srv)
            st = c.request("GET", "/api/auth/status")[2]
            self.assertEqual((st["has_password"], st["setup_needed"]), (True, False))
            self.assertEqual(c.request("POST", "/api/auth/login", {"password": "from the environment"})[0], 200)
            self.assertEqual(c.request("GET", "/api/files/info")[0], 401)                  # still locked without logging in
        finally:
            srv.stop()
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
