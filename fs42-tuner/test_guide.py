"""Fake SiliconDust guide + a third-party XMLTV feed; check the wrapper emits
one document that Jellyfin can bind to every channel in the lineup."""
import json, os, sqlite3, threading, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import resolver
from guide import GuideBuilder

PORT = 5098
BASE = f"http://127.0.0.1:{PORT}"
NOW = int(time.time())

# SiliconDust returns one object per channel with a Guide array, and paginates
# forward when asked for a Start timestamp.
SD_PAGE1 = [
    {"GuideNumber": "7.1", "GuideName": "WXYZ", "ImageURL": f"{BASE}/logo.png",
     "Guide": [
         {"Title": "Evening News", "StartTime": NOW - 600, "EndTime": NOW + 1200,
          "Synopsis": "Local news.", "Filter": ["News"]},
         {"Title": "Wheel of Fortune", "StartTime": NOW + 1200, "EndTime": NOW + 3000,
          "EpisodeTitle": "Teen Week", "EpisodeNumber": "S41E12",
          "OriginalAirdate": NOW - 86400 * 30, "Filter": ["Game Show"]},
     ]},
    {"GuideNumber": "11.1", "GuideName": "WABC", "Guide": [
        {"Title": "Movie Night", "StartTime": NOW, "EndTime": NOW + 7200,
         "Filter": ["Movie"]}]},
]
# Deliberately mislabelled: a page for 7.1 returned to any channel's query.
# The builder must match on GuideNumber and not graft it onto 11.1.
SD_PAGE2 = [{"GuideNumber": "7.1", "Guide": [
    {"Title": "Late Show", "StartTime": NOW + 3000, "EndTime": NOW + 6600}]}]

EXTERNAL_XMLTV = f"""<?xml version="1.0"?>
<tv>
  <channel id="cable.55"><display-name>Retro Movie Net</display-name></channel>
  <programme start="{datetime.now().strftime('%Y%m%d%H%M%S')} -0400"
             stop="{(datetime.now()+timedelta(hours=2)).strftime('%Y%m%d%H%M%S')} -0400"
             channel="cable.55">
    <title>The Thing From Another World</title>
  </programme>
</tv>"""

calls = {"sd": 0}


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.startswith("/api/guide.php"):
            calls["sd"] += 1
            body = json.dumps(SD_PAGE2 if "Start=" in self.path else SD_PAGE1).encode()
            return self._send(body, "application/json")
        if self.path == "/epg.xml":
            return self._send(EXTERNAL_XMLTV.encode(), "application/xml")
        self.send_error(404)

    def _send(self, b, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = ThreadingHTTPServer(("127.0.0.1", PORT), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)

import guide as guide_mod
guide_mod.SD_GUIDE = f"{BASE}/api/guide.php"

DB = "guide_test.db"
if os.path.exists(DB):
    os.remove(DB)
c = sqlite3.connect(DB)
c.execute("""CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT,
 station TEXT NOT NULL, liquid_type TEXT NOT NULL, start_time TIMESTAMP, end_time TIMESTAMP,
 break_strategy TEXT NOT NULL, title TEXT NOT NULL, sequence_key TEXT, break_info TEXT,
 content_json TEXT, plan_json TEXT NOT NULL)""")
t = datetime.now() - timedelta(minutes=5)
for i, title in enumerate(["The Twilight Zone", "Night Gallery"]):
    s = t + timedelta(minutes=30 * i)
    c.execute("INSERT INTO liquid_blocks (station,liquid_type,start_time,end_time,break_strategy,title,plan_json)"
              " VALUES (?,?,?,?,?,?,?)",
              ("PublicDomain", "LiquidBlock", s.isoformat(),
               (s + timedelta(minutes=30)).isoformat(), "standard", title, "[]"))
c.commit(); c.close()


class FakeUpstream:
    url = BASE
    def discover(self, force=False):
        return {"DeviceAuth": "TESTAUTH123", "FriendlyName": "HDHomeRun CONNECT"}


gb = GuideBuilder(
    fs42_channels={"801": "PublicDomain"},
    resolver=resolver, db_path=DB, guide_hours=2,
    sources=[{"type": "hdhomerun"},
             {"type": "xmltv", "name": "cable", "url": f"{BASE}/epg.xml",
              "number_map": {"cable.55": "55"}}],
    refresh_minutes=60, upstream=FakeUpstream())

doc = gb.xmltv(force=True)
root = ET.fromstring(doc)
chans = {c.get("id"): [d.text for d in c.findall("display-name")] for c in root.findall("channel")}
progs = {}
for p in root.findall("programme"):
    progs.setdefault(p.get("channel"), []).append(p.find("title").text)

print("channels in merged guide:")
for cid, names in chans.items():
    print(f"  {cid:<12} display-names={names}")
print("\nprogrammes:")
for cid, titles in progs.items():
    print(f"  {cid:<12} {titles}")
print("\nper-source:", json.dumps(gb.stats, indent=None))

ok = True
# Every channel must carry its tuner NUMBER as a display-name -- that is the
# fallback Jellyfin uses when the guide id does not equal the channel id.
checks = [
    ("fs42.801 present",        "fs42.801" in chans),
    ("fs42.801 has number 801", "801" in chans.get("fs42.801", [])),
    ("hdhr.7.1 present",        "hdhr.7.1" in chans),
    ("hdhr.7.1 has number 7.1", "7.1" in chans.get("hdhr.7.1", [])),
    ("hdhr.11.1 present",       "hdhr.11.1" in chans),
    ("external channel merged", "cable.55" in chans),
    ("number_map injected 55",  "55" in chans.get("cable.55", [])),
    ("FS42 listings present",   "The Twilight Zone" in progs.get("fs42.801", [])),
    ("OTA listings present",    "Evening News" in progs.get("hdhr.7.1", [])),
    ("pagination followed",     "Late Show" in progs.get("hdhr.7.1", [])),
    ("external listing merged", "The Thing From Another World" in progs.get("cable.55", [])),
    ("no cross-channel leak",   "Late Show" not in progs.get("hdhr.11.1", [])),
]
for label, hit in checks:
    print(f"  {'PASS' if hit else 'FAIL'}  {label}")
    ok &= hit

# Episode/category detail should survive the conversion.
wheel = [p for p in root.findall("programme")
         if p.find("title") is not None and p.find("title").text == "Wheel of Fortune"]
detail = bool(wheel) and wheel[0].find("sub-title") is not None and wheel[0].find("category") is not None
print(f"  {'PASS' if detail else 'FAIL'}  episode title + category preserved")
ok &= detail

# A broken source must degrade, not take the whole guide down.
gb2 = GuideBuilder(fs42_channels={"801": "PublicDomain"}, resolver=resolver, db_path=DB,
                   guide_hours=2, sources=[{"type": "xmltv", "url": "http://127.0.0.1:1/nope.xml"}],
                   refresh_minutes=60, upstream=None)
root2 = ET.fromstring(gb2.xmltv())
survived = any(c.get("id") == "fs42.801" for c in root2.findall("channel"))
print(f"  {'PASS' if survived else 'FAIL'}  dead source degrades, FS42 guide still served")
ok &= survived

srv.shutdown()
os.path.exists(DB) and os.remove(DB)
print("\nPASS" if ok else "\nFAIL")
