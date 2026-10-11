#!/usr/bin/env python3
"""A developer check, not part of CI: run the image with the CURRENT repo code mounted over it and a few real (tiny) videos, and
check what FieldStation42 itself does with the break settings the wrapper writes: default commercials on and off (no breaks,
videos back to back), a commercials block that opts its hours in, mid cards of a slot's own, and default mid cards off.

Needs Docker and ffmpeg on this machine.   Usage: scripts/e2e-breaks.py [image]    (default station-wrapper-43-smoke:local)
The image only supplies FieldStation42 and the system packages; the wrapper's own code is the repo's (mounted read-only).
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
IMAGE = sys.argv[1] if len(sys.argv) > 1 else "station-wrapper-43-smoke:local"
ROOT = tempfile.mkdtemp(prefix="e2e-breaks-", dir=os.environ.get("E2E_DIR", None))
NAME = f"e2e-breaks-{os.getpid()}"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def req(method, path, body=None, timeout=90):
    c = http.client.HTTPConnection("127.0.0.1", 5004, timeout=timeout)
    c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, raw


def wait(fn, what, secs=120, every=1.5):
    end, last = time.time() + secs, None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:
            last = e
        time.sleep(every)
    raise RuntimeError(f"timed out waiting for {what}: {last}")


def clip(path, seconds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=160x120:d={seconds}:r=10",
                    "-f", "lavfi", "-i", f"sine=frequency=500:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", path], check=True)


def station(name, number, schedule, **form):
    f = {"name": name, "number": number, "kind": "standard", "increment": 5, "bump": "Cards", "commercials": "", "midcards": False,
         "breaks": "end", "pack": False, "schedule": schedule, **form}
    code, out = req("POST", "/api/stations/save", {"original": None, "form": f})
    if code != 200:
        return None, out
    code, job = req("POST", "/api/stations/build", {"name": name, "days": 1, "rebuild": True})
    done = wait(lambda: (lambda j: j if j["state"] != "running" else None)(req("GET", f"/api/stations/job?id={job['id']}")[1]), f"the build of {name}", 300)
    return done, out


def first_block(name):
    s = req("GET", f"/api/schedule?name={name}")[1]
    if not s.get("blocks"):
        return None, []
    b = s["blocks"][0]
    detail = req("GET", f"/api/schedule/block?name={name}&start={b['start']}&limit=200")[1]
    return b, detail.get("entries", [])


def kinds(entries):
    return [(e["file"], e["type"]) for e in entries]


def day(*videos, ads=(), cards=()):
    return {"videos": [dict({"from": a, "to": b, "lists": l}, **(extra[0] if extra else {})) for a, b, l, *extra in videos],
            "commercials": [{"from": a, "to": b, "list": l} for a, b, l in ads],
            "cards": [{"from": a, "to": b, "start": s, "end": e} for a, b, s, e in cards]}


def week(d):
    return {n: d for n in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}


def sched(d):
    return {"default": week(d), "seasons": {"dates": {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"},
                                           "spring": None, "summer": None, "fall": None, "winter": None}, "events": []}


def main():
    for d in ("config", "catalog", "wrapper"):
        os.makedirs(os.path.join(ROOT, d))
    media = os.path.join(ROOT, "media")
    for n in ("A", "B", "C", "D"):
        clip(os.path.join(media, "shows", f"Show{n}", f"Show{n} one.mp4"), 90)
        clip(os.path.join(media, "shows", f"Show{n}", f"Show{n} two.mp4"), 90)
    for i in range(1, 6):
        clip(os.path.join(media, "ads", f"spot{i}.mp4"), 10)
    clip(os.path.join(media, "cards", "default_card.mp4"), 4)
    clip(os.path.join(media, "cards", "open_card.mp4"), 4)
    clip(os.path.join(media, "cards", "close_card.mp4"), 4)
    # what the Dockerfile copies into /opt/wrapper and /opt/supervisor, from the repo as it is now
    for src in ("scripts/init.py", "scripts/build_channels.py", "scripts/fs42_link.py", "scripts/firstrun.py", "scripts/update_catalog.py",
                "fs42-tuner/schedlock.py", "fieldstation42/console_launch.py"):
        shutil.copy(os.path.join(REPO, src), os.path.join(ROOT, "wrapper", os.path.basename(src)))
    os.makedirs(os.path.join(ROOT, "supervisor"))
    for src in ("supervisor/supervisor.py", "fs42-tuner/stations.py"):
        shutil.copy(os.path.join(REPO, src), os.path.join(ROOT, "supervisor", os.path.basename(src)))
    mounts = ["-v", f"{ROOT}/config:/config", "-v", f"{ROOT}/catalog:/app/catalog", "-v", f"{media}:/mnt/media:ro",
              "-v", f"{REPO}/fs42-tuner:/opt/tuner:ro", "-v", f"{ROOT}/wrapper:/opt/wrapper:ro", "-v", f"{ROOT}/supervisor:/opt/supervisor:ro",
              "-v", f"{REPO}/assets:/opt/assets:ro"]
    subprocess.run(["docker", "run", "-d", "--name", NAME, "--network", "host", "-e", "DASHBOARD_LOGIN=0", "-e", "SYNC_ENABLED=0",
                    "-e", "FIRSTRUN_TEST_STATION=0", "-e", "FIRSTRUN_GUIDE_STATION=0", *mounts, IMAGE], check=True, capture_output=True)
    try:
        wait(lambda: req("GET", "/api/files/info")[0] == 200, "the dashboard", 120)
        wait(lambda: req("GET", "/api/stations")[0] == 200, "the FieldStation42 console", 150)
        for tag, src in (("Cards", "cards/default_card.mp4"), ("Ads", "ads"), ("Open", "cards/open_card.mp4"), ("Close", "cards/close_card.mp4"),
                         ("A", "shows/ShowA"), ("B", "shows/ShowB"), ("C", "shows/ShowC"), ("D", "shows/ShowD")):
            req("POST", "/api/files/create-tag", {"name": tag})
            req("POST", "/api/files/add", {"tag": tag, "sources": [{"root": "media", "path": src}]})

        # 1. no breaks: commercials off, videos back to back
        done, out = station("NoBreaks", 11, sched(day((0, 24, ["A"]))))
        check("1. commercials off: the station builds", done and done["state"] == "done", (done, out))
        b, entries = first_block("NoBreaks")
        check("1. no commercials and no mid cards in a block", entries and len(entries) == 1 and "spot" not in entries[0]["file"], kinds(entries))
        dur = (__import__("datetime").datetime.fromisoformat(b["end"]) - __import__("datetime").datetime.fromisoformat(b["start"])).total_seconds() if b else 0
        check("1. the block is the video's own length (soft slot)", 88 <= dur <= 92, dur)

        # 2. a commercials block opts its hours in even with the default off
        done, out = station("OptIn", 12, sched(day((0, 24, ["B"]), ads=[(0, 24, "Ads", )])))
        check("2. a commercials block: the station builds", done and done["state"] == "done", (done, out))
        b, entries = first_block("OptIn")
        types = [t for _f, t in kinds(entries)]
        check("2. the break has commercials from the block's list", "commercial" in types, kinds(entries))

        # 3. commercials on by default, mid cards on by default
        done, out = station("Defaults", 13, sched(day((0, 24, ["C"]))), commercials="Ads", midcards=True)
        check("3. defaults on: the station builds", done and done["state"] == "done", (done, out))
        b, entries = first_block("Defaults")
        files = [f for f, _t in kinds(entries)]
        check("3. the default mid card starts the break and is used again at its end",
              "default_card" in files[1] and files.count("default_card.mp4") >= 2, files)

        # 4. a slot's own start and end cards, default mid cards off
        done, out = station("OwnCards", 14, sched(day((0, 24, ["D"], {"start": "Open", "end": "Close"}), ads=[(0, 24, "Ads")])))
        check("4. a slot's own cards: the station builds", done and done["state"] == "done", (done, out))
        b, entries = first_block("OwnCards")
        files = [f for f, _t in kinds(entries)]
        check("4. the break starts with the slot's start card and its end card is used",
              "open_card" in files[1] and "close_card.mp4" in files, files)
        check("4. the default card is not used there", not any("default_card" in f for f in files), files)

        # 5. default mid cards off and nothing chosen for the slot: a break of commercials only
        done, out = station("NoCards", 15, sched(day((0, 24, ["A"]), ads=[(0, 24, "Ads")])))
        check("5. cards off, commercials from a block: the station builds", done and done["state"] == "done", (done, out))
        b, entries = first_block("NoCards")
        files = [f for f, _t in kinds(entries)]
        check("5. the break is commercials only", any("spot" in f for f in files) and not any("card" in f for f in files), files)
    finally:
        subprocess.run(["docker", "stop", NAME], capture_output=True)
        logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        bad = [l for l in text.splitlines() if "Traceback" in l or "ConfigurationError" in l]
        if bad and os.environ.get("E2E_LOG"):
            with open(os.environ["E2E_LOG"], "w") as f:
                f.write(text)
        check("no tracebacks or configuration errors in the log", not bad, bad[:3])
        check("the console started through console_launch.py", "console: " in text and "wrapper_no_breaks" in text, "")
        subprocess.run(["docker", "rm", NAME], capture_output=True)
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
