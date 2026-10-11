"""Tests for auth.py. Run: python3 test_auth.py"""
import json
import os
import shutil
import stat
import tempfile
import unittest

import auth
from auth import Auth, AuthError


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "auth.json")
        self.clock = Clock()
        self.a = Auth(self.path, clock=self.clock)
        self.saved = auth.ITERATIONS
        auth.ITERATIONS = 1000                  # fast tests; the real value is 200,000

    def tearDown(self):
        auth.ITERATIONS = self.saved
        shutil.rmtree(self.dir)

    def test_no_password_until_one_is_set(self):
        self.assertFalse(self.a.has_password())
        self.assertFalse(self.a.verify("anything at all"))
        self.assertFalse(self.a.check_session(self.a.make_session()))     # nothing to sign with: nothing is valid
        self.a.set_password("correct horse")
        self.assertTrue(self.a.has_password())

    def test_the_password_is_stored_hashed_and_the_file_is_private(self):
        self.a.set_password("correct horse")
        raw = open(self.path).read()
        self.assertNotIn("correct horse", raw)
        data = json.loads(raw)
        self.assertTrue(data["hash"].startswith("pbkdf2_sha256$"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".auth-")], [])    # no temp left

    def test_only_the_right_password_verifies(self):
        self.a.set_password("correct horse")
        self.assertTrue(self.a.verify("correct horse"))
        for bad in ("correct hors", "Correct horse", "", " correct horse", None, 5, "x" * 500):
            self.assertFalse(self.a.verify(bad), repr(bad))

    def test_two_passwords_the_same_are_stored_differently(self):
        self.a.set_password("correct horse")
        first = json.load(open(self.path))["hash"]
        self.a.set_password("correct horse")
        self.assertNotEqual(first, json.load(open(self.path))["hash"])      # a fresh salt each time

    def test_new_passwords_are_checked(self):
        for bad in ("short", "", None, 12345678, " leading space", "trailing space ", "x" * 201):
            with self.assertRaises(AuthError, msg=repr(bad)):
                self.a.set_password(bad)
        self.assertFalse(os.path.exists(self.path))
        self.a.set_password("a perfectly fine one")

    def test_a_session_works_until_it_expires(self):
        self.a.set_password("correct horse")
        s = self.a.make_session()
        self.assertTrue(self.a.check_session(s))
        self.clock.now += auth.SESSION_SECONDS - 5
        self.assertTrue(self.a.check_session(s))
        self.clock.now += 10
        self.assertFalse(self.a.check_session(s))

    def test_a_session_cannot_be_forged_or_tampered_with(self):
        self.a.set_password("correct horse")
        s = self.a.make_session()
        expiry, nonce, sig = s.split(".")
        for forged in (f"{int(expiry) + 99999}.{nonce}.{sig}",                     # extend the expiry
                       f"{expiry}.{nonce}.{'0' * len(sig)}", f"{expiry}.{nonce}", "", "a.b.c", "1.2.3.4",
                       f"{expiry}.{nonce}.{sig}x", None, 5):
            self.assertFalse(self.a.check_session(forged), repr(forged))
        other = Auth(os.path.join(self.dir, "other.json"), clock=self.clock)
        other.set_password("correct horse")
        self.assertFalse(self.a.check_session(other.make_session()))               # signed with someone else's key

    def test_logging_out_kills_the_session_everywhere_and_survives_a_restart(self):
        self.a.set_password("correct horse")
        s, other = self.a.make_session(), self.a.make_session()
        self.a.revoke(s)
        self.assertFalse(self.a.check_session(s))                       # even though the cookie is still correctly signed
        self.assertTrue(self.a.check_session(other))                    # other sessions are not affected
        again = Auth(self.path, clock=self.clock)                       # a restart: a new object reading the same file
        self.assertFalse(again.check_session(s))
        self.assertTrue(again.check_session(other))

    def test_revoked_entries_are_forgotten_once_they_would_have_expired(self):
        self.a.set_password("correct horse")
        old = self.a.make_session()
        self.a.revoke(old)
        self.clock.now += auth.SESSION_SECONDS + 10
        self.a.revoke(self.a.make_session())                            # the next logout tidies the list
        self.assertEqual(len(json.load(open(self.path))["revoked"]), 1)

    def test_revoking_junk_changes_nothing(self):
        self.a.set_password("correct horse")
        before = open(self.path).read()
        for junk in (None, "", "a.b.c", "1.2", 5):
            self.a.revoke(junk)
        self.assertEqual(open(self.path).read(), before)

    def test_changing_the_password_signs_everyone_out(self):
        self.a.set_password("correct horse")
        s = self.a.make_session()
        self.a.set_password("a different one")
        self.assertFalse(self.a.check_session(s))
        self.assertTrue(self.a.check_session(self.a.make_session()))

    def test_login_gives_a_session_for_the_right_password_only(self):
        self.a.set_password("correct horse")
        with self.assertRaises(AuthError) as cm:
            self.a.login("wrong one", "1.2.3.4")
        self.assertIn("not right", str(cm.exception))
        self.assertTrue(self.a.check_session(self.a.login("correct horse", "1.2.3.4")))

    def test_guessing_is_limited_per_address_and_forgiven_after_a_while(self):
        self.a.set_password("correct horse")
        for _ in range(auth.FAIL_LIMIT):
            with self.assertRaises(AuthError):
                self.a.login("nope nope", "6.6.6.6")
        with self.assertRaises(AuthError) as cm:
            self.a.login("correct horse", "6.6.6.6")                    # even the right one is refused while blocked
        self.assertIn("too many", str(cm.exception))
        self.assertTrue(self.a.check_session(self.a.login("correct horse", "7.7.7.7")))   # another address is fine
        self.clock.now += auth.FAIL_WINDOW + 1
        self.assertTrue(self.a.check_session(self.a.login("correct horse", "6.6.6.6")))   # the block lapses

    def test_a_good_login_clears_the_count(self):
        self.a.set_password("correct horse")
        for _ in range(auth.FAIL_LIMIT - 1):
            with self.assertRaises(AuthError):
                self.a.login("nope nope", "8.8.8.8")
        self.a.login("correct horse", "8.8.8.8")
        for _ in range(auth.FAIL_LIMIT - 1):                            # a fresh allowance
            with self.assertRaises(AuthError):
                self.a.login("nope nope", "8.8.8.8")

    def test_a_damaged_file_never_raises_and_never_lets_anyone_in(self):
        self.a.set_password("correct horse")
        for text in ("{not json", "[]", '{"hash": 5, "secret": "x"}', '{"hash": "a$b", "secret": "x"}', ""):
            with open(self.path, "w") as f:
                f.write(text)
            self.assertFalse(self.a.verify("correct horse"), text)
            self.assertFalse(self.a.check_session("1.2.3"), text)


if __name__ == "__main__":
    unittest.main()
