"""Tests for the pool scanner and the catalog watcher.

Run: python3 test_sync.py   (from this folder)
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime

import pool
import sync


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def time_far_ahead():
    """A 'now' long after everything the test touched."""
    import time
    return time.time_ns() + 10**15


def age(root, seconds=3600):
    """Backdate every folder under root, so the scanner treats them as settled, not freshly changed."""
    old = (os.stat(root).st_mtime_ns - seconds * 10**9)
    for r, dirs, _files in os.walk(root, topdown=False):
        os.utime(r, ns=(old, old))


def listing(root):
    return sorted(os.path.relpath(os.path.join(r, f), root) for r, _d, fs in os.walk(root) for f in fs)


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        self.pool = os.path.join(self.root, "pool")
        for f in ("tvseries/Show A/Season 1/e1.mp4", "tvseries/Show A/Season 1/e1.nfo",
                  "tvseries/Show A/tvshow.nfo", "movies/Film/Film.mkv", "bump/b.mp4"):
            touch(os.path.join(self.media, f))
        touch(os.path.join(self.media, "tvseries/Show A/poster.jpg"))        # not media
        touch(os.path.join(self.media, "tvseries/Show A/notes.txt"))
        touch(os.path.join(self.media, "tvseries/@eaDir/junk.mp4"))          # NAS junk
        touch(os.path.join(self.media, "tvseries/.hidden/h.mp4"))
        touch(os.path.join(self.media, "loose.mp4"))                         # straight in the share's top level
        age(self.media)                                                      # an established share, not one made a moment ago
        self.state = pool.new_state()

    def tearDown(self):
        shutil.rmtree(self.root)

    def scan(self, **kw):
        return pool.scan_pool(self.media, self.pool, self.state, **kw)

    def test_each_video_and_nfo_is_linked_by_the_path_this_process_sees(self):
        stats = self.scan()
        self.assertEqual(listing(self.pool), [
            "bump/b.mp4", "movies/Film/Film.mkv", "tvseries/Show A/Season 1/e1.mp4",
            "tvseries/Show A/Season 1/e1.nfo", "tvseries/Show A/tvshow.nfo"])
        link = os.path.join(self.pool, "tvseries/Show A/Season 1/e1.mp4")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), f"{self.media}/tvseries/Show A/Season 1/e1.mp4")
        self.assertEqual(stats["links_added"], 5)

    def test_images_text_junk_hidden_and_top_level_files_are_left_out(self):
        self.scan()
        names = " ".join(listing(self.pool))
        for left_out in ("poster.jpg", "notes.txt", "@eaDir", ".hidden", "loose.mp4"):
            self.assertNotIn(left_out, names)

    def test_a_second_scan_lists_nothing_and_adds_nothing(self):
        self.scan()
        again = self.scan()
        self.assertEqual((again["links_added"], again["dirs_listed"]), (0, 0))
        self.assertGreater(again["dirs_cached"], 0)

    def test_a_new_file_is_found_by_listing_only_the_folder_it_is_in(self):
        self.scan()
        touch(os.path.join(self.media, "tvseries/Show A/Season 1/e2.mp4"))
        stats = self.scan()
        self.assertEqual(stats["links_added"], 1)
        self.assertEqual(stats["dirs_listed"], 1)                     # not the whole share
        self.assertTrue(os.path.islink(os.path.join(self.pool, "tvseries/Show A/Season 1/e2.mp4")))

    def test_a_new_folder_deep_in_the_tree_is_found(self):
        self.scan()
        touch(os.path.join(self.media, "tvseries/Show B/Season 1/x.mp4"))
        self.assertEqual(self.scan()["links_added"], 1)

    def test_a_server_that_does_not_move_folder_times_is_caught_by_a_full_scan(self):
        self.scan()
        d = os.path.join(self.media, "tvseries/Show A/Season 1")
        before = os.stat(d).st_mtime_ns                               # an hour old: settled
        touch(os.path.join(d, "e3.mp4"))
        os.utime(d, ns=(before, before))                              # the share did not update the folder's time
        self.assertEqual(self.scan()["links_added"], 0)               # incremental is fooled, by design
        self.assertEqual(self.scan(full=True)["links_added"], 1)      # the periodic full scan is the safety net

    def test_a_folder_changed_in_the_same_instant_as_the_scan_is_not_trusted(self):
        # Fresh folders: their times are within the safety margin of the scan, as when a file
        # is added in the same timestamp tick the scan recorded. The folder time cannot be
        # told apart from "unchanged", so the folder must be looked at again next pass.
        fresh = os.path.join(self.root, "fresh-media")
        touch(os.path.join(fresh, "movies/Film/a.mkv"))
        state = pool.new_state()
        pool.scan_pool(fresh, os.path.join(self.root, "fresh-pool"), state)
        d = os.path.join(fresh, "movies/Film")
        recorded = os.stat(d).st_mtime_ns
        touch(os.path.join(d, "b.mkv"))
        os.utime(d, ns=(recorded, recorded))                          # the time did not move: same tick
        stats = pool.scan_pool(fresh, os.path.join(self.root, "fresh-pool"), state)
        self.assertEqual(stats["links_added"], 1)                     # found anyway
        self.assertTrue(state["dirs"]["movies/Film"]["racy"])

    def test_a_settled_folder_is_trusted_and_a_fresh_one_is_not(self):
        self.scan()
        self.assertFalse(self.state["dirs"]["tvseries/Show A"]["racy"])               # an hour old
        touch(os.path.join(self.media, "tvseries/Show A/Season 1/e9.mp4"))
        self.scan()
        season = "tvseries/Show A/Season 1"
        self.assertTrue(self.state["dirs"][season]["racy"])                            # changed a moment ago
        # Long after, it is looked at once more (it was not trusted), found unchanged, and settles...
        later = pool.scan_pool(self.media, self.pool, self.state, now_ns=time_far_ahead())
        self.assertGreaterEqual(later["dirs_listed"], 1)
        self.assertFalse(self.state["dirs"][season]["racy"])
        # ...and only then is it skipped.
        again = pool.scan_pool(self.media, self.pool, self.state, now_ns=time_far_ahead())
        self.assertEqual(again["dirs_listed"], 0)

    def test_include_and_exclude_choose_libraries(self):
        self.scan(include=["movies"])
        self.assertEqual(listing(self.pool), ["movies/Film/Film.mkv"])
        shutil.rmtree(self.pool)
        self.scan(exclude=["bump", "movies"])
        self.assertEqual(sorted({p.split("/")[0] for p in listing(self.pool)}), ["tvseries"])

    def test_changing_the_filter_does_not_reuse_the_old_folder_lists(self):
        self.scan(include=["movies"])
        stats = self.scan(include=["movies", "bump"])
        self.assertTrue(stats["full"])
        self.assertIn("bump/b.mp4", listing(self.pool))

    def test_existing_links_and_files_are_never_replaced(self):
        mine = os.path.join(self.pool, "movies/Film/Film.mkv")
        os.makedirs(os.path.dirname(mine))
        os.symlink("/somewhere/else.mkv", mine)
        stats = self.scan()
        self.assertEqual(os.readlink(mine), "/somewhere/else.mkv")
        self.assertTrue(any("points elsewhere" in n for n in stats["notes"]))
        real = os.path.join(self.pool, "bump/b.mp4")
        os.remove(real)                                              # the link the first scan made
        touch(real, "a real file")                                   # a real file where a link would go
        self.assertTrue(any("kept existing file" in n for n in self.scan(full=True)["notes"]))
        with open(real) as f:
            self.assertEqual(f.read(), "a real file")

    def test_removed_files_leave_their_links_until_pruned(self):
        self.scan()
        os.remove(os.path.join(self.media, "movies/Film/Film.mkv"))
        self.scan(full=True)
        self.assertTrue(os.path.islink(os.path.join(self.pool, "movies/Film/Film.mkv")))
        mine = os.path.join(self.pool, "movies", "my own link.mkv")
        os.symlink("/not/under/media/x.mkv", mine)                    # a dead link that is not ours
        self.assertEqual(pool.prune_pool(self.media, self.pool), 1)
        self.assertFalse(os.path.lexists(os.path.join(self.pool, "movies/Film")))     # emptied folder removed
        self.assertTrue(os.path.islink(mine))                                         # foreign link kept
        self.assertTrue(os.path.islink(os.path.join(self.pool, "bump/b.mp4")))

    def test_a_vanished_folder_does_not_break_the_scan(self):
        self.scan()
        shutil.rmtree(os.path.join(self.media, "movies"))
        stats = self.scan(full=True)
        self.assertEqual(stats["notes"], [])
        self.assertNotIn("movies", self.state["dirs"])

    def test_the_scan_keeps_its_state_for_next_time(self):
        self.scan()
        self.assertIn("tvseries/Show A/Season 1", self.state["dirs"])
        self.assertEqual(self.state["dirs"]["tvseries/Show A"]["subdirs"], ["Season 1"])


class TagTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cat = os.path.join(self.root, "by-genre")
        os.makedirs(os.path.join(self.cat, "Comedy"))
        os.makedirs(os.path.join(self.cat, "Drama"))
        os.symlink("/m/a.mp4", os.path.join(self.cat, "Comedy", "a.mp4"))
        os.symlink("/m/b.mp4", os.path.join(self.cat, "Drama", "b.mp4"))
        os.symlink("/m/bump", os.path.join(self.cat, "bump"))             # a link to a folder on the share

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_a_tags_signature_changes_when_a_video_is_added_removed_or_repointed(self):
        base = sync.tag_signatures(self.cat)
        self.assertEqual(sorted(base), ["Comedy", "Drama"])               # bump is not a tag
        os.symlink("/m/new.mp4", os.path.join(self.cat, "Comedy", "new.mp4"))
        after = sync.tag_signatures(self.cat)
        self.assertNotEqual(after["Comedy"], base["Comedy"])
        self.assertEqual(after["Drama"], base["Drama"])                   # only that tag moved
        os.remove(os.path.join(self.cat, "Comedy", "new.mp4"))
        self.assertEqual(sync.tag_signatures(self.cat)["Comedy"], base["Comedy"])
        os.remove(os.path.join(self.cat, "Drama", "b.mp4"))
        os.symlink("/m/other.mp4", os.path.join(self.cat, "Drama", "b.mp4"))
        self.assertNotEqual(sync.tag_signatures(self.cat)["Drama"], base["Drama"])

    def test_nfos_and_other_files_do_not_count(self):
        base = sync.tag_signatures(self.cat)
        touch(os.path.join(self.cat, "Comedy", "x.nfo"))
        touch(os.path.join(self.cat, "Comedy", "poster.jpg"))
        self.assertEqual(sync.tag_signatures(self.cat), base)

    def test_a_video_in_a_nested_folder_counts(self):
        base = sync.tag_signatures(self.cat)["Comedy"]
        os.makedirs(os.path.join(self.cat, "Comedy", "tvseries", "Show"))
        os.symlink("/m/deep.mp4", os.path.join(self.cat, "Comedy", "tvseries", "Show", "d.mp4"))
        self.assertNotEqual(sync.tag_signatures(self.cat)["Comedy"], base)

    def test_a_whole_folder_linked_into_a_tag_counts_by_where_it_points(self):
        base = sync.tag_signatures(self.cat)["Comedy"]
        show = os.path.join(self.root, "share", "Show")              # a folder that exists, as on the share
        os.makedirs(show)
        os.symlink(show, os.path.join(self.cat, "Comedy", "Show"))
        self.assertNotEqual(sync.tag_signatures(self.cat)["Comedy"], base)

    def test_a_missing_folder_is_no_tags(self):
        self.assertEqual(sync.tag_signatures(os.path.join(self.root, "nope")), {})

    def test_stations_are_matched_by_the_strings_in_their_config(self):
        stations = {
            "Comedy": {"network_name": "Comedy", "day_templates": {"daily": {"0": {"tags": "Comedy"}}}},
            "Mixed": {"network_name": "Mixed", "day_templates": {"d": {"0": {"tags": ["Drama", "Comedy/tvseries/Show"]}}}},
            "Other": {"network_name": "Other", "commercial_dir": "commercials", "bump_dir": "bump"},
            "Nothing": {"network_name": "Nothing"},
        }
        self.assertEqual(sync.stations_using("Comedy", stations), ["Comedy", "Mixed"])    # a nested tag counts
        self.assertEqual(sync.stations_using("Drama", stations), ["Mixed"])
        self.assertEqual(sync.stations_using("commercials", stations), ["Other"])
        self.assertEqual(sync.stations_using("Comed", stations), [])                       # not a prefix match
        self.assertEqual(sync.stations_using("Absent", stations), [])


class FakeConsole:
    stations_now = {}
    calls = []
    fail_on = None

    def __init__(self, base):
        pass

    def stations(self):
        return FakeConsole.stations_now

    def update_catalog(self, name):
        if FakeConsole.fail_on == name:
            raise RuntimeError("boom")
        FakeConsole.calls.append(("update_catalog", name))


class WatchTest(unittest.TestCase):
    """The rule: never surprise a viewer. Report always; rebuild only when asked, in a quiet window."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cat = os.path.join(self.root, "by-genre")
        for tag in ("Comedy", "Drama"):
            os.makedirs(os.path.join(self.cat, tag))
            os.symlink(f"/m/{tag}.mp4", os.path.join(self.cat, tag, "a.mp4"))
        FakeConsole.stations_now = {
            "Comedy": {"network_name": "Comedy", "tags": "Comedy"},
            "Drama": {"network_name": "Drama", "tags": "Drama"},
        }
        FakeConsole.calls, FakeConsole.fail_on = [], None
        self.cfg = {**sync.config({}), "catalog": self.cat, "rebuild": False, "rebuild_hour": 4}
        self.state = {}
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.root)

    def watch(self, hour=12, day=4):
        return sync.watch_catalogs(self.cfg, self.state, FakeConsole, datetime(2026, 10, day, hour, 30), self.logs.append)

    def add_video(self, tag="Comedy", name="new.mp4"):
        os.symlink(f"/m/{name}", os.path.join(self.cat, tag, name))

    def test_the_first_run_records_a_baseline_and_flags_nothing(self):
        self.assertEqual(self.watch(), [])
        self.assertTrue(any("baseline" in m for m in self.logs))
        self.assertEqual(self.watch(), [])

    def test_a_change_is_reported_and_nothing_is_rebuilt_by_default(self):
        self.watch()
        self.add_video()
        self.assertEqual(self.watch(), ["Comedy"])
        self.assertTrue(any("Comedy" in m and "needs a catalog rebuild" in m for m in self.logs))
        self.assertTrue(any("SYNC_REBUILD is off" in m for m in self.logs))
        self.assertEqual(FakeConsole.calls, [])
        self.assertEqual(self.watch(), ["Comedy"])                    # and it stays pending

    def test_with_rebuild_on_it_waits_for_the_quiet_window(self):
        self.cfg["rebuild"] = True
        self.watch()
        self.add_video()
        self.assertEqual(self.watch(hour=19), ["Comedy"])             # evening: not now
        self.assertEqual(FakeConsole.calls, [])
        self.assertEqual(self.watch(hour=5), [])                      # 4:00-6:00 window
        self.assertEqual(FakeConsole.calls, [("update_catalog", "Comedy")])

    def test_only_the_affected_station_is_rebuilt(self):
        self.cfg["rebuild"] = True
        self.watch()
        self.add_video("Drama")
        self.watch(hour=4)
        self.assertEqual([c for c in FakeConsole.calls if c[0] == "update_catalog"], [("update_catalog", "Drama")])

    def test_a_rebuild_runs_once_a_day(self):
        self.cfg["rebuild"] = True
        self.watch()
        self.add_video()
        self.watch(hour=4)
        self.add_video(name="later.mp4")
        self.assertEqual(self.watch(hour=5), ["Comedy"])              # same day: wait until tomorrow
        self.assertEqual(len(FakeConsole.calls), 1)
        self.assertEqual(self.watch(hour=4, day=5), [])
        self.assertEqual(len(FakeConsole.calls), 2)

    def test_a_failed_rebuild_stays_pending_and_is_retried(self):
        self.cfg["rebuild"] = True
        self.watch()
        self.add_video()
        FakeConsole.fail_on = "Comedy"
        self.assertEqual(self.watch(hour=4), ["Comedy"])
        self.assertTrue(any("failed" in m for m in self.logs))
        FakeConsole.fail_on = None
        self.assertEqual(self.watch(hour=5), [])                      # the same day: today was not used up
        self.assertEqual(FakeConsole.calls, [("update_catalog", "Comedy")])

    def test_a_tag_no_station_uses_is_recorded_and_not_pending(self):
        self.watch()
        os.makedirs(os.path.join(self.cat, "Unused"))
        os.symlink("/m/u.mp4", os.path.join(self.cat, "Unused", "u.mp4"))
        self.assertEqual(self.watch(), [])
        self.assertTrue(any("no station uses it" in m for m in self.logs))

    def test_an_unreachable_console_is_logged_and_retried(self):
        class Down(FakeConsole):
            def stations(self):
                raise OSError("connection refused")
        self.watch()
        self.add_video()
        out = sync.watch_catalogs(self.cfg, self.state, Down, datetime(2026, 10, 4, 12), self.logs.append)
        self.assertEqual(out, [])
        self.assertTrue(any("not reachable" in m for m in self.logs))
        self.assertEqual(self.watch(), ["Comedy"])                    # still seen as changed next time

    def test_the_window_wraps_past_midnight(self):
        self.assertTrue(sync.in_window(23, 22, 4))
        self.assertTrue(sync.in_window(1, 22, 4))
        self.assertFalse(sync.in_window(2, 22, 4))
        self.assertFalse(sync.in_window(21, 22, 4))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        self.pool = os.path.join(self.root, "pool")
        os.makedirs(self.pool)
        touch(os.path.join(self.media, "movies/Film/Film.mkv"))
        age(self.media)
        self.cfg = {**sync.config({}), "media": self.media, "pool": self.pool,
                    "catalog": os.path.join(self.root, "by-genre")}

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_a_pass_links_the_media_and_remembers_what_it_saw(self):
        logs = []
        sync.run_once(self.cfg, FakeConsole, datetime(2026, 10, 4, 12), logs.append)
        self.assertTrue(os.path.islink(os.path.join(self.pool, "movies/Film/Film.mkv")))
        self.assertTrue(os.path.exists(os.path.join(self.pool, sync.STATE_NAME)))
        touch(os.path.join(self.media, "movies/Film/Extra.mp4"))
        out = sync.run_once(self.cfg, FakeConsole, datetime(2026, 10, 4, 13), logs.append)
        self.assertEqual(out["pool"]["links_added"], 1)
        self.assertEqual(out["pool"]["dirs_listed"], 1)               # state survived between passes

    def test_the_first_pass_is_a_full_scan_and_the_next_is_not(self):
        first = sync.run_once(self.cfg, FakeConsole, datetime(2026, 10, 4, 12), lambda m: None)
        second = sync.run_once(self.cfg, FakeConsole, datetime(2026, 10, 4, 13), lambda m: None)
        self.assertTrue(first["pool"]["full"])
        self.assertFalse(second["pool"]["full"])

    def test_the_pool_must_not_be_on_smb(self):
        mounts = "36 35 98:0 / / rw - ext4 /dev/sda1 rw\n50 36 0:45 / /app/pool rw - cifs //srv/s rw\n"
        self.assertEqual(sync.filesystem_of("/app/pool/x", mounts), "cifs")
        self.assertEqual(sync.filesystem_of("/app/other", mounts), "ext4")

    def test_config_reads_the_environment(self):
        cfg = sync.config({"SYNC_INTERVAL_MINUTES": "15", "SYNC_INCLUDE": "movies, tvseries", "SYNC_REBUILD": "1",
                           "SYNC_REBUILD_HOUR": "27", "SYNC_PRUNE": "1"})
        self.assertEqual((cfg["interval"], cfg["include"], cfg["rebuild"], cfg["rebuild_hour"], cfg["prune"]),
                         (15, ["movies", "tvseries"], True, 3, True))
        default = sync.config({})
        self.assertEqual((default["rebuild"], default["prune"], default["interval"], default["rebuild_hour"]),
                         (True, False, 60, 4))                              # the nightly update is on unless switched off
        for off in ("0", "false", "no", "off"):
            self.assertFalse(sync.config({"SYNC_REBUILD": off})["rebuild"], off)
        self.assertTrue(sync.config({"SYNC_REBUILD": ""})["rebuild"])             # an empty compose value is "not set"


if __name__ == "__main__":
    unittest.main()
