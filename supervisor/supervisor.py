#!/usr/bin/env python3
"""Runs everything in the one container and keeps it running.

  console   FieldStation42's web console and scheduler    always
  tuner     the dashboard, player and video streams       always (needs its config)
  sync      keeps the symlink pool current                SYNC_ENABLED=1
  ta-nfo    the TubeArchivist conversion                  always; the dashboard switches it on

It starts what is enabled, prefixes each line of output with the service name,
restarts a service that stops (waiting a little longer after each quick failure),
and on SIGTERM stops them all, giving each a few seconds before killing it.

Exit status 78 from a service means "I refuse to run here" (for example the
catalog is on an SMB share). That is not retried: if the console or tuner says it,
the container stops with 78; if an optional service says it, only that one stops,
and the message stays in the log.

SIGUSR1 restarts the tuner alone, straight away. That is how new channels are picked
up (the tuner reads its config at start) without bouncing the console:
    docker kill --signal=USR1 station-wrapper-43
tini passes the signal on to this process.

It is not PID 1: tini is, so processes that outlive their parent get reaped.
"""
import os
import signal
import subprocess
import sys
import threading
import time

try:
    from stations import discover as discover_stations, is_busy as builds_running
except ImportError:         # run from the repo: the module sits in the tuner's folder
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fs42-tuner"))
    from stations import discover as discover_stations, is_busy as builds_running

REFUSED = 78
TRUE = ("1", "true", "yes", "on")


class Service:
    def __init__(self, name, argv, cwd=None, env=None, core=False):
        self.name, self.argv, self.cwd, self.env, self.core = name, argv, cwd, env or {}, core
        self.proc = None
        self.reader = None
        self.fails = 0
        self.started_at = 0.0
        self.next_start = 0.0
        self.state = "waiting"          # waiting | running | finished | refused
        self.restart_requested = False


def services_from_env(env=None, exists=os.path.exists):
    """The services this container should run, from its environment."""
    env = os.environ if env is None else env
    flag = lambda name: env.get(name, "").lower() in TRUE
    config = env.get("FS42_TUNER_CONF", "/config/tuner/config.json")
    out = [Service("console", ["python3", env.get("FS42_CONSOLE_LAUNCH", "/opt/wrapper/console_launch.py"), "-s"], cwd=env.get("FS42_APP", "/app"), core=True)]
    if exists(config):
        out.append(Service("tuner", ["python3", "-u", "tuner.py"], cwd=env.get("FS42_TUNER_DIR", "/opt/tuner"),
                           env={"FS42_TUNER_CONF": config}, core=True))
    # One-shot, on every start: puts a test station and the guide channel on a brand new install (see
    # scripts/firstrun.py). It exits at once when it has already run, or when the install has stations.
    if any(env.get(v, "1").lower() not in ("0", "false", "no", "off") for v in ("FIRSTRUN_TEST_STATION", "FIRSTRUN_GUIDE_STATION")):
        out.append(Service("firstrun", ["python3", "-u", "/opt/wrapper/firstrun.py"], cwd="/opt/wrapper"))
    if env.get("FARM_ENABLED", "1").lower() not in ("0", "false", "no", "off"):     # on unless switched off
        out.append(Service("farm", ["/opt/farm/run.sh"]))
    if flag("SYNC_ENABLED"):
        out.append(Service("sync", ["python3", "-u", "/opt/sync/sync.py"]))
    # Always started: it idles until the dashboard switches the TubeArchivist conversion on (TA_ENABLED=1
    # is only the starting value), so turning it on needs no restart.
    out.append(Service("ta-nfo", ["python3", "-u", "/opt/ta-nfo/service.py"]))
    return out


def notes_from_env(env=None, exists=os.path.exists):
    """What was left out and why, for the log."""
    env = os.environ if env is None else env
    notes = []
    if not exists(env.get("FS42_TUNER_CONF", "/config/tuner/config.json")):
        notes.append("the tuner is not started: no config.json at "
                     f"{env.get('FS42_TUNER_CONF', '/config/tuner/config.json')}")
    if env.get("FARM_ENABLED", "1").lower() in ("0", "false", "no", "off"):
        notes.append("farm is off (FARM_ENABLED=0): catalog/by-genre will not be filled for you")
    if env.get("SYNC_ENABLED", "").lower() not in TRUE:
        notes.append("sync is off (SYNC_ENABLED=1 turns on the symlink pool scanner)")
    # (the TubeArchivist conversion is switched on from the dashboard's Files page; no note needed)
    return notes


class Supervisor:
    def __init__(self, services, out=None, backoff_base=1.0, backoff_max=30.0, stable_after=60.0,
                 grace=10.0, poll=0.2, watch_stations=None, watch_every=3.0, busy=None, settle=0.0):
        self.services = services
        # watch_stations() -> {channel: station}. The tuner reads its channels at start, so when the
        # stations you manage in FieldStation42's console change, it is restarted to pick them up.
        self.watch_stations, self.watch_every = watch_stations, watch_every
        self._stations_seen, self._next_watch = None, 0.0
        # busy() -> True while the dashboard is running a build: restarting it then would cut the build off
        self.busy, self._said_waiting = busy, False
        # a change is acted on only once it has stood this long: a new station is followed at once by the dashboard
        # starting its build, and restarting the tuner in between would lose that build
        self.settle, self._changed = settle, None          # _changed: (what was seen, when it was first seen)
        self.out = out or sys.stdout
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.stable_after, self.grace, self.poll = stable_after, grace, poll
        self.stopping = threading.Event()
        self.exit_code = None
        self._lock = threading.Lock()

    # ---- output
    def say(self, name, line):
        with self._lock:
            self.out.write(f"[{name}] {line.rstrip()}\n")
            self.out.flush()

    def _forward(self, svc, stream):
        for line in iter(stream.readline, ""):
            self.say(svc.name, line)
        stream.close()

    # ---- one service
    def _start(self, svc):
        env = {**os.environ, **svc.env}
        try:
            svc.proc = subprocess.Popen(svc.argv, cwd=svc.cwd, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            self.say("supervisor", f"could not start {svc.name}: {e}")
            svc.started_at = time.monotonic()           # a start that fails counts as a quick failure
            self._failed(svc, svc.started_at)
            return
        svc.state, svc.started_at = "running", time.monotonic()
        svc.reader = threading.Thread(target=self._forward, args=(svc, svc.proc.stdout), daemon=True)
        svc.reader.start()
        self.say("supervisor", f"started {svc.name} (pid {svc.proc.pid})")

    def _failed(self, svc, now):
        # quick failures wait longer each time; a service that ran a while starts over
        svc.fails = 0 if now - svc.started_at >= self.stable_after else svc.fails + 1
        delay = min(self.backoff_max, self.backoff_base * (2 ** max(0, svc.fails - 1)))
        svc.next_start = now + delay
        svc.state, svc.proc = "waiting", None
        self.say("supervisor", f"{svc.name} will restart in {delay:g}s")

    def _exited(self, svc, code):
        if svc.reader:
            svc.reader.join(timeout=2)
        now = time.monotonic()
        svc.proc = None
        if svc.restart_requested:
            svc.restart_requested = False
            svc.fails, svc.next_start, svc.state = 0, now, "waiting"
            self.say("supervisor", f"{svc.name} stopped for a restart you asked for")
            return
        if code == REFUSED:
            svc.state = "refused"
            self.say("supervisor", f"{svc.name} refused to run (exit {REFUSED}); not restarting it")
            if svc.core:
                self.say("supervisor", f"{svc.name} is essential, so the container is stopping")
                self.exit_code = REFUSED
                self.stopping.set()
            return
        if code == 0 and not svc.core:
            svc.state = "finished"
            self.say("supervisor", f"{svc.name} finished")
            return
        self.say("supervisor", f"{svc.name} exited with status {code}")
        self._failed(svc, now)

    # ---- the whole thing
    def stop(self):
        self.stopping.set()

    def request_restart(self, name):
        """Restart one service now, with no wait. Safe to call from a signal handler."""
        for svc in self.services:
            if svc.name == name and svc.proc is not None and svc.proc.poll() is None:
                svc.restart_requested = True
                svc.proc.terminate()
                return True
        return False

    def _check_stations(self):
        try:
            seen = self.watch_stations()
        except Exception as e:      # a half-written config must never take the supervisor down
            self.say("supervisor", f"could not read the station configs: {e}")
            return
        if self._stations_seen is None:
            self._stations_seen = seen
        elif seen != self._stations_seen:
            now = time.monotonic()
            if self._changed is None or self._changed[0] != seen:
                self._changed = (seen, now)                  # a new change (or it changed again): start the wait over
            if now - self._changed[1] < self.settle:
                return
            if self.busy and self.busy():
                if not self._said_waiting:
                    self._said_waiting = True
                    self.say("supervisor", "the stations changed, but a build is running: restarting the tuner when it is done")
                return                                   # look again next time; _stations_seen is left as it was
            self._said_waiting = False
            self._changed = None
            added = sorted(set(seen.items()) - set(self._stations_seen.items()))
            gone = sorted(set(self._stations_seen.items()) - set(seen.items()))
            self.say("supervisor", "the stations changed ("
                     + "; ".join([f"+{n} {s}" for n, s in added] + [f"-{n} {s}" for n, s in gone])
                     + "): restarting the tuner so the dashboard shows them")
            self._stations_seen = seen
            self.request_restart("tuner")

    def _terminate_all(self):
        running = [s for s in self.services if s.proc and s.proc.poll() is None]
        for s in running:
            s.proc.terminate()
        deadline = time.monotonic() + self.grace
        for s in running:
            try:
                s.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self.say("supervisor", f"{s.name} did not stop in {self.grace:.0f}s; killing it")
                s.proc.kill()
                s.proc.wait()
            if s.reader:
                s.reader.join(timeout=2)

    def run(self):
        for svc in self.services:
            self._start(svc)
        while not self.stopping.is_set():
            now = time.monotonic()
            for svc in self.services:
                if svc.proc is not None:
                    code = svc.proc.poll()
                    if code is not None:
                        self._exited(svc, code)
                elif svc.state == "waiting" and now >= svc.next_start:
                    self._start(svc)
            if self.watch_stations and now >= self._next_watch:
                self._next_watch = now + self.watch_every
                self._check_stations()
            self.stopping.wait(self.poll)
        self._terminate_all()
        return self.exit_code if self.exit_code is not None else 0


def main():
    for note in notes_from_env():
        print(f"[supervisor] {note}", flush=True)
    svcs = services_from_env()
    print(f"[supervisor] running: {', '.join(s.name for s in svcs)}", flush=True)
    confs = os.environ.get("FS42_CONFS", "/app/confs")
    sup = Supervisor(svcs, watch_stations=lambda: discover_stations(confs), busy=builds_running,
                     settle=float(os.environ.get("SUPERVISOR_SETTLE_SECONDS", "8")))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sup.stop())
    signal.signal(signal.SIGUSR1, lambda *_: sup.request_restart("tuner"))
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
