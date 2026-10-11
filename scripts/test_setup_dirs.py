"""setup-dirs.sh creates the layout (a config folder and a catalog folder, under one local
root), moves an older layout only when asked, refuses places the symlinks and database can't
live, and finds what an earlier layout left behind in Docker.

Run: python3 scripts/test_setup_dirs.py   (needs bash)
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup-dirs.sh")


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


class SetupDirsBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        shutil.copy(SCRIPT, self.repo)
        self.local = os.path.join(self.root, "local")
        self.media = os.path.join(self.root, "media")
        os.makedirs(self.media)
        self.shim = os.path.join(self.root, "shim")
        os.makedirs(self.shim)

    def tearDown(self):
        shutil.rmtree(self.root)

    def env_file(self, **kv):
        with open(os.path.join(self.repo, ".env"), "w") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")

    def run_script(self, *args, shim=False):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FS42_")}
        if shim:
            env["PATH"] = self.shim + ":" + env["PATH"]
        out = subprocess.run(["bash", os.path.join(self.repo, "setup-dirs.sh"), *args],
                             capture_output=True, text=True, env=env, cwd=self.repo)
        return out.returncode, out.stdout + out.stderr

    def good_env(self, **extra):
        self.env_file(FS42_LOCAL_ROOT=self.local, FS42_MEDIA_ROOT=self.media, **extra)

    def make_shim(self, name, body):
        path = os.path.join(self.shim, name)
        write(path, "#!/bin/bash\n" + body)
        os.chmod(path, 0o755)

    def path(self, *parts):
        return os.path.join(self.local, *parts)


class LayoutTest(SetupDirsBase):
    def test_it_creates_the_whole_layout(self):
        self.good_env()
        rc, out = self.run_script()
        self.assertEqual(rc, 0, out)
        for d in ("config/runtime", "config/confs", "config/tuner", "catalog", "catalog/media-links"):
            self.assertTrue(os.path.isdir(self.path(d)), d)
        self.assertFalse(os.path.exists(self.path("fs42")))              # no longer a level in the layout
        self.assertFalse(os.path.exists(self.path("media-links")))        # the pool is inside the catalog
        self.assertEqual(os.listdir(self.path("catalog", "media-links")), [])   # probes cleaned up
        self.assertIn("config", out)
        self.assertIn("catalog", out)

    def test_running_it_again_changes_nothing(self):
        self.good_env()
        self.run_script()
        write(self.path("config/runtime/keep.db"), "data")
        rc, _ = self.run_script()
        self.assertEqual(rc, 0)
        self.assertEqual(read(self.path("config/runtime/keep.db")), "data")

    def test_the_catalog_and_the_config_can_each_be_put_elsewhere(self):
        cat, cfg = os.path.join(self.root, "elsewhere"), os.path.join(self.root, "cfg")
        self.good_env(FS42_CATALOG_ROOT=cat, FS42_CONFIG_ROOT=cfg)
        rc, out = self.run_script()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(os.path.join(cat, "media-links")))     # the pool follows the catalog
        self.assertTrue(os.path.isdir(os.path.join(cfg, "runtime")))
        self.assertFalse(os.path.exists(self.path("catalog")))
        self.assertFalse(os.path.exists(self.path("config")))


class MigrateTest(SetupDirsBase):
    def first_layout(self):
        """runtime/ and catalog/ straight under the root, confs on the NAS."""
        write(self.path("runtime/fs42_fluid.db"), "schedule")
        write(self.path("catalog/by-genre/.keep"))
        self.nas = os.path.join(self.root, "nas")
        write(os.path.join(self.nas, "confs/Comedy.json"), '{"station_conf": {}}')
        self.env_file(FS42_LOCAL_ROOT=self.local, FS42_MEDIA_ROOT=self.media, FS42_NAS_DATA=self.nas)

    def second_layout(self):
        """Everything under fs42/, and the pool beside it."""
        write(self.path("fs42/runtime/fs42_fluid.db"), "schedule")
        write(self.path("fs42/catalog/by-genre/.keep"))
        write(self.path("fs42/confs/Comedy.json"), "{}")
        write(self.path("fs42/tuner/config.json"), '{"channels": {"801": "Comedy"}}')
        write(self.path("media-links/tvseries/Show/e1.mp4"))
        self.good_env()

    def test_an_older_layout_is_not_touched_unless_asked(self):
        for layout, marker in ((self.first_layout, "runtime/fs42_fluid.db"), (self.second_layout, "fs42/runtime/fs42_fluid.db")):
            layout()
            rc, out = self.run_script()
            self.assertEqual(rc, 1, out)
            self.assertIn("--migrate", out)
            self.assertIn("EMPTY schedule", out)
            self.assertTrue(os.path.exists(self.path(marker)), marker)           # not moved
            self.assertFalse(os.path.exists(self.path("config")))                # nothing created
            shutil.rmtree(self.local)

    def test_migrating_the_first_layout_moves_the_database_and_copies_the_confs(self):
        self.first_layout()
        rc, out = self.run_script("--migrate")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.path("config/runtime/fs42_fluid.db")), "schedule")   # the data came along
        self.assertFalse(os.path.exists(self.path("runtime")))                          # moved, not copied
        self.assertTrue(os.path.exists(self.path("catalog/by-genre/.keep")))            # the catalog was already in place
        self.assertTrue(os.path.exists(self.path("config/confs/Comedy.json")))
        self.assertTrue(os.path.exists(os.path.join(self.nas, "confs/Comedy.json")))    # the original stays
        rc, _ = self.run_script("--migrate")                                            # and it is repeatable
        self.assertEqual(rc, 0)

    def test_migrating_the_second_layout_moves_everything_and_folds_the_pool_into_the_catalog(self):
        self.second_layout()
        rc, out = self.run_script("--migrate")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.path("config/runtime/fs42_fluid.db")), "schedule")
        self.assertTrue(os.path.exists(self.path("config/confs/Comedy.json")))
        self.assertIn('"801": "Comedy"', read(self.path("config/tuner/config.json")))   # channel numbers survive
        self.assertTrue(os.path.exists(self.path("catalog/by-genre/.keep")))
        self.assertTrue(os.path.exists(self.path("catalog/media-links/tvseries/Show/e1.mp4")))
        self.assertFalse(os.path.exists(self.path("fs42")))                             # the emptied level is removed
        self.assertFalse(os.path.exists(self.path("media-links")))
        rc, _ = self.run_script("--migrate")
        self.assertEqual(rc, 0)

    def test_migrate_leaves_a_catalog_that_lives_elsewhere_where_it_is(self):
        self.second_layout()
        other = os.path.join(self.root, "elsewhere")
        write(os.path.join(other, "by-genre/.keep"))
        self.good_env(FS42_CATALOG_ROOT=other)
        rc, out = self.run_script("--migrate")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(other, "by-genre/.keep")))
        self.assertTrue(os.path.exists(os.path.join(other, "media-links/tvseries/Show/e1.mp4")))   # the pool joins it
        self.assertTrue(os.path.exists(self.path("fs42/catalog/by-genre/.keep")))                  # the old local one is untouched

    def test_the_old_variable_name_is_carried_for_the_move_and_a_rename_is_demanded(self):
        write(self.path("runtime/fs42_fluid.db"), "schedule")
        self.env_file(FS42_SSD_ROOT=self.local, FS42_MEDIA_ROOT=self.media)
        rc, out = self.run_script("--migrate")
        self.assertEqual(rc, 1)                                                          # compose needs the new name
        self.assertIn("FS42_LOCAL_ROOT", out)
        self.assertIn("sed -i", out)
        self.assertTrue(os.path.exists(self.path("config/runtime/fs42_fluid.db")))       # but the data did move


class RefusalTest(SetupDirsBase):
    def stat_shim(self, path, kind):
        self.make_shim("stat", 'for a in "$@"; do last="$a"; done\n'
                               f'case "$last" in {path}) echo {kind};; *) exec /usr/bin/stat "$@";; esac\n')

    def test_a_catalog_that_cannot_hold_a_symlink_is_refused(self):
        self.good_env()
        self.make_shim("ln", 'echo "Operation not supported" >&2; exit 1\n')
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 1)
        self.assertIn("cannot hold working symlinks", out)
        self.assertIn("FS42_LOCAL_ROOT", out)

    def test_a_catalog_on_a_share_gets_a_note_when_links_still_work(self):
        self.good_env()
        self.stat_shim(self.path("catalog"), "cifs")
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("the catalog", out)
        self.assertIn("network share", out)

    def test_a_config_folder_on_a_share_is_refused_because_it_holds_the_database(self):
        self.good_env()
        self.stat_shim(self.path("config"), "smb2")
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 1)
        self.assertIn("schedule database", out)

    def test_a_missing_media_root_is_a_clear_error(self):
        self.env_file(FS42_LOCAL_ROOT=self.local, FS42_MEDIA_ROOT=os.path.join(self.root, "not-mounted"))
        rc, out = self.run_script()
        self.assertEqual(rc, 1)
        self.assertIn("isn't mounted", out)

    def test_no_local_root_and_a_bad_argument(self):
        self.env_file(FS42_MEDIA_ROOT=self.media)
        rc, out = self.run_script()
        self.assertNotEqual(rc, 0)
        self.assertIn("FS42_LOCAL_ROOT", out)
        self.good_env()
        self.assertEqual(self.run_script("--bogus")[0], 2)


class DockerLeftoversTest(SetupDirsBase):
    """Docker keeps a bind volume's path AND options from the day it is created."""

    def fake_docker(self, volumes=None, containers=(), compose=True):
        """A stand-in `docker`. volumes: {name: (device, options)}; containers: [(name, service)]."""
        self.state = os.path.join(self.root, "dockerstate")
        os.makedirs(self.state, exist_ok=True)
        write(os.path.join(self.state, "volumes.json"), json.dumps({k: list(v) for k, v in (volumes or {}).items()}))
        write(os.path.join(self.state, "containers.txt"), "".join(f"{n} {svc}\n" for n, svc in containers))
        write(os.path.join(self.state, "calls.log"))
        write(os.path.join(self.shim, "docker"), f'''#!/usr/bin/env python3
import json, os, sys
S = {self.state!r}
args = sys.argv[1:]
def log(): open(os.path.join(S, "calls.log"), "a").write(" ".join(args) + "\\n")
vols = lambda: json.load(open(os.path.join(S, "volumes.json")))
if args[:2] == ["compose", "version"]:
    sys.exit(0 if {compose!r} else 1)
if args[:2] == ["compose", "config"]:
    root = os.environ["FS42_LOCAL_ROOT"]
    want = {{"config": (root + "/config", "bind"), "catalog": (root + "/catalog", "bind"), "media": ("/media", "rbind")}}
    print(json.dumps({{"name": "station-wrapper-43", "volumes": {{k: {{"name": "station-wrapper-43_" + k, "driver_opts": {{"device": d, "o": o}}}}
                                                       for k, (d, o) in want.items()}}}}))
    sys.exit(0)
if args[:2] == ["compose", "down"]:
    log(); open(os.path.join(S, "containers.txt"), "w").close(); sys.exit(0)
if args[:2] == ["volume", "inspect"]:
    v = vols()
    if args[2] in v:
        print(v[args[2]][0] + "|" + v[args[2]][1]); sys.exit(0)
    sys.exit(1)
if args[:2] == ["volume", "ls"]:
    print("\\n".join(vols())); sys.exit(0)
if args[:2] == ["volume", "rm"]:
    log(); v = vols(); v.pop(args[2], None); json.dump(v, open(os.path.join(S, "volumes.json"), "w")); print(args[2]); sys.exit(0)
if args[0] == "rm":
    log(); name = args[-1]
    lines = [l for l in open(os.path.join(S, "containers.txt")).read().splitlines() if l.split()[0] != name]
    open(os.path.join(S, "containers.txt"), "w").write("".join(l + "\\n" for l in lines)); sys.exit(0)
if args[0] == "ps":
    sys.stdout.write(open(os.path.join(S, "containers.txt")).read()); sys.exit(0)
sys.exit(0)
''')
        os.chmod(os.path.join(self.shim, "docker"), 0o755)

    def calls(self):
        return read(os.path.join(self.state, "calls.log")).splitlines()

    def right(self):
        return {"station-wrapper-43_config": (self.path("config"), "bind"),
                "station-wrapper-43_catalog": (self.path("catalog"), "bind"),
                "station-wrapper-43_media": ("/media", "rbind")}

    def test_a_fresh_server_has_nothing_to_clear(self):
        self.good_env()
        self.fake_docker()
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])

    def test_volumes_set_up_correctly_are_left_alone(self):
        self.good_env()
        self.fake_docker(volumes=self.right(), containers=[("station-wrapper-43", "station-wrapper-43")])
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])

    def test_a_volume_still_pointing_at_an_old_path_is_reported_with_the_fix_and_not_touched(self):
        self.good_env()
        self.fake_docker(volumes={**self.right(), "station-wrapper-43_catalog": ("/srv/station-wrapper-43/fs42/catalog", "bind")})
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 1)
        self.assertIn("station-wrapper-43_catalog", out)
        self.assertIn("/srv/station-wrapper-43/fs42/catalog", out)                    # how it is set up now
        self.assertIn(self.path("catalog"), out)                                   # how it should be
        self.assertIn("docker compose down --remove-orphans", out)
        self.assertIn("docker volume rm station-wrapper-43_catalog", out)
        self.assertNotIn("volume rm station-wrapper-43_config", out)                    # the correct ones are not blamed
        self.assertIn("folders and their contents stay", out)
        self.assertEqual(self.calls(), [])                                         # reporting changed nothing

    def test_a_media_volume_with_the_right_path_but_the_old_plain_bind_is_still_stale(self):
        # Same name, same device. But a plain bind leaves out shares mounted inside the folder,
        # and Docker would silently keep using it: it must be flagged.
        self.good_env()
        self.fake_docker(volumes={**self.right(), "station-wrapper-43_media": ("/media", "bind")})
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 1)
        self.assertIn("station-wrapper-43_media", out)
        self.assertIn("(bind)", out)
        self.assertIn("(rbind)", out)

    def test_volumes_the_compose_file_no_longer_has_are_reported_as_unused(self):
        self.good_env()
        self.fake_docker(volumes={**self.right(), "station-wrapper-43_runtime": ("/old/runtime", "bind"),
                                  "station-wrapper-43_pool": ("/old/pool", "bind")})
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 1)
        self.assertIn("station-wrapper-43_runtime is no longer in the compose file", out)
        self.assertIn("station-wrapper-43_pool is no longer in the compose file", out)
        self.assertNotIn("station-wrapper-43_config is", out)

    def test_containers_from_an_earlier_set_of_services_are_reported(self):
        self.good_env()
        self.fake_docker(containers=[("206_fieldstation42", "fieldstation42"), ("206_fs42-tuner", "fs42-tuner")])
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 1)
        self.assertIn("206_fieldstation42", out)
        self.assertIn("206_fs42-tuner", out)
        self.assertIn("--remove-orphans", out)

    def test_migrate_clears_exactly_the_stale_volumes_and_the_old_containers(self):
        self.good_env()
        self.fake_docker(volumes={"station-wrapper-43_config": (self.path("config"), "bind"),       # right: stays
                                  "station-wrapper-43_catalog": ("/old/catalog", "bind"),            # wrong path
                                  "station-wrapper-43_media": ("/media", "bind"),                    # wrong options
                                  "station-wrapper-43_runtime": ("/old/runtime", "bind")},           # unused
                         containers=[("206_fieldstation42", "fieldstation42")])
        rc, out = self.run_script("--migrate", shim=True)
        self.assertEqual(rc, 0, out)
        calls = self.calls()
        self.assertEqual(calls[0], "compose down --remove-orphans")                # the stack is stopped first
        self.assertEqual(sorted(c for c in calls if c.startswith("volume rm")),
                         ["volume rm station-wrapper-43_catalog", "volume rm station-wrapper-43_media",
                          "volume rm station-wrapper-43_runtime"])
        self.assertNotIn("volume rm station-wrapper-43_config", calls)
        self.assertIn("rm -f 206_fieldstation42", calls)                           # an old container goes by name too
        self.assertIn("removed the container 206_fieldstation42", out)
        self.assertIn("removed the volume station-wrapper-43_runtime", out)
        rc, out = self.run_script(shim=True)                                        # and a second run finds nothing left
        self.assertEqual(rc, 0, out)

    def test_without_docker_compose_the_check_is_skipped_not_fatal(self):
        self.good_env()
        self.fake_docker(volumes={"station-wrapper-43_runtime": ("/old", "bind")}, compose=False)
        rc, out = self.run_script(shim=True)
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
