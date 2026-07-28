import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from cronicled.__main__ import main
from cronicled.config import CONFIG_DIR_ENV_VAR
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
                     "candidate": {"id": "c-1", "title": "A Title",
                                   "image": None, "performers": [],
                                   "studio": None},
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


class HostAndPortEnvironmentDefaults(_Base):
    # Mirrors the coverage --db already has for $CRONICLED_DB: a container
    # can only pass these through ENV (see the Dockerfile), so the flag
    # having no working environment default at all is exactly the kind of
    # gap that shows up nowhere in a log -- the service starts, binds
    # whatever the argparse default happens to be, and looks fine right up
    # until nothing can reach it.

    def test_host_defaults_from_its_environment_variable(self):
        self._seed()
        captured = _CapturedServe()
        with patch.dict(os.environ, {"CRONICLED_HOST": "0.0.0.0"}):
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path])
        self.assertEqual(captured.kwargs["host"], "0.0.0.0")

    def test_port_defaults_from_its_environment_variable(self):
        self._seed()
        captured = _CapturedServe()
        with patch.dict(os.environ, {"CRONICLED_PORT": "9001"}):
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path])
        self.assertEqual(captured.kwargs["port"], 9001)

    def test_explicit_host_flag_overrides_the_environment_variable(self):
        self._seed()
        captured = _CapturedServe()
        with patch.dict(os.environ, {"CRONICLED_HOST": "0.0.0.0"}):
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path, "--host", "127.0.0.3"])
        self.assertEqual(captured.kwargs["host"], "127.0.0.3")

    def test_host_still_defaults_to_loopback_outside_a_container(self):
        # DEFAULT_HOST must not change for anyone running this directly --
        # only the image's own ENV is allowed to move the effective default.
        self._seed()
        captured = _CapturedServe()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRONICLED_HOST", None)
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path])
        self.assertEqual(captured.kwargs["host"], "127.0.0.1")


class ConfigDirThreading(_Base):
    # `--config-dir` has to reach `cronicled.config.config_dir` (and, when a
    # future caller needs them, `load_server`/`load_adapters`) through their
    # injectable `env` parameter rather than by mutating the real
    # environment -- see cronicled/config.py's module docstring for why.
    # `main()` proves the value actually reached that function by printing
    # what it resolved to; these tests read that line rather than reaching
    # into `main()`'s internals.

    def _run(self, argv, environ_overrides=None):
        captured = _CapturedServe()
        out = io.StringIO()
        with patch.dict(os.environ, environ_overrides or {}):
            with patch("cronicled.__main__.serve", captured):
                with redirect_stdout(out):
                    main(argv)
        return out.getvalue()

    def test_config_dir_flag_is_threaded_to_config_dir(self):
        output = self._run(["--db", self.db_path, "--config-dir", "/explicit/config"])
        self.assertIn("config directory: /explicit/config", output)

    def test_config_dir_also_reaches_the_adapters(self):
        # The seam this project has been bitten at repeatedly: two features
        # each correct on their own, joined by a call that quietly drops the
        # thing connecting them. `main()` prints the directory it resolved
        # AND loads adapters -- if the loader is called on the ambient
        # environment instead of the same `env`, the printed line is right,
        # the flag looks honoured, and the adapters come from somewhere else
        # entirely. Nothing raises.
        #
        # Asserted by loading a real adapters.json out of the directory the
        # flag names, so the only way to pass is for the value to have
        # travelled the whole distance.
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf)
        with open(os.path.join(conf, "adapters.json"), "w") as fh:
            json.dump({"adapters": [{"name": "invented",
                                     "display": "An Invented Store",
                                     "scraper_id": "InventedStore",
                                     "owner_source": "url_segment",
                                     "owner_segment": 3,
                                     "title_match_counts_as_ownership": True}]}, fh)
        captured = _CapturedServe()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path, "--config-dir", conf])
        adapters = captured.kwargs["actions"]._adapters
        self.assertIn(
            "invented", adapters,
            "the adapter directory the flag named was not read")
        adapter = adapters["invented"]
        self.assertEqual(adapter.name, "invented")
        self.assertEqual(adapter.scraper_id, "InventedStore")

    def test_config_dir_flag_does_not_mutate_os_environ(self):
        # Deliberately NOT run through `self._run`'s `patch.dict`: that
        # context manager restores `os.environ` to its pre-call snapshot on
        # exit regardless of what happened inside, which would silently
        # erase the very mutation this test exists to catch. This talks to
        # the real `os.environ` directly, with its own cleanup, so a write
        # main() makes is still visible after the call returns.
        self._seed()
        original = os.environ.get(CONFIG_DIR_ENV_VAR)

        def _restore():
            if original is None:
                os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            else:
                os.environ[CONFIG_DIR_ENV_VAR] = original
        self.addCleanup(_restore)

        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            with redirect_stdout(io.StringIO()):
                main(["--db", self.db_path, "--config-dir", "/explicit/config"])
        self.assertEqual(os.environ.get(CONFIG_DIR_ENV_VAR), original)

    def test_config_dir_flag_wins_over_the_ambient_environment_variable(self):
        output = self._run(
            ["--db", self.db_path, "--config-dir", "/explicit/config"],
            environ_overrides={CONFIG_DIR_ENV_VAR: "/ambient/config"})
        self.assertIn("config directory: /explicit/config", output)
        self.assertNotIn("/ambient/config", output)

    def test_config_dir_defaults_from_its_environment_variable(self):
        output = self._run(
            ["--db", self.db_path],
            environ_overrides={CONFIG_DIR_ENV_VAR: "/from/environment"})
        self.assertIn("config directory: /from/environment", output)

    def test_config_dir_falls_back_to_the_default_directory(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            output = self._run(["--db", self.db_path])
        self.assertIn("config directory: config", output)
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

    def test_with_no_adapters_configured_the_adapters_are_empty_not_a_crash(self):
        # A fresh install (no config/adapters.json committed -- this repo's
        # own working tree has none) is a legitimate state: the app must
        # still start, with `Actions.scan` left to give its own clear
        # refusal only once someone actually presses Scan.
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        self.assertEqual(captured.kwargs["actions"]._adapters, {})


if __name__ == "__main__":
    unittest.main()
