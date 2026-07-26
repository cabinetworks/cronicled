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


class MissingConfigRule(unittest.TestCase):
    """The rule stated in cronicled/config.py's module docstring: config the
    thing cannot function without RAISES and names what is missing; config
    whose absence is a legitimate state RETURNS AN EMPTY VALUE.

    The two loaders differ ON PURPOSE and neither behaviour is an oversight to
    tidy away. Making load_adapters raise would stop a fresh install from
    starting at all; making load_server return empty would hand a URL-less,
    key-less client to the network layer instead of a message naming what to
    set. Both halves are pinned here, against the SAME empty config directory,
    so the asymmetry is visible in one place rather than inferred from two
    files that each only describe themselves."""

    def test_config_required_to_function_raises_naming_every_missing_value(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ValueError) as ctx:
                load_server(env={"CRONICLED_CONFIG_DIR": empty})
            # the whole missing-list, not a sampled name: a catch-all message
            # mentioning neither, or only one of the two, is the failure this
            # exists to catch
            self.assertIn("missing media-server config: url, api_key",
                          str(ctx.exception))

    def test_config_required_to_function_names_where_it_could_come_from(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ValueError) as ctx:
                load_server(env={"CRONICLED_CONFIG_DIR": empty})
            msg = str(ctx.exception)
            self.assertIn("STASH_URL", msg)
            self.assertIn("STASH_API_KEY", msg)
            self.assertIn(os.path.join(empty, "server.json"), msg)

    def test_supplying_the_required_config_is_accepted(self):
        # the permissive side of the same guard: "raises when missing" must not
        # drift into "raises", which no fixture asserting only the refusal
        # would notice. Whole-shape equality, so an extra key cannot be
        # introduced under a green suite either.
        with tempfile.TemporaryDirectory() as empty:
            got = load_server(env={"CRONICLED_CONFIG_DIR": empty,
                                   "STASH_URL": "http://server.example.test",
                                   "STASH_API_KEY": "K"})
            self.assertEqual(got, {"url": "http://server.example.test",
                                   "api_key": "K"})

    def test_config_whose_absence_is_legitimate_returns_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            loaded = load_adapters(env={"CRONICLED_CONFIG_DIR": empty})
            self.assertEqual(loaded, {})
            self.assertIsNone(loaded.default)

    def test_the_two_loaders_disagree_deliberately_on_the_same_empty_dir(self):
        # one empty directory, two answers, both correct. If a future change
        # makes these agree, this is the test that says it was not an
        # improvement.
        with tempfile.TemporaryDirectory() as empty:
            env = {"CRONICLED_CONFIG_DIR": empty}
            with self.assertRaises(ValueError):
                load_server(env=env)
            self.assertEqual(load_adapters(env=env), {})


class EnvironmentVariableSplit(unittest.TestCase):
    """Two prefixes, on purpose. $STASH_URL/$STASH_API_KEY name the media
    server being managed — someone else's software, whose variables an
    operator may already have set for reasons that have nothing to do with
    this project. $CRONICLED_CONFIG_DIR names this project's own directory.

    Folding the first pair into a CRONICLED_ prefix to "match" would silently
    stop reading an environment that already works, on machines where it was
    the only configuration present. Each half is pinned in both directions:
    the name that IS read, and the tidied-up alias that must NOT be, since an
    alias quietly creates a second name for one setting and the two then drift
    apart."""

    def test_media_server_credentials_are_read_from_the_stash_names(self):
        got = load_server("/nonexistent/server.json",
                          env={"STASH_URL": "http://server.example.test",
                               "STASH_API_KEY": "K"})
        self.assertEqual(got, {"url": "http://server.example.test",
                               "api_key": "K"})

    def test_no_cronicled_prefixed_alias_supplies_the_credentials(self):
        with self.assertRaises(ValueError):
            load_server("/nonexistent/server.json",
                        env={"CRONICLED_STASH_URL": "http://server.example.test",
                             "CRONICLED_STASH_API_KEY": "K",
                             "CRONICLED_URL": "http://server.example.test",
                             "CRONICLED_API_KEY": "K"})

    def test_this_projects_directory_is_read_from_the_cronicled_name(self):
        self.assertEqual(config_dir({"CRONICLED_CONFIG_DIR": "/mnt/elsewhere"}),
                         "/mnt/elsewhere")

    def test_no_stash_prefixed_alias_supplies_this_projects_directory(self):
        self.assertEqual(config_dir({"STASH_CONFIG_DIR": "/mnt/elsewhere",
                                     "STASH_DIR": "/mnt/elsewhere"}),
                         "config")


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
