#!/usr/bin/env python3
"""The TubeArchivist conversion job. It always runs, and does nothing until you switch it on.

Settings (on/off, which folder is the TubeArchivist library, how often) come from the dashboard
(settings.json in the config volume); the TA_* environment variables are only the starting values.
It looks at them every few seconds, so a change takes effect without restarting the container.

When on, it runs TA_NFOWriter once per library: read the library (read-only) and build its "view" in
the catalog volume, catalog/tanfo/<folder name>-nfo, a folder of readable channels whose videos are
SYMLINKS written with this container's own paths. Several libraries each get a view of their own.
Nothing is written to a library. It runs when switched on, when you press Convert now,
when the library folder changes, and then every `interval_minutes`. A run that finishes leaves
/tmp/farm.now so the farm job makes tag folders for new channels right away.

What it is doing is written to /tmp/ta-status.json for the dashboard to show.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

try:
    import settings
except ImportError:         # run from the repo: the module sits in the tuner's folder
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs42-tuner"))
    import settings

VIEW = os.environ.get("TA_VIEW", "/app/catalog/tanfo")
VIEW_SUFFIX = settings.VIEW_SUFFIX
ARGS = os.environ.get("TA_ARGS", "--cleanup --playlists --quiet --progress").split()
TA_NFO = os.environ.get("TA_NFO", "/opt/ta-nfo/ta_nfo.py")
NETWORK_FS = {"cifs", "smb", "smb2", "smb3", "smbfs"}
POLL = float(os.environ.get("TA_POLL_SECONDS", "5"))

SAMPLE_SECONDS = float(os.environ.get("TA_SAMPLE_SECONDS", "3"))
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")           # how TubeArchivist names a channel folder
UNIQUEID = re.compile(r"<uniqueid[^>]*>(UC[A-Za-z0-9_-]{22})</uniqueid>")

stopping = False
child = None


def say(msg):
    print(f"ta-nfo: {msg}", flush=True)


def fs_type(path):
    try:
        return subprocess.run(["stat", "-f", "-c", "%T", path], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def channel_count(view=None):
    view = view or VIEW
    try:
        return sum(1 for d in os.listdir(view) if os.path.isfile(os.path.join(view, d, "tvshow.nfo")))
    except OSError:
        return 0


def problem(library, env=os.environ):
    """Why a run can't start, or None."""
    if not library or not os.path.isdir(library):
        return (f"there is no TubeArchivist library at {library or '(not set)'}. Choose its folder on the "
                f"dashboard's Files page (it must be inside a mounted media volume).")
    os.makedirs(VIEW, exist_ok=True)
    if fs_type(VIEW) in NETWORK_FS and env.get("FS42_ALLOW_NETWORK_CATALOG") != "1":
        return (f"{VIEW} is on an SMB/CIFS share, where symlinks don't work. Use local disk "
                f"(the catalog volume, FS42_CATALOG_ROOT).")
    return None


def channel_names(view):
    """{channel id: the channel's folder name in the view}, read from each channel's tvshow.nfo."""
    out = {}
    try:
        folders = os.listdir(view)
    except OSError:
        return out
    for d in folders:
        try:
            with open(os.path.join(view, d, "tvshow.nfo"), encoding="utf-8", errors="replace") as f:
                m = UNIQUEID.search(f.read(4000))
        except OSError:
            continue
        if m:
            out[m.group(1)] = d
    return out


def open_channel(pid, library):
    """The id of the channel whose video the converter has open right now, or None. The converter prints nothing while
    it reads a channel (it is run quietly), so what it has open is the one way to see where it is."""
    roots = {library.rstrip("/"), os.path.realpath(library).rstrip("/")}
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        for root in roots:
            if target.startswith(root + "/"):
                first = target[len(root) + 1:].split("/", 1)[0]
                if CHANNEL_ID.match(first):
                    return first
    return None


def watch_progress(proc, library, view, stop):
    """While the converter runs, say which channel it is on (and how far through the library) in the status the
    dashboard shows. Looks every few seconds; never touches the converter."""
    try:
        ids = sorted(d for d in os.listdir(library) if CHANNEL_ID.match(d))
    except OSError:
        ids = []
    names = {}
    while not stop.wait(SAMPLE_SECONDS) and proc.poll() is None:
        cid = open_channel(proc.pid, library)
        if not cid:
            continue
        if cid not in names:
            names.update(channel_names(view))
        shown = names.get(cid)
        if shown:
            shown = re.sub(r"\s*\[UC[A-Za-z0-9_-]{22}\]$", "", shown)
        settings.write_status(channel=shown or f"{cid} (new, not named yet)", channel_id=cid,
                              channel_pos=(ids.index(cid) + 1) if cid in ids else None, channel_total=len(ids) or None,
                              channel_seen=time.time())


_NUM = r"([\d,]+)"
_P_TOTAL = re.compile(rf"^{_NUM} channels, {_NUM} videos to look at")
_P_CHANNEL = re.compile(rf"^\[{_NUM}/{_NUM}\] (UC[A-Za-z0-9_-]{{22}}):")
_P_BEAT = re.compile(rf"^\s+\.\.\. (UC[A-Za-z0-9_-]{{22}}): {_NUM}/{_NUM} videos")
_P_OTHER = re.compile(r"^time: ")


def _n(text):
    return int(text.replace(",", ""))


def parse_progress(line):
    """What a --progress line of TA_NFOWriter says, as status fields, or None when it is not one. Four kinds: the total
    up front, a line when a channel is finished, a heartbeat every 30 s inside a big one, and the timing at the end."""
    m = _P_TOTAL.match(line)
    if m:
        return {"channel_total": _n(m.group(1)), "videos_total": _n(m.group(2))}
    m = _P_CHANNEL.match(line)
    if m:
        return {"channels_done": _n(m.group(1)), "channel_total": _n(m.group(2)), "last_channel_id": m.group(3)}
    m = _P_BEAT.match(line)
    if m:
        return {"channel_id": m.group(1), "channel_videos_done": _n(m.group(2)), "channel_videos_total": _n(m.group(3))}
    return {} if _P_OTHER.match(line) else None


def run_once(library, view):
    """One conversion of one library into its view folder. Returns (ok, message)."""
    global child
    os.makedirs(view, exist_ok=True)
    done = threading.Event()
    settings.write_status(channel=None, channel_id=None, channel_pos=None, channel_total=None, channel_seen=None,
                          channels_done=None, videos_total=None, channel_videos_done=None, channel_videos_total=None)
    cmd = [sys.executable, TA_NFO, library, "--view-dir", view, *ARGS]
    say(f"{time.strftime('%Y-%m-%d %H:%M:%S')} updating {view} from {library}")
    tail, summary, names = [], None, {}
    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=watch_progress, args=(child, library, view, done), daemon=True, name="ta-progress").start()
        for line in child.stdout:
            line = line.rstrip()
            print(line, flush=True)
            fields = parse_progress(line)
            if fields is None:
                tail = (tail + [line])[-3:]              # a real message (an error, or the summary), not a progress line
                if re.match(r"^(done|stopped):", line):
                    summary = line
                continue
            cid = fields.get("channel_id")
            if cid:                                      # a heartbeat names the channel it is inside
                if cid not in names:
                    names.update(channel_names(view))
                fields["channel"] = re.sub(r"\s*\[UC[A-Za-z0-9_-]{22}\]$", "", names.get(cid, f"{cid} (new, not named yet)"))
                fields["channel_seen"] = time.time()
            if fields:
                settings.write_status(**fields)
        code = child.wait()
    except OSError as e:
        return False, f"could not run TA_NFOWriter: {e}"
    finally:
        done.set()
        child = None
        settings.write_status(channel=None, channel_id=None, channel_pos=None, channel_total=None, channel_seen=None,
                              channels_done=None, videos_total=None, channel_videos_done=None, channel_videos_total=None)
    if code == 0:
        return True, summary or (tail[-1] if tail else "done")
    return False, f"TA_NFOWriter reported errors (exit {code}): " + " | ".join(tail)


def tidy_views(names):
    """Remove what the settings no longer ask for inside the view folder: a view (<name>-nfo) whose library was
    taken out of the list, and the single flat view an older version made (channels directly in tanfo/).
    Only folders made of our own links and files are removed, never a link to somewhere else."""
    keep = {n.casefold() for n in names}
    try:
        entries = sorted(os.listdir(VIEW))
    except OSError:
        return []
    gone = []
    for d in entries:
        p = os.path.join(VIEW, d)
        if d.casefold() in keep or os.path.islink(p) or not os.path.isdir(p):
            continue
        old_flat = d == "Playlists" or os.path.isfile(os.path.join(p, "tvshow.nfo"))
        if d.endswith(VIEW_SUFFIX) or old_flat:
            shutil.rmtree(p, ignore_errors=True)
            gone.append(d)
            say(f"removed {p}: " + ("its library is no longer in the list" if d.endswith(VIEW_SUFFIX)
                                    else "left by an older version, which kept one flat view"))
    return gone


def convert_all(cfg, on_start=None):
    """Convert every library. Returns (ok, message, details), one detail per library. on_start(position, count,
    name) is called as each library begins, so the dashboard can say which one is being converted."""
    details = []
    for i, v in enumerate(cfg["views"]):
        if on_start:
            on_start(i + 1, len(cfg["views"]), v["name"])
        lib, view = v["library"], os.path.join(VIEW, v["name"])
        why = problem(lib)
        if why:
            say(why)
            details.append({"library": lib, "name": v["name"], "ok": False, "message": why, "channels": channel_count(view)})
            continue
        ok, msg = run_once(lib, view)
        details.append({"library": lib, "name": v["name"], "ok": ok, "message": msg, "channels": channel_count(view)})
    ok = all(d["ok"] for d in details)
    if ok:      # only after a clean run: a library setting that is wrong (a folder that is not there) must never wipe a view
        tidy_views([v["name"] for v in cfg["views"]])
    else:
        say("not removing any converted folder, because this run did not finish cleanly")
    if len(details) == 1:
        msg = details[0]["message"]
    else:
        bad = [f"{d['name']}: {d['message']}" for d in details if not d["ok"]]
        msg = " | ".join(bad) if bad else f"converted {len(details)} libraries"
    return ok, msg, details


def main():
    def stop(*_):
        global stopping
        stopping = True
        if child is not None:
            child.send_signal(signal.SIGINT)         # TA_NFOWriter 0.5 stops cleanly on this and the next run carries on
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_run, last_libraries = 0.0, None
    settings.write_status(state="starting", message="")
    while not stopping:
        cfg = settings.get_ta()
        now = time.time()
        shown = ", ".join(cfg["libraries"])
        if not cfg["enabled"]:
            last_libraries = None
            if settings.read_status().get("state") != "disabled":
                settings.write_status(state="disabled", message="", next_run=None, library=shown)
            if os.path.exists(settings.RUN_NOW):
                os.unlink(settings.RUN_NOW)
        else:
            requested = os.path.exists(settings.RUN_NOW)
            due = (requested or cfg["libraries"] != last_libraries
                   or now - last_run >= cfg["interval_minutes"] * 60)
            if due:
                if requested:
                    try:
                        os.unlink(settings.RUN_NOW)
                    except OSError:
                        pass
                last_libraries, last_run = list(cfg["libraries"]), now
                settings.write_status(state="running", message="converting", library=shown, last_start=now)
                ok, msg, details = convert_all(cfg, lambda i, n, name: settings.write_status(
                    current=f"{name} ({i} of {n})" if n > 1 else name))
                end = time.time()
                settings.write_status(state="idle" if ok else "error", message=msg, library=shown, libraries=details, current=None,
                                      last_end=end, last_ok=ok, channels=sum(d["channels"] for d in details),
                                      next_run=end + cfg["interval_minutes"] * 60)
                if any(d["ok"] for d in details):
                    open(settings.FARM_NOW, "w").close()     # new channels: make their tag folders right away
                say("done" if ok else msg)
                last_run = end
            elif settings.read_status().get("state") in ("disabled", "starting"):
                settings.write_status(state="idle", message="waiting for the next run", library=shown,
                                      next_run=last_run + cfg["interval_minutes"] * 60 if last_run else None)
        for _ in range(int(POLL * 10)):
            if stopping:
                break
            time.sleep(0.1)
    say("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
