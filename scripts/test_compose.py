"""The compose files: ONE service, and they must say, and enforce, where things may live.

The catalog and the pool are folders of symlinks, so they must be on local disk and not an
SMB/CIFS share. These tests render the real compose files (skipped when `docker compose`
isn't available) and check the explicit statement is still there.

Run: python3 scripts/test_compose.py
"""
import json
import os
import subprocess
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
COMPOSE = os.path.join(ROOT, "docker-compose.yml")
VIEW = os.path.join(ROOT, "examples", "docker-compose.view.example.yml")


def read(path):
    with open(path) as f:
        return f.read()


def render(file=COMPOSE, extra_files=(), **env):
    """docker compose config as JSON, ignoring the repo's own .env. Returns (config, error text)."""
    full = {k: v for k, v in os.environ.items() if not k.startswith("FS42_")}
    full.update({k: v for k, v in env.items() if v is not None})
    flags = [x for f in (file, *extra_files) for x in ("-f", f)]
    out = subprocess.run(["docker", "compose", "--env-file", "/dev/null", *flags, "config", "--format", "json"],
                         capture_output=True, text=True, env=full, cwd=ROOT)
    if out.returncode != 0:
        return None, out.stderr
    return json.loads(out.stdout), ""


def have_compose():
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
    except OSError:
        return False


def mounts(cfg, svc="station-wrapper-43"):
    """{container path: (host path, read_only)} for a service."""
    vols = cfg.get("volumes", {})
    out = {}
    for v in cfg["services"][svc]["volumes"]:
        device = v["source"] if v["type"] == "bind" else vols[v["source"]]["driver_opts"]["device"]
        out[v["target"]] = (device, bool(v.get("read_only")))
    return out


class ComposeTextTest(unittest.TestCase):
    """The statements themselves, which need no Docker."""

    def test_the_header_says_the_catalog_is_not_for_smb(self):
        head = read(COMPOSE).split("name: station-wrapper-43")[0]
        self.assertIn("NOT an SMB/CIFS share", head)
        self.assertIn("LOCAL DISK", head)
        self.assertIn("SYMLINKS", head)
        self.assertIn("FS42_ALLOW_NETWORK_CATALOG", head)

    def test_the_header_says_it_is_one_image_one_container_three_volumes(self):
        head = read(COMPOSE).split("name: station-wrapper-43")[0]
        self.assertIn("ONE image, ONE container, THREE volumes", head)

    def test_the_header_describes_each_volume_and_what_is_in_it(self):
        head = read(COMPOSE).split("name: station-wrapper-43")[0]
        for needle in ("CONFIG", "CATALOG", "MEDIA", "runtime/", "confs/", "tuner/", "groups.toml", "by-genre/",
                       "tanfo/", "media-links/", "FS42_LOCAL_ROOT", "rbind", "ONLY SOME FOLDERS"):
            self.assertIn(needle, head + read(COMPOSE))

    def test_the_catalog_volume_repeats_it(self):
        text = read(COMPOSE)
        volume = text[text.index("CATALOG: MUST NOT be"):text.index("catalog:\n    driver_opts")]
        self.assertIn("MUST NOT be an SMB/CIFS share", volume)

    def test_the_container_refuses_rather_than_runs_broken(self):
        entry = read(os.path.join(ROOT, "fieldstation42", "entrypoint.sh"))
        self.assertIn("REFUSING TO START", entry)
        self.assertIn("exit 78", entry)



@unittest.skipUnless(have_compose(), "docker compose is not available")
class ComposeRenderTest(unittest.TestCase):
    BASE = dict(FS42_LOCAL_ROOT="/local", FS42_MEDIA_ROOT="/media")

    def cfg(self, **env):
        config, err = render(**{**self.BASE, **env})
        self.assertIsNotNone(config, err)
        return config

    def test_it_is_one_service_with_one_image(self):
        config = self.cfg()
        self.assertEqual(list(config["services"]), ["station-wrapper-43"])
        svc = config["services"]["station-wrapper-43"]
        self.assertEqual(svc["image"], "ghcr.io/jamesm92/station-wrapper-43:dev")
        self.assertEqual(svc["container_name"], "station-wrapper-43")
        self.assertTrue(svc["build"]["context"].rstrip("/").endswith(os.path.basename(os.path.abspath(ROOT))))

    def test_both_the_console_and_the_dashboard_are_published(self):
        ports = sorted((int(p["target"]), p["published"]) for p in self.cfg()["services"]["station-wrapper-43"]["ports"])
        self.assertEqual(ports, [(4242, "4242"), (5004, "5004")])

    def test_config_and_catalog_are_local_folders_under_the_one_root(self):
        m = mounts(self.cfg())
        self.assertEqual(m["/config"][0], "/local/config")
        self.assertEqual(m["/app/catalog"][0], "/local/catalog")
        self.assertEqual(set(m), {"/config", "/app/catalog", "/mnt/media", "/etc/localtime"})   # three volumes, not six

    def test_the_media_is_the_only_thing_on_the_share_and_it_is_read_only(self):
        m = mounts(self.cfg())
        self.assertEqual(m["/mnt/media"], ("/media", True))
        for target, (_dev, read_only) in m.items():
            if target not in ("/mnt/media", "/etc/localtime"):
                self.assertFalse(read_only, f"{target} must be writable: the container writes there")

    def test_the_media_volume_is_a_recursive_bind_so_shares_mounted_inside_it_show_up(self):
        opts = self.cfg()["volumes"]["media"]["driver_opts"]
        self.assertEqual(opts["o"], "rbind")
        self.assertEqual(self.cfg()["volumes"]["config"]["driver_opts"]["o"], "bind")      # the local ones need no recursion
        self.assertEqual(self.cfg()["volumes"]["catalog"]["driver_opts"]["o"], "bind")

    def test_the_catalog_and_the_config_can_each_be_moved_to_another_local_path(self):
        m = mounts(self.cfg(FS42_CATALOG_ROOT="/elsewhere", FS42_CONFIG_ROOT="/cfg"))
        self.assertEqual(m["/app/catalog"][0], "/elsewhere")
        self.assertEqual(m["/config"][0], "/cfg")

    def test_a_missing_local_path_stops_compose_with_the_reason(self):
        for missing in ({"FS42_LOCAL_ROOT": None},
                        {"FS42_LOCAL_ROOT": None, "FS42_CATALOG_ROOT": "/c"},     # runtime still needs it
                        {"FS42_LOCAL_ROOT": None, "FS42_SSD_ROOT": "/old"}):      # the old name is not enough
            config, err = render(**{**self.BASE, **missing})
            self.assertIsNone(config)
            self.assertIn("FS42_LOCAL_ROOT", err)
            self.assertIn("LOCAL", err)
            self.assertIn("SMB", err)
            self.assertIn("FS42_SSD_ROOT", err)                                   # and it says how to rename

    def test_the_optional_parts_are_off_unless_switched_on(self):
        env = self.cfg()["services"]["station-wrapper-43"]["environment"]
        self.assertEqual((env["SYNC_ENABLED"], env["TA_ENABLED"]), ("", ""))
        on = self.cfg(SYNC_ENABLED="1", TA_ENABLED="1", TA_LIBRARY="/mnt/media/yt")["services"]["station-wrapper-43"]["environment"]
        self.assertEqual((on["SYNC_ENABLED"], on["TA_ENABLED"], on["TA_LIBRARY"]), ("1", "1", "/mnt/media/yt"))

    def test_the_nightly_catalog_update_is_left_to_the_sync_default_which_is_on(self):
        env = self.cfg()["services"]["station-wrapper-43"]["environment"]
        self.assertEqual(env["SYNC_REBUILD"], "")
        self.assertEqual((env["SYNC_INTERVAL_MINUTES"], env["SYNC_REBUILD_HOUR"], env["SYNC_PRUNE"],
                          env["TA_INTERVAL_MINUTES"]), ("60", "4", "", "1440"))
        on = self.cfg(SYNC_REBUILD="1", SYNC_REBUILD_HOUR="3", SYNC_INCLUDE="movies,tvseries")["services"]["station-wrapper-43"]["environment"]
        self.assertEqual((on["SYNC_REBUILD"], on["SYNC_REBUILD_HOUR"], on["SYNC_INCLUDE"]), ("1", "3", "movies,tvseries"))

    def test_the_override_is_passed_through_and_the_label_states_the_rule(self):
        svc = self.cfg()["services"]["station-wrapper-43"]
        self.assertEqual(svc["environment"]["FS42_ALLOW_NETWORK_CATALOG"], "")            # off unless set
        self.assertIn("LOCAL disk", svc["labels"]["fs42.requires"])
        self.assertEqual(self.cfg(FS42_ALLOW_NETWORK_CATALOG="1")["services"]["station-wrapper-43"]["environment"]
                         ["FS42_ALLOW_NETWORK_CATALOG"], "1")

    def test_the_host_view_override_adds_one_read_only_mount(self):
        config, err = render(extra_files=(VIEW,), FS42_VIEW_ROOT="/view", **self.BASE)
        self.assertIsNotNone(config, err)
        self.assertEqual(mounts(config)["/view"], ("/view", True))
        self.assertEqual(list(config["services"]), ["station-wrapper-43"])
        config, err = render(extra_files=(VIEW,), **self.BASE)                            # without the variable
        self.assertIsNone(config)
        self.assertIn("FS42_VIEW_ROOT", err)


if __name__ == "__main__":
    unittest.main()
