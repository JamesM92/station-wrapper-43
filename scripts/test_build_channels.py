"""Tests for build_channels.py. Pure logic plus a symlink farm in a temp dir.

Run: python3 scripts/test_build_channels.py  (needs defusedxml)
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import build_channels as bc


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def tvshow_nfo(*genres):
    body = "".join(f"<genre>{g}</genre>" for g in genres)
    return f"<tvshow><title>x</title>{body}</tvshow>"


def movie_nfo(*genres):
    body = "".join(f"<genre>{g}</genre>" for g in genres)
    return f"<movie><title>x</title>{body}</movie>"


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        touch(os.path.join(self.media, "bump", "bump1.mp4"))

        show_a = os.path.join(self.media, "tvseries", "Show A (2010) {tvdb-1}")
        touch(os.path.join(show_a, "tvshow.nfo"), tvshow_nfo("Comedy", "Drama"))
        touch(os.path.join(show_a, "Season 01", "Show A - S01E01 - Pilot.mkv"))
        touch(os.path.join(show_a, "Season 01", "Show A - S01E01 - Pilot.nfo"))

        show_b = os.path.join(self.media, "tvseries", "Show B (2012)")
        touch(os.path.join(show_b, "tvshow.nfo"), tvshow_nfo("Drama"))
        touch(os.path.join(show_b, "Season 01", "Show B - S01E01.mp4"))

        show_c = os.path.join(self.media, "tvseries", "Show C")
        touch(os.path.join(show_c, "Season 01", "Show C - S01E01.mp4"))

        film = os.path.join(self.media, "movies", "Film One (1999)")
        touch(os.path.join(film, "Film One (1999).mkv"))
        touch(os.path.join(film, "Film One (1999).nfo"), movie_nfo("Sci-Fi"))

        film2 = os.path.join(self.media, "movies", "Film Two (2001)")
        touch(os.path.join(film2, "Film Two (2001).mp4"))
        touch(os.path.join(film2, "movie.nfo"), movie_nfo("Sci-Fi", "Action"))

        touch(os.path.join(self.media, "anime", "Anime X", "Season 01", "X - 001.mkv"))
        touch(os.path.join(self.media, "anime", "Anime X", "tvshow.nfo"), tvshow_nfo("Animation"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_discovers_primary_genre_and_unclassified(self):
        items, _ = bc.discover(self.media, {})
        by_name = {i["item"]: i for i in items}
        self.assertEqual(by_name["Show A (2010) {tvdb-1}"]["genre"], "Comedy")
        self.assertEqual(by_name["Show B (2012)"]["genre"], "Drama")
        self.assertIsNone(by_name["Show C"]["genre"])
        self.assertEqual(by_name["Film One (1999)"]["genre"], "Sci-Fi")
        self.assertEqual(by_name["Film Two (2001)"]["genre"], "Sci-Fi")
        self.assertEqual(by_name["Anime X"]["genre"], "Animation")

    def test_genre_map_applies_case_insensitively(self):
        items, _ = bc.discover(self.media, {"sci-fi": "Science Fiction"})
        film = next(i for i in items if i["item"] == "Film One (1999)")
        self.assertEqual(film["genre"], "Science Fiction")

    def test_unclassified_is_skipped_by_default(self):
        items, _ = bc.discover(self.media, {})
        plan, skipped = bc.plan_farm(items, include_unclassified=False)
        self.assertNotIn(bc.UNCLASSIFIED, plan)
        self.assertIn("tvseries/Show C", skipped)
        plan, skipped = bc.plan_farm(items, include_unclassified=True)
        self.assertIn(bc.UNCLASSIFIED, plan)
        self.assertEqual(skipped, [])

    def test_sidecar_nfo_is_linked_with_its_video(self):
        items, _ = bc.discover(self.media, {})
        plan, _ = bc.plan_farm(items, include_unclassified=False)
        links = {link for link, _ in plan["Comedy"]}
        self.assertIn("Comedy/tvseries/Show A (2010) {tvdb-1}/Season 01/Show A - S01E01 - Pilot.nfo", links)

    def test_numbers_are_stable_across_runs(self):
        state = {"genres": {"Drama": {"number": 805, "hash": "x"}}}
        numbers = bc.assign_numbers({"Comedy", "Drama", "Sci-Fi"}, state, 801)
        self.assertEqual(numbers["Drama"], 805)
        self.assertEqual(sorted(numbers.values()), sorted(set(numbers.values())))
        self.assertIn(801, numbers.values())

    def plan(self):
        items, _ = bc.discover(self.media, {})
        plan, _ = bc.plan_farm(items, include_unclassified=False)
        return plan

    def test_farm_links_point_at_target_root(self):
        catalog = os.path.join(self.root, "catalog")
        added, notes = bc.import_links(catalog, self.plan(), "/mnt/media")
        link = os.path.join(catalog, "by-genre", "Drama", "tvseries", "Show B (2012)",
                            "Season 01", "Show B - S01E01.mp4")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link),
                         "/mnt/media/tvseries/Show B (2012)/Season 01/Show B - S01E01.mp4")
        self.assertEqual(notes, [])
        self.assertGreater(added["Drama"], 0)

    def test_a_second_run_adds_nothing_and_a_new_video_adds_one_link(self):
        catalog = os.path.join(self.root, "catalog")
        bc.import_links(catalog, self.plan(), "/mnt/media")
        added, notes = bc.import_links(catalog, self.plan(), "/mnt/media")
        self.assertEqual((added, notes), ({}, []))
        touch(os.path.join(self.media, "tvseries", "Show B (2012)", "Season 01", "Show B - S01E02.mp4"))
        added, _ = bc.import_links(catalog, self.plan(), "/mnt/media")
        self.assertEqual(added, {"Drama": 1})

    def test_existing_links_files_and_folders_are_never_touched(self):
        catalog = os.path.join(self.root, "catalog")
        farm = os.path.join(catalog, "by-genre")
        # a hand-made tag folder, a hand-made link in a generated one, and an
        # existing link the generator would have pointed somewhere else
        touch(os.path.join(farm, "Mine", "keep.mp4"))
        os.makedirs(os.path.join(farm, "Drama"))
        os.symlink("/elsewhere/mine.mp4", os.path.join(farm, "Drama", "mine.mp4"))
        taken = os.path.join(farm, "Drama", "tvseries", "Show B (2012)", "Season 01")
        os.makedirs(taken)
        os.symlink("/somewhere/else.mp4", os.path.join(taken, "Show B - S01E01.mp4"))
        added, notes = bc.import_links(catalog, self.plan(), "/mnt/media")
        self.assertTrue(os.path.exists(os.path.join(farm, "Mine", "keep.mp4")))
        self.assertEqual(os.readlink(os.path.join(farm, "Drama", "mine.mp4")), "/elsewhere/mine.mp4")
        self.assertEqual(os.readlink(os.path.join(taken, "Show B - S01E01.mp4")), "/somewhere/else.mp4")
        self.assertTrue(any("points elsewhere" in n and "Show B - S01E01.mp4" in n for n in notes))

    def test_stale_links_are_kept_without_prune(self):
        catalog = os.path.join(self.root, "catalog")
        bc.import_links(catalog, self.plan(), "/mnt/media")
        shutil.rmtree(os.path.join(self.media, "tvseries", "Show B (2012)"))
        bc.import_links(catalog, self.plan(), "/mnt/media")
        self.assertTrue(os.path.islink(os.path.join(
            catalog, "by-genre", "Drama", "tvseries", "Show B (2012)", "Season 01", "Show B - S01E01.mp4")))

    def test_dry_run_adds_nothing_but_counts(self):
        catalog = os.path.join(self.root, "catalog")
        added, _ = bc.import_links(catalog, self.plan(), "/mnt/media", dry=True)
        self.assertGreater(added["Drama"], 0)
        self.assertFalse(os.path.exists(catalog))

    def test_the_bump_clips_are_linked_into_a_real_folder_not_the_whole_folder_linked(self):
        touch(os.path.join(self.media, "bump", "pre", "p1.mp4"))
        touch(os.path.join(self.media, "bump", "notes.txt"))              # not a clip
        touch(os.path.join(self.media, "bump", ".hidden.mp4"))
        clips = bc.bump_entries(self.media, "/mnt/media/bump")
        self.assertEqual(clips, [("bump/bump1.mp4", "/mnt/media/bump/bump1.mp4"),
                                 ("bump/pre/p1.mp4", "/mnt/media/bump/pre/p1.mp4")])
        catalog = os.path.join(self.root, "catalog")
        added, _ = bc.import_links(catalog, {}, "/mnt/media", extra=clips)
        folder = os.path.join(catalog, "by-genre", "bump")
        self.assertEqual(added, {"bump": 2})
        self.assertTrue(os.path.isdir(folder) and not os.path.islink(folder))      # a real folder
        self.assertTrue(os.path.isdir(os.path.join(folder, "pre")) and not os.path.islink(os.path.join(folder, "pre")))
        self.assertEqual(os.readlink(os.path.join(folder, "pre", "p1.mp4")), "/mnt/media/bump/pre/p1.mp4")
        self.assertEqual(bc.import_links(catalog, {}, "/mnt/media", extra=clips)[0], {})   # and once only

    def test_the_old_whole_folder_bump_link_is_replaced_but_a_link_of_yours_is_not(self):
        catalog = os.path.join(self.root, "catalog")
        farm = os.path.join(catalog, "by-genre")
        os.makedirs(farm)
        os.symlink("/mnt/media/bump", os.path.join(farm, "bump"))                  # what an earlier version made
        note, blocked = bc.retire_bump_link(catalog, "/mnt/media/bump")
        self.assertIn("real folder", note)
        self.assertFalse(blocked)
        self.assertFalse(os.path.lexists(os.path.join(farm, "bump")))
        # a link to somewhere else is yours: nothing is added under it, which would write into that place
        elsewhere = os.path.join(self.root, "my-own-bumps")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(farm, "bump"))
        note, blocked = bc.retire_bump_link(catalog, "/mnt/media/bump")
        self.assertTrue(blocked)
        self.assertIn("points to", note.replace("is a link to", "points to"))
        self.assertTrue(os.path.islink(os.path.join(farm, "bump")))
        self.assertEqual(os.listdir(elsewhere), [])

    def test_links_are_never_written_through_a_folder_link_that_points_elsewhere(self):
        catalog = os.path.join(self.root, "catalog")
        farm = os.path.join(catalog, "by-genre")
        elsewhere = os.path.join(self.root, "a-folder-on-the-share")
        os.makedirs(elsewhere)
        os.makedirs(os.path.join(farm, "Mine"))
        os.symlink(elsewhere, os.path.join(farm, "Mine", "LinkedShow"))             # the user linked a whole folder in
        added, notes = bc.import_links(catalog, {}, "/mnt/media", extra=[("Mine/LinkedShow/new.mp4", "/mnt/media/x.mp4")])
        self.assertEqual(added, {})
        self.assertTrue(any("inside a linked folder" in n for n in notes))
        self.assertEqual(os.listdir(elsewhere), [])                                 # nothing leaked into it

    def test_prune_reaches_the_bump_clips_too(self):
        catalog = os.path.join(self.root, "catalog")
        clips = bc.bump_entries(self.media, "/mnt/media/bump")
        bc.import_links(catalog, {}, "/mnt/media", extra=clips)
        os.remove(os.path.join(self.media, "bump", "bump1.mp4"))
        dead = bc.find_dead_links(catalog, [("/mnt/media", self.media)])
        self.assertEqual([os.path.basename(d) for d in dead], ["bump1.mp4"])

    def test_prune_removes_only_dead_links_this_script_manages(self):
        catalog = os.path.join(self.root, "catalog")
        farm = os.path.join(catalog, "by-genre")
        mapping = [("/mnt/media", self.media)]
        bc.import_links(catalog, self.plan(), "/mnt/media")
        os.symlink("/elsewhere/gone.mp4", os.path.join(farm, "Drama", "hand_made_dead.mp4"))
        os.makedirs(os.path.join(farm, "Empty Folder I Made"))
        shutil.rmtree(os.path.join(self.media, "tvseries", "Show B (2012)"))
        dead = bc.find_dead_links(catalog, mapping)
        # the video and the show's tvshow.nfo, both linked and both gone
        self.assertEqual(sorted(os.path.basename(d) for d in dead), ["Show B - S01E01.mp4", "tvshow.nfo"])
        removed = bc.prune_links(catalog, dead)
        self.assertEqual(removed, {"Drama": 2})
        self.assertFalse(os.path.exists(os.path.join(farm, "Drama", "tvseries", "Show B (2012)")))
        # the other Drama show, the hand-made link and the hand-made empty folder all stay
        self.assertTrue(os.path.islink(os.path.join(farm, "Drama", "hand_made_dead.mp4")))
        self.assertTrue(os.path.isdir(os.path.join(farm, "Empty Folder I Made")))
        self.assertTrue(os.path.isdir(os.path.join(farm, "Comedy")))

    def test_duplicates_are_the_same_video_twice_in_one_tag_only(self):
        catalog = os.path.join(self.root, "catalog")
        farm = os.path.join(catalog, "by-genre")
        real = os.path.join(self.media, "tvseries", "Show B (2012)", "Season 01", "Show B - S01E01.mp4")
        bc.import_links(catalog, self.plan(), self.media)       # real paths, so the links can be followed
        # a second name for the same file in Drama, and the same file in another tag (fine)
        os.symlink(real, os.path.join(farm, "Drama", "second name.mp4"))
        os.makedirs(os.path.join(farm, "Mine"))
        os.symlink(real, os.path.join(farm, "Mine", "also here.mp4"))
        dups = bc.find_duplicate_links(catalog)
        self.assertEqual(list(dups), ["Drama"])
        self.assertEqual(len(dups["Drama"]), 1)
        self.assertEqual(len(dups["Drama"][0]), 2)

    def test_no_duplicates_in_a_clean_farm_or_a_missing_one(self):
        self.assertEqual(bc.find_duplicate_links(os.path.join(self.root, "nothing")), {})
        catalog = os.path.join(self.root, "catalog")
        bc.import_links(catalog, self.plan(), self.media)
        self.assertEqual(bc.find_duplicate_links(catalog), {})

    def test_to_host_uses_the_longest_prefix(self):
        mapping = [("/mnt/media", "/host/media"), ("/mnt/media/tanfo", "/host/view")]
        self.assertEqual(bc.to_host("/mnt/media/tanfo/A/x.mp4", mapping), "/host/view/A/x.mp4")
        self.assertEqual(bc.to_host("/mnt/media/tvseries/x.mp4", mapping), "/host/media/tvseries/x.mp4")
        self.assertIsNone(bc.to_host("/elsewhere/x.mp4", mapping))
        self.assertIsNone(bc.to_host("/mnt/media2/x.mp4", mapping))

    def test_station_conf_is_valid_for_fieldstation(self):
        conf = bc.station_conf("Drama", 803, bc.BUMP)
        self.assertEqual(conf["network_name"], "Drama")
        self.assertEqual(conf["content_dir"], "catalog/by-genre")
        self.assertEqual(conf["day_templates"]["daily"]["0"], {"tags": "Drama"})
        self.assertEqual(len(conf["day_templates"]["daily"]), 24)
        for day in ("monday", "sunday"):
            self.assertEqual(conf[day], "daily")

    def test_tuner_channels_keep_hand_made_and_drop_stale(self):
        path = os.path.join(self.root, "config.json")
        with open(path, "w") as f:
            json.dump({"port": 5004, "channels": {"800": "TestNet", "801": "Old Genre"}}, f)
        bc.write_tuner_channels(path, {"Drama": 802, "Comedy": 801}, {"Old Genre"})
        with open(path) as f:
            conf = json.load(f)
        self.assertEqual(conf["channels"], {"800": "TestNet", "801": "Comedy", "802": "Drama"})
        self.assertEqual(conf["port"], 5004)

    def test_numbers_skip_channels_taken_by_other_stations(self):
        numbers = bc.assign_numbers({"Comedy", "Drama"}, {"genres": {}}, 801, reserved={801, 802})
        self.assertEqual(sorted(numbers.values()), [803, 804])



class TvLibraryTest(unittest.TestCase):
    """A folder that is itself a TV library: the TA_NFOWriter view layout."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.src = os.path.join(self.root, "tubearchivist")      # the real videos
        self.view = os.path.join(self.root, "tanfo")             # channel folders + links
        real = os.path.join(self.src, "UCabc", "vid1.mp4")
        touch(real)
        chan = os.path.join(self.view, "NBC News")
        touch(os.path.join(chan, "tvshow.nfo"), tvshow_nfo("News &amp; Politics"))
        season = os.path.join(chan, "Season 2026")
        base = os.path.join(season, "20260911_[vid1]_Special")
        os.makedirs(season)
        os.symlink(real, base + ".mp4")                          # video is a symlink
        touch(base + ".nfo", "<episodedetails/>")
        touch(base + ".en.vtt")                                  # subtitles: not linked
        touch(base + "-thumb.jpg")
        touch(os.path.join(season, "season.nfo"), "<season/>")
        touch(os.path.join(self.view, "ta_nfo.py"))              # the script sits in the view
        touch(os.path.join(self.view, "__pycache__", "x.pyc"))
        self.media = os.path.join(self.root, "media")            # no tvseries/anime/movies here
        os.makedirs(self.media)

    def tearDown(self):
        shutil.rmtree(self.root)

    def lib(self, spec=None):
        return [bc.parse_tv_library(spec or self.view, {"bump", "tvseries", "anime", "movies"})]

    def test_view_is_read_as_a_library_labelled_by_folder(self):
        items, warnings = bc.discover(self.media, {}, self.lib(), default_libraries=False)
        self.assertEqual(warnings, [])
        self.assertEqual([(i["library"], i["item"], i["genre"]) for i in items],
                         [("tanfo", "NBC News", "News & Politics")])

    def test_script_and_pycache_are_not_shows(self):
        items, _ = bc.discover(self.media, {}, self.lib(), default_libraries=False)
        self.assertEqual([i["item"] for i in items], ["NBC News"])

    def test_video_symlinks_and_nfos_are_listed_but_not_subtitles(self):
        items, _ = bc.discover(self.media, {}, self.lib(), default_libraries=False)
        files = items[0]["files"]
        self.assertIn("Season 2026/20260911_[vid1]_Special.mp4", files)
        self.assertIn("Season 2026/20260911_[vid1]_Special.nfo", files)
        self.assertIn("tvshow.nfo", files)
        self.assertFalse([f for f in files if f.endswith(".vtt") or f.endswith(".jpg")])

    def test_farm_links_resolve_through_the_view_to_the_archive(self):
        catalog = os.path.join(self.root, "catalog")
        items, _ = bc.discover(self.media, {}, self.lib(), default_libraries=False)
        plan, _ = bc.plan_farm(items, include_unclassified=False)
        bc.import_links(catalog, plan, "/ignored/for/absolute/targets")
        link = os.path.join(catalog, bc.FARM, "News & Politics", "tanfo", "NBC News",
                            "Season 2026", "20260911_[vid1]_Special.mp4")
        # farm link -> view link -> real file
        self.assertEqual(os.readlink(link), os.path.join(self.view, "NBC News", "Season 2026",
                                                         "20260911_[vid1]_Special.mp4"))
        self.assertEqual(os.path.realpath(link), os.path.join(self.src, "UCabc", "vid1.mp4"))

    def test_container_path_can_differ_from_host_path(self):
        items, _ = bc.discover(self.media, {}, self.lib(self.view + "=/data/tanfo"),
                               default_libraries=False)
        plan, _ = bc.plan_farm(items, include_unclassified=False)
        target = plan["News & Politics"][0][1]
        self.assertTrue(target.startswith("/data/tanfo/NBC News/"), target)

    def test_default_libraries_still_use_target_root(self):
        touch(os.path.join(self.media, "tvseries", "Show", "tvshow.nfo"), tvshow_nfo("Drama"))
        touch(os.path.join(self.media, "tvseries", "Show", "S01.mp4"))
        items, _ = bc.discover(self.media, {}, self.lib())
        plan, _ = bc.plan_farm(items, include_unclassified=False)
        self.assertEqual(plan["Drama"][0][1], "tvseries/Show/S01.mp4")
        self.assertTrue(plan["News & Politics"][0][1].startswith(self.view))

    def test_label_must_not_clash_with_a_default_library(self):
        clash = os.path.join(self.root, "tvseries")
        os.makedirs(clash)
        with self.assertRaises(SystemExit):
            bc.parse_tv_library(clash, {"bump", "tvseries", "anime", "movies"})

    def test_missing_folder_and_relative_target_are_refused(self):
        with self.assertRaises(SystemExit):
            bc.parse_tv_library(os.path.join(self.root, "nope"), set())
        with self.assertRaises(SystemExit):
            bc.parse_tv_library(self.view + "=relative/path", set())


class PlaylistTest(unittest.TestCase):
    """A TA_NFOWriter playlist groups videos without moving them."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.src = os.path.join(self.root, "tubearchivist")
        self.view = os.path.join(self.root, "tanfo")
        for chan, vid, genre in (("80s Ads", "ad1", "Entertainment"), ("80s Ads", "ad2", "Entertainment"),
                                 ("News", "n1", "News"), ("News", "n2", "News")):
            real = os.path.join(self.src, "UC" + chan[:3], vid + ".mp4")
            touch(real)
            touch(os.path.join(self.view, chan, "tvshow.nfo"), tvshow_nfo(genre))
            season = os.path.join(self.view, chan, "Season 1986")
            os.makedirs(season, exist_ok=True)
            os.symlink(real, os.path.join(season, f"19860101_[{vid}]_T.mp4"))
            touch(os.path.join(season, f"19860101_[{vid}]_T.nfo"), "<episodedetails/>")
        touch(os.path.join(self.src, "UC80s", "show.mp4"))
        os.symlink(os.path.join(self.src, "UC80s", "show.mp4"),
                   os.path.join(self.view, "80s Ads", "Season 1986", "19860102_[show]_Show.mp4"))
        pl = os.path.join(self.view, "Playlists")
        touch(os.path.join(pl, "1980s Commercials.m3u8"),
              "#EXTM3U\n#PLAYLIST:1980s Commercials\n"
              "#EXTINF:-1,A\n../80s Ads/Season 1986/19860101_[ad1]_T.mp4\n"
              "#EXTINF:-1,B\n../80s Ads/Season 1986/19860101_[ad2]_T.mp4\n"
              "../80s Ads/Season 1986/gone.mp4\n")
        touch(os.path.join(pl, "Mixed.m3u8"),
              "#EXTM3U\n../News/Season 1986/19860101_[n1]_T.mp4\n"
              "../80s Ads/Season 1986/19860101_[ad1]_T.mp4\n")
        self.libs = [bc.parse_tv_library(self.view + "=/data/tanfo", {"bump", "tvseries", "anime", "movies"})]

    def tearDown(self):
        shutil.rmtree(self.root)

    def build(self, *specs, genre_map=None, include_unclassified=False):
        """specs are "NAME[=DEST]" playlists, or (kind, spec) tuples."""
        groups = [s if isinstance(s, tuple) else ("playlist", s) for s in specs]
        items, _ = bc.discover("/none", genre_map or {}, self.libs, default_libraries=False)
        com, routes, warns = bc.resolve_groups(groups, items, self.libs, genre_map or {})
        plan, skipped = bc.plan_farm(items, include_unclassified, routes)
        return com, plan, skipped, warns

    def videos(self, plan, genre):
        return sorted(os.path.basename(l) for l, _ in plan.get(genre, []) if l.endswith(".mp4"))

    def test_a_bare_playlist_is_its_own_channel_and_the_videos_stay_where_they_were(self):
        _, plan, _, _ = self.build("Mixed")
        self.assertEqual(self.videos(plan, "Mixed"), ["19860101_[ad1]_T.mp4", "19860101_[n1]_T.mp4"])
        # linked into the new place and still in their own channels: no copies, several locations
        self.assertEqual(self.videos(plan, "News"), ["19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])
        self.assertIn("19860101_[ad1]_T.mp4", self.videos(plan, "Entertainment"))

    def test_dest_genre_joins_that_channel(self):
        _, plan, _, _ = self.build("Mixed=News")
        self.assertEqual(self.videos(plan, "News"),
                         ["19860101_[ad1]_T.mp4", "19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])
        self.assertNotIn("Mixed", plan)

    def test_sending_a_channel_elsewhere_does_not_empty_it(self):
        touch(os.path.join(self.view, "Playlists", "AllNews.m3u8"),
              "#EXTM3U\n../News/Season 1986/19860101_[n1]_T.mp4\n../News/Season 1986/19860101_[n2]_T.mp4\n")
        _, plan, _, _ = self.build("AllNews=Entertainment")
        self.assertEqual(len(self.videos(plan, "News")), 2)
        self.assertIn("19860101_[n1]_T.mp4", self.videos(plan, "Entertainment"))

    def test_dest_goes_through_the_genre_map(self):
        _, plan, _, _ = self.build("Mixed=news", genre_map={"news": "Current Affairs"})
        self.assertIn("Current Affairs", plan)

    def test_sidecar_nfo_follows_its_video(self):
        _, plan, _, _ = self.build("Mixed")
        self.assertIn("19860101_[n1]_T.nfo", [os.path.basename(l) for l, _ in plan["Mixed"]])
        self.assertIn("19860101_[n1]_T.nfo", [os.path.basename(l) for l, _ in plan["News"]])

    def test_a_video_can_be_imported_by_several_groups_into_several_places(self):
        _, plan, _, _ = self.build("Mixed=Alpha", "Mixed=Beta")
        for genre in ("Alpha", "Beta", "News"):
            self.assertIn("19860101_[n1]_T.mp4", self.videos(plan, genre))

    def test_routed_video_of_a_show_with_no_genre_is_not_skipped(self):
        os.remove(os.path.join(self.view, "News", "tvshow.nfo"))
        _, plan, skipped, _ = self.build("Mixed")
        self.assertEqual(self.videos(plan, "Mixed"), ["19860101_[ad1]_T.mp4", "19860101_[n1]_T.mp4"])
        self.assertIn(f"{self.libs[0][0]}/News", skipped)

    def test_commercials_become_container_paths_and_leave_the_channels(self):
        com, plan, _, warns = self.build("1980s Commercials=commercials")
        self.assertEqual([l for l, _ in com], [
            "commercials/1980s Commercials/19860101_[ad1]_T.mp4",
            "commercials/1980s Commercials/19860101_[ad2]_T.mp4"])
        self.assertEqual(com[0][1], "/data/tanfo/80s Ads/Season 1986/19860101_[ad1]_T.mp4")
        self.assertTrue(any("gone.mp4" in w for w in warns))
        self.assertEqual(self.videos(plan, "Entertainment"), ["19860102_[show]_Show.mp4"])

    def test_channel_made_only_of_commercials_disappears(self):
        os.remove(os.path.join(self.view, "80s Ads", "Season 1986", "19860102_[show]_Show.mp4"))
        _, plan, _, _ = self.build("1980s Commercials=commercials")
        self.assertNotIn("Entertainment", plan)

    def test_commercials_are_linked_under_their_own_tag_and_only_added_once(self):
        com, _, _, _ = self.build("1980s Commercials=commercials")
        catalog = os.path.join(self.root, "catalog")
        added, _ = bc.import_links(catalog, {}, "/x", com)
        self.assertEqual(added, {bc.COMMERCIALS: 2})
        self.assertEqual(bc.import_links(catalog, {}, "/x", com)[0], {})
        link = os.path.join(catalog, bc.FARM, "commercials", "1980s Commercials", "19860101_[ad1]_T.mp4")
        self.assertEqual(os.readlink(link), "/data/tanfo/80s Ads/Season 1986/19860101_[ad1]_T.mp4")

    def test_station_uses_commercials_only_when_given(self):
        self.assertTrue(bc.station_conf("News", 801, "bump")["commercial_free"])
        self.assertNotIn("commercial_dir", bc.station_conf("News", 801, "bump"))
        conf = bc.station_conf("News", 801, "bump", bc.COMMERCIALS)
        self.assertEqual((conf["commercial_dir"], conf["commercial_free"]), ("commercials", False))

    def test_a_video_in_two_commercial_groups_is_one_commercial(self):
        com, _, _, _ = self.build("1980s Commercials=commercials", ("show", "80s Ads=commercials"))
        targets = [t for _, t in com]
        self.assertEqual(len(targets), len(set(targets)))
        self.assertEqual(len(targets), 3)

    def test_several_playlists_can_build_one_commercial_set(self):
        pl = os.path.join(self.view, "Playlists")
        touch(os.path.join(pl, "1980s Ads Two.m3u8"),
              "#EXTM3U\n../News/Season 1986/19860101_[n1]_T.mp4\n"
              "../80s Ads/Season 1986/19860101_[ad2]_T.mp4\n")   # ad2 is also in the first playlist
        touch(os.path.join(pl, "1980s Ads Three.m3u8"), "#EXTM3U\n../News/Season 1986/19860101_[n2]_T.mp4\n")
        com, plan, _, _ = self.build("1980s Commercials=commercials", "1980s Ads Two=commercials",
                                     "1980s Ads Three=commercials")
        self.assertEqual(sorted(os.path.basename(l) for l, _ in com),
                         ["19860101_[ad1]_T.mp4", "19860101_[ad2]_T.mp4",
                          "19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])     # ad2 once, not twice
        self.assertEqual(self.videos(plan, "News"), [])  # News has no videos left, so no station
        self.assertNotIn("News", plan)

    def test_several_playlists_can_share_one_named_channel(self):
        touch(os.path.join(self.view, "Playlists", "More.m3u8"), "#EXTM3U\n../News/Season 1986/19860101_[n2]_T.mp4\n")
        _, plan, _, _ = self.build("Mixed=Retro Mix", "More=Retro Mix")
        self.assertEqual(self.videos(plan, "Retro Mix"),
                         ["19860101_[ad1]_T.mp4", "19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])

    def test_wildcard_matches_every_playlist_with_that_pattern(self):
        pl = os.path.join(self.view, "Playlists")
        touch(os.path.join(pl, "1980s Ads Two.m3u8"), "#EXTM3U\n../News/Season 1986/19860101_[n1]_T.mp4\n")
        touch(os.path.join(pl, "1980s Ads Three.m3u8"), "#EXTM3U\n../News/Season 1986/19860101_[n2]_T.mp4\n")
        com, _, _, _ = self.build("1980s *=commercials")      # also matches "1980s Commercials"
        self.assertEqual(len(com), 4)
        self.assertEqual({l.split("/")[1] for l, _ in com},
                         {"1980s Commercials", "1980s Ads Two", "1980s Ads Three"})
        with self.assertRaises(SystemExit) as cm:
            self.build("zzz*")
        self.assertIn("Mixed", str(cm.exception))

    def test_a_show_can_become_its_own_channel_with_its_nfos(self):
        _, plan, _, _ = self.build(("show", "News"))
        self.assertEqual(self.videos(plan, "News"), ["19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])
        names = [os.path.basename(l) for l, _ in plan["News"]]
        self.assertIn("tvshow.nfo", names)
        self.assertEqual(self.videos(plan, "Entertainment"), ["19860101_[ad1]_T.mp4",
                         "19860101_[ad2]_T.mp4", "19860102_[show]_Show.mp4"])

    def test_a_show_can_join_a_genre_or_be_the_commercials(self):
        _, plan, _, _ = self.build(("show", "News=Entertainment"))
        self.assertEqual(len(self.videos(plan, "News")), 2)            # still its own channel too
        self.assertEqual(len(self.videos(plan, "Entertainment")), 5)
        com, plan, _, _ = self.build(("show", "80s Ads=commercials"))
        self.assertEqual(len(com), 3)
        self.assertEqual(com[0][1].split("/")[:4], ["", "data", "tanfo", "80s Ads"])
        self.assertNotIn("Entertainment", plan)      # all its videos are commercials: no station
        self.assertEqual(self.videos(plan, "News"), ["19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])

    def test_show_names_take_wildcards_and_ignore_case(self):
        com, _, _, _ = self.build(("show", "* ads=commercials"))
        self.assertEqual(len(com), 3)

    def test_individual_videos_by_pattern(self):
        _, plan, _, _ = self.build(("video", "News/*[[]n1]*=Entertainment"))
        self.assertIn("19860101_[n1]_T.mp4", self.videos(plan, "Entertainment"))
        self.assertEqual(self.videos(plan, "News"), ["19860101_[n1]_T.mp4", "19860101_[n2]_T.mp4"])

    def test_video_needs_a_dest_and_a_match(self):
        with self.assertRaises(SystemExit):
            self.build(("video", "News/*"))
        with self.assertRaises(SystemExit):
            self.build(("video", "Nothing/*=Comedy"))
        with self.assertRaises(SystemExit):
            self.build(("show", "No Such Show"))

    def test_playlists_shows_and_videos_can_build_one_set(self):
        com, plan, _, _ = self.build("Mixed=commercials", ("show", "80s Ads=commercials"),
                                     ("video", "News/*[[]n2]*=commercials"))
        self.assertEqual(sorted(os.path.basename(l) for l, _ in com),
                         ["19860101_[ad1]_T.mp4", "19860101_[ad2]_T.mp4", "19860101_[n1]_T.mp4",
                          "19860101_[n2]_T.mp4", "19860102_[show]_Show.mp4"])   # ad1 once
        self.assertEqual(plan, {})

    def test_the_same_video_in_two_groups_is_linked_once_per_place(self):
        _, plan, _, _ = self.build(("show", "News=Retro"), "Mixed=Retro")
        links = [l for l, _ in plan["Retro"] if "[n1]" in l and l.endswith(".mp4")]
        self.assertEqual(len(links), 1)

    def test_group_options_keep_command_line_order(self):
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--playlist", action=bc.GroupAction, const="playlist", dest="groups")
        ap.add_argument("--show", action=bc.GroupAction, const="show", dest="groups")
        ap.add_argument("--video", action=bc.GroupAction, const="video", dest="groups")
        ns = ap.parse_args(["--show", "A", "--playlist", "B=x", "--video", "C=y", "--show", "D"])
        self.assertEqual(ns.groups, [("show", "A"), ("playlist", "B=x"), ("video", "C=y"), ("show", "D")])
        self.assertIsNone(ap.parse_args([]).groups)

    def test_unknown_playlist_lists_what_exists(self):
        with self.assertRaises(SystemExit) as cm:
            self.build("nope")
        self.assertIn("Mixed", str(cm.exception))
        with self.assertRaises(SystemExit):
            self.build("../escape")


class FieldStationSchemaTest(unittest.TestCase):
    """The station configs we write must satisfy FieldStation42's own schema,
    so a FS42 update that tightens or renames a key fails here, at sync time,
    instead of when the generator next saves a station."""

    # FS42_SRC points the test at another FieldStation42 checkout, which is how the
    # scheduled upstream-compat workflow tries the generator against upstream's HEAD.
    SCHEMA = os.path.join(
        os.environ.get("FS42_SRC") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                   "fieldstation42", "src"),
        "fs42", "station_config_schema.json")

    def setUp(self):
        # Locally a missing piece skips the test. With FS42_SRC set (the upstream
        # job) it fails, so a moved schema file can't make the check pass quietly.
        strict = bool(os.environ.get("FS42_SRC"))
        try:
            import jsonschema
        except ImportError:
            if strict:
                self.fail("jsonschema is not installed")
            self.skipTest("pip install jsonschema to check configs against FieldStation42's schema")
        if not os.path.exists(self.SCHEMA):
            if strict:
                self.fail(f"no station_config_schema.json at {self.SCHEMA}")
            self.skipTest("fieldstation42/src is not present (run scripts/sync-fs42.sh)")
        with open(self.SCHEMA) as f:
            self.validator = jsonschema.Draft7Validator(json.load(f))

    def errors(self, conf):
        return sorted(e.message for e in self.validator.iter_errors({"station_conf": conf}))

    def test_generated_stations_are_valid(self):
        self.assertEqual(self.errors(bc.station_conf("Comedy", 801, "bump")), [])
        self.assertEqual(self.errors(bc.station_conf("Comedy", 801, "bump", bc.COMMERCIALS)), [])

    def test_the_schema_does_reject_a_broken_station(self):
        # guards this test against a schema that quietly stopped checking anything
        conf = bc.station_conf("Comedy", 801, "bump")
        conf["schedule_increment"] = "sixty"
        del conf["network_name"]
        self.assertTrue(self.errors(conf))




class ContainerPathsTest(unittest.TestCase):
    """A view built inside the containers links to paths that exist only there.

    On the host, /mnt/media/tubearchivist/... is not a real path, so nothing
    host-side may follow a view link to its end.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.view = os.path.join(self.root, "catalog", "tanfo")
        chan = os.path.join(self.view, "Chan")
        touch(os.path.join(chan, "tvshow.nfo"), tvshow_nfo("News"))
        self.videos = []
        for vid in ("a", "b"):
            link = os.path.join(chan, "Season 1", f"2026_[{vid}]_T.mp4")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(f"/mnt/not-on-this-host/tubearchivist/UCx/{vid}.mp4", link)   # a container path
            self.videos.append(link)
            touch(link[:-4] + ".nfo", "<episodedetails/>")
        touch(os.path.join(self.view, "Playlists", "Both.m3u8"),
              "#EXTM3U\n../Chan/Season 1/2026_[a]_T.mp4\n../Chan/Season 1/2026_[b]_T.mp4\n")
        self.libs = [bc.parse_tv_library(self.view + "=/app/catalog/tanfo", {"bump", "tvseries", "anime", "movies"})]

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_videos_are_found_without_following_their_links(self):
        items, warnings = bc.discover("/none", {}, self.libs, default_libraries=False)
        self.assertEqual(warnings, [])
        self.assertEqual(len([f for f in items[0]["files"] if f.endswith(".mp4")]), 2)

    def test_a_playlist_keeps_every_video_in_it(self):
        videos, warnings = bc.read_playlist("Both", self.libs)
        self.assertEqual(len(videos), 2)
        self.assertEqual(warnings, [])
        self.assertTrue(videos[0][1].startswith("/app/catalog/tanfo/Chan/"))     # the container's path

    def test_a_playlist_still_skips_an_entry_whose_link_is_not_in_the_view(self):
        touch(os.path.join(self.view, "Playlists", "Gone.m3u8"), "#EXTM3U\n../Chan/Season 1/2026_[zzz]_T.mp4\n")
        videos, warnings = bc.read_playlist("Gone", self.libs)
        self.assertEqual(videos, [])
        self.assertTrue(any("missing video" in w for w in warnings))

    def test_prune_leaves_healthy_view_links_alone_and_catches_a_removed_one(self):
        catalog = os.path.join(self.root, "catalog")
        items, _ = bc.discover("/none", {}, self.libs, default_libraries=False)
        plan, _ = bc.plan_farm(items, include_unclassified=False)
        bc.import_links(catalog, plan, "/ignored")
        mapping = [("/app/catalog/tanfo", self.view)]
        self.assertEqual(bc.find_dead_links(catalog, mapping), [])       # nothing wrongly flagged
        os.remove(self.videos[0])                                        # the view's cleanup removed one
        dead = [os.path.basename(d) for d in bc.find_dead_links(catalog, mapping)]
        self.assertEqual(dead, ["2026_[a]_T.mp4"])

    def test_a_group_can_use_such_a_playlist_for_commercials(self):
        items, _ = bc.discover("/none", {}, self.libs, default_libraries=False)
        com, routes, warns = bc.resolve_groups([("playlist", "Both=commercials")], items, self.libs, {})
        self.assertEqual(len(com), 2)
        self.assertEqual(warns, [])
        self.assertTrue(all(t.startswith("/app/catalog/tanfo/Chan/") for _, t in com))

MOUNTINFO = """\
36 35 98:0 / / rw,relatime - ext4 /dev/sda1 rw
50 36 0:45 / /mnt/nas rw,relatime - cifs //server/share rw,vers=3.0
51 50 0:46 / /mnt/nas/local-bit rw - ext4 /dev/sdb1 rw
52 36 0:47 / /mnt/with\\040space rw - nfs4 server:/x rw
"""


class SymlinkSupportTest(unittest.TestCase):
    """The catalog is symlinks, and SMB shares are bad at those. Say so before building anything."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._saved = (bc.filesystem_of, os.symlink, os.readlink)

    def tearDown(self):
        bc.filesystem_of, os.symlink, os.readlink = self._saved
        shutil.rmtree(self.root)

    def test_filesystem_of_uses_the_deepest_mount_that_holds_the_path(self):
        fs = lambda p: bc.filesystem_of(p, MOUNTINFO)
        self.assertEqual(fs("/home/me/catalog"), "ext4")
        self.assertEqual(fs("/mnt/nas/catalog"), "cifs")
        self.assertEqual(fs("/mnt/nas"), "cifs")
        self.assertEqual(fs("/mnt/nas/local-bit/x"), "ext4")         # a local mount inside the share
        self.assertEqual(fs("/mnt/with space/x"), "nfs4")            # \040 in mountinfo means a space
        self.assertEqual(fs("/mnt/nasty"), "ext4")                   # not under /mnt/nas
        self.assertIsNone(bc.filesystem_of("/x", "garbage\n"))

    def test_the_probe_passes_on_a_normal_folder_and_leaves_nothing_behind(self):
        self.assertIsNone(bc.probe_symlinks(self.root))
        self.assertEqual(os.listdir(self.root), [])

    def test_the_probe_reports_a_share_that_refuses_symlinks(self):
        def refuse(*a, **k):
            raise OSError(95, "Operation not supported")
        os.symlink = refuse
        self.assertIn("Operation not supported", bc.probe_symlinks(self.root))
        self.assertEqual(os.listdir(self.root), [])                  # still cleaned up

    def test_the_probe_reports_a_link_that_reads_back_as_something_else(self):
        os.readlink = lambda p: "/server/side/path"                   # what a server-resolved link can look like
        self.assertIn("not the path it was given", bc.probe_symlinks(self.root))

    def test_a_working_local_folder_is_ok(self):
        level, message = bc.symlink_check(self.root, "catalog")
        self.assertEqual(level, "ok")

    def test_a_folder_that_does_not_exist_yet_is_judged_by_its_parent(self):
        self.assertEqual(bc.symlink_check(os.path.join(self.root, "a", "b"), "catalog")[0], "ok")

    def test_a_working_smb_folder_still_gets_a_note(self):
        bc.filesystem_of = lambda p, mountinfo=None: "cifs"
        level, message = bc.symlink_check(self.root, "catalog")
        self.assertEqual(level, "note")
        self.assertIn("SMB", message)
        self.assertIn("local disk", message)

    def test_a_broken_smb_folder_fails_with_the_fix(self):
        bc.filesystem_of = lambda p, mountinfo=None: "cifs"
        def refuse(*a, **k):
            raise OSError(1, "Operation not permitted")
        os.symlink = refuse
        level, message = bc.symlink_check(self.root, "catalog")
        self.assertEqual(level, "fail")
        for needle in ("SMB", "Operation not permitted", "local disk", "FS42_CATALOG_ROOT", "mfsymlinks"):
            self.assertIn(needle, message)

    def test_a_failure_on_a_local_disk_does_not_mention_smb(self):
        def refuse(*a, **k):
            raise OSError(1, "Operation not permitted")
        os.symlink = refuse
        bc.filesystem_of = lambda p, mountinfo=None: "ext4"
        level, message = bc.symlink_check(self.root, "catalog")
        self.assertEqual(level, "fail")
        self.assertNotIn("mfsymlinks", message)

    def test_a_folder_we_cannot_write_to_is_skipped_not_failed(self):
        real_access = os.access
        os.access = lambda p, mode: False
        try:
            self.assertEqual(bc.symlink_check(self.root, "TV library")[0], "skip")
        finally:
            os.access = real_access

class FakeConsole:
    """Stands in for the FieldStation42 console, recording what main() asks of it."""
    stations_now = {}
    log = []

    def __init__(self, base):
        pass

    def stations(self):
        return {k: dict(v) for k, v in FakeConsole.stations_now.items()}

    def station_channels(self):
        return {k: v["channel_number"] for k, v in FakeConsole.stations_now.items()}

    def save_station(self, conf):
        FakeConsole.log.append(("save", conf["network_name"]))
        FakeConsole.stations_now[conf["network_name"]] = dict(conf)

    ensure_station = bc.Console.ensure_station

    def rebuild_catalog(self, name):
        FakeConsole.log.append(("rebuild", name))


class FullRunTest(unittest.TestCase):
    """main() end to end against a fake console: add-only, and stations left alone."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        touch(os.path.join(self.media, "bump", "b.mp4"))
        for show, genre in (("Show A", "Comedy"), ("Show B", "Drama")):
            d = os.path.join(self.media, "tvseries", show)
            touch(os.path.join(d, "tvshow.nfo"), tvshow_nfo(genre))
            touch(os.path.join(d, "Season 01", f"{show} - S01E01.mp4"))
        self.ads = os.path.join(self.media, "tvseries", "Ads")
        touch(os.path.join(self.ads, "tvshow.nfo"), tvshow_nfo("Drama"))
        touch(os.path.join(self.ads, "Season 01", "ad1.mp4"))
        self.catalog = os.path.join(self.root, "catalog")
        os.makedirs(self.catalog)
        self.tuner = os.path.join(self.root, "tuner.json")
        with open(self.tuner, "w") as f:
            json.dump({"channels": {}}, f)
        self.farm = os.path.join(self.catalog, "by-genre")
        FakeConsole.stations_now, FakeConsole.log = {}, []
        self._saved = (bc.Console, bc.add_first_day, bc.restart_tuner)
        self.signalled = []
        bc.Console, bc.add_first_day = FakeConsole, lambda container, name, console=None: None
        bc.restart_tuner = lambda container: self.signalled.append(container) or True

    def tearDown(self):
        bc.Console, bc.add_first_day, bc.restart_tuner = self._saved
        shutil.rmtree(self.root)

    def run_main(self, *extra):
        FakeConsole.log = []
        argv = ["--media-root", self.media, "--target-root", "/mnt/media", "--catalog", self.catalog,
                "--tuner-config", self.tuner, "--fs42-container", "x", *extra]
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(bc.main(argv), 0)
        return buf.getvalue()

    def rebuilt(self):
        return sorted(n for op, n in FakeConsole.log if op == "rebuild")

    def test_first_run_builds_everything_and_a_second_run_nothing(self):
        self.run_main("--include-unclassified")
        self.assertEqual(self.rebuilt(), ["Comedy", "Drama"])
        self.assertEqual(sorted(FakeConsole.stations_now), ["Comedy", "Drama"])
        self.run_main("--include-unclassified")
        self.assertEqual(self.rebuilt(), [])

    def test_hand_made_links_survive_and_do_not_trigger_a_rebuild(self):
        self.run_main()
        mine = os.path.join(self.farm, "Comedy", "my_extra.mp4")
        os.symlink("/somewhere/mine.mp4", mine)
        os.makedirs(os.path.join(self.farm, "Mine"))
        self.run_main()
        self.assertEqual(os.readlink(mine), "/somewhere/mine.mp4")
        self.assertTrue(os.path.isdir(os.path.join(self.farm, "Mine")))
        self.assertEqual(self.rebuilt(), [])

    def test_a_new_video_rebuilds_only_its_genre(self):
        self.run_main()
        touch(os.path.join(self.media, "tvseries", "Show B", "Season 01", "Show B - S01E02.mp4"))
        self.run_main()
        self.assertEqual(self.rebuilt(), ["Drama"])

    def test_existing_stations_are_never_rewritten(self):
        self.run_main()
        FakeConsole.stations_now["Drama"]["break_duration"] = 300      # a hand edit in FieldStation42
        touch(os.path.join(self.media, "tvseries", "Show B", "Season 01", "Show B - S01E02.mp4"))
        FakeConsole.log = []
        self.run_main()
        self.assertEqual(FakeConsole.stations_now["Drama"]["break_duration"], 300)
        self.assertNotIn(("save", "Drama"), FakeConsole.log)

    def test_adding_commercials_later_fills_them_into_existing_stations_and_rebuilds_all(self):
        self.run_main()
        FakeConsole.stations_now["Comedy"]["break_duration"] = 300
        view = os.path.join(self.root, "view")
        self.run_main("--show", "Ads=commercials")
        # (no --tv-library needed: a show can come from the default libraries)
        self.assertEqual(self.rebuilt(), ["Comedy", "Drama"])
        for name in ("Comedy", "Drama"):
            conf = FakeConsole.stations_now[name]
            self.assertEqual((conf["commercial_dir"], conf["commercial_free"]), ("commercials", False))
        self.assertEqual(FakeConsole.stations_now["Comedy"]["break_duration"], 300)
        link = os.path.join(self.farm, "commercials", "Ads", "ad1.mp4")
        self.assertEqual(os.readlink(link), "/mnt/media/tvseries/Ads/Season 01/ad1.mp4")
        self.run_main("--show", "Ads=commercials")
        self.assertEqual(self.rebuilt(), [])

    def test_prune_removes_only_what_has_gone_and_rebuilds_that_genre(self):
        self.run_main()
        mine = os.path.join(self.farm, "Drama", "mine.mp4")
        os.symlink("/somewhere/mine.mp4", mine)
        shutil.rmtree(os.path.join(self.media, "tvseries", "Show B"))   # Drama keeps the Ads show
        gone = os.path.join(self.farm, "Drama", "tvseries", "Show B")
        self.run_main()                                   # without --prune: nothing is removed
        self.assertTrue(os.path.isdir(gone))
        self.assertEqual(self.rebuilt(), [])
        out = self.run_main("--prune")
        self.assertFalse(os.path.exists(gone))
        self.assertTrue(os.path.islink(mine))
        self.assertTrue(os.path.isdir(os.path.join(self.farm, "Comedy", "tvseries", "Show A")))
        self.assertEqual(self.rebuilt(), ["Drama"])
        self.assertIn("links whose source video is gone", out)

    def test_a_genre_that_vanishes_is_left_in_place_not_rebuilt_empty(self):
        self.run_main()
        shutil.rmtree(os.path.join(self.media, "tvseries", "Show A"))
        out = self.run_main("--prune")
        self.assertEqual(self.rebuilt(), [])
        self.assertIn("Comedy", FakeConsole.stations_now)
        self.assertIn("no longer in the archive", out)

    def test_a_run_from_a_manifest_alone(self):
        manifest = os.path.join(self.root, "groups.toml")
        with open(manifest, "w") as f:
            f.write(f'''
media_root = "{self.media}"
target_root = "/mnt/media"
catalog = "{self.catalog}"
tuner_config = "{self.tuner}"
fs42_container = "x"
include_unclassified = true
[[group]]
show = "Ads"
to = "commercials"
''')
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bc.main(["--manifest", manifest]), 0)
        self.assertEqual(sorted(FakeConsole.stations_now), ["Comedy", "Drama"])
        self.assertTrue(os.path.islink(os.path.join(self.farm, "commercials", "Ads", "ad1.mp4")))
        self.assertEqual(FakeConsole.stations_now["Comedy"]["commercial_dir"], "commercials")

    def test_a_video_linked_twice_in_one_tag_is_reported(self):
        self.run_main()
        a = os.path.join(self.farm, "Comedy", "tvseries", "Show A", "Season 01", "Show A - S01E01.mp4")
        os.symlink(os.readlink(a), os.path.join(self.farm, "Comedy", "copy of it.mp4"))   # hand-made
        out = self.run_main()
        self.assertIn("linked 2 times", out)
        self.assertIn("Comedy", out)
        self.assertIn("linked 2 times", self.run_main("--dry-run"))
        self.assertEqual(self.rebuilt(), [])         # a note, not a change

    def test_a_catalog_that_cannot_hold_symlinks_stops_the_run_before_anything_happens(self):
        saved = bc.probe_symlinks
        bc.probe_symlinks = lambda folder: "Operation not supported"
        try:
            with self.assertRaises(SystemExit) as cm:
                self.run_main()
        finally:
            bc.probe_symlinks = saved
        self.assertIn("cannot hold working symlinks", str(cm.exception))
        self.assertEqual(FakeConsole.log, [])
        self.assertFalse(os.path.exists(self.farm))

    def test_the_tuner_is_restarted_after_the_channels_are_written_so_they_are_live(self):
        out = self.run_main()
        self.assertEqual(self.signalled, ["x"])
        self.assertIn("restart its tuner", out)

    def test_no_restart_tuner_leaves_it_alone_and_says_how(self):
        out = self.run_main("--no-restart-tuner")
        self.assertEqual(self.signalled, [])
        self.assertIn("docker kill --signal=USR1", out)

    def test_a_failed_signal_is_reported_with_the_command_to_run(self):
        bc.restart_tuner = lambda container: False
        out = self.run_main()
        self.assertIn("could not signal x", out)
        self.assertIn("docker kill --signal=USR1 x", out)

    def test_a_dry_run_does_not_touch_the_tuner(self):
        self.run_main("--dry-run")
        self.assertEqual(self.signalled, [])

    def test_the_bump_folder_in_the_farm_is_real_and_a_new_clip_rebuilds_every_station(self):
        self.run_main()
        bump = os.path.join(self.farm, "bump")
        self.assertTrue(os.path.isdir(bump) and not os.path.islink(bump))
        self.assertEqual(os.readlink(os.path.join(bump, "b.mp4")), "/mnt/media/bump/b.mp4")
        touch(os.path.join(self.media, "bump", "b2.mp4"))
        self.run_main()
        self.assertEqual(self.rebuilt(), ["Comedy", "Drama"])               # every station uses bump

    def test_an_old_whole_folder_bump_link_is_replaced_on_the_next_run(self):
        os.makedirs(self.farm)
        os.symlink("/mnt/media/bump", os.path.join(self.farm, "bump"))
        out = self.run_main()
        self.assertIn("replaced the old whole-folder bump link", out)
        self.assertFalse(os.path.islink(os.path.join(self.farm, "bump")))
        self.assertTrue(os.path.islink(os.path.join(self.farm, "bump", "b.mp4")))

    def test_no_media_bump_folder_is_needed_when_clips_are_in_the_catalog(self):
        import shutil
        shutil.rmtree(os.path.join(self.media, "bump"))
        os.makedirs(os.path.join(self.farm, "bump"))
        touch(os.path.join(self.farm, "bump", "mine.mp4"))               # a clip the user dropped in
        self.run_main()
        self.assertEqual(self.rebuilt(), ["Comedy", "Drama"])
        self.assertTrue(os.path.isfile(os.path.join(self.farm, "bump", "mine.mp4")))
        self.assertFalse(os.path.islink(os.path.join(self.farm, "bump", "mine.mp4")))   # left as it was

    def test_without_any_bump_clips_it_stops_and_makes_the_folder_to_drop_them_in(self):
        import shutil
        shutil.rmtree(os.path.join(self.media, "bump"))
        argv = ["--media-root", self.media, "--target-root", "/mnt/media", "--catalog", self.catalog,
                "--tuner-config", self.tuner, "--fs42-container", "x"]
        with self.assertRaises(SystemExit) as cm:
            bc.main(argv + ["--dry-run"])
        self.assertIn("no channel mid cards", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.farm, "bump")))        # a dry run writes nothing
        with self.assertRaises(SystemExit) as cm:
            bc.main(argv)
        self.assertIn(os.path.join(self.farm, "bump"), str(cm.exception))
        self.assertTrue(os.path.isdir(os.path.join(self.farm, "bump")))
        self.assertEqual(self.rebuilt(), [])

    def test_dry_run_changes_nothing(self):
        out = self.run_main("--dry-run")
        self.assertFalse(os.path.exists(self.farm))
        self.assertEqual(FakeConsole.log, [])
        self.assertIn("dry run", out)


class ManifestTest(unittest.TestCase):
    """groups.toml: the same settings as the flags, in a file beside the catalog."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, "groups.toml")

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        return self.path

    def test_a_manifest_holds_settings_libraries_groups_and_a_genre_map(self):
        m = bc.load_manifest(self.write('''
media_root = "media"
target_root = "/mnt/media"
start_channel = 900
include_unclassified = true
tv_libraries = ["tanfo", "/abs/view=/data/view"]
[genre_map]
"sci-fi" = "Science Fiction"
[[group]]
playlist = "1980s Ads *"
to = "commercials"
[[group]]
show = "Old Cartoons"
[[group]]
video = "News/*x*"
to = "Retro Mix"
'''))
        d = m["defaults"]
        self.assertEqual(d["media_root"], os.path.join(self.root, "media"))     # host path: relative to the file
        self.assertEqual(d["target_root"], "/mnt/media")                        # container path: left alone
        self.assertEqual((d["start_channel"], d["include_unclassified"]), (900, True))
        self.assertEqual(d["tv_library"], [os.path.join(self.root, "tanfo"), "/abs/view=/data/view"])
        self.assertEqual(d["groups"], [("playlist", "1980s Ads *=commercials"), ("show", "Old Cartoons"),
                                       ("video", "News/*x*=Retro Mix")])
        self.assertEqual(m["genre_map"], {"sci-fi": "Science Fiction"})

    def test_mistakes_are_refused_with_a_message(self):
        for text in ('catalogue = "x"',                               # unknown key: a typo must not do nothing
                     'start_channel = "801"',                         # wrong type
                     'tv_libraries = "tanfo"',                        # not a list
                     '[[group]]\nshow = "A"\nplaylist = "B"',         # two kinds
                     '[[group]]\nto = "X"',                           # no kind
                     '[[group]]\nvideo = "A/*"',                      # video needs a destination
                     '[[group]]\nshow = "A"\nwhere = "X"',           # unknown group key
                     'not toml ['):
            with self.assertRaises(SystemExit, msg=text):
                bc.load_manifest(self.write(text))
        with self.assertRaises(SystemExit):
            bc.load_manifest(os.path.join(self.root, "missing.toml"))

    def test_flags_beat_the_manifest_and_lists_add_to_it(self):
        os.makedirs(os.path.join(self.root, "catalog"))
        os.makedirs(os.path.join(self.root, "tuner"))
        with open(os.path.join(self.root, "tuner", "config.json"), "w") as f:
            f.write("{}")
        self.write('''
start_channel = 900
tv_libraries = ["/lib/one"]
[[group]]
show = "From Manifest"
''')
        args = bc.parse_args(["--manifest", self.path, "--start-channel", "950",
                              "--tv-library", "/lib/two", "--show", "From Flag=Genre"])
        self.assertEqual(args.start_channel, 950)
        self.assertEqual(args.tv_library, ["/lib/one", "/lib/two"])
        self.assertEqual(args.groups, [("show", "From Manifest"), ("show", "From Flag=Genre")])
        self.assertEqual(bc.parse_args(["--manifest", self.path]).start_channel, 900)

    def test_catalog_defaults_to_a_folder_beside_the_manifest(self):
        self.write("")
        with self.assertRaises(SystemExit):                              # no catalog anywhere: refused
            bc.parse_args(["--manifest", self.path])
        os.makedirs(os.path.join(self.root, "catalog"))
        args = None
        with self.assertRaises(SystemExit) as cm:                        # a catalog, but no tuner config yet
            bc.parse_args(["--manifest", self.path])
        os.makedirs(os.path.join(self.root, "tuner"))
        with open(os.path.join(self.root, "tuner", "config.json"), "w") as f:
            f.write("{}")
        args = bc.parse_args(["--manifest", self.path])
        self.assertEqual(args.catalog, os.path.join(self.root, "catalog"))
        self.assertEqual(args.tuner_config, os.path.join(self.root, "tuner", "config.json"))

    def test_the_tuner_config_is_looked_for_in_config_beside_the_catalog_and_the_error_says_how_to_make_it(self):
        catalog = os.path.join(self.root, "catalog")
        os.makedirs(catalog)
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            with self.assertRaises(SystemExit):
                bc.parse_args(["--catalog", catalog])
        self.assertIn("Start the container once", out.getvalue())
        self.assertIn(os.path.join(self.root, "config", "tuner", "config.json"), out.getvalue())
        os.makedirs(os.path.join(self.root, "config", "tuner"))
        with open(os.path.join(self.root, "config", "tuner", "config.json"), "w") as f:
            f.write("{}")
        self.assertEqual(bc.parse_args(["--catalog", catalog]).tuner_config,
                         os.path.join(self.root, "config", "tuner", "config.json"))

    def test_an_earlier_layouts_tuner_folder_is_still_found(self):
        catalog = os.path.join(self.root, "fs42", "catalog")
        os.makedirs(catalog)
        os.makedirs(os.path.join(self.root, "fs42", "tuner"))
        with open(os.path.join(self.root, "fs42", "tuner", "config.json"), "w") as f:
            f.write("{}")
        self.assertEqual(bc.parse_args(["--catalog", catalog]).tuner_config,
                         os.path.join(self.root, "fs42", "tuner", "config.json"))

    def test_a_manifest_in_the_config_folder_finds_the_catalog_beside_it(self):
        os.makedirs(os.path.join(self.root, "config", "tuner"))
        os.makedirs(os.path.join(self.root, "catalog"))
        with open(os.path.join(self.root, "config", "tuner", "config.json"), "w") as f:
            f.write("{}")
        manifest = os.path.join(self.root, "config", "groups.toml")
        with open(manifest, "w") as f:
            f.write("")
        args = bc.parse_args(["--manifest", manifest])
        self.assertEqual(args.catalog, os.path.join(self.root, "catalog"))
        self.assertEqual(args.tuner_config, os.path.join(self.root, "config", "tuner", "config.json"))

    def test_without_a_manifest_the_flags_work_as_before(self):
        args = bc.parse_args(["--catalog", "/c", "--tuner-config", "/t.json"])
        self.assertEqual((args.catalog, args.tuner_config, args.groups, args.start_channel),
                         ("/c", "/t.json", None, 801))

class LinksOnlyTest(unittest.TestCase):
    """--links-only: get the files ready and stop. No console, no stations, no tuner config."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        touch(os.path.join(self.media, "bump", "b.mp4"))
        for show, genre in (("Show A", "Comedy"), ("Show B", "Drama")):
            d = os.path.join(self.media, "tvseries", show)
            touch(os.path.join(d, "tvshow.nfo"), tvshow_nfo(genre))
            touch(os.path.join(d, "Season 01", f"{show} - S01E01.mp4"))
        d = os.path.join(self.media, "tvseries", "No Genre Show")           # no genre in its NFO
        touch(os.path.join(d, "tvshow.nfo"), "<tvshow><title>x</title></tvshow>")
        touch(os.path.join(d, "Season 01", "e1.mp4"))
        self.catalog = os.path.join(self.root, "catalog")
        self.view = os.path.join(self.catalog, "tanfo", "youtube-nfo")       # a converted TubeArchivist library: channels
        for chan in ("NBC News", "Some Channel"):
            touch(os.path.join(self.view, chan, "tvshow.nfo"), "<tvshow><title>%s</title></tvshow>" % chan)
            touch(os.path.join(self.view, chan, "Season 2026", f"20260101_[abc]_{chan}.mp4"))
        self.farm = os.path.join(self.catalog, "by-genre")
        self._saved = bc.Console
        bc.Console = None                        # any use of the console would raise
        self.argv = ["--links-only", "--media-root", self.media, "--target-root", "/mnt/media",
                     "--catalog", self.catalog]

    def tearDown(self):
        bc.Console = self._saved
        shutil.rmtree(self.root)

    def run_main(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(bc.main([*self.argv, *extra]), 0)
        return buf.getvalue()

    def test_tag_folders_come_from_genres_and_not_from_youtube_channels(self):
        out = self.run_main()
        tags = sorted(os.listdir(self.farm))
        self.assertEqual(tags, ["Comedy", "Drama", "Unclassified", "bump"])               # no list per YouTube channel
        self.assertIn("farm: added", out)
        for tag in tags:                                                      # real folders, not folder links
            self.assertFalse(os.path.islink(os.path.join(self.farm, tag)), tag)
        self.assertFalse(os.path.exists(os.path.join(self.farm, "NBC News")))             # a channel is added by hand

    def test_converted_libraries_are_not_listed_by_channel_and_an_old_flat_view_is_ignored(self):
        other = os.path.join(self.catalog, "tanfo", "YT Archive-nfo")
        touch(os.path.join(other, "Old Channel", "tvshow.nfo"), "<tvshow><title>Old</title></tvshow>")
        touch(os.path.join(other, "Old Channel", "Season 2020", "20200101_[zzz]_o.mp4"))
        touch(os.path.join(self.catalog, "tanfo", "Flat Channel", "tvshow.nfo"), "<tvshow><title>F</title></tvshow>")
        touch(os.path.join(self.catalog, "tanfo", "Flat Channel", "Season 1", "f.mp4"))
        self.run_main()
        tags = sorted(os.listdir(self.farm))
        for channel in ("Old Channel", "NBC News", "Flat Channel"):
            self.assertNotIn(channel, tags)

    def test_no_tuner_config_and_no_console_are_needed(self):
        self.run_main()                          # bc.Console is None: touching it would have raised
        self.assertFalse(os.path.exists(os.path.join(self.root, "config")))

    def test_a_second_run_adds_nothing_and_a_new_channel_is_not_listed_on_its_own(self):
        self.run_main()
        self.assertIn("added 0 links", self.run_main())
        touch(os.path.join(self.view, "New Channel", "tvshow.nfo"), "<tvshow><title>New</title></tvshow>")
        touch(os.path.join(self.view, "New Channel", "Season 2026", "20260102_[def]_n.mp4"))
        self.run_main()
        self.assertNotIn("New Channel", os.listdir(self.farm))

    def test_what_you_arranged_by_hand_is_left_alone(self):
        self.run_main()
        mine = os.path.join(self.farm, "Comedy", "my_own.mp4")
        os.symlink("/somewhere/mine.mp4", mine)
        os.makedirs(os.path.join(self.farm, "My Tag"))
        self.run_main()
        self.assertTrue(os.path.islink(mine))
        self.assertTrue(os.path.isdir(os.path.join(self.farm, "My Tag")))

    def test_it_does_not_insist_on_bump_clips(self):
        shutil.rmtree(os.path.join(self.media, "bump"))
        out = self.run_main()                    # the generator proper would stop here; this just says so
        self.assertIn("no channel mid cards", out)

    def test_with_no_playlists_chosen_none_are_made(self):
        touch(os.path.join(self.view, "Playlists", "Picks.m3u8"), "#EXTM3U\n../NBC News/Season 2026/20260101_[abc]_NBC News.mp4\n")
        saved = os.environ.get("FS42_SETTINGS")
        os.environ["FS42_SETTINGS"] = os.path.join(self.root, "nothing.json")
        try:
            self.run_main()
        finally:
            if saved is None:
                os.environ.pop("FS42_SETTINGS")
            else:
                os.environ["FS42_SETTINGS"] = saved
        self.assertNotIn("Picks", os.listdir(self.farm))

    def test_a_missing_standard_library_is_not_repeated_on_every_pass(self):
        out = self.run_main()                    # there is no anime/ or movies/ here
        self.assertNotIn("library not found", out)

    def test_dry_run_writes_nothing(self):
        out = self.run_main("--dry-run")
        self.assertIn("would add", out)
        self.assertFalse(os.path.exists(self.farm))


class InsideTheContainerTest(unittest.TestCase):
    """FS42_IN_CONTAINER=1 (the image sets it): the container's own paths, and no docker."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.config = os.path.join(self.root, "config")
        os.makedirs(os.path.join(self.config, "tuner"))
        self.catalog = os.path.join(self.root, "catalog")
        os.makedirs(self.catalog)
        touch(os.path.join(self.config, "tuner", "config.json"), "{}")
        self._saved = (bc.CONTAINER_CATALOG, bc.CONTAINER_MANIFEST, bc.CONTAINER_TUNER_CONFIG,
                       os.environ.get("FS42_IN_CONTAINER"))
        bc.CONTAINER_CATALOG = self.catalog
        bc.CONTAINER_MANIFEST = os.path.join(self.config, "groups.toml")
        bc.CONTAINER_TUNER_CONFIG = os.path.join(self.config, "tuner", "config.json")
        os.environ["FS42_IN_CONTAINER"] = "1"

    def tearDown(self):
        bc.CONTAINER_CATALOG, bc.CONTAINER_MANIFEST, bc.CONTAINER_TUNER_CONFIG, env = self._saved
        if env is None:
            os.environ.pop("FS42_IN_CONTAINER", None)
        else:
            os.environ["FS42_IN_CONTAINER"] = env
        shutil.rmtree(self.root)

    def test_no_flags_needed_the_catalog_tuner_config_and_manifest_are_known(self):
        touch(bc.CONTAINER_MANIFEST, 'start_channel = 900\n[[group]]\nshow = "NBC News"\n')
        args = bc.parse_args([])
        self.assertEqual((args.catalog, args.tuner_config, args.start_channel, args.groups),
                         (self.catalog, bc.CONTAINER_TUNER_CONFIG, 900, [("show", "NBC News")]))

    def test_host_paths_in_a_manifest_written_on_the_host_are_ignored(self):
        touch(bc.CONTAINER_MANIFEST,
              'media_root = "/mnt/media"\ncatalog = "/srv/station-wrapper-43/catalog"\n'
              'tuner_config = "/srv/station-wrapper-43/config/tuner/config.json"\n'
              'tv_libraries = ["/srv/station-wrapper-43/catalog/tanfo=/app/catalog/tanfo"]\n')
        args = bc.parse_args([])
        self.assertEqual(args.catalog, self.catalog)
        self.assertEqual(args.tuner_config, bc.CONTAINER_TUNER_CONFIG)
        self.assertEqual(args.tv_library, ["/app/catalog/tanfo"])        # only the container side exists here

    def test_a_flag_still_beats_the_defaults(self):
        self.assertEqual(bc.parse_args(["--catalog", "/other"]).catalog, "/other")

    def test_inside_the_first_day_is_added_through_the_console_so_it_shows_the_schedule(self):
        import subprocess
        asked, ran = [], []

        class Console:
            def add_day(self, name):
                asked.append(name)

        def fake_run(cmd, **kw):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="added a day", stderr="")
        saved, subprocess.run = subprocess.run, fake_run
        try:
            bc.add_first_day(None, "Comedy", Console())              # inside: ask the console itself
            bc.add_first_day("station-wrapper-43", "Drama")                        # on a host: docker exec, as before
        finally:
            subprocess.run = saved
        self.assertEqual(asked, ["Comedy"])
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0][:3], ["docker", "exec", "station-wrapper-43"])      # a separate process is never used inside

    def test_the_console_call_polls_until_done_and_reports_a_failure(self):
        calls = []
        replies = iter([{"task_id": "t1"}, {"status": "running"}, {"status": "done"}])

        c = bc.Console("http://x")
        c._call = lambda method, path, body=None: (calls.append((method, path)), next(replies))[1]
        saved, bc.time.sleep = bc.time.sleep, lambda s: None
        try:
            c.add_day("Test Pattern")
            self.assertEqual(calls[0], ("POST", "/build/schedule/add_time/day/Test%20Pattern"))
            self.assertEqual(calls[-1], ("GET", "/build/schedule/add_time/status/t1"))
            failing = iter([{"task_id": "t2"}, {"status": "error", "log": "no catalog"}])
            c._call = lambda method, path, body=None: next(failing)
            with self.assertRaises(RuntimeError) as cm:
                c.add_day("X")
            self.assertIn("no catalog", str(cm.exception))
        finally:
            bc.time.sleep = saved

    def test_the_tuner_restart_signals_pid_1_which_hands_it_to_the_supervisor(self):
        import signal
        sent = []
        saved, os.kill = os.kill, lambda pid, sig: sent.append((pid, sig))
        try:
            self.assertTrue(bc.restart_tuner(None))
        finally:
            os.kill = saved
        self.assertEqual(sent, [(1, signal.SIGUSR1)])

    def test_what_it_creates_is_handed_to_the_catalog_owner_not_left_root_s(self):
        farm = os.path.join(self.catalog, "by-genre")
        os.makedirs(os.path.join(farm, "Comedy"))
        os.symlink("/mnt/media/x.mp4", os.path.join(farm, "Comedy", "x.mp4"))
        calls = []
        real_stat, real_lchown, real_geteuid = os.stat, os.lchown, os.geteuid

        def stat_as_other(path, *a, **k):          # the catalog "belongs to" uid 4242
            st = real_stat(path, *a, **k)
            if path == self.catalog:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 4242, 4343,
                                       st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
            return st
        os.stat, os.lchown, os.geteuid = stat_as_other, lambda p, u, g: calls.append((p, u, g)), lambda: 0
        try:
            changed = bc.adopt_owner(farm, self.catalog)
        finally:
            os.stat, os.lchown, os.geteuid = real_stat, real_lchown, real_geteuid
        self.assertEqual(changed, 3)       # Comedy/, the link, and by-genre/ itself; no link is followed
        self.assertTrue(all((u, g) == (4242, 4343) for _p, u, g in calls))
        self.assertIn(os.path.join(farm, "Comedy", "x.mp4"), [p for p, _u, _g in calls])   # the link itself

    def test_a_non_root_run_changes_nothing(self):
        saved, os.geteuid = os.geteuid, lambda: 1000
        try:
            self.assertEqual(bc.adopt_owner(self.catalog, self.catalog), 0)
        finally:
            os.geteuid = saved

    def test_outside_the_container_nothing_changes(self):
        os.environ.pop("FS42_IN_CONTAINER")
        self.assertFalse(bc.in_container())
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                bc.parse_args([])                      # --catalog is still required on a host


if __name__ == "__main__":
    unittest.main()
