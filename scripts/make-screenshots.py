#!/usr/bin/env python3
"""A developer tool, not part of CI: run the image with the repo's code and the demo media, make a couple of stations, and take
the screenshots in docs/images with a headless browser.

Needs Docker, ffmpeg, and Chromium's headless shell (Playwright's is found in ~/.cache/ms-playwright). --media is a folder of
test videos you have: it expects shows/<series>/..., movies/..., commercials/ and cards/ inside it.
    python3 scripts/make-screenshots.py --media /path/to/test-media [--image station-wrapper-43-smoke:local] [--out docs/images]
"""
import argparse
import base64
import glob
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PORT = 5004


class Browser:
    """A very small Chrome DevTools client using only the standard library."""

    def __init__(self, width=1280, height=860):
        exe = (glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
               or glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))[-1]
        self.p = subprocess.Popen([exe, "--no-sandbox", "--remote-debugging-port=9334", "--hide-scrollbars", "--force-dark-mode",
                                   f"--window-size={width},{height}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tabs = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json"))  # nosec B310 - the browser we started, on this machine
                break
            except Exception:
                time.sleep(0.25)
        ws = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        self.s = socket.create_connection(("127.0.0.1", 9334))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{ws.split('/', 3)[3]} HTTP/1.1\r\nHost: 127.0.0.1:9334\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(1)
        self.n = 0
        self.width, self.height = width, height
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    def _send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        head = b"\x81" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536 else b"\xff" + struct.pack(">Q", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self):
        def need(k):
            b = b""
            while len(b) < k:
                c = self.s.recv(k - len(b))
                if not c:
                    raise EOFError
                b += c
            return b
        h = need(2)
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", need(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", need(8))[0]
        return need(n)

    def call(self, method, **params):
        self.n += 1
        self._send({"id": self.n, "method": method, "params": params})
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == self.n:
                return msg.get("result", msg)

    def go(self, url, wait=2.0):
        self.call("Page.navigate", url=url)
        time.sleep(wait)

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:400])
        return r["result"].get("value")

    def shot(self, path, full=True):
        if full:                                           # the whole page, however long
            h = self.js("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
            self.call("Emulation.setDeviceMetricsOverride", width=self.width, height=min(int(h), 3000), deviceScaleFactor=1, mobile=False)
            time.sleep(0.3)
        data = self.call("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        self.call("Emulation.setDeviceMetricsOverride", width=self.width, height=self.height, deviceScaleFactor=1, mobile=False)

    def clip(self, selector, path, pad=8):
        """A picture of one element of the page (its panel), however far down it is."""
        r = self.js("(() => { const e = document.querySelector(%s); if (!e) return null; const b = e.getBoundingClientRect();"
                    " return [b.left + scrollX, b.top + scrollY, b.width, b.height]; })()" % json.dumps(selector))
        if not r:
            raise RuntimeError(f"nothing matches {selector}")
        x, y, w, h = r
        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                         clip={"x": max(x - pad, 0), "y": max(y - pad, 0), "width": w + 2 * pad, "height": h + 2 * pad, "scale": 1})["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))

    def close(self):
        self.p.terminate()


def req(method, path, body=None, timeout=120):
    """One call. The dashboard restarts for a few seconds when a station is added, so a refused connection is waited out
    (nothing was sent, so it is safe to try again)."""
    for attempt in range(60):
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
            c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            raw = r.read()
            break
        except (ConnectionRefusedError, ConnectionResetError, http.client.RemoteDisconnected):
            time.sleep(1.5)
    else:
        raise RuntimeError("the dashboard did not come back")
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, raw


def wait(fn, what, secs=180, every=2):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media", required=True)
    ap.add_argument("--image", default="station-wrapper-43-smoke:local")
    ap.add_argument("--out", default=os.path.join(REPO, "docs", "images"))
    ap.add_argument("--hdhomerun", help="the address of an HDHomeRun on your network (http://192.168.1.20): added as a GUIDE ONLY source, "
                    "so the TV guide shows an antenna tuner's channels with their real listings and the Sources page shows how it is set")
    ap.add_argument("--keep", action="store_true", help="leave the demo container running when done (stop it with docker rm -f), "
                    "for pointing a Jellyfin at it")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    root = tempfile.mkdtemp(prefix="shots-")
    for d in ("config", "catalog", "wrapper", "supervisor"):
        os.makedirs(os.path.join(root, d))
    for src in ("scripts/init.py", "scripts/build_channels.py", "scripts/fs42_link.py", "scripts/firstrun.py", "scripts/update_catalog.py",
                "fs42-tuner/schedlock.py", "fs42-tuner/rundir.py", "fs42-tuner/webget.py", "fs42-tuner/ordering.py",
                "fieldstation42/console_launch.py"):
        shutil.copy(os.path.join(REPO, src), os.path.join(root, "wrapper", os.path.basename(src)))
    for src in ("supervisor/supervisor.py", "fs42-tuner/stations.py", "fs42-tuner/rundir.py"):
        shutil.copy(os.path.join(REPO, src), os.path.join(root, "supervisor", os.path.basename(src)))
    name = f"shots-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "-e", "DASHBOARD_LOGIN=0", "-e", "SYNC_ENABLED=0",
                    "-e", f"PUID={os.getuid()}", "-e", f"PGID={os.getgid()}", "--tmpfs", "/cache:size=1g,mode=1777",
                    "-e", "PREFETCH_STREAM_ANY=1", "-e", "PREFETCH_STREAM_MIN_MB=20", "-e", "PREFETCH_MAX_MB=900",
                    "-v", f"{root}/config:/config", "-v", f"{root}/catalog:/app/catalog", "-v", f"{os.path.abspath(a.media)}:/mnt/media:ro",
                    "-v", f"{REPO}/fs42-tuner:/opt/tuner:ro", "-v", f"{root}/wrapper:/opt/wrapper:ro",
                    "-v", f"{root}/supervisor:/opt/supervisor:ro", "-v", f"{REPO}/assets:/opt/assets:ro", a.image], check=True, capture_output=True)
    b = None
    try:
        wait(lambda: req("GET", "/api/files/info")[0] == 200, "the dashboard", 150)
        wait(lambda: req("GET", "/api/stations")[0] == 200, "the console", 180)
        base = f"http://127.0.0.1:{PORT}"
        b = Browser()
        shots = os.path.join(a.out)

        # a fresh install: the guide channel and the test pattern, and the getting-started card
        time.sleep(8)
        b.go(f"{base}/index.html", 3)
        b.shot(os.path.join(shots, "dashboard-first-run.png"), full=False)

        # lists from the demo media
        def make(tag, *paths):
            req("POST", "/api/files/create-tag", {"name": tag})
            req("POST", "/api/files/add", {"tag": tag, "sources": [{"root": "media", "path": p} for p in paths], "follow": True})
        make("Superman cartoons", "shows/Fleischer Superman Cartoons")
        make("Holiday cartoons", "shows/Holiday Cartoons")
        make("Movies", *[f"movies/{m}" for m in os.listdir(os.path.join(a.media, "movies"))])
        make("Commercials", "commercials")
        make("We'll be right back", "cards/brb.mp4")
        make("Welcome back", "cards/welcome-back.mp4")

        day = lambda *v, ads=(): {"videos": [{"from": f, "to": t, "lists": l} for f, t, l in v],
                                  "commercials": [{"from": f, "to": t, "list": l} for f, t, l in ads], "cards": []}
        week = lambda d: {n: d for n in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
        sched = lambda d: {"default": week(d), "events": [], "seasons": {"dates": {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"},
                                                                         "spring": None, "summer": None, "fall": None, "winter": None}}
        eps = req("GET", "/api/stations/show-episodes?list=Holiday%20cartoons")[1]
        second = (eps.get("episodes") or [""])[min(1, max(len(eps.get("episodes") or []) - 1, 0))]
        DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        weeknight = {}                                    # Mon-Fri 12:00-13:00 is ONE linked slot of Holiday cartoons, Friday its premiere night
        for d in DAYS:
            videos = [{"from": 0, "to": 12, "lists": ["Superman cartoons"]}, {"from": 12, "to": 13, "lists": ["Holiday cartoons"]},
                      {"from": 13, "to": 24, "lists": ["Superman cartoons"]}]
            if d in DAYS[:5]:
                videos[1]["span"] = "weeknight1"
            if d == "friday":
                videos[1]["premiere"] = True
            weeknight[d] = {"videos": videos, "commercials": [], "cards": []}
        stations = [
            {"name": "Saturday Cartoons", "number": 3, "kind": "standard", "increment": 15, "pack": False, "bump": "We'll be right back", "bump_end": "Welcome back",
             "commercials": "Commercials", "midcards": True, "gen_cards": True, "breaks": "end",
             "schedule": sched(day((0, 12, ["Superman cartoons"]), (12, 24, ["Holiday cartoons"])))},
            {"name": "Weeknight Cartoons", "number": 5, "kind": "standard", "increment": 30, "pack": False, "bump": "We'll be right back", "bump_end": "Welcome back",
             "commercials": "Commercials", "midcards": False,
             "schedule": {"default": weeknight, "events": [], "premieres": {"Holiday cartoons": {"from": second, "since": time.strftime("%Y-%m-%d")}},
                          "seasons": {"dates": {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"},
                                      "spring": None, "summer": None, "fall": None, "winter": None}}},
            {"name": "Commercial Break TV", "number": 6, "kind": "standard", "increment": 5, "pack": True, "bump": "We'll be right back", "commercials": "", "midcards": False,
             "schedule": sched(day((0, 24, ["Commercials"])))},
            {"name": "Movie Night", "number": 4, "kind": "standard", "increment": 15, "bump": "We'll be right back", "commercials": "", "midcards": False,
             "pack": False,
             "schedule": sched(day((0, 24, ["Movies"])))},
        ]
        def build(name_, days=2):
            """Build, and again if the dashboard restarted under the build (a new station makes the supervisor restart it)."""
            for _attempt in range(4):
                code, job = req("POST", "/api/stations/build", {"name": name_, "days": days, "rebuild": True})
                if code != 200:
                    time.sleep(5)
                    continue
                end = time.time() + 300
                while time.time() < end:
                    time.sleep(2)
                    st = req("GET", f"/api/stations/job?id={job['id']}")[1]
                    if isinstance(st, dict) and st.get("state") == "done":
                        return
                    if not isinstance(st, dict) or "state" not in st:      # the job is gone: the dashboard restarted
                        break
            raise RuntimeError(f"could not build {name_}")

        for f in stations:
            code, out = req("POST", "/api/stations/save", {"original": None, "form": f})
            assert code == 200, out
            time.sleep(12)                                  # the dashboard restarts for a new station
            wait(lambda: req("GET", "/api/stations")[0] == 200, "the dashboard", 60)
            build(f["name"])
        # somebody watching two channels, so the joins between pieces are recorded (the dashboard's Joins card)
        import threading

        def watch(ch, secs):
            try:
                r = urllib.request.urlopen(f"{base}/auto/v{ch}", timeout=30)      # nosec B310 - the container started above
                end = time.time() + secs
                while time.time() < end and r.read(188 * 64):
                    pass
                r.close()
            except Exception:
                pass
        for ch in (3, 6):
            threading.Thread(target=watch, args=(ch, 900), daemon=True).start()
        if a.hdhomerun:
            code, out = req("POST", "/api/iptv/save", {"name": "Antenna", "type": "hdhomerun", "url": a.hdhomerun, "mode": "guide"})   # guide only: Jellyfin takes the channels from the tuner itself
            assert code == 200, out
            wait(lambda: any(s["channels"] for s in req("GET", "/api/iptv/sources")[1]["sources"]), "the tuner's channels", 90)
            req("GET", "/xmltv.xml?refresh")                 # the antenna's listings, now
            time.sleep(10)
        time.sleep(15)

        # the library is scanned in the background: wait for it, so the pages show real counts
        req("GET", "/library.json?refresh")
        wait(lambda: not req("GET", "/library.json")[1].get("never") and not req("GET", "/library.json")[1].get("scanning"), "the library scan", 120, 3)

        def page(path, out, wait_s=2.5, full=True):
            b.go(f"{base}/{path}", wait_s)
            b.shot(os.path.join(shots, out), full=full)

        def in_programme(number):
            """Is this channel on a programme (not a commercial or a card) right now?"""
            for c in req("GET", "/channels.json")[1]:
                if str(c.get("channel")) == str(number):
                    return ((c.get("now") or {}).get("content_type") or "feature") == "feature"
            return True
        for number in (3, 4):                                 # a picture taken in a break shows a commercial: wait it out
            try:
                wait(lambda n=number: in_programme(n), f"channel {number} to be on a programme", 240, 4)
            except RuntimeError:
                pass
        # the dashboard shows a picture only for a channel somebody is watching (a channel nobody watches says "idle"): watch
        # the two programme channels from here for a few seconds, as a viewer would
        import threading

        def watch(number, seconds=90):
            try:
                with urllib.request.urlopen(f"{base}/auto/v{number}", timeout=30) as r:    # nosec B310 - the container started above
                    t0 = time.time()
                    while time.time() - t0 < seconds and r.read(65536):
                        pass
            except Exception:
                pass
        for number in (3, 4):
            threading.Thread(target=watch, args=(number,), daemon=True).start()
        time.sleep(40)                                  # the encoders start and run on a little, past a dark opening
        # the card drawn for Saturday Cartoons: a frame of the clip (it is made in the background after the save)
        wait(lambda: (req("GET", "/api/stations/get?name=Saturday%20Cartoons")[1].get("cards") or {}).get("state") in ("ok", "error"),
             "the mid cards to be drawn", 240, 4)
        for kind in ("start", "end"):
            clips = glob.glob(os.path.join(root, "catalog", "**", ".generated-cards", f"* - {kind}.mp4"), recursive=True)
            if clips:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", clips[0], "-frames:v", "1",
                                os.path.join(shots, f"mid-card-{kind}.png")], capture_output=True)
        page("index.html", "dashboard.png", 8, full=True)
        page("stations.html", "stations.png", 3)
        b.go(f"{base}/files.html", 3)
        b.js("document.getElementById('ta-details') && (document.getElementById('ta-details').open = false)")
        b.js("[...document.querySelectorAll('#src-list li button')].find(x => x.textContent.startsWith('shows'))?.click()")
        time.sleep(1.2)
        b.js("[...document.querySelectorAll('#src-list li button')].find(x => x.textContent.startsWith('Fleischer'))?.click()")
        time.sleep(1.0)
        b.js("[...document.querySelectorAll('#tag-list li')].find(l => l.textContent.includes('Superman'))?.click()")
        time.sleep(0.8)
        b.clip(".files-cols", os.path.join(shots, "files.png"))
        # the schedule of the cartoon channel with a block opened that has commercials and mid cards in it
        b.go(f"{base}/schedule.html", 3)
        b.js("(() => { const sel = document.getElementById('station'); const o = [...sel.options].find(x => x.textContent.includes('Saturday')); "
             "if (o) { sel.value = o.value; sel.dispatchEvent(new Event('change')); } })()")
        time.sleep(2.5)
        for _ in range(2):                                    # the container's clock may be a day ahead of this browser's
            if b.js("document.body.innerText.includes('Nothing is scheduled on this day')"):
                b.js("document.getElementById('next').click()")
                time.sleep(2)
        for i in range(8):                                    # open blocks until one shows a break (commercials and mid cards)
            b.js(f"document.querySelectorAll('.twisty')[{i}] && document.querySelectorAll('.twisty')[{i}].click()")
            time.sleep(1.5)
            if b.js("document.body.innerText.includes('commercial') && document.body.innerText.includes('mid card')"):
                break
        b.shot(os.path.join(shots, "schedule.png"), full=False)           # the top of the day: the opened block and a few after it
        b.go(f"{base}/iptv.html", 3)
        b.js("document.querySelectorAll('details').forEach(d => d.open = true)")
        b.js("(() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n; "
             "while ((n = w.nextNode())) n.nodeValue = n.nodeValue.replace(/192\\.168\\.\\d+\\.\\d+/g, '192.168.1.50'); })()")   # a private address, tidied for a picture
        time.sleep(0.5)
        b.shot(os.path.join(shots, "sources.png"), full=True)
        page("status.html", "status.png", 4)

        # the schedule editor: the week, a slot chosen, and the one browser for what plays
        b.go(f"{base}/station.html?name=Saturday%20Cartoons", 4)
        b.js("document.querySelectorAll('.cal-block:not(.ghost)')[1].dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        b.js("""(() => { const col = document.querySelectorAll('.cal-col')[0]; const blocks = [...col.querySelectorAll('.cal-block:not(.ghost)')];
          const r = blocks[1].getBoundingClientRect();
          const ev = (t) => blocks[1].dispatchEvent(new PointerEvent(t, {bubbles: true, clientX: r.left + 8, clientY: r.top + 20, pointerId: 1, button: 0, isPrimary: true}));
          ev('pointerdown'); window.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, clientX: r.left + 8, clientY: r.top + 20, pointerId: 1})); })()""")
        time.sleep(0.8)
        b.js("(() => { const x = document.querySelector('#slot-editor .chooser > button'); if (x) x.click(); })()")
        time.sleep(0.5)
        b.js("[...document.querySelectorAll('#slot-editor .chooser-row .link')].find(l => l.textContent.startsWith('Media'))?.click()")
        time.sleep(0.8)
        b.clip("#sched", os.path.join(shots, "calendar.png"))
        b.js("document.querySelector('#layer-tabs button:nth-child(2)')?.click()")
        time.sleep(0.5)
        b.js("[...document.querySelectorAll('.panel')].find(p => (p.querySelector('h2') || {}).textContent === 'Details').id = 'details-panel'")
        b.clip("#details-panel", os.path.join(shots, "station-details.png"))
        # one linked slot Monday to Friday, with Friday as its premiere night: the editor with the preview of the nights
        b.go(f"{base}/station.html?name=Weeknight%20Cartoons", 4)
        b.js("""(() => { const col = document.querySelector('.cal-col[data-key="friday"]');
          const blk = [...col.querySelectorAll('.cal-block:not(.ghost)')].find((x) => x.textContent.includes('Holiday'));
          const r = blk.getBoundingClientRect();
          const at = {bubbles: true, clientX: r.left + 8, clientY: r.top + 8, pointerId: 1, button: 0, isPrimary: true};
          blk.dispatchEvent(new PointerEvent('pointerdown', at)); window.dispatchEvent(new PointerEvent('pointerup', at)); })()""")
        time.sleep(3.0)
        b.clip("#sched", os.path.join(shots, "calendar-premiere.png"))
        b.go(f"{base}/station.html", 3)
        b.clip("form", os.path.join(shots, "station-new.png"))

        # the TV guide channel: drawn here from the container's own guide, at the top of its scroll (a frame of the live stream
        # would catch it part way down)
        sys.path.insert(0, os.path.join(REPO, "fs42-tuner"))
        import guide_channel
        from PIL import Image
        xml = urllib.request.urlopen(f"{base}/xmltv.xml", timeout=120).read()  # nosec B310 - the container started above
        g = guide_channel.Guide(guide_channel.options({}), (1280, 720),
                                rows_fn=lambda lo, hi: guide_channel.rows_from_xmltv(xml, lo, hi, skip_names=("Channel Guide",)))
        Image.frombytes("RGB", (1280, 720), g.frame(0.0)).save(os.path.join(shots, "tv-guide.png"))
        print("screenshots in", shots)
        if a.keep:
            print(f"left running: container {name}, dashboard http://<this host>:5004 (stop it: docker rm -f {name})")
    finally:
        if b:
            b.close()
        if not a.keep:                                       # (a return here would hide an error: leave the container and say so)
            subprocess.run(["docker", "stop", name], capture_output=True)
            subprocess.run(["docker", "rm", name], capture_output=True)
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
