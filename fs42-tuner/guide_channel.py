"""A TV guide channel: a scrolling grid of what is on every channel, drawn here and streamed like any other channel.

FieldStation42's own guide channel needs a screen (it draws in a Tk window), which this container never has. This
draws the same kind of picture itself: Pillow makes each frame, the frames go to ffmpeg, and ffmpeg encodes them with
the same settings as every other channel, with the user's music underneath. It only runs while someone is watching.

What it shows: every channel in the guide (the schedule's channels and any other guide source the dashboard is
set up with), the next few hours as a grid, the programme airing now lit, a line for the time now, and messages
along the bottom. Channels with nothing listed are left out. The rows scroll up when there are more than fit.

The station's own FieldStation42 keys carry messages and scroll_speed (so its console understands them). The look, the
hours and the music (a folder, or one file) are in `wrapper_guide`: FieldStation42 checks a sound_to_play file when
its console starts and refuses to start if it is missing (a media share that is away), so it is never given one.
A guide made before this (music in sound_to_play) still plays its music.
"""
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta

from defusedxml import ElementTree as ET      # the guide document carries feeds from outside: parsed the careful way

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                      # the image has it; a bare checkout may not
    Image = ImageDraw = ImageFont = None

AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".opus", ".wma"}
DEFAULT_MESSAGES = ["Welcome to the Channel Guide"]
FONT_REGULAR = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"]
FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]

THEMES = {
    "modern-blue": {
        "bg_top": (8, 24, 64), "bg_bottom": (16, 62, 140),
        "header": (6, 18, 50), "ruler": (12, 36, 92),
        "row_a": (255, 255, 255, 14), "row_b": (255, 255, 255, 4),
        "cell": (30, 78, 176), "cell_edge": (92, 140, 240), "cell_now": (52, 120, 235), "cell_now_edge": (170, 205, 255),
        "text": (255, 255, 255), "dim": (170, 195, 240), "now_line": (255, 205, 70), "ticker": (6, 18, 50),
        "ticker_text": (255, 235, 170),
    },
}
THEMES["midnight"] = {       # near-black with an orange line: easy on a dark room
    "bg_top": (10, 10, 14), "bg_bottom": (28, 28, 38),
    "header": (6, 6, 9), "ruler": (22, 22, 30),
    "row_a": (255, 255, 255, 12), "row_b": (255, 255, 255, 3),
    "cell": (46, 46, 62), "cell_edge": (92, 92, 120), "cell_now": (96, 62, 24), "cell_now_edge": (255, 160, 60),
    "text": (240, 240, 245), "dim": (150, 150, 170), "now_line": (255, 140, 30), "ticker": (6, 6, 9),
    "ticker_text": (255, 190, 110),
}
THEMES["classic-prevue"] = {   # the look of the 90s cable guide: deep blue, teal cells, a yellow clock line
    "bg_top": (0, 0, 120), "bg_bottom": (0, 0, 70),
    "header": (0, 0, 90), "ruler": (0, 40, 150),
    "row_a": (255, 255, 255, 10), "row_b": (0, 0, 0, 30),
    "cell": (0, 96, 128), "cell_edge": (120, 220, 235), "cell_now": (0, 150, 150), "cell_now_edge": (255, 255, 120),
    "text": (255, 255, 255), "dim": (160, 210, 235), "now_line": (255, 255, 0), "ticker": (0, 0, 90),
    "ticker_text": (255, 255, 120),
}
THEMES["emerald"] = {
    "bg_top": (4, 36, 28), "bg_bottom": (10, 82, 62),
    "header": (3, 26, 20), "ruler": (8, 58, 44),
    "row_a": (255, 255, 255, 14), "row_b": (255, 255, 255, 4),
    "cell": (22, 110, 84), "cell_edge": (90, 205, 165), "cell_now": (40, 160, 120), "cell_now_edge": (190, 255, 230),
    "text": (255, 255, 255), "dim": (170, 230, 210), "now_line": (255, 220, 90), "ticker": (3, 26, 20),
    "ticker_text": (200, 255, 225),
}
THEMES["sunset"] = {
    "bg_top": (52, 14, 70), "bg_bottom": (150, 44, 60),
    "header": (36, 8, 52), "ruler": (92, 26, 84),
    "row_a": (255, 255, 255, 14), "row_b": (255, 255, 255, 4),
    "cell": (140, 48, 96), "cell_edge": (235, 130, 150), "cell_now": (214, 84, 78), "cell_now_edge": (255, 210, 160),
    "text": (255, 255, 255), "dim": (240, 190, 210), "now_line": (255, 215, 90), "ticker": (36, 8, 52),
    "ticker_text": (255, 220, 170),
}
DEFAULT_THEME = "modern-blue"
THEME_NAMES = {"modern-blue": "Modern blue", "midnight": "Midnight", "classic-prevue": "Classic cable", "emerald": "Emerald", "sunset": "Sunset"}
LAPS = 2                            # a long name goes right through its cell this many times while the guide scrolls up once
TITLE_HOLD = 2.5                    # seconds it rests at the start of each lap (never more than a seventh of the lap)
MIN_MOVING = 36                     # pixels: a cell narrower than this shows the start of a long name and does not move it
MIN_LAP = 8.0                       # seconds: no name goes round faster than this to be read, however quick the vertical scroll
STILL_CYCLE = 40.0                  # seconds one round is taken to last when the guide is not scrolling up (it all fits)
SPEEDS = {"slow": 0.6, "normal": 1.0, "fast": 1.8}
READABLE = 1.4                      # a cell narrower than this many row heights can't be read: a run of them is merged into one
ROTATE = 3.0                        # seconds each clip's name of a merged cell is shown before the next


# ---- options -----------------------------------------------------------------------------------------------
def options(conf):
    """The guide's settings from its station config, cleaned. Never raises."""
    conf = conf or {}
    extra = conf.get("wrapper_guide") if isinstance(conf.get("wrapper_guide"), dict) else {}
    msgs = conf.get("messages")
    msgs = [m.strip() for m in msgs if isinstance(m, str) and m.strip()] if isinstance(msgs, list) else []
    try:
        speed = float(conf.get("scroll_speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    music = extra.get("music") if isinstance(extra.get("music"), str) else (conf.get("sound_to_play") if conf.get("play_sound") else "")
    try:
        hours = int(extra.get("hours", 3))
    except (TypeError, ValueError):
        hours = 3
    title = extra.get("title") if isinstance(extra.get("title"), str) and extra.get("title").strip() else "Channel Guide"
    return {"messages": msgs or list(DEFAULT_MESSAGES), "scroll_speed": min(5.0, max(0.1, speed)),
            "music": music if isinstance(music, str) else "", "hours": min(6, max(1, hours)),
            "title": title.strip()[:40], "theme": extra.get("theme") if extra.get("theme") in THEMES else "modern-blue"}


def audio_files(path):
    """Audio files under a folder (every level), or the one file: sorted. [] when there are none."""
    if not path:
        return []
    if os.path.isfile(path):
        return [path] if os.path.splitext(path)[1].lower() in AUDIO_EXT else []
    out = []
    for root, _dirs, names in os.walk(path, followlinks=True):
        out += [os.path.join(root, n) for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXT and not n.startswith(".")]
    return sorted(out)


class MusicIndex:
    """The music files of a folder, found once and remembered (also across restarts). Looking through a big music folder on a
    network share can take minutes, and the guide channel must not wait for that to start playing: get() waits only a
    few seconds, then says it is not ready and the scan carries on in the background."""

    def __init__(self, store=None, ttl=6 * 3600):
        self.store, self.ttl = store, ttl
        self._lock = threading.Lock()
        self._found = {}                      # path -> (when, [files])
        self._busy = {}                       # path -> the thread scanning it
        try:
            with open(store) as f:
                self._found = {k: (v["at"], v["files"]) for k, v in json.load(f).items()}
        except (OSError, ValueError, KeyError, TypeError):
            self._found = {}

    def _save(self):
        if not self.store:
            return
        try:
            tmp = self.store + ".tmp"
            with open(tmp, "w") as f:
                json.dump({k: {"at": a, "files": fl} for k, (a, fl) in self._found.items()}, f)
            os.replace(tmp, self.store)
        except OSError:
            pass

    def _scan(self, path):
        files = audio_files(path)
        with self._lock:
            self._found[path] = (time.time(), files)
            self._busy.pop(path, None)
            self._save()

    def get(self, path, budget=3.0):
        """(files, ready). A remembered list comes back at once (and is looked at again in the background when it is old);
        a folder not seen yet is given `budget` seconds, then (the files found so far = [], False)."""
        with self._lock:
            hit = self._found.get(path)
            running = self._busy.get(path)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1], True
            if running is None:
                running = threading.Thread(target=self._scan, args=(path,), daemon=True, name="guide-music-scan")
                self._busy[path] = running
                running.start()
        if hit:
            return hit[1], True                  # old, but good until the new look is done
        running.join(budget)
        with self._lock:
            hit = self._found.get(path)
        return (hit[1], True) if hit else ([], False)

    def wait(self, path, timeout=3600):
        """Block until the scan of `path` is done; returns the files (for the thread that restarts the guide with music)."""
        with self._lock:
            running = self._busy.get(path)
        if running is not None:
            running.join(timeout)
        with self._lock:
            hit = self._found.get(path)
        return hit[1] if hit else []


def playlist_text(files, rounds=30, seed=None):
    """An ffconcat playlist that plays the files in a shuffled order, over and over (a new shuffle each round)."""
    rng = random.Random(seed)
    lines = ["ffconcat version 1.0"]
    for _ in range(rounds):
        order = list(files)
        rng.shuffle(order)
        lines += ["file '" + f.replace("'", "'\\''") + "'" for f in order]
    return "\n".join(lines) + "\n"


# ---- what is on ---------------------------------------------------------------------------------------------
def _when(text):
    """An XMLTV time ('20261007204500 -0400') as a local naive datetime."""
    try:
        dt = datetime.strptime(text.strip(), "%Y%m%d%H%M%S %z")
        return dt.astimezone().replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(text.strip()[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None


def rows_from_xmltv(xml, start, end, skip_names=()):
    """[{"number", "name", "items": [{"title", "start", "end"}]}] for every channel, in channel order. A channel with nothing
    in start..end is still listed, with one "Off air" cell across the window, as a TV guide lists a channel that is dark.
    `xml` is the dashboard's merged XMLTV guide (bytes)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    info = {}
    for ch in root.iter("channel"):
        names = [(d.text or "").strip() for d in ch.findall("display-name") if (d.text or "").strip()]
        number = next((n for n in names if re.fullmatch(r"\d+(\.\d+)?", n)), "")
        name = next((n for n in names if n != number), number)
        info[ch.get("id")] = {"number": number, "name": name, "items": []}
    for pr in root.iter("programme"):
        row = info.get(pr.get("channel"))
        a, b = _when(pr.get("start", "")), _when(pr.get("stop", ""))
        if row is None or a is None or b is None or b <= start or a >= end:
            continue
        t, st = pr.find("title"), pr.find("sub-title")
        row["items"].append({"title": ((t.text if t is not None else "") or "").strip() or "(no title)", "start": a, "end": b,
                             "sub": ((st.text if st is not None else "") or "").strip()})
    rows = [r for r in info.values() if r["name"] not in skip_names]
    for r in rows:
        r["items"].sort(key=lambda i: i["start"])
        if not r["items"]:
            r["items"] = [{"title": "Off air", "start": start, "end": end, "sub": ""}]

    def key(r):
        try:
            return (0, float(r["number"]), r["name"])
        except ValueError:
            return (1, 0.0, r["name"])
    return sorted(rows, key=key)


# ---- drawing ------------------------------------------------------------------------------------------------
def _font(paths, size):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _clip(draw, text, font, width):
    """text cut to fit `width` pixels, with an ellipsis."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return (text + "…") if text else ""


class Guide:
    """Makes the frames. frame(t) is one raw RGB picture of the guide at t seconds into its run."""

    def __init__(self, opts, size=(1280, 720), rows_fn=None, now=datetime.now):
        if Image is None:
            raise RuntimeError("the guide channel needs Pillow (python3 -m pip install pillow)")
        self.opts, self.W, self.H = opts, size[0], size[1]
        self.rows_fn, self.now = rows_fn or (lambda start, end: []), now
        self.theme = THEMES[opts["theme"]]
        W, H = self.W, self.H
        self.header_h, self.ruler_h, self.ticker_h = int(H * 0.11), int(H * 0.055), int(H * 0.07)
        self.row_h = max(24, int(H * 0.09))
        self.label_w = int(W * 0.17)
        self.view_h = H - self.header_h - self.ruler_h - self.ticker_h
        self.f_title = _font(FONT_BOLD, int(H * 0.055))
        self.f_clock = _font(FONT_REGULAR, int(H * 0.04))
        self.f_num = _font(FONT_BOLD, int(self.row_h * 0.36))
        self.f_name = _font(FONT_REGULAR, int(self.row_h * 0.24))
        self.f_cell = _font(FONT_BOLD, int(self.row_h * 0.30))
        self.f_time = _font(FONT_REGULAR, int(self.row_h * 0.22))
        self.f_ruler = _font(FONT_BOLD, int(self.ruler_h * 0.5))
        self.f_ticker = _font(FONT_REGULAR, int(self.ticker_h * 0.5))
        self.bg = self._background()
        self.ticker = self._ticker_strip()
        self._board_key, self._board, self._ruler, self._t0 = None, None, None, None
        self._clock_key, self._clock = None, None

    # -- pieces made once
    def _background(self):
        top, bottom = self.theme["bg_top"], self.theme["bg_bottom"]
        col = Image.new("RGB", (1, self.H))
        col.putdata([tuple(int(top[i] + (bottom[i] - top[i]) * y / (self.H - 1)) for i in range(3)) for y in range(self.H)])
        return col.resize((self.W, self.H))

    def _ticker_strip(self):
        text = "   •   ".join(self.opts["messages"]) + "   •   "
        d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        w = max(int(d.textlength(text, font=self.f_ticker)), self.W)
        strip = Image.new("RGBA", (w * 2, self.ticker_h), self.theme["ticker"] + (255,))
        sd = ImageDraw.Draw(strip)
        y = (self.ticker_h - self.f_ticker.size) // 2 - 2 if hasattr(self.f_ticker, "size") else 4
        for x in (0, w):
            sd.text((x, y), text, font=self.f_ticker, fill=self.theme["ticker_text"])
        return strip, w

    # -- the grid, made again every half minute
    def _window(self, now):
        t0 = now.replace(second=0, microsecond=0)
        t0 = t0.replace(minute=0 if now.minute < 30 else 30)
        return t0, t0 + timedelta(hours=self.opts["hours"])

    def _build_board(self, now):
        t0, t1 = self._window(now)
        rows = self.rows_fn(t0, t1) or []
        W, th = self.W, self.theme
        grid_x, grid_w = self.label_w, self.W - self.label_w - int(self.W * 0.015)
        pps = grid_w / ((t1 - t0).total_seconds())
        board = Image.new("RGBA", (W, max(self.row_h * len(rows), 1)), (0, 0, 0, 0))
        d = ImageDraw.Draw(board, "RGBA")
        marquees = []                                     # titles too long for their cell: they scroll (see frame())
        rotors = []                                       # the names of a merged cell: one is shown at a time (see frame())
        for i, r in enumerate(rows):
            y = i * self.row_h
            d.rectangle([0, y, W, y + self.row_h], fill=th["row_a"] if i % 2 == 0 else th["row_b"])
            d.text((int(W * 0.012), y + int(self.row_h * 0.08)), r["number"] or "", font=self.f_num, fill=th["text"])
            d.text((int(W * 0.012), y + int(self.row_h * 0.58)), _clip(d, r["name"], self.f_name, self.label_w - int(W * 0.02)),
                   font=self.f_name, fill=th["dim"])
            for it in self._cells(r["items"], t0, t1, grid_x, pps):
                a, b = max(it["start"], t0), min(it["end"], t1)
                x0, x1 = grid_x + int((a - t0).total_seconds() * pps), grid_x + int((b - t0).total_seconds() * pps)
                if x1 - x0 < 6:
                    continue
                live = it["start"] <= now < it["end"]
                box = [x0 + 2, y + 4, x1 - 2, y + self.row_h - 4]
                # a programme that carries on past an edge of the grid (it started before the left edge, or runs on past the right
                # one) has a square end on that side; one that starts or ends inside the grid keeps its rounded corners
                round_left, round_right = it["start"] >= t0, it["end"] <= t1
                radius = max(0, min(int(self.row_h * 0.14), (box[2] - box[0]) // 2 - 1))     # never more than the cell can take
                d.rounded_rectangle(box, radius=radius, fill=th["cell_now" if live else "cell"],
                                    outline=th["cell_now_edge" if live else "cell_edge"], width=1,
                                    corners=(round_left, round_right, round_right, round_left))
                pad = int(self.row_h * 0.14)
                room = x1 - x0 - 2 * pad
                if it.get("names"):                                  # a run of clips too narrow to read, as one cell
                    self._merged(d, rotors, it, t0, x0, x1, y, pad, room)
                    continue
                # the show's name, big; a name too long for its slot moves along the cell and is not cut off
                self._line(d, marquees, ("◂ " if it["start"] < t0 else "") + it["title"], self.f_cell, th["text"], x0 + pad, y + int(self.row_h * 0.12), room)
                # then the time and, on the same line and in the same small type, the episode's name (which moves too when it is long)
                sy = y + int(self.row_h * 0.58)
                span = f"{it['start']:%-I:%M} – {it['end']:%-I:%M %p}"
                span_w = int(d.textlength(span, font=self.f_time))
                if span_w > room or not it.get("sub"):
                    d.text((x0 + pad, sy), _clip(d, span, self.f_time, room), font=self.f_time, fill=th["dim"])
                else:
                    d.text((x0 + pad, sy), span, font=self.f_time, fill=th["dim"])
                    sep = "  ·  "
                    ex = x0 + pad + span_w
                    d.text((ex, sy), sep, font=self.f_time, fill=th["dim"])
                    ex += int(d.textlength(sep, font=self.f_time))
                    if x1 - pad - ex >= 40:
                        self._line(d, marquees, it["sub"], self.f_time, th["dim"], ex, sy, x1 - pad - ex)
        ruler = Image.new("RGBA", (W, self.ruler_h), th["ruler"] + (255,))
        rd = ImageDraw.Draw(ruler)
        t = t0
        while t < t1:
            x = grid_x + int((t - t0).total_seconds() * pps)
            rd.line([x, int(self.ruler_h * 0.3), x, self.ruler_h], fill=th["dim"], width=1)
            rd.text((x + 6, int(self.ruler_h * 0.18)), f"{t:%-I:%M %p}", font=self.f_ruler, fill=th["text"])
            t += timedelta(minutes=30)
        self._board, self._ruler, self._t0, self._pps = board, ruler, t0, pps
        self._marquees = marquees
        self._rotors = rotors
        # every name that moves goes right through its cell LAPS times while the board scrolls up once (so they are all
        # in step and none is still half read when the rows move on); when it all fits and nothing scrolls up, a calm pace
        total = board.height
        if rows and total > self.view_h:
            self._lap = max(((total + self.row_h) / (30 * self.opts["scroll_speed"])) / LAPS, MIN_LAP)
        else:
            self._lap = STILL_CYCLE / LAPS
        self._rows = len(rows)

    def _cells(self, items, t0, t1, grid_x, pps):
        """The cells of a row. A run of two or more programmes that are each too narrow to read (a feed of short clips) becomes
        ONE cell over the whole run, which names its clips in turn; anything else is a cell of its own."""
        need = self.row_h * READABLE

        def width(it):
            return (min(it["end"], t1) - max(it["start"], t0)).total_seconds() * pps

        cells, run = [], []

        def flush():
            if len(run) >= 2:
                names = []
                for it in run:
                    if not names or names[-1] != it["title"]:
                        names.append(it["title"])
                cells.append({"title": names[0], "sub": "", "start": run[0]["start"], "end": run[-1]["end"],
                              "names": names, "clips": len(run)})
            else:
                cells.extend(run)
            run.clear()
        for it in items:
            if 6 <= width(it) < need:
                run.append(it)
            else:
                flush()
                cells.append(it)
        flush()
        return cells

    def _line(self, d, marquees, text, font, fill, x, y, room):
        """One line of text in a cell: drawn as it is when it fits, else set up to move along the cell. A cell too narrow to
        read anything in (a programme of a minute or two) gets the start of the text and no movement."""
        if room <= 0:
            return
        if d.textlength(text, font=font) <= room:
            d.text((x, y), text, font=font, fill=fill)
        elif room < MIN_MOVING:
            d.text((x, y), _clip(d, text, font, room), font=font, fill=fill)
        else:
            marquees.append(self._marquee(d, text, font, fill, x, y, room))

    def _merged(self, d, rotors, it, t0, x0, x1, y, pad, room):
        """The inside of a merged cell: the clips' names one after another (each drawn once here, shown in turn by frame(); a
        name too long for the cell moves along it while it is up), and under them the time it covers and how many clips."""
        th = self.theme
        names = ["◂ " + n if it["start"] < t0 and i == 0 else n for i, n in enumerate(it["names"])]
        h = int(self.f_cell.size * 1.45) if hasattr(self.f_cell, "size") else int(self.row_h * 0.4)
        shown, starts, at = [], [], 0.0
        for n in names:
            w = int(d.textlength(n, font=self.f_cell))
            strip = Image.new("RGBA", (max(w, room, 1), h), (0, 0, 0, 0))
            ImageDraw.Draw(strip).text((0, 0), n, font=self.f_cell, fill=th["text"])
            starts.append(at)
            shown.append({"strip": strip, "over": max(0, w - room)})
            at += ROTATE if w <= room else ROTATE * 1.6          # a long name stays up longer: it has to be read as it moves
        rotors.append({"x": x0 + pad, "y": y + int(self.row_h * 0.12), "w": max(room, 1), "h": h, "names": shown,
                       "starts": starts, "period": at})
        sy = y + int(self.row_h * 0.58)
        for label in (f"{it['start']:%-I:%M} – {it['end']:%-I:%M %p}  ·  {it['clips']} clips", f"{it['clips']} clips"):
            if d.textlength(label, font=self.f_time) <= room:
                break
        d.text((x0 + pad, sy), _clip(d, label, self.f_time, room), font=self.f_time, fill=th["dim"])

    def _marquee(self, d, text, font, fill, x, y, room):
        """A line as a strip of its own (the text, a gap, the text again), to be cropped to the cell as it moves."""
        gap = int(self.row_h * 0.9)
        text_w = int(d.textlength(text, font=font))
        h = int((font.size if hasattr(font, "size") else self.row_h * 0.3) * 1.45)
        strip = Image.new("RGBA", (2 * (text_w + gap) + room, h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(strip)
        for dx in (0, text_w + gap):
            sd.text((dx, 0), text, font=font, fill=fill)
        return {"x": x, "y": y, "w": room, "h": h, "strip": strip, "cycle": text_w + gap}

    @staticmethod
    def _marquee_offset(m, t, lap):
        """How far along its strip a long line is at t seconds, when one round takes `lap` seconds: still for a moment,
        then moving all the way round, then again from the start with no jump."""
        hold = min(TITLE_HOLD, lap / 7)
        phase = t % lap
        return 0 if phase < hold else min(int((phase - hold) / (lap - hold) * m["cycle"]), m["cycle"])

    def _draw_marquees(self, view, t, shifts):
        """The long titles, at their place on each copy of the board that is showing."""
        lap = self._lap
        for m in self._marquees:
            piece = None
            for shift in shifts:
                y = m["y"] + shift
                if y + m["h"] <= 0 or y >= self.view_h:
                    continue                                   # not in the picture
                if piece is None:
                    off = self._marquee_offset(m, t, lap)
                    piece = m["strip"].crop((off, 0, off + m["w"], m["h"]))
                view.alpha_composite(piece, (m["x"], y)) if y >= 0 else view.alpha_composite(piece.crop((0, -y, piece.width, piece.height)), (m["x"], 0))

    def _draw_rotors(self, view, t, shifts):
        """The name each merged cell is showing at t seconds (and how far along the cell it has moved, when it is long)."""
        for m in self._rotors:
            phase = t % m["period"]
            i = max(k for k, st in enumerate(m["starts"]) if st <= phase)
            end = m["starts"][i + 1] if i + 1 < len(m["starts"]) else m["period"]
            name = m["names"][i]
            into = (phase - m["starts"][i]) / max(end - m["starts"][i], 0.001)
            off = 0 if not name["over"] or into < 0.2 else int(min((into - 0.2) / 0.6, 1.0) * name["over"])
            piece = name["strip"].crop((off, 0, off + m["w"], m["h"]))
            for shift in shifts:
                y = m["y"] + shift
                if y + m["h"] <= 0 or y >= self.view_h:
                    continue
                view.alpha_composite(piece, (m["x"], y)) if y >= 0 else view.alpha_composite(piece.crop((0, -y, piece.width, piece.height)), (m["x"], 0))

    # -- one frame
    def frame(self, t, now=None):
        now = now or self.now()
        key = (now.replace(second=0, microsecond=0), int(now.second // 30))
        if key != self._board_key:
            self._build_board(now)
            self._board_key = key
        W, H = self.W, self.H
        img = self.bg.copy().convert("RGBA")
        # the rows, scrolling up when there are more than fit (and again from the top, so there is no jump)
        total = self._board.height
        top = self.header_h + self.ruler_h
        view = Image.new("RGBA", (W, self.view_h), (0, 0, 0, 0))
        if self._rows == 0:
            ImageDraw.Draw(view).text((W // 2 - 160, self.view_h // 2 - 20), "No listings yet", font=self.f_title, fill=self.theme["dim"])
        elif total <= self.view_h:
            view.alpha_composite(self._board, (0, 0))
            self._draw_marquees(view, t, [0])
            self._draw_rotors(view, t, [0])
        else:
            gap = self.row_h
            offset = int(t * 30 * self.opts["scroll_speed"]) % (total + gap)
            view.alpha_composite(self._board, (0, -offset))
            view.alpha_composite(self._board, (0, total + gap - offset))
            self._draw_marquees(view, t, [-offset, total + gap - offset])
            self._draw_rotors(view, t, [-offset, total + gap - offset])
        img.alpha_composite(view, (0, top))
        # the line for the time now
        nx = self.label_w + int((now - self._t0).total_seconds() * self._pps)
        if self.label_w < nx < W:
            ImageDraw.Draw(img).line([nx, top, nx, top + self.view_h], fill=self.theme["now_line"], width=3)
        img.alpha_composite(self._ruler, (0, self.header_h))
        # the header: title on the left, the time on the right (drawn once a second)
        hdr = Image.new("RGBA", (W, self.header_h), self.theme["header"] + (255,))
        hd = ImageDraw.Draw(hdr)
        hd.text((int(W * 0.02), int(self.header_h * 0.18)), self.opts["title"], font=self.f_title, fill=self.theme["text"])
        clock = f"{now:%a %b %-d  %-I:%M:%S %p}"
        cw = int(hd.textlength(clock, font=self.f_clock))
        hd.text((W - cw - int(W * 0.02), int(self.header_h * 0.3)), clock, font=self.f_clock, fill=self.theme["dim"])
        img.alpha_composite(hdr, (0, 0))
        # the messages along the bottom
        strip, w = self.ticker
        off = int(t * 70 * self.opts["scroll_speed"]) % w
        img.alpha_composite(strip.crop((off, 0, off + W, self.ticker_h)), (0, H - self.ticker_h))
        return img.convert("RGB").tobytes()


# ---- the stream -----------------------------------------------------------------------------------------------
def ffmpeg_command(size, fps, playlist, normalize, mux):
    """ffmpeg that reads raw RGB frames on stdin plus the music (or silence) and writes the tuner's usual transport
    stream on stdout. `normalize` and `mux` are the tuner's own encode and mux options."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-framerate", str(fps), "-i", "pipe:0"]
    if playlist:
        cmd += ["-re", "-f", "concat", "-safe", "0", "-i", playlist]
    else:
        cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    return cmd + ["-map", "0:v", "-map", "1:a"] + list(normalize) + list(mux) + ["pipe:1"]


def feed(stdin, guide, fps, stop, started=time.time, log=None):
    """Write frames to ffmpeg at the real frame rate until `stop` is set or the pipe closes. A frame that cannot be drawn
    (whatever the listings held) is reported and the last good picture is shown again: the stream must never lose its video."""
    log = log or (lambda m: print(m, flush=True))
    begin = started()
    n = 0
    last, failures = None, 0
    try:
        while not stop.is_set():
            t = n / fps
            try:
                frame = guide.frame(t)
                last = frame
            except Exception as e:
                failures += 1
                if failures <= 3 or failures % 300 == 0:
                    import traceback
                    log(f"[guide channel] a frame could not be drawn ({type(e).__name__}: {e}); showing the last picture again ({failures})\n"
                        + traceback.format_exc(limit=4))
                frame = last if last is not None else bytes(guide.W * guide.H * 3)
            stdin.write(frame)
            n += 1
            wait = begin + n / fps - started()
            if wait > 0:
                time.sleep(wait)
            elif wait < -1.0:                  # far behind (a slow machine): skip ahead rather than race to catch up
                begin = started() - n / fps
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stdin.close()
        except OSError:
            pass
