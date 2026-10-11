"""A small client for FieldStation42's console API (the stations, catalog and schedule routes).

Everything goes through the console, never around it: the console keeps its own view of stations and
schedules in memory, and only changes made through it are seen by its own pages.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


import schedlock


class ConsoleError(Exception):
    """The console said no, or could not be reached. The text is safe to show."""


def _detail(e):
    try:
        d = json.loads(e.read() or b"{}").get("detail")
        if isinstance(d, list):
            d = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
        return str(d) if d else f"the console answered {e.code}"
    except (ValueError, AttributeError):
        return f"the console answered {e.code}"


def prune_dead_links(tags=None, media=None):
    """Remove the links in the lists (catalog/by-genre) whose video is gone, before a catalog is built: FieldStation42
    stops the whole build with FileNotFoundError at the first one. A followed folder links it back if the video returns.
    Nothing is removed when the media looks away (an empty or missing media folder, or most links dead at once): an
    unmounted share must not empty your lists. Returns (removed, why nothing was removed or None)."""
    tags = tags or os.environ.get("FS42_TAGS", "/app/catalog/by-genre")
    media = media or os.environ.get("FS42_MEDIA", "/mnt/media")
    dead, total = [], 0
    for root, dirs, files in os.walk(tags):
        for n in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            p = os.path.join(root, n)
            if os.path.islink(p):
                total += 1
                if not os.path.exists(p):
                    dead.append(p)
    if not dead:
        return 0, None
    try:
        away = not os.listdir(media)
    except OSError:
        away = True
    if away or len(dead) > max(20, total // 2):
        return 0, f"{len(dead)} of {total} links in your lists point at videos that are not there; your media may be unmounted, so none were removed"
    removed = 0
    for p in dead:
        try:
            os.unlink(p)
            removed += 1
        except OSError:
            pass
    return removed, None


class Console:
    def __init__(self, base="http://127.0.0.1:4242", sleep=time.sleep):
        self.base = base.rstrip("/")
        self.sleep = sleep

    def _call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - the console's own address from our config
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise ConsoleError(_detail(e))
        except (urllib.error.URLError, OSError) as e:
            raise ConsoleError("the FieldStation42 console is not reachable right now")
        except ValueError:
            raise ConsoleError("the console sent something unreadable")

    @staticmethod
    def _q(name):
        return urllib.parse.quote(name, safe="")

    def stations(self):
        """{name: config} for every station."""
        return {s["station_conf"]["network_name"]: s["station_conf"]
                for s in self._call("GET", "/stations").get("stations", []) if isinstance(s.get("station_conf"), dict)}

    def summary(self):
        return {s["network_name"]: s for s in self._call("GET", "/summary/").get("summary_data", [])}

    def create(self, conf):
        return self._call("POST", "/stations", {"station_conf": conf})

    def update(self, conf, name=None):
        """Save a station's config. `name` is the station's CURRENT name, when the new config renames it."""
        return self._call("PUT", f"/stations/{self._q(name or conf['network_name'])}", {"station_conf": conf})

    def delete(self, name):
        return self._call("DELETE", f"/stations/{self._q(name)}")

    def _snap_breaks(self, name, progress=None):
        """After a catalog is built, move the break marks of a station that asks for it onto real gaps in the picture
        (breaksnap.py). Runs inside the build lock, so no day is built from half-moved marks. Never fails a build."""
        try:
            import breaksnap
            if not self.stations().get(name, {}).get("wrapper_break_snap"):
                return
            breaksnap.snap_station(
                os.environ.get("FS42_DB", "/app/runtime/fs42_fluid.db"), name,
                progress=lambda d, t: progress and progress({"log": f"Moving break points onto the picture: {d} of {t} videos"}))
        except Exception as e:
            print(f"[breaksnap] {name}: {e}", file=sys.stderr, flush=True)

    @staticmethod
    def _prune(name):
        try:
            removed, why = prune_dead_links()
            if removed:
                print(f"[catalog] {name}: removed {removed} link(s) in the lists whose video is gone", file=sys.stderr, flush=True)
            if why:
                print(f"[catalog] {name}: {why}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[catalog] {name}: could not look for dead links: {e}", file=sys.stderr, flush=True)

    def _task(self, start_path, status_path, what, timeout, poll=2, progress=None, on_wait=None, after=None, before=None):
        # One build at a time across every program that can ask for one (see schedlock.py): two at once on the
        # same station would build the same day twice. progress(status) is called on every look at the task, and
        # on_wait() once if another build holds the lock first.
        try:
            with schedlock.held(on_wait=on_wait):
                if before:
                    before()
                task = self._call("POST", start_path)["task_id"]
                deadline = time.time() + timeout
                while time.time() < deadline:
                    st = self._call("GET", f"{status_path}/{task}")
                    if progress:
                        progress(st)
                    if st.get("status") == "done":
                        if after:
                            after()
                        return
                    if st.get("status") == "error":
                        full = str(st.get("log", ""))
                        print(f"[console] {what} failed:\n{full}", file=sys.stderr, flush=True)       # all of it, for `docker logs`
                        cause = [ln for ln in full.strip().splitlines() if ln.strip()]
                        raise ConsoleError(f"{what} failed: " + (" | ".join(cause[-3:]) if cause else "no reason given")[-600:])
                    self.sleep(poll)
        except schedlock.LockTimeout as e:
            raise ConsoleError(f"{what}: {e}")
        raise ConsoleError(f"{what} took too long")

    def rebuild_catalog(self, name, timeout=4 * 3600, progress=None, on_wait=None):
        """A big library on a network share can take a long time: the limit is hours, not minutes."""
        self._task(f"/build/catalog/{self._q(name)}", "/build/catalog/status", f"building the catalog for {name}", timeout,
                   progress=progress, on_wait=on_wait, after=lambda: self._snap_breaks(name, progress),
                   before=lambda: self._prune(name))

    def update_catalog(self, name, timeout=4 * 3600, progress=None, on_wait=None):
        """Refresh a station's catalog with new videos WITHOUT clearing its schedule (see scripts/update_catalog.py).
        Not a console route: the console's own rebuild always deletes the schedule, so this runs the same catalog code
        in a process of its own, under the same one-build-at-a-time lock."""
        script = os.environ.get("FS42_UPDATE_CATALOG", "/opt/wrapper/update_catalog.py")
        try:
            with schedlock.held(on_wait=on_wait):
                self._prune(name)
                proc = subprocess.Popen([sys.executable, script, name], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, cwd=os.environ.get("FS42_APP", "/app") if os.path.isdir(os.environ.get("FS42_APP", "/app")) else None)
                end = time.time() + timeout
                while True:
                    try:
                        out, err = proc.communicate(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        if progress:
                            progress({"log": ""})
                        if time.time() > end:
                            proc.kill()
                            proc.communicate()
                            raise ConsoleError(f"updating the catalog for {name} took too long")
                if proc.returncode == 0:
                    self._snap_breaks(name, progress)
        except OSError as e:
            raise ConsoleError(f"could not update the catalog for {name}: {e}")
        if proc.returncode != 0:
            raise ConsoleError((err or out or "could not update the catalog").strip().splitlines()[-1])
        return (out or "").strip()

    def snap_breaks(self, name, progress=None, on_wait=None):
        """Finish moving a station's break marks (see breaksnap.py) after a build that stopped part way: what is done
        stays done, so only the videos not yet looked at are scanned."""
        try:
            with schedlock.held(on_wait=on_wait):
                self._snap_breaks(name, progress)
        except schedlock.LockTimeout as e:
            raise ConsoleError(f"{name}: {e}")

    def reset_schedule(self, name, timeout=1800, progress=None, on_wait=None):
        """Clear a station's schedule (not its catalog)."""
        self._task(f"/build/schedule/reset/{self._q(name)}", "/build/schedule/reset/status",
                   f"clearing the schedule of {name}", timeout, progress=progress, on_wait=on_wait)

    def add_day(self, name, timeout=1800, progress=None, on_wait=None):
        self._task(f"/build/schedule/add_time/day/{self._q(name)}", "/build/schedule/add_time/status",
                   f"adding a day to {name}", timeout, progress=progress, on_wait=on_wait)
