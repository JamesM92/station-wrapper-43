"""Seasonal schedules and special days for a standard station.

What the station page edits is a DEFINITION, kept by the wrapper (schedules.json in the config volume):

    {"default": WEEK,
     "seasons": {"dates": {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"},
                 "spring": WEEK or None, "summer": ..., "fall": ..., "winter": ...},     # None: same as default
     "events": [EVENT, ...]}
    WEEK  = {"monday": DAY, ... "sunday": DAY}
    DAY   = {"videos": [{"from": 18, "to": 20.5, "lists": ["Sitcoms"]}], "commercials": [{"from": 18, "to": 21, "list": "Ads"}]}
    EVENT = {"id", "name", "when": RANGE or NTH, "replace": bool, "mode": "same" or "weekday", "schedule": DAY or WEEK}
    RANGE = {"type": "range", "start": "07-21", "end": "07-27", "year": 2026 or None (every year)}
    NTH   = {"type": "nth", "month": 11, "weekday": "thursday", "nth": 4 (1-5, or -1 for the last), "days": 1}

The definition is turned into the keys of a FieldStation42 station config, which has these layers natively:

    date_overrides   one day's hourly slots for a date            (special days: highest priority)
    week_overrides   a week of hourly slots for a date range      (seasons)
    day_templates    the normal week                              (default)

An hour that a higher layer does not define falls through to the next one, which is what lets a special day change
only some slots. FieldStation42 builds a day when it is added to the schedule, so each day gets the layer that
applies to ITS date, and days already built keep what they were built with.

FieldStation42's dates have no year, so a one-off event is put in the config only while it is close (within
LOOKAHEAD days), and the keeper regenerates the config every day. FieldStation42 cannot name 29 February.

Premieres: a show in a slot marked `premiere` airs its NEW episodes there, one each time the slot's day comes round (every
Friday, say), and plays its OLDER episodes, in order and looping, wherever else it is scheduled:

    "premieres": {"<show list>": {"from": "<where the new episodes start, a folder or file inside the show>", "since": "2026-10-16"}}

The k-th such day since `since` airs the k-th new episode, as a date override that names a folder holding only that
episode; when there is no new episode left that day plays the loop as usual. The loop is a hidden folder of the older
episodes plus the ones already premiered (premieres.py makes the folders). It is all a function of the date, so a rebuilt
or repeated build gives the same answer, and FieldStation42 needs no state of its own for it.

The smallest unit is 30 minutes: an hour can be split into two halves of one list each (tags: [first half, second
half]); an hour with several lists in one block picks one at random (random_tags). Commercials are per hour
(`commercial_dir` on the hour's slot).
"""
import copy
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import date, timedelta

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SEASONS = ("spring", "summer", "fall", "winter")
DEFAULT_DATES = {"spring": "03-01", "summer": "06-01", "fall": "09-01", "winter": "12-01"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
LOOKAHEAD = 21                       # days ahead a special day is put into the station config
MAX_EVENTS = 60
MAX_NAME = 60
PATH = os.environ.get("FS42_SCHEDULES", "/config/schedules.json")
_lock = threading.Lock()


class ScheduleError(ValueError):
    """The definition is not valid. The text is for the person editing it."""


# ---- cleaning ---------------------------------------------------------------------------------------------
def _list_name(value, where):
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value or value.startswith("."):
        raise ScheduleError(f"{where}: a list is one folder name from the Lists page")
    return value.strip()


def _half_hours(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value * 2 != int(value * 2):
        raise ScheduleError(f"{where}: times must be in half hours")
    return float(value)


def _cells(videos):
    """48 half-hour cells, each the index of the block covering it (or None)."""
    cells = [None] * 48
    for i, b in enumerate(videos):
        for c in range(int(b["from"] * 2), int(b["to"] * 2)):
            cells[c] = i
    return cells


def _videos(blocks, full, where):
    if not isinstance(blocks, list):
        raise ScheduleError(f"{where}: the time blocks are not valid")
    out = []
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict):
            raise ScheduleError(f"{where}: block {i} is not valid")
        lo, hi = _half_hours(b.get("from"), where), _half_hours(b.get("to"), where)
        if not 0 <= lo < hi <= 24:
            raise ScheduleError(f"{where}: block {i} must end after it starts, within the day")
        lists = b.get("lists")
        if not isinstance(lists, list) or not lists:
            raise ScheduleError(f"{where}: block {i} needs at least one list")
        clean = []
        for name in lists:
            name = _list_name(name, where)
            if name not in clean:
                clean.append(name)
        blk = {"from": lo, "to": hi, "lists": clean}
        start = b.get("start") or b.get("end")            # cards of this slot's own: the start and end of its breaks
        if start:
            blk["start"] = _list_name(start, where)
            blk["end"] = _list_name(b.get("end") or start, where)
        if b.get("burn") not in (None, "", False):          # captions drawn on the picture while this block plays
            blk["burn"] = True
        if b.get("premiere") not in (None, "", False):      # the new episodes of the show in this block air here
            if len(clean) != 1:
                raise ScheduleError(f"{where}: block {i} premieres new episodes of one show, so it can hold only that one list")
            blk["premiere"] = True
        if b.get("span") not in (None, ""):                 # one slot drawn across days: its pieces share this id
            if not isinstance(b["span"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", b["span"]):
                raise ScheduleError(f"{where}: block {i} has a link to other days that is not valid")
            blk["span"] = b["span"]
        out.append(blk)
    out.sort(key=lambda b: b["from"])
    for a, b in zip(out, out[1:]):
        if b["from"] < a["to"]:
            raise ScheduleError(f"{where}: two blocks overlap around {_clock(b['from'])}")
    if full:
        mark = 0.0
        for b in out:
            if b["from"] != mark:
                raise ScheduleError(f"{where}: nothing is scheduled at {_clock(mark)}: the day needs to be covered all the way")
            mark = b["to"]
        if mark != 24:
            raise ScheduleError(f"{where}: nothing is scheduled at {_clock(mark)}: the day needs to be covered all the way")
    cells = _cells(out)
    for h in range(24):
        a, b = cells[2 * h], cells[2 * h + 1]
        if a != b:
            if a is None or b is None:
                raise ScheduleError(f"{where}: the hour at {_clock(h)} is only half filled: a half hour can't stand alone")
            if len(out[a]["lists"]) != 1 or len(out[b]["lists"]) != 1:
                raise ScheduleError(f"{where}: a block that starts or ends at half past ({_clock(h + 0.5)}) can hold only one list")
    return out


def _commercials(blocks, where):
    if blocks is None:
        return []
    if not isinstance(blocks, list):
        raise ScheduleError(f"{where}: the commercial blocks are not valid")
    out = []
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict):
            raise ScheduleError(f"{where}: commercial block {i} is not valid")
        lo, hi = b.get("from"), b.get("to")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) for v in (lo, hi)):
            raise ScheduleError(f"{where}: commercials change on the hour")
        lo, hi = int(lo), int(hi)
        if not 0 <= lo < hi <= 24:
            raise ScheduleError(f"{where}: commercial block {i} must end after it starts, within the day")
        if not (isinstance(b.get("list"), str) and b["list"].strip()):
            raise ScheduleError(f"{where}: choose what plays for the commercial block at {_clock(lo)}")
        out.append({"from": lo, "to": hi, "list": _list_name(b.get("list"), where)})
    out.sort(key=lambda b: b["from"])
    for a, b in zip(out, out[1:]):
        if b["from"] < a["to"]:
            raise ScheduleError(f"{where}: two commercial blocks overlap around {_clock(b['from'])}")
    return out


def _cards(blocks, where):
    """The mid card blocks of a day: [{"from", "to", "start", "end"}] on whole hours: which list plays at the start of a break
    and which at its end, for the hours of that block. The hours it leaves out use the station's own two lists."""
    if blocks is None:
        return []
    if not isinstance(blocks, list):
        raise ScheduleError(f"{where}: the mid card blocks are not valid")
    out = []
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict):
            raise ScheduleError(f"{where}: mid card block {i} is not valid")
        lo, hi = b.get("from"), b.get("to")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) for v in (lo, hi)):
            raise ScheduleError(f"{where}: mid cards change on the hour")
        lo, hi = int(lo), int(hi)
        if not 0 <= lo < hi <= 24:
            raise ScheduleError(f"{where}: mid card block {i} must end after it starts, within the day")
        if not (isinstance(b.get("start"), str) and b["start"].strip()):
            raise ScheduleError(f"{where}: choose the mid cards for the block at {_clock(lo)}")
        start = _list_name(b.get("start"), where)
        out.append({"from": lo, "to": hi, "start": start, "end": _list_name(b.get("end") or start, where)})
    out.sort(key=lambda b: b["from"])
    for a, b in zip(out, out[1:]):
        if b["from"] < a["to"]:
            raise ScheduleError(f"{where}: two mid card blocks overlap around {_clock(b['from'])}")
    return out


def cards_folder(start, end):
    """The bump folder a pair of lists is played from: the list itself when it is the same one for both, else a hidden
    folder of the lists folder holding `pre` (the start cards) and `post` (the end cards), which FieldStation42 reads as such."""
    if start == end:
        return start
    clean = lambda n: re.sub(r"[^A-Za-z0-9_-]+", "_", n).strip("_") or "x"
    return f".cards.{clean(start)}--{clean(end)}"


def card_pairs(defn):
    """Every (start, end) pair of different lists a definition uses in its mid card blocks (the folders to make)."""
    pairs = []

    def day(d):
        for c in list(d.get("cards", [])) + [b for b in d.get("videos", []) if b.get("start")]:
            p = (c["start"], c["end"])
            if p[0] != p[1] and p not in pairs:
                pairs.append(p)
    for w in [defn["default"]] + [defn["seasons"][s] for s in SEASONS if defn["seasons"].get(s)]:
        for d in DAYS:
            day(w[d])
    for ev in defn["events"]:
        if ev["mode"] == "same":
            day(ev["schedule"])
        else:
            for d in DAYS:
                day(ev["schedule"][d])
    return pairs


def _clock(h):
    return f"{int(h):02d}:{'30' if h % 1 else '00'}"


def _day(day, full, where):
    if not isinstance(day, dict):
        raise ScheduleError(f"{where}: that day is not valid")
    return {"videos": _videos(day.get("videos", []), full, where),
            "commercials": _commercials(day.get("commercials"), where),
            "cards": _cards(day.get("cards"), where)}


def _week(week, full, where):
    if not isinstance(week, dict) or set(week) != set(DAYS):
        raise ScheduleError(f"{where}: a week needs all seven days")
    out = {d: _day(week[d], full, f"{where}, {d.capitalize()}") for d in DAYS}
    _spans(out, where)
    return out


def _spans(week, where):
    """A slot drawn across days is one piece on each day with a shared `span` id. The pieces must say the same thing
    (lists, cards, captions) and there is at most one on a day: that is what makes it one slot."""
    seen = {}
    for d in DAYS:
        for b in week[d]["videos"]:
            sid = b.get("span")
            if not sid:
                continue
            same = {k: b.get(k) for k in ("lists", "start", "end", "burn")}
            if sid in seen and seen[sid][0] == d:
                raise ScheduleError(f"{where}: {d.capitalize()} has two pieces of the same slot that runs across days")
            if sid in seen and seen[sid][1] != same:
                raise ScheduleError(f"{where}: the slot that runs across days has different settings on {seen[sid][0].capitalize()} and {d.capitalize()}")
            seen.setdefault(sid, (d, same))


def _md(text, where):
    """'03-01' -> (3, 1). 29 February does not exist for FieldStation42."""
    try:
        m, d = text.split("-")
        m, d = int(m), int(d)
        date(2001, m, d)
        return m, d
    except (AttributeError, ValueError):
        raise ScheduleError(f"{where}: that date is not valid (use month-day, like 07-21; 29 February can't be used)")


def _event(ev, i):
    where = f"special day {i}"
    if not isinstance(ev, dict):
        raise ScheduleError(f"{where} is not valid")
    name = ev.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise ScheduleError(f"{where} needs a name (up to {MAX_NAME} characters)")
    name = name.strip()
    where = f"special day '{name}'"
    when = ev.get("when")
    if not isinstance(when, dict):
        raise ScheduleError(f"{where} needs dates")
    if when.get("type") == "range":
        sm, sd = _md(when.get("start"), where)
        em, ed = _md(when.get("end"), where)
        year = when.get("year")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100):
            raise ScheduleError(f"{where}: the year is not valid")
        clean_when = {"type": "range", "start": f"{sm:02d}-{sd:02d}", "end": f"{em:02d}-{ed:02d}", "year": year}
        if year is not None and (em, ed) < (sm, sd):
            raise ScheduleError(f"{where}: it ends before it starts (for a range over New Year, leave the year empty)")
    elif when.get("type") == "nth":
        month, weekday, nth, days = when.get("month"), when.get("weekday"), when.get("nth"), when.get("days", 1)
        if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
            raise ScheduleError(f"{where}: the month is not valid")
        if weekday not in DAYS:
            raise ScheduleError(f"{where}: the weekday is not valid")
        if nth not in (1, 2, 3, 4, 5, -1):
            raise ScheduleError(f"{where}: choose the 1st to 5th, or the last, of the weekday")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
            raise ScheduleError(f"{where}: how many days must be between 1 and 31")
        clean_when = {"type": "nth", "month": month, "weekday": weekday, "nth": nth, "days": days}
    else:
        raise ScheduleError(f"{where}: choose a date range or a day like 'the 4th Thursday of November'")
    replace = bool(ev.get("replace", False))
    mode = ev.get("mode", "same")
    if mode not in ("same", "weekday"):
        raise ScheduleError(f"{where}: the lineup is the same every day, or different on each weekday")
    sched = ev.get("schedule")
    full = replace                                   # replacing the whole day means the day must be filled
    if mode == "same":
        sched = _day(sched, full, where)
        if not sched["videos"]:
            raise ScheduleError(f"{where}: draw at least one slot")
    else:
        sched = _week(sched, full, where)
        if not any(sched[d]["videos"] for d in DAYS):
            raise ScheduleError(f"{where}: draw at least one slot")
    ident = ev.get("id") if isinstance(ev.get("id"), str) and ev.get("id") else uuid.uuid4().hex[:8]
    return {"id": ident, "name": name, "when": clean_when, "replace": replace, "mode": mode, "schedule": sched}


def _premieres(raw, clean):
    """{show list: {"from", "since"}} for the shows that have a block marked as their premiere slot (settings for a show
    nothing premieres are dropped; a marked block with no settings gets none until they are given: no new episodes yet)."""
    raw = raw if isinstance(raw, dict) else {}
    used = set()

    def day(d):
        for b in d["videos"]:
            if b.get("premiere"):
                used.add(b["lists"][0])
    for w in [clean["default"]] + [clean["seasons"][s] for s in SEASONS if clean["seasons"].get(s)]:
        for d in DAYS:
            day(w[d])
    for ev in clean["events"]:
        for d in ([ev["schedule"]] if ev["mode"] == "same" else [ev["schedule"][x] for x in DAYS]):
            day(d)
    out = {}
    for name in sorted(used):
        got = raw.get(name) if isinstance(raw.get(name), dict) else {}
        frm = got.get("from", "")
        if not isinstance(frm, str) or ".." in frm.split("/") or frm.startswith("/") or len(frm) > 400:
            raise ScheduleError(f"the premiere of '{name}': where the new episodes start is not valid")
        since = got.get("since")
        try:
            since = date.fromisoformat(since).isoformat() if isinstance(since, str) else date.today().isoformat()
        except ValueError:
            raise ScheduleError(f"the premiere of '{name}': the date it starts from is not valid")
        out[name] = {"from": frm.strip("/"), "since": since}
    return out


def validate(defn):
    """A definition cleaned, or ScheduleError."""
    if not isinstance(defn, dict):
        raise ScheduleError("the schedule is not valid")
    clean = {"default": _week(defn.get("default"), False, "the default schedule")}      # gaps play the test pattern (off air)
    seasons = defn.get("seasons") or {}
    if not isinstance(seasons, dict):
        raise ScheduleError("the seasons are not valid")
    dates = dict(DEFAULT_DATES)
    given = seasons.get("dates") or {}
    if not isinstance(given, dict):
        raise ScheduleError("the season dates are not valid")
    for s in SEASONS:
        if s in given:
            m, d = _md(given[s], f"the start of {s}")
            dates[s] = f"{m:02d}-{d:02d}"
    if len(set(dates.values())) != 4:
        raise ScheduleError("each season needs its own start date")
    clean_seasons = {"dates": dates}
    for s in SEASONS:
        w = seasons.get(s)
        clean_seasons[s] = None if w is None else _week(w, False, f"the {s} schedule")       # a gap falls back to the default week
    clean["seasons"] = clean_seasons
    events = defn.get("events") or []
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ScheduleError(f"there can be at most {MAX_EVENTS} special days")
    clean["events"] = [_event(e, i) for i, e in enumerate(events, 1)]
    clean["premieres"] = _premieres(defn.get("premieres"), clean)
    ids = [e["id"] for e in clean["events"]]
    if len(set(ids)) != len(ids):
        for e in clean["events"]:
            e["id"] = uuid.uuid4().hex[:8]
    return clean


def from_legacy(form):
    """The default week of a form that has the older `blocks` / `days` (whole hours, no seasons or special days)."""
    def day(blocks):
        return {"videos": [{"from": b["from"], "to": b["to"], "lists": list(b["lists"])} for b in blocks], "commercials": [], "cards": []}
    if form.get("days"):
        week = {d: day(form["days"][d]) for d in DAYS}
    else:
        week = {d: day(form["blocks"]) for d in DAYS}
    return {"default": week, "seasons": {"dates": dict(DEFAULT_DATES), **{s: None for s in SEASONS}}, "events": []}


def lists_of(defn):
    """Every list a definition plays or draws commercials from, once each, in the order they first appear."""
    seen = []

    def day(d):
        for b in d["videos"]:
            seen.extend(x for x in b["lists"] if x not in seen)
            seen.extend(x for x in (b.get("start"), b.get("end")) if x and x not in seen)
        for c in d["commercials"]:
            if c["list"] not in seen:
                seen.append(c["list"])
        for c in d.get("cards", []):
            seen.extend(x for x in (c["start"], c["end"]) if x not in seen)
    for w in [defn["default"]] + [defn["seasons"][s] for s in SEASONS if defn["seasons"].get(s)]:
        for d in DAYS:
            day(w[d])
    for ev in defn["events"]:
        if ev["mode"] == "same":
            day(ev["schedule"])
        else:
            for d in DAYS:
                day(ev["schedule"][d])
    return seen


def apply_parts(conf, parts):
    """A copy of a live station config with its schedule keys replaced by these parts (everything else kept)."""
    out = {k: copy.deepcopy(v) for k, v in conf.items() if k not in ("day_templates", "week_overrides", "date_overrides") + DAYS}
    out.update(copy.deepcopy(parts))
    return out


def is_plain(defn):
    """True when there are no seasons, no special days and no commercials: a schedule the older form could hold."""
    if defn.get("events") or any(defn["seasons"].get(s) for s in SEASONS):
        return False
    return not any(defn["default"][d]["commercials"] or defn["default"][d].get("cards")
                   or any(b.get("start") for b in defn["default"][d]["videos"]) for d in DAYS)


# ---- dates ------------------------------------------------------------------------------------------------
def nth_weekday(year, month, weekday, nth):
    """The date of the nth (1-5, or -1 for the last) weekday of a month, or None (there is no 5th one)."""
    wd = DAYS.index(weekday)
    if nth == -1:
        d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        return d - timedelta(days=(d.weekday() - wd) % 7)
    first = date(year, month, 1)
    d = first + timedelta(days=(wd - first.weekday()) % 7 + 7 * (nth - 1))
    return d if d.month == month else None


def occurrences(ev, lo, hi):
    """[(first date, last date)] of an event that overlap lo..hi (the days that can matter now)."""
    when, out = ev["when"], []
    for year in range(lo.year - 1, hi.year + 1):
        if when["type"] == "nth":
            start = nth_weekday(year, when["month"], when["weekday"], when["nth"])
            if start is None:
                continue
            end = start + timedelta(days=when["days"] - 1)
        else:
            if when["year"] is not None and when["year"] != year:
                continue
            sm, sd = _md(when["start"], "")
            em, ed = _md(when["end"], "")
            start = date(year, sm, sd)
            end = date(year + ((em, ed) < (sm, sd)), em, ed)
        if end >= lo and start <= hi:
            out.append((start, end))
    return out


def _key(d):
    return f"{MONTHS[d.month - 1]} {d.day}"


def _winner(events, d):
    """The event that applies on date d (the one that starts latest), and the day schedule it gives, or (None, None)."""
    best = None
    for ev in events:
        for a, b in occurrences(ev, d, d):
            if a <= d <= b and (best is None or a > best[0] or (a == best[0] and ev["name"] > best[1]["name"])):
                best = (a, ev)
    if best is None:
        return None, None
    ev = best[1]
    day = ev["schedule"] if ev["mode"] == "same" else ev["schedule"][DAYS[d.weekday()]]
    return ev, day


def _week_on(defn, d):
    """The week of the schedule definition that applies on date d: the season's own, or the default."""
    seasons = defn["seasons"]
    if any(seasons.get(s) for s in SEASONS):
        order = sorted(SEASONS, key=lambda s: _md(seasons["dates"][s], s))
        md = (d.month, d.day)
        label = order[-1]                               # before the first start of the year: the last season
        for s in reversed(order):
            if md >= _md(seasons["dates"][s], s):
                label = s
                break
        if seasons.get(label):
            return seasons[label]
    return defn["default"]


def burn_at(defn, when):
    """Are the captions drawn on the picture at `when` (a datetime)? The `burn` of the video block that covers that time
    on the layer that wins that day (a special day, else a season, else the default week)."""
    d = when.date()
    t = when.hour + when.minute / 60.0
    layers = []
    if (d.month, d.day) != (2, 29):
        ev, day = _winner(defn.get("events", []), d)
        if ev is not None:
            layers.append(day)
    layers.append(_week_on(defn, d)[DAYS[d.weekday()]])
    for day in layers:                                  # an hour a special day does not cover falls through
        for b in day.get("videos", []):
            if b["from"] <= t < b["to"]:
                return b.get("burn") is True
    return False


# ---- the config -------------------------------------------------------------------------------------------
SERIES = "series"          # the sequence name of a slot that plays a show: FieldStation42 keeps one place in each show


def _slot(lists, commercial=None, shows=(), cards=None):
    slot = {"tags": lists[0]} if len(lists) == 1 else {"tags": list(lists), "random_tags": True}
    if commercial:
        slot["commercial_dir"] = commercial
    if cards:
        slot["bump_dir"] = cards
    if any(n in shows for n in lists):          # a show is played in order, one episode further each time it airs
        slot["sequence"] = SERIES
    return slot


def hours_of(day, shows=(), skip=()):
    """One DAY as FieldStation42's hourly slots ({"20": {"tags": ...}}). Hours with no video are left out, and so are hours
    whose lists are all in `skip` (lists with no videos in them yet: they stay in the schedule definition, and the hour
    plays the test pattern until they have some)."""
    videos, cells = day["videos"], _cells(day["videos"])
    ads = {}
    for c in day["commercials"]:
        for h in range(c["from"], c["to"]):
            ads[h] = c["list"]
    cards = {}
    for c in day.get("cards", []):
        for h in range(c["from"], c["to"]):
            cards[h] = cards_folder(c["start"], c["end"])
    for h in range(24):                                  # a video slot's own cards beat the time period's (the Mid cards tab)
        for i in (cells[2 * h], cells[2 * h + 1]):
            if i is not None and videos[i].get("start"):
                cards[h] = cards_folder(videos[i]["start"], videos[i]["end"])
                break
    hours = {}
    for h in range(24):
        a, b = cells[2 * h], cells[2 * h + 1]
        if a is None and b is None:
            continue
        if a == b:
            lists = [x for x in videos[a]["lists"] if x not in skip]
            if not lists:
                continue
            slot = _slot(lists, ads.get(h), shows, cards.get(h))
        else:
            firsts = [x for x in (videos[a]["lists"][0], videos[b]["lists"][0]) if x not in skip]
            if not firsts:
                continue
            slot = {"tags": firsts if len(firsts) > 1 else firsts[0]}
            if ads.get(h):
                slot["commercial_dir"] = ads[h]
            if cards.get(h):
                slot["bump_dir"] = cards[h]
            if any(x in shows for x in firsts):
                slot["sequence"] = SERIES
        hours[str(h)] = slot
    return hours


def _season_entries(defn, shows=(), skip=()):
    """[(date range text, week)] for the seasons, or [] when no season has a schedule of its own. A season with none
    uses the default week, written out, so every day of the year is covered by a season."""
    seasons = defn["seasons"]
    if not any(seasons.get(s) for s in SEASONS):
        return []
    dates = {s: _md(seasons["dates"][s], s) for s in SEASONS}
    order = sorted(SEASONS, key=lambda s: dates[s])
    entries, late = [], []
    for i, s in enumerate(order):
        nxt = dates[order[(i + 1) % 4]]
        end = date(2001, nxt[0], nxt[1]) - timedelta(days=1)
        widened = (end.month, end.day) == (2, 28)           # 29 February can't be named: stretch the end to 1 March
        end_text = "March 1" if widened else _key(end)
        text = f"{_key(date(2001, *dates[s]))} - {end_text}"
        week = seasons.get(s) or defn["default"]
        item = (text, {d: hours_of(week[d], shows, skip) for d in DAYS})
        (late if widened else entries).append(item)
    return entries + late                                   # the one that was stretched comes last: the next season wins 1 March


def _premiere_day_pieces(day, show):
    return [i for i, b in enumerate(day["videos"]) if b.get("premiere") and b["lists"][0] == show]


def premiere_dates(defn, show, lo, hi):
    """The dates in lo..hi on which `show`'s premiere slot airs: a day with a block marked for it that no special day
    replaces. These are what the new episodes are counted by."""
    out, d = [], lo
    while d <= hi:
        if (d.month, d.day) != (2, 29) and _winner(defn.get("events", []), d)[0] is None \
                and _premiere_day_pieces(_week_on(defn, d)[DAYS[d.weekday()]], show):
            out.append(d)
        d += timedelta(days=1)
    return out


def premiere_index(defn, show, d):
    """How many premieres of `show` aired before date d, counted from the date they started: so the premiere on d airs the
    new episode with this number (0 is the first new episode)."""
    since = date.fromisoformat((defn.get("premieres") or {}).get(show, {}).get("since", d.isoformat()))
    if d <= since:
        return 0
    return len(premiere_dates(defn, show, since, d - timedelta(days=1)))


def _swapped(defn, loops):
    """A copy of the definition whose blocks play the loop folder of a show with premieres instead of the show itself."""
    out = copy.deepcopy(defn)

    def day(d):
        for b in d["videos"]:
            b["lists"] = [loops.get(x, x) for x in b["lists"]]
    for w in [out["default"]] + [out["seasons"][s] for s in SEASONS if out["seasons"].get(s)]:
        for d in DAYS:
            day(w[d])
    for ev in out["events"]:
        for d in ([ev["schedule"]] if ev["mode"] == "same" else [ev["schedule"][x] for x in DAYS]):
            day(d)
    return out


def _premiere_hours(orig, base, d, premieres, shows, skip):
    """The hours of date d (no special day) that a premiere changes: the premiere slot plays the new episode's folder."""
    key = DAYS[d.weekday()]
    o, b = _week_on(orig, d)[key], _week_on(base, d)[key]
    day = copy.deepcopy(b)
    changed = False
    for show, info in premieres.items():
        for i in _premiere_day_pieces(o, show):
            k = premiere_index(orig, show, d)
            if k < len(info.get("new", ())):
                day["videos"][i]["lists"] = [info["new"][k]]
                changed = True
    if not changed:
        return {}
    was, now = hours_of(b, shows, skip), hours_of(day, shows, skip)
    return {h: slot for h, slot in now.items() if was.get(h) != slot}


def conf_parts(defn, today, shows=(), skip=(), premieres=None):
    """The station-config keys this definition makes: day_templates + the seven days, week_overrides, date_overrides.
    Keys with nothing to say are left out (so a plain schedule makes a plain config)."""
    parts = {}
    templates, names = {}, {}
    orig = defn
    premieres = {s: i for s, i in (premieres or {}).items() if s in (defn.get("premieres") or {})}
    loops = {s: i["loop"] for s, i in premieres.items() if i.get("loop")}
    if loops:                               # every other airing of the show plays its older episodes, in order, looping
        defn = _swapped(defn, loops)
        shows = list(shows) + list(loops.values())
    for d in DAYS:
        hours = hours_of(defn["default"][d], shows, skip)
        key = json.dumps(hours, sort_keys=True)
        if key not in names:
            names[key] = f"schedule{len(names) + 1}"
            templates[names[key]] = hours
        parts[d] = names[key]
    if len(templates) == 1:                                  # every day the same: the plain form's template name
        (only,) = templates.values()
        templates, parts = {"daily": only}, {d: "daily" for d in DAYS}
    parts["day_templates"] = templates
    weeks = _season_entries(defn, shows, skip)
    if weeks:
        parts["week_overrides"] = {text: week for text, week in weeks}
    dates = {}
    for n in range(LOOKAHEAD + 1):
        d = today + timedelta(days=n)
        if (d.month, d.day) == (2, 29):
            continue
        ev, day = _winner(defn["events"], d)
        if ev is None:
            hours = _premiere_hours(orig, defn, d, premieres, shows, skip) if premieres else {}
        else:
            hours = hours_of(day, shows, skip)
        if hours:
            dates[_key(d)] = hours
    if dates:
        parts["date_overrides"] = dates
    return parts


def conf_matches(conf, parts):
    """Does a station's live config carry exactly these schedule keys?"""
    for key in ("day_templates", "week_overrides", "date_overrides") + DAYS:
        if json.dumps(conf.get(key), sort_keys=True) != json.dumps(parts.get(key), sort_keys=True):
            return False
    return True


def sources(defn, start, days=14):
    """[(date, what applies)] for the next days, to show which layer wins each day."""
    out = []
    seasons = defn["seasons"]
    has_seasons = any(seasons.get(s) for s in SEASONS)
    for n in range(days):
        d = start + timedelta(days=n)
        ev, day = _winner(defn["events"], d) if (d.month, d.day) != (2, 29) else (None, None)
        if ev is not None:
            out.append((d, {"layer": "event", "name": ev["name"], "replace": ev["replace"]}))
            continue
        if has_seasons:
            label = None
            order = sorted(SEASONS, key=lambda s: _md(seasons["dates"][s], s))
            md = (d.month, d.day)
            for s in reversed(order):
                if md >= _md(seasons["dates"][s], s):
                    label = s
                    break
            label = label or order[-1]                      # before the first start of the year: the last season
            out.append((d, {"layer": "season" if seasons.get(label) else "default", "name": label}))
        else:
            out.append((d, {"layer": "default", "name": "default"}))
    return out


# ---- keeping it ---------------------------------------------------------------------------------------------
def _load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("stations"), dict) else {"stations": {}}
    except (OSError, ValueError):
        return {"stations": {}}


def _save(path, data):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".schedules-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        os.chmod(tmp, 0o644)  # the owner is the config folder's: chown below
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if os.geteuid() == 0:
        try:
            ref = os.stat(folder)
            os.chown(path, ref.st_uid, ref.st_gid)
        except OSError:
            pass


def get(station, path=None):
    """A station's saved definition, or None."""
    with _lock:
        d = _load(path or PATH)["stations"].get(station)
    return copy.deepcopy(d) if isinstance(d, dict) else None


def put(station, defn, path=None, skipped=None):
    """Keep a station's definition. `skipped`: the lists that had no videos when its config was made, and so were left
    out of it (see hours_of): it is how the config is recognised as the one made from this definition."""
    with _lock:
        data = _load(path or PATH)
        data["stations"][station] = defn
        data.setdefault("skipped", {})
        if skipped:
            data["skipped"][station] = sorted(skipped)
        else:
            data["skipped"].pop(station, None)
        _save(path or PATH, data)


def skipped_of(station, path=None):
    """The lists left out of a station's config when it was last made from its definition."""
    with _lock:
        got = (_load(path or PATH).get("skipped") or {}).get(station)
    return frozenset(x for x in got if isinstance(x, str)) if isinstance(got, list) else frozenset()


def drop(station, path=None):
    with _lock:
        data = _load(path or PATH)
        data.get("skipped", {}).pop(station, None)
        if data["stations"].pop(station, None) is not None:
            _save(path or PATH, data)


def move(old, new, path=None):
    with _lock:
        data = _load(path or PATH)
        if old in data["stations"]:
            data["stations"][new] = data["stations"].pop(old)
            sk = data.get("skipped", {})
            if old in sk:
                sk[new] = sk.pop(old)
            _save(path or PATH, data)


def names(path=None):
    with _lock:
        return sorted(_load(path or PATH)["stations"])
