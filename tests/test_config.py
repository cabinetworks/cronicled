import json
import os
import tempfile
import unittest

from cronicled.config import (
    config_dir, default_scan_path, default_schedule_path, default_server_path,
    default_stashbox_path, load_marker_tag, load_schedule, load_server,
    load_stashbox)
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


class LoadStashbox(unittest.TestCase):
    """A stash-box endpoint is optional infrastructure -- a better refusal is
    unavailable without it, nothing more -- so this follows `load_adapters`'s
    half of the rule stated in `cronicled/config.py`'s module docstring:
    absence returns `None`, it never raises.
    """

    def test_environment_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "stashbox.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            got = load_stashbox(p, env={"STASHBOX_URL": "http://env.example.test",
                                        "STASHBOX_API_KEY": "E"})
            self.assertEqual(got, {"url": "http://env.example.test", "api_key": "E"})

    def test_falls_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "stashbox.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            self.assertEqual(load_stashbox(p, env={}),
                             {"url": "http://file.example.test", "api_key": "F"})

    def test_a_missing_file_and_empty_env_returns_none_not_an_error(self):
        self.assertIsNone(load_stashbox("/nonexistent/stashbox.json", env={}))

    def test_finds_the_file_under_cronicled_config_dir_with_no_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "stashbox.json"), "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            env = {"CRONICLED_CONFIG_DIR": d}
            self.assertEqual(load_stashbox(env=env)["url"], "http://file.example.test")

    def test_default_path_is_built_under_config_dir(self):
        env = {"CRONICLED_CONFIG_DIR": "/mnt/config"}
        self.assertEqual(default_stashbox_path(env), "/mnt/config/stashbox.json")

    def test_a_url_with_no_api_key_is_still_configured(self):
        # A stash-box instance that permits anonymous reads has no key to
        # give -- treating that as "unconfigured" would refuse a perfectly
        # usable endpoint over a field it does not need.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "stashbox.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test"}, fh)
            got = load_stashbox(p, env={})
            self.assertEqual(got, {"url": "http://file.example.test", "api_key": None})

    def test_an_api_key_with_no_url_is_not_configured(self):
        # Only `url` gates whether this counts as configured at all -- a
        # stray API key with nothing to point it at is not a usable endpoint.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "stashbox.json")
            with open(p, "w") as fh:
                json.dump({"api_key": "F"}, fh)
            self.assertIsNone(load_stashbox(p, env={}))


class LoadAdaptersConfigDir(unittest.TestCase):
    def test_finds_the_file_under_cronicled_config_dir_with_no_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "adapters.json"), "w") as fh:
                json.dump({"adapters": [{"name": "site", "owner_source": "none",
                                         "title_match_counts_as_ownership": True}]}, fh)
            env = {"CRONICLED_CONFIG_DIR": d}
            loaded = load_adapters(env=env)
            self.assertEqual(sorted(loaded), ["site"])

    def test_explicit_path_still_overrides_cronicled_config_dir(self):
        with tempfile.TemporaryDirectory() as configured, \
             tempfile.TemporaryDirectory() as explicit:
            with open(os.path.join(configured, "adapters.json"), "w") as fh:
                json.dump({"adapters": [{"name": "configured", "owner_source": "none",
                                         "title_match_counts_as_ownership": True}]}, fh)
            p = os.path.join(explicit, "adapters.json")
            with open(p, "w") as fh:
                json.dump({"adapters": [{"name": "explicit", "owner_source": "none",
                                         "title_match_counts_as_ownership": True}]}, fh)
            env = {"CRONICLED_CONFIG_DIR": configured}
            loaded = load_adapters(p, env=env)
            self.assertEqual(sorted(loaded), ["explicit"])


class LoadSchedule(unittest.TestCase):
    """Schedule overrides fall on `load_adapters`'s side of the rule in
    `cronicled/config.py`'s module docstring: every producer already declares
    its own cadence, so an operator who is happy with it configures nothing
    and the file is simply absent.

    What it deliberately does NOT do is validate the overrides. That belongs
    to `cronicled.schedule.resolve`, which refuses an unknown producer name, an
    unknown key, a cadence that is not a positive number and a non-boolean
    `enabled` — and refuses them at the moment the schedule is wired up, which
    is the same moment this file is read. A second validator here would be a
    second place for the two to disagree, and the one reading the file is the
    one that would go stale.
    """

    def test_it_is_found_under_the_config_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(default_schedule_path({"CRONICLED_CONFIG_DIR": d}),
                             os.path.join(d, "schedule.json"))

    def test_an_absent_file_is_a_legitimate_state_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(load_schedule(env={"CRONICLED_CONFIG_DIR": empty}),
                             {})

    def test_what_the_file_says_is_handed_on_whole(self):
        # Whole-shape equality, and two producers with different keys: a
        # loader that returned only the first entry, or dropped `enabled` in
        # favour of `every`, would leave an operator's explicit "do not run
        # this" doing nothing with nothing raised.
        overrides = {"nightly-library-scan": {"every": 3600},
                     "some-other-producer": {"enabled": False}}
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "schedule.json"), "w") as fh:
                json.dump(overrides, fh)
            self.assertEqual(load_schedule(env={"CRONICLED_CONFIG_DIR": d}),
                             overrides)

    def test_an_explicit_path_overrides_the_config_directory(self):
        with tempfile.TemporaryDirectory() as configured, \
             tempfile.TemporaryDirectory() as explicit:
            with open(os.path.join(configured, "schedule.json"), "w") as fh:
                json.dump({"from-the-config-dir": {"every": 60}}, fh)
            p = os.path.join(explicit, "schedule.json")
            with open(p, "w") as fh:
                json.dump({"from-the-explicit-path": {"every": 60}}, fh)
            self.assertEqual(
                load_schedule(p, env={"CRONICLED_CONFIG_DIR": configured}),
                {"from-the-explicit-path": {"every": 60}})

    def test_a_top_level_value_that_is_not_an_object_is_refused_by_name(self):
        # `resolve` receives this as `dict(overrides)`. A JSON list of names
        # would raise there as a `ValueError` about a dictionary update
        # sequence, and a bare string as a set of one-letter producer names
        # nobody wrote. Neither message mentions the file, and the file is
        # the only thing the operator can edit.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "schedule.json")
            with open(p, "w") as fh:
                json.dump(["nightly-library-scan"], fh)
            with self.assertRaises(ValueError) as ctx:
                load_schedule(env={"CRONICLED_CONFIG_DIR": d})
            self.assertIn(p, str(ctx.exception))


class LoadMarkerTag(unittest.TestCase):
    """The name of the tag that says a scene was organized PROVISIONALLY.

    It falls on `load_adapters`'s side of the rule in `cronicled/config.py`'s
    module docstring — most libraries carry no such tag, and a scan with none
    configured pools what it always did — with the one distinction that side
    of the rule does not otherwise have to draw: a key that is PRESENT and
    unusable is not absence. Every falsy spelling of it (an empty string, a
    blank one, a number) would fold into `None` under a plain `or`, quietly
    restoring the behaviour the operator was configuring their way out of.
    """

    def _write(self, directory, payload):
        with open(os.path.join(directory, "scan.json"), "w") as fh:
            json.dump(payload, fh)
        return {"CRONICLED_CONFIG_DIR": directory}

    def test_it_is_found_under_the_config_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(default_scan_path({"CRONICLED_CONFIG_DIR": d}),
                             os.path.join(d, "scan.json"))

    def test_an_absent_file_is_a_legitimate_state_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(
                load_marker_tag(env={"CRONICLED_CONFIG_DIR": empty}))

    def test_the_configured_name_is_handed_back_as_written(self):
        # As WRITTEN: `Stash.tag_id_by_name` matches a tag name exactly, so a
        # loader that lowered or trimmed the value would look up a tag the
        # operator did not name and find nothing.
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, {"marker_tag": "Inferred Metadata"})
            self.assertEqual(load_marker_tag(env=env), "Inferred Metadata")

    def test_padding_around_the_name_is_not_trimmed_away(self):
        # The other half of "as written", and the one a loader is tempted to
        # be helpful about. `Stash.tag_id_by_name` matches EXACTLY, so
        # trimming here looks up a name the operator did not write: it works
        # for the typo and breaks for the tag whose name really does carry a
        # space. Left alone, the typo fails the run with the name quoted --
        # spaces and all -- which is a mistake somebody can see and fix.
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, {"marker_tag": " Inferred Metadata "})
            self.assertEqual(load_marker_tag(env=env), " Inferred Metadata ")

    def test_a_file_that_names_no_marker_is_absence_not_a_mistake(self):
        # The file may one day hold other scan settings; lacking this key is
        # "no marker configured", which is a state, not a malformed file.
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, {"something_else": True})
            self.assertIsNone(load_marker_tag(env=env))

    def test_an_empty_name_is_refused_rather_than_read_as_absence(self):
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, {"marker_tag": ""})
            with self.assertRaises(ValueError) as ctx:
                load_marker_tag(env=env)
            self.assertIn(os.path.join(d, "scan.json"), str(ctx.exception))
            self.assertIn("marker_tag", str(ctx.exception))

    def test_a_blank_name_is_refused_too(self):
        # A tag whose whole name is whitespace is not a tag anyone can name
        # on the server either, so this cannot be a real setting.
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, {"marker_tag": "   "})
            with self.assertRaises(ValueError):
                load_marker_tag(env=env)

    def test_a_name_that_is_not_a_string_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, {"marker_tag": 7})
            with self.assertRaises(ValueError) as ctx:
                load_marker_tag(env=env)
            self.assertIn("7", str(ctx.exception))

    def test_a_top_level_value_that_is_not_an_object_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            env = self._write(d, ["inferred-metadata"])
            with self.assertRaises(ValueError) as ctx:
                load_marker_tag(env=env)
            self.assertIn(os.path.join(d, "scan.json"), str(ctx.exception))

    def test_an_explicit_path_overrides_the_config_directory(self):
        with tempfile.TemporaryDirectory() as configured, \
             tempfile.TemporaryDirectory() as explicit:
            env = self._write(configured, {"marker_tag": "from-the-config-dir"})
            p = os.path.join(explicit, "scan.json")
            with open(p, "w") as fh:
                json.dump({"marker_tag": "from-the-explicit-path"}, fh)
            self.assertEqual(load_marker_tag(p, env=env),
                             "from-the-explicit-path")


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

    def test_a_missing_stashbox_config_is_legitimate_too(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(load_stashbox(env={"CRONICLED_CONFIG_DIR": empty}))

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
                json.dump({"adapters": [{"name": "mounted", "owner_source": "none",
                                         "title_match_counts_as_ownership": True}]}, fh)
            env = {"CRONICLED_CONFIG_DIR": mount}

            server = load_server(env=env)
            adapters = load_adapters(env=env)

            self.assertEqual(server["api_key"], "M")
            self.assertEqual(sorted(adapters), ["mounted"])
