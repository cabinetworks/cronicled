import contextlib
import io
import json
import os
import tempfile
import unittest

from cronicled.config import (
    ZONE_ENV_VAR, config_dir, default_scan_path, default_schedule_path,
    default_server_path, default_stashbox_path, load_marker_tag, load_schedule,
    load_server, load_stashbox, load_zone)
from cronicled.adapters.registry import default_adapters_path, load_adapters
from cronicled.schedule import resolve


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
        overrides = {"scene-scan": {"every": 3600},
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


class _ProducerThatOnlyDeclaresATiming:
    """Everything `cronicled.schedule.resolve` is allowed to read off a
    producer, and nothing else — a name and a declared cadence.

    Deliberately no more capable than the real thing: `resolve` looks a
    producer up BY THE NAME an override uses, so a stand-in that answered to
    more than one name, or that ignored a name it did not know, would let a
    migration that translated to the wrong name pass.
    """

    def __init__(self, name, every):
        self.name = name
        self.every = every


class ScheduleFileNamesJobsThatHaveBeenRenamed(unittest.TestCase):
    """An operator's `schedule.json` outlives the names it was written with.

    `cronicled.schedule.resolve` refuses an override naming a producer that
    does not exist, and refuses it at START-UP, before there is a page on
    which to read the refusal — so a rename landing on a deployment that has
    a schedule file is a crash loop, not a warning. `load_schedule` translates
    the names this project itself changed, so that window is closed for one
    release.

    What it must NOT do is make an unknown name acceptable. That refusal is
    what turns a typo into a stack trace instead of a job that silently never
    runs, and translating a known old name is not the same act as accepting
    an unknown one.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "schedule.json")
        self.env = {"CRONICLED_CONFIG_DIR": self._dir.name}

    def _write_schedule(self, text):
        # Raw text, never `json.dump`: a duplicate key is exactly what
        # `json.dump` cannot produce from a dict, and it is half of what this
        # class exists to test.
        with open(self.path, "w") as fh:
            fh.write(text)

    def _load_watching_stdout(self):
        """`load_schedule`, and every line it printed while running.

        Captured from the real stdout rather than through an injected
        reporter: the thing an operator reads is a line on a terminal, and a
        double that collected messages some other way would be agreeing with
        the test rather than with the deployment.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            loaded = load_schedule(env=self.env)
        return loaded, out.getvalue()

    def _load(self):
        """`load_schedule`, with the migration notice swallowed.

        Only for the tests that are about the mapping rather than about what
        an operator is told; the notice itself has its own tests below.
        """
        return self._load_watching_stdout()[0]

    # -- the three translations, each pinned by literals on both sides ---- #
    #
    # Literal old name, literal new name, one test each. Deriving either side
    # from the constants the code reads would move both together, and every
    # entry could then be deleted or repointed under a green suite -- which is
    # how two of the three renames went unpinned when they were made.

    def test_a_schedule_naming_the_old_scene_job_is_migrated(self):
        self._write_schedule('{"nightly-library-scan": {"at": "03:00"}}')
        self.assertEqual(self._load(), {"scene-scan": {"at": "03:00"}})

    def test_a_schedule_naming_the_old_performer_job_is_migrated(self):
        self._write_schedule('{"performer-descriptions": {"every": 86400}}')
        self.assertEqual(self._load(), {"performer-scan": {"every": 86400}})

    def test_a_schedule_naming_the_old_tag_job_is_migrated(self):
        self._write_schedule('{"tag-merge": {"enabled": false}}')
        self.assertEqual(self._load(), {"tag-scan": {"enabled": False}})

    def test_a_schedule_using_current_names_is_unchanged(self):
        self._write_schedule('{"tag-scan": {"at": "03:40"}}')
        self.assertEqual(load_schedule(env=self.env),
                         {"tag-scan": {"at": "03:40"}})

    def test_the_settings_under_a_migrated_name_are_carried_over_whole(self):
        # The key moves; the value must not be rebuilt on the way. Whole-shape
        # equality, with a setting the loader has no reason to know about, so
        # a migration that copied only the keys it recognised is visible here
        # rather than at the moment `resolve` refuses the entry it kept.
        self._write_schedule(
            '{"nightly-library-scan": {"at": "03:00", "zone": "Europe/Lisbon",'
            ' "enabled": true}}')
        self.assertEqual(
            self._load(),
            {"scene-scan": {"at": "03:00", "zone": "Europe/Lisbon",
                            "enabled": True}})

    # -- the refusal the migration works around, still intact -------------- #

    def test_a_name_this_project_never_used_is_handed_on_as_written(self):
        self._write_schedule('{"scene-scam": {"every": 3600}}')
        self.assertEqual(load_schedule(env=self.env),
                         {"scene-scam": {"every": 3600}})

    def test_a_typo_is_still_refused_at_start_up_rather_than_ignored(self):
        # HARM: the whole value of `resolve`'s refusal is that a mistyped job
        # name stops the process instead of leaving the real job running on
        # the cadence the operator believed they had changed. A migration that
        # smoothed unknown names away -- dropping them, or matching them to
        # something near enough -- would buy the rename at the cost of that.
        self._write_schedule('{"scene-scam": {"every": 3600}}')
        overrides = load_schedule(env=self.env)
        with self.assertRaises(ValueError) as caught:
            resolve([_ProducerThatOnlyDeclaresATiming("scene-scan",
                                                      every=3600)], overrides)
        self.assertIn("scene-scam", str(caught.exception))

    def test_a_migrated_name_reaches_resolve_as_a_job_that_exists(self):
        # The other half, end to end: the old name goes in, and the schedule
        # wires up rather than refusing. Asserting only the refusal above
        # would be satisfied by a loader that refused everything.
        self._write_schedule('{"nightly-library-scan": {"every": 3600}}')
        overrides = self._load()
        entries = resolve(
            [_ProducerThatOnlyDeclaresATiming("scene-scan", every=86400)],
            overrides)
        self.assertEqual(entries["scene-scan"].every, 3600)

    # -- the operator is told ---------------------------------------------- #

    def test_every_migration_is_reported_naming_the_old_and_the_new(self):
        # Two of them, not one: a report that named only the first migration
        # would leave the second old name in the file with nothing said, and
        # a fixture of one cannot tell "each" from "any".
        self._write_schedule('{"nightly-library-scan": {"every": 3600},'
                             ' "tag-merge": {"enabled": false}}')
        loaded, printed = self._load_watching_stdout()
        self.assertEqual(loaded, {"scene-scan": {"every": 3600},
                                  "tag-scan": {"enabled": False}})
        for name in ("nightly-library-scan", "scene-scan",
                     "tag-merge", "tag-scan"):
            self.assertIn(name, printed)
        self.assertIn(self.path, printed)

    def test_a_file_that_needs_no_migration_is_reported_on_at_all(self):
        # The permissive side. A loader that announced a migration whatever
        # the file said would tell every operator to edit a file that is
        # already correct, and the notice would be ignored by the time it
        # meant something.
        self._write_schedule('{"scene-scan": {"every": 3600}}')
        _, printed = self._load_watching_stdout()
        self.assertEqual(printed, "")

    # -- one job, two names ------------------------------------------------ #

    def test_naming_one_job_by_both_its_names_is_refused_naming_both(self):
        # JSON sees two different keys, so nothing upstream can catch this,
        # and it is the file an operator part-way through the rename writes.
        # Resolving it by iteration order would leave one of the two entries
        # doing nothing, silently -- the same defect as a duplicate key,
        # wearing a different spelling.
        self._write_schedule('{"nightly-library-scan": {"every": 86400},'
                             ' "scene-scan": {"at": "03:00"}}')
        with self.assertRaises(ValueError) as caught:
            load_schedule(env=self.env)
        message = str(caught.exception)
        self.assertIn("nightly-library-scan", message)
        self.assertIn("scene-scan", message)
        self.assertIn("86400", message)
        self.assertIn("03:00", message)

    def test_it_is_refused_whichever_order_the_two_names_come_in(self):
        # Order-independence, because the file is written by a person: a check
        # that only looked at names already seen would accept exactly this
        # file and refuse the one above.
        self._write_schedule('{"scene-scan": {"at": "03:00"},'
                             ' "nightly-library-scan": {"every": 86400}}')
        with self.assertRaises(ValueError) as caught:
            load_schedule(env=self.env)
        message = str(caught.exception)
        self.assertIn("nightly-library-scan", message)
        self.assertIn("86400", message)
        self.assertIn("03:00", message)

    # -- ...and the half of it that is not a conflict at all --------------- #
    #
    # Two entries naming one job with the SAME settings do not disagree about
    # anything. Whichever were kept, the job runs on the same cadence, so the
    # refusal's own justification -- "one of them would silently do nothing"
    # -- does not hold, and refusing stops the process starting over a file
    # that expresses one unambiguous intent. It is the exact half-renamed
    # state RENAMED_JOBS exists to let a configured deployment start in.

    def test_two_names_asking_for_the_same_thing_is_agreement_not_a_refusal(self):
        # HARM this fixes: an operator part-way through the rename, who has
        # copied their entry under the new name and not yet deleted the old
        # one, could not start the process at all -- defeating the one
        # release of grace the rename mechanism exists to give them.
        self._write_schedule('{"tag-merge": {"every": 3600},'
                             ' "tag-scan": {"every": 3600}}')

        loaded, printed = self._load_watching_stdout()

        self.assertEqual(loaded, {"tag-scan": {"every": 3600}})
        # Both spellings named, so "delete one" is actionable.
        self.assertIn("tag-merge", printed)
        self.assertIn("tag-scan", printed)

    def test_agreeing_entries_read_the_same_whichever_order_they_come_in(self):
        # The file is written by a person, and nothing an operator controls
        # only by ordering their file may decide either the mapping or what
        # they are told. Asserted against the OTHER order's own output rather
        # than against a literal, so a rule that read position could not
        # satisfy both halves.
        self._write_schedule('{"tag-merge": {"every": 3600},'
                             ' "tag-scan": {"every": 3600}}')
        forwards, forwards_printed = self._load_watching_stdout()
        self._write_schedule('{"tag-scan": {"every": 3600},'
                             ' "tag-merge": {"every": 3600}}')
        backwards, backwards_printed = self._load_watching_stdout()

        self.assertEqual(forwards, backwards)
        self.assertEqual(forwards_printed, backwards_printed)

    def test_settings_that_differ_at_all_are_still_refused(self):
        # The direction that must NOT be weakened. The same key on both sides
        # and only the VALUE differing is the smallest disagreement the file
        # can express -- a rule comparing only which keys were written would
        # read this as agreement and schedule one of the two cadences with
        # nothing said about the other.
        self._write_schedule('{"tag-merge": {"every": 3600},'
                             ' "tag-scan": {"every": 7200}}')

        with self.assertRaises(ValueError) as caught:
            load_schedule(env=self.env)

        message = str(caught.exception)
        self.assertIn("3600", message)
        self.assertIn("7200", message)

    def test_an_agreeing_pair_is_not_reported_as_a_plain_rename(self):
        # The two notices say different things -- "your file is one release
        # out of date" versus "your file says this twice" -- and an operator
        # acts on them differently. A single catch-all sentence naming the
        # old and the new spelling would satisfy every assertion above about
        # both names appearing while telling them only half of it.
        self._write_schedule('{"tag-merge": {"every": 3600},'
                             ' "tag-scan": {"every": 3600}}')
        agreeing = self._load_watching_stdout()[1]
        self._write_schedule('{"tag-merge": {"every": 3600}}')
        renamed = self._load_watching_stdout()[1]

        self.assertNotEqual(agreeing, renamed)
        self.assertIn("more than once", agreeing)
        self.assertNotIn("more than once", renamed)


class ScheduleFileNamesAKeyTwice(unittest.TestCase):
    """JSON keeps the last occurrence of a repeated key and discards the rest
    without a word.

    Observed on a real deployment: every job named twice, so three interval
    entries were dead and three passes shared one appointment. Nothing was
    wrong with the file as far as any parser was concerned, and the operator
    believed something untrue about their own configuration for as long as it
    ran.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "schedule.json")
        self.env = {"CRONICLED_CONFIG_DIR": self._dir.name}

    def _write_schedule(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)

    def test_a_duplicate_key_is_refused_naming_both_values(self):
        # BOTH values, and they are different values on purpose: which half
        # of the file the parser threw away is the only thing the operator
        # needs to know, and a message saying merely that the key repeats
        # leaves them to guess. Two assertions against one catch-all string
        # would both pass on a message that named neither.
        self._write_schedule(
            '{"scene-scan": {"every": 86400}, "scene-scan": {"at": "03:00"}}')
        with self.assertRaises(ValueError) as caught:
            load_schedule(env=self.env)
        message = str(caught.exception)
        self.assertIn("scene-scan", message)
        self.assertIn("86400", message)
        self.assertIn("03:00", message)
        self.assertIn(self.path, message)

    def test_a_duplicate_inside_one_jobs_settings_is_refused_too(self):
        # The same mistake one level down, with the same consequence: the
        # operator's stated cadence is discarded and the pass runs on the
        # other one. A check that ran only over the top-level keys would see
        # a file with one job in it and nothing wrong.
        self._write_schedule(
            '{"scene-scan": {"every": 86400, "every": 3600}}')
        with self.assertRaises(ValueError) as caught:
            load_schedule(env=self.env)
        message = str(caught.exception)
        self.assertIn("every", message)
        self.assertIn("86400", message)
        self.assertIn("3600", message)

    def test_two_jobs_with_the_same_settings_are_not_a_duplicate(self):
        # The permissive side of the guard, which is the half a refusal test
        # cannot see: repeated VALUES are ordinary -- two passes may well run
        # on the same cadence -- and only a repeated KEY is the mistake. A
        # guard that drifted to refusing this would stop a correct file from
        # starting.
        self._write_schedule('{"scene-scan": {"every": 3600},'
                             ' "tag-scan": {"every": 3600}}')
        self.assertEqual(load_schedule(env=self.env),
                         {"scene-scan": {"every": 3600},
                          "tag-scan": {"every": 3600}})

    def test_one_key_written_twice_is_refused_even_when_it_says_the_same_thing(self):
        """No change needed: this refusal is correct on agreement too, and
        the test exists so that stays a rule rather than an accident.

        Deliberately UNLIKE `_migrate_renamed_jobs`, which was changed to
        migrate two names that ask for the same thing. The difference is not
        the values but what the second occurrence IS. Two job names are two
        keys a file may legitimately hold at once -- the rename mechanism
        asks operators to pass through exactly that state for one release --
        so refusing agreement there blocks a sanctioned workflow. One key
        written twice in one object is never a state anything asks for; it
        is a single author repeating themselves, the second occurrence
        corroborates nothing the first did not already say, and the parser
        discards half the file whatever the values are.
        """
        self._write_schedule(
            '{"scene-scan": {"every": 3600}, "scene-scan": {"every": 3600}}')

        with self.assertRaises(ValueError) as caught:
            load_schedule(env=self.env)

        self.assertIn("scene-scan", str(caught.exception))


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


class LoadZone(unittest.TestCase):
    """The ONE zone setting: the hour each unattended pass keeps, and the hour
    every timestamp on the page is shown in.

    One setting rather than two because the two disagreeing is worse than
    either being wrong: a page saying 3am while a pass runs at a different 3am
    is evidence FOR the schedule an operator is trying to check. Nothing here
    validates the name -- `cronicled.schedule.check_zone` is the one rule that
    does, and it is the same rule an override's own `zone` goes through.
    """

    def test_honours_the_environment_variable(self):
        self.assertEqual(load_zone({ZONE_ENV_VAR: "Europe/Madrid"}),
                         "Europe/Madrid")

    def test_nothing_configured_is_utc_and_not_the_hosts_zone(self):
        # UTC by name, spelled out here rather than compared to the constant
        # the code reads: a default that drifted to "localtime" or to the
        # host's own zone would satisfy `load_zone(...) == DEFAULT_ZONE` and
        # move every appointment by the deployment's offset.
        self.assertEqual(load_zone({}), "UTC")

    def test_a_variable_set_to_nothing_is_not_read_as_absence(self):
        # The `marker_tag` mistake, not repeated: `or DEFAULT_ZONE` would fold
        # an empty setting into UTC, which is the very behaviour an operator
        # who set the variable was trying to change, reported as success. It is
        # handed back as written so `check_zone` refuses it out loud.
        self.assertEqual(load_zone({ZONE_ENV_VAR: ""}), "")

    def test_it_hands_back_a_name_and_never_a_built_zone(self):
        # It reads configuration; it does not decide whether the configuration
        # is usable. A loader that returned a `tzinfo` would be a second
        # validator, free to accept a name `resolve` refuses.
        self.assertIsInstance(load_zone({ZONE_ENV_VAR: "Mars/Olympus"}), str)
