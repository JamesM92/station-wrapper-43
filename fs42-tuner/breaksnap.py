"""Move a video's commercial-break points onto the nearest real gap in the picture and sound.

FieldStation42 puts the breaks of an "interrupting" station at the chapter marks of each video. Marks made by
a ripping tool or a release group are often a minute or more away from the real scene change. This looks at
the video around each mark, for a black frame and for silence, and moves the mark to the best one nearby. A
mark with nothing close by is left as it was.

An EDL file beside a video (see breakstore.py) is used as it is and wins over both the chapter marks and the
scan. Every result is kept in our own store (breakstore.py), so each video is looked at once, and again only if the
video or its EDL changes. The marks are also put into the one table of FieldStation42's database its schedule
builder reads, chapter_points; nothing else there is written. FieldStation42 only reads a file's marks once, so what
we put there stays.

    snap_station(db_path, station)   the videos of one station: EDL marks, or chapter marks moved onto gaps
"""
import json
import os
import re
import sqlite3
import subprocess

import breakstore

WINDOW = float(os.environ.get("BREAK_SNAP_WINDOW", "90"))      # look this far either side of a mark, seconds
MIN_SEGMENT = 20.0            # never leave a piece of programme shorter than this between two marks
MIN_BLACK = 0.04
MIN_SILENCE = 0.35

_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_detect(text):
    """ffmpeg's blackdetect/silencedetect log -> (black [(start, end)], silence [(start, end)])."""
    black = [(float(a), float(b)) for a, b in _BLACK.findall(text)]
    silence, start = [], None
    for line in text.splitlines():
        m = _SIL_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = _SIL_END.search(line)
        if m and start is not None:
            silence.append((start, float(m.group(1))))
            start = None
    return black, silence


def choose(target, black, silence, window=WINDOW):
    """The best place for a break near `target`, from the black and silent stretches found around it, or None.

    A black frame with silence over it is a real cut. Black alone is next best, and a long silence on its own
    (a pause) the last resort: a weaker kind is only picked when it is much nearer."""
    def overlaps(a0, a1):
        return any(s0 <= a1 + 0.3 and s1 >= a0 - 0.3 for s0, s1 in silence)

    cands = []
    for b0, b1 in black:
        if b1 - b0 >= MIN_BLACK:
            cands.append(((b0 + b1) / 2, 0.0 if overlaps(b0, b1) else 12.0))
    for s0, s1 in silence:
        if s1 - s0 >= MIN_SILENCE and not any(b0 <= s1 and b1 >= s0 for b0, b1 in black):
            cands.append(((s0 + s1) / 2, 40.0))
    best, best_score = None, None
    for at, penalty in cands:
        d = abs(at - target)
        if d > window:
            continue
        if best_score is None or d + penalty < best_score:
            best, best_score = at, d + penalty
    return best


def snap(chapters, duration, detect):
    """(new chapters, how many marks moved). `detect(lo, hi)` returns (black, silence), times counted from the
    start of the file."""
    if not chapters or len(chapters) < 2:
        return chapters, 0
    out = [dict(c) for c in chapters]
    moved = 0
    for i in range(1, len(out)):
        old = float(out[i]["chapter_start"])
        if old < MIN_SEGMENT or old > duration - MIN_SEGMENT:
            continue                                    # a mark at the very start or end is not a break
        black, silence = detect(max(0.0, old - WINDOW), min(duration, old + WINDOW))
        at = choose(old, black, silence)
        if at is None:
            continue
        before = float(out[i - 1]["chapter_start"])
        after = float(out[i + 1]["chapter_start"]) if i + 1 < len(out) else duration
        if at - before < MIN_SEGMENT or after - at < MIN_SEGMENT:
            continue                                    # it would leave a sliver of programme
        out[i]["chapter_start"] = round(at, 3)
        moved += 1
    for i, c in enumerate(out):
        was_next = float(chapters[i + 1]["chapter_start"]) if i + 1 < len(chapters) else duration
        now_next = float(out[i + 1]["chapter_start"]) if i + 1 < len(out) else duration
        if abs(float(chapters[i].get("chapter_end", was_next)) - was_next) < 0.5:
            c["chapter_end"] = round(now_next, 3)          # it ended where the next one began: it still does
        c["segment_duration"] = round(now_next - float(c["chapter_start"]), 3)
    return out, moved


def ffmpeg_detect(path, lo, hi, run=subprocess.run):
    """Look at [lo, hi] of a file for black frames and silence. Times come back counted from the start of the file."""
    cmd = ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-threads", "2",
           "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", path,
           "-vf", "scale=160:-2,blackdetect=d=0.04:pix_th=0.12:pic_th=0.92",
           "-af", "silencedetect=n=-45dB:d=0.12", "-f", "null", "-"]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return [], []
    black, silence = parse_detect(r.stderr or "")
    return [(a + lo, b + lo) for a, b in black], [(a + lo, b + lo) for a, b in silence]


def snap_station(db_path, station, detect_for=ffmpeg_detect, progress=None):
    """Settle the break marks of every video in `station`'s catalog. Returns (videos changed, marks moved or read).
    progress(done, total) is called after each video."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            "SELECT DISTINCT c.realpath, c.duration, p.points FROM catalog_entries c "
            "LEFT JOIN chapter_points p ON p.path = c.realpath WHERE c.station = ? AND c.realpath IS NOT NULL", (station,)).fetchall()
    except sqlite3.Error:
        conn.close()
        return 0, 0
    store = breakstore.load()
    changed = marks = 0
    try:
        for n, (path, duration, points) in enumerate(rows, 1):
            duration = float(duration or 0)
            try:
                st = os.stat(path)
                chapters = json.loads(points) if points else []
            except (ValueError, OSError):
                continue
            key = f"{st.st_size}:{int(st.st_mtime)}"
            have = store.get(path) or {}
            edl, edl_sig = breakstore.read_edl(path)
            new = None
            if edl:                                      # 1. an EDL beside the video
                if not (have.get("source") == "edl" and have.get("sig") == edl_sig and have.get("file") == key
                        and points and breakstore.sig(json.loads(points)) == have.get("out")):
                    new = breakstore.chapters_from_points(edl, duration)
                    entry = {"source": "edl", "orig": have.get("orig", chapters or None), "points": new,
                             "file": key, "sig": edl_sig}
                    marks += len(new) - 1
            elif isinstance(chapters, list) and len(chapters) > 1:       # 2. chapter marks moved onto real gaps
                cur = breakstore.sig(chapters)
                if not (have.get("source") == "snap" and have.get("out") == cur and have.get("file") == key):
                    orig = have.get("orig") if have.get("source") == "edl" else None
                    base = orig or chapters                # an EDL that has gone away: back to the marks the file had
                    new, moved = snap(base, duration, lambda lo, hi, p=path: detect_for(p, lo, hi))
                    entry = {"source": "snap", "orig": base, "points": new, "file": key, "sig": None}
                    marks += moved
            if new is not None:
                if new != chapters:
                    conn.execute("REPLACE INTO chapter_points VALUES (?, ?, datetime('now'))", (path, json.dumps(new)))
                    conn.commit()
                    changed += 1
                entry["out"] = breakstore.sig(new)
                store[path] = entry
                breakstore.save(store)
            if progress:
                progress(n, len(rows))
    finally:
        conn.close()
    return changed, marks
