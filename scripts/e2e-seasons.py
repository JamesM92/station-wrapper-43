#!/usr/bin/env python3
"""A developer check, not part of CI: run the image with a few real (tiny) videos and make a station that uses seasons,
a special day and commercials by the hour, to prove FieldStation42 itself accepts the config the wrapper writes
(week_overrides, date_overrides, random_tags, commercial_dir) and can build a catalog and days from it.

Needs Docker and ffmpeg on this machine.   Usage: scripts/e2e-seasons.py [image]    (default station-wrapper-43-smoke:local)
Build the image first:  docker build -t station-wrapper-43-smoke:local .
"""
import datetime
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "station-wrapper-43-smoke:local"
ROOT = tempfile.mkdtemp(prefix="e2e-seasons-", dir=os.environ.get("E2E_DIR", None))
NAME = f"e2e-seasons-{os.getpid()}"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def req(method, path, body=None, timeout=60):
    c = http.client.HTTPConnection("127.0.0.1", 5004, timeout=timeout)
    c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, raw


def wait(fn, what, secs=120, every=1.5):
    end = time.time() + secs
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:
            last = e
        time.sleep(every)
    raise RuntimeError(f"timed out waiting for {what}: {last}")


def make_show(name, genre):
    folder = os.path.join(ROOT, "media", "tvseries", name, "Season 01")
    os.makedirs(folder)
    for i in range(1, 4):
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=6:r=10",
                        "-f", "lavfi", "-i", "sine=frequency=500:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", os.path.join(folder, f"{name} - S01E0{i}.mp4")], check=True)
    with open(os.path.join(ROOT, "media", "tvseries", name, "tvshow.nfo"), "w") as f:
        f.write(f"<tvshow><title>{name}</title><genre>{genre}</genre></tvshow>")


def main():
    os.makedirs(os.path.join(ROOT, "config"))
    os.makedirs(os.path.join(ROOT, "catalog"))
    make_show("Funny", "Comedy")
    make_show("Serious", "Drama")
    subprocess.run(["docker", "run", "-d", "--name", NAME, "--network", "host", "-e", "DASHBOARD_LOGIN=0", "-e", "SYNC_ENABLED=1",
                    "-e", "FIRSTRUN_TEST_STATION=0", "-e", "FIRSTRUN_GUIDE_STATION=0", "-v", f"{ROOT}/config:/config", "-v", f"{ROOT}/catalog:/app/catalog",
                    "-v", f"{ROOT}/media/tvseries:/mnt/media/tvseries:ro", IMAGE], check=True, capture_output=True)
    try:
        wait(lambda: req("GET", "/api/files/info")[0] == 200, "the dashboard", 90)
        wait(lambda: {"Comedy", "Drama"} <= {e["name"] for e in req("GET", "/api/files/list?root=tags&path=")[1]["entries"]}, "the genre lists", 150, 3)
        wait(lambda: req("GET", "/api/stations")[0] == 200, "the FieldStation42 console", 120)
        req("POST", "/api/files/create-tag", {"name": "Ads"})
        req("POST", "/api/files/add", {"tag": "Ads", "sources": [{"root": "media", "path": "tvseries/Funny"}]})
        day = lambda *v, ads=(): {"videos": [{"from": a, "to": b, "lists": l} for a, b, l in v],
                                  "commercials": [{"from": a, "to": b, "list": l} for a, b, l in ads]}
        week = lambda d: {n: d for n in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
        today = datetime.datetime.now(datetime.timezone.utc).date()          # the container's clock (UTC unless TZ is set)
        md = today.strftime("%m-%d")
        sched = {"default": week(day((0, 12, ["Comedy"]), (12, 24, ["Drama", "Comedy"]), ads=[(6, 12, "Ads")])),
                 "seasons": {"dates": {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"}, "spring": None,
                             "summer": week(day((0, 24, ["Drama"]))), "fall": None,
                             "winter": week(day((0, 20.5, ["Comedy"]), (20.5, 24, ["Drama"])))},
                 "events": [{"name": "Special", "when": {"type": "range", "start": md, "end": md, "year": today.year}, "replace": False,
                             "mode": "same", "schedule": day((0, 24, ["Drama"]))}]}
        form = {"name": "Seasonal", "number": 8, "kind": "standard", "increment": 30, "bump": "bump", "commercials": "Ads", "schedule": sched}
        code, out = req("POST", "/api/stations/save", {"original": None, "form": form})
        check("the station saves (FieldStation42 accepts the config)", code == 200 and out.get("created"), out)
        code, job = req("POST", "/api/stations/build", {"name": "Seasonal", "days": 3, "rebuild": True})
        done = wait(lambda: (lambda j: j if j["state"] != "running" else None)(req("GET", f"/api/stations/job?id={job['id']}")[1]), "the build", 240)
        check("it builds a catalog and days from the overrides", done["state"] == "done", done)
        got = req("GET", "/api/stations/get?name=Seasonal")[1]
        conf = got["conf"]
        check("it reads back as the same definition, editable", got["form"] and got["why"] is None, got.get("why"))
        check("the config has four season ranges and today's special day", len(conf.get("week_overrides", {})) == 4 and conf.get("date_overrides"), list(conf))
        sched_now = req("GET", "/api/schedule?name=Seasonal")[1]
        check("it has a schedule for today", sched_now.get("blocks") and sched_now.get("scheduled_until"), sched_now.get("scheduled_until"))
        check("the special day is named in the preview", req("POST", "/api/stations/schedule-preview", {"schedule": sched})[1]["days"][0]["name"] == "Special")
    finally:
        subprocess.run(["docker", "stop", NAME], capture_output=True)
        logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
        bad = [l for l in (logs.stdout + logs.stderr).splitlines() if "Traceback" in l or "ConfigurationError" in l]
        check("no tracebacks or configuration errors in the log", not bad, bad[:3])
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
