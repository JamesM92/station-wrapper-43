"""Login for the dashboard's management pages.

One password for the whole dashboard (no user accounts). It is stored salted and slow-hashed (PBKDF2) in
auth.json in the config volume, never in clear. A successful login gives a signed session cookie (HttpOnly,
SameSite=Strict). Changing the password changes the signing key, which signs everyone out. Wrong guesses are
rate-limited per address.

What this protects is decided in tuner.py: the management pages and every /api/ call. Watching (the player,
the channel list) and the HDHomeRun and guide endpoints that Jellyfin and other clients use stay open, since
those clients cannot log in. FieldStation42's own console (a separate program on its own port) is NOT covered.
"""
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

ITERATIONS = 200_000
MIN_LENGTH = 8
MAX_LENGTH = 200
SESSION_SECONDS = 30 * 24 * 3600
FAIL_LIMIT = 5                  # wrong passwords per address ...
FAIL_WINDOW = 300               # ... within this many seconds, then it must wait


class AuthError(ValueError):
    """The text is for the person logging in."""


def _hash(password, salt, iterations=None):
    # read at call time, so what is written to the file and what is computed can never disagree
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations or ITERATIONS)


class Auth:
    def __init__(self, path, enabled=True, clock=time.time):
        self.path, self.enabled, self.clock = path, enabled, clock
        self._fails, self._lock = {}, threading.Lock()

    # ---- the stored password
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def has_password(self):
        d = self._load()
        return bool(d.get("hash")) and bool(d.get("secret"))

    def _save(self, data):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".auth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)                 # the hash is for this program and the folder's owner only
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if os.geteuid() == 0:                    # we run as root; hand it to whoever owns the config folder
            try:
                ref = os.stat(folder)
                os.chown(self.path, ref.st_uid, ref.st_gid)
            except OSError:
                pass

    @staticmethod
    def check_new(password):
        if not isinstance(password, str) or len(password) < MIN_LENGTH:
            raise AuthError(f"the password must be at least {MIN_LENGTH} characters")
        if len(password) > MAX_LENGTH:
            raise AuthError(f"the password can be at most {MAX_LENGTH} characters")
        if password.strip() != password:
            raise AuthError("the password can't start or end with a space")

    def set_password(self, password):
        """Set (or replace) the password. A new signing key is made, so every earlier session stops working."""
        self.check_new(password)
        salt = secrets.token_bytes(16)
        self._save({"hash": f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${_hash(password, salt).hex()}",
                    "secret": secrets.token_hex(32), "changed": int(self.clock())})

    def verify(self, password):
        if not isinstance(password, str) or len(password) > MAX_LENGTH:
            return False
        try:
            _, iterations, salt, digest = self._load().get("hash", "").split("$")
            want = bytes.fromhex(digest)
            got = _hash(password, bytes.fromhex(salt), int(iterations))
        except (ValueError, AttributeError):
            _hash(password, b"\0" * 16)          # spend the same time, so a missing password is not a tell
            return False
        return hmac.compare_digest(got, want)

    # ---- sessions
    def _sign(self, payload):
        secret = self._load().get("secret", "")
        return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def make_session(self):
        payload = f"{int(self.clock()) + SESSION_SECONDS}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def _parse(self, token):
        """(expiry, nonce) of a token that is correctly signed and not expired, else None."""
        if not self.has_password() or not isinstance(token, str):
            return None
        try:
            expiry, nonce, sig = token.split(".")
            int(expiry)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sign(f"{expiry}.{nonce}")) or int(expiry) <= self.clock():
            return None
        return int(expiry), nonce

    def check_session(self, token):
        parsed = self._parse(token)
        return bool(parsed) and parsed[1] not in self._load().get("revoked", {})

    def revoke(self, token):
        """Log a session out for good (not just in the browser that held it). Remembered in the file, so a
        restart does not bring it back; entries are dropped once the session would have expired anyway."""
        parsed = self._parse(token)
        if not parsed:
            return
        data = self._load()
        now = self.clock()
        revoked = {n: e for n, e in data.get("revoked", {}).items() if isinstance(e, int) and e > now}
        revoked[parsed[1]] = parsed[0]
        data["revoked"] = revoked
        self._save(data)

    # ---- guessing
    def blocked_for(self, who):
        """Seconds this address must still wait, or 0."""
        now = self.clock()
        with self._lock:
            recent = [t for t in self._fails.get(who, []) if now - t < FAIL_WINDOW]
            self._fails[who] = recent
            if len(recent) >= FAIL_LIMIT:
                return int(FAIL_WINDOW - (now - recent[0])) + 1
        return 0

    def failed(self, who):
        with self._lock:
            self._fails.setdefault(who, []).append(self.clock())

    def succeeded(self, who):
        with self._lock:
            self._fails.pop(who, None)

    def login(self, password, who="?"):
        """A session token for the right password. AuthError for a wrong one or when too many were tried."""
        wait = self.blocked_for(who)
        if wait:
            raise AuthError(f"too many wrong passwords: wait {wait} seconds and try again")
        if not self.verify(password):
            self.failed(who)
            raise AuthError("that password is not right")
        self.succeeded(who)
        return self.make_session()
