"""Tests for stations.py. Run: python3 test_stations.py"""
import json
import os
import shutil
import tempfile
import unittest

import stations


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def put(self, name, data):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_reads_the_name_and_number_of_each_station(self):
        self.put("comedy.json", {"station_conf": {"network_name": "Comedy", "channel_number": 801}})
        self.put("news.json", {"station_conf": {"network_name": "NBC News", "channel_number": "802"}})
        self.assertEqual(stations.discover(self.dir), {"801": "Comedy", "802": "NBC News"})

    def test_things_that_are_not_stations_are_skipped(self):
        self.put("main_config.json", {"main_config": {"x": 1}})
        self.put("broken.json", "{not json")
        self.put("nonumber.json", {"station_conf": {"network_name": "NoNumber"}})
        self.put("noname.json", {"station_conf": {"channel_number": 5}})
        self.put("list.json", [1, 2])
        self.put("flag.json", {"station_conf": {"network_name": "X", "channel_number": True}})
        self.put("ok.json", {"station_conf": {"network_name": "Ok", "channel_number": 3}})
        self.assertEqual(stations.discover(self.dir), {"3": "Ok"})

    def test_a_missing_folder_is_no_stations(self):
        self.assertEqual(stations.discover(os.path.join(self.dir, "nope")), {})


class BusyMarkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "busy")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_marked_cleared_and_stale(self):
        self.assertFalse(stations.is_busy(self.path))
        stations.mark_busy(self.path)
        self.assertTrue(stations.is_busy(self.path))
        stations.clear_busy(self.path)
        self.assertFalse(stations.is_busy(self.path))
        stations.clear_busy(self.path)                                   # clearing twice is fine

    def test_a_mark_nobody_refreshes_goes_stale(self):
        stations.mark_busy(self.path)
        later = lambda: os.stat(self.path).st_mtime + stations.BUSY_MAX_AGE + 5
        self.assertFalse(stations.is_busy(self.path, now=later))        # a crashed dashboard must not block restarts forever
        self.assertTrue(stations.is_busy(self.path, now=lambda: os.stat(self.path).st_mtime + 5))

    def test_a_folder_that_is_not_there_is_just_not_busy(self):
        stations.mark_busy(os.path.join(self.dir, "nope", "busy"))
        self.assertFalse(stations.is_busy(os.path.join(self.dir, "nope", "busy")))


if __name__ == "__main__":
    unittest.main()
