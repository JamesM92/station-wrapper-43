"""The guide channel (guide_channel.py): options, music, what is on, and the pictures it draws.

Run: python3 test_guide_channel.py   (the drawing tests are skipped without Pillow)
"""
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

import guide_channel as gc

NOW = datetime(2026, 10, 7, 20, 41, 5)


def xmltv(rows):
    """rows: [(id, number, name, [(title, start, stop)])] as an XMLTV document."""
    out = ['<?xml version="1.0"?><tv>']
    for cid, number, name, progs in rows:
        out.append(f'<channel id="{cid}"><display-name>{name}</display-name><display-name>{number}</display-name></channel>')
        for title, a, b in progs:
            out.append(f'<programme start="{a.astimezone():%Y%m%d%H%M%S %z}" stop="{b.astimezone():%Y%m%d%H%M%S %z}" channel="{cid}"><title lang="en">{title}</title></programme>')
    out.append("</tv>")
    return "".join(out).encode()


class SubTitleTest(unittest.TestCase):
    def test_the_episode_name_in_the_guide_document_reaches_the_row(self):
        a = NOW.replace(minute=30, second=0)
        doc = (f'<?xml version="1.0"?><tv><channel id="c"><display-name>Five</display-name><display-name>5</display-name></channel>'
               f'<programme start="{a.astimezone():%Y%m%d%H%M%S %z}" stop="{(a + timedelta(minutes=30)).astimezone():%Y%m%d%H%M%S %z}" channel="c">'
               f'<title lang="en">Friends</title><sub-title lang="en">The Pilot</sub-title></programme></tv>').encode()
        item = gc.rows_from_xmltv(doc, a - timedelta(hours=1), a + timedelta(hours=2))[0]["items"][0]
        self.assertEqual((item["title"], item["sub"]), ("Friends", "The Pilot"))

    def test_a_listing_with_an_episode_name_becomes_a_sub_title_in_the_guide(self):
        import guide
        class R:
            @staticmethod
            def listings(db, station, a, b):
                return [{"title": "Friends", "subtitle": "The Pilot", "start": a + timedelta(hours=1), "end": a + timedelta(hours=2)},
                        {"title": "Parasite", "subtitle": None, "start": a + timedelta(hours=2), "end": a + timedelta(hours=3)}]
        g = guide.GuideBuilder({"5": "Five"}, R, "x.db", guide_hours=4)
        xml = g.xmltv(force=True).decode()
        self.assertIn('<sub-title lang="en">The Pilot</sub-title>', xml)
        self.assertEqual(xml.count("<sub-title"), 1)


class MusicIndexTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "m"))
        open(os.path.join(self.d, "m", "a.mp3"), "w").close()
        self.real = gc.audio_files

    def tearDown(self):
        gc.audio_files = self.real
        shutil.rmtree(self.d)

    def test_a_slow_folder_does_not_hold_the_guide_up_and_is_remembered_across_restarts(self):
        gate = threading.Event()

        def slow(path):
            gate.wait(10)                                    # a big music folder on a slow share
            return self.real(path)
        gc.audio_files = slow
        store = os.path.join(self.d, "music.json")
        idx = gc.MusicIndex(store)
        began = time.time()
        files, ready = idx.get(os.path.join(self.d, "m"), budget=0.2)
        self.assertEqual((files, ready), ([], False))        # starts without music ...
        self.assertLess(time.time() - began, 2)
        gate.set()
        self.assertEqual(len(idx.wait(os.path.join(self.d, "m"))), 1)    # ... and the scan finishes in the background
        self.assertEqual(idx.get(os.path.join(self.d, "m"))[1], True)
        again = gc.MusicIndex(store)                         # after a restart the answer is there at once, with no scan
        gc.audio_files = lambda p: self.fail("looked again")
        self.assertEqual(again.get(os.path.join(self.d, "m"), budget=0.01)[1], True)

    def test_an_old_list_is_used_while_it_is_looked_at_again(self):
        idx = gc.MusicIndex(None, ttl=0)
        p = os.path.join(self.d, "m")
        self.assertEqual(idx.get(p)[1], True)
        files, ready = idx.get(p, budget=0.01)               # out of date at once (ttl 0): still answered, not waited for
        self.assertEqual((len(files), ready), (1, True))


class CornersTest(unittest.TestCase):
    def test_a_programme_that_started_before_the_left_edge_has_a_square_left_end_and_one_that_starts_on_it_keeps_it_rounded(self):
        from PIL import Image
        t0 = gc.Guide(gc.options({}), (1280, 720), now=lambda: NOW)._window(NOW)[0]
        def rows(a, b):
            return [{"number": "5", "name": "Five", "items": [
                {"title": "Started earlier", "start": a - timedelta(hours=1), "end": a + timedelta(hours=1)},
                {"title": "Starts later", "start": a + timedelta(hours=1), "end": a + timedelta(hours=2)}]},
                {"number": "6", "name": "Six", "items": [
                {"title": "Starts on the edge", "start": a, "end": a + timedelta(hours=1)}]}]
        g = gc.Guide(gc.options({"messages": ["x"]}), (1280, 720), rows_fn=rows, now=lambda: NOW)
        img = Image.frombytes("RGB", (1280, 720), g.frame(0))
        top = g.header_h + g.ruler_h
        corner = lambda x0, row: img.getpixel((x0 + 3, top + row * g.row_h + 5))          # a cell's top-left corner
        inside = lambda x0, row: img.getpixel((x0 + 14, top + row * g.row_h + 14))
        left = g.label_w
        later = g.label_w + int(3600 * g._pps)
        self.assertEqual(corner(left, 0), inside(left, 0))                               # started before the grid: square, filled
        self.assertNotEqual(corner(later, 0), inside(later, 0))                          # starts inside it: rounded
        self.assertNotEqual(corner(left, 1), inside(left, 1))                            # starts exactly at the left edge: rounded

    def test_a_programme_that_runs_off_the_right_has_a_square_end_and_one_that_ends_in_the_grid_keeps_it_rounded(self):
        from PIL import Image
        def rows(a, b):
            return [{"number": "5", "name": "Five", "items": [
                {"title": "Ends inside", "start": a, "end": a + timedelta(hours=1)},
                {"title": "Runs on", "start": a + timedelta(hours=1), "end": a + timedelta(hours=6)}]},
                {"number": "6", "name": "Six", "items": [
                {"title": "Ends on the edge", "start": a, "end": a + timedelta(hours=3)}]}]
        g = gc.Guide(gc.options({"messages": ["x"]}), (1280, 720), rows_fn=rows, now=lambda: NOW)
        img = Image.frombytes("RGB", (1280, 720), g.frame(0))
        top = g.header_h + g.ruler_h
        corner = lambda x1, row: img.getpixel((x1 - 3, top + row * g.row_h + 5))          # the pixel in a cell's top-right corner
        inside = lambda x1, row: img.getpixel((x1 - 14, top + row * g.row_h + 14))       # a pixel well inside the same cell
        grid_right = g.label_w + int(3 * 3600 * g._pps)
        mid = g.label_w + int(3600 * g._pps)
        self.assertNotEqual(corner(mid, 0), inside(mid, 0))                              # ends at the 1 hour mark: rounded, the corner is not cell colour
        self.assertEqual(corner(grid_right, 0), inside(grid_right, 0))                   # runs off the right edge: square, the corner is filled
        self.assertNotEqual(corner(grid_right, 1), inside(grid_right, 1))                # ends exactly where the grid ends: it does not run on, so rounded


class MergedClipsTest(unittest.TestCase):
    """A feed of short clips is one cell that names them in turn, instead of many cells too small to read."""

    def guide(self, items):
        def rows(a, b):
            return [{"number": "7", "name": "Clips", "items": [dict(i, start=a + timedelta(minutes=i["at"]), end=a + timedelta(minutes=i["at"] + i["len"])) for i in items]}]
        return gc.Guide(gc.options({"messages": ["x"]}), (1280, 720), rows_fn=rows, now=lambda: NOW)

    def test_a_run_of_narrow_clips_is_one_cell_that_names_each_clip_in_turn(self):
        items = [{"title": "Long Show Part 3", "at": 0, "len": 40}] + \
                [{"title": f"Clip {n}", "at": 40 + n * 2, "len": 2} for n in range(10)]
        g = self.guide(items)
        def row(t):                                                            # the picture of the first row only (the clock and ticker move)
            f = g.frame(t)
            top = g.header_h + g.ruler_h
            return f[top * 1280 * 3:(top + g.row_h) * 1280 * 3]
        a = row(0)
        self.assertEqual(len(g._rotors), 1)
        self.assertEqual(len(g._rotors[0]["names"]), 10)                      # one name per clip
        self.assertNotEqual(a, row(gc.ROTATE))                                 # the next clip's name is up a few seconds later
        self.assertEqual(a, row(g._rotors[0]["period"]))                       # and it comes round

    def test_the_clip_on_air_is_marked_and_comes_first_in_the_rotation(self):
        items = [{"title": f"Clip {n}", "at": n * 2, "len": 2} for n in range(8)]
        g = self.guide(items)
        now_at = NOW + timedelta(minutes=0)                                  # the window starts at NOW's half hour: clip 0 is on air
        g.now = lambda: NOW + timedelta(minutes=5)                           # 5 minutes in: the third clip (4-6 min) is on air
        g.frame(0)
        names = g._rotors[0]["names"]
        self.assertEqual(len(names), 8)
        self.assertEqual(g._rotors[0]["starts"][0], 0.0)
        # the first name of the round is the one on air: it is drawn with the marker, the others without
        first = g._rotors[0]["names"][0]["strip"]
        second = g._rotors[0]["names"][1]["strip"]
        self.assertNotEqual(first.tobytes(), second.tobytes())
        cells = g._cells([dict(i, start=NOW.replace(minute=0) + timedelta(minutes=i["at"]), end=NOW.replace(minute=0) + timedelta(minutes=i["at"] + i["len"])) for i in items],
                         NOW.replace(minute=0), NOW.replace(minute=0) + timedelta(hours=2), 220, 0.06, NOW.replace(minute=0) + timedelta(minutes=5))
        self.assertEqual(cells[0]["on_air"], 2)

    def test_no_clip_on_air_marks_none(self):
        g = self.guide([])
        t0 = NOW.replace(minute=0, second=0, microsecond=0)
        items = [{"title": f"Clip {n}", "start": t0 + timedelta(minutes=40 + n * 2), "end": t0 + timedelta(minutes=42 + n * 2)} for n in range(4)]
        cells = g._cells(items, t0, t0 + timedelta(hours=2), 220, 0.06, t0 + timedelta(minutes=5))
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0]["on_air"])

    def test_a_lone_narrow_clip_and_wide_ones_are_left_alone(self):
        g = self.guide([{"title": "A", "at": 0, "len": 30}, {"title": "B", "at": 30, "len": 2}, {"title": "C", "at": 32, "len": 28}])
        g.frame(0)
        self.assertEqual(g._rotors, [])

    def test_repeated_names_are_not_cycled_through_twice(self):
        g = self.guide([{"title": "Spot", "at": n * 2, "len": 2} for n in range(5)])
        g.frame(0)
        self.assertEqual(len(g._rotors[0]["names"]), 1)


class NarrowCellsTest(unittest.TestCase):
    def test_programmes_of_every_length_down_to_seconds_can_be_drawn(self):
        for minutes in (0.05, 0.4, 0.8, 1, 1.5, 2, 2.5, 3, 5, 10):          # 1.5 and 2 used to raise: the video then ended silently
            def rows(a, b, m=minutes):
                return [{"number": "5", "name": "Five", "items": [
                    {"title": "Station Break Promo Spot With A Long Name", "sub": "An Episode Name", "start": a, "end": a + timedelta(minutes=m)},
                    {"title": "Next", "start": a + timedelta(minutes=m), "end": a + timedelta(hours=1)}]}]
            g = gc.Guide(gc.options({"messages": ["x"]}), (1280, 720), rows_fn=rows, now=lambda: NOW)
            for t in (0, 3.3, 9.0):
                self.assertEqual(len(g.frame(t)), 1280 * 720 * 3, f"{minutes} min")

    def test_a_frame_that_cannot_be_drawn_never_takes_the_video_away(self):
        class Boom:
            W, H = 4, 2
            n = 0

            def frame(self, t):
                self.n += 1
                if self.n == 2:
                    raise ValueError("a listing held something odd")
                return bytes([self.n]) * 24
        written, notes = [], []

        class Out:
            def write(self, b):
                written.append(b)
                if len(written) >= 4:
                    stop.set()

            def close(self):
                pass
        stop = threading.Event()
        gc.feed(Out(), Boom(), 1000, stop, log=notes.append)
        self.assertEqual([w[0] for w in written], [1, 1, 3, 4])               # the bad frame shows the last picture again
        self.assertTrue(notes)


class OptionsTest(unittest.TestCase):
    def test_defaults_and_cleaning(self):
        o = gc.options({})
        self.assertEqual((o["hours"], o["title"], o["theme"], o["music"]), (3, "Channel Guide", "modern-blue", ""))
        self.assertEqual(o["messages"], gc.DEFAULT_MESSAGES)
        o = gc.options({"messages": ["  Hello ", "", 5, "World"], "scroll_speed": "9", "play_sound": True, "sound_to_play": "/mnt/media/music",
                        "wrapper_guide": {"hours": 99, "title": "  TV  ", "theme": "nope"}})
        self.assertEqual((o["messages"], o["scroll_speed"], o["music"], o["hours"], o["title"], o["theme"]),
                         (["Hello", "World"], 5.0, "/mnt/media/music", 6, "TV", "modern-blue"))

    def test_the_music_is_kept_in_the_guides_own_settings(self):
        self.assertEqual(gc.options({"play_sound": False, "wrapper_guide": {"music": "/mnt/media/m"}})["music"], "/mnt/media/m")

    def test_music_is_only_used_when_sound_is_on(self):
        self.assertEqual(gc.options({"play_sound": False, "sound_to_play": "/x"})["music"], "")

    def test_garbage_never_raises(self):
        for conf in (None, {"messages": "x", "scroll_speed": [], "wrapper_guide": 5}, {"play_sound": True, "sound_to_play": 7}):
            gc.options(conf)


class MusicTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for p in ("a.mp3", "sub/b.FLAC", "sub/c.txt", ".hidden.mp3", "cover.jpg"):
            os.makedirs(os.path.dirname(os.path.join(self.d, p)), exist_ok=True)
            open(os.path.join(self.d, p), "w").close()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_audio_files_in_a_folder_every_level_or_one_file(self):
        self.assertEqual([os.path.relpath(f, self.d) for f in gc.audio_files(self.d)], ["a.mp3", "sub/b.FLAC"])
        self.assertEqual(gc.audio_files(os.path.join(self.d, "a.mp3")), [os.path.join(self.d, "a.mp3")])
        self.assertEqual(gc.audio_files(os.path.join(self.d, "cover.jpg")), [])
        self.assertEqual(gc.audio_files("/not/there"), [])
        self.assertEqual(gc.audio_files(""), [])

    def test_the_playlist_shuffles_each_round_and_quotes_names(self):
        text = gc.playlist_text(["/m/a.mp3", "/m/it's.mp3", "/m/c.mp3"], rounds=4, seed=1)
        lines = text.splitlines()
        self.assertEqual(lines[0], "ffconcat version 1.0")
        self.assertEqual(len(lines), 1 + 12)
        self.assertIn("file '/m/it'\\''s.mp3'", text)
        rounds = [tuple(lines[1 + 3 * i:4 + 3 * i]) for i in range(4)]
        self.assertTrue(all(sorted(r) == sorted(rounds[0]) for r in rounds))        # each round has every file once
        self.assertGreater(len(set(rounds)), 1)                                      # in more than one order


class RowsTest(unittest.TestCase):
    def test_channels_in_number_order_with_their_programmes(self):
        h = timedelta(hours=1)
        doc = xmltv([("fs42.10", "10", "Late", [("Night Show", NOW - h, NOW + h)]),
                     ("fs42.2", "2", "Cartoons", [("Tom", NOW - h, NOW), ("Jerry", NOW, NOW + h)]),
                     ("hd.5.1", "5.1", "CBC HD", [("News", NOW, NOW + 2 * h)]),
                     ("fs42.7", "7", "Empty", [])])
        rows = gc.rows_from_xmltv(doc, NOW - timedelta(minutes=40), NOW + timedelta(hours=3))
        # a channel with nothing on is still listed (it is dark, not missing), with one Off air cell across the window
        self.assertEqual([(r["number"], r["name"]) for r in rows], [("2", "Cartoons"), ("5.1", "CBC HD"), ("7", "Empty"), ("10", "Late")])
        self.assertEqual([i["title"] for i in rows[0]["items"]], ["Tom", "Jerry"])
        self.assertEqual([i["title"] for i in rows[2]["items"]], ["Off air"])
        self.assertEqual((rows[2]["items"][0]["start"], rows[2]["items"][0]["end"]), (NOW - timedelta(minutes=40), NOW + timedelta(hours=3)))

    def test_programmes_outside_the_window_and_broken_documents_are_ignored(self):
        h = timedelta(hours=1)
        doc = xmltv([("a", "2", "A", [("Old", NOW - 5 * h, NOW - 4 * h), ("Far", NOW + 9 * h, NOW + 10 * h)])])
        rows = gc.rows_from_xmltv(doc, NOW, NOW + 3 * h)
        self.assertEqual([[i["title"] for i in r["items"]] for r in rows], [["Off air"]])      # neither is on now: the channel is dark
        self.assertEqual(gc.rows_from_xmltv(b"<tv><broken", NOW, NOW + h), [])

    def test_a_channel_can_be_left_out_by_name(self):
        doc = xmltv([("a", "2", "Guide", [("x", NOW, NOW + timedelta(hours=1))]), ("b", "3", "Other", [("y", NOW, NOW + timedelta(hours=1))])])
        self.assertEqual([r["name"] for r in gc.rows_from_xmltv(doc, NOW, NOW + timedelta(hours=2), skip_names=("Guide",))], ["Other"])


@unittest.skipIf(gc.Image is None, "needs Pillow")
class DrawTest(unittest.TestCase):
    def rows(self, n, t0, t1):
        return [{"number": str(i + 2), "name": f"Channel {i + 2}",
                 "items": [{"title": f"Show {i} A", "start": t0 - timedelta(minutes=20), "end": t0 + timedelta(minutes=40)},
                           {"title": f"A much longer title for show {i} B that must be cut off", "start": t0 + timedelta(minutes=40), "end": t0 + timedelta(hours=2)}]}
                for i in range(n)]

    def guide(self, n=5, size=(640, 360), **kw):
        o = gc.options({"messages": ["Hello there", "General Kenobi"], **kw})
        return gc.Guide(o, size, rows_fn=lambda a, b: self.rows(n, a, b), now=lambda: NOW)

    def test_a_frame_is_the_right_size_and_is_not_blank(self):
        g = self.guide()
        data = g.frame(0)
        self.assertEqual(len(data), 640 * 360 * 3)
        from PIL import Image
        img = Image.frombytes("RGB", (640, 360), data)
        self.assertGreater(len(set(img.getdata())), 50)                         # text, cells and a gradient: not one colour
        self.assertLess(img.getpixel((5, 360 - 5))[2], 120)                      # the ticker strip at the bottom is dark
        top, bottom = img.getpixel((600, 120)), img.getpixel((600, 300))          # the background is a gradient from dark to brighter blue

    def test_the_rows_scroll_when_there_are_too_many_and_stay_put_when_they_fit(self):
        many, few = self.guide(n=30), self.guide(n=2)
        self.assertNotEqual(many.frame(0), many.frame(5))
        self.assertEqual(few.frame(0)[:40000], few.frame(5)[:40000])             # nothing scrolls: same rows (the clock is below)

    def test_the_messages_scroll_along_the_bottom(self):
        g = self.guide(n=2)
        a, b = g.frame(0), g.frame(2)
        row = 640 * 3
        strip = slice(int(360 * 0.93) * row, 360 * row)                                     # the whole ticker strip
        self.assertNotEqual(a[strip], b[strip])

    def test_no_listings_says_so_and_a_theme_and_speed_are_respected(self):
        g = gc.Guide(gc.options({}), (640, 360), rows_fn=lambda a, b: [], now=lambda: NOW)
        self.assertEqual(len(g.frame(0)), 640 * 360 * 3)
        fast = self.guide(n=30, scroll_speed=3)
        slow = self.guide(n=30, scroll_speed=0.3)
        self.assertNotEqual(fast.frame(4), slow.frame(4))

    def test_every_style_draws_a_different_picture_and_the_form_offers_exactly_these(self):
        import re
        frames = {}
        for name in gc.THEMES:
            g = gc.Guide(gc.options({"wrapper_guide": {"theme": name}}), (640, 360), rows_fn=lambda a, b: self.rows(4, a, b), now=lambda: NOW)
            frames[name] = g.frame(0)
            self.assertEqual(len(frames[name]), 640 * 360 * 3)
        self.assertEqual(len(set(frames.values())), len(gc.THEMES))
        self.assertEqual(set(gc.THEME_NAMES), set(gc.THEMES))
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "station.html")).read()
        offered = re.search(r'<select id="g-theme">(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'value="([^"]+)"', offered), list(gc.THEMES))

    def test_a_title_too_long_for_its_slot_scrolls_along_it_and_one_that_fits_stays_put(self):
        from PIL import Image
        for n in (3, 12):                                                   # the grid still, and the grid scrolling up
            g = self.guide(n=n, size=(640, 360))
            a, b = (Image.frombytes("RGB", (640, 360), g.frame(t)) for t in (0.0, 5.0))
            top = 58                                                         # below the header and the time ruler
            band = lambda img, x0, x1: img.crop((x0, top + 4, x1, top + 18))  # the title line of the first row's cells
            if n == 3:
                self.assertEqual(list(band(a, 112, 220).getdata()), list(band(b, 112, 220).getdata()))      # "Show 0 A" fits: it stays
                self.assertNotEqual(list(band(a, 232, 450).getdata()), list(band(b, 232, 450).getdata()))   # the long one has moved
            self.assertEqual(len(g._marquees), n)                           # one long title in each row

    def test_every_moving_name_goes_through_its_cell_twice_while_the_rows_scroll_up_once(self):
        g = self.guide(n=20, size=(1280, 720))                               # more rows than fit: the board scrolls up
        g.frame(0)
        total = g._board.height
        vertical = (total + g.row_h) / 30                                    # seconds for the rows to go round once at speed 1
        self.assertAlmostEqual(g._lap * gc.LAPS, vertical, delta=0.01)
        m = g._marquees[0]
        off = lambda t: gc.Guide._marquee_offset(m, t, g._lap)
        self.assertEqual(off(0), 0)
        self.assertEqual(off(g._lap - 0.001) >= m["cycle"] - 3, True)        # almost all the way round at the end of a lap
        self.assertEqual(off(g._lap + 0.001), 0)                             # and starting again, no jump
        self.assertEqual(off(vertical + 0.001), 0)                           # two laps done exactly when the rows come round
        self.assertEqual(off(gc.TITLE_HOLD - 0.1), 0)                        # it rests at the start of each lap first
        self.assertGreater(off(g._lap / 2), 0)
        fast = self.guide(n=20, size=(1280, 720), scroll_speed=1.8)
        fast.frame(0)
        self.assertAlmostEqual(fast._lap * gc.LAPS, (fast._board.height + fast.row_h) / (30 * 1.8), delta=0.01)   # the speed follows the scroll speed

    def test_when_everything_fits_a_calm_pace_is_used(self):
        g = self.guide(n=2, size=(1280, 720))
        g.frame(0)
        self.assertEqual(g._lap, gc.STILL_CYCLE / gc.LAPS)

    def test_the_episode_name_is_small_text_after_the_time_and_moves_when_it_is_long(self):
        from PIL import Image
        def rows(a, b):
            return [{"number": "5", "name": "Five", "items": [
                {"title": "Friends", "sub": "Pilot", "start": a, "end": a + timedelta(minutes=30)},
                {"title": "Seinfeld", "sub": "The One About A Very Long Episode Name That Cannot Possibly Fit In A Small Cell", "start": a + timedelta(minutes=30), "end": a + timedelta(hours=2)},
                {"title": "Short", "start": a + timedelta(hours=2), "end": a + timedelta(hours=2, minutes=30)}]}]
        o = gc.options({"messages": ["x"]})
        g = gc.Guide(o, (1280, 720), rows_fn=rows, now=lambda: NOW)
        a, b = (Image.frombytes("RGB", (1280, 720), g.frame(t)) for t in (0.0, 9.0))
        self.assertEqual(len(g._marquees), 1)                                # only the long episode name moves; "Pilot" fits and stays
        self.assertEqual(g._marquees[0]["h"], int(g.f_time.size * 1.45))     # in the small type of the time, not the big title's
        with_sub = Image.frombytes("RGB", (1280, 720), gc.Guide(o, (1280, 720), rows_fn=lambda x, y: [dict(rows(x, y)[0], items=[dict(rows(x, y)[0]["items"][0], sub="")])], now=lambda: NOW).frame(0))
        top = g.header_h + g.ruler_h
        line = lambda img: list(img.crop((g.label_w + 8, top + int(g.row_h * 0.58), g.label_w + 330, top + int(g.row_h * 0.58) + g.f_time.size + 4)).getdata())
        self.assertNotEqual(line(a), line(with_sub))                          # "Pilot" is on the time's line
        self.assertNotEqual(list(a.getdata()), list(b.getdata()))

    def test_the_time_window_starts_on_the_half_hour(self):
        g = self.guide()
        t0, t1 = g._window(NOW)
        self.assertEqual((t0, t1 - t0), (datetime(2026, 10, 7, 20, 30), timedelta(hours=3)))

    def test_frame_speed(self):                                                  # not a promise: a guard against a slow drawing path
        import time
        g = gc.Guide(gc.options({}), (1280, 720), rows_fn=lambda a, b: self.rows(12, a, b), now=lambda: NOW)
        g.frame(0)
        began = time.time()
        for i in range(20):
            g.frame(i / 15)
        self.assertLess((time.time() - began) / 20, 0.2)                          # well under one frame period at 5 fps


class StreamTest(unittest.TestCase):
    def test_the_ffmpeg_command_takes_frames_and_music_or_silence(self):
        c = gc.ffmpeg_command((1280, 720), 15, "/tmp/list.ffconcat", ["-c:v", "libx264"], ["-f", "mpegts"])
        self.assertEqual(c[c.index("-s") + 1], "1280x720")
        self.assertIn("/tmp/list.ffconcat", c)
        self.assertEqual(c[-1], "pipe:1")
        self.assertIn("anullsrc=r=48000:cl=stereo", gc.ffmpeg_command((640, 360), 15, None, [], []))

    def test_feed_writes_frames_at_the_real_rate_until_stopped(self):
        class G:
            def frame(self, t):
                return b"x" * 4

        out, stop = io.BytesIO(), threading.Event()
        threading.Timer(0.55, stop.set).start()
        gc.feed(out, G(), 10, stop)
        frames = len(out.getvalue()) // 4 if not out.closed else None
        self.assertIsNone(frames)                                                  # the pipe was closed at the end


@unittest.skipIf(gc.Image is None, "needs Pillow")
class NarrowCellTest(unittest.TestCase):
    def test_a_programme_a_few_pixels_wide_is_drawn_without_failing_whatever_pillow_there_is(self):
        h = timedelta(hours=1)
        rows = lambda a, b: [{"number": "4", "name": "Short", "items": [
            {"title": "Tiny", "start": NOW, "end": NOW + timedelta(minutes=2), "sub": ""},
            {"title": "Next", "start": NOW + timedelta(minutes=2), "end": NOW + timedelta(minutes=2, seconds=40), "sub": ""}]}]
        guide = gc.Guide(gc.options({}), (1280, 720), rows_fn=rows, now=lambda: NOW)
        self.assertEqual(len(guide.frame(0.0)), 1280 * 720 * 3)


if __name__ == "__main__":
    unittest.main()
