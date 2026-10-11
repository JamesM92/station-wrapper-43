"""Tests for files.py: browse, build lists, symlinks only, nothing written outside the lists.

Run: python3 test_files.py
"""
import os
import shutil
import tempfile
import unittest

import files
from files import FileError, Files


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        self.catalog = os.path.join(self.root, "catalog")
        touch(os.path.join(self.media, "movies", "Film", "Film.mkv"), "VIDEO")
        touch(os.path.join(self.media, "movies", "Film", "Film.nfo"), "<movie/>")
        touch(os.path.join(self.media, "movies", "Film", "poster.jpg"))
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e1.mp4"))
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e2.mp4"))
        touch(os.path.join(self.media, "anime", "Show", "tvshow.nfo"))
        touch(os.path.join(self.media, ".hidden", "x.mp4"))
        touch(os.path.join(self.media, "@eaDir", "x.mp4"))
        os.makedirs(os.path.join(self.catalog, "by-genre"))
        self.f = Files(self.media, self.catalog)
        self.tags = os.path.join(self.catalog, "by-genre")

    def tearDown(self):
        shutil.rmtree(self.root)

    def names(self, listing):
        return [e["name"] for e in listing["entries"]]


class BrowseTest(Base):
    def test_lists_folders_first_and_hides_dotfiles_and_nas_junk(self):
        out = self.f.list("media", "")
        self.assertEqual(self.names(out), ["anime", "movies"])
        self.assertTrue(all(e["kind"] == "dir" for e in out["entries"]))

    def test_files_say_whether_they_are_videos_and_how_big(self):
        out = self.f.list("media", "movies/Film")
        by = {e["name"]: e for e in out["entries"]}
        self.assertTrue(by["Film.mkv"]["video"])
        self.assertEqual(by["Film.mkv"]["size"], 5)
        self.assertEqual(sorted(by), ["Film.mkv"])                     # the .nfo and the poster are not shown

    def test_links_show_where_they_point_and_whether_it_is_still_there(self):
        os.symlink(os.path.join(self.media, "movies", "Film", "Film.mkv"), os.path.join(self.tags, "good.mkv"))
        os.symlink("/nowhere/at/all.mkv", os.path.join(self.tags, "dead.mkv"))
        by = {e["name"]: e for e in self.f.list("tags", "")["entries"]}
        self.assertTrue(by["good.mkv"]["ok"])
        self.assertFalse(by["dead.mkv"]["ok"])
        self.assertEqual(by["dead.mkv"]["link"], "/nowhere/at/all.mkv")

    def test_paths_that_try_to_leave_are_refused(self):
        for bad in ("..", "../x", "movies/../..", "/etc", "a\\b", "movies//Film", "./movies"):
            with self.assertRaises(FileError, msg=bad):
                self.f.list("media", bad)

    def test_a_folder_link_that_leaves_the_place_is_refused(self):
        outside = os.path.join(self.root, "secret")
        touch(os.path.join(outside, "s.mp4"))
        os.symlink(outside, os.path.join(self.media, "escape"))
        with self.assertRaises(FileError) as cm:
            self.f.list("media", "escape")
        self.assertEqual(cm.exception.code, 403)

    def test_unknown_places_and_missing_folders(self):
        with self.assertRaises(FileError):
            self.f.list("etc", "")
        with self.assertRaises(FileError) as cm:
            self.f.list("media", "nope")
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(FileError):
            self.f.list("pool", "")                      # the pool is not there yet

    def test_a_long_listing_is_cut_and_says_so(self):
        saved, files.MAX_ENTRIES = files.MAX_ENTRIES, 2
        try:
            for i in range(5):
                touch(os.path.join(self.media, "many", f"{i}.mp4"))
            out = self.f.list("media", "many")
        finally:
            files.MAX_ENTRIES = saved
        self.assertEqual(len(out["entries"]), 2)
        self.assertTrue(out["truncated"])


class OneMediaViewTest(Base):
    """The page shows ONE media view: the pool when there is one (else the real folders), with the readable
    YouTube view as a folder in it, and only folders and videos."""

    def pool(self):
        os.makedirs(os.path.join(self.catalog, "media-links", "anime", "Show"))
        os.symlink(os.path.join(self.media, "anime", "Show", "Part1", "e1.mp4"),
                   os.path.join(self.catalog, "media-links", "anime", "Show", "e1.mp4"))
        touch(os.path.join(self.catalog, "media-links", "anime", "Show", "e1.nfo"))

    def tanfo(self, lib="youtube", enabled=False):
        """A converted library: <media>/<lib> turned into catalog/tanfo/<lib>-nfo (and the settings that say so)."""
        view = os.path.join(self.catalog, "tanfo", lib + "-nfo")
        for chan in ("NBC News", "Some Channel"):
            touch(os.path.join(view, chan, "tvshow.nfo"))
            touch(os.path.join(view, chan, "Season 2026", f"20260101_[abc]_{chan}.mp4"))
            touch(os.path.join(view, chan, "poster.jpg"))
        touch(os.path.join(view, "Playlists", "Picks.m3u8"))
        libs = [*getattr(self, "libs", []), os.path.join(self.media, lib)]
        self.libs = libs
        self.f.ta = lambda: {"enabled": enabled, "libraries": libs}

    def test_with_no_pool_the_real_media_folders_are_shown(self):
        self.assertEqual(self.f.info()["media_source"], "media")
        self.assertEqual(self.names(self.f.list("media", "")), ["anime", "movies"])

    def test_an_empty_pool_does_not_hide_the_media(self):
        os.makedirs(os.path.join(self.catalog, "media-links"))                     # made at start, not filled yet
        self.assertEqual(self.f.info()["media_source"], "media")
        self.assertEqual(self.names(self.f.list("media", "")), ["anime", "movies"])
        self.tanfo()
        self.assertEqual(self.names(self.f.list("media", "")), ["anime", "movies", "youtube-nfo"])

    def test_with_a_pool_the_pool_is_what_is_shown_and_it_is_just_called_media(self):
        self.pool()
        info = self.f.info()
        self.assertEqual((info["media_source"], info["roots"]["media"]), ("pool", True))
        self.assertEqual(self.names(self.f.list("media", "")), ["anime"])               # the pool's layout, not the raw folders
        self.assertEqual(self.names(self.f.list("media", "anime/Show")), ["e1.mp4"])

    def test_a_converted_library_is_a_folder_named_after_it_with_nfo_added(self):
        self.pool()
        self.tanfo()
        top = self.f.list("media", "")
        self.assertEqual(self.names(top), ["anime", "youtube-nfo"])
        self.assertEqual([e["kind"] for e in top["entries"]], ["dir", "dir"])
        inside = self.f.list("media", "youtube-nfo")
        self.assertEqual(self.names(inside), ["Playlists", "NBC News", "Some Channel"])   # the playlists show as a folder, first
        self.assertEqual(inside["path"], "youtube-nfo")                                # the path the page used is reported back
        self.assertEqual(self.names(self.f.list("media", "youtube-nfo/NBC News/Season 2026")), ["20260101_[abc]_NBC News.mp4"])

    def test_a_channels_shorts_are_one_choice_that_keeps_following_new_shorts_and_a_playlist_is_a_source(self):
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "follows.json"))
        self.tanfo()
        libs = self.libs
        self.f.ta = lambda: {"enabled": False, "libraries": libs}
        view = os.path.join(self.catalog, "tanfo", "youtube-nfo", "NBC News")
        touch(os.path.join(view, "Season 12026", "short1.mp4"))
        touch(os.path.join(view, "Season 22026", "live1.mp4"))
        shorts = self.f.add_show("youtube-nfo/NBC News", types=["shorts"])
        self.assertEqual(shorts["tag"], "NBC News - Shorts")
        linked = sorted(os.path.relpath(os.path.join(r, n), os.path.join(self.tags, shorts["tag"])) for r, _d, ns in os.walk(os.path.join(self.tags, shorts["tag"])) for n in ns if n.endswith(".mp4"))
        self.assertEqual(linked, [os.path.join("NBC News", "Season 12026", "short1.mp4")])   # no standard video, no stream
        touch(os.path.join(view, "Season 12027", "short2.mp4"))                                    # next year's shorts appear
        self.f.refresh_follows()
        self.assertTrue(os.path.islink(os.path.join(self.tags, shorts["tag"], "NBC News", "Season 12027", "short2.mp4")))
        touch(os.path.join(view, "Season 2027", "v.mp4"))
        self.f.refresh_follows()
        self.assertFalse(os.path.exists(os.path.join(self.tags, shorts["tag"], "NBC News", "Season 2027")))
        self.assertEqual(self.f.show_members()[shorts["tag"]], [{"root": "media", "path": "youtube-nfo/NBC News", "types": ["shorts"]}])
        with self.assertRaises(files.FileError):
            self.f.add_show("movies/Film/Film.mkv", types=["shorts"])                             # only a YouTube channel has kinds
        play = self.f.add_show("youtube-nfo/Playlists/Picks")
        self.assertEqual((play["tag"], play["kind"]), ("Picks", "playlist"))

    def test_several_libraries_each_show_up_under_their_own_name(self):
        self.tanfo("youtube")
        self.tanfo("YT Archive")
        self.assertEqual(self.names(self.f.list("media", "")), ["anime", "movies", "youtube-nfo", "YT Archive-nfo"])
        self.assertEqual(self.names(self.f.list("media", "YT Archive-nfo")), ["Playlists", "NBC News", "Some Channel"])

    def test_a_library_that_has_not_been_converted_yet_shows_no_folder(self):
        self.f.ta = lambda: {"enabled": True, "libraries": [os.path.join(self.media, "youtube")]}
        self.assertNotIn("youtube-nfo", self.names(self.f.list("media", "")))

    def test_the_raw_tubearchivist_folders_are_hidden_once_their_readable_versions_are_on(self):
        touch(os.path.join(self.media, "youtube", "UCaaaaaaaaaaaaaaaaaaaaaa", "v.mp4"))
        touch(os.path.join(self.media, "My Videos", "UCbbbbbbbbbbbbbbbbbbbbbb", "v.mp4"))
        self.tanfo("youtube")
        self.tanfo("My Videos")
        self.assertIn("youtube", self.names(self.f.list("media", "")))                   # the conversion is off: show it
        libs = self.libs
        self.f.ta = lambda: {"enabled": True, "libraries": libs}
        names = self.names(self.f.list("media", ""))
        self.assertNotIn("youtube", names)                                             # on: the readable ones replace them
        self.assertNotIn("My Videos", names)
        self.assertIn("youtube-nfo", names)
        self.assertIn("My Videos-nfo", names)
        self.f.ta = lambda: {"enabled": True, "libraries": [os.path.join(self.media, "anime", "deeper")]}
        self.assertIn("youtube", self.names(self.f.list("media", "")))                   # only a top-level folder is hidden

    def test_the_original_stays_visible_until_its_converted_version_exists(self):
        touch(os.path.join(self.media, "youtube", "UCaaaaaaaaaaaaaaaaaaaaaa", "v.mp4"))
        self.f.ta = lambda: {"enabled": True, "libraries": [os.path.join(self.media, "youtube")]}
        self.assertIn("youtube", self.names(self.f.list("media", "")))               # nothing converted yet
        self.tanfo(enabled=True)
        self.assertNotIn("youtube", self.names(self.f.list("media", "")))            # now the readable one replaces it

    def test_a_folder_that_already_has_the_converted_name_keeps_it(self):
        os.makedirs(os.path.join(self.media, "youtube-nfo"))
        self.tanfo()
        names = self.names(self.f.list("media", ""))
        self.assertIn("youtube-nfo", names)
        self.assertIn("youtube-nfo (converted)", names)
        self.assertEqual(self.names(self.f.list("media", "youtube-nfo (converted)")), ["Playlists", "NBC News", "Some Channel"])

    def test_a_folder_that_is_just_one_movie_shows_as_the_movie(self):
        top = self.f.list("media", "movies")                       # movies/Film/Film.mkv + Film.nfo
        self.assertEqual([(e["label"], e["name"], e["kind"]) for e in top["entries"]],
                         [("Film", "Film/Film.mkv", "file")])
        self.assertTrue(top["entries"][0]["video"])
        touch(os.path.join(self.media, "movies", "Two Parter", "a.mkv"))
        touch(os.path.join(self.media, "movies", "Two Parter", "b.mkv"))
        touch(os.path.join(self.media, "movies", "Saga", "Part 1", "p1.mkv"))
        shown = {(e.get("label") or e["name"]): e["kind"] for e in self.f.list("media", "movies")["entries"]}
        self.assertEqual(shown, {"Saga": "dir", "Two Parter": "dir", "Film": "file"})       # more than one video, or sub-folders: still folders
        self.assertEqual(self.names(self.f.list("media", "anime/Show")), ["Part1"])            # a show's season is not flattened

    def test_adding_such_a_movie_links_just_the_movie_without_its_folder(self):
        self.f.create_tag("Films")
        out = self.f.add("Films", [{"root": "media", "path": "movies/Film/Film.mkv"}])
        self.assertEqual(out["added"], 2)                          # the video and its .nfo
        self.assertEqual(sorted(os.listdir(os.path.join(self.tags, "Films"))), ["Film.mkv", "Film.nfo"])

    def test_the_lists_themselves_are_never_flattened(self):
        self.f.create_tag("One")
        self.f.add("One", [{"root": "media", "path": "movies/Film/Film.mkv"}])
        self.assertEqual([(e["name"], e["kind"]) for e in self.f.list("tags", "")["entries"]], [("One", "dir")])

    def test_only_folders_and_videos_are_shown_in_any_view(self):
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e1.nfo"))
        touch(os.path.join(self.media, "anime", "Show", "Part1", "cover.jpg"))
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e1.en.vtt"))
        self.assertEqual(self.names(self.f.list("media", "anime/Show/Part1")), ["e1.mp4", "e2.mp4"])
        self.f.create_tag("Mix")
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.assertEqual(self.names(self.f.list("tags", "Mix/Show/Part1")), ["e1.mp4", "e2.mp4"])    # the nfo links are there, hidden
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Mix", "Show", "tvshow.nfo")))

    def test_adding_from_a_converted_folder_works_and_keeps_its_name(self):
        self.pool()
        self.tanfo()
        self.f.create_tag("News")
        out = self.f.add("News", [{"root": "media", "path": "youtube-nfo/NBC News"}])
        self.assertEqual(out["added"], 2)                                              # the video and the channel's tvshow.nfo
        link = os.path.join(self.tags, "News", "NBC News", "Season 2026", "20260101_[abc]_NBC News.mp4")
        self.assertTrue(os.path.islink(link))
        self.f.add("News", [{"root": "media", "path": "youtube-nfo"}])                 # the whole folder: named after it
        self.assertTrue(os.path.isdir(os.path.join(self.tags, "News", "youtube-nfo", "Some Channel")))

    def test_adding_from_the_pool_points_at_the_real_file(self):
        self.pool()
        self.f.create_tag("Mix")
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.assertEqual(os.readlink(os.path.join(self.tags, "Mix", "Show", "e1.mp4")),
                         os.path.join(self.media, "anime", "Show", "Part1", "e1.mp4"))        # not at the pool's link

    def test_paths_that_try_to_leave_a_converted_folder_are_refused(self):
        self.tanfo()
        for bad in ("youtube-nfo/..", "youtube-nfo/../..", "youtube-nfo/NBC News/../../.."):
            with self.assertRaises(FileError, msg=bad):
                self.f.list("media", bad)


class FollowTest(Base):
    """A folder added to a list stays followed: videos that appear in it later are linked in too."""

    def setUp(self):
        super().setUp()
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "config", "follows.json"))
        self.f.create_tag("Anime")
        self.show = os.path.join(self.tags, "Anime", "Show", "Part1")

    def test_a_followed_folder_gets_its_new_videos(self):
        out = self.f.add("Anime", [{"root": "media", "path": "anime/Show"}], follow=True)
        self.assertEqual(out["following"], ["Show"])
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        touch(os.path.join(self.media, "anime", "Show", "S2", "e1.mp4"))            # a new season too
        self.assertEqual(self.f.refresh_follows(), {"added": 2, "checked": 1})
        self.assertTrue(os.path.islink(os.path.join(self.show, "e3.mp4")))
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Anime", "Show", "S2", "e1.mp4")))
        self.assertEqual(self.f.refresh_follows()["added"], 0)                       # nothing new: nothing to do
        self.assertIsNone(self.f.follows()[0]["error"])

    def test_a_folder_added_without_following_is_a_snapshot(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show"}])
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.assertEqual(self.f.refresh_follows(), {"added": 0, "checked": 0})
        self.assertEqual(self.f.follows(), [])

    def test_a_single_video_is_never_followed(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show/Part1/e1.mp4"}], follow=True)
        self.assertEqual(self.f.follows(), [])

    def test_what_you_take_out_of_a_followed_folder_stays_out(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show"}], follow=True)
        self.f.remove("Anime", ["Show/Part1/e1.mp4"])
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e1.nfo"))
        self.f.refresh_follows()
        self.assertFalse(os.path.lexists(os.path.join(self.show, "e1.mp4")))        # not put back
        self.assertTrue(os.path.islink(os.path.join(self.show, "e2.mp4")))
        self.assertEqual(self.f.follows()[0]["excluded"], 2)                         # the video and its nfo

    def test_removing_the_whole_followed_folder_stops_following_it(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show"}], follow=True)
        self.f.remove("Anime", ["Show"])
        self.assertEqual(self.f.follows(), [])
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.f.refresh_follows()
        self.assertFalse(os.path.exists(os.path.join(self.tags, "Anime", "Show")))

    def test_unfollow_keeps_the_links_and_stops_pulling(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show"}], follow=True)
        self.f.unfollow("Anime", "media", "anime/Show")
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.f.refresh_follows()
        self.assertTrue(os.path.islink(os.path.join(self.show, "e1.mp4")))
        self.assertFalse(os.path.lexists(os.path.join(self.show, "e3.mp4")))
        with self.assertRaises(FileError):
            self.f.unfollow("Anime", "media", "anime/Show")

    def test_renaming_or_deleting_the_list_takes_its_follows_along(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show"}], follow=True)
        self.f.rename_tag("Anime", "Cartoons")
        self.assertEqual([x["tag"] for x in self.f.follows()], ["Cartoons"])
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.f.refresh_follows()
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Cartoons", "Show", "Part1", "e3.mp4")))
        self.f.delete_tag("Cartoons")
        self.assertEqual(self.f.follows(), [])

    def test_a_folder_that_has_gone_is_reported_and_kept_for_when_it_comes_back(self):
        self.f.add("Anime", [{"root": "media", "path": "anime/Show"}], follow=True)
        hidden = os.path.join(self.root, "away")
        os.rename(os.path.join(self.media, "anime", "Show"), hidden)                 # an unmounted share, say
        self.f.refresh_follows()
        self.assertIn("not there", self.f.follows()[0]["error"])
        self.assertTrue(os.path.islink(os.path.join(self.show, "e1.mp4")))           # nothing was removed
        os.rename(hidden, os.path.join(self.media, "anime", "Show"))
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.assertEqual(self.f.refresh_follows()["added"], 1)
        self.assertIsNone(self.f.follows()[0]["error"])

    def test_a_converted_youtube_channel_can_be_followed_too(self):
        view = os.path.join(self.catalog, "tanfo", "youtube-nfo", "NBC News")
        touch(os.path.join(view, "tvshow.nfo"))
        touch(os.path.join(view, "Season 2026", "20260101_[a]_one.mp4"))
        libs = [os.path.join(self.media, "youtube")]
        self.f.ta = lambda: {"enabled": True, "libraries": libs}
        touch(os.path.join(view, "Season 2026", "20260101_[c]_three.mp4"))
        self.f.add("Anime", [{"root": "media", "path": "youtube-nfo/NBC News"}], follow=True)         # a whole channel can go in a list
        touch(os.path.join(view, "Season 2026", "20260102_[b]_two.mp4"))
        self.assertEqual(self.f.refresh_follows()["added"], 1)
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Anime", "NBC News", "Season 2026", "20260102_[b]_two.mp4")))
        self.assertFalse(any(e.get("show") for e in self.f.list("media", "youtube-nfo")["entries"]))      # a channel is not marked as a TV show


class PlaylistAndListTest(Base):
    """A TubeArchivist playlist can be added to a list like a folder, and a list can hold other lists."""

    def setUp(self):
        super().setUp()
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "config", "follows.json"))
        self.view = os.path.join(self.catalog, "tanfo", "youtube-nfo")
        for chan, vid in (("NBC News", "a"), ("Some Channel", "b")):
            touch(os.path.join(self.view, chan, "tvshow.nfo"))
            touch(os.path.join(self.view, chan, "Season 2026", f"20260101_[{vid}]_{chan}.mp4"))
            touch(os.path.join(self.view, chan, "Season 2026", f"20260101_[{vid}]_{chan}.nfo"))
        touch(os.path.join(self.view, "Playlists", "Picks.m3u8"),
              "#EXTM3U\n#EXTINF:-1,x\n../NBC News/Season 2026/20260101_[a]_NBC News.mp4\n"
              "#EXTINF:-1,y\n../Some Channel/Season 2026/20260101_[b]_Some Channel.mp4\n../Gone/x.mp4\n")
        libs = [os.path.join(self.media, "youtube")]
        self.f.ta = lambda: {"enabled": True, "libraries": libs}
        self.f.create_tag("Mix")

    def test_a_playlists_folder_lists_the_playlists(self):
        self.assertIn("Playlists", self.names(self.f.list("media", "youtube-nfo")))
        got = self.f.list("media", "youtube-nfo/Playlists")["entries"]
        self.assertEqual([(e["name"], e["kind"], e["videos"]) for e in got], [("Picks", "playlist", 3)])

    def test_adding_a_playlist_links_its_videos_into_a_folder_named_for_it(self):
        out = self.f.add("Mix", [{"root": "media", "path": "youtube-nfo/Playlists/Picks"}])
        self.assertEqual(out["added"], 4)                                   # two videos and their episode nfos; the missing one is skipped
        picks = os.path.join(self.tags, "Mix", "Picks")
        self.assertEqual(sorted(f for f in os.listdir(picks) if f.endswith(".mp4")),
                         ["20260101_[a]_NBC News.mp4", "20260101_[b]_Some Channel.mp4"])
        self.assertTrue(os.path.islink(os.path.join(picks, "20260101_[a]_NBC News.mp4")))

    def test_a_followed_playlist_picks_up_videos_added_to_it(self):
        self.f.add("Mix", [{"root": "media", "path": "youtube-nfo/Playlists/Picks"}], follow=True)
        touch(os.path.join(self.view, "Some Channel", "Season 2026", "20260102_[c]_new.mp4"))
        with open(os.path.join(self.view, "Playlists", "Picks.m3u8"), "a") as f:
            f.write("../Some Channel/Season 2026/20260102_[c]_new.mp4\n")
        self.assertEqual(self.f.refresh_follows()["added"], 1)
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Mix", "Picks", "20260102_[c]_new.mp4")))

    def test_a_list_can_hold_another_list_and_follows_it(self):
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.f.create_tag("Big")
        out = self.f.add("Big", [{"root": "tags", "path": "Mix"}], follow=True)
        self.assertEqual(out["following"], ["Mix"])
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e1.mp4")))
        self.assertEqual(os.readlink(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e1.mp4")),
                         os.path.join(self.media, "anime", "Show", "Part1", "e1.mp4"))       # the real file, not the other list's link
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])                       # a new video lands in Mix ...
        self.f.refresh_follows()
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e3.mp4")))   # ... and reaches Big
        self.f.remove("Mix", ["Show/Part1/e1.mp4"])                                           # a video leaves Mix ...
        self.f.refresh_follows()
        self.assertFalse(os.path.lexists(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e1.mp4")))  # ... and leaves Big
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e2.mp4")))

    def test_lists_cannot_go_round_in_a_circle(self):
        self.f.create_tag("Big")
        with self.assertRaises(FileError):
            self.f.add("Mix", [{"root": "tags", "path": "Mix"}], follow=True)
        self.f.add("Big", [{"root": "tags", "path": "Mix"}], follow=True)
        with self.assertRaises(FileError) as cm:
            self.f.add("Mix", [{"root": "tags", "path": "Big"}], follow=True)
        self.assertIn("circle", str(cm.exception))
        with self.assertRaises(FileError):
            self.f.add("Mix", [{"root": "tags", "path": "Nope"}], follow=True)
        with self.assertRaises(FileError):
            self.f.add("Mix", [{"root": "tags", "path": "Big/Mix"}], follow=True)

    def test_renaming_or_deleting_the_inner_list(self):
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.f.create_tag("Big")
        self.f.add("Big", [{"root": "tags", "path": "Mix"}], follow=True)
        self.f.rename_tag("Mix", "Shows")
        self.assertEqual([(x["tag"], x["path"]) for x in self.f.follows()], [("Big", "Shows")])
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.f.add("Shows", [{"root": "media", "path": "anime/Show"}])
        self.f.refresh_follows()
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e3.mp4")))   # same folder name in Big
        self.f.delete_tag("Shows")
        self.assertEqual(self.f.follows(), [])
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Big", "Mix", "Show", "Part1", "e1.mp4")))   # what was linked stays


class HiddenFilesTest(Base):
    def setUp(self):
        super().setUp()
        self.f.create_tag("Mix")
        self.mix = os.path.join(self.tags, "Mix")
        self.f.add("Mix", [{"root": "media", "path": "movies/Film"}, {"root": "media", "path": "anime/Show"}])

    def test_removing_a_video_takes_its_hidden_nfo_link_with_it(self):
        self.assertTrue(os.path.islink(os.path.join(self.mix, "Film", "Film.nfo")))
        self.f.remove("Mix", ["Film/Film.mkv"])
        self.assertFalse(os.path.lexists(os.path.join(self.mix, "Film", "Film.nfo")))
        self.assertFalse(os.path.exists(os.path.join(self.mix, "Film")))                # and the folder is gone

    def test_a_folder_with_no_videos_left_loses_its_hidden_links_and_is_removed(self):
        self.f.remove("Mix", ["Show/Part1/e1.mp4", "Show/Part1/e2.mp4"])
        self.assertFalse(os.path.exists(os.path.join(self.mix, "Show")))

    def test_a_folder_that_still_has_a_video_keeps_everything(self):
        self.f.remove("Mix", ["Show/Part1/e1.mp4"])
        self.assertTrue(os.path.islink(os.path.join(self.mix, "Show", "Part1", "e2.mp4")))
        self.assertTrue(os.path.islink(os.path.join(self.mix, "Show", "tvshow.nfo")))

    def test_a_real_file_you_put_there_keeps_its_folder(self):
        touch(os.path.join(self.mix, "Film", "my notes.txt"), "mine")
        self.f.remove("Mix", ["Film/Film.mkv"])
        self.assertTrue(os.path.isfile(os.path.join(self.mix, "Film", "my notes.txt")))
        self.assertFalse(os.path.lexists(os.path.join(self.mix, "Film", "Film.nfo")))      # the sidecar link still goes


class ListsTest(Base):
    def test_create_rename_delete(self):
        self.f.create_tag("Comedy")
        self.assertTrue(os.path.isdir(os.path.join(self.tags, "Comedy")))
        with self.assertRaises(FileError) as cm:
            self.f.create_tag("Comedy")
        self.assertEqual(cm.exception.code, 409)
        out = self.f.rename_tag("Comedy", "Funny")
        self.assertIn("station", out["note"])
        self.assertEqual(os.listdir(self.tags), ["Funny"])
        self.f.delete_tag("Funny")
        self.assertEqual(os.listdir(self.tags), [])

    def test_a_list_name_is_one_plain_folder_name(self):
        for bad in ("", "  ", "a/b", "..", ".hidden", "a\\b", None, 5, "x" * 300):
            with self.assertRaises(FileError, msg=repr(bad)):
                self.f.create_tag(bad)
        self.assertEqual(os.listdir(self.tags), [])
        self.f.create_tag("  NBC News  ")                 # spaces around are trimmed, inside kept
        self.assertEqual(os.listdir(self.tags), ["NBC News"])

    def test_a_list_that_is_a_folder_link_cannot_be_changed(self):
        os.symlink(self.media, os.path.join(self.tags, "Linked"))
        for call in (lambda: self.f.add("Linked", [{"root": "media", "path": "movies"}]),
                     lambda: self.f.delete_tag("Linked"), lambda: self.f.rename_tag("Linked", "X")):
            with self.assertRaises(FileError):
                call()
        self.assertTrue(os.path.isfile(os.path.join(self.media, "movies", "Film", "Film.mkv")))

    def test_deleting_a_list_refuses_when_it_holds_a_real_file(self):
        self.f.create_tag("Mine")
        touch(os.path.join(self.tags, "Mine", "my own clip.mp4"), "REAL")
        with self.assertRaises(FileError) as cm:
            self.f.delete_tag("Mine")
        self.assertIn("real files", str(cm.exception))
        self.assertTrue(os.path.isfile(os.path.join(self.tags, "Mine", "my own clip.mp4")))

    def test_deleting_a_list_removes_its_links_and_never_what_they_point_at(self):
        self.f.create_tag("Both")
        self.f.add("Both", [{"root": "media", "path": "movies/Film"}, {"root": "media", "path": "anime/Show"}])
        self.f.delete_tag("Both")
        self.assertFalse(os.path.exists(os.path.join(self.tags, "Both")))
        self.assertTrue(os.path.isfile(os.path.join(self.media, "movies", "Film", "Film.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.media, "anime", "Show", "Part1", "e1.mp4")))

    def test_a_missing_list_is_a_404(self):
        with self.assertRaises(FileError) as cm:
            self.f.add("Nope", [{"root": "media", "path": "movies"}])
        self.assertEqual(cm.exception.code, 404)


class AddTest(Base):
    def setUp(self):
        super().setUp()
        self.f.create_tag("Mix")
        self.mix = os.path.join(self.tags, "Mix")

    def link_targets(self):
        out = {}
        for r, d, fs in os.walk(self.mix):
            for n in fs + d:
                p = os.path.join(r, n)
                if os.path.islink(p):
                    out[os.path.relpath(p, self.mix)] = os.readlink(p)
        return out

    def test_one_video_is_linked_with_its_nfo_but_not_its_poster(self):
        out = self.f.add("Mix", [{"root": "media", "path": "movies/Film/Film.mkv"}])
        self.assertEqual(out["added"], 2)
        self.assertEqual(self.link_targets(), {
            "Film.mkv": os.path.join(self.media, "movies", "Film", "Film.mkv"),
            "Film.nfo": os.path.join(self.media, "movies", "Film", "Film.nfo")})

    def test_a_folder_keeps_its_name_and_layout_so_two_shows_do_not_collide(self):
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.assertEqual(sorted(self.link_targets()),
                         ["Show/Part1/e1.mp4", "Show/Part1/e2.mp4", "Show/tvshow.nfo"])
        for p in ("Show", "Show/Part1"):
            self.assertTrue(os.path.isdir(os.path.join(self.mix, p)))
            self.assertFalse(os.path.islink(os.path.join(self.mix, p)))       # real folders, never folder links

    def test_nothing_is_ever_copied(self):
        self.f.add("Mix", [{"root": "media", "path": ""}])
        for r, d, fs in os.walk(self.mix):
            for n in fs:
                self.assertTrue(os.path.islink(os.path.join(r, n)), n)

    def test_adding_again_adds_nothing_and_a_new_file_is_picked_up(self):
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        again = self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.assertEqual((again["added"], again["already_there"]), (0, 3))
        touch(os.path.join(self.media, "anime", "Show", "Part1", "e3.mp4"))
        self.assertEqual(self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])["added"], 1)

    def test_a_link_from_the_pool_keeps_pointing_at_the_real_file(self):
        pool = os.path.join(self.catalog, "media-links")
        os.makedirs(os.path.join(pool, "movies"))
        real = os.path.join(self.media, "movies", "Film", "Film.mkv")
        os.symlink(real, os.path.join(pool, "movies", "Film.mkv"))
        self.f.add("Mix", [{"root": "pool", "path": "movies/Film.mkv"}])
        self.assertEqual(os.readlink(os.path.join(self.mix, "Film.mkv")), real)     # one hop, not the pool link

    def test_what_you_already_arranged_is_kept(self):
        os.symlink("/somewhere/else.mp4", os.path.join(self.mix, "Film.mkv"))
        out = self.f.add("Mix", [{"root": "media", "path": "movies/Film/Film.mkv"}])
        self.assertEqual(os.readlink(os.path.join(self.mix, "Film.mkv")), "/somewhere/else.mp4")
        self.assertTrue(any("kept" in n for n in out["notes"]))

    def test_nothing_is_written_through_a_folder_link(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.mix, "Show"))          # you linked a whole folder in
        out = self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        self.assertEqual(out["added"], 0)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertTrue(any("links are never written through" in n for n in out["notes"]))

    def test_only_the_media_the_pool_and_the_view_can_be_sources(self):
        for root in ("tags", "etc", None, 5):
            with self.assertRaises(FileError, msg=str(root)):
                self.f.add("Mix", [{"root": root, "path": ""}])
        with self.assertRaises(FileError):
            self.f.add("Mix", [{"root": "media", "path": "../.."}])
        with self.assertRaises(FileError):
            self.f.add("Mix", [{"root": "media", "path": "movies/Film/poster.jpg"}])     # not a video
        with self.assertRaises(FileError):
            self.f.add("Mix", [])
        with self.assertRaises(FileError):
            self.f.add("Mix", ["movies"])

    def test_a_folder_link_that_leaves_the_media_is_not_a_source(self):
        outside = os.path.join(self.root, "secret")
        touch(os.path.join(outside, "s.mp4"))
        os.symlink(outside, os.path.join(self.media, "escape"))
        with self.assertRaises(FileError):
            self.f.add("Mix", [{"root": "media", "path": "escape"}])
        self.assertEqual(self.link_targets(), {})

    def test_too_many_files_is_refused_not_half_done(self):
        saved, files.MAX_LINKS = files.MAX_LINKS, 2
        try:
            with self.assertRaises(FileError):
                self.f.add("Mix", [{"root": "media", "path": "anime/Show"}])
        finally:
            files.MAX_LINKS = saved
        self.assertEqual(self.link_targets(), {})


class RemoveTest(Base):
    def setUp(self):
        super().setUp()
        self.f.create_tag("Mix")
        self.mix = os.path.join(self.tags, "Mix")
        self.f.add("Mix", [{"root": "media", "path": "anime/Show"}, {"root": "media", "path": "movies/Film"}])

    def test_a_link_comes_out_and_the_media_stays(self):
        out = self.f.remove("Mix", ["Film/Film.mkv"])
        self.assertEqual(out["removed"], 1)                                            # the one video the person sees
        self.assertFalse(os.path.lexists(os.path.join(self.mix, "Film", "Film.mkv")))
        self.assertFalse(os.path.lexists(os.path.join(self.mix, "Film", "Film.nfo")))   # its hidden nfo link went with it
        self.assertFalse(os.path.exists(os.path.join(self.mix, "Film")))                # leaving an empty folder, so that went too
        self.assertTrue(os.path.isfile(os.path.join(self.media, "movies", "Film", "Film.mkv")))

    def test_a_folder_of_links_comes_out_and_folders_left_with_no_videos_are_tidied(self):
        self.f.remove("Mix", ["Show/Part1/e1.mp4"])
        self.assertTrue(os.path.islink(os.path.join(self.mix, "Show", "Part1", "e2.mp4")))      # a video is left: kept
        self.assertTrue(os.path.islink(os.path.join(self.mix, "Show", "tvshow.nfo")))
        self.f.remove("Mix", ["Show/Part1/e2.mp4"])
        self.assertFalse(os.path.exists(os.path.join(self.mix, "Show")))            # no videos left: the hidden tvshow.nfo link goes too
        self.assertTrue(os.path.isdir(self.mix))                                   # the list itself stays

    def test_a_real_file_is_never_removed(self):
        touch(os.path.join(self.mix, "mine.mp4"), "REAL")
        out = self.f.remove("Mix", ["mine.mp4"])
        self.assertEqual((out["removed"], out["refused"]), (0, ["mine.mp4"]))
        self.assertTrue(os.path.isfile(os.path.join(self.mix, "mine.mp4")))

    def test_a_folder_with_a_real_file_in_it_loses_its_links_only(self):
        touch(os.path.join(self.mix, "Show", "Part1", "mine.mp4"), "REAL")
        out = self.f.remove("Mix", ["Show"])
        self.assertIn("Show/Part1/mine.mp4", out["refused"])
        self.assertTrue(os.path.isfile(os.path.join(self.mix, "Show", "Part1", "mine.mp4")))
        self.assertFalse(os.path.lexists(os.path.join(self.mix, "Show", "Part1", "e1.mp4")))

    def test_paths_cannot_escape_the_list(self):
        for bad in ("..", "../Mix", "../../etc", "", "/etc/passwd"):
            with self.assertRaises(FileError, msg=bad):
                self.f.remove("Mix", [bad])
        self.assertTrue(os.path.isdir(self.media))

    def test_a_folder_link_in_the_way_is_not_followed(self):
        outside = os.path.join(self.root, "outside")
        touch(os.path.join(outside, "keep.mp4"), "REAL")
        os.symlink(outside, os.path.join(self.mix, "Linked"))
        out = self.f.remove("Mix", ["Linked/keep.mp4"])
        self.assertEqual(out["removed"], 0)
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.mp4")))


class BackgroundAddTest(Base):
    def wait(self, job_id, timeout=20):
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            j = self.f.add_status(job_id)
            if j["state"] != "running":
                return j
            _t.sleep(0.02)
        self.fail("the add did not finish")

    def test_a_big_pick_is_added_in_the_background_with_progress_and_the_same_result(self):
        for c in range(17):                                              # seventeen channels
            for v in range(60):
                touch(os.path.join(self.media, "yt", f"Channel {c}", "Season 1", f"v{v}.mp4"))
        self.f.create_tag("Commercials")
        seen = []
        started = self.f.add_in_background("Commercials", [{"root": "media", "path": f"yt/Channel {c}"} for c in range(17)], done=seen.append)
        self.assertEqual(started["tag"], "Commercials")
        j = self.wait(started["id"])
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(j["result"]["added"], 17 * 60)
        self.assertEqual(seen, ["Commercials"])
        self.assertEqual(len([n for _r, _d, fs in os.walk(self.tags) for n in fs]), 17 * 60)

    def test_a_bad_pick_is_refused_at_once_or_reported_by_the_job(self):
        with self.assertRaises(FileError):
            self.f.add_in_background("Nothing Here", [{"root": "media", "path": "movies"}])        # no such list
        self.f.create_tag("Mix")
        j = self.wait(self.f.add_in_background("Mix", [{"root": "media", "path": "movies/Gone"}])["id"])
        self.assertEqual(j["state"], "error")
        self.assertIn("not there", j["message"])
        with self.assertRaises(FileError):
            self.f.add_status("nope")


class MoviesAndShowsTest(Base):
    def setUp(self):
        super().setUp()
        touch(os.path.join(self.media, "tv", "Sitcom", "Season 1", "e1.mp4"))
        touch(os.path.join(self.media, "tv", "Sitcom", "Season 1", "e2.mp4"))
        touch(os.path.join(self.media, "tv", "Sitcom", "tvshow.nfo"))

    def by(self, rel):
        return {e["name"]: e for e in self.f.list("media", rel)["entries"]}

    def test_a_folder_with_one_video_is_a_movie_even_with_extras_beside_it(self):
        touch(os.path.join(self.media, "movies", "Film2 (2020)", "Film2.mkv"))
        touch(os.path.join(self.media, "movies", "Film2 (2020)", "Extras", "making-of.mkv"))        # extras: not a season folder
        touch(os.path.join(self.media, "movies", "Film2 (2020)", "Subs", "en.srt"))
        by = self.by("movies")
        self.assertEqual(by["Film2 (2020)/Film2.mkv"]["label"], "Film2 (2020)")
        self.assertEqual(by["Film2 (2020)/Film2.mkv"]["kind"], "file")
        self.assertEqual(by["Film/Film.mkv"]["label"], "Film")
        self.assertFalse(any(e.get("show") for e in by.values()))

    def test_several_videos_or_a_season_folder_make_a_show(self):
        touch(os.path.join(self.media, "tv", "Flat", "e1.mkv"))
        touch(os.path.join(self.media, "tv", "Flat", "e2.mkv"))
        touch(os.path.join(self.media, "tv", "Seasoned", "Season 1", "e1.mkv"))
        touch(os.path.join(self.media, "tv", "Specials Only", "Specials", "x.mkv"))
        by = self.by("tv")
        for name in ("Flat", "Seasoned", "Specials Only"):
            self.assertTrue(by[name]["show"], name)
            self.assertEqual(by[name]["kind"], "dir")              # still a folder you can open
        self.assertNotIn("show", self.by("anime")["Show"])                 # a folder of parts with no season folders and no videos of its own is ordinary
        self.assertNotIn("show", self.by("")["movies"])               # a folder of movies is an ordinary folder

    def test_trailers_and_samples_beside_a_movie_do_not_make_it_a_show(self):
        touch(os.path.join(self.media, "movies", "Film", "Film-trailer.mkv"))
        touch(os.path.join(self.media, "movies", "Film", "Film-behindthescenes.mkv"))
        touch(os.path.join(self.media, "movies", "Film", "sample.mkv"))
        self.assertEqual(self.by("movies")["Film/Film.mkv"]["label"], "Film")

    def test_a_season_inside_a_show_is_not_itself_a_show_and_shows_can_go_in_lists_to_make_a_pool(self):
        inside = {e["name"]: e for e in self.f.list("media", "tv/Sitcom")["entries"]}
        self.assertNotIn("show", inside["Season 1"])
        self.f.create_tag("Mix")
        self.f.add("Mix", [{"root": "media", "path": "tv/Sitcom"}])                    # a whole show in a list: a pool of shows
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Mix", "Sitcom", "Season 1", "e1.mp4")))
        self.f.add("Mix", [{"root": "media", "path": "tv/Sitcom/Season 1"}])           # so is one season, or a single episode
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Mix", "Season 1", "e1.mp4")))

    def test_a_new_video_in_a_folder_is_noticed(self):
        self.assertEqual(files.classify(os.path.join(self.media, "movies", "Film"))[0], "movie")
        touch(os.path.join(self.media, "movies", "Film", "Film Part 2.mkv"))
        os.utime(os.path.join(self.media, "movies", "Film"), (1, 4_000_000_000))                 # a new modified time, as a real change would
        self.assertEqual(files.classify(os.path.join(self.media, "movies", "Film"))[0], "show")

    def test_a_show_becomes_a_source_that_is_kept_up_to_date_and_is_not_one_of_the_lists(self):
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "follows.json"))
        out = self.f.add_show("tv/Sitcom")
        self.assertEqual(out["tag"], "Sitcom")
        self.assertEqual(self.f.shows(), ["Sitcom"])
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Sitcom", "Sitcom", "Season 1", "e1.mp4")))
        self.assertTrue(self.by("tv")["Sitcom"]["source"])
        self.assertEqual([e["name"] for e in self.f.list("tags", "")["entries"]], [])               # not in the lists
        touch(os.path.join(self.media, "tv", "Sitcom", "Season 1", "e3.mp4"))
        self.f.refresh_follows()
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Sitcom", "Sitcom", "Season 1", "e3.mp4")))           # a new episode follows
        self.f.remove_show("Sitcom")
        self.assertEqual((self.f.shows(), os.path.exists(os.path.join(self.tags, "Sitcom"))), ([], False))
        self.assertTrue(os.path.isfile(os.path.join(self.media, "tv", "Sitcom", "Season 1", "e1.mp4")))  # media untouched

    def test_a_series_a_season_or_one_episode_can_each_be_a_source(self):
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "follows.json"))
        series = self.f.add_show("tv/Sitcom")
        self.assertEqual((series["tag"], series["kind"]), ("Sitcom", "series"))
        season = self.f.add_show("tv/Sitcom/Season 1")
        self.assertEqual((season["tag"], season["kind"]), ("Sitcom - Season 1", "season"))
        ep = self.f.add_show("tv/Sitcom/Season 1/e2.mp4")
        self.assertEqual((ep["tag"], ep["kind"]), ("Sitcom - Season 1 - e2", "episode"))
        movie = self.f.add_show("movies/Film/Film.mkv")                              # a movie in its own folder: named by the folder
        self.assertEqual((movie["tag"], movie["kind"]), ("Film", "episode"))
        self.assertEqual(self.f.show_kinds(), {"Sitcom": "series", "Sitcom - Season 1": "season", "Sitcom - Season 1 - e2": "episode", "Film": "episode"})
        self.assertEqual(sorted(os.listdir(os.path.join(self.tags, "Sitcom - Season 1 - e2"))), [".show", "e2.mp4"])
        self.assertEqual(self.f.add_show("tv/Sitcom")["tag"], "Sitcom")             # again: the same source, not a second one
        self.assertEqual(len(self.f.shows()), 4)

    def test_a_video_inside_a_list_can_be_picked_on_its_own_by_following_its_link_back_to_the_media(self):
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "follows.json"))
        self.f.create_tag("Mix")
        self.f.add("Mix", [{"root": "media", "path": "tv/Sitcom"}])
        self.assertEqual(self.f.media_path_of_list_item("Mix/Sitcom/Season 1/e1.mp4"), "tv/Sitcom/Season 1/e1.mp4")
        self.assertEqual(self.f.media_path_of_list_item("Mix/Sitcom/Season 1"), "tv/Sitcom/Season 1")
        out = self.f.add_show(self.f.media_path_of_list_item("Mix/Sitcom/Season 1/e1.mp4"))
        self.assertEqual(out["kind"], "episode")
        with self.assertRaises(files.FileError):
            self.f.media_path_of_list_item("Mix")                                   # a whole list is used as it is
        with self.assertRaises(files.FileError):
            self.f.media_path_of_list_item("Mix/Sitcom/nothing.mp4")

    def test_season_folders_tell_the_kind_of_video(self):
        self.assertEqual([files.season_type(x) for x in ("Season 2026", "Season 12026", "Season 22026", "Season 1", "Specials")],
                         ["videos", "shorts", "streams", None, None])
        self.assertTrue(files._type_ok("Season 12026/s.mp4", ["shorts"]))
        self.assertFalse(files._type_ok("Season 2026/a.mp4", ["shorts"]))
        self.assertTrue(files._type_ok("tvshow.nfo", ["shorts"]))                          # the channel's own files stay
        with self.assertRaises(files.FileError):
            self.f._clean_types(["clips"])
        self.assertEqual(self.f._clean_types(["streams", "videos"]), ["videos", "streams"])

    def test_several_sources_become_one_list_that_is_the_same_list_for_the_same_members(self):
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "follows.json"))
        self.f.create_tag("Mix2")
        members = [{"root": "media", "path": "tv/Sitcom/Season 1"}, {"root": "media", "path": "movies/Film/Film.mkv"},
                   {"root": "tags", "path": "Mix2"}]
        out = self.f.add_set(members)
        self.assertEqual(out["kind"], "set")
        self.assertTrue(os.path.islink(os.path.join(self.tags, out["tag"], "Season 1", "e1.mp4")))
        self.assertTrue(os.path.islink(os.path.join(self.tags, out["tag"], "Film.mkv")))
        self.assertEqual(self.f.add_set(list(reversed(members)))["tag"], out["tag"])      # the same members: the same set
        self.assertEqual(self.f.show_members()[out["tag"]], files.read_mark(os.path.join(self.tags, out["tag"], ".show"))["members"])
        self.assertNotIn(out["tag"], [e["name"] for e in self.f.list("tags", "")["entries"]])      # not among the lists
        with self.assertRaises(files.FileError):
            self.f.add_set([])
        # a second, different set that starts the same way gets a number, not an error
        other = self.f.add_set([{"root": "media", "path": "tv/Sitcom/Season 1"}, {"root": "media", "path": "movies/Film/Film.mkv"}])
        third = self.f.add_set([{"root": "media", "path": "tv/Sitcom/Season 1"}, {"root": "tags", "path": "Mix2"},
                                {"root": "media", "path": "movies/Film/Film.mkv"}, {"root": "media", "path": "tv/Sitcom"}])
        self.assertEqual(len({out["tag"], other["tag"], third["tag"]}), 3)

    def test_a_source_can_be_linked_in_the_background_and_a_build_can_wait_for_it(self):
        import time as _t
        self.f = Files(self.media, self.catalog, follows_path=os.path.join(self.root, "follows.json"))
        real = self.f.add
        gate = {"go": False}

        def slow(*a, **k):                                   # a series on a slow share
            while not gate["go"]:
                _t.sleep(0.01)
            return real(*a, **k)
        self.f.add = slow
        seen = []
        out = self.f.add_show("tv/Sitcom", wait=False, done=seen.append)
        self.assertEqual((out["tag"], out["linking"]), ("Sitcom", True))                 # it answered at once
        self.assertEqual(files.linking(), ["Sitcom"])
        self.assertFalse(files.wait_linked(timeout=0.2))                                 # a build would still be waiting
        gate["go"] = True
        self.assertTrue(files.wait_linked(timeout=10))
        self.assertEqual((files.linking(), seen), ([], ["Sitcom"]))
        self.assertTrue(os.path.islink(os.path.join(self.tags, "Sitcom", "Sitcom", "Season 1", "e1.mp4")))

    def test_a_whole_top_level_folder_is_not_a_source(self):
        for rel in ("tv", "movies", "anime"):
            with self.assertRaises(FileError) as cm:
                self.f.add_show(rel)
            self.assertIn("choose a series inside it", str(cm.exception))
        with self.assertRaises(FileError):
            self.f.add_show("tv/Nothing")

    def test_a_name_that_is_taken_gets_its_folder_in_front(self):
        touch(os.path.join(self.media, "anime", "Sitcom", "Season 1", "a.mp4"))
        first = self.f.add_show("tv/Sitcom")["tag"]
        second = self.f.add_show("anime/Sitcom")["tag"]
        self.assertEqual((first, second), ("Sitcom", "anime - Sitcom"))

    def test_a_marker_an_earlier_version_made_still_reads(self):
        self.f.add_show("tv/Sitcom")
        with open(os.path.join(self.tags, "Sitcom", files.SHOW_MARK), "w") as fh:
            fh.write("tv/Sitcom")
        self.assertEqual(self.f.show_kinds(), {"Sitcom": "series"})

    def test_a_list_with_the_same_name_is_never_taken_over(self):
        with self.assertRaises(FileError):
            self.f.add_show("")
        self.f.create_tag("Sitcom")
        out = self.f.add_show("tv/Sitcom")                              # the list keeps its name; the source gets its folder in front
        self.assertEqual(out["tag"], "tv - Sitcom")
        self.f.create_tag("tv - Other")
        touch(os.path.join(self.media, "tv", "Other", "Season 1", "x.mp4"))
        self.f.create_tag("Other")
        with self.assertRaises(FileError) as cm:
            self.f.add_show("tv/Other")                                 # both names taken: say so, take over nothing
        self.assertEqual(cm.exception.code, 409)
        with self.assertRaises(FileError):
            self.f.remove_show("Sitcom")                                  # a plain list is not removed through here


if __name__ == "__main__":
    unittest.main()
