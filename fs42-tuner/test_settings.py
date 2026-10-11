"""Tests for settings.py. Run: python3 test_settings.py"""
import json
import os
import shutil
import tempfile
import unittest

import settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def ta(self, env=None):
        return settings.get_ta(env or {}, self.path)

    def test_defaults_when_nothing_is_set(self):
        t = self.ta()
        self.assertEqual((t["enabled"], t["library"], t["interval_minutes"]), (False, "/mnt/media/tubearchivist", 1440))
        self.assertEqual(set(t["source"].values()), {"default"})

    def test_the_old_environment_variables_are_the_starting_values(self):
        t = self.ta({"TA_ENABLED": "1", "TA_LIBRARY": "/mnt/media/youtube", "TA_INTERVAL_MINUTES": "60"})
        self.assertEqual((t["enabled"], t["library"], t["interval_minutes"]), (True, "/mnt/media/youtube", 60))
        self.assertEqual({k: t["source"][k] for k in ("enabled", "libraries", "interval_minutes")}, {k: "env" for k in ("enabled", "libraries", "interval_minutes")})
        self.assertFalse(self.ta({"TA_ENABLED": "0"})["enabled"])
        self.assertEqual(self.ta({"TA_INTERVAL_MINUTES": "junk"})["interval_minutes"], 1440)

    def test_a_saved_setting_beats_the_environment_one_key_at_a_time(self):
        env = {"TA_ENABLED": "1", "TA_LIBRARY": "/mnt/media/old"}
        settings.set_ta({"library": "/mnt/media/youtube"}, self.path)
        t = self.ta(env)
        self.assertEqual((t["enabled"], t["library"]), (True, "/mnt/media/youtube"))
        self.assertEqual((t["source"]["enabled"], t["source"]["libraries"]), ("env", "saved"))
        settings.set_ta({"enabled": False}, self.path)
        self.assertFalse(self.ta(env)["enabled"])               # the dashboard's off beats TA_ENABLED=1

    def test_several_libraries_each_get_a_name_with_nfo_added(self):
        settings.set_ta({"libraries": ["/mnt/media/youtube", "/mnt/media/more/YT Archive", "/mnt/media/other/youtube/"]}, self.path)
        t = self.ta()
        self.assertEqual([v["name"] for v in t["views"]], ["youtube-nfo", "YT Archive-nfo", "youtube-nfo-2"])
        self.assertEqual(t["library"], "/mnt/media/youtube")
        self.assertEqual(settings.get_ta({"TA_LIBRARY": "/a/one, /b/two"}, self.path + ".none")["libraries"], ["/a/one", "/b/two"])

    def test_a_folder_saved_by_an_older_version_survives_other_changes(self):
        with open(self.path, "w") as f:
            json.dump({"ta": {"library": "/mnt/media/youtube"}}, f)
        self.assertEqual(self.ta()["libraries"], ["/mnt/media/youtube"])
        settings.set_ta({"enabled": True}, self.path)
        self.assertEqual(json.load(open(self.path))["ta"]["libraries"], ["/mnt/media/youtube"])
        self.assertNotIn("library", json.load(open(self.path))["ta"])

    def test_bad_library_lists_are_refused(self):
        for bad in ([], ["relative"], [5], "/mnt/media/x", ["/a"] * 0, [f"/m/{i}" for i in range(13)]):
            with self.assertRaises(settings.SettingsError, msg=str(bad)):
                settings.set_ta({"libraries": bad}, self.path)

    def test_saving_validates(self):
        for bad in ({"enabled": "yes"}, {"enabled": 1}, {"library": 5}, {"library": "relative/path"},
                    {"interval_minutes": 5}, {"interval_minutes": 99999}, {"interval_minutes": "60"},
                    {"interval_minutes": True}):
            with self.assertRaises(settings.SettingsError, msg=str(bad)):
                settings.set_ta(bad, self.path)
        self.assertFalse(os.path.exists(self.path))             # nothing half-saved

    def test_other_settings_in_the_file_are_kept(self):
        with open(self.path, "w") as f:
            json.dump({"something": {"else": 1}}, f)
        settings.set_ta({"enabled": True}, self.path)
        self.assertEqual(json.load(open(self.path))["something"], {"else": 1})

    def test_a_broken_or_odd_file_never_raises(self):
        for text in ("{not json", "[1,2]", '{"ta": 5}', '{"ta": {"enabled": "maybe", "interval_minutes": 3}}'):
            with open(self.path, "w") as f:
                f.write(text)
            t = self.ta()
            self.assertEqual((t["enabled"], t["interval_minutes"]), (False, 1440), text)

    def test_the_file_is_readable_and_editable_from_the_host(self):
        settings.set_ta({"enabled": True}, self.path)
        self.assertEqual(os.stat(self.path).st_mode & 0o666, 0o666)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".settings-")], [])   # no temp left


class CaptionLanguage(unittest.TestCase):
    def test_english_by_default_and_a_language_is_kept(self):
        path = os.path.join(tempfile.mkdtemp(), "settings.json")
        self.assertEqual(settings.get_captions(path), {"language": "en"})
        self.assertEqual(settings.set_captions({"language": " JA "}, path), {"language": "ja"})
        self.assertEqual(settings.get_captions(path), {"language": "ja"})
        for bad in ("english", "", None, 5):
            with self.assertRaises(settings.SettingsError):
                settings.set_captions({"language": bad}, path)


if __name__ == "__main__":
    unittest.main()
