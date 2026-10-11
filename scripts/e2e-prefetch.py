#!/usr/bin/env python3
"""A developer check, not part of CI: run the image with a RAM-disk cache and a few real (tiny) videos, watch a channel
that has commercial breaks, and prove the read-ahead copies the next break/episode into RAM and plays from the copy.

Needs Docker and ffmpeg on this machine.   Usage: scripts/e2e-prefetch.py [image]    (default station-wrapper-43-smoke:local)
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "station-wrapper-43-smoke:local"
ROOT = tempfile.mkdtemp(prefix="e2e-prefetch-", dir=os.environ.get("E2E_DIR", None))
NAME = f"e2e-prefetch-{os.getpid()}"
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


def make_show(name, genre, seconds=8):
    folder = os.path.join(ROOT, "media", "tvseries", name, "Season 01")
    os.makedirs(folder)
    for i in range(1, 4):
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s=320x240:d={seconds}:r=10",
                        "-f", "lavfi", "-i", f"sine=frequency={400 + 100 * i}:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", os.path.join(folder, f"{name} - S01E0{i}.mp4")], check=True)
    with open(os.path.join(ROOT, "media", "tvseries", name, "tvshow.nfo"), "w") as f:
        f.write(f"<tvshow><title>{name}</title><genre>{genre}</genre></tvshow>")


def main():
    os.makedirs(os.path.join(ROOT, "config"))
    os.makedirs(os.path.join(ROOT, "catalog"))
    make_show("Funny", "Comedy")
    make_show("Serious", "Drama")
    subprocess.run(["docker", "run", "-d", "--name", NAME, "--network", "host", "--tmpfs", "/cache:size=256m,mode=1777",
                    "-e", "DASHBOARD_LOGIN=0", "-e", "FIRSTRUN_TEST_STATION=0", "-e", "FIRSTRUN_GUIDE_STATION=0", "-e", "PREFETCH_MAX_MB=200",
                    "-v", f"{ROOT}/config:/config", "-v", f"{ROOT}/catalog:/app/catalog",
                    "-v", f"{ROOT}/media/tvseries:/mnt/media/tvseries:ro", IMAGE], check=True, capture_output=True)
    viewer = None
    try:
        wait(lambda: req("GET", "/api/files/info")[0] == 200, "the dashboard", 90)
        status = req("GET", "/system.json")[1]["prefetch"]
        check("the read-ahead is on with the RAM disk", status["enabled"] and status["ram"], status)
        wait(lambda: {"Comedy", "Drama"} <= {e["name"] for e in req("GET", "/api/files/list?root=tags&path=")[1]["entries"]}, "the genre lists", 150, 3)
        wait(lambda: req("GET", "/api/stations")[0] == 200, "the FieldStation42 console", 120)
        req("POST", "/api/files/create-tag", {"name": "Ads"})
        req("POST", "/api/files/add", {"tag": "Ads", "sources": [{"root": "media", "path": "tvseries/Serious"}]})
        day = {"videos": [{"from": 0, "to": 24, "lists": ["Comedy"]}], "commercials": []}
        sched = {"default": {d: day for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
                 "seasons": {"dates": {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"}, "spring": None, "summer": None, "fall": None, "winter": None},
                 "events": []}
        form = {"name": "Breaks", "number": 8, "kind": "standard", "increment": 5, "breaks": "standard", "bump": "bump", "commercials": "Ads", "schedule": sched}
        code, out = req("POST", "/api/stations/save", {"original": None, "form": form})
        check("the station saves", code == 200, out)
        code, job = req("POST", "/api/stations/build", {"name": "Breaks", "days": 2, "rebuild": True})
        done = wait(lambda: (lambda j: j if j["state"] != "running" else None)(req("GET", f"/api/stations/job?id={job['id']}")[1]), "the build", 240)
        check("it builds", done["state"] == "done", done)
        wait(lambda: any(c["channel"] == "8" and c["on_air"] for c in req("GET", "/channels.json")[1]), "channel 8 on the dashboard", 120, 2)
        time.sleep(3)
        viewer = subprocess.Popen(["curl", "-s", "-m", "100", "http://127.0.0.1:5004/auto/v8", "-o", "/dev/null"])
        got = wait(lambda: (lambda p: p if p["enabled"] and p["cache"]["files"] >= 1 else None)(req("GET", "/system.json")[1]["prefetch"]), "a read-ahead copy", 90, 3)
        check("something was copied into RAM for the channel being watched", got["cache"]["files"] >= 1, got)
        check("it measured the share", any(v.get("samples") for v in got["shares"].values()), got["shares"])
        time.sleep(40)
        logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        check("playback used the copies", "[from the read-ahead copy]" in text, [l for l in text.splitlines() if "[ch8]" in l][-5:])
        mem = subprocess.run(["docker", "exec", NAME, "sh", "-c", "df -k /cache | tail -1"], capture_output=True, text=True).stdout
        check("the cache is in RAM (tmpfs), not on a drive", subprocess.run(["docker", "exec", NAME, "grep", " /cache tmpfs ", "/proc/mounts"], capture_output=True).returncode == 0, mem)
    finally:
        if viewer:
            viewer.kill()
        subprocess.run(["docker", "stop", NAME], capture_output=True)
        logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
        bad = [l for l in (logs.stdout + logs.stderr).splitlines() if "Traceback" in l or "[prefetch] pass failed" in l]
        check("no tracebacks or failed passes in the log", not bad, bad[:3])
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
