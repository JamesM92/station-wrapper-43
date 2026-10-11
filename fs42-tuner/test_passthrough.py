"""Stand up a fake real HDHomeRun and check the wrapper fronts it correctly:
   - merged lineup carries both sets, OTA numbers/names untouched
   - FS42 channels flagged Favorite, OTA not
   - OTA streams relay byte-for-byte
   - OTA concurrency capped at the real device's physical tuner count
   - duplicate numbers refused at startup
"""
import json, os, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.request
import tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="tuner-test-")      # its database, config and log: not the working folder
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_PORT, WRAP_PORT = 5099, 5007
FAKE = f"http://127.0.0.1:{FAKE_PORT}"
WRAP = f"http://127.0.0.1:{WRAP_PORT}"

OTA = [{"GuideNumber": n, "GuideName": f"WXYZ-{n}", "HD": 1,
        "URL": f"{FAKE}/auto/v{n}"} for n in ["2.1", "4.1", "7.1", "11.1"]]
PAYLOAD = bytes(range(256)) * 400          # recognisable "video"
TUNERS = 2                                 # physical tuners on the fake device

live = {"n": 0}
live_lock = threading.Lock()


class FakeHdhr(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/discover.json":
            return self._json({"FriendlyName": "HDHomeRun CONNECT", "ModelNumber": "HDHR5-2US",
                               "DeviceID": "10ABCDEF", "BaseURL": FAKE,
                               "LineupURL": f"{FAKE}/lineup.json", "TunerCount": TUNERS})
        if self.path == "/lineup.json":
            return self._json(OTA)
        if self.path.startswith("/auto/v"):
            with live_lock:
                live["n"] += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                for _ in range(40):          # dribble it out like a live feed
                    self.wfile.write(PAYLOAD)
                    time.sleep(0.25)
            except Exception:
                pass
            finally:
                with live_lock:
                    live["n"] -= 1
            return
        self.send_error(404)

    def _json(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeHdhr)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

if os.path.exists(os.path.join(TMP, "pt.db")):
    os.remove(os.path.join(TMP, "pt.db"))
c = sqlite3.connect(os.path.join(TMP, "pt.db"))
c.execute("""CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT,
 station TEXT NOT NULL, liquid_type TEXT NOT NULL, start_time TIMESTAMP, end_time TIMESTAMP,
 break_strategy TEXT NOT NULL, title TEXT NOT NULL, sequence_key TEXT, break_info TEXT,
 content_json TEXT, plan_json TEXT NOT NULL)""")
c.commit(); c.close()

BASE = {"db_path": os.path.join(TMP, "pt.db"), "catalog_root": ".", "max_encoders": 2, "tuner_count": 8,
        "port": WRAP_PORT, "advertise_url": WRAP,
        "passthrough": {"url": FAKE, "mode": "proxy"}}


def start(channels):
    json.dump(dict(BASE, channels=channels), open(os.path.join(TMP, "pt_config.json"), "w"))
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "tuner.py")],
                         env=dict(os.environ, FS42_TUNER_CONF=os.path.join(TMP, "pt_config.json"), FS42_KEEP_PASSTHROUGH="1"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    import socket
    for _ in range(150):   # up to 30s; a CI runner can be slow to import and bind
        try: socket.create_connection(("127.0.0.1", WRAP_PORT), timeout=0.2).close(); break
        except OSError: time.sleep(0.2)
    return p


def get(path):
    return json.load(urllib.request.urlopen(WRAP + path, timeout=5))


ok = True
srv_proc = start({"801": "PublicDomain", "802": "ClassicMovies"})
try:
    lineup = get("/lineup.json")
    nums = [c["GuideNumber"] for c in lineup]
    print("merged lineup:", nums)
    ok &= nums == ["2.1", "4.1", "7.1", "11.1", "801", "802"] or set(nums) == {
        "2.1", "4.1", "7.1", "11.1", "801", "802"}

    by = {c["GuideNumber"]: c for c in lineup}
    print("  OTA name preserved:", by["7.1"]["GuideName"] == "WXYZ-7.1")
    ok &= by["7.1"]["GuideName"] == "WXYZ-7.1"
    print("  FS42 favourite:", by["801"]["Favorite"], " OTA favourite:", by["4.1"]["Favorite"])
    ok &= by["801"]["Favorite"] is True and by["4.1"]["Favorite"] is False
    print("  OTA routed through wrapper:", by["7.1"]["URL"] == f"{WRAP}/auto/v7.1")
    ok &= by["7.1"]["URL"] == f"{WRAP}/auto/v7.1"

    d = get("/discover.json")
    print(f"  advertised TunerCount: {d['TunerCount']} (8 viewer slots + {TUNERS} real tuners)")
    ok &= d["TunerCount"] == 8 + TUNERS
    print("  ModelNumber has no 'hdtc':", "hdtc" not in d["ModelNumber"].lower())
    ok &= "hdtc" not in d["ModelNumber"].lower()

    print("\nrelaying an OTA channel:")
    r = urllib.request.urlopen(f"{WRAP}/auto/v2.1", timeout=10)
    got = r.read(len(PAYLOAD))
    print("  bytes identical to upstream:", got == PAYLOAD)
    ok &= got == PAYLOAD

    r2 = urllib.request.urlopen(f"{WRAP}/auto/v4.1", timeout=10)
    r2.read(1024)
    time.sleep(0.5)
    st = get("/status.json")
    print(f"  passthrough tuners in use: {st['passthrough']['tuners_in_use']}/{st['passthrough']['tuners']}")
    ok &= st["passthrough"]["tuners_in_use"] == 2

    print("\nthird OTA stream, real device only has 2 tuners:")
    try:
        urllib.request.urlopen(f"{WRAP}/auto/v7.1", timeout=10).read(256)
        print("  FAIL -- should have been refused"); ok = False
    except urllib.error.HTTPError as e:
        print(f"  refused with {e.code}")
        ok &= e.code == 503

    print("\n  ...but an FS42 channel still tunes (separate budget):")
    try:
        r3 = urllib.request.urlopen(f"{WRAP}/auto/v801", timeout=10)
        r3.read(188)
        print("  ok")
        r3.close()
    except Exception as e:
        print(f"  FAIL: {e}"); ok = False

    for x in (r, r2):
        try: x.close()
        except Exception: pass
finally:
    srv_proc.terminate()
    try: srv_proc.wait(timeout=5)
    except subprocess.TimeoutExpired: srv_proc.kill()

print("\nduplicate numbering must be refused:")
p = subprocess.Popen([sys.executable, os.path.join(HERE, "tuner.py")],
                     env=dict(os.environ, FS42_TUNER_CONF=os.path.join(TMP, "pt_config.json"), FS42_KEEP_PASSTHROUGH="1"),
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
json.dump(dict(BASE, channels={"7.1": "Clash", "801": "Fine"}), open(os.path.join(TMP, "pt_config.json"), "w"))
p.terminate(); p.wait(timeout=5)
p = subprocess.run([sys.executable, os.path.join(HERE, "tuner.py")],
                   env=dict(os.environ, FS42_TUNER_CONF=os.path.join(TMP, "pt_config.json"), FS42_KEEP_PASSTHROUGH="1"),
                   capture_output=True, text=True, timeout=30)
out = p.stdout + p.stderr
print("  " + "\n  ".join(l for l in out.strip().splitlines() if l.strip())[:600])
ok &= p.returncode != 0 and "7.1" in out

srv.shutdown()
for f in (os.path.join(TMP, "pt.db"), os.path.join(TMP, "pt_config.json")):
    os.path.exists(f) and os.remove(f)
print("\nPASS" if ok else "\nFAIL")
