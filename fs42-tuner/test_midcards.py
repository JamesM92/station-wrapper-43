"""Generated mid cards: the names, the break folder that links a chosen list and a channel's own clips, and a real (short) draw.

Run: python3 test_midcards.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import midcards as mc
import stationform as sf


class Names(unittest.TestCase):
    def test_each_exact_name_has_its_own_key_and_it_is_safe(self):
        self.assertNotEqual(mc.key("Cartoons"), mc.key("cartoons"))
        k = mc.key("../../etc/Cartoons: Late / Night")
        self.assertNotIn("/", k)
        self.assertNotIn("..", k)
        self.assertTrue(mc.folder_name("Cartoons").startswith(".cards.gen."))


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for n in ("Bumpers", "Welcome"):
            os.makedirs(os.path.join(self.root, n))
            open(os.path.join(self.root, n, "a.mp4"), "w").close()
        os.makedirs(os.path.join(self.root, mc.GEN_DIR))
        for kind in ("start", "end"):
            open(os.path.join(self.root, mc.GEN_DIR, mc.clip_name("Toons", kind)), "w").close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_each_side_links_the_chosen_list_and_the_channels_own_clip_and_changes_neither(self):
        mc.link(self.root, "Toons", "Bumpers", "Welcome")
        pre = os.path.join(self.root, mc.folder_name("Toons"), "pre")
        post = os.path.join(self.root, mc.folder_name("Toons"), "post")
        self.assertEqual(sorted(os.listdir(pre)), sorted(["list", mc.clip_name("Toons", "start")]))
        self.assertEqual(os.path.realpath(os.path.join(pre, "list")), os.path.realpath(os.path.join(self.root, "Bumpers")))
        self.assertTrue(os.path.exists(os.path.join(post, mc.clip_name("Toons", "end"))))
        self.assertEqual(os.listdir(os.path.join(self.root, "Bumpers")), ["a.mp4"])          # the list itself has not been added to
        self.assertEqual(os.listdir(os.path.join(self.root, "Welcome")), ["a.mp4"])

    def test_a_list_that_is_not_there_is_left_out_and_the_clip_still_plays(self):
        mc.link(self.root, "Toons", "Nope", "Nope")
        self.assertEqual(os.listdir(os.path.join(self.root, mc.folder_name("Toons"), "pre")), [mc.clip_name("Toons", "start")])

    def test_changing_the_list_swaps_the_link(self):
        mc.link(self.root, "Toons", "Bumpers", "Bumpers")
        mc.link(self.root, "Toons", "Welcome", "Welcome")
        link = os.path.join(self.root, mc.folder_name("Toons"), "pre", "list")
        self.assertEqual(os.path.basename(os.path.realpath(link)), "Welcome")

    def test_forgetting_a_channel_takes_only_its_own_files_and_links(self):
        mc.link(self.root, "Toons", "Bumpers", "Welcome")
        mc.forget(self.root, "Toons")
        self.assertFalse(os.path.exists(os.path.join(self.root, mc.folder_name("Toons"))))
        self.assertFalse(os.path.exists(os.path.join(self.root, mc.GEN_DIR, mc.clip_name("Toons", "start"))))
        self.assertEqual(os.listdir(os.path.join(self.root, "Bumpers")), ["a.mp4"])
        self.assertEqual(os.listdir(os.path.join(self.root, "Welcome")), ["a.mp4"])


class Form(unittest.TestCase):
    def form(self, **kw):
        f = {"name": "Toons", "number": 12, "kind": "standard", "blocks": [{"from": 0, "to": 24, "lists": ["Cartoons"]}],
             "increment": 30, "bump": "Bumpers", "bump_end": "Welcome", "commercials": "", "midcards": True, "gen_cards": True}
        f.update(kw)
        return f

    def test_the_option_makes_the_stations_own_break_folder_and_keeps_the_lists_it_was_given(self):
        form = sf.validate(self.form(), [], {}, None)
        conf = sf.to_conf(form)
        self.assertEqual(conf["bump_dir"], mc.folder_name("Toons"))
        self.assertEqual((conf["wrapper_start_cards"], conf["wrapper_end_cards"]), ("Bumpers", "Welcome"))
        self.assertTrue(conf["wrapper_gen_cards"])
        back, why = sf.parse(conf)
        self.assertTrue(back["gen_cards"])
        self.assertEqual((back["bump"], back["bump_end"]), ("Bumpers", "Welcome"))

    def test_without_default_mid_cards_the_option_does_nothing(self):
        form = sf.validate(self.form(midcards=False), [], {}, None)
        self.assertFalse(form["gen_cards"])
        self.assertNotIn("wrapper_gen_cards", sf.to_conf(form))

    def test_the_style_is_one_of_the_guides_looks_and_unknown_falls_back(self):
        self.assertEqual(sf.validate(self.form(gen_style="midnight"), [], {}, None)["gen_style"], "midnight")
        self.assertEqual(sf.validate(self.form(gen_style="neon"), [], {}, None)["gen_style"], "modern-blue")
        conf = sf.to_conf(sf.validate(self.form(gen_style="emerald"), [], {}, None))
        self.assertEqual(conf["wrapper_gen_style"], "emerald")
        self.assertNotIn("wrapper_gen_style", sf.to_conf(sf.validate(self.form(), [], {}, None)))      # the default is not written

    def test_off_by_default(self):
        f = self.form()
        f.pop("gen_cards")
        self.assertFalse(sf.validate(f, [], {}, None)["gen_cards"])


class Draw(unittest.TestCase):
    """A real draw (shortened: a second at a few frames a second) through Pillow and ffmpeg."""

    @unittest.skipUnless(shutil.which("ffmpeg") and mc.Image, "needs ffmpeg and Pillow")
    def test_two_clips_are_drawn_and_remembered(self):
        root = tempfile.mkdtemp()
        saved = (mc.FPS, mc.SECONDS, mc.SLICES)
        mc.FPS, mc.SECONDS, mc.SLICES = 6, 1, 3
        try:
            self.assertFalse(mc.current(root, "Toons"))
            mc.draw(root, "Toons", log=lambda *_: None)
            self.assertTrue(mc.current(root, "Toons"))
            for kind in ("start", "end"):
                path = os.path.join(root, mc.GEN_DIR, mc.clip_name("Toons", kind))
                out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                                     capture_output=True, text=True).stdout.split()
                self.assertIn("1280,720", out)
            self.assertFalse(mc.current(root, "Toons", "sunset"))               # another style is drawn again
            self.assertTrue(mc.current(root, "Toons", "modern-blue"))
            self.assertFalse(mc.current(root, "Toons 2"))                       # another name is another clip
            stale = mc.VERSION
            mc.VERSION = stale + 1
            self.assertFalse(mc.current(root, "Toons"))                         # an older design is drawn again
            mc.VERSION = stale
        finally:
            mc.FPS, mc.SECONDS, mc.SLICES = saved
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
