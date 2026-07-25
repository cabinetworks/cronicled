import json
import os
import tempfile
import unittest

from cronicled.config import config_dir, default_server_path, load_server
from cronicled.adapters.registry import default_adapters_path, load_adapters


class ConfigDir(unittest.TestCase):
    def test_honours_the_environment_variable(self):
        self.assertEqual(config_dir({"CRONICLED_CONFIG_DIR": "/mnt/config"}),
                         "/mnt/config")

    def test_falls_back_to_the_config_directory(self):
        self.assertEqual(config_dir({}), "config")

    def test_an_empty_value_falls_back_too(self):
        # an env var set to "" is indistinguishable from "not really set" for
        # this purpose - a blank mount path is not a usable directory
        self.assertEqual(config_dir({"CRONICLED_CONFIG_DIR": ""}), "config")

    def test_default_paths_are_built_under_it(self):
        env = {"CRONICLED_CONFIG_DIR": "/mnt/config"}
        self.assertEqual(default_server_path(env), "/mnt/config/server.json")
        self.assertEqual(default_adapters_path(env), "/mnt/config/adapters.json")


class LoadServer(unittest.TestCase):
    def test_environment_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            got = load_server(p, env={"STASH_URL": "http://env.example.test",
                                      "STASH_API_KEY": "E"})
            self.assertEqual(got["url"], "http://env.example.test")
            self.assertEqual(got["api_key"], "E")

    def test_falls_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            self.assertEqual(load_server(p, env={})["api_key"], "F")

    def test_missing_api_key_names_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test"}, fh)
            with self.assertRaises(ValueError) as ctx:
                load_server(p, env={})
            self.assertIn("api_key", str(ctx.exception))

    def test_absent_file_and_empty_env_raises(self):
        with self.assertRaises(ValueError):
            load_server("/nonexistent/server.json", env={})

    def test_no_default_host_is_baked_in(self):
        # a hardcoded hostname would identify the operator's machine
        import inspect
        import cronicled.config as mod
        self.assertNotIn(".local", inspect.getsource(mod))

    def test_finds_the_file_under_cronicled_config_dir_with_no_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "server.json"), "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            env = {"CRONICLED_CONFIG_DIR": d}
            got = load_server(env=env)
            self.assertEqual(got["api_key"], "F")

    def test_explicit_path_still_overrides_cronicled_config_dir(self):
        with tempfile.TemporaryDirectory() as configured, \
             tempfile.TemporaryDirectory() as explicit:
            with open(os.path.join(configured, "server.json"), "w") as fh:
                json.dump({"url": "http://configured.example.test", "api_key": "C"}, fh)
            p = os.path.join(explicit, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://explicit.example.test", "api_key": "X"}, fh)
            env = {"CRONICLED_CONFIG_DIR": configured}
            got = load_server(p, env=env)
            self.assertEqual(got["api_key"], "X")


class LoadAdaptersConfigDir(unittest.TestCase):
    def test_finds_the_file_under_cronicled_config_dir_with_no_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "adapters.json"), "w") as fh:
                json.dump({"adapters": [{"name": "site", "owner_source": "none"}]}, fh)
            env = {"CRONICLED_CONFIG_DIR": d}
            loaded = load_adapters(env=env)
            self.assertEqual(sorted(loaded), ["site"])

    def test_explicit_path_still_overrides_cronicled_config_dir(self):
        with tempfile.TemporaryDirectory() as configured, \
             tempfile.TemporaryDirectory() as explicit:
            with open(os.path.join(configured, "adapters.json"), "w") as fh:
                json.dump({"adapters": [{"name": "configured", "owner_source": "none"}]}, fh)
            p = os.path.join(explicit, "adapters.json")
            with open(p, "w") as fh:
                json.dump({"adapters": [{"name": "explicit", "owner_source": "none"}]}, fh)
            env = {"CRONICLED_CONFIG_DIR": configured}
            loaded = load_adapters(p, env=env)
            self.assertEqual(sorted(loaded), ["explicit"])


class ContainerConfigLayout(unittest.TestCase):
    """The Dockerfile sets $CRONICLED_CONFIG_DIR=/config and declares /config
    as a volume; the README tells users to mount their config there. This
    confirms that documented layout actually works: both loaders, given only
    that environment variable and no explicit path, read the files a user
    would have mounted."""

    def test_both_loaders_read_the_mounted_directory(self):
        with tempfile.TemporaryDirectory() as mount:
            with open(os.path.join(mount, "server.json"), "w") as fh:
                json.dump({"url": "http://mounted.example.test", "api_key": "M"}, fh)
            with open(os.path.join(mount, "adapters.json"), "w") as fh:
                json.dump({"adapters": [{"name": "mounted", "owner_source": "none"}]}, fh)
            env = {"CRONICLED_CONFIG_DIR": mount}

            server = load_server(env=env)
            adapters = load_adapters(env=env)

            self.assertEqual(server["api_key"], "M")
            self.assertEqual(sorted(adapters), ["mounted"])
