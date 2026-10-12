"""
Resolve "what is FieldStation42 channel N playing right now, and how far into
the file are we" straight from the FS42 schedule database.

This reimplements LiquidManager.get_play_point() (fs42/liquid_manager.py) against
the sqlite `liquid_blocks` table, so it needs neither the FS42 player nor the
FS42 server to be running -- only read access to the db and the catalog.
"""

import json
import os
import sqlite3

import legacy
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class PlanEntry:
    """One flat segment of playback: a file, a seek offset, a duration.

    FS42 pre-flattens each programming block into these -- feature segments,
    commercials and bumps all land in the same list -- so playing a channel is
    just walking the list. content_type is one of feature/commercial/bump/stream.
    """
    path: str
    skip: float
    duration: float
    is_stream: bool = False
    content_type: str = "feature"
    media_type: str = "video"


@dataclass
class Cursor:
    """Where a channel is at a given instant."""
    station: str
    block_title: str
    block_start: datetime
    block_end: datetime
    plan: list          # full plan for the current block
    index: int          # index of the entry playing now
    offset: float       # seconds already elapsed *within* that entry

    @property
    def entry(self) -> PlanEntry:
        return self.plan[self.index]

    @property
    def seek(self) -> float:
        """Absolute seek position in the underlying file.

        FS42 stores an intra-file `skip` per segment (a show cut into parts
        around ad breaks keeps one path and advancing skips), so the real seek
        is the segment's own skip plus however far we are into that segment.
        See station_player._play_from_point: total_skip = entry.skip + initial_skip
        """
        return self.entry.skip + self.offset

    @property
    def remaining(self) -> float:
        """Seconds left in the current entry."""
        return self.entry.duration - self.offset


class ScheduleGap(Exception):
    """No block covers the requested instant -- schedule ran out or has a hole."""


def _rows_to_plan(plan_json: str) -> list:
    return [
        PlanEntry(
            path=p["path"],
            skip=p.get("skip", 0) or 0,
            duration=p.get("duration", 0) or 0,
            is_stream=p.get("is_stream", False),
            content_type=p.get("content_type", "feature"),
            media_type=p.get("media_type", "video"),
        )
        for p in json.loads(plan_json or "[]")
    ]


def _have_db(db_path: str) -> bool:
    """FieldStation42 creates its schedule database when the first station gets a schedule,
    not at start-up. Until then there is nothing scheduled, which is not an error."""
    return os.path.exists(db_path)


NAMES_HOOK = None      # optional: fn(path) -> (title, episode name or None) for the guide, which shows them apart (titles.py)
TITLE_HOOK = None      # optional: fn(path) -> the name people know a video by, or None (titles.py sets it up in the tuner)


def _names(feature, title):
    """(title, episode name or None) for a listing of the video at `feature`: from its .nfo when there is one."""
    if feature:
        try:
            if NAMES_HOOK is not None:
                got = NAMES_HOOK(feature)
                if got and got[0]:
                    return got[0], got[1]
            elif TITLE_HOOK is not None:
                return TITLE_HOOK(feature) or title, None
        except Exception:
            pass
    return title, None


def _named(title, plan):
    """A block's title: the name from the .nfo of the video it plays when there is one (see titles.py), else FieldStation42's."""
    if TITLE_HOOK is None:
        return title
    for e in plan:
        if e.content_type == "feature" and not e.is_stream:
            try:
                return TITLE_HOOK(e.path) or title
            except Exception:
                return title
    return title


def resolve(db_path: str, station: str, when: datetime = None) -> Cursor:
    """Find the block covering `when` and the exact plan entry + offset inside it."""
    when = when or datetime.now()
    cut = legacy.cut(station)                 # an old schedule is being kept while this station is rebuilt
    if cut is not None and when < cut:
        row = legacy.row_at(station, when)
        if row is None:
            raise ScheduleGap(f"{station} has no scheduled block covering {when.isoformat()}")
    else:
        if not _have_db(db_path):
            raise ScheduleGap(f"{station}: no schedule database yet (build a station and add a day in the console)")
        # julianday, not text comparison: FS42 stores "YYYY-MM-DD HH:MM:SS" with a
        # space, and comparing that against isoformat()'s "T" separator misorders
        # same-day rows.
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                """SELECT title, start_time, end_time, plan_json
                     FROM liquid_blocks
                    WHERE station = ?
                      AND julianday(start_time) <= julianday(?)
                      AND julianday(end_time) > julianday(?)
                    ORDER BY start_time DESC LIMIT 1""",
                (station, when.isoformat(), when.isoformat()),
            ).fetchone()

    if row is None:
        raise ScheduleGap(f"{station} has no scheduled block covering {when.isoformat()}")

    title, start_s, end_s, plan_json = row
    start = datetime.fromisoformat(start_s)
    plan = _rows_to_plan(plan_json)
    title = _named(title, plan)

    # Walk the plan accumulating durations until we pass `when`.
    mark = start
    for i, entry in enumerate(plan):
        nxt = mark + timedelta(seconds=entry.duration)
        if nxt > when:
            return Cursor(
                station=station,
                block_title=title,
                block_start=start,
                block_end=datetime.fromisoformat(end_s),
                plan=plan,
                index=i,
                offset=(when - mark).total_seconds(),
            )
        mark = nxt

    # Block's plan is shorter than the block's wall-clock span: FS42's own
    # get_play_point returns None here and the player falls through. Treat the
    # tail as dead air rather than crashing the stream.
    raise ScheduleGap(f"{station}: plan for '{title}' underruns its slot at {when.isoformat()}")


def stations(db_path: str) -> list:
    found = set(legacy.stations())
    if _have_db(db_path):
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            found |= {r[0] for r in conn.execute("SELECT DISTINCT station FROM liquid_blocks")}
    return sorted(found)


def listings(db_path: str, station: str, start: datetime, end: datetime) -> list:
    """Blocks overlapping a window -- the raw material for XMLTV. While an old schedule is being kept, it supplies
    everything before the switch-over time and the new one everything after (a block that straddles it is cut)."""
    cut = legacy.cut(station)
    out = []
    if cut is not None and start < cut:
        for t, s, e, feature in legacy.rows_between(station, start, min(end, cut), with_feature=True):
            t, sub = _names(feature, t)                               # the old schedule being kept gets the names too
            out.append({"title": t, "subtitle": sub, "start": datetime.fromisoformat(s), "end": min(datetime.fromisoformat(e), cut), "path": feature})
    if _have_db(db_path) and (cut is None or end > cut):
        lo = start if cut is None else max(start, cut)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = None
            if TITLE_HOOK is not None or NAMES_HOOK is not None:      # the path of the video each block plays, taken out of its plan by SQLite
                try:
                    rows = conn.execute(
                        """SELECT title, start_time, end_time,
                                  (SELECT json_extract(j.value, '$.path') FROM json_each(plan_json) j
                                    WHERE json_extract(j.value, '$.content_type') = 'feature' LIMIT 1)
                             FROM liquid_blocks
                            WHERE station = ?
                              AND julianday(start_time) < julianday(?)
                              AND julianday(end_time) > julianday(?)
                            ORDER BY start_time""",
                        (station, end.isoformat(), lo.isoformat())).fetchall()
                except sqlite3.Error:
                    rows = None                      # no JSON support here: the file names it is
            if rows is None:
                rows = [r + (None,) for r in conn.execute(
                    """SELECT title, start_time, end_time FROM liquid_blocks
                        WHERE station = ?
                          AND julianday(start_time) < julianday(?)
                          AND julianday(end_time) > julianday(?)
                        ORDER BY start_time""",
                    (station, end.isoformat(), lo.isoformat()))]
            for t, s, e, feature in rows:
                t, sub = _names(feature, t)
                out.append({"title": t, "subtitle": sub, "start": max(datetime.fromisoformat(s), lo), "end": datetime.fromisoformat(e), "path": feature})
    return out


def block_plan(db_path: str, station: str, start: datetime):
    """The files inside one scheduled block: (title, block start, block end, [(offset seconds, PlanEntry)]), or None.

    `start` is the block's own start time, as listings() reports it."""
    row = None
    cut = legacy.cut(station)
    if cut is not None and start < cut:
        row = legacy.row_by_start(station, start)
    if row is None and _have_db(db_path):
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT title, start_time, end_time, plan_json FROM liquid_blocks "
                "WHERE station = ? AND julianday(start_time) = julianday(?) LIMIT 1",
                (station, start.isoformat())).fetchone()
    if not row:
        return None
    entries, offset = [], 0.0
    try:
        plan = _rows_to_plan(row[3])
    except (ValueError, KeyError, TypeError, AttributeError):      # an unreadable plan: show the block with no files
        plan = []
    for e in plan:
        entries.append((offset, e))
        offset += e.duration
    return _named(row[0], [e for _, e in entries]), datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2]), entries


def titles(db_path: str, station: str, paths) -> dict:
    """{path: title} as FieldStation42's catalog knows them, for the paths it has. Never raises: with no catalog
    table (or none for this station) the caller just shows file names."""
    paths = list(dict.fromkeys(paths))
    out = {}
    if paths and _have_db(db_path):
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                for i in range(0, len(paths), 400):
                    chunk = paths[i:i + 400]
                    for path, title in conn.execute(
                            "SELECT path, title FROM catalog_entries WHERE station = ? "
                            "AND path IN (SELECT value FROM json_each(?))",
                            (station, json.dumps(chunk))):
                        if title:
                            out[path] = title
        except sqlite3.Error:
            out = {}
    if TITLE_HOOK is not None:                       # the name from the video's .nfo wins over the file name
        for p in paths:
            try:
                better = TITLE_HOOK(p)
            except Exception:
                better = None
            if better:
                out[p] = better
    return out


def file_meta_count(db_path: str, table: str = "file_meta") -> int:
    """How many files FieldStation42 has read so far (one row each, written as it reads them). While a catalog
    builds this climbs by one per file, which is the only progress it gives; `chapter_points` then climbs by one per
    file in the pass that reads chapter marks. 0 when it cannot be read."""
    if table not in ("file_meta", "chapter_points") or not _have_db(db_path):
        return 0
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1) as conn:
            query = ("SELECT COUNT(*) FROM chapter_points" if table == "chapter_points"
                     else "SELECT COUNT(*) FROM file_meta")
            return conn.execute(query).fetchone()[0]
    except sqlite3.Error:
        return 0


def catalog_count(db_path: str, station: str) -> int:
    """How many entries a station's catalog has. FieldStation42 writes a catalog in one step at the end of a build, so
    more than 0 means the catalog build finished."""
    if not _have_db(db_path):
        return 0
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            return conn.execute("SELECT COUNT(*) FROM catalog_entries WHERE station = ?", (station,)).fetchone()[0]
    except sqlite3.Error:
        return 0


def days_built(db_path: str, station: str, now=None) -> int:
    """How many days of the NEW schedule FieldStation42 has built (today counts as the first), from where it ends.
    The old schedule being kept (legacy.py) is not counted."""
    if not _have_db(db_path):
        return 0
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            end = conn.execute("SELECT MAX(end_time) FROM liquid_blocks WHERE station = ?", (station,)).fetchone()[0]
    except sqlite3.Error:
        return 0
    if not end:
        return 0
    now = now or datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return max(0, round((datetime.fromisoformat(end) - midnight).total_seconds() / 86400))


def coverage(db_path: str, station: str):
    """(first start, last end) of everything scheduled for a station, as datetimes, or None if nothing is."""
    new = None
    if _have_db(db_path):
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT MIN(start_time), MAX(end_time) FROM liquid_blocks WHERE station = ?", (station,)).fetchone()
        if row and row[0] is not None:
            new = (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]))
    old = legacy.bounds(station) if legacy.cut(station) is not None else None
    if new and old:
        return min(new[0], old[0]), max(new[1], old[1])
    return new or old
