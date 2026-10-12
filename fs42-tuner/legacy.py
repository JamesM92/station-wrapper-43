"""Keep playing the old schedule while a station is rebuilt, then switch to the new one.

FieldStation42 deletes a station's schedule the moment a rebuild starts, so a rebuild that takes 45 minutes would put
the channel off air for 45 minutes. Before a rebuild starts, the schedule that is still to come is copied here (a small
database of our own, in the config volume: FieldStation42's database is never written). resolver.py then reads the
two together:

    until the switch-over time ("cut"), the channel plays the OLD schedule
    from the cut on, it plays whatever FieldStation42 has built (the NEW schedule)

While the rebuild is running the cut is "the end of the old schedule", so the old one plays for as long as it
lasts. When the rebuild finishes, the cut is set to the end of the file that is on air at that moment, so the switch
happens between two files, never in the middle of one. If the rebuild fails the old schedule simply plays on until
it runs out.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

PATH = os.environ.get("FS42_LEGACY", "/config/legacy-schedule.db")
KEEP_AFTER_CUT = timedelta(hours=1)          # an overlay is cleared this long after its switch-over time
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (station TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
                                   title TEXT, plan_json TEXT);
CREATE INDEX IF NOT EXISTS blocks_station ON blocks (station, start_time);
CREATE TABLE IF NOT EXISTS meta (station TEXT PRIMARY KEY, handover TEXT, created TEXT);
"""


def _rw():
    folder = os.path.dirname(PATH) or "."
    os.makedirs(folder, exist_ok=True)
    fresh = not os.path.exists(PATH)
    conn = sqlite3.connect(PATH, timeout=10)
    conn.executescript(SCHEMA)
    if fresh:
        try:
            os.chmod(PATH, 0o644)  # the owner is the config folder's: chown below
            if os.geteuid() == 0:
                ref = os.stat(folder)
                os.chown(PATH, ref.st_uid, ref.st_gid)
        except OSError:
            pass
    return conn


def _ro():
    """A read-only connection, or None when there is no file (the usual case: nothing is being rebuilt)."""
    if not os.path.exists(PATH):
        return None
    try:
        return sqlite3.connect(f"file:{PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def _dt(text):
    return datetime.fromisoformat(text) if text else None


def _walk(row, when):
    """(entry end time) of the plan entry airing at `when` in a block row (title, start, end, plan_json), or None."""
    start = _dt(row[1])
    try:
        plan = json.loads(row[3] or "[]")
    except ValueError:
        return None
    mark = start
    for e in plan:
        mark = mark + timedelta(seconds=float(e.get("duration", 0) or 0))
        if mark > when:
            return mark
    return None


# ---- writing (the stations page's build job)
def snapshot(main_db, station, as_name=None, now=None):
    try:
        return _snapshot(main_db, station, as_name, now)
    except (OSError, sqlite3.Error):
        return False                                   # nowhere to keep it: the rebuild just goes ahead as before


def _snapshot(main_db, station, as_name=None, now=None):
    """Copy the schedule still to come for `station` (from FieldStation42's database) so it can keep playing.
    as_name stores it under another name (a station being renamed). Returns True if there is an old schedule being
    kept for that name afterwards. An old schedule already being kept is left as it is, unless it has run out: a
    second rebuild must not replace the good old schedule with the half-built new one."""
    name = as_name or station
    now = now or datetime.now()
    with _lock:
        conn = _rw()
        try:
            cut = _cut(conn, name)
            if cut is not None and cut > now:
                return True
            if not main_db or not os.path.exists(main_db):
                return False
            try:
                with sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=10) as src:
                    rows = src.execute("SELECT title, start_time, end_time, plan_json FROM liquid_blocks "
                                       "WHERE station = ? AND julianday(end_time) > julianday(?) ORDER BY start_time",
                                       (station, now.isoformat())).fetchall()
            except sqlite3.Error:
                return False
            conn.execute("DELETE FROM blocks WHERE station = ?", (name,))
            conn.execute("DELETE FROM meta WHERE station = ?", (name,))
            if not rows:
                conn.commit()
                return False
            conn.executemany("INSERT INTO blocks VALUES (?,?,?,?,?)", [(name, s, e, t, p) for t, s, e, p in rows])
            conn.execute("INSERT INTO meta VALUES (?, NULL, ?)", (name, now.isoformat()))
            conn.commit()
            return True
        finally:
            conn.close()


def handover(station, now=None):
    try:
        return _handover(station, now)
    except (OSError, sqlite3.Error):
        return None


def _handover(station, now=None):
    """The rebuild is finished: switch to the new schedule at the end of the file airing now. Returns that time, or
    None when no old schedule is being kept."""
    now = now or datetime.now()
    with _lock:
        conn = _rw()
        try:
            if conn.execute("SELECT 1 FROM meta WHERE station = ?", (station,)).fetchone() is None:
                return None
            row = conn.execute("SELECT title, start_time, end_time, plan_json FROM blocks WHERE station = ? "
                               "AND julianday(start_time) <= julianday(?) AND julianday(end_time) > julianday(?) "
                               "ORDER BY start_time DESC LIMIT 1", (station, now.isoformat(), now.isoformat())).fetchone()
            at = (_walk(row, now) or _dt(row[2])) if row else now
            conn.execute("UPDATE meta SET handover = ? WHERE station = ?", (at.isoformat(), station))
            conn.commit()
            return at
        finally:
            conn.close()


def drop(station):
    with _lock:
        conn = _rw()
        try:
            conn.execute("DELETE FROM blocks WHERE station = ?", (station,))
            conn.execute("DELETE FROM meta WHERE station = ?", (station,))
            conn.commit()
        finally:
            conn.close()


def cleanup(now=None):
    try:
        _cleanup(now)
    except (OSError, sqlite3.Error):
        pass


def _cleanup(now=None):
    """Forget old schedules that are done with: their switch-over is more than an hour past, or (when the rebuild
    never finished) they ended more than an hour ago."""
    if not os.path.exists(PATH):
        return
    now = now or datetime.now()
    with _lock:
        conn = _rw()
        try:
            for (name,) in conn.execute("SELECT station FROM meta").fetchall():
                cut = _cut(conn, name)
                if cut is None or cut < now - KEEP_AFTER_CUT:
                    conn.execute("DELETE FROM blocks WHERE station = ?", (name,))
                    conn.execute("DELETE FROM meta WHERE station = ?", (name,))
            conn.commit()
        finally:
            conn.close()


# ---- reading (resolver.py)
def _cut(conn, station):
    meta = conn.execute("SELECT handover FROM meta WHERE station = ?", (station,)).fetchone()
    if meta is None:
        return None
    if meta[0]:
        return _dt(meta[0])
    last = conn.execute("SELECT MAX(end_time) FROM blocks WHERE station = ?", (station,)).fetchone()[0]
    return _dt(last)


def cut(station):
    """When the new schedule takes over for this station, or None when no old schedule is being kept."""
    conn = _ro()
    if conn is None:
        return None
    try:
        return _cut(conn, station)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def info(station, now=None):
    """For the stations page: {"cut": iso, "finished": bool} while an old schedule is being kept, else None.
    `finished` is False while the rebuild is still running (the old schedule plays until it ends)."""
    now = now or datetime.now()
    conn = _ro()
    if conn is None:
        return None
    try:
        meta = conn.execute("SELECT handover FROM meta WHERE station = ?", (station,)).fetchone()
        c = _cut(conn, station)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if meta is None or c is None or c < now:
        return None
    return {"cut": c.isoformat(), "finished": bool(meta[0])}


def row_at(station, when):
    conn = _ro()
    if conn is None:
        return None
    try:
        return conn.execute("SELECT title, start_time, end_time, plan_json FROM blocks WHERE station = ? "
                            "AND julianday(start_time) <= julianday(?) AND julianday(end_time) > julianday(?) "
                            "ORDER BY start_time DESC LIMIT 1", (station, when.isoformat(), when.isoformat())).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def rows_between(station, start, end, with_feature=False):
    """(title, start, end) of the kept blocks overlapping a window; with_feature adds the path of the video each plays
    (taken out of its plan by SQLite), so a better name can be found for it. None where that can't be done."""
    conn = _ro()
    if conn is None:
        return []
    try:
        if with_feature:
            try:
                return conn.execute(
                    """SELECT title, start_time, end_time,
                              (SELECT json_extract(j.value, '$.path') FROM json_each(plan_json) j
                                WHERE json_extract(j.value, '$.content_type') = 'feature' LIMIT 1)
                         FROM blocks WHERE station = ? AND julianday(start_time) < julianday(?) AND julianday(end_time) > julianday(?)
                        ORDER BY start_time""", (station, end.isoformat(), start.isoformat())).fetchall()
            except sqlite3.Error:
                pass
        rows = conn.execute("SELECT title, start_time, end_time FROM blocks WHERE station = ? "
                            "AND julianday(start_time) < julianday(?) AND julianday(end_time) > julianday(?) "
                            "ORDER BY start_time", (station, end.isoformat(), start.isoformat())).fetchall()
        return [r + (None,) for r in rows] if with_feature else rows
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def row_by_start(station, start):
    conn = _ro()
    if conn is None:
        return None
    try:
        return conn.execute("SELECT title, start_time, end_time, plan_json FROM blocks WHERE station = ? "
                            "AND julianday(start_time) = julianday(?) LIMIT 1", (station, start.isoformat())).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def bounds(station):
    """(first start, last end) of the kept old schedule, or None."""
    conn = _ro()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT MIN(start_time), MAX(end_time) FROM blocks WHERE station = ?", (station,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return (_dt(row[0]), _dt(row[1])) if row and row[0] else None


def stations():
    conn = _ro()
    if conn is None:
        return []
    try:
        return [r[0] for r in conn.execute("SELECT station FROM meta")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
