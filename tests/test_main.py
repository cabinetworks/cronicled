import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cronicled.__main__ import build_scheduler, main
from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.config import CONFIG_DIR_ENV_VAR
from cronicled.jobs import JobRunner
from cronicled.runscan import SCHEDULED_SCAN_NAME, build_producer
from cronicled.scan import ScanProducer
from cronicled.stash import Stash
from cronicled.store import Store
from cronicled.web.actions import Actions

WAIT = 10

NOW = datetime(2026, 7, 27, 3, 0, 0, tzinfo=timezone.utc)

# A store that exists nowhere. Every field is invented; `.invalid` is the
# reserved TLD that cannot resolve, so nothing here can reach anything even
# if a fake were removed by accident.
_ADAPTER_SPEC = {"name": "invented", "display": "An Invented Store",
                 "scraper_id": "InventedStore", "owner_source": "url_segment",
                 "owner_segment": 3, "title_match_counts_as_ownership": True}


def _ago(**delta):
    return (NOW - timedelta(**delta)).isoformat()


class _ReadOnlyStash:
    """Everything a scan asks a media server for, answering an empty library.

    It matches the real client's LIMITATIONS rather than offering a
    convenience it does not have: a scan enumerates the unorganized set, asks
    whichever stash-boxes are configured (none, as on a fresh install), and
    looks candidates up. It never writes, and any other call raises -- these
    tests are the first place a write introduced by the SCHEDULING wiring
    would show up.

    `gate` holds `unorganized_scenes` open so a test can look at a scan that
    is genuinely still running rather than at a fake that says it is. Bounded,
    and the bound is only ever reached by a test that is already failing.
    """

    def __init__(self, url=None, api_key=None):
        self.url = url
        self.api_key = api_key
        self.gate = None
        self.calls = []

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", limit))
        if self.gate is not None:
            self.gate.wait(WAIT)
        return 0, []

    def stash_boxes(self):
        self.calls.append(("stash_boxes",))
        return []

    def scrape_scene_url(self, url):
        self.calls.append(("scrape_scene_url", url))
        return None

    def scrape_scenes_by_query(self, scraper_id, query):
        self.calls.append(("scrape_scenes_by_query", scraper_id, query))
        return []

    def scrape_scenes_by_fingerprint(self, endpoint, scene_ids):
        self.calls.append(("scrape_scenes_by_fingerprint", endpoint,
                           list(scene_ids)))
        return [[] for _ in scene_ids]

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the scheduled scan called %r on the media server; a scan "
                "reads and looks things up, it never writes" % (name,))
        return refuse


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


class AppliedSectionWiring(_Base):
    """Ticket 98: an applied proposal moves out of the main inbox list and
    into its own section -- wired here in `main()`'s own callables, not by
    narrowing `Store.items()`'s default (see `cronicled/__main__.py`'s
    comment on `_inbox_rows` for why: `Actions._find`, and so `undo`, still
    needs `items(state=None)` to include an applied row).
    """

    def _seed_and_apply(self, subject_id="1"):
        store = Store(self.db_path)
        fp = store.record(
            folder="scene-matches", subject_type="scene",
            subject_id=subject_id, summary="a proposal",
            payload={"path": "/library/reel.mp4",
                     "creator": {"name": "Someone", "source": "folder",
                                 "competing": None, "rejected_folder": None},
                     "candidate": {"id": "c-1", "title": "A Title",
                                   "image": None, "performers": [],
                                   "studio": None},
                     "score": 0.9, "runners_up": []},
            producer="test-producer", confidence=0.9)
        store.mark_applied(fp, prior_state={"title": "old"})
        store.close()
        return fp

    def test_an_applied_row_does_not_appear_in_the_main_inbox_list(self):
        self._seed_and_apply(subject_id="1")
        self._seed(subject_id="2")  # an ordinary, still-open proposal
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        rows = captured.kwargs["rows"]()
        self.assertEqual([r.state for r in rows], ["new"])

    def test_an_applied_row_reaches_the_applied_section(self):
        fp = self._seed_and_apply()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        applied = captured.kwargs["applied"]()
        self.assertEqual([r.fingerprint for r in applied], [fp])

    def test_a_reverted_row_leaves_applied_and_returns_to_the_inbox(self):
        # HARM this pins: a reverted row is not an applied one (ticket 98).
        # Undo already moves it out of `applied` in the store; the wiring
        # here must not put it back by, say, including "reverted" in
        # whatever the Applied section asks `items()` for.
        fp = self._seed_and_apply()
        store = Store(self.db_path)
        store.mark_reverted(fp)
        store.close()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        self.assertEqual(captured.kwargs["applied"](), [])
        rows = captured.kwargs["rows"]()
        self.assertEqual([r.fingerprint for r in rows], [fp])
        self.assertEqual(rows[0].state, "reverted")


class SceneUrlWiring(_Base):
    """Ticket 97: the configured `--server` address reaches every row
    builder as its `base_url` -- reused from the same resolution `Stash`
    itself was built from, never a second, separate piece of
    configuration."""

    def test_a_configured_server_reaches_a_rows_scene_url(self):
        self._seed(subject_id="55")
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path, "--server", "http://media.example"])
        rows = captured.kwargs["rows"]()
        self.assertEqual(rows[0].scene_url, "http://media.example/scenes/55")

    def test_no_configured_server_leaves_a_rows_scene_url_none(self):
        self._seed(subject_id="55")
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        rows = captured.kwargs["rows"]()
        self.assertIsNone(rows[0].scene_url)

    def test_the_muted_sections_link_uses_the_same_configured_server(self):
        store = Store(self.db_path)
        store.mute("scene", "9", reason="never identifiable")
        store.close()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path, "--server", "http://media.example"])
        muted = captured.kwargs["muted"]()
        self.assertEqual(muted[0]["scene_url"],
                         "http://media.example/scenes/9")


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


class ScheduledScanWiring(_Base):
    """Registering a scan at start-up and resolving a schedule over it.

    Every test here goes through `build_scheduler` and then calls `tick`
    directly, on this thread, with `now` handed in. That is deliberate:
    `cronicled.schedule` keeps every scheduling rule as arithmetic over
    arguments precisely so no test of one has to start a loop and wait for a
    clock to move. The loop's own lifecycle is `SchedulerLifecycleInMain`
    below, and it is the only place here that starts a thread.
    """

    def setUp(self):
        super().setUp()
        self.conf = os.path.join(self._dir, "conf")
        os.makedirs(self.conf)
        self.env = {CONFIG_DIR_ENV_VAR: self.conf}
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        # After the store's cleanup is registered, so it runs BEFORE it:
        # a worker still reading a database that has been closed under it
        # fails on a daemon thread, where nothing would report it.
        self.addCleanup(self._drain)
        self.runner = JobRunner(self.store)
        self.stash = _ReadOnlyStash("http://media.invalid")
        self.adapters = {"invented": DeclarativeAdapter(_ADAPTER_SPEC)}

    def _drain(self):
        for job in self.runner.jobs():
            self.runner.wait(job.id, WAIT)

    def _schedule_file(self, overrides):
        with open(os.path.join(self.conf, "schedule.json"), "w") as fh:
            json.dump(overrides, fh)

    def _build(self, **over):
        # Overrides by keyword rather than `None` defaults: `stash=None` is a
        # value a test passes on purpose here -- an install with no media
        # server -- and a helper reading it as "not supplied" would hand back
        # the configured one and quietly test the opposite of what was asked.
        args = {"stash": self.stash, "adapters": self.adapters}
        args.update(over)
        return build_scheduler(self.runner, self.store, args["stash"],
                               args["adapters"], env=self.env)

    def test_the_nightly_scan_is_in_the_schedule_the_first_tick_reads(self):
        # THE silent one. `Scheduler.__init__` resolves the schedule once,
        # from the producers the runner holds at that instant. Built before
        # the producer is registered it resolves an EMPTY registry: it
        # schedules nothing, raises nothing, and ticks on time forever
        # without ever starting anything. There is no exception to catch and
        # no line in any log -- the only symptom is an inbox that never
        # fills, weeks later.
        #
        # So this asserts the whole of what the first tick decided, not that
        # a scheduler was built: with the two statements swapped, `due` is
        # empty and `started` is empty and both assertions below fail.
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [SCHEDULED_SCAN_NAME])
        self.assertEqual(list(result.started), [SCHEDULED_SCAN_NAME])
        self.assertEqual(result.skipped, {})
        self.assertEqual(result.failed_to_start, {})
        # It started for real, through the runner, and ran to completion --
        # not merely "a name came back from the schedule".
        job_id = result.started[SCHEDULED_SCAN_NAME]
        self.assertTrue(self.runner.wait(job_id, WAIT))
        job = self.runner.job(job_id)
        self.assertEqual(job.state, "done", job.traceback)
        self.assertEqual(job.producer, SCHEDULED_SCAN_NAME)
        self.assertEqual(job.cost, "scraping")
        self.assertIn(("unorganized_scenes", None), self.stash.calls)
        # And the run was recorded against the moment the tick decided, so
        # the next one is counted from it.
        self.assertEqual(self.store.last_run(SCHEDULED_SCAN_NAME), result.at)

    def test_a_manual_scan_leaves_the_scheduled_producer_untouched(self):
        # `Actions.scan` builds a producer per click and `reregister`s it,
        # which REPLACES whatever holds the name. Sharing a name would make
        # somebody typing 25 into the box silently reconfigure the
        # unattended run -- and the next one, hours later with nobody
        # watching, would scan 25 files instead of the library.
        #
        # Asserting only that both producers exist would pass under exactly
        # that collision, because the replacement is registered under the
        # same name: the registry would still hold one entry per name. So
        # this asserts the scheduled producer is the SAME OBJECT, still
        # unbounded, still on its own cadence.
        scheduler = self._build()
        before = {p.name: p for p in self.runner.producers()}
        self.assertEqual(sorted(before), [SCHEDULED_SCAN_NAME])
        actions = Actions(self.store, self.stash, runner=self.runner,
                          adapters=self.adapters)

        job = actions.scan(25)
        self.assertTrue(self.runner.wait(job.id, WAIT))

        after = {p.name: p for p in self.runner.producers()}
        self.assertEqual(sorted(after),
                         sorted([ScanProducer.name, SCHEDULED_SCAN_NAME]))
        nightly = after[SCHEDULED_SCAN_NAME]
        self.assertIs(nightly, before[SCHEDULED_SCAN_NAME])
        self.assertIsNone(nightly._limit)
        self.assertEqual(nightly.every, 86400)
        self.assertEqual(after[ScanProducer.name]._limit, 25)
        # And the schedule still starts the unbounded one afterwards.
        result = scheduler.tick(NOW)
        self.assertEqual(list(result.started), [SCHEDULED_SCAN_NAME])

    def test_the_scheduled_and_manual_scans_never_scrape_at_once(self):
        # Asserted on the runner's own accounting, never on timing: the
        # manual scan is held open inside the media client, so there is a
        # genuinely running job to be refused rather than a race to win.
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.stash.gate = gate
        scheduler = self._build()
        actions = Actions(self.store, self.stash, runner=self.runner,
                          adapters=self.adapters)
        manual = actions.scan(25)

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [SCHEDULED_SCAN_NAME])
        self.assertEqual(result.started, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(list(result.skipped), [SCHEDULED_SCAN_NAME])
        self.assertIn("cost class", result.skipped[SCHEDULED_SCAN_NAME])
        running = [job.producer for job in self.runner.jobs()
                   if job.state == "running"]
        self.assertEqual(running, [ScanProducer.name])
        # Not recorded as having run, so it is still due on the next tick.
        # Recording a refusal as a run would lose a whole day's scan.
        self.assertIsNone(self.store.last_run(SCHEDULED_SCAN_NAME))

        gate.set()
        self.assertTrue(self.runner.wait(manual.id, WAIT))

    def test_a_producer_with_no_cadence_fails_at_startup_not_at_a_tick(self):
        # `resolve` refuses an enabled producer that declares no cadence, and
        # it refuses it where the schedule is wired up. That is what makes
        # every producer in the start-up registry a decision: registering a
        # second one without a cadence takes the process down with a stack
        # trace an operator reads, rather than leaving it unscheduled and
        # unmentioned. No tick is involved, which is the point.
        self.runner.register(build_producer(self.stash, self.adapters,
                                            self.store, limit=5))
        with self.assertRaisesRegex(ValueError, "cadence"):
            self._build()

    def test_the_cadence_is_overridable_through_the_existing_mechanism(self):
        self.store.record_run(SCHEDULED_SCAN_NAME, _ago(hours=2))
        self._schedule_file({SCHEDULED_SCAN_NAME: {"every": 3600}})
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [SCHEDULED_SCAN_NAME])

    def test_the_same_fixture_is_not_due_without_that_override(self):
        # The discriminating half. Without it the test above passes on a
        # fixture that was due anyway, and would go on passing with the
        # overrides never reaching `resolve` at all.
        self.store.record_run(SCHEDULED_SCAN_NAME, _ago(hours=2))
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [])
        self.assertEqual(result.started, {})
        self.assertEqual(list(result.skipped), [SCHEDULED_SCAN_NAME])
        self.assertIn("next due at", result.skipped[SCHEDULED_SCAN_NAME])

    def test_the_scheduled_scan_can_be_disabled_through_the_same_file(self):
        self._schedule_file({SCHEDULED_SCAN_NAME: {"enabled": False}})
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [])
        self.assertEqual(result.started, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(list(result.skipped), [SCHEDULED_SCAN_NAME])
        self.assertIn("disabled", result.skipped[SCHEDULED_SCAN_NAME])
        self.assertEqual(self.runner.jobs(), [])

    def test_an_override_naming_a_producer_that_does_not_exist_is_refused(self):
        # A typo in a producer name would otherwise leave the real producer
        # running on the cadence the operator believed they had changed.
        # Refused at start-up, by the one validator -- which is also the
        # proof that the file reaches `resolve` at all.
        self._schedule_file({"a-producer-that-was-never-registered":
                             {"every": 60}})
        with self.assertRaisesRegex(ValueError, "unknown producer"):
            self._build()

    def test_no_media_server_schedules_nothing_and_says_so(self):
        out = io.StringIO()
        with redirect_stdout(out):
            scheduler = self._build(stash=None)
        self.assertIsNone(scheduler)
        # Nothing registered either: a producer in the registry that no
        # schedule covers is the state `Scheduler.start` exists to refuse.
        self.assertEqual(self.runner.producers(), [])
        self.assertIn("no scan is scheduled", out.getvalue())
        self.assertIn("--server", out.getvalue())

    def test_no_configured_adapter_schedules_nothing_and_says_so(self):
        out = io.StringIO()
        with redirect_stdout(out):
            scheduler = self._build(adapters={})
        self.assertIsNone(scheduler)
        self.assertEqual(self.runner.producers(), [])
        self.assertIn("no scan is scheduled", out.getvalue())
        self.assertIn("adapters.json", out.getvalue())


class SchedulerLifecycleInMain(_Base):
    """The loop the entry point starts, and stops.

    A scheduler that is never started is a component, not a service; a
    scheduler that is never closed is a daemon thread scraping a media server
    after the thing that owned it has gone. Both are asserted through what
    `main()` handed to `serve`, so neither can be satisfied by inspecting the
    source.
    """

    def _run_main(self, argv_extra=()):
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "adapters.json"), "w") as fh:
            json.dump({"adapters": [_ADAPTER_SPEC]}, fh)
        captured = _CapturedServe()
        # The media client is replaced, not the scheduler: everything from
        # the registration to the loop to the shutdown is the real thing,
        # and only the socket at the far end of it is not.
        with patch("cronicled.__main__.Stash", _ReadOnlyStash):
            with patch("cronicled.__main__.serve", captured):
                with redirect_stdout(io.StringIO()):
                    main(["--db", self.db_path, "--config-dir", conf,
                          "--server", "http://media.invalid", *argv_extra])
        return captured

    def test_the_loop_ticks_and_is_closed_on_the_way_out(self):
        captured = self._run_main()

        status = captured.kwargs["schedule_status"]()
        # `closed` is the discriminator, and it is why `LoopStatus` carries
        # both fields: not running and closed is a clean shutdown, while not
        # running and NOT closed is a loop that died. A `main()` that never
        # closed the scheduler would leave the thread ticking and report
        # `closed=False`, and one that closed it without ever starting it
        # would report zero ticks.
        self.assertTrue(status.closed)
        self.assertFalse(status.running)
        self.assertGreaterEqual(status.ticks, 1)
        self.assertEqual(status.failures, 0, status.last_traceback)

    def test_what_the_loop_started_was_the_unbounded_nightly_scan(self):
        captured = self._run_main()

        result = captured.kwargs["schedule_status"]().last_result
        self.assertEqual(result.due, [SCHEDULED_SCAN_NAME])
        self.assertEqual(list(result.started), [SCHEDULED_SCAN_NAME])
        self.assertEqual(result.skipped, {})
        self.assertEqual(result.failed_to_start, {})

    def test_an_install_with_nothing_to_schedule_passes_no_status(self):
        # No media server and no adapter: the page must be able to say that
        # nothing is scheduled, which it cannot do if it is handed a status
        # that looks like an idle but healthy loop.
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            with redirect_stdout(io.StringIO()):
                main(["--db", self.db_path])
        self.assertIsNone(captured.kwargs["schedule_status"])


if __name__ == "__main__":
    unittest.main()
