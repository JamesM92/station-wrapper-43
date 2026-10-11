"""Tests for init.py: detection, the container check, and the starter manifest.

Run: python3 scripts/test_init.py  (needs defusedxml)
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest

import build_channels as bc
import init


def read(path):
    with open(path) as f:
        return f.read()


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class InitTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        touch(os.path.join(self.media, "bump", "b.mp4"))
        touch(os.path.join(self.media, "tvseries", "Show", "tvshow.nfo"), "<tvshow><genre>Comedy</genre></tvshow>")
        touch(os.path.join(self.media, "tvseries", "Show", "S1", "e1.mp4"))
        # a TubeArchivist-style view: channels with tvshow.nfo, and playlists
        touch(os.path.join(self.media, "tanfo", "Chan", "tvshow.nfo"), "<tvshow><genre>News</genre></tvshow>")
        touch(os.path.join(self.media, "tanfo", "Chan", "Season 1", "v.mp4"))
        touch(os.path.join(self.media, "tanfo", "Playlists", "Ads.m3u8"), "#EXTM3U\n")
        touch(os.path.join(self.media, "tanfo", "ta_nfo.py"))
        touch(os.path.join(self.media, "random stuff", "notes.txt"))        # not a library

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_init(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = init.main(["--media-root", self.media, "--fs42-container", "no-such-container-xyz", *argv])
        return rc, buf.getvalue()

    def test_finds_a_tv_library_that_is_not_one_of_the_standard_folders(self):
        libs = init.candidate_libraries(self.media)
        self.assertEqual([(l["label"], l["shows"], l["playlists"]) for l in libs], [("tanfo", 1, ["Ads"])])

    def test_a_folder_with_no_shows_or_playlists_is_not_a_library(self):
        self.assertNotIn("random stuff", [l["label"] for l in init.candidate_libraries(self.media)])

    def test_the_container_check_reports_paths_no_mount_covers(self):
        dests = ["/mnt/media", "/app/runtime"]
        self.assertEqual(init.uncovered(["/mnt/media", "/mnt/media/tanfo"], dests), [])
        self.assertEqual(init.uncovered(["/data/tanfo", "/mnt/media2"], dests), ["/data/tanfo", "/mnt/media2"])

    def test_it_writes_a_manifest_the_generator_can_read_back(self):
        out = os.path.join(self.root, "groups.toml")
        rc, text = self.run_init("--out", out)
        self.assertEqual(rc, 0)
        m = bc.load_manifest(out)
        self.assertEqual(m["defaults"]["media_root"], self.media)
        # the container sees the media under /mnt/media whatever this host's path is, so the
        # library is written with both: the host folder, and where the container finds it
        self.assertEqual(m["defaults"]["tv_library"], [os.path.join(self.media, "tanfo") + "=/mnt/media/tanfo"])
        self.assertEqual(m["defaults"]["target_root"], "/mnt/media")
        self.assertEqual(m["defaults"]["groups"], [])       # the examples are comments until you edit them
        self.assertIn("Comedy", text)                       # the channel preview
        self.assertIn("News", text)

    def test_when_the_host_path_is_the_container_path_nothing_needs_mapping(self):
        out = os.path.join(self.root, "g.toml")
        self.run_init("--out", out, "--target-root", self.media)
        m = bc.load_manifest(out)
        self.assertEqual(m["defaults"]["tv_library"], [os.path.join(self.media, "tanfo")])
        self.assertNotIn("target_root", m["defaults"])                         # left as a comment

    def test_a_library_below_the_media_root_keeps_its_place_in_the_container(self):
        touch(os.path.join(self.media, "yt", "deep", "Chan", "tvshow.nfo"), "<tvshow/>")
        touch(os.path.join(self.media, "nested", "x.txt"))
        libs = {l["label"]: l for l in init.candidate_libraries(self.media)}
        self.assertIn("tanfo", libs)                                           # one level down is what is looked for
        out = os.path.join(self.root, "g.toml")
        self.run_init("--out", out, "--target-root", "/data/share")
        got = bc.load_manifest(out)["defaults"]
        self.assertEqual(got["target_root"], "/data/share")
        self.assertEqual(got["tv_library"], [os.path.join(self.media, "tanfo") + "=/data/share/tanfo"])

    # ---- the container's mounts: the whole media folder, or one volume per folder
    def stub_mounts(self, destinations):
        saved = init.container_destinations
        init.container_destinations = lambda container: destinations
        self.addCleanup(setattr, init, "container_destinations", saved)

    def check(self):
        _, text = self.run_init("--out", os.path.join(self.root, "g.toml"))
        return text.split("Container check")[1].split("Symlink check")[0]

    def test_the_whole_media_folder_mounted_covers_every_library(self):
        self.stub_mounts(["/mnt/media", "/config", "/app/catalog"])
        checked = self.check()
        self.assertIn("every folder above is mounted", checked)
        self.assertNotIn("NOT VISIBLE", checked)

    def test_one_volume_per_folder_is_checked_folder_by_folder(self):
        # the fixture has tvseries, bump and a TV library "tanfo" on the host
        self.stub_mounts(["/mnt/media/tvseries", "/mnt/media/bump", "/mnt/media/tanfo"])
        checked = self.check()
        self.assertIn("every folder above is mounted", checked)

    def test_a_folder_that_has_no_volume_is_named(self):
        self.stub_mounts(["/mnt/media/tvseries", "/mnt/media/tanfo"])          # bump has no volume
        checked = self.check()
        self.assertIn("NOT VISIBLE: /mnt/media/bump", checked)
        self.assertNotIn("NOT VISIBLE: /mnt/media/tvseries", checked)
        self.assertNotIn("NOT VISIBLE: /mnt/media/tanfo", checked)
        self.assertIn("read-only volume", checked)

    def test_a_folder_of_the_host_that_the_container_cannot_see_is_not_blamed_on_a_neighbour(self):
        self.stub_mounts(["/mnt/media/tvserie"])                               # a different folder, similar name
        self.assertIn("NOT VISIBLE: /mnt/media/tvseries", self.check())

    def test_folders_that_are_not_on_the_host_are_not_demanded(self):
        shutil.rmtree(os.path.join(self.media, "tvseries"))                    # anime and movies were never here
        self.stub_mounts(["/mnt/media/bump", "/mnt/media/tanfo"])
        self.assertNotIn("NOT VISIBLE", self.check())

    def test_it_will_not_overwrite_without_force(self):
        out = os.path.join(self.root, "groups.toml")
        touch(out, "mine = 1\n")
        with self.assertRaises(SystemExit):
            self.run_init("--out", out)
        self.assertEqual(read(out), "mine = 1\n")
        self.run_init("--out", out, "--force")
        self.assertIn("media_root", read(out))

    def test_the_manifest_goes_in_the_config_folder_and_finds_the_catalog_without_naming_it(self):
        catalog = os.path.join(self.root, "data", "catalog")
        os.makedirs(catalog)
        _, text = self.run_init("--catalog", catalog)
        out = os.path.join(self.root, "data", "config", "groups.toml")
        self.assertTrue(os.path.exists(out))                      # config/ beside catalog/, made if needed
        self.assertNotIn("catalog =", read(out))
        self.assertIn("not there yet", text)                      # no tuner config until the container has started
        self.assertIn("start it once", text)
        touch(os.path.join(self.root, "data", "config", "tuner", "config.json"), "{}")
        _, text = self.run_init("--catalog", catalog, "--force")
        self.assertIn("Dashboard config: found", text)
        self.assertEqual(bc.parse_args(["--manifest", out]).catalog, catalog)

    def test_a_catalog_elsewhere_is_named_in_the_manifest(self):
        catalog = os.path.join(self.root, "somewhere", "my_catalog")
        os.makedirs(catalog)
        out = os.path.join(self.root, "groups.toml")
        self.run_init("--catalog", catalog, "--out", out)
        self.assertEqual(bc.load_manifest(out)["defaults"]["catalog"], catalog)

    def test_it_reports_a_catalog_that_cannot_hold_symlinks(self):
        saved = bc.probe_symlinks
        bc.probe_symlinks = lambda folder: "Operation not supported"
        try:
            _, text = self.run_init("--out", os.path.join(self.root, "g.toml"),
                                    "--catalog", os.path.join(self.root, "catalog"))
        finally:
            bc.probe_symlinks = saved
        self.assertIn("PROBLEM", text)
        self.assertIn("cannot hold working symlinks", text)
        self.assertIn("Fix this first", text)

    def test_a_good_setup_reports_symlinks_working_and_no_problem(self):
        os.makedirs(os.path.join(self.root, "catalog"))
        _, text = self.run_init("--out", os.path.join(self.root, "g.toml"),
                                "--catalog", os.path.join(self.root, "catalog"))
        self.assertIn("symlinks work", text)
        self.assertNotIn("PROBLEM", text)

    def test_it_finds_the_view_the_ta_nfo_service_builds_in_the_catalog(self):
        catalog = os.path.join(self.root, "data", "catalog")
        touch(os.path.join(catalog, "tanfo", "Chan", "tvshow.nfo"), "<tvshow><genre>News</genre></tvshow>")
        touch(os.path.join(catalog, "tanfo", "Chan", "S1", "v.mp4"))
        touch(os.path.join(catalog, "tanfo", "Playlists", "Ads.m3u8"), "#EXTM3U\n")
        out = os.path.join(self.root, "g.toml")
        saved = init.container_destinations
        init.container_destinations = lambda container: ["/somewhere/else"]    # a container that mounts neither
        try:
            _, text = self.run_init("--catalog", catalog, "--out", out)
        finally:
            init.container_destinations = saved
        self.assertIn("containers see it as /app/catalog/tanfo", text)
        checked = text.split("Container check")[1].split("Symlink check")[0]
        self.assertIn("NOT VISIBLE: /mnt/media", checked)                      # the container's own media mount is what is checked
        self.assertNotIn(self.media, checked)                                  # not this host's path, which the container never sees
        self.assertNotIn(f"NOT VISIBLE: {os.path.join(catalog, 'tanfo')}", checked)   # the view is judged by its container path
        libs = bc.load_manifest(out)["defaults"]["tv_library"]
        self.assertIn(os.path.join(catalog, "tanfo") + "=/app/catalog/tanfo", libs)
        # and the generator accepts that exact entry
        label, host, target = bc.parse_tv_library(os.path.join(catalog, "tanfo") + "=/app/catalog/tanfo", set())
        self.assertEqual((label, target), ("tanfo", "/app/catalog/tanfo"))

    def test_an_empty_catalog_view_is_not_reported(self):
        catalog = os.path.join(self.root, "catalog")
        os.makedirs(os.path.join(catalog, "tanfo"))
        self.assertIsNone(init.catalog_view(catalog))
        self.assertIsNone(init.catalog_view(None))

    def test_a_missing_media_root_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as cm:
            init.main(["--media-root", os.path.join(self.root, "nope")])
        self.assertIn("not a folder", str(cm.exception))

    def test_a_missing_bump_folder_is_called_out(self):
        shutil.rmtree(os.path.join(self.media, "bump"))
        _, text = self.run_init("--out", os.path.join(self.root, "g.toml"))
        self.assertIn("no channel mid cards", text)
        self.assertIn(os.path.join("by-genre", "bump"), text)

    def test_clips_in_the_catalogs_bump_folder_are_enough(self):
        shutil.rmtree(os.path.join(self.media, "bump"))
        bump = os.path.join(self.root, "catalog", "by-genre", "bump")
        os.makedirs(bump)
        open(os.path.join(bump, "x.mp4"), "w").close()
        _, text = self.run_init("--catalog", os.path.join(self.root, "catalog"),
                                "--out", os.path.join(self.root, "g.toml"))
        self.assertNotIn("no channel mid cards", text)

    def test_inside_the_container_it_needs_no_flags_and_writes_to_the_config_volume(self):
        catalog = os.path.join(self.root, "catalog")
        config = os.path.join(self.root, "config")
        os.makedirs(catalog)
        saved = (bc.CONTAINER_CATALOG, bc.CONTAINER_MANIFEST, os.environ.get("FS42_IN_CONTAINER"))
        bc.CONTAINER_CATALOG, bc.CONTAINER_MANIFEST = catalog, os.path.join(config, "groups.toml")
        os.environ["FS42_IN_CONTAINER"] = "1"
        try:
            rc, text = self.run_init()
        finally:
            bc.CONTAINER_CATALOG, bc.CONTAINER_MANIFEST = saved[0], saved[1]
            if saved[2] is None:
                os.environ.pop("FS42_IN_CONTAINER")
            else:
                os.environ["FS42_IN_CONTAINER"] = saved[2]
        self.assertEqual(rc, 0)
        self.assertIn("skipped: this is running inside the container", text)     # no docker to ask
        self.assertIn("fs42-channels build --dry-run", text)
        self.assertNotIn("scripts/build_channels.py", text)
        self.assertTrue(os.path.isfile(os.path.join(config, "groups.toml")))     # in /config, not beside the catalog


if __name__ == "__main__":
    unittest.main()
