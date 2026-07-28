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
from cronicled.store import Store


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
            runner.start.assert_called_once_with("stashbox-check")
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

        with self._patched_except_producer() as mocks:
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

    def _patched_except_producer(self):
        return mock.patch.multiple(
            "cronicled.runstashbox",
            load_server=mock.DEFAULT,
            load_stashbox=mock.DEFAULT,
            load_performer_ids=mock.DEFAULT,
            Stash=mock.DEFAULT,
            StashBox=mock.DEFAULT,
            Store=mock.DEFAULT,
        )


if __name__ == "__main__":
    unittest.main()
