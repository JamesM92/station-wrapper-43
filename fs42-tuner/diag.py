"""What the Status page and the diagnostics download are made of.

Almost every problem on this system has been found by reading container logs. This keeps the last lines of what the
tuner said (so the page can show them), measures how fast the media share reads, and turns the state of the system into a
short list of plain-language checks: what is wrong, what to look at, and what is fine.
"""
import builtins
import collections
import os
import re
import shutil
import threading
import time

PROBLEM_WORDS = re.compile(r"(^|\W)(error|failed|could not|cannot|unreachable|refused|traceback|exception|!)(\W|$)|^! ", re.I)


class LogRing:
    """The last lines printed by this process, with when they were printed. install() makes print() record into it."""

    def __init__(self, size=400):
        self.lines = collections.deque(maxlen=size)
        self._lock = threading.Lock()
        self._real = None

    def add(self, text):
        with self._lock:
            for line in str(text).splitlines() or [""]:
                if line.strip():
                    self.lines.append((time.time(), line[:500]))

    def install(self):
        if self._real is not None:
            return
        self._real = builtins.print
        ring, real = self, builtins.print

        def recording_print(*args, **kw):
            real(*args, **kw)
            if kw.get("file") is None:
                try:
                    ring.add(kw.get("sep", " ").join(str(a) for a in args))
                except Exception:
                    pass
        builtins.print = recording_print

    def recent(self, n=60, problems_only=False):
        with self._lock:
            rows = list(self.lines)
        if problems_only:
            rows = [r for r in rows if PROBLEM_WORDS.search(r[1])]
        return [{"at": a, "line": l} for a, l in rows[-n:]]


RING = LogRing()


def speed_test(media_root, want_bytes=24 * 1024 * 1024, max_seconds=20, limit=3000):
    """Read part of one video from the media and say how fast it came: {"file", "mb", "seconds", "mb_per_s"} or {"error"}."""
    exts = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm", ".mpg", ".wmv"}
    seen = 0
    target = None
    try:
        for root, dirs, files in os.walk(media_root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))[:50]
            for f in sorted(files):
                seen += 1
                if os.path.splitext(f)[1].lower() in exts and not f.startswith("."):
                    p = os.path.join(root, f)
                    if os.path.getsize(p) >= 2 * 1024 * 1024:
                        target = p
                        break
            if target or seen > limit:
                break
    except OSError as e:
        return {"error": f"cannot look in {media_root}: {e.strerror}"}
    if not target:
        return {"error": f"no video found under {media_root} (is the media mounted?)"}
    began, n = time.time(), 0
    try:
        with open(target, "rb", buffering=0) as f:
            f.seek(min(os.path.getsize(target) // 3, 200 * 1024 * 1024))          # not the start: it is the part likely cached
            while n < want_bytes and time.time() - began < max_seconds:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                n += len(chunk)
    except OSError as e:
        return {"error": f"cannot read {os.path.basename(target)}: {e.strerror}"}
    secs = max(time.time() - began, 1e-6)
    return {"file": os.path.basename(target), "mb": round(n / 1048576, 1), "seconds": round(secs, 2), "mb_per_s": round(n / 1048576 / secs, 1)}


def disk_free(paths):
    out = {}
    for name, p in paths.items():
        try:
            u = shutil.disk_usage(p)
            out[name] = {"path": p, "free_mb": u.free // 1048576, "total_mb": u.total // 1048576}
        except OSError:
            out[name] = {"path": p, "free_mb": None, "total_mb": None}
    return out


SECRET = re.compile(r"(pass|secret|token|auth|key)", re.I)


def redact(obj):
    """A copy with every value under a secret-looking key replaced (a diagnostics file is meant to be sent to someone)."""
    if isinstance(obj, dict):
        return {k: ("(hidden)" if SECRET.search(str(k)) and not isinstance(v, (dict, list)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def checks(report):
    """The state of the system as a short list of {"level": "error"|"warn"|"ok", "text"}, worst first."""
    out = []

    def add(level, text, href=None, fix=None):
        row = {"level": level, "text": text}
        if href:
            row["href"], row["fix"] = href, fix or "Fix it"        # where to go to put it right
        out.append(row)
    con = report.get("console", {})
    if not con.get("reachable"):
        add("error", f"FieldStation42's console is not reachable ({con.get('error', 'no answer')}). Stations can't be changed or built until it is back; check the container log for [console] lines.", "/status.html", "Look again")
    for s in report.get("stations", []):
        if s.get("type") == "guide":
            continue
        if not s.get("scheduled_until"):
            add("warn", f"Station {s['name']} has no schedule yet: build it on the Stations page.", "/stations.html", "Open Stations")
        elif not s.get("on_air"):
            add("error", f"Station {s['name']} is off air: its schedule ran out. Add days on the Stations page (or switch on Keep schedules going).", "/stations.html", "Open Stations")
        elif s.get("low"):
            add("warn", f"Station {s['name']} has less than a day of schedule left.", "/stations.html", "Add days")
        if s.get("number") == 0:
            add("warn", f"Station {s['name']} is on channel 0, which Jellyfin ignores: give it a number from 1 up.", "/stations.html", "Open Stations")
    for src in report.get("sources", []):
        if src.get("enabled") and src.get("error"):
            add("error", f"Source {src['name']} could not be read: {src['error']}", "/iptv.html", "Open Sources")
    media = report.get("media", {})
    if media.get("present") is False:
        add("error", f"The media folder {media.get('path')} is missing or empty: is the share mounted?", "/files.html", "Open Lists & files")
    for name, d in (report.get("disk") or {}).items():
        if d.get("free_mb") is not None and d["free_mb"] < 1024:
            add("warn", f"Only {d['free_mb']} MB free on the {name} volume.")
    g = report.get("guide", {})
    if g.get("last_build") and g["last_build"].get("seconds", 0) > 60:
        add("warn", f"The guide took {g['last_build']['seconds']:.0f}s to build: a listings source is slow.")
    for k, v in (g.get("sources") or {}).items():
        if isinstance(v, dict) and v.get("error"):
            add("warn", f"Guide source {k} failed: {v['error']}", "/iptv.html", "Open Sources")
    if not report.get("ffmpeg", {}).get("found"):
        add("error", "ffmpeg was not found: nothing can be streamed.")
    if not out:
        add("ok", "Nothing is wrong that this page can see.")
    order = {"error": 0, "warn": 1, "ok": 2}
    return sorted(out, key=lambda c: order[c["level"]])


def as_text(report):
    """The report as plain text for the diagnostics download."""
    import json
    lines = ["Station Wrapper 43: diagnostics", "=" * 40, ""]
    lines += [f"[{c['level']}] {c['text']}" for c in report.get("checks", [])]
    body = {k: v for k, v in report.items() if k not in ("checks", "recent")}
    lines += ["", "State", "-" * 40, json.dumps(redact(body), indent=2, default=str), "", "Recent messages", "-" * 40]
    for r in report.get("recent", []):
        lines.append(time.strftime("%H:%M:%S", time.localtime(r["at"])) + "  " + r["line"])
    return "\n".join(lines) + "\n"
