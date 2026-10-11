"""Build a synthetic FS42-shaped schedule db and check the resolver's arithmetic.

FS42 writes timestamps with a space separator ("2026-10-03 20:00:00"), not the
T that isoformat() defaults to. Rows here use FS42's format so the query is
exercised against what production actually stores.
"""
import json, sqlite3, os
from datetime import datetime, timedelta
from resolver import resolve, stations, listings, ScheduleGap

DB = "/tmp/fs42_test.db"
if os.path.exists(DB):
    os.remove(DB)

conn = sqlite3.connect(DB)
conn.execute("""CREATE TABLE liquid_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT NOT NULL,
    liquid_type TEXT NOT NULL, start_time TIMESTAMP, end_time TIMESTAMP,
    break_strategy TEXT NOT NULL, title TEXT NOT NULL, sequence_key TEXT,
    break_info TEXT, content_json TEXT, plan_json TEXT NOT NULL)""")

T0 = datetime(2026, 9, 20, 20, 0, 0)  # 8pm block

# A realistic half-hour: bump, show part 1, two ads, show part 2, ad, bump.
# Note the show keeps ONE path and advances `skip` across its parts -- this is
# the detail that makes naive "one file per segment" tuners desync.
plan = [
    {"path": "catalog/ch3/bumps/ident.mp4",   "skip": 0,    "duration": 10,  "is_stream": False, "content_type": "bump"},
    {"path": "catalog/ch3/shows/ep1.mkv",     "skip": 0,    "duration": 600, "is_stream": False, "content_type": "feature"},
    {"path": "catalog/ch3/ads/soda.mp4",      "skip": 0,    "duration": 30,  "is_stream": False, "content_type": "commercial"},
    {"path": "catalog/ch3/ads/car.mp4",       "skip": 0,    "duration": 30,  "is_stream": False, "content_type": "commercial"},
    {"path": "catalog/ch3/shows/ep1.mkv",     "skip": 600,  "duration": 1040, "is_stream": False, "content_type": "feature"},
    {"path": "catalog/ch3/ads/beer.mp4",      "skip": 0,    "duration": 80,  "is_stream": False, "content_type": "commercial"},
    {"path": "catalog/ch3/bumps/outro.mp4",   "skip": 0,    "duration": 10,  "is_stream": False, "content_type": "bump"},
]
assert sum(p["duration"] for p in plan) == 1800

conn.execute(
    "INSERT INTO liquid_blocks (station, liquid_type, start_time, end_time, break_strategy, title, plan_json) VALUES (?,?,?,?,?,?,?)",
    ("ch3", "LiquidBlock", T0.isoformat(sep=" "), (T0 + timedelta(minutes=30)).isoformat(sep=" "),
     "standard", "The Twilight Zone", json.dumps(plan)))
conn.execute(
    "INSERT INTO liquid_blocks (station, liquid_type, start_time, end_time, break_strategy, title, plan_json) VALUES (?,?,?,?,?,?,?)",
    ("ch3", "LiquidBlock", (T0 + timedelta(minutes=30)).isoformat(sep=" "), (T0 + timedelta(minutes=60)).isoformat(sep=" "),
     "standard", "Night Gallery", json.dumps([
         {"path": "catalog/ch3/shows/ng1.mkv", "skip": 0, "duration": 1800, "is_stream": False, "content_type": "feature"}])))
conn.commit(); conn.close()

print("stations:", stations(DB))

cases = [
    (0,     "bumps/ident.mp4",  0,    "cold open on the ident"),
    (5,     "bumps/ident.mp4",  5,    "mid-ident"),
    (10,    "shows/ep1.mkv",    0,    "show starts exactly as ident ends"),
    (310,   "shows/ep1.mkv",    300,  "5 min into part 1"),
    (610,   "ads/soda.mp4",     0,    "first ad"),
    (645,   "ads/car.mp4",      5,    "5s into second ad"),
    (700,   "shows/ep1.mkv",    630,  "part 2 -- seek must be skip(600)+offset(30)"),
    (1795,  "bumps/outro.mp4",  5,    "closing bump, last 5s of the block"),
    (1800,  "shows/ng1.mkv",    0,    "rolls into the next block"),
]

ok = True
for secs, want_path, want_seek, note in cases:
    c = resolve(DB, "ch3", T0 + timedelta(seconds=secs))
    hit = c.entry.path.endswith(want_path) and abs(c.seek - want_seek) < 0.001
    ok &= hit
    print(f"{'PASS' if hit else 'FAIL'}  t+{secs:>4}s  {c.block_title:<18} "
          f"{os.path.basename(c.entry.path):<12} seek={c.seek:<6.1f} rem={c.remaining:<6.1f} | {note}")

try:
    resolve(DB, "ch3", T0 + timedelta(hours=3))
    print("FAIL  past end of schedule should raise"); ok = False
except ScheduleGap:
    print("PASS  past end of schedule raises ScheduleGap")

# A new install has no schedule database until FieldStation42 gives a station a schedule.
# That is "nothing scheduled", not a crash.
MISSING = "/tmp/fs42_does_not_exist.db"
if os.path.exists(MISSING):
    os.remove(MISSING)
try:
    resolve(MISSING, "ch3")
    print("FAIL  a missing database should raise ScheduleGap"); ok = False
except ScheduleGap as e:
    print("PASS  a missing database raises ScheduleGap:", e)
if stations(MISSING) == [] and listings(MISSING, "ch3", T0, T0 + timedelta(hours=1)) == []:
    print("PASS  a missing database has no stations and no listings")
else:
    print("FAIL  a missing database should be empty"); ok = False
if os.path.exists(MISSING):
    print("FAIL  looking must not create the database"); ok = False
else:
    print("PASS  looking does not create the database")

print("\nlistings:", [(l["title"], l["start"].strftime("%H:%M")) for l in
                      listings(DB, "ch3", T0, T0 + timedelta(hours=1))])
print("\nALL PASS" if ok else "\nFAILURES")
