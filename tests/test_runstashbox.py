"""`cronicled.runstashbox` is the first thing that can actually run
`cronicled.stashbox_scan.StashBoxCheckProducer` against a real media server
and a real stash-box instance: it wires a `Stash`, a `StashBox`, an
operator-maintained `performer_ids` mapping and a `Store` together, and gives
that a command line, the same shape `cronicled.runscan` already gives
`ScanProducer`.

No test here opens a socket or touches the real environment or filesystem
outside a temporary directory this file creates and cleans up itself.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from cronicled.jobs import JobRunner
from cronicled.runstashbox import (
    build_producer, default_performer_ids_path, load_performer_ids, main)
from cronicled.stash import StashError
from cronicled.stashbox import SourceListing
from cronicled.store import Store


def _patched_except_producer():
    return mock.patch.multiple(
        "cronicled.runstashbox",
        load_server=mock.DEFAULT,
        load_stashbox=mock.DEFAULT,
        load_performer_ids=mock.DEFAULT,
        Stash=mock.DEFAULT,
        StashBox=mock.DEFAULT,
        Store=mock.DEFAULT,
    )


def _patched_all():
    return mock.patch.multiple(
        "cronicled.runstashbox",
        load_server=mock.DEFAULT,
        load_stashbox=mock.DEFAULT,
        load_performer_ids=mock.DEFAULT,
        Stash=mock.DEFAULT,
        StashBox=mock.DEFAULT,
        Store=mock.DEFAULT,
        JobRunner=mock.DEFAULT,
        build_producer=mock.DEFAULT,
    )


class BuildProducerRequiresALimit(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_limit_none_is_refused(self):
        with self.assertRaises(ValueError):
            build_producer(mock.Mock(), mock.Mock(), {}, self.store, limit=None)

    def test_omitting_the_argument_entirely_is_refused_too(self):
        with self.assertRaises(TypeError):
            build_producer(mock.Mock(), mock.Mock(), {}, self.store)

    def test_limit_zero_is_accepted_as_a_deliberate_instruction(self):
        producer = build_producer(
            mock.Mock(), mock.Mock(), {}, self.store, limit=0)
        self.assertEqual(producer._limit, 0)

    def test_the_performer_ids_mapping_reaches_the_producer(self):
        producer = build_producer(
            mock.Mock(), mock.Mock(), {"Velvet Crane": "pf-1"}, self.store,
            limit=10)
        self.assertEqual(producer._performer_ids, {"Velvet Crane": "pf-1"})


class LoadPerformerIds(unittest.TestCase):
    def test_an_absent_file_is_a_legitimate_empty_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                load_performer_ids(os.path.join(d, "performer_ids.json")), {})

    def test_a_present_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "performer_ids.json")
            with open(p, "w") as fh:
                json.dump({"Velvet Crane": "pf-1"}, fh)
            self.assertEqual(load_performer_ids(p), {"Velvet Crane": "pf-1"})

    def test_finds_the_file_under_cronicled_config_dir_with_no_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "performer_ids.json"), "w") as fh:
                json.dump({"Velvet Crane": "pf-1"}, fh)
            env = {"CRONICLED_CONFIG_DIR": d}
            self.assertEqual(load_performer_ids(env=env),
                             {"Velvet Crane": "pf-1"})

    def test_default_path_is_built_under_config_dir(self):
        env = {"CRONICLED_CONFIG_DIR": "/mnt/config"}
        self.assertEqual(default_performer_ids_path(env),
                         "/mnt/config/performer_ids.json")


class MainRequiresALimitFlag(unittest.TestCase):
    def test_omitting_limit_exits_before_touching_any_config(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--db", ":memory:"])


class MainOrchestration(unittest.TestCase):
    """Everything `main` constructs is replaced by a double, mirroring
    `tests.test_runscan.MainOrchestration`: what is pinned here is the ORDER
    and the ARGUMENTS `main` wires together, not the check's own mechanics,
    which belong to `tests/test_stashbox_scan.py`.
    """

    def _patched(self):
        return mock.patch.multiple(
            "cronicled.runstashbox",
            load_server=mock.DEFAULT,
            load_stashbox=mock.DEFAULT,
            load_performer_ids=mock.DEFAULT,
            Stash=mock.DEFAULT,
            StashBox=mock.DEFAULT,
            Store=mock.DEFAULT,
            JobRunner=mock.DEFAULT,
            build_producer=mock.DEFAULT,
        )

    def test_it_wires_server_stashbox_producer_and_runner_together(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            mocks["load_performer_ids"].return_value = {"Velvet Crane": "pf-1"}
            producer = mock.Mock()
            producer.name = "stashbox-check"
            mocks["build_producer"].return_value = producer
            runner = mocks["JobRunner"].return_value
            job = mock.Mock(id="job-1")
            runner.start.return_value = job
            runner.job.return_value = mock.Mock(
                id="job-1", state="done",
                message="finished: checked 1, 1 unlisted, 0 present, "
                       "0 inconclusive, 0 skipped", error=None)
            store_instance = mocks["Store"].return_value

            rc = main(["--limit", "5", "--db", "irrelevant.sqlite3"])

            self.assertEqual(rc, 0)
            mocks["Stash"].assert_called_once_with(
                "http://server.example.test", "K")
            mocks["StashBox"].assert_called_once_with(
                "http://box.example.test", "BK")
            _, kwargs = mocks["build_producer"].call_args
            self.assertEqual(kwargs["limit"], 5)
            args, _ = mocks["build_producer"].call_args
            self.assertEqual(args[2], {"Velvet Crane": "pf-1"})
            runner.register.assert_called_once_with(producer)
            runner.start.assert_called_once_with("stashbox-check", trigger="manual")
            runner.wait.assert_called_once_with("job-1")
            store_instance.close.assert_called_once()

    def test_a_failed_job_is_reported_and_exits_nonzero(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            mocks["load_performer_ids"].return_value = {}
            producer = mock.Mock()
            producer.name = "stashbox-check"
            mocks["build_producer"].return_value = producer
            runner = mocks["JobRunner"].return_value
            runner.start.return_value = mock.Mock(id="job-1")
            runner.job.return_value = mock.Mock(
                id="job-1", state="failed", message="finished: checked 0",
                error="StashError: unreachable")

            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)

    def test_an_unconfigured_server_is_refused_before_anything_is_built(self):
        with self._patched() as mocks:
            mocks["load_server"].side_effect = ValueError(
                "missing media-server config: url, api_key")

            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)
            mocks["Stash"].assert_not_called()
            mocks["StashBox"].assert_not_called()
            mocks["Store"].assert_not_called()
            mocks["build_producer"].assert_not_called()

    def test_an_unconfigured_stashbox_endpoint_is_refused_before_anything_is_built(self):
        # A missing media-server config raises (load_server's own contract);
        # a missing STASH-BOX config does not -- see `load_stashbox` -- so
        # this checks the OTHER half: `main` itself refuses to proceed
        # rather than handing `None` on to `StashBox(...)`.
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = None

            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)
            self.assertIn("no stash-box endpoint is configured", err.getvalue())
            mocks["StashBox"].assert_not_called()
            mocks["Store"].assert_not_called()
            mocks["build_producer"].assert_not_called()


class EndToEndWithRealProducer(unittest.TestCase):
    """One test that does NOT replace `build_producer` or `JobRunner`, so a
    wiring mistake in how `main` calls the real, unmocked producer -- an
    argument in the wrong position, a keyword misspelled -- would show up
    here even if every mocked test above stayed green.
    """

    def test_a_real_run_against_fakes_completes(self):
        class _FakeStash:
            def unorganized_scenes(self, limit):
                return 0, []

            def performers_with_stash_ids(self):
                return []

        with _patched_except_producer() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            mocks["load_performer_ids"].return_value = {}
            mocks["Stash"].return_value = _FakeStash()
            mocks["StashBox"].return_value = mock.Mock()
            mocks["Store"].return_value = Store(":memory:")

            rc = main(["--limit", "5"])

        self.assertEqual(rc, 0)


BOX_ENDPOINT = "http://box.example.test/graphql"


class DerivedPerformerIdsReachTheCheck(unittest.TestCase):
    """The whole point of ticket #81: a performer id nothing but an
    operator-typed JSON file used to supply now comes from the media
    server's own performer records (`cronicled.performer_ids
    .derive_performer_ids`). This runs `main` for real -- no mock in place
    of `build_producer` or `JobRunner` -- against a fake media server that
    exposes exactly one performer, linked to the configured stash-box
    endpoint, and NO manual `performer_ids.json` mapping at all.
    """

    class _FakeStash:
        def __init__(self, scenes, performers):
            self._scenes = list(scenes)
            self._performers = list(performers)

        def unorganized_scenes(self, limit):
            return len(self._scenes), list(self._scenes)

        def performers_with_stash_ids(self):
            return list(self._performers)

    def _run(self, path):
        scene = {"id": "1", "files": [{"path": path}]}
        performer = {"id": "pf-1", "name": "Velvet Crane",
                    "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-1"}]}
        with _patched_except_producer() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            mocks["load_performer_ids"].return_value = {}
            mocks["Stash"].return_value = self._FakeStash([scene], [performer])
            box = mock.Mock()
            box.url = BOX_ENDPOINT
            # `total`/`pages_read` are required: a listing has to say how
            # much of itself it read, because "complete" is a claim about a
            # paged read and nothing else may stand in for it.
            box.performer_listing.return_value = SourceListing(
                "pf-1", [], complete=True, total=0, pages_read=1)
            mocks["StashBox"].return_value = box
            mocks["Store"].return_value = Store(":memory:")

            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = main(["--limit", "5"])
            box_seen = box
        return rc, out.getvalue(), box_seen

    def test_an_uncontested_file_is_checked_using_the_derived_id(self):
        rc, output, box = self._run("/library/Velvet Crane/Morning Ritual.mp4")

        self.assertEqual(rc, 0)
        box.performer_listing.assert_called_once()
        self.assertEqual(box.performer_listing.call_args[0][0], "pf-1")
        self.assertIn("checked 1", output)

    def test_a_contested_attribution_is_not_settled_by_a_derived_id_either(self):
        # HARM (acceptance criterion): a performer id that WAS successfully
        # derived from the media server's own records must not make a
        # contested folder/filename disagreement read as though it were
        # settled. Mutating away the attribution_certain guard anywhere on
        # this path would turn this file's verdict from "inconclusive" into
        # a confident "unlisted"/"present" -- a wrong answer sent to a
        # reviewer with nothing in it to say the attribution was ever in
        # doubt.
        rc, output, box = self._run(
            "/library/Velvet Crane/Ivy Thorn - Morning Ritual.mp4")

        self.assertEqual(rc, 0)
        # the id WAS available and WAS used to read a listing...
        box.performer_listing.assert_called_once_with(
            "pf-1", per_page=mock.ANY, max_pages=mock.ANY, timeout=mock.ANY)
        # ...but the folder/filename disagreement still downgrades the verdict
        self.assertIn("checked 1, 0 unlisted, 0 present, 1 inconclusive, 0 skipped",
                      output)


class _FakeStashOfPerformers:
    def __init__(self, performers):
        self._performers = list(performers)

    def unorganized_scenes(self, limit):
        return 0, []

    def performers_with_stash_ids(self):
        return list(self._performers)


def _done(message="finished: checked 0"):
    return mock.Mock(id="job-1", state="done", message=message, error=None)


class ManualAndDerivedPerformerIdsMerge(unittest.TestCase):
    """`main` merges an operator's `performer_ids.json` with whatever
    `cronicled.performer_ids.derive_performer_ids` reads off the media
    server -- see that module's own docstring for why an operator's entry
    always wins for a name it names at all, and why a name the server's own
    performer records disagree about is reported rather than guessed.
    `build_producer` and `JobRunner` are replaced here (mirroring
    `MainOrchestration`): what these tests pin is the MAPPING `main`
    assembles, not the check's own mechanics.
    """

    def _run(self, stash, manual, argv=("--limit", "5")):
        with _patched_all() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            mocks["load_performer_ids"].return_value = manual
            mocks["Stash"].return_value = stash
            box = mock.Mock()
            box.url = BOX_ENDPOINT
            mocks["StashBox"].return_value = box
            producer = mock.Mock()
            producer.name = "stashbox-check"
            mocks["build_producer"].return_value = producer
            runner = mocks["JobRunner"].return_value
            runner.start.return_value = mock.Mock(id="job-1")
            runner.job.return_value = _done()

            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = main(list(argv))

            args, _ = mocks["build_producer"].call_args
        return rc, args[2], err.getvalue()

    def test_a_derived_only_name_reaches_the_producer_alongside_a_manual_one(self):
        stash = _FakeStashOfPerformers([
            {"id": "pf-1", "name": "Velvet Crane",
             "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-derived"}]},
        ])

        rc, performer_ids, _ = self._run(stash, {"Ivy Thorn": "pf-manual"})

        self.assertEqual(rc, 0)
        self.assertEqual(performer_ids, {"Ivy Thorn": "pf-manual",
                                         "Velvet Crane": "pf-derived"})

    def test_a_manual_entry_overrides_a_conflicting_derived_one(self):
        stash = _FakeStashOfPerformers([
            {"id": "pf-1", "name": "Velvet Crane",
             "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-derived"}]},
        ])

        rc, performer_ids, _ = self._run(stash, {"Velvet Crane": "pf-manual"})

        self.assertEqual(rc, 0)
        self.assertEqual(performer_ids, {"Velvet Crane": "pf-manual"})

    def test_an_ambiguous_derived_name_is_reported_and_excluded_not_guessed(self):
        # HARM (acceptance criterion): two performers this library holds
        # share a name and disagree about the id at this endpoint. Taking
        # whichever one the server happened to list first would file a
        # listing read under a performer nobody confirmed is this file's
        # creator -- exactly the wrong-person read `cronicled.stashbox`'s
        # own docstring calls the sharp edge of this feature.
        stash = _FakeStashOfPerformers([
            {"id": "pf-1", "name": "Ivy Thorn",
             "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-1"}]},
            {"id": "pf-2", "name": "Ivy Thorn",
             "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-2"}]},
        ])

        rc, performer_ids, stderr = self._run(stash, {})

        self.assertEqual(rc, 0)
        self.assertNotIn("Ivy Thorn", performer_ids)
        self.assertIn("Ivy Thorn", stderr)
        self.assertIn("pf-1", stderr)
        self.assertIn("pf-2", stderr)

    def test_a_manual_entry_settles_an_ambiguous_derived_name(self):
        stash = _FakeStashOfPerformers([
            {"id": "pf-1", "name": "Ivy Thorn",
             "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-1"}]},
            {"id": "pf-2", "name": "Ivy Thorn",
             "stash_ids": [{"endpoint": BOX_ENDPOINT, "stash_id": "pf-2"}]},
        ])

        rc, performer_ids, stderr = self._run(stash, {"Ivy Thorn": "pf-2"})

        self.assertEqual(rc, 0)
        self.assertEqual(performer_ids.get("Ivy Thorn"), "pf-2")
        self.assertNotIn("Ivy Thorn", stderr)


class DerivationFailureAbortsBeforeAnythingRuns(unittest.TestCase):
    def test_a_media_server_failure_while_deriving_ids_is_reported(self):
        class _FailingStash:
            def performers_with_stash_ids(self):
                raise StashError("cannot reach the media server")

        with _patched_all() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            mocks["load_performer_ids"].return_value = {}
            mocks["Stash"].return_value = _FailingStash()
            box = mock.Mock()
            box.url = BOX_ENDPOINT
            mocks["StashBox"].return_value = box

            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)
            self.assertIn("could not read performer ids", err.getvalue())
            mocks["build_producer"].assert_not_called()
            mocks["Store"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
