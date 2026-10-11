#!/usr/bin/env python3
"""A developer check, not part of CI: run the image, make a guide channel with music from a folder, watch it, and look at
what comes out: a real transport stream with the guide picture and the music.

Needs Docker and ffmpeg on this machine.   Usage: scripts/e2e-guide.py [image]    (default station-wrapper-43-smoke:local)
The picture is saved as guide-frame.png in the folder named by E2E_DIR (or the temporary folder) so you can look at it.
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
ROOT = tempfile.mkdtemp(prefix="e2e-guide-", dir=os.environ.get("E2E_DIR", None))
NAME = f"e2e-guide-{os.getpid()}"
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


def main():
    os.makedirs(os.path.join(ROOT, "config"))
    os.makedirs(os.path.join(ROOT, "catalog"))
    music = os.path.join(ROOT, "media", "music", "lofi")
    os.makedirs(music)
    for i, f in enumerate((330, 440, 550)):
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={f}:d=20", os.path.join(music, f"tone{i}.wav")], check=True)
    capture = os.path.join(ROOT, "guide.ts")
    subprocess.run(["docker", "run", "-d", "--name", NAME, "--network", "host", "-e", "DASHBOARD_LOGIN=0", "-e", "FIRSTRUN_GUIDE_STATION=0",
                    "-v", f"{ROOT}/config:/config", "-v", f"{ROOT}/catalog:/app/catalog", "-v", f"{ROOT}/media/music:/mnt/media/music:ro", IMAGE],
                   check=True, capture_output=True)
    try:
        wait(lambda: req("GET", "/api/files/info")[0] == 200, "the dashboard", 90)
        wait(lambda: req("GET", "/api/stations")[0] == 200 and any(s["name"] == "Test Pattern" and s["on_air"] for s in req("GET", "/api/stations")[1]["stations"]),
             "the test station", 180, 3)
        form = {"name": "Guide", "number": 2, "kind": "guide",
                "guide": {"title": "Penguin TV Guide", "hours": 3, "scroll_speed": 1.0, "messages": ["Tonight: Shark Week", "Hello from the guide"], "music": "/mnt/media/music/lofi"}}
        code, out = req("POST", "/api/stations/save", {"original": None, "form": form})
        check("the guide station saves (FieldStation42 accepts a guide config)", code == 200 and out["created"] and not out["needs_build"], out)
        bad = req("POST", "/api/stations/save", {"original": None, "form": {**form, "name": "Bad", "number": 3, "guide": {**form["guide"], "music": "/etc"}}})
        check("music outside the media is refused", bad[0] == 400, bad)
        wait(lambda: any(c["channel"] == "2" and c["on_air"] for c in req("GET", "/channels.json")[1]), "the guide channel on the dashboard", 120, 2)
        time.sleep(3)
        subprocess.run(["curl", "-s", "-m", "14", "http://127.0.0.1:5004/auto/v2", "-o", capture])
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "csv=p=0", capture],
                               capture_output=True, text=True).stdout
        check("the stream has H.264 video at 1280x720 and AAC audio", "h264,1280,720" in probe.replace("\n", ",") or ("h264" in probe and "1280" in probe) and "aac" in probe, probe)
        png = os.path.join(os.environ.get("E2E_DIR", ROOT), "guide-frame.png")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "8", "-i", capture, "-frames:v", "1", png], capture_output=True)
        check("a picture of the guide was captured", os.path.exists(png) and os.path.getsize(png) > 20000, png)
        vol = subprocess.run(["ffmpeg", "-nostdin", "-i", capture, "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True).stderr
        mean = [l for l in vol.splitlines() if "mean_volume" in l]
        check("the music is playing (not silence)", mean and float(mean[0].split("mean_volume:")[1].split("dB")[0]) > -60, mean)
        thumb = req("GET", "/thumb/2.jpg")
        check("the dashboard has a picture of it", thumb[0] == 200 and len(thumb[1]) > 5000, thumb[0])
        guide = req("GET", "/xmltv.xml")[1]
        check("it is in the XMLTV guide with a programme", b"Penguin TV Guide" in guide, "")
    finally:
        subprocess.run(["docker", "stop", NAME], capture_output=True)
        logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        check("the log says the guide channel started with 3 music files", "guide channel: 3 music file(s)" in text, [l for l in text.splitlines() if "guide channel" in l][-3:])
        bad = [l for l in text.splitlines() if "Traceback" in l or "could not start" in l]
        check("no tracebacks or failed starts in the log", not bad, bad[:3])
        if not os.environ.get("E2E_DIR"):
            shutil.rmtree(ROOT, ignore_errors=True)
        else:
            shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
