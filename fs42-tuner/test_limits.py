"""Prove the two limits are independent:
   - many viewers on ONE channel cost one encoder
   - the (N+1)th DISTINCT channel is refused
"""
import json, os, sqlite3, subprocess, sys, time, urllib.error, urllib.request
import tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="tuner-test-")      # its database, config and log: not the working folder
from datetime import datetime, timedelta

BASE = "http://127.0.0.1:5005"

# 4 channels, but only 2 encoders allowed.
conf = {
    "db_path": os.path.join(TMP, "limits.db"), "catalog_root": ".",
    "channels": {"3": "ch3", "4": "ch4", "5": "ch5", "6": "ch6"},
    "max_encoders": 2, "tuner_count": 12, "linger_seconds": 1,
    "port": 5005, "advertise_url": BASE, "friendly_name": "FS42 limits",
    "video": {"width": 320, "height": 240, "fps": 15, "vbitrate": "500k", "preset": "ultrafast"},
}
json.dump(conf, open(os.path.join(TMP, "limits_config.json"), "w"))

if os.path.exists(os.path.join(TMP, "limits.db")):
    os.remove(os.path.join(TMP, "limits.db"))
c = sqlite3.connect(os.path.join(TMP, "limits.db"))
c.execute("""CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT,
 station TEXT NOT NULL, liquid_type TEXT NOT NULL, start_time TIMESTAMP, end_time TIMESTAMP,
 break_strategy TEXT NOT NULL, title TEXT NOT NULL, sequence_key TEXT, break_info TEXT,
 content_json TEXT, plan_json TEXT NOT NULL)""")
t = datetime.now() - timedelta(seconds=10)
plan = [{"path": "catalog/ch3/shows/ep1.mkv", "skip": 0, "duration": 110, "is_stream": False, "content_type": "feature"}]
for st in ("ch3", "ch4", "ch5", "ch6"):
    c.execute("INSERT INTO liquid_blocks (station,liquid_type,start_time,end_time,break_strategy,title,plan_json)"
              " VALUES (?,?,?,?,?,?,?)",
              (st, "LiquidBlock", t.isoformat(), (t + timedelta(seconds=110)).isoformat(),
               "standard", f"Show on {st}", json.dumps(plan)))
c.commit(); c.close()

env = dict(os.environ, FS42_TUNER_CONF=os.path.join(TMP, "limits_config.json"))
srv = subprocess.Popen([sys.executable, os.path.join(HERE, "tuner.py")], env=env,
                       stdout=open(os.path.join(TMP, "limits.log"), "w"), stderr=subprocess.STDOUT)
import socket
for _ in range(150):   # up to 30s; a CI runner can be slow to import and bind
    try: socket.create_connection(("127.0.0.1", 5005), timeout=0.2).close(); break
    except OSError: time.sleep(0.2)

clients = []
def tune(ch):
    """Open a stream and hold it. Returns 'ok' or the HTTP error code."""
    try:
        r = urllib.request.urlopen(f"{BASE}/auto/v{ch}", timeout=30)   # first bytes wait on ffmpeg starting; a loaded CI runner needs well over 5s
        r.read(4096)                       # pull a little so the connection is real
        clients.append(r)
        return "ok"
    except urllib.error.HTTPError as e:
        return str(e.code)

def status():
    return json.load(urllib.request.urlopen(f"{BASE}/status.json"))

results = []
try:
    print("Six viewers pile onto channel 3 (one distinct channel):")
    for i in range(6):
        results.append(("ch3 viewer", tune(3)))
        print(f"   viewer {i+1}: {results[-1][1]}")
    s = status()
    enc = s["encoders"]["in_use"]
    v3 = next(c["viewers"] for c in s["channels"] if str(c["channel"]) == "3")
    print(f"   -> encoders in use: {enc}   viewers on ch3: {v3}")
    assert enc == 1, f"expected 1 encoder, got {enc}"
    assert v3 == 6, f"expected 6 viewers, got {v3}"

    print("\nA second distinct channel (within the 2-encoder budget):")
    print(f"   ch4: {tune(4)}   -> encoders in use: {status()['encoders']['in_use']}")
    assert status()["encoders"]["in_use"] == 2

    print("\nA third distinct channel (over budget):")
    r5 = tune(5)
    print(f"   ch5: {r5}")
    assert r5 == "503", f"expected 503, got {r5}"

    print("\nBut another viewer on an ALREADY-ENCODING channel still gets in:")
    r3 = tune(3)
    print(f"   ch3 viewer 7: {r3}")
    assert r3 == "ok"
    s = status()
    print(f"   -> encoders: {s['encoders']['in_use']}/{s['encoders']['max']}, "
          f"ch3 viewers: {next(c['viewers'] for c in s['channels'] if str(c['channel'])=='3')}")

    print("\nPASS -- viewers and encoders are counted separately")
finally:
    for r in clients:
        try: r.close()
        except Exception: pass
    time.sleep(2)
    srv.terminate()
    try: srv.wait(timeout=5)
    except subprocess.TimeoutExpired: srv.kill()
