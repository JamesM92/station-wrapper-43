"""What the Stations and Schedule pages do, in plain functions (tuner.py only routes to them).

Stations are made, changed and removed THROUGH FieldStation42's console, which stays the one place a
station's settings live. Building one (its catalog, then days of schedule) can take a while, so it runs
as a background job the page polls. Reading what is scheduled is straight from the schedule database.
"""
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import console_client
import files as filemgr
import guide_channel
import legacy
import resolver
import schedules
import stationform as sf
import stations as stationlib

VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
LOW = timedelta(hours=24)               # warn when less than this much schedule is left
MAX_DAYS = 30
MAX_BLOCKS = 500
MAX_ENTRIES = 500                   # files returned for one block in one go


class NotFound(Exception):
    pass


def list_path(lists_dir, name):
    """The folder of a list: its name joined to the lists folder, normalised, and checked to stay inside it (NotFound when a
    name would lead out of it). Names are also checked when a form is saved; this is the one place they become paths."""
    root = os.path.normpath(lists_dir)
    full = os.path.normpath(os.path.join(root, str(name)))
    if not full.startswith(root.rstrip(os.sep) + os.sep):
        raise NotFound(f"'{name}' is not a list")
    return full


def _has_video(folder):
    """Is there a video anywhere under this folder? Stops at the first one, and does not look at what a link points to
    (a list is thousands of links into a network share: looking at each one is what made this slow): a name that is a
    video settles it, and only a name that is not a video is checked for being a folder."""
    stack = [folder]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                subdirs = []
                for e in it:
                    if os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                        return True
                    if not e.name.startswith(".") and "." not in e.name[-5:]:       # no extension: maybe a folder
                        subdirs.append(e.path)
                    elif e.is_dir(follow_symlinks=False):
                        subdirs.append(e.path)
        except OSError:
            continue
        stack.extend(subdirs)
    return False


class Jobs:
    """Background builds. One per station at a time; the page polls get()."""

    on_built = None          # optional: called with a station's name when its build has finished (the guide is made again)

    def __init__(self, console, db_path=None, lists_dir=None):
        self.console = console
        self.db_path, self.lists_dir = db_path, lists_dir
        self._jobs, self._lock = {}, threading.Lock()

    def _count_videos(self, name):
        """How many videos the lists this station plays hold: what a catalog build has to read. None if unknown."""
        if not self.lists_dir:
            return None
        try:
            form, _why = sf.parse(self.console.stations().get(name) or {})
            used = set(sf.lists_used(form)) if form else set()
            if form and form.get("kind") == "standard":
                used.add(form.get("bump"))
                if form.get("commercials"):
                    used.add(form["commercials"])
            total = 0
            for n in used:
                if not n:
                    continue
                for _r, _d, names in os.walk(list_path(self.lists_dir, n), followlinks=True):
                    total += sum(1 for f in names if os.path.splitext(f)[1].lower() in VIDEO_EXT)
            return total or None
        except Exception:
            return None

    def _progress_for(self, job, base, total, base_chapters=0, what="Building the catalog"):
        """A callback for the console client: turns each look at the build into a message with time and files read.
        A catalog build has two passes over the videos: reading each one's length, then (a second look at every file)
        its chapter marks. Both can be slow on a network share, so each is shown."""
        def progress(st):
            seen = max(0, resolver.file_meta_count(self.db_path) - base) if self.db_path else 0
            chapters = max(0, resolver.file_meta_count(self.db_path, "chapter_points") - base_chapters) if self.db_path else 0
            secs = int(time.time() - job["started"])
            text = f"{what}… {secs // 60}m {secs % 60:02d}s so far"
            if total and seen >= total and chapters < total:
                text += f", all {total} videos checked; now reading chapter marks: {chapters} of {total} (one more pass over every file)"
            elif total and seen:
                text += f", {min(seen, total)} of {total} videos checked"
            elif seen:
                text += f", {seen} videos checked"
            elif total and secs >= 4:
                # nothing new was read: every video was read by an earlier build and is taken from FieldStation42's saved scan, which
                # leaves no count to show. What is left is walking the folders over the share and writing the catalog.
                text += f", going through {total} videos: the ones read before are not read again, so this step shows no count"
            log = st.get("log") if isinstance(st, dict) else None
            last = (log[-1] if isinstance(log, list) and log else log if isinstance(log, str) else "")
            last = str(last).strip().splitlines()[-1][:120] if last else ""
            note = "the schedule is kept" if job.get("refresh") else "this clears the station's schedule"
            self._set(job, message=text + f" ({note}; you can leave this page)",
                      checked=seen, total=total, chapters=chapters, detail=last)
        return progress

    def list(self):
        """Running jobs first, then the recent ones: lets a page that was reloaded show what is still going on."""
        with self._lock:
            jobs = [dict(j) for j in self._jobs.values()]
        jobs.sort(key=lambda j: (j["state"] != "running", -j["started"]))
        now = time.time()
        for j in jobs:
            j["elapsed"] = int(now - j["started"])
        return jobs

    def start(self, name, days, rebuild, refresh=False, resume=False):
        """refresh: only bring new videos into the catalog; the schedule is left alone (days is then 0)."""
        days = 0 if refresh else max(1, min(MAX_DAYS, int(days)))
        with self._lock:
            for j in self._jobs.values():
                if j["station"] == name and j["state"] == "running":
                    raise sf.FormError(f"'{name}' is already being built: it is still working (see the progress on the Stations page), "
                                       "so there is no need to start it again")
            job_id = uuid.uuid4().hex[:12]
            job = {"id": job_id, "station": name, "state": "running", "message": "Starting…", "days": days,
                   "rebuild": rebuild, "refresh": refresh, "resume": bool(resume), "done_days": 0, "started": time.time()}
            self._jobs[job_id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j["started"])[:-20]:
                if old["state"] != "running":
                    self._jobs.pop(old["id"], None)
        stationlib.mark_busy()
        self._beat_if_needed()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return dict(job)

    def _running(self):
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["state"] == "running")

    def _beat_if_needed(self):
        """While any build runs, keep the busy mark fresh, so a crashed dashboard's mark goes stale and is ignored."""
        with self._lock:
            if getattr(self, "_beating", False):
                return
            self._beating = True

        def beat():
            while self._running():
                stationlib.mark_busy()
                time.sleep(10)
            with self._lock:
                self._beating = False
            if not self._running():
                stationlib.clear_busy()
        threading.Thread(target=beat, daemon=True, name="jobs-heartbeat").start()

    def _set(self, job, **kw):
        with self._lock:
            job.update(kw)

    def _run(self, job):
        try:
            self._run_steps(job)
            if self.on_built and job.get("state") == "done":
                try:
                    self.on_built(job["station"])        # the guide is made again: it was made before this schedule was there
                except Exception:
                    pass
        finally:
            if not self._running():              # the last build just ended: let the supervisor restart the dashboard now
                stationlib.clear_busy()

    def _run_steps(self, job):
        name = job["station"]
        kept = False
        start_day = 0
        try:
            if filemgr.linking():                  # a series just picked for a slot is still being linked in
                self._set(job, message="Linking the series you picked into its list first…")
                filemgr.wait_linked()
            if job.get("refresh"):
                base = resolver.file_meta_count(self.db_path) if self.db_path else 0
                base_ch = resolver.file_meta_count(self.db_path, "chapter_points") if self.db_path else 0
                self._set(job, message="Looking for new videos (the schedule is kept as it is)…")
                self.console.update_catalog(
                    name, progress=self._progress_for(job, base, self._count_videos(name), base_ch, "Updating the catalog"),
                    on_wait=lambda: self._set(job, message="Waiting for another schedule change to finish first…"))
                self._set(job, state="done", message="Done: the new videos are in the catalog. They are used the next time "
                          "days are added to the schedule (the schedule keeper does that each morning); days already scheduled are unchanged.")
                return
            if job["rebuild"]:
                # the old schedule keeps the channel on air while this runs (see legacy.py)
                kept = legacy.snapshot(self.db_path, name) if self.db_path else False
                if kept:
                    self._set(job, message="Keeping the old schedule on air while the new one is built…")
                base = resolver.file_meta_count(self.db_path) if self.db_path else 0
                base_ch = resolver.file_meta_count(self.db_path, "chapter_points") if self.db_path else 0
                if job.get("resume") and self.db_path and resolver.catalog_count(self.db_path, name) > 0:
                    # the catalog is written in one step at the end of its build, so one that is there is a finished one:
                    # carry on from the days not built yet instead of reading every video again
                    start_day = min(job["days"], resolver.days_built(self.db_path, name))
                    self._set(job, message=f"The catalog was already built: picking up at day {start_day + 1} of {job['days']}…", done_days=start_day)
                    self.console.snap_breaks(name, on_wait=lambda: self._set(job, message="Waiting for another schedule change to finish first…"))
                else:
                    self._set(job, message="Building the catalog (this clears the station's schedule)…")
                    self.console.rebuild_catalog(
                        name, progress=self._progress_for(job, base, self._count_videos(name), base_ch),
                        on_wait=lambda: self._set(job, message="Waiting for another schedule change to finish first…"))
            for i in range(start_day, job["days"]):
                self._set(job, message=f"Adding day {i + 1} of {job['days']} to the schedule…", done_days=i)
                self.console.add_day(
                    name, on_wait=lambda: self._set(job, message="Waiting for another schedule change to finish first…"))
            text = f"Done: {job['days']} day{'s' if job['days'] != 1 else ''} of schedule."
            if kept:
                at = legacy.handover(name)
                if at is not None and at > datetime.now():
                    text += f" The old schedule keeps playing until {at:%H:%M}, then the new one starts."
            self._set(job, state="done", done_days=job["days"], message=text)
        except console_client.ConsoleError as e:
            self._set(job, state="error", message=str(e) + (" The old schedule keeps playing until it ends." if kept else ""))
        except Exception as e:      # a job must always end in done or error, never hang the page
            self._set(job, state="error", message=f"that did not work: {e}" + (" The old schedule keeps playing until it ends." if kept else ""))
        finally:
            try:
                legacy.cleanup()
            except Exception:
                pass

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def running(self, name):
        """The build running for this station (with its elapsed seconds), or None."""
        with self._lock:
            for j in self._jobs.values():
                if j["station"] == name and j["state"] == "running":
                    return {**j, "elapsed": int(time.time() - j["started"])}
        return None

    def busy(self, name):
        with self._lock:
            return any(j["station"] == name and j["state"] == "running" for j in self._jobs.values())


class Stations:
    def __init__(self, console, db_path, lists_dir, horizon=None, media_root=None):
        """horizon: optional, returns how many days ahead to show (the schedule keeper's setting), or None for all.
        The schedule really runs a day further (the keeper's hidden extra day); it is not shown, so what you see
        is the same all day."""
        self.console, self.db_path, self.lists_dir = console, db_path, lists_dir
        self.media_root = media_root                    # where a guide channel's music may come from
        self.horizon = horizon or (lambda: None)
        self._empty_seen = {}                           # list name -> (when looked at, it had no videos)
        self.jobs = Jobs(console, db_path, lists_dir)

    # ---- reading
    def _coverage(self, name, now):
        cov = resolver.coverage(self.db_path, name)
        if not cov:
            return {"scheduled_from": None, "scheduled_until": None, "on_air": False, "low": True}
        first, last = cov
        shown = min(last, self._cap(now)) if self._cap(now) else last
        return {"scheduled_from": first.isoformat(), "scheduled_until": shown.isoformat(),
                "on_air": first <= now < last, "low": last - now < LOW}

    def _cap(self, now):
        """The end of what is shown: midnight at the end of the last day the keeper promises, or None."""
        try:
            days = self.horizon()
        except Exception:
            days = None
        if not days:
            return None
        return datetime(now.year, now.month, now.day) + timedelta(days=days)

    def listing(self, now=None):
        now = now or datetime.now()
        out = []
        for name, conf in self.console.stations().items():
            form, why = self._form_of(name, conf, now.date())
            job, old = self.jobs.running(name), legacy.info(name, now)
            if old and not old["finished"] and not job:
                old["interrupted"] = True            # a rebuild was started and is not running: the server restarted, or it failed
            row = {"name": name, "number": conf.get("channel_number"), "type": conf.get("network_type", "standard"),
                   "hidden": bool(conf.get("hidden", False)), "editable": form is not None, "why": why,
                   "lists": self._lists_of(form), "job": job, "old_schedule": old,
                   **self._coverage(name, now)}
            out.append(row)
        out.sort(key=lambda r: (r["number"] if isinstance(r["number"], int) else 10 ** 6, r["name"]))
        return out

    @staticmethod
    def _lists_of(form):
        return sf.lists_used(form) if form else []

    def _empty_lists(self, defn=None, names=None, fresh=False):
        """The lists a definition (or this list of names) plays from that exist but have no videos in them yet. A slot whose
        lists are all empty plays the test pattern until they have some, instead of the build failing for want of a video."""
        if not self.lists_dir:
            return frozenset()
        names = names if names is not None else (schedules.lists_of(defn) if defn else [])
        out, now = set(), time.time()
        for n in names:
            seen = self._empty_seen.get(n)
            if seen and not fresh and now - seen[0] < 20:        # looked at a moment ago: the Stations page asks often
                empty = seen[1]
            else:
                path = list_path(self.lists_dir, n)
                empty = os.path.isdir(path) and not _has_video(path)
                self._empty_seen[n] = (now, empty)
            if empty:
                out.add(n)
        return frozenset(out)

    def _shows(self):
        """The lists that are shows used as sources: a slot that plays one is played in order (schedules.SERIES)."""
        return filemgr.shows_in(self.lists_dir) if self.lists_dir else []

    def _form_of(self, name, conf, today):
        """(form, why not). A station whose schedule the wrapper keeps (seasons, special days, commercials by the
        hour) is read from that definition, as long as its live config is still the one made from it; a station made
        any other way is read from its config as before. Either way the form carries a `schedule` for the editor."""
        defn = schedules.get(name)
        if defn is not None and conf.get("network_type", "standard") == "standard":
            base, why = sf.parse_base(conf)
            if base is None:
                return None, why
            # the config is what the definition makes, with or without the lists that have no videos yet left out of it. The
            # usual case (nothing left out) is tried first and needs no look at the lists; the ones that were left out when it
            # was made are remembered; looking at the lists now is the last resort
            shows = self._shows()

            def made_from(skip):
                return schedules.conf_matches(conf, schedules.conf_parts(defn, today, shows, skip))
            if not (made_from(frozenset()) or made_from(schedules.skipped_of(name)) or made_from(self._empty_lists(defn))):
                return None, ("its schedule was changed in the FieldStation42 console after it was set here, so the "
                              "page can't show it faithfully (saving here would replace those changes)")
            return {**base, "schedule": defn}, None
        form, why = sf.parse(conf)
        if form is not None and form["kind"] == "standard":
            form = {**form, "schedule": schedules.from_legacy(form)}
        return form, why

    def get(self, name):
        conf = self.console.stations().get(name)
        if conf is None:
            raise NotFound(f"there is no station called '{name}'")
        form, why = self._form_of(name, conf, datetime.now().date())
        return {"name": name, "form": form, "why": why, "conf": {k: v for k, v in conf.items() if not k.startswith("_")}}

    # ---- changing
    def _check_music(self, form):
        """A guide channel's music must be a folder (or one file) inside the media with something to play."""
        music = form["guide"]["music"]
        if not music or not self.media_root:
            return
        root = os.path.realpath(self.media_root)
        real = os.path.realpath(os.path.join(root, music))        # an address inside the media; one outside it is refused below
        if not real.startswith(root.rstrip("/") + "/"):
            raise sf.FormError(f"the music must be a folder or file inside your media ({self.media_root}), because that is what the container can see")
        if not os.path.exists(real):
            raise sf.FormError(f"{music} is not there: is its volume mounted?")
        if not guide_channel.audio_files(real):
            raise sf.FormError(f"there is no music in {music} (it plays mp3, flac, ogg, m4a, wav and opus files)")

    def _check_lists(self, form):
        if form["kind"] == "guide":
            self._check_music(form)
            return []
        names = sf.lists_used(form)
        if form["kind"] == "standard":
            names += [form["bump"], form["bump_end"]]
            for r in form.get("show_cards", []):
                names += [r["start"], r["end"]]
            if form["commercials"]:
                names.append(form["commercials"])
        try:
            missing = sorted({n for n in names if not os.path.isdir(list_path(self.lists_dir, n))})
        except NotFound as e:
            raise sf.FormError(str(e))
        if missing:
            raise sf.FormError("these lists don't exist yet: " + ", ".join(f"'{m}'" for m in missing)
                               + ". Create them on the Lists & files page first.")
        empty = sorted({n for n in names if not _has_video(list_path(self.lists_dir, n))})
        return [f"'{n}' has no videos in it yet, so it will play nothing" for n in empty]

    def save(self, original, raw):
        stations = self.console.stations()
        if original is not None and original not in stations:
            raise NotFound(f"there is no station called '{original}'")
        numbers = {c.get("channel_number"): n for n, c in stations.items()}
        form = sf.validate(raw, list(stations), numbers, original)
        existing = None
        if original is not None:
            if form["name"] != original:
                raise sf.FormError("to rename a station use Rename on the Stations page (it also rebuilds the schedule)")
            existing = stations[original]
            if self._form_of(original, existing, datetime.now().date())[0] is None:
                raise sf.FormError("this station uses settings the form can't hold: edit it in the FieldStation42 console")
        warnings = self._check_lists(form)
        if form.get("logo") and self.media_root:
            import channel_logo
            if channel_logo.custom(self.media_root, form["logo"]) is None:
                warnings.append(f"the logo “{form['logo']}” is not there (or is not a picture under 2 MB inside your media), "
                                "so a logo drawn from the channel's name is used")
        conf = sf.to_conf(form, existing, shows=self._shows(),
                          skip=self._empty_lists(names=sf.lists_used(form), fresh=True) if form["kind"] == "standard" else ())
        if existing is not None:
            self.console.update(conf)
        else:
            self.console.create(conf)
        if form["kind"] == "standard" and self.lists_dir:
            pairs = ([(form["bump"], form["bump_end"])] + (schedules.card_pairs(form["schedule"]) if "schedule" in form else [])
                     + [(r["start"], r["end"]) for r in form.get("show_cards", [])])
            try:
                for start, end in pairs:
                    sf.make_cards_dir(self.lists_dir, start, end)
                for off, name in ((form.get("midcards") is False, sf.OFF_CARDS), (not form["commercials"], sf.COM_OFF)):
                    if off:
                        os.makedirs(list_path(self.lists_dir, name), exist_ok=True)
            except OSError as e:
                warnings.append(f"the start and end of break cards could not be set up: {e}")
        if "schedule" in form and form["kind"] == "standard":
            schedules.put(form["name"], form["schedule"], skipped=self._empty_lists(names=sf.lists_used(form), fresh=True))   # the editor's own copy: seasons, special days, commercials
        else:
            schedules.drop(form["name"])
        return {"name": form["name"], "created": existing is None, "needs_build": form["kind"] != "guide", "warnings": warnings}

    def rename(self, old, new):
        """Rename a station of any kind. FieldStation42 keeps a station's schedule and catalog under its NAME, so
        they would be left behind under the old one: the old schedule is cleared first, and a station that had one
        gets a fresh catalog and a week of schedule under the new name (as a background job)."""
        stations = self.console.stations()
        if old not in stations:
            raise NotFound(f"there is no station called '{old}'")
        new = sf._name(new)
        if new == old:
            raise sf.FormError("that is already its name")
        clash = [n for n in stations if n != old and n.casefold() == new.casefold()]
        if clash:
            raise sf.FormError(f"there is already a station called '{clash[0]}'")
        if self.jobs.busy(old):
            raise sf.FormError(f"'{old}' is being built right now: wait for it to finish")
        had_schedule = resolver.coverage(self.db_path, old) is not None
        conf = {k: v for k, v in stations[old].items() if not k.startswith("_")}
        conf["network_name"] = new
        if had_schedule:
            if self.db_path:
                legacy.snapshot(self.db_path, old, as_name=new)      # the channel keeps its old programming under the new name
            self.console.reset_schedule(old)
        try:
            self.console.update(conf, name=old)
        except console_client.ConsoleError as e:
            raise console_client.ConsoleError(f"{e}" + (" (its schedule was already cleared: use Rebuild to make a new one)"
                                                        if had_schedule else ""))
        schedules.move(old, new)
        job = self.jobs.start(new, 7, rebuild=True) if had_schedule else None
        return {"name": new, "old": old, "job": job}

    def delete(self, name):
        if name not in self.console.stations():
            raise NotFound(f"there is no station called '{name}'")
        self.console.delete(name)
        schedules.drop(name)

        return {"name": name}

    # ---- keeping the config in step with the dates
    def sync_schedules(self, now=None):
        """Special days are put into a station's config only while they are near (FieldStation42 has no year in its
        dates), so the config is made again each day. Returns the names of the stations whose config was updated."""
        now = now or datetime.now()
        changed = []
        stations = self.console.stations()
        for name in schedules.names():
            conf, defn = stations.get(name), schedules.get(name)
            if conf is None or defn is None or conf.get("network_type", "standard") != "standard":
                continue
            skip = self._empty_lists(defn, fresh=True)
            parts = schedules.conf_parts(defn, now.date(), self._shows(), skip)
            if schedules.conf_matches(conf, parts):
                if skip != schedules.skipped_of(name):
                    schedules.put(name, defn, skipped=skip)
                continue
            live = {k: v for k, v in conf.items() if not k.startswith("_")}
            self.console.update(schedules.apply_parts(live, parts))
            schedules.put(name, defn, skipped=skip)
            changed.append(name)
        return changed

    def preview(self, raw, days=14, now=None):
        """Which layer wins each of the next days for a schedule being edited (not saved)."""
        try:
            defn = schedules.validate(raw)
        except schedules.ScheduleError as e:
            raise sf.FormError(str(e))
        now = now or datetime.now()
        return {"days": [{"date": d.isoformat(), "weekday": schedules.DAYS[d.weekday()], **what}
                         for d, what in schedules.sources(defn, now.date(), days)]}

    def quickstart(self, files, taken_numbers=(), done=None):
        """One click: a working channel from what there is. It plays the biggest list with videos (not the mid cards or
        commercials), or, with no list yet, the first series, folder or movie in the media, all day, in 30 minute slots, with no
        commercials and no mid cards, then builds a week. Raises FormError when there is nothing to make one from."""
        stations = self.console.stations()
        source = label = None
        best = (0, None)
        if self.lists_dir and os.path.isdir(self.lists_dir):
            for name in sorted(os.listdir(self.lists_dir), key=str.casefold):
                path = list_path(self.lists_dir, name)
                if name.startswith(".") or name in ("bump", "commercials") or not os.path.isdir(path):
                    continue
                n = 0
                for _r, _d, names in os.walk(path, followlinks=True):
                    n += sum(1 for f in names if os.path.splitext(f)[1].lower() in VIDEO_EXT)
                    if n >= 2000:
                        break
                if n > best[0]:
                    best = (n, name)
        if best[1]:
            source, label = best[1], f"the list “{best[1]}” ({best[0]}{'+' if best[0] >= 2000 else ''} videos)"
        else:
            try:
                tops = [e for e in files.list("media", "")["entries"] if e["kind"] == "dir"]
                for top in tops:
                    inside = [e for e in files.list("media", top["name"])["entries"] if e["kind"] == "dir" or e.get("video")]
                    if inside:
                        made = files.add_show(f"{top['name']}/{inside[0]['name']}", wait=False, done=done)
                        source, label = made["tag"], f"“{inside[0].get('label') or inside[0]['name']}” from your media"
                        break
            except Exception:
                source = None
        if not source:
            raise sf.FormError("There is nothing to make a channel from yet: put videos in your media folder, or make a list on the "
                               "Lists & files page. The Status page says if the media folder can't be seen.")
        used_names = {n.casefold() for n in stations}
        base, name, k = "My Channel", "My Channel", 1
        while name.casefold() in used_names:
            k += 1
            name = f"{base} {k}"
        used = {c.get("channel_number") for c in stations.values()} | {int(n) for n in taken_numbers if str(n).isdigit()}
        number = next(n for n in range(3, 10000) if n not in used)
        form = {"name": name, "number": number, "kind": "standard", "blocks": [{"from": 0, "to": 24, "lists": [source]}],
                "increment": 30, "breaks": "end", "bump": "bump", "commercials": "", "midcards": False}
        self.save(None, form)
        job = self.jobs.start(name, 7, rebuild=True)
        return {"name": name, "number": number, "source": source, "describe": label, "job": job}

    def build(self, name, days, rebuild=True, refresh=False, resume=False):
        conf = self.console.stations().get(name)
        if conf is None:
            raise NotFound(f"there is no station called '{name}'")
        if conf.get("network_type") == "guide":
            raise sf.FormError("a guide channel is drawn live: there is nothing to build")
        return self.jobs.start(name, days, rebuild, refresh, resume)

    # ---- the schedule
    def schedule(self, name, day=None, now=None):
        """What is scheduled for one calendar day (local time), plus how much schedule the station has."""
        now = now or datetime.now()
        if name not in self.console.stations():
            raise NotFound(f"there is no station called '{name}'")
        day = day or now.date()
        start = datetime(day.year, day.month, day.day)
        cap = self._cap(now)
        beyond = bool(cap and start >= cap)
        blocks = [] if beyond else resolver.listings(self.db_path, name, start, start + timedelta(days=1))
        truncated = len(blocks) > MAX_BLOCKS
        rows = [{"start": b["start"].isoformat(), "end": b["end"].isoformat(),
                 "title": b["title"] + (f": {b['subtitle']}" if b.get("subtitle") else ""),
                 "now": b["start"] <= now < b["end"]} for b in blocks[:MAX_BLOCKS]]
        return {"name": name, "date": day.isoformat(), "blocks": rows, "truncated": truncated,
                "beyond": beyond, "notes": self._empty_notes(name, day), **self._coverage(name, now)}

    def _empty_notes(self, name, day):
        """Hours of this day whose lists have no videos yet: they play the test pattern, which looks like a fault if nothing
        says why. Uses the default week or the season that applies (a special day is not looked at)."""
        try:
            defn = schedules.get(name)
            if not defn:
                return []
            what = schedules.sources(defn, day, 1)[0][1]
            if what["layer"] == "event":
                return []
            week = defn["seasons"].get(what["name"]) if what["layer"] == "season" else None
            videos = (week or defn["default"])[schedules.DAYS[day.weekday()]]["videos"]
            empty = self._empty_lists(names=[x for b in videos for x in b["lists"]])
            out = []
            for b in videos:
                if not set(b["lists"]) <= empty:
                    continue
                names = ", ".join(f"“{x}”" for x in b["lists"])
                what = "the list " + names + " has" if len(b["lists"]) == 1 else "the lists " + names + " have"
                out.append(f"{schedules._clock(b['from'])} to {schedules._clock(b['to'])}: {what} no videos yet, so the test pattern plays.")
            return out[:6]
        except Exception:
            return []

    def block(self, name, start, offset=0, limit=200):
        """The files inside one block of the schedule, a page at a time (a looping station's day has thousands)."""
        if name not in self.console.stations():
            raise NotFound(f"there is no station called '{name}'")
        offset, limit = max(0, int(offset)), max(1, min(MAX_ENTRIES, int(limit)))
        found = resolver.block_plan(self.db_path, name, start)
        if found is None:
            raise NotFound("that part of the schedule is not there any more")
        title, b_start, b_end, entries = found
        page = entries[offset:offset + limit]
        titles = resolver.titles(self.db_path, name, [e.path for _, e in page])
        return {"name": name, "title": title, "start": b_start.isoformat(), "end": b_end.isoformat(),
                "total": len(entries), "offset": offset,
                "entries": [{"file": os.path.basename(e.path), "title": titles.get(e.path), "type": e.content_type,
                             "duration": round(e.duration, 1),
                             "at": (b_start + timedelta(seconds=o)).isoformat(), "stream": e.is_stream}
                            for o, e in page]}
