import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from cronicled.__main__ import main
from cronicled.jobs import JobRunner
from cronicled.stash import Stash
from cronicled.store import Store


class _CapturedServe:
    """Stands in for `web.app.serve` so `main()` can be exercised without
    binding a socket or looping forever -- it records exactly what it was
    called with and returns."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs


class _Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._dir, "cronicled.sqlite3")
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)

    def _seed(self, subject_id="1"):
        # Opened and closed before `main()` runs its own `Store` on the same
        # path -- `Store` refuses a second handle on a path already open.
        store = Store(self.db_path)
        store.record(
            folder="scene-matches", subject_type="scene", subject_id=subject_id,
            summary="a proposal",
            payload={"path": "/library/reel.mp4",
                     "creator": {"name": "Someone", "source": "folder",
                                 "competing": None, "rejected_folder": None},
                     "candidate": {"id": "c-1", "title": "A Title"},
                     "score": 0.9, "runners_up": []},
            producer="test-producer", confidence=0.9)
        store.close()


class NoServerConfigured(_Base):
    # The AttributeError this covers: `Stash.__init__` does `url.rstrip("/")`
    # unconditionally, and `--server`/`--api-key` default to None with no
    # required flag forcing them -- so the entry point crashed on its own
    # documented invocation the moment a server wasn't supplied. The fix
    # picked here: start read-only rather than refuse outright, since nothing
    # about browsing or dismissing/muting a proposal needs a media server at
    # all (see cronicled/__main__.py's module docstring for the reasoning).

    def test_the_inbox_starts_without_a_server_and_warns(self):
        captured = _CapturedServe()
        out = io.StringIO()
        with patch("cronicled.__main__.serve", captured):
            with redirect_stdout(out):
                main(["--db", self.db_path])
        self.assertIn("WARNING", out.getvalue())
        self.assertIn("--server", out.getvalue())
        # The whole point: no AttributeError, and `serve` is still reached.
        self.assertIsNotNone(captured.kwargs)
        self.assertIsNone(captured.kwargs["actions"]._stash)

    def test_a_configured_server_prints_no_warning(self):
        captured = _CapturedServe()
        out = io.StringIO()
        with patch("cronicled.__main__.serve", captured):
            with redirect_stdout(out):
                main(["--db", self.db_path, "--server", "http://media.example"])
        self.assertNotIn("WARNING", out.getvalue())
        self.assertIsInstance(captured.kwargs["actions"]._stash, Stash)


class MainWiring(_Base):
    # Four mutations reported as surviving with the suite green: the host
    # argument being dropped in favour of a hardcoded "0.0.0.0", the two
    # constructor calls having their arguments swapped, and the rows callable
    # being replaced by one that always answers empty. Each is targeted here
    # by inspecting what `main()` actually built and handed to `serve`,
    # rather than only what the wiring *looks* like from the source.

    def test_the_configured_host_reaches_serve(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path, "--host", "127.0.0.2"])
        self.assertEqual(captured.kwargs["host"], "127.0.0.2")

    def test_stash_is_built_from_server_then_api_key_in_that_order(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path,
                  "--server", "http://media.example",
                  "--api-key", "topsecret123"])
        stash = captured.kwargs["actions"]._stash
        self.assertIsInstance(stash, Stash)
        self.assertEqual(stash.url, "http://media.example/graphql")
        self.assertEqual(stash.api_key, "topsecret123")

    def test_actions_is_wired_with_the_store_then_the_stash_in_that_order(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path,
                  "--server", "http://media.example",
                  "--api-key", "topsecret123"])
        actions = captured.kwargs["actions"]
        # A swap leaves `_store` holding the Stash and `_stash` holding the
        # Store -- the types alone tell them apart without needing a separate
        # handle on the real objects main() built internally.
        self.assertIsInstance(actions._store, Store)
        self.assertIsInstance(actions._stash, Stash)

    def test_rows_reflects_what_is_actually_in_the_store(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        rows = captured.kwargs["rows"]()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].filename, "reel.mp4")


class ScanWiring(_Base):
    # The runner a request's `/scan` starts a job on has to outlive that
    # request -- so it must be something `main()` builds once and hands to
    # `actions`, not something a handler could construct per-request and
    # lose the moment the connection closes.

    def test_a_job_runner_reaches_actions(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        self.assertIsInstance(captured.kwargs["actions"]._runner, JobRunner)

    def test_the_same_runner_backs_the_actions_scan_status_callable(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        actions = captured.kwargs["actions"]
        # Bound-method equality: same underlying object and function, not
        # merely the same behaviour by coincidence -- a `scan_status` that
        # `main()` built fresh from a DIFFERENT runner would still equal
        # this by return value alone, on an empty runner, without this
        # checking they are the same object.
        self.assertEqual(captured.kwargs["scan_status"], actions.scan_status)

    def test_with_no_adapters_configured_the_adapter_is_none_not_a_crash(self):
        # A fresh install (no config/adapters.json committed -- this repo's
        # own working tree has none) is a legitimate state: the app must
        # still start, with `Actions.scan` left to give its own clear
        # refusal only once someone actually presses Scan.
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        self.assertIsNone(captured.kwargs["actions"]._adapter)


if __name__ == "__main__":
    unittest.main()
