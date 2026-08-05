"""`cronicled.runenrichment` wires a `Stash`, a `StashBox` and a `Store`
together and gives that a command line, the same shape
`cronicled.runstashbox` already gives `StashBoxCheckProducer`.

No test here opens a socket or touches the real environment or filesystem.
"""
import contextlib
import io
import unittest
from unittest import mock

from cronicled.runenrichment import build_producer, main


class BuildProducerRequiresALimit(unittest.TestCase):
    def test_limit_none_is_refused(self):
        with self.assertRaises(ValueError):
            build_producer(mock.Mock(), mock.Mock(), limit=None)

    def test_omitting_the_argument_entirely_is_refused_too(self):
        with self.assertRaises(TypeError):
            build_producer(mock.Mock(), mock.Mock())

    def test_limit_zero_is_accepted_as_a_deliberate_instruction(self):
        producer = build_producer(mock.Mock(), mock.Mock(), limit=0)
        self.assertEqual(producer._limit, 0)


class MainRequiresALimitFlag(unittest.TestCase):
    def test_omitting_limit_exits_before_touching_any_config(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--db", ":memory:"])


class MainOrchestration(unittest.TestCase):
    """What is pinned here is the ORDER and the ARGUMENTS `main` wires
    together, not the producer's own mechanics, which belong to
    `tests/test_enrichment.py`.
    """

    def _patched(self):
        return mock.patch.multiple(
            "cronicled.runenrichment",
            load_server=mock.DEFAULT,
            load_stashbox=mock.DEFAULT,
            Stash=mock.DEFAULT,
            StashBox=mock.DEFAULT,
            Store=mock.DEFAULT,
            JobRunner=mock.DEFAULT,
            build_producer=mock.DEFAULT,
        )

    def _ok_job(self, mocks, message="finished: 0 proposed"):
        runner = mocks["JobRunner"].return_value
        runner.start.return_value = mock.Mock(id="job-1")
        runner.job.return_value = mock.Mock(
            id="job-1", state="done", message=message, error=None)
        return runner

    def test_it_wires_server_stashbox_producer_and_runner_together(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            producer = mock.Mock()
            producer.name = "performer-enrichment"
            mocks["build_producer"].return_value = producer
            runner = self._ok_job(mocks)
            store_instance = mocks["Store"].return_value

            rc = main(["--limit", "5", "--db", "irrelevant.sqlite3"])

            self.assertEqual(rc, 0)
            mocks["Stash"].assert_called_once_with(
                "http://server.example.test", "K")
            mocks["StashBox"].assert_called_once_with(
                "http://box.example.test", "BK")
            _, kwargs = mocks["build_producer"].call_args
            self.assertEqual(kwargs["limit"], 5)
            runner.register.assert_called_once_with(producer)
            runner.start.assert_called_once_with(
                "performer-enrichment", trigger="manual")
            runner.wait.assert_called_once_with("job-1")
            store_instance.close.assert_called_once()

    def test_no_stashbox_configured_still_runs_with_a_none_box(self):
        # The same "not configured" state `cronicled.runstashbox` treats as
        # ordinary rather than fatal -- `EnrichmentProducer` reads a `None`
        # box and proposes nothing rather than raising.
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = None
            producer = mock.Mock()
            producer.name = "performer-enrichment"
            mocks["build_producer"].return_value = producer
            self._ok_job(mocks)

            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 0)
            mocks["StashBox"].assert_not_called()
            args, _ = mocks["build_producer"].call_args
            self.assertIsNone(args[1])
            self.assertIn("no stash-box endpoint is configured", err.getvalue())

    def test_a_failed_job_is_reported_and_exits_nonzero(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            producer = mock.Mock()
            producer.name = "performer-enrichment"
            mocks["build_producer"].return_value = producer
            runner = mocks["JobRunner"].return_value
            runner.start.return_value = mock.Mock(id="job-1")
            runner.job.return_value = mock.Mock(
                id="job-1", state="failed", message="finished: 0 proposed",
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

    def test_the_folder_argument_reaches_the_producer(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["load_stashbox"].return_value = {
                "url": "http://box.example.test", "api_key": "BK"}
            producer = mock.Mock()
            producer.name = "performer-enrichment"
            mocks["build_producer"].return_value = producer
            self._ok_job(mocks)

            self.assertEqual(
                main(["--limit", "5", "--folder", "second-library"]), 0)

            _, kwargs = mocks["build_producer"].call_args
            self.assertEqual(kwargs["folder"], "second-library")


if __name__ == "__main__":
    unittest.main()
