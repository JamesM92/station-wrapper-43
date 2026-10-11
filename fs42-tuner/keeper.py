"""The schedule keeper: keeps every station's schedule running a few days ahead.

FieldStation42 only extends a schedule from inside its on-screen player, which this container never runs, so
without this every station goes off air when the days it was given run out. This looks at each station that
has been given a schedule and, when less than `days_ahead` days are left, adds the missing days.

Rules:
  * It only ADDS days, through the console (the way the Add days button does), so the console's own pages
    stay right. It never rebuilds a catalog or clears anything.
  * "Add a day" continues from the END of the existing schedule, so a schedule that ran out (the server was
    off for a while) needs one call per missing day to catch up, not just one.
  * It uses the same one-job-per-station rule as the Stations page: if you are building or extending a
    station by hand, the keeper leaves it alone until you are done.
  * It keeps ONE MORE day than it shows. The pages show `days_ahead` days (7); the schedule really runs one day
    further (8). The extra day is added once a day, in the early morning (KEEPER_TOPUP_HOUR, 4 by default), so
    what you see stays the same all day and the schedule is never built a bit at a time. The exception is a
    station that is close to running out (less than 12 hours left, say after the server was off): that is
    topped up straight away, at any hour.
  * A station that has never been given a schedule is left alone: building the first schedule is your
    decision (it needs a catalog, and you may not want it on air yet). Hidden stations are left alone too.
"""
import math
import os
import threading
import time
from datetime import datetime, timedelta

import console_client
import legacy
import resolver
import settings
import stationform as sf

MAX_PER_PASS = 30                # days added to one station in one pass (the job runner's own limit)
BUFFER_DAYS = 1                  # built but not shown: the day that is added each morning
URGENT = timedelta(hours=12)     # less schedule than this is left: do not wait for the morning
TOPUP_HOUR = int(os.environ.get("KEEPER_TOPUP_HOUR", "4") or 4)


def days_needed(end, now, days_ahead):
    """How many whole days to add so the schedule covers today and the next `days_ahead` days plus the hidden extra
    day (calendar days, so the answer only changes at midnight). 0 if it already does."""
    target = datetime(now.year, now.month, now.day) + timedelta(days=days_ahead + BUFFER_DAYS)
    if end >= target:
        return 0
    return math.ceil((target - end) / timedelta(days=1))


class Keeper:
    def __init__(self, api, log=print, topup_hour=None):
        self.api, self.log = api, log
        self.topup_hour = TOPUP_HOUR if topup_hour is None else topup_hour
        self.status = {"last_pass": None, "next_pass": None, "last_action": None, "error": None, "more": False}
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return dict(self.status)

    def run_once(self, days_ahead, now=None):
        """One pass over every station. Returns [(station, days_added_or_started)]."""
        now = now or datetime.now()
        actions, more, error = [], False, None
        try:
            for name in self.api.sync_schedules(now):          # special days come and go with the dates (schedules.py)
                self.log(f"[keeper] {name}: updated its seasons and special days for {now:%Y-%m-%d}")
        except Exception as e:                                  # never stop the keeper's real job for this
            self.log(f"[keeper] could not update the special days: {e}")
        try:
            stations = self.api.console.stations()
        except console_client.ConsoleError as e:
            stations, error = {}, str(e)
        for name, conf in sorted(stations.items()):
            if conf.get("hidden"):
                continue
            cov = resolver.coverage(self.api.db_path, name)
            if cov is None:                      # never given a schedule: not ours to start
                continue
            n = days_needed(cov[1], now, days_ahead)
            if not n:
                continue
            if cov[1] - now >= URGENT and now.hour < self.topup_hour:
                continue                         # plenty is left and it is the middle of the night: do it in the morning
            try:
                job = self.api.jobs.start(name, n, rebuild=False)
            except sf.FormError:                 # being built or extended by hand right now: leave it
                continue
            actions.append((name, job["days"]))
            more = more or n > job["days"]
            self.log(f"[keeper] {name}: schedule ends {cov[1]:%Y-%m-%d %H:%M}, adding {job['days']} day(s)"
                     + (f" (still {n - job['days']} more to catch up)" if n > job["days"] else ""))
        legacy.cleanup(now)                      # old schedules kept for a rebuild that has long since switched over
        with self._lock:
            self.status.update(last_pass=now.isoformat(), error=error, more=more)
            if actions:                          # remember the last time it actually did something
                self.status["last_action"] = {"at": now.isoformat(),
                                              "added": [{"station": s, "days": d} for s, d in actions]}
        if error:
            self.log(f"[keeper] could not check the stations: {error}")
        return actions

    def loop(self, interval_minutes=30, first_delay=45, sleep=time.sleep, now=datetime.now, forever=True):
        """Run passes for as long as the process lives. Settings are read each pass, so the dashboard's switch
        takes effect without a restart."""
        sleep(first_delay)
        while True:
            cfg = settings.get_keeper()
            wait = interval_minutes * 60
            if cfg["enabled"]:
                try:
                    self.run_once(cfg["days_ahead"], now())
                except Exception as e:           # nothing may stop the keeper for good
                    self.log(f"[keeper] pass failed: {e}")
                if self.status["more"]:
                    wait = 60                    # still catching up: don't wait half an hour
            with self._lock:
                self.status["next_pass"] = (now() + timedelta(seconds=wait)).isoformat() if cfg["enabled"] else None
            if not forever:
                return
            sleep(wait)
