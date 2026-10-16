"""The week calendar's mouse behaviour (static/calendar.js), driven in a real headless browser: a slot dragged sideways is put on the
next day as one linked slot, its settings and edges are changed on every day at once, Alt moves it instead, and drawing across days makes
one linked slot. Skipped when no Chrome or Chromium is installed.

Run: python3 test_calendar_ui.py
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


def browser():
    """A Chrome that can open a local file headless: Playwright's own build, else Chrome or Chromium on the path (a snap
    Chromium cannot read files outside its sandbox, so it comes last)."""
    for pattern in ("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell", "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"):
        hit = sorted(glob.glob(os.path.expanduser(pattern)))
        if hit:
            return hit[-1]
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


PAGE = """<!doctype html><meta charset=utf-8>
<link rel=stylesheet href="file://%(static)s/calendar.css">
<body style="width:1100px"><div id=cal></div><pre id=out></pre>
<script src="file://%(static)s/calendar.js"></script>
<script>
const days = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"];
const data = Object.fromEntries(days.map(d => [d, {videos: [{from:0,to:24,lists:["A"]}], commercials: [], cards: []}]));
data.monday.videos = [{from:0,to:15,lists:["A"]},{from:15,to:16,lists:["Movies"],premiere:true},{from:16,to:24,lists:["A"]}];
const cal = Calendar.create({host: document.getElementById("cal"), columns: days.map(k => ({key:k,label:k})),
  getDay: k => data[k], layer: () => "videos", full: () => true, defaults: () => ({lists: ["New"]}), onChange: () => {}, onSelect: () => {}});
cal.render();
const out = {};
const col = k => document.querySelector(`.cal-col[data-key="${k}"]`);
const find = (k, name) => [...col(k).querySelectorAll(".cal-block")].find(b => b.textContent.includes(name));
function ev(type, target, x, y, extra) { target.dispatchEvent(new PointerEvent(type, {bubbles:true, clientX:x, clientY:y, button:0, pointerId:1, ...(extra||{})})); }
const pt = (k, hour) => { const r = col(k).getBoundingClientRect(); return [r.left + r.width/2, r.top + hour*2*Calendar.ROW + 1]; };
const row = (k) => data[k].videos.map(b => [b.from, b.to, b.lists[0], b.span ? 1 : 0, b.premiere ? 1 : 0]);
try {
  // sideways onto Wednesday at the same time
  let b = find("monday", "Movies"), r = b.getBoundingClientRect();
  ev("pointerdown", b, r.left + 20, r.top + 18);
  let [x, y] = pt("tuesday", 15.5); ev("pointermove", window, x, y);
  [x, y] = pt("wednesday", 15.5); ev("pointermove", window, x, y);
  out.ghosts = document.querySelectorAll(".cal-block.drawing").length;
  ev("pointerup", window, x, y);
  out.mon = row("monday"); out.tue = row("tuesday"); out.wed = row("wednesday"); out.thu = row("thursday");
  out.bounds = cal.selectedSpan();
  cal.update(v => { v.lists = ["Drama"]; });
  out.updated = ["monday","tuesday","wednesday"].map(k => data[k].videos.find(v => v.span).lists[0]);
  b = find("tuesday", "Drama"); let h = b.querySelector(".cal-handle.to"); r = h.getBoundingClientRect();
  ev("pointerdown", h, r.left + 5, r.top + 3); [x, y] = pt("tuesday", 17); ev("pointermove", window, x, y); ev("pointerup", window, x, y);
  out.resizedMon = row("monday"); out.resizedWed = row("wednesday");
  // Alt moves a plain slot instead
  data.friday.videos = [{from:0,to:10,lists:["A"]},{from:10,to:11,lists:["Solo"]},{from:11,to:24,lists:["A"]}]; cal.render();
  b = find("friday", "Solo"); r = b.getBoundingClientRect();
  ev("pointerdown", b, r.left + 20, r.top + 18); [x, y] = pt("saturday", 10.5); ev("pointermove", window, x, y, {altKey:true}); ev("pointerup", window, x, y);
  out.fri = row("friday"); out.sat = row("saturday");
  // drawing across days on empty space: the same hours on each
  data.tuesday.videos = [{from:0,to:5,lists:["A"]}]; data.wednesday.videos = [{from:0,to:5,lists:["A"]}]; data.thursday.videos = [{from:0,to:5,lists:["A"]}];
  cal.render();
  [x, y] = pt("tuesday", 8); let [x2, y2] = pt("thursday", 9.75);
  ev("pointerdown", col("tuesday"), x, y); ev("pointermove", window, x, y + 10); ev("pointermove", window, x2, y2); ev("pointerup", window, x2, y2);
  out.drawn = ["tuesday","wednesday","thursday"].map(row);
  cal.removeOne();
  out.afterRemoveOne = ["tuesday","wednesday","thursday"].map(row);
} catch (e) { out.error = String(e.stack || e); }
document.getElementById("out").textContent = JSON.stringify(out);
</script>"""


@unittest.skipUnless(browser(), "needs Chrome or Chromium")
class CalendarInABrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        page = os.path.join(tmp, "page.html")
        with open(page, "w") as f:
            f.write(PAGE % {"static": STATIC})
        out = subprocess.run([browser(), "--headless=new" if "headless-shell" not in browser() else "--headless", "--no-sandbox", "--disable-gpu",
                              "--allow-file-access-from-files", "--virtual-time-budget=4000", "--dump-dom", f"file://{page}"],
                             capture_output=True, text=True, timeout=120).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', out, re.S)
        if not m:
            raise unittest.SkipTest("this browser could not open the test page")
        cls.r = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        shutil.rmtree(tmp, ignore_errors=True)

    def test_there_was_no_script_error(self):
        self.assertNotIn("error", self.r)

    def test_a_slot_dragged_sideways_is_on_the_next_days_at_the_same_time_as_one_slot(self):
        r = self.r
        self.assertEqual(r["ghosts"], 3)                                          # a ghost on each of the three days while dragging
        piece = [15, 16, "Movies", 1]
        self.assertEqual(r["mon"][1][:4], piece)
        self.assertEqual(r["tue"][1][:4], piece)
        self.assertEqual(r["wed"][1][:4], piece)
        self.assertEqual(r["thu"], [[0, 24, "A", 0, 0]])                          # no further
        self.assertEqual(r["mon"][1][4], 1)                                       # the premiere mark stays on the day it was set on
        self.assertEqual(r["tue"][1][4], 0)
        self.assertEqual(r["bounds"]["keys"], ["monday", "tuesday", "wednesday"])

    def test_a_setting_and_an_edge_change_every_day_of_it(self):
        self.assertEqual(self.r["updated"], ["Drama", "Drama", "Drama"])
        self.assertEqual(self.r["resizedMon"][1][:2], [15, 17])
        self.assertEqual(self.r["resizedWed"][1][:2], [15, 17])

    def test_alt_moves_a_slot_instead_of_putting_it_on_the_next_day(self):
        self.assertEqual(self.r["fri"], [[0, 24, "A", 0, 0]])                    # gone from Friday (its hole closed)
        self.assertEqual(self.r["sat"][1][:3], [10, 11, "Solo"])

    def test_drawing_across_days_is_one_slot_with_the_same_hours_and_one_day_can_be_removed(self):
        for day in self.r["drawn"]:
            self.assertEqual(day[-1][:2], [8, 10])
            self.assertEqual(day[-1][3], 1)
        tue, wed, thu = self.r["afterRemoveOne"]
        self.assertTrue(all(b[3] == 0 for b in tue))                              # taken out of Tuesday
        self.assertEqual(wed[-1][3], 1)
        self.assertEqual(thu[-1][3], 1)


if __name__ == "__main__":
    unittest.main()
