import importlib
import io
import json
import os
import pkgutil
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import cronicled
from cronicled import (descriptions, performer_tags, scan, tag_descriptions,
                       tag_hygiene, tags)
from cronicled.__main__ import (WAITING_SECTIONS, build_scheduler, main,
                                waiting_counts)
from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.artist import Aliases
from cronicled.config import CONFIG_DIR_ENV_VAR, RENAMED_JOBS, ZONE_ENV_VAR
from cronicled.descriptions import PRODUCER_NAME as DESCRIPTION_PRODUCER_NAME
from cronicled.jobs import JobRunner
from cronicled.performer_tags import index_performers, match_tag
from cronicled.performer_tags import proposal as reconcile_proposal
from cronicled.runscan import SCHEDULED_SCAN_NAME, build_producer
from cronicled.scan import ScanProducer
from cronicled.schedule import Entry, as_utc, due, resolve
from cronicled.stash import Stash, StashError
from cronicled.store import RUN_OUTCOME_INTERRUPTED, Store
from cronicled.tag_hygiene import NO_SCENES, ONE_SCENE
from cronicled.tag_hygiene import proposal as unused_proposal
from cronicled.tags import TagMergeProducer, cluster_tags
from cronicled.tags import proposal as tag_proposal
from cronicled.web.actions import Actions
from cronicled.web.rows import to_summary_view

WAIT = 10

NOW = datetime(2026, 7, 27, 3, 0, 0, tzinfo=timezone.utc)

# The zone the three unattended appointments are read in throughout this file.
#
# A REAL zone that observes daylight saving, and one whose offset is non-zero on
# BOTH sides of every transition (+01:00 in winter, +02:00 in summer). Neither
# property is decoration: a fixed-offset zone cannot tell an offset applied as a
# constant from one read from a zone, and a zone whose winter offset is zero
# would make half of these assertions identical to doing nothing at all.
#
# Because it is not UTC, `NOW` above (03:00 UTC, so 05:00 here in July) is
# deliberately NOT one of the appointments -- every fixture below states its own
# relationship to 03:00 local rather than inheriting one by coincidence.
ZONE_NAME = "Europe/Madrid"
ZONE = ZoneInfo(ZONE_NAME)

# A second zone, for the tests about WHICH zone is used. Half an hour off UTC
# and with no daylight saving of its own, so an instant computed in it cannot
# coincide with one computed in the zone above, or in UTC, by accident.
OTHER_ZONE_NAME = "Asia/Kolkata"
OTHER_ZONE = ZoneInfo(OTHER_ZONE_NAME)

# Every producer `build_scheduler` registers at start-up, sorted the way
# `schedule.due` sorts its answer. Imported from the modules that name them
# rather than copied, so renaming one cannot leave a test asserting the old
# name against a schedule that no longer has it.
#
# THREE, and named as a whole set rather than as "the scan and whatever else":
# every assertion below compares against this entire list, so a producer
# dropped from the registry -- or one added and never scheduled -- fails here
# rather than passing an `assertIn` that only ever looked for the scan.
ALL_PRODUCERS = sorted([SCHEDULED_SCAN_NAME, DESCRIPTION_PRODUCER_NAME,
                        TagMergeProducer.name])

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

    def performers_with_descriptions(self):
        # The one read the description producer makes. An empty library, the
        # same answer `unorganized_scenes` gives: these tests are about the
        # SCHEDULING wiring, and a producer that found something to propose
        # would only add store writes to what they have to assert about.
        self.calls.append(("performers_with_descriptions",))
        return []

    def all_tags(self):
        # The THIRD unattended producer's one read. An empty library holds no
        # tags, so it holds no duplicate spellings either -- the same "empty
        # library" answer `unorganized_scenes` gives above, present so the
        # tag-merge pass this wiring registers alongside the scan runs to a
        # real finish instead of falling through to `__getattr__`'s refusal.
        self.calls.append(("all_tags",))
        return []

    def stash_box_credentials(self):
        # The tag pass's second read. An install with no stash-box configured
        # is an ordinary state and is what these wiring tests want: the pass
        # asks nobody for a description and still runs to a real finish.
        self.calls.append(("stash_box_credentials",))
        return []

    def performers_with_aliases(self):
        # The tag pass's third read, for the half that looks for tags which are
        # really a performer filed as one. An empty library holds no performers
        # either, so no tag can match one and the pass still runs to a real
        # finish rather than falling through to the refusal below.
        self.calls.append(("performers_with_aliases",))
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


class _OfflineStash(Stash):
    """The client `main()` builds, minus the one thing no test may have: a
    transport that reaches a media server.

    `main()` constructs its own `Stash(url, api_key)`, so the seam the class
    already offers -- an injected transport -- is reached by replacing the
    name `__main__` looks the class up under. Everything the assertions below
    read is still the real constructor's work on the real arguments: this is
    a `Stash`, its `url` is what `main` resolved with `/graphql` appended,
    and its `api_key` is whatever was configured.

    It is not more capable than what it stands in for. A media server that
    cannot be reached raises a transient `StashError` out of the transport,
    and that is exactly what these tests were already getting -- the address
    they configure names nothing, so every start-up call failed. What is gone
    is the resolution attempt on the way to that failure, which is the only
    part that depended on the machine the suite ran on. Nothing here ANSWERS
    a query, because a fake that answered one would be standing in for a
    library these tests never set up.
    """

    def __init__(self, url, api_key):
        super().__init__(url, api_key, transport=self._refuse)

    def _refuse(self, body, timeout):
        raise StashError("no media server is reachable from a test",
                         transient=True)


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
        # Whole-module, not per-test: any `main()` here that is given a media
        # server address builds a client for it, and the scheduler it starts
        # takes the overnight passes' first tick immediately -- so the address
        # was reached for on a worker thread, long after the line that named
        # it. That is where all twelve of this suite's resolutions of names
        # off this machine came from, and a seam applied test by test would
        # have to be remembered by the next test that configures a server.
        patcher = patch("cronicled.__main__.Stash", _OfflineStash)
        patcher.start()
        self.addCleanup(patcher.stop)

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


    def _seed_merge(self, state=None):
        """A real tag-merge proposal, built through `cronicled.tags`' own
        producer path, in whichever `state` a person's decision would leave
        it. Opened and closed before `main()` runs its own `Store`."""
        store = Store(self.db_path)
        built = tag_proposal(cluster_tags([
            {"id": "1", "name": "Velvet Crane", "aliases": [], "description": None,
             "scene_count": 12},
            {"id": "9", "name": "VelvetCrane", "aliases": [], "description": None,
             "scene_count": 4}])[0], "library", [])
        fp = store.record(folder=built["folder"],
                          subject_type=built["subject_type"],
                          subject_id=built["subject_id"],
                          summary=built["summary"], payload=built["payload"],
                          producer="tag-merge")
        if state == "dismissed":
            store.dismiss(fp)
        elif state == "muted":
            store.mute(built["subject_type"], built["subject_id"])
        elif state == "applied":
            store.mark_applied(fp)
        store.close()
        return fp

    def _seed_reconcile(self, state=None, prior=None):
        """A real tag/performer reconciliation, built through
        `cronicled.performer_tags`' own proposal path, in whichever `state` a
        person's decision would leave it. Opened and closed before `main()`
        runs its own `Store`."""
        store = Store(self.db_path)
        index = index_performers(
            [{"id": "p-1", "name": "Marlowe Quill",
              "alias_list": ["Delia Ashgrove"]}])
        tag_row = {"id": "44", "name": "Delia Ashgrove", "aliases": [],
                   "description": None, "scene_count": 3}
        built = reconcile_proposal(tag_row, match_tag(tag_row, index),
                                   ["sc-1", "sc-2", "sc-3"], folder="library")
        fp = store.record(folder=built["folder"],
                          subject_type=built["subject_type"],
                          subject_id=built["subject_id"],
                          summary=built["summary"], payload=built["payload"],
                          producer="tag-merge")
        if state == "dismissed":
            store.dismiss(fp)
        elif state == "muted":
            store.mute(built["subject_type"], built["subject_id"])
        elif state == "applied":
            store.mark_applied(fp, prior_state=prior)
        elif state == "failed":
            store.mark_applied(fp, prior_state=prior)
            store.mark_failed(fp, "wrote 1 of 3 scenes and then stopped")
        store.close()
        return fp


    def _seed_unused(self, state=None, subject_id="55", name="Lantern Drift",
                     scene_count=0):
        """A real low-count tag proposal, built through
        `cronicled.tag_hygiene`'s own proposal path, in whichever `state` a
        person's decision would leave it."""
        store = Store(self.db_path)
        built = unused_proposal({"id": subject_id, "name": name,
                                 "aliases": [], "description": None,
                                 "scene_count": scene_count},
                                folder="library")
        fp = store.record(folder=built["folder"],
                          subject_type=built["subject_type"],
                          subject_id=built["subject_id"],
                          summary=built["summary"], payload=built["payload"],
                          producer="tag-merge")
        if state == "dismissed":
            store.dismiss(fp)
        elif state == "muted":
            store.mute(built["subject_type"], built["subject_id"])
        elif state == "applied":
            store.mark_applied(fp)
        store.close()
        return fp


class UnusedTagSectionWiring(_Base):
    """Where a low-count tag proposal is rendered, and -- just as much -- where
    it must NOT be.

    `to_row` INDEXES `payload["path"]` and `payload["candidate"]`, and this
    payload has neither. So one reaching any scene list is not an odd-looking
    row: it is a `KeyError` that takes the whole page down, and with it the
    inbox and every control on it. Each assertion calls the section's callable,
    which is the only way to see that -- `serve` receives functions, and a
    wiring mistake is invisible until one is invoked.
    """

    def _served(self):
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        return captured.kwargs

    def test_it_reaches_its_own_section_and_no_other(self):
        self._seed()              # one ordinary scene proposal
        self._seed_merge()        # and one tag merge
        self._seed_reconcile()    # and one reconciliation
        self._seed_unused()
        kwargs = self._served()

        groups = kwargs["unused"]()
        self.assertEqual([(g.group, g.count) for g in groups],
                         [(NO_SCENES, 1)])
        self.assertEqual([r.name for r in groups[0].rows], ["Lantern Drift"])
        self.assertEqual([r.filename for r in kwargs["rows"]()], ["reel.mp4"])
        self.assertEqual([r.key for r in kwargs["merges"]()], ["velvetcrane"])
        self.assertEqual([r.tag_name for r in kwargs["reconciles"]()],
                         ["Delia Ashgrove"])

    def test_every_scene_section_survives_one_in_the_store(self):
        for state in ("dismissed", "muted", "applied"):
            with self.subTest(state=state):
                self.setUp()
                self._seed_unused(state=state)
                kwargs = self._served()
                for section in ("rows", "muted", "dismissed", "superseded",
                                "applied"):
                    self.assertEqual(kwargs[section](), [], section)

    def test_a_dismissed_one_keeps_its_reversal_on_the_page(self):
        self._seed_unused(state="dismissed")

        groups = self._served()["unused"]()

        self.assertEqual([r.state for r in groups[0].rows], ["dismissed"])
        self.assertTrue(groups[0].rows[0].undismissable)

    def test_a_tag_somebody_kept_keeps_the_control_that_stops_keeping_it(self):
        self._seed_unused(state="muted")

        groups = self._served()["unused"]()

        self.assertEqual([r.state for r in groups[0].rows], ["muted"])
        self.assertTrue(groups[0].rows[0].unmutable)

    def test_an_applied_deletion_stays_visible_with_its_warning(self):
        self._seed_unused(state="applied")

        rows = self._served()["unused"]()[0].rows

        self.assertEqual([r.state for r in rows], ["applied"])
        self.assertIn("cannot be undone", rows[0].warning)

    def test_two_populations_reach_the_page_as_two_groups(self):
        self._seed_unused(subject_id="55", name="Lantern Drift",
                          scene_count=0)
        self._seed_unused(subject_id="56", name="Copper Kettle",
                          scene_count=1)
        self._seed_unused(subject_id="57", name="Harbour Ferry",
                          scene_count=1)

        groups = self._served()["unused"]()

        self.assertEqual([(g.group, g.count) for g in groups],
                         [(NO_SCENES, 1), (ONE_SCENE, 2)])

    def test_the_configured_server_reaches_every_rows_tag_link(self):
        self._seed_unused()

        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path, "--server", "http://server.invalid"])

        self.assertEqual(captured.kwargs["unused"]()[0].rows[0].tag_url,
                         "http://server.invalid/tags/55")


class MergeSectionWiring(_Base):
    """Where a tag-merge proposal is rendered, and -- just as much -- where
    it must NOT be.

    `to_row` INDEXES `payload["path"]` and `payload["candidate"]`; a merge
    payload has neither. So a merge item reaching any scene list is not an
    odd-looking row, it is a `KeyError` that takes the whole page down. Every
    assertion here calls the section's callable, which is the only way to see
    that: `serve` receives functions, and a wiring mistake is invisible until
    one is invoked.
    """

    def _served(self):
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        return captured.kwargs

    def test_a_merge_reaches_the_merges_section_and_no_other(self):
        self._seed()            # one ordinary scene proposal
        self._seed_merge()
        kwargs = self._served()

        merges = kwargs["merges"]()
        self.assertEqual([r.key for r in merges], ["velvetcrane"])
        self.assertEqual([r.filename for r in kwargs["rows"]()], ["reel.mp4"])

    def test_every_scene_section_survives_a_merge_in_the_store(self):
        # Each of these would raise rather than render if the merge item
        # reached it, and the failure would be a blank page rather than a
        # misplaced row.
        for state in ("dismissed", "muted", "applied"):
            with self.subTest(state=state):
                self.setUp()
                self._seed_merge(state=state)
                kwargs = self._served()
                for section in ("rows", "muted", "dismissed", "superseded",
                                "applied"):
                    self.assertEqual(kwargs[section](), [], section)

    def test_a_dismissed_merge_keeps_its_reversal_on_the_page(self):
        # `items()`'s default view hides a person's own rejections, so a
        # dismissed cluster is only reachable if this section reads for it
        # explicitly -- and without the row there is no Undismiss control.
        self._seed_merge(state="dismissed")

        rows = self._served()["merges"]()

        self.assertEqual([r.state for r in rows], ["dismissed"])
        self.assertTrue(rows[0].undismissable)

    def test_a_muted_merge_keeps_its_reversal_on_the_page(self):
        self._seed_merge(state="muted")

        rows = self._served()["merges"]()

        self.assertEqual([r.state for r in rows], ["muted"])
        self.assertTrue(rows[0].unmutable)

    def test_an_applied_merge_stays_visible_with_its_warning(self):
        self._seed_merge(state="applied")

        rows = self._served()["merges"]()

        self.assertEqual([r.state for r in rows], ["applied"])
        self.assertIn("cannot be undone", rows[0].warning)


class ReconcileSectionWiring(_Base):
    """Where a tag/performer reconciliation is rendered, and -- just as much
    -- where it must NOT be.

    `to_row` INDEXES `payload["path"]` and `payload["candidate"]`, and a
    reconciliation payload has neither. So one reaching any scene list is not
    an odd-looking row: it is a `KeyError` that takes the whole page down, and
    with it the inbox, the merge section and every control on them. Each
    assertion here calls the section's callable, which is the only way to see
    that -- `serve` receives functions, and a wiring mistake is invisible until
    one is invoked.
    """

    def _served(self):
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        return captured.kwargs

    def test_it_reaches_its_own_section_and_no_other(self):
        self._seed()             # one ordinary scene proposal
        self._seed_merge()       # and one tag merge
        self._seed_reconcile()
        kwargs = self._served()

        self.assertEqual([r.tag_name for r in kwargs["reconciles"]()],
                         ["Delia Ashgrove"])
        self.assertEqual([r.filename for r in kwargs["rows"]()], ["reel.mp4"])
        self.assertEqual([r.key for r in kwargs["merges"]()], ["velvetcrane"])

    def test_every_scene_section_survives_one_in_the_store(self):
        for state in ("dismissed", "muted", "applied"):
            with self.subTest(state=state):
                self.setUp()
                self._seed_reconcile(state=state)
                kwargs = self._served()
                for section in ("rows", "muted", "dismissed", "superseded",
                                "applied"):
                    self.assertEqual(kwargs[section](), [], section)

    def test_a_dismissed_one_keeps_its_reversal_on_the_page(self):
        self._seed_reconcile(state="dismissed")

        rows = self._served()["reconciles"]()

        self.assertEqual([r.state for r in rows], ["dismissed"])
        self.assertTrue(rows[0].undismissable)

    def test_a_muted_one_keeps_its_reversal_on_the_page(self):
        self._seed_reconcile(state="muted")

        rows = self._served()["reconciles"]()

        self.assertEqual([r.state for r in rows], ["muted"])
        self.assertTrue(rows[0].unmutable)

    def test_a_partly_applied_one_keeps_its_undo_on_the_page(self):
        # HARM: the scenes a partial run changed have no other way back from
        # the page at all, and the row has to be BUILT for the button to exist.
        self._seed_reconcile(state="failed",
                             prior={"tag_id": "44", "performer_id": "p-1",
                                    "attached": ["sc-1"],
                                    "untagged": ["sc-1"]})

        rows = self._served()["reconciles"]()

        self.assertEqual([r.state for r in rows], ["failed"])
        self.assertTrue(rows[0].undoable)
        self.assertFalse(rows[0].appliable)


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


class AnInterruptedRunIsReconciledAtStartup(_Base):
    """The observed incident: a container stops mid-run and leaves an open
    row nothing else will ever close. `main()` must close it, as the very
    first thing it does with the store -- see `Store.close_interrupted_runs`
    and the comment above where `__main__.py` calls it.
    """

    def _seed_open_run(self, at="2026-03-01T01:06:40+00:00"):
        """Open a run and walk away -- opened and closed before `main()`
        runs its own `Store` on the same path, exactly as `_seed` does for a
        proposal, and never finished, standing in for the process that
        stopped existing mid-pass."""
        store = Store(self.db_path)
        run_id = store.start_run("scene-scan", trigger="scheduled", at=at)
        store.close()
        return run_id

    def test_a_row_left_open_by_an_earlier_process_is_closed(self):
        orphan = self._seed_open_run()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        row = [r for r in captured.kwargs["actions"]._store.recent_runs()
              if r["id"] == orphan][0]
        self.assertIsNotNone(row["finished"])
        self.assertEqual(row["outcome"], RUN_OUTCOME_INTERRUPTED)

    def test_startup_reports_how_many_it_closed(self):
        self._seed_open_run()
        self._seed_open_run(at="2026-03-01T01:06:41+00:00")
        captured = _CapturedServe()
        out = io.StringIO()
        with patch("cronicled.__main__.serve", captured):
            with redirect_stdout(out):
                main(["--db", self.db_path])
        self.assertIn("closed 2", out.getvalue())

    def test_a_fresh_install_with_nothing_open_says_nothing_about_it(self):
        captured = _CapturedServe()
        out = io.StringIO()
        with patch("cronicled.__main__.serve", captured):
            with redirect_stdout(out):
                main(["--db", self.db_path])
        self.assertNotIn("run log", out.getvalue())

    def test_a_finished_run_from_before_the_restart_is_untouched(self):
        store = Store(self.db_path)
        run_id = store.start_run("scene-scan", trigger="scheduled",
                                 at="2026-03-01T01:00:00+00:00")
        store.finish_run(run_id, outcome="completed",
                         counts={"proposed": 3},
                         at="2026-03-01T01:04:00+00:00")
        store.close()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        row = [r for r in captured.kwargs["actions"]._store.recent_runs()
              if r["id"] == run_id][0]
        self.assertEqual(row["outcome"], "completed")
        self.assertEqual(row["counts"], {"proposed": 3})


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


class GoneSectionWiring(_Base):
    """A subject the media server no longer holds reaches its own section and
    leaves every other list.

    The two halves have to be pinned together. Leaving the working lists is
    what stops a control being drawn for a file that is not there; reaching a
    section is what makes "marked, not removed" true for the person reading
    the page, rather than only true inside the database.
    """

    def _seed_and_mark(self, subject_id="1"):
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
        store.mark_gone("scene", subject_id)
        store.close()
        return fp

    def _served(self):
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        return captured.kwargs

    def test_a_marked_row_reaches_the_gone_section(self):
        fp = self._seed_and_mark()
        self.assertEqual([r.fingerprint for r in self._served()["gone"]()],
                         [fp])

    def test_and_appears_in_no_other_scene_section(self):
        # HARM: a row drawn in the inbox, the dismissed list or the applied
        # list offers a control that writes to an id the server does not
        # have. Every section is checked, not the inbox alone.
        self._seed_and_mark(subject_id="1")
        self._seed(subject_id="2")  # an ordinary, still-open proposal
        kwargs = self._served()
        self.assertEqual([r.state for r in kwargs["rows"]()], ["new"])
        for section in ("dismissed", "superseded", "applied"):
            self.assertEqual(kwargs[section](), [], section)

    def test_an_ordinary_proposal_does_not_reach_the_gone_section(self):
        # The other direction: the section must not be a second copy of the
        # inbox.
        self._seed(subject_id="2")
        self.assertEqual(self._served()["gone"](), [])


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
        self.assertEqual(muted[0]["subject_url"],
                         "http://media.example/scenes/9")


class PerInboxRouteWiring(_Base):
    """`/{inbox}` and `/{inbox}/{state}` narrow by subject type through
    `Store.items(subject_types=)` directly (see `web.app._serve_inbox_route`)
    rather than through a pre-built callable the way every other section is
    wired -- so what reaches `serve()` is the store itself and the same
    `base_url` every other row's link is built from, not a section-shaped
    function.
    """

    def test_the_store_reaches_serve(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        # The SAME store `actions` was wired with, not a second handle on the
        # same file: two connections would each hold their own view of
        # in-flight writes, and a per-inbox page reading one while an approve
        # commits through the other is exactly the kind of split this project
        # avoids elsewhere by threading one object through.
        self.assertIs(captured.kwargs["store"], captured.kwargs["actions"]._store)

    def test_a_configured_server_reaches_the_per_inbox_base_url(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path, "--server", "http://media.example"])
        self.assertEqual(captured.kwargs["base_url"], "http://media.example")

    def test_no_configured_server_leaves_the_per_inbox_base_url_none(self):
        self._seed()
        captured = _CapturedServe()
        with patch("cronicled.__main__.serve", captured):
            main(["--db", self.db_path])
        self.assertIsNone(captured.kwargs["base_url"])


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

    def test_config_dir_also_reaches_the_marker_tag(self):
        # The same seam as the adapters above, for the value that decides
        # whether a scan can see provisionally-organized files at all. A
        # loader called on the ambient environment instead of the same `env`
        # finds no scan.json, answers "no marker configured" -- a legitimate
        # state, so nothing raises -- and every scan this process starts goes
        # on pooling only the unorganized set while the operator's config
        # sits in the directory they named.
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf)
        with open(os.path.join(conf, "scan.json"), "w") as fh:
            json.dump({"marker_tag": "inferred-metadata"}, fh)
        captured = _CapturedServe()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path, "--config-dir", conf])

        self.assertEqual(captured.kwargs["actions"]._marker,
                         "inferred-metadata")

    def test_no_marker_configured_leaves_the_actions_without_one(self):
        # The other half of the rule the loader states: an absent scan.json
        # is a legitimate install, not a start-up failure, and it must reach
        # the page as "no marker" rather than as something invented here.
        conf = os.path.join(self._dir, "empty-conf")
        os.makedirs(conf)
        captured = _CapturedServe()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            with patch("cronicled.__main__.serve", captured):
                main(["--db", self.db_path, "--config-dir", conf])

        self.assertIsNone(captured.kwargs["actions"]._marker)

    def test_the_marker_read_here_also_reaches_the_scheduled_scan(self):
        # `main` hands the marker to two places -- the page's control and the
        # unattended schedule -- and one of them silently not getting it is
        # the shape this project has already shipped once. The `Actions` half
        # is asserted above; this is the other half, at the call `main` makes.
        conf = os.path.join(self._dir, "conf-scheduled")
        os.makedirs(conf)
        with open(os.path.join(conf, "scan.json"), "w") as fh:
            json.dump({"marker_tag": "inferred-metadata"}, fh)
        captured = _CapturedServe()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            with patch("cronicled.__main__.serve", captured):
                with patch("cronicled.__main__.build_scheduler") as scheduler:
                    scheduler.return_value = None
                    main(["--db", self.db_path, "--config-dir", conf])

        self.assertEqual(scheduler.call_args.kwargs["marker"],
                         "inferred-metadata")

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


class AdapterConfigThatCannotLoad(_Base):
    """An adapters.json that is present and unreadable is a start-up
    failure; an ABSENT one is a fresh install and starts silently.

    `load_adapters` draws exactly that line -- absent returns empty, broken
    raises -- and `main()` used to undo it, catching ValueError, RuntimeError
    and KeyError alike and substituting an empty mapping. A syntax error, a
    retired key and a missing required field then all came out as "no site
    adapter is configured", which tells the operator to create a file they
    already have. Both halves are pinned here: the broken configs below each
    surface their OWN message, and the two loadable ones (absent, and present
    with no adapters in it) still start with nothing said.

    Every fixture is invented. No test here supplies `--server`, so nothing
    builds a media-server client and no scheduler loop is started.
    """

    def _conf(self):
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        return conf

    def _adapters_path(self, conf):
        return os.path.join(conf, "adapters.json")

    def _write(self, text):
        conf = self._conf()
        with open(self._adapters_path(conf), "w") as fh:
            fh.write(text)
        return conf

    def _start(self, conf):
        """`main()` over `conf`, returning what it handed `serve` and what it
        printed."""
        captured = _CapturedServe()
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            with patch("cronicled.__main__.serve", captured):
                with redirect_stdout(out):
                    main(["--db", self.db_path, "--config-dir", conf])
        return captured, out.getvalue()

    def _refuse(self, conf):
        """`main()` over a `conf` that must not start, returning the message
        the operator gets and whatever was printed before it stopped."""
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            with patch("cronicled.__main__.serve", _CapturedServe()):
                with redirect_stdout(out):
                    with self.assertRaises(RuntimeError) as ctx:
                        main(["--db", self.db_path, "--config-dir", conf])
        return str(ctx.exception), out.getvalue()

    def test_a_syntax_error_reaches_the_operator_with_its_own_line_number(self):
        # A trailing comma left by a hand edit -- one of the three real
        # causes. The parse error names line 3, and that number is the whole
        # value of surfacing it: it is what an operator opens the file to.
        conf = self._write('{\n  "adapters": [],\n}\n')
        message, _ = self._refuse(conf)
        self.assertIn(self._adapters_path(conf), message)
        self.assertIn("line 3", message)

    def test_the_parse_error_itself_is_kept_as_the_cause(self):
        # Chained rather than replaced, so the traceback still reaches the
        # decoder that actually objected.
        conf = self._write('{\n  "adapters": [],\n}\n')
        out = io.StringIO()
        with patch("cronicled.__main__.serve", _CapturedServe()):
            with redirect_stdout(out):
                with self.assertRaises(RuntimeError) as ctx:
                    main(["--db", self.db_path, "--config-dir", conf])
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertIn("line 3", str(ctx.exception.__cause__))

    def test_the_retired_default_key_reaches_the_operator_with_its_own_message(self):
        # The second real cause. `load_adapters` already says what to do
        # about it -- delete the line -- and that instruction is what was
        # being thrown away.
        conf = self._write(json.dumps({"default": "invented",
                                       "adapters": [_ADAPTER_SPEC]}))
        message, _ = self._refuse(conf)
        self.assertIn(self._adapters_path(conf), message)
        self.assertIn('"default"', message)
        self.assertIn("Remove", message)

    def test_a_missing_required_field_reaches_the_operator_by_name(self):
        # The third. `DeclarativeAdapter` names the field AND the adapter it
        # is missing from; a config with several adapters is unactionable
        # without the second half.
        spec = dict(_ADAPTER_SPEC)
        del spec["title_match_counts_as_ownership"]
        conf = self._write(json.dumps({"adapters": [spec]}))
        message, _ = self._refuse(conf)
        self.assertIn(self._adapters_path(conf), message)
        self.assertIn("title_match_counts_as_ownership", message)
        self.assertIn("invented", message)

    def test_an_adapter_with_no_name_is_a_startup_failure_too(self):
        # This one raises KeyError, not ValueError, and `KeyError('name')`
        # says nothing on its own -- which is exactly why the file has to be
        # named around it. A catch narrowed to ValueError drops it back to a
        # bare KeyError with no file in it.
        spec = dict(_ADAPTER_SPEC)
        del spec["name"]
        conf = self._write(json.dumps({"adapters": [spec]}))
        message, _ = self._refuse(conf)
        self.assertIn(self._adapters_path(conf), message)
        self.assertIn("'name'", message)

    def test_a_config_that_did_not_load_reports_no_config_directory(self):
        # The line that made the log look healthy through all three
        # failures. It reports what the load produced, so it cannot be
        # printed before the load, and a config that never loaded has
        # nothing for it to report.
        conf = self._write('{\n  "adapters": [],\n}\n')
        _, printed = self._refuse(conf)
        self.assertNotIn("config directory", printed)
        self.assertEqual(printed, "")

    def test_a_config_that_did_not_load_leaves_no_database_behind(self):
        # Nothing else starts first: the failure lands before a `Store` is
        # opened, so a mistyped config does not leave a database file at
        # whatever path the flag happened to name.
        conf = self._write('{\n  "adapters": [],\n}\n')
        self._refuse(conf)
        self.assertFalse(os.path.exists(self.db_path))

    def test_an_absent_config_still_starts_with_no_adapters(self):
        # The permissive half, and the case the old catch was protecting.
        # Genuinely ABSENT: the config directory exists and holds no
        # adapters.json at all. Deliberately not an empty one -- see the
        # test below, which is a different file on a different code path,
        # and a fixture conflating the two could not tell "a missing file is
        # fine" from "an empty list is fine".
        conf = self._conf()
        self.assertFalse(os.path.exists(self._adapters_path(conf)))
        captured, printed = self._start(conf)
        self.assertEqual(captured.kwargs["actions"]._adapters, {})
        self.assertNotIn("could not be loaded", printed)

    def test_a_config_present_but_holding_no_adapters_starts_too(self):
        # The neighbouring loadable case: a file that exists, parses, and
        # configures nothing. Also empty, also silent -- an operator who has
        # emptied the list has not broken anything.
        conf = self._write(json.dumps({"adapters": []}))
        self.assertTrue(os.path.exists(self._adapters_path(conf)))
        captured, printed = self._start(conf)
        self.assertEqual(captured.kwargs["actions"]._adapters, {})
        self.assertNotIn("could not be loaded", printed)

    def test_the_startup_line_says_no_adapters_loaded_when_none_did(self):
        # Whole first two lines, not a substring of one: the start-up report
        # is two facts in a fixed order, and an assertion sampling one of
        # them cannot see the other going missing.
        conf = self._conf()
        _, printed = self._start(conf)
        self.assertEqual(printed.splitlines()[:2],
                         ["config directory: %s (adapters: none)" % conf,
                          "database: %s" % self.db_path])

    def test_the_startup_line_names_the_adapters_that_did_load(self):
        # The other side of the same line: it reflects what loaded, so a
        # healthy-looking directory and a working config are no longer the
        # same sentence.
        conf = self._write(json.dumps({"adapters": [_ADAPTER_SPEC]}))
        captured, printed = self._start(conf)
        self.assertEqual(printed.splitlines()[:2],
                         ["config directory: %s (adapters: invented)" % conf,
                          "database: %s" % self.db_path])
        self.assertEqual(sorted(captured.kwargs["actions"]._adapters),
                         ["invented"])


class ServerJsonFallback(_Base):
    """`server.json` is the fifth file in the config directory, and the
    service never read it.

    The two command-line entry points do (`runscan`, `runstashbox`); the one
    the container runs took its address from `--server`/`$CRONICLED_SERVER`
    and nothing else. So an operator with a complete config directory started
    the service, watched it report four things it had found in that directory,
    and was told no media server was configured -- with the file that
    configures one sitting in the same directory, unread and unmentioned.

    Every test here passes `--config-dir`, so the value has to travel the whole
    distance through `main`'s injected `env` to reach the file: a loader called
    on the ambient environment instead would look for `config/server.json`
    relative to the working directory, find nothing, and report a legitimate
    absence.

    Every fixture is invented. `.example` is a reserved name that resolves to
    nothing, and the key below is not a key.
    """

    # Distinctive on purpose. The whole-output assertion below searches for
    # THIS string, and a key that looked like an ordinary word could hide
    # inside a start-up line while the test read as though it had looked.
    FILE_API_KEY = "fake-api-key-must-never-be-printed"
    FILE_URL = "http://media.example"

    def _conf(self, payload=None, text=None):
        """A config directory holding `server.json`, or -- given neither
        argument -- holding nothing at all."""
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        if payload is not None or text is not None:
            with open(self._server_path(conf), "w") as fh:
                fh.write(json.dumps(payload) if text is None else text)
        return conf

    def _server_path(self, conf):
        return os.path.join(conf, "server.json")

    def _environ(self, overrides):
        """The ambient environment every test here starts from: none of the
        four variables that could supply an address, and no zone, so what a
        test does configure is the only thing configuring anything."""
        for name in ("CRONICLED_SERVER", "CRONICLED_API_KEY", "STASH_URL",
                     "STASH_API_KEY", CONFIG_DIR_ENV_VAR, ZONE_ENV_VAR):
            if name not in overrides:
                os.environ.pop(name, None)

    def _start(self, conf, argv=(), environ_overrides=None):
        """`main()` over `conf`, returning what it handed `serve` and every
        line it printed.

        `build_scheduler` is replaced by one that schedules nothing. The real
        one would start a background loop that reaches for whatever address
        the fixture named, and no test here may open a socket. The double is
        not more capable than what it stands in for: `None` -- schedule
        nothing -- is an answer the real `build_scheduler` returns itself, and
        it is the answer `main` already handles.
        """
        overrides = dict(environ_overrides or {})
        captured = _CapturedServe()
        out = io.StringIO()
        with patch.dict(os.environ, overrides):
            self._environ(overrides)
            with patch("cronicled.__main__.serve", captured):
                with patch("cronicled.__main__.build_scheduler",
                           return_value=None):
                    with redirect_stdout(out):
                        main(["--db", self.db_path, "--config-dir", conf,
                              *argv])
        return captured, out.getvalue()

    def _refuse(self, conf):
        """`main()` over a `conf` that must not start, returning the error the
        operator gets and the `serve` that was never reached."""
        captured = _CapturedServe()
        with patch.dict(os.environ, {}):
            self._environ({})
            with patch("cronicled.__main__.serve", captured):
                with patch("cronicled.__main__.build_scheduler",
                           return_value=None):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(ValueError) as ctx:
                            main(["--db", self.db_path, "--config-dir", conf])
        return str(ctx.exception), captured

    def test_the_file_alone_configures_the_media_server(self):
        # The defect itself: no flag, no environment variable, a complete
        # server.json, and the service has to reach the media server.
        conf = self._conf({"url": self.FILE_URL,
                           "api_key": self.FILE_API_KEY})
        captured, printed = self._start(conf)
        stash = captured.kwargs["actions"]._stash
        self.assertIsInstance(stash, Stash)
        self.assertEqual(stash.url, "http://media.example/graphql")
        self.assertEqual(stash.api_key, self.FILE_API_KEY)
        # And it must not ALSO say none is configured, which is the sentence
        # the operator was reading while the file sat there.
        self.assertNotIn("WARNING", printed)

    def test_the_address_from_the_file_reaches_every_rows_scene_url(self):
        # The second thing the address is for. Left as `args.server`, this is
        # `None` on exactly the installs this change configures, and every
        # link on the page silently disappears while the client works.
        self._seed(subject_id="55")
        conf = self._conf({"url": self.FILE_URL,
                           "api_key": self.FILE_API_KEY})
        captured, _ = self._start(conf)
        rows = captured.kwargs["rows"]()
        self.assertEqual(rows[0].scene_url, "http://media.example/scenes/55")

    def test_an_explicit_flag_wins_over_the_file(self):
        # The precedence that keeps every existing deployment where it is. A
        # change making the file win would redirect anyone who mounts a
        # config directory AND passes the flag -- silently, to a server they
        # did not name in the invocation they are looking at.
        conf = self._conf({"url": "http://from-file.example",
                           "api_key": self.FILE_API_KEY})
        captured, printed = self._start(
            conf, argv=["--server", "http://from-flag.example",
                        "--api-key", "fake-key-from-the-flag"])
        stash = captured.kwargs["actions"]._stash
        self.assertEqual(stash.url, "http://from-flag.example/graphql")
        self.assertEqual(stash.api_key, "fake-key-from-the-flag")
        self.assertNotIn("from-file.example", printed)

    def test_an_environment_variable_wins_over_the_file(self):
        # The other direction of the same rule, and the one the container
        # documents: `-e CRONICLED_SERVER` beside a mounted /config. Pinned
        # separately from the flag because they are two different sources and
        # a fix could easily leave one of them beneath the file.
        conf = self._conf({"url": "http://from-file.example",
                           "api_key": self.FILE_API_KEY})
        captured, printed = self._start(
            conf, environ_overrides={
                "CRONICLED_SERVER": "http://from-environment.example",
                "CRONICLED_API_KEY": "fake-key-from-the-environment"})
        stash = captured.kwargs["actions"]._stash
        self.assertEqual(stash.url, "http://from-environment.example/graphql")
        self.assertEqual(stash.api_key, "fake-key-from-the-environment")
        self.assertNotIn("from-file.example", printed)

    def test_an_address_from_a_flag_does_not_take_the_files_key(self):
        # The fallback is gated on the ADDRESS, and the file is one setting
        # rather than two halves to be mixed with whatever else is around.
        # Someone passing `--server` alone today gets a client with no key,
        # and this change must not quietly hand them one out of a file they
        # did not ask to be read.
        conf = self._conf({"url": "http://from-file.example",
                           "api_key": self.FILE_API_KEY})
        captured, _ = self._start(
            conf, argv=["--server", "http://from-flag.example"])
        stash = captured.kwargs["actions"]._stash
        self.assertEqual(stash.url, "http://from-flag.example/graphql")
        self.assertIsNone(stash.api_key)

    def test_a_file_naming_no_api_key_stops_the_start(self):
        # A present, malformed file is not an absent one. Reported as "none
        # configured" -- which is what catching this would do -- it sends the
        # operator to check a file they already have, and this project has
        # already fixed exactly that defect one file over, in the adapter
        # loader above.
        conf = self._conf({"url": self.FILE_URL})
        message, captured = self._refuse(conf)
        self.assertIn("api_key", message)
        self.assertIn(self._server_path(conf), message)
        # Nothing started: `serve` was never reached, so this is a start-up
        # failure rather than a service running in a state nobody chose.
        self.assertIsNone(captured.kwargs)

    def test_a_file_that_is_not_valid_json_stops_the_start_too(self):
        # The second real cause, and a different code path: this one raises
        # out of the decoder rather than out of the loader's own check, and
        # the line number is what an operator opens the file to. A gate that
        # only asked whether the file existed would start anyway.
        conf = self._conf(text='{\n  "url": "http://media.example",\n}\n')
        message, captured = self._refuse(conf)
        self.assertIn("line 3", message)
        self.assertIsNone(captured.kwargs)

    def test_an_absent_file_still_starts_and_warns(self):
        # The permissive half. A fresh install with nothing configured is a
        # legitimate state: it starts, it browses, and Approve and Undo refuse
        # with a message. Reading the file unconditionally would turn this
        # into a start-up failure, since the loader raises when nothing
        # supplies both halves.
        conf = self._conf()
        self.assertFalse(os.path.exists(self._server_path(conf)))
        captured, printed = self._start(conf)
        self.assertIsNone(captured.kwargs["actions"]._stash)
        self.assertIn("WARNING", printed)

    def test_the_refusal_names_the_file_alongside_the_flag_and_the_variables(self):
        # Three sources now, and the message has to admit to all three. It
        # listed the flag and the variables and not the file, which is how an
        # operator reads "no media server is configured" as "my file is
        # wrong", checks it, finds it correct, and has nowhere to go.
        conf = self._conf()
        _, printed = self._start(conf)
        for source in ("--server", "--api-key", "$CRONICLED_SERVER",
                       "$CRONICLED_API_KEY", self._server_path(conf)):
            self.assertIn(source, printed, source)

    def test_the_key_from_the_file_appears_nowhere_in_the_start_up_output(self):
        # THE WHOLE emitted text, not one line of it. A test asserting that
        # the media-server line is free of the key cannot see it turn up in
        # another line, and a secret in a start-up log is worse than the
        # defect this change fixes: it is invisible until somebody pastes the
        # log somewhere.
        #
        # The expected text is written out here rather than imported from the
        # module, so that a line added to the start-up report has to be added
        # here too, by someone who has read what it prints.
        conf = self._conf({"url": self.FILE_URL,
                           "api_key": self.FILE_API_KEY})
        _, printed = self._start(conf)
        self.assertEqual(printed, "".join(line + "\n" for line in [
            "config directory: %s (adapters: none)" % conf,
            "database: %s" % self.db_path,
            "zone: UTC (the unattended passes run overnight in it, and every "
            "time on the page is shown in it)",
            "media server: configured from %s" % self._server_path(conf),
        ]))
        # Stated separately as well, so this fails by NAME rather than as a
        # long diff on the day it matters.
        self.assertNotIn(self.FILE_API_KEY, printed)


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
        args = {"stash": self.stash, "adapters": self.adapters,
                "marker": None, "zone": ZONE}
        args.update(over)
        return build_scheduler(self.runner, self.store, args["stash"],
                               args["adapters"], env=self.env,
                               marker=args["marker"], zone=args["zone"])

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

        # ALL the start-up producers, and the whole set of them: the scan, the
        # description pass and the tag-merge pass are separate registrations
        # with separate cadences, and asserting only that the scan is there
        # would pass with either of the others silently unscheduled.
        self.assertEqual(result.due, ALL_PRODUCERS)
        self.assertEqual(sorted(result.started), ALL_PRODUCERS)
        self.assertEqual(result.skipped, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertTrue(self.runner.wait(
            result.started[DESCRIPTION_PRODUCER_NAME], WAIT))
        self.assertTrue(self.runner.wait(
            result.started[TagMergeProducer.name], WAIT))
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

    def test_the_configured_marker_tag_reaches_the_scheduled_scan(self):
        # A SEPARATE call site from the page's Scan button, and so a separate
        # test, for the same reason the aliases below have one: the run
        # nobody watches is the one where a value quietly not arriving costs
        # the most. Without the marker here the nightly pass keeps skipping
        # every provisionally-organized file in the library, and the only
        # symptom is an inbox that stays smaller than it should.
        self._build(marker="inferred-metadata")

        nightly = {p.name: p for p in self.runner.producers()}[
            SCHEDULED_SCAN_NAME]
        self.assertEqual(nightly._marker, "inferred-metadata")

    def test_the_configured_aliases_reach_the_scheduled_scan(self):
        # A SEPARATE call site from the page's Scan button, and so a separate
        # test: `build_scheduler` builds the unattended producer here, and a
        # fix that reached only the manual path would repeat the whole defect
        # on the run nobody is watching -- every night, silently.
        #
        # Asserted as the whole resolved map, pooled from both configured
        # adapters: the index `ScanProducer` hands to `resolve` for every file
        # it examines (see `tests/test_scan.py`'s own
        # `test_the_aliases_reach_the_resolver`, which pins that half). One
        # adapter's entries silently missing here is an alias an operator
        # wrote that the nightly scan will never apply.
        adapters = {
            "invented": DeclarativeAdapter(
                dict(_ADAPTER_SPEC, aliases={"vcrane": "Velvet Crane"})),
            "second": DeclarativeAdapter(
                dict(_ADAPTER_SPEC, name="second",
                     aliases={"i m k": "Ivy May Kingsley"}))}

        self._build(adapters=adapters)

        nightly = {p.name: p for p in self.runner.producers()}[
            SCHEDULED_SCAN_NAME]
        self.assertEqual(nightly._aliases,
                         Aliases({"vcrane": "Velvet Crane",
                                  "i m k": "Ivy May Kingsley"}))

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
        self.assertEqual(sorted(before), ALL_PRODUCERS)
        actions = Actions(self.store, self.stash, runner=self.runner,
                          adapters=self.adapters)

        job = actions.scan(25)
        self.assertTrue(self.runner.wait(job.id, WAIT))

        after = {p.name: p for p in self.runner.producers()}
        self.assertEqual(sorted(after),
                         sorted([ScanProducer.name] + ALL_PRODUCERS))
        nightly = after[SCHEDULED_SCAN_NAME]
        self.assertIs(nightly, before[SCHEDULED_SCAN_NAME])
        self.assertIsNone(nightly._limit)
        self.assertEqual(nightly.at, time(3, 0))
        self.assertIs(nightly.zone, ZONE)
        self.assertEqual(after[ScanProducer.name]._limit, 25)
        # And the schedule still starts the unbounded one afterwards.
        result = scheduler.tick(NOW)
        self.assertEqual(sorted(result.started), ALL_PRODUCERS)
        self.assertTrue(self.runner.wait(
            result.started[DESCRIPTION_PRODUCER_NAME], WAIT))
        self.assertTrue(self.runner.wait(
            result.started[TagMergeProducer.name], WAIT))

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

        self.assertEqual(result.due, ALL_PRODUCERS)
        # The scan is held off by the busy cost class; the description pass
        # and the tag-merge pass are NOT, and that is the point of both being
        # `local` rather than `scraping` -- neither drives a scraper, so
        # queueing either behind a twenty-minute scrape would be a limit
        # protecting nothing. Asserted as the whole started set, so a producer
        # mis-filed into `scraping` shows up here as a missing name rather
        # than passing an `assertIn` on the one that was filed correctly.
        self.assertEqual(sorted(result.started),
                         sorted([DESCRIPTION_PRODUCER_NAME,
                                 TagMergeProducer.name]))
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(list(result.skipped), [SCHEDULED_SCAN_NAME])
        self.assertIn("cost class", result.skipped[SCHEDULED_SCAN_NAME])
        # Both local jobs are waited out BEFORE `running` is read below: a
        # job still finishing would appear in that accounting and make this
        # test about timing rather than about the scraping limit.
        self.assertTrue(self.runner.wait(
            result.started[DESCRIPTION_PRODUCER_NAME], WAIT))
        self.assertTrue(self.runner.wait(
            result.started[TagMergeProducer.name], WAIT))
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

    def test_an_interval_override_still_wins_over_the_declared_appointment(self):
        # The interval form is still supported and an operator who configured
        # one keeps it: this ticket changes what the producers DECLARE, not
        # anybody's configuration. `resolve`'s rule is that an override naming
        # any timing key supplies the whole of that producer's timing, so an
        # `every` has to set the declared `at` aside -- not merge with it,
        # which `resolve` would refuse as a contradiction and take start-up
        # down with.
        #
        # 90 minutes ago is chosen so that the two answers genuinely differ,
        # and not at any boundary: 5400 seconds is well past the overridden
        # hour, and 01:30 UTC is well past 03:00 in the configured zone (01:00
        # UTC in July). See the discriminating half below.
        self.store.record_run(SCHEDULED_SCAN_NAME, _ago(minutes=90))
        # The tag-merge pass is disabled throughout this pair so both tests
        # stay about the SCAN's timing: it has never run, so it would be due
        # on every tick here and would drown the one answer being read.
        self._schedule_file({SCHEDULED_SCAN_NAME: {"every": 3600},
                             TagMergeProducer.name: {"enabled": False}})
        # The description pass is held off the same way, and for the same
        # reason -- by a recorded run rather than a disable, because either
        # keeps it out of `due` and the two mechanisms between them show the
        # override reaching `resolve` at all.
        self.store.record_run(DESCRIPTION_PRODUCER_NAME, _ago(minutes=90))
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [SCHEDULED_SCAN_NAME])

    def test_the_same_fixture_is_not_due_without_that_override(self):
        # The discriminating half, and it discriminates in the direction that
        # matters now: the declared 03:00 must not override the operator's
        # configuration, and the only way to see that is a fixture the
        # DECLARATION leaves alone and the OVERRIDE makes due. The same run,
        # 90 minutes ago, is after today's 03:00 in the configured zone, so
        # nothing is owed until tomorrow's.
        self.store.record_run(SCHEDULED_SCAN_NAME, _ago(minutes=90))
        self.store.record_run(DESCRIPTION_PRODUCER_NAME, _ago(minutes=90))
        self._schedule_file({TagMergeProducer.name: {"enabled": False}})
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [])
        self.assertEqual(result.started, {})
        self.assertEqual(sorted(result.skipped), ALL_PRODUCERS)
        self.assertIn("next due at", result.skipped[SCHEDULED_SCAN_NAME])
        self.assertIn("disabled", result.skipped[TagMergeProducer.name])
        # Tomorrow's 03:00 in the configured zone, named as the instant it is
        # rather than as "some time later": 03:00 Madrid on 28 July 2026 is
        # 01:00 UTC, because July is +02:00 there. A reason naming today's
        # appointment, or one naming an hour drifted from a restart, both fail
        # here.
        self.assertIn("next due at 2026-07-28T01:00:00+00:00",
                      result.skipped[SCHEDULED_SCAN_NAME])

    def test_the_scheduled_scan_can_be_disabled_through_the_same_file(self):
        self._schedule_file({SCHEDULED_SCAN_NAME: {"enabled": False},
                             DESCRIPTION_PRODUCER_NAME: {"enabled": False},
                             TagMergeProducer.name: {"enabled": False}})
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertEqual(result.due, [])
        self.assertEqual(result.started, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(sorted(result.skipped), ALL_PRODUCERS)
        self.assertIn("disabled", result.skipped[SCHEDULED_SCAN_NAME])
        self.assertIn("disabled", result.skipped[DESCRIPTION_PRODUCER_NAME])
        self.assertIn("disabled", result.skipped[TagMergeProducer.name])
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

    def test_no_configured_adapter_schedules_the_other_two_passes(self):
        # The three producers need different things, so an install missing one
        # of those things must not be an all-or-nothing decision: a scan needs
        # a store to search against, while a description pass reads a field
        # the server already holds and a tag merge reads the server's own
        # vocabulary. Neither of the latter two wants an adapter at all.
        # Folding the three requirements together would leave two producers
        # with a perfectly good reason to run silently unregistered -- so the
        # scan is the thing not scheduled, it says so, and the passes that CAN
        # run still do.
        #
        # The registry is asserted as a WHOLE list. `assertIn` on either name
        # would pass with the other one dropped, which is the exact failure
        # this covers.
        out = io.StringIO()
        with redirect_stdout(out):
            scheduler = self._build(adapters={})
        self.assertIsNotNone(scheduler)
        self.assertEqual(sorted(p.name for p in self.runner.producers()),
                         sorted([DESCRIPTION_PRODUCER_NAME,
                                 TagMergeProducer.name]))
        self.assertIn("no scan is scheduled", out.getvalue())
        self.assertIn("adapters.json", out.getvalue())

        result = scheduler.tick(NOW)

        self.assertEqual(sorted(result.due),
                         sorted([DESCRIPTION_PRODUCER_NAME,
                                 TagMergeProducer.name]))
        self.assertEqual(sorted(result.started),
                         sorted([DESCRIPTION_PRODUCER_NAME,
                                 TagMergeProducer.name]))
        self.assertTrue(self.runner.wait(
            result.started[DESCRIPTION_PRODUCER_NAME], WAIT))
        self.assertTrue(self.runner.wait(
            result.started[TagMergeProducer.name], WAIT))

    def test_the_tag_merge_pass_is_in_the_schedule_and_declares_a_cadence(self):
        # The same silent failure the nightly scan's own wiring test names:
        # a producer registered after `Scheduler.__init__` resolves is
        # scheduled by nothing, raises nothing, and simply never runs. So
        # this reads what the first tick actually decided, and then that the
        # job it started reached the media server and finished.
        scheduler = self._build()

        result = scheduler.tick(NOW)

        self.assertIn(TagMergeProducer.name, result.started)
        job_id = result.started[TagMergeProducer.name]
        self.assertTrue(self.runner.wait(job_id, WAIT))
        job = self.runner.job(job_id)
        self.assertEqual(job.state, "done", job.traceback)
        # `box`, not `local`: the pass reads each configured stash-box's whole
        # tag catalogue, which is the rate-limited resource that class exists
        # to ration.
        self.assertEqual(job.cost, "box")
        self.assertIn(("all_tags",), self.stash.calls)
        self.assertEqual(self.store.last_run(TagMergeProducer.name), result.at)

    def test_the_tag_merge_pass_declares_a_cadence_the_schedule_can_read(self):
        # `resolve` refuses an ENABLED producer that declares no cadence, and
        # it refuses it at start-up. So the cadence is asserted through the
        # entry it produced, not by reading `.every` off the object: a value
        # the schedule never consulted would satisfy the attribute and still
        # take the process down here.
        producers = {p.name: p for p in
                     [TagMergeProducer(self.stash, store=self.store,
                                       every=86400)]}
        entries = resolve(producers.values())
        self.assertEqual(entries[TagMergeProducer.name],
                         Entry(producer=TagMergeProducer.name, every=86400,
                               enabled=True))

    def test_a_tag_merge_pass_with_no_cadence_is_refused_at_startup(self):
        # The discriminating half of the pair above. Without it, the entry
        # test passes on a producer whose cadence came from anywhere at all,
        # including a default `resolve` might have invented.
        with self.assertRaisesRegex(ValueError, "cadence"):
            resolve([TagMergeProducer(self.stash, store=self.store)])


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
        self.assertEqual(result.due, ALL_PRODUCERS)
        self.assertEqual(sorted(result.started), ALL_PRODUCERS)
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


class TheThreeUnattendedAppointments(_Base):
    """The three passes run overnight, and never all at the same moment.

    Asserted through `resolve` over the producers `build_scheduler` actually
    registered -- the same call `Scheduler.__init__` makes -- rather than
    against the constants the code declares. A test comparing a constant to
    itself holds for any value, including one appointment for all three.
    """

    def setUp(self):
        super().setUp()
        self.conf = os.path.join(self._dir, "conf")
        os.makedirs(self.conf)
        self.env = {CONFIG_DIR_ENV_VAR: self.conf}
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        self.addCleanup(self._drain)
        self.runner = JobRunner(self.store)
        self.stash = _ReadOnlyStash("http://media.invalid")
        self.adapters = {"invented": DeclarativeAdapter(_ADAPTER_SPEC)}

    def _drain(self):
        for job in self.runner.jobs():
            self.runner.wait(job.id, WAIT)

    def _entries(self, zone=ZONE):
        build_scheduler(self.runner, self.store, self.stash, self.adapters,
                        env=self.env, marker=None, zone=zone)
        return resolve(self.runner.producers())

    def test_each_pass_declares_an_overnight_time_and_not_an_interval(self):
        # The whole resolved schedule, every field of every entry, against
        # values written out here. Three separate things would each pass a
        # narrower assertion and each be wrong: an appointment left as an
        # interval, an appointment in the wrong zone, and two appointments
        # sharing a minute.
        self.assertEqual(self._entries(), {
            SCHEDULED_SCAN_NAME: Entry(
                producer=SCHEDULED_SCAN_NAME, every=None, enabled=True,
                at=time(3, 0), zone=ZONE),
            DESCRIPTION_PRODUCER_NAME: Entry(
                producer=DESCRIPTION_PRODUCER_NAME, every=None, enabled=True,
                at=time(3, 20), zone=ZONE),
            TagMergeProducer.name: Entry(
                producer=TagMergeProducer.name, every=None, enabled=True,
                at=time(3, 40), zone=ZONE),
        })

    def test_no_two_of_them_share_one_appointment(self):
        # The rule in its own right, so that a mutation collapsing two onto
        # one time fails a test whose NAME is that rule. Computed from the
        # resolved entries rather than listed, so it holds for a fourth
        # producer added later.
        entries = self._entries()
        appointments = [entry.at for entry in entries.values()]
        self.assertEqual(len(appointments), 3)
        self.assertEqual(len(set(appointments)), len(appointments),
                         "two unattended passes are due at the same moment; "
                         "two of these three drive the media server's "
                         "headless browser and they are in different cost "
                         "classes, so nothing would hold them apart")

    def test_the_scan_is_the_first_of_the_three(self):
        # The order is a judgement call and this is where it is recorded: what
        # the scan proposes -- studios, performers, tags on scenes -- is the
        # material the other two pass over.
        entries = self._entries()
        self.assertEqual(
            sorted(entries, key=lambda name: entries[name].at),
            [SCHEDULED_SCAN_NAME, DESCRIPTION_PRODUCER_NAME,
             TagMergeProducer.name])

    def test_they_all_read_the_one_zone_they_were_given(self):
        # Not `== ZONE` but `is`: the setting is resolved once at start-up and
        # handed down, and a producer that rebuilt it from a name would be a
        # second chance to end up with a different zone.
        for entry in self._entries(zone=OTHER_ZONE).values():
            self.assertIs(entry.zone, OTHER_ZONE)

    def test_the_appointments_are_instants_the_zone_decides(self):
        # The appointments are wall-clock times, so the SAME declaration is a
        # different instant in a different zone -- which is the whole reason
        # the zone travels with them. 03:00 in Madrid in July is 01:00 UTC;
        # 03:00 in Kolkata is 21:30 UTC the day before. Both are stated as the
        # NEXT one due after a run recorded at 03:00 UTC on 27 July, which is
        # after Madrid's 03:00 that day and after Kolkata's -- so each answer
        # is the following day's, and the two differ by more than an offset.
        # Read through `due`'s own reason, which is the number an operator
        # sees.
        for zone, expected in ((ZONE, "2026-07-28T01:00:00+00:00"),
                               (OTHER_ZONE, "2026-07-27T21:30:00+00:00")):
            runner = JobRunner(self.store)
            build_scheduler(runner, self.store, self.stash, self.adapters,
                            env=self.env, marker=None, zone=zone)
            entries = resolve(runner.producers())
            _names, reasons = due(
                {SCHEDULED_SCAN_NAME: entries[SCHEDULED_SCAN_NAME]},
                {SCHEDULED_SCAN_NAME: NOW.isoformat()}, NOW)
            self.assertIn("next due at %s" % expected,
                          reasons[SCHEDULED_SCAN_NAME], str(zone))


class OneZoneForTheScheduleAndForThePage(_Base):
    """The zone the schedule keeps and the zone the page reads are ONE setting.

    Two would be worse than either being wrong on its own: a page saying 3am
    while a pass ran at a different 3am is evidence FOR the schedule an
    operator is trying to check. So every test here changes one environment
    variable and asserts that BOTH halves moved.

    `main()` is exercised whole, with only the media client replaced -- no
    socket is opened, and everything from the registration through the loop to
    the shutdown is the real thing.
    """

    def _run_main(self, zone_name=ZONE_NAME, db=None):
        """One whole `main()`, on a database of its own.

        A database of its own because `Store` refuses a second handle on a path
        already open and `main()` never closes the one it built -- so a test
        that runs the entry point twice, which every test here does, needs two
        files. `_seed_mute` writes into whichever one is about to be used.
        """
        db = self.db_path if db is None else db
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "adapters.json"), "w") as fh:
            json.dump({"adapters": [_ADAPTER_SPEC]}, fh)
        captured = _CapturedServe()
        out = io.StringIO()
        environ = {} if zone_name is None else {ZONE_ENV_VAR: zone_name}
        with patch.dict(os.environ, environ):
            if zone_name is None:
                os.environ.pop(ZONE_ENV_VAR, None)
            with patch("cronicled.__main__.Stash", _ReadOnlyStash):
                with patch("cronicled.__main__.serve", captured):
                    with redirect_stdout(out):
                        main(["--db", db, "--config-dir", conf,
                              "--server", "http://media.invalid"])
        return captured, out.getvalue()

    def _mute_row(self, captured):
        rows = captured.kwargs["muted"]()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_setting_moves_the_schedule_and_the_page_together(self):
        # THE test for one setting rather than two. The same environment
        # variable is read twice with two different values, and both halves --
        # the zone the appointments are DECLARED in and the zone the page
        # renders in -- move with it. Two settings show up here as one half
        # moving and the other not.
        #
        # The schedule half is read off the registered producers, NOT off the
        # status the page was handed: that status has already been converted
        # for display, so asserting an offset on it would be asserting the
        # page's zone twice and calling one of them the schedule's. A
        # mutation handing `build_scheduler` its own hardcoded UTC survived
        # exactly that mistake.
        for name, expected_offset in ((ZONE_NAME, "+02:00"),
                                      (OTHER_ZONE_NAME, "+05:30")):
            with self.subTest(zone=name):
                db = os.path.join(self._dir, "%s.sqlite3" % expected_offset)
                self._seed_mute(db)
                captured, _out = self._run_main(name, db=db)

                producers = captured.kwargs["actions"]._runner.producers()
                declared = {p.name: p.zone for p in producers}
                self.assertEqual(sorted(declared), ALL_PRODUCERS)
                self.assertEqual(set(declared.values()), {ZoneInfo(name)},
                                 "the schedule's own appointments are not in "
                                 "the configured zone")
                # And the page, from the same run and the same setting.
                self.assertTrue(
                    self._mute_row(captured)["at"].endswith(expected_offset),
                    self._mute_row(captured)["at"])
                status = captured.kwargs["schedule_status"]()
                self.assertTrue(status.last_tick_at.endswith(expected_offset),
                                status.last_tick_at)

    def test_an_install_naming_no_zone_keeps_utc_in_both_halves(self):
        # The default is a real answer, not an evasion: UTC everywhere, which
        # is what this project did before the setting existed. Asserted on
        # both halves, because a default reaching only one of them is the same
        # disagreement arriving quietly.
        self._seed_mute()
        captured, out = self._run_main(zone_name=None)
        self.assertIn("zone: UTC", out)
        # Both halves again: what the passes declare, and what the page shows.
        declared = {p.zone for p in
                    captured.kwargs["actions"]._runner.producers()}
        self.assertEqual(declared, {ZoneInfo("UTC")})
        self.assertTrue(
            captured.kwargs["schedule_status"]().last_tick_at.endswith(
                "+00:00"))
        self.assertTrue(self._mute_row(captured)["at"].endswith("+00:00"))

    def test_a_zone_this_system_does_not_know_stops_the_service_starting(self):
        # A configuration mistake belongs at start-up, where an operator reads
        # a stack trace, and not at 3am as a tick that raises and starts
        # nothing for anybody. `serve` is asserted never to have been reached,
        # so this cannot pass on a process that started and then complained.
        captured = _CapturedServe()
        with patch.dict(os.environ, {ZONE_ENV_VAR: "Nowhere/Atlantis"}):
            with patch("cronicled.__main__.Stash", _ReadOnlyStash):
                with patch("cronicled.__main__.serve", captured):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(ValueError,
                                                    "not a time zone"):
                            main(["--db", self.db_path])
        self.assertIsNone(captured.kwargs)

    def test_the_startup_line_names_the_zone_whether_or_not_it_was_set(self):
        # Always printed, so an operator wondering why the overnight passes
        # ran at 4am has the answer in the log. A line printed only when the
        # setting was written would be silent for exactly the install that got
        # it wrong -- which is why both directions are asserted here.
        _captured, configured = self._run_main(ZONE_NAME)
        self.assertIn("zone: %s" % ZONE_NAME, configured)
        _captured, unset = self._run_main(
            zone_name=None, db=os.path.join(self._dir, "unset.sqlite3"))
        self.assertIn("zone: UTC", unset)

    def _seed_mute(self, db=None):
        store = Store(self.db_path if db is None else db)
        try:
            store.mute("scene", "1", reason="never identifiable")
        finally:
            store.close()


class AScheduleOverrideMayNotNameADisagreeingZone(_Base):
    """The live incident this covers: a schedule override naming a zone for
    each of its entries, and no deployment-wide zone setting of its own.
    Before this, the three appointments were kept in the override's zone
    while the page -- reading the deployment's -- rendered every timestamp in
    a different one, with nothing said.

    Exercised through the whole of `main()`, the same way
    `OneZoneForTheScheduleAndForThePage` is: the bug is only visible end to
    end, because `resolve()` alone has no notion of what the page renders in
    and `build_scheduler` is what hands the two the same setting.
    """

    def _run_main(self, schedule_overrides, zone_name=None, db=None):
        db = self.db_path if db is None else db
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "adapters.json"), "w") as fh:
            json.dump({"adapters": [_ADAPTER_SPEC]}, fh)
        with open(os.path.join(conf, "schedule.json"), "w") as fh:
            json.dump(schedule_overrides, fh)
        captured = _CapturedServe()
        out = io.StringIO()
        environ = {} if zone_name is None else {ZONE_ENV_VAR: zone_name}
        with patch.dict(os.environ, environ):
            if zone_name is None:
                os.environ.pop(ZONE_ENV_VAR, None)
            with patch("cronicled.__main__.Stash", _ReadOnlyStash):
                with patch("cronicled.__main__.serve", captured):
                    with redirect_stdout(out):
                        main(["--db", db, "--config-dir", conf,
                              "--server", "http://media.invalid"])
        return captured, out.getvalue()

    def test_a_disagreeing_override_zone_stops_the_service_starting(self):
        # The exact shape observed live: every unattended producer named,
        # every one given a zone, none of them the deployment's -- which here
        # is left unset and so defaults to UTC. Startup must refuse rather
        # than start a service that keeps its appointments in one zone and
        # renders its page in another.
        overrides = {name: {"at": "03:00", "zone": OTHER_ZONE_NAME}
                    for name in ALL_PRODUCERS}
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "adapters.json"), "w") as fh:
            json.dump({"adapters": [_ADAPTER_SPEC]}, fh)
        with open(os.path.join(conf, "schedule.json"), "w") as fh:
            json.dump(overrides, fh)
        captured = _CapturedServe()
        with patch("cronicled.__main__.Stash", _ReadOnlyStash):
            with patch("cronicled.__main__.serve", captured):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "different zone"):
                        main(["--db", self.db_path, "--config-dir", conf,
                              "--server", "http://media.invalid"])
        # `serve` must never have been reached: this is a start-up failure,
        # not a service that started and rendered something wrong.
        self.assertIsNone(captured.kwargs)

    def test_a_disagreeing_override_zone_stops_startup_even_against_a_configured_deployment_zone(self):
        # The disagreement does not need an UNSET deployment zone to matter --
        # naming any deployment zone and an override that disagrees with it
        # must refuse the same way.
        overrides = {SCHEDULED_SCAN_NAME: {"at": "03:00",
                                           "zone": OTHER_ZONE_NAME}}
        with self.assertRaisesRegex(ValueError, "different zone"):
            self._run_main(overrides, zone_name=ZONE_NAME)

    def test_an_override_naming_the_deployments_own_zone_keeps_working(self):
        # Agreement -- the exact same zone, same spelling -- must not become
        # a start-up failure. `serve` having been reached at all is the
        # assertion: `build_scheduler`, `Scheduler.__init__` and `resolve` all
        # sit between this override being read and `serve` being called, and
        # any of them raising would leave `captured.kwargs` unset the same
        # way it stays unset in the disagreement tests above.
        overrides = {name: {"at": "03:00", "zone": ZONE_NAME}
                    for name in ALL_PRODUCERS}
        captured, out = self._run_main(overrides, zone_name=ZONE_NAME)
        self.assertIsNotNone(captured.kwargs)
        self.assertIn("zone: %s" % ZONE_NAME, out)

    def test_an_override_naming_the_same_zone_by_a_different_spelling_keeps_working(self):
        # THE point of the whole rule: two spellings of one zone are
        # agreement, not disagreement, and must not stop the service. Same
        # shape as the test above, spelling the override's zone the other
        # way `check_zone`/`resolve` already accept for the deployment's own
        # setting.
        overrides = {name: {"at": "03:00", "zone": "Etc/UTC"}
                    for name in ALL_PRODUCERS}
        captured, out = self._run_main(overrides, zone_name="UTC")
        self.assertIsNotNone(captured.kwargs)
        self.assertIn("zone: UTC", out)

    def _refusal_message(self, schedule_overrides, zone_name=None):
        try:
            self._run_main(schedule_overrides, zone_name=zone_name)
            self.fail("the disagreeing override was not refused")
        except ValueError as exc:
            return str(exc)

    def test_the_live_shape_names_no_repr_and_all_three_remedies(self):
        # The exact incident reported live: every unattended producer given
        # the same zone deliberately, and $CRONICLED_ZONE never set -- so the
        # deployment's zone is the unset default, not a choice, and the
        # message reaching the operator must say so in a form they can act
        # on rather than a `repr` of the `tzinfo` object.
        overrides = {name: {"at": "03:00", "zone": "America/New_York"}
                    for name in ALL_PRODUCERS}
        message = self._refusal_message(overrides, zone_name=None)
        self.assertNotIn("ZoneInfo(", message, message)
        self.assertNotIn("zoneinfo.", message, message)
        self.assertIn("configured for (UTC)", message, message)
        self.assertIn(
            "set $CRONICLED_ZONE to 'America/New_York' so the deployment "
            "reads the zone this override already does", message, message)
        self.assertIn(
            "drop 'zone' from this override so it reads the deployment's "
            "zone instead", message, message)
        self.assertIn(
            "change this override's 'zone' to name the deployment's (UTC)",
            message, message)
        self.assertIn("most likely wanted", message, message)

    def test_an_explicitly_chosen_deployment_zone_does_not_get_the_same_hint(self):
        # The mirror image, through the whole entry point rather than
        # `resolve()` directly: the deployment's zone here was set on
        # purpose ($CRONICLED_ZONE=UTC), so the message must not tell the
        # operator their own choice is the less likely truth.
        overrides = {name: {"at": "03:00", "zone": "America/New_York"}
                    for name in ALL_PRODUCERS}
        message = self._refusal_message(overrides, zone_name="UTC")
        self.assertNotIn("most likely wanted", message, message)
        self.assertNotIn("unset default", message, message)


class StoredTimestampsStayUtc(_Base):
    """The expensive direction, guarded on the raw database rather than
    through any reader.

    A rendering in the wrong hour is wrong on a screen and one edit fixes it.
    A LOCAL time written into the database is a different kind of loss: during
    the hour a clock repeats, two rows an hour apart carry the same text, so
    nothing afterwards can order them -- not the scheduler comparing a run
    against an appointment, not `ORDER BY created_at`, not a person reading the
    table. Nothing recovers it, because the information is gone rather than
    mislabelled.

    So this runs the entry point in a zone whose offset is NEVER zero, lets it
    tick and record, and then reads the timestamps back with sqlite3 directly.
    Going through `Store` would ask a reader whether a writer wrote what it
    should have.
    """

    # Every column in the schema that holds a time. Compared as a whole set
    # below, so a table added later with a timestamp this sweep does not know
    # about fails here rather than being quietly exempt from the rule.
    EXPECTED_COLUMNS = [
        ("dismissal", "at"),
        ("gone", "at"),
        ("item", "created_at"),
        ("item", "last_seen_at"),
        ("item", "resolved_at"),
        ("mute", "at"),
        ("producer_run", "at"),
        ("refusal", "at"),
        ("supersede", "at"),
    ]

    def _run_main(self):
        conf = os.path.join(self._dir, "conf")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "adapters.json"), "w") as fh:
            json.dump({"adapters": [_ADAPTER_SPEC]}, fh)
        captured = _CapturedServe()
        with patch.dict(os.environ, {ZONE_ENV_VAR: ZONE_NAME}):
            with patch("cronicled.__main__.Stash", _ReadOnlyStash):
                with patch("cronicled.__main__.serve", captured):
                    with redirect_stdout(io.StringIO()):
                        main(["--db", self.db_path, "--config-dir", conf,
                              "--server", "http://media.invalid"])
        return captured

    def _stamps(self):
        """`{(table, column): [every non-null value]}` for every timestamp
        column the schema has, read straight out of the file."""
        conn = sqlite3.connect(self.db_path)
        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]
            found = {}
            for table in sorted(tables):
                for row in conn.execute("PRAGMA table_info(%s)" % table):
                    column = row[1]
                    if column != "at" and not column.endswith("_at"):
                        continue
                    values = [value for (value,) in conn.execute(
                        "SELECT %s FROM %s" % (column, table))
                        if value is not None]
                    found[(table, column)] = values
            return found
        finally:
            conn.close()

    def test_the_sweep_below_actually_looks_at_every_timestamp_column(self):
        # A sweep that found nothing would pass every assertion in this class
        # while the database filled with local times. So the columns it visits
        # are pinned first, as a whole set.
        self._run_main()
        self.assertEqual(sorted(self._stamps()), self.EXPECTED_COLUMNS)

    def test_and_there_is_something_in_the_ones_this_run_writes(self):
        # The other half of the same guard: three columns are genuinely
        # written by a run that seeds a proposal, mutes a subject and ticks,
        # and a fixture that stopped producing rows would make the assertion
        # below vacuous. Counted, not merely non-empty: a producer_run row per
        # producer is three, and one is the shape a tick that abandoned the
        # rest would leave.
        self._seed()
        self._mute()
        self._run_main()
        stamps = self._stamps()
        self.assertEqual(len(stamps[("producer_run", "at")]), 3)
        self.assertEqual(len(stamps[("mute", "at")]), 1)
        self.assertEqual(len(stamps[("item", "created_at")]), 1)
        self.assertEqual(len(stamps[("item", "last_seen_at")]), 1)

    def test_every_stored_timestamp_is_utc_and_not_the_configured_zone(self):
        # Read from the file, not through `Store`. A local stamp anywhere here
        # is the unrecoverable direction, and the run above was deliberately
        # made in a zone two hours off UTC so that a conversion leaking into a
        # write has somewhere visible to show up.
        self._seed()
        self._mute()
        self._run_main()
        for (table, column), values in sorted(self._stamps().items()):
            for value in values:
                where = "%s.%s = %r" % (table, column, value)
                self.assertTrue(value.endswith("+00:00"), where)
                # And it is a real instant, not text that merely ends that
                # way: a stamp the scheduler cannot read is due-immediately
                # forever.
                self.assertIsNotNone(as_utc(value), where)
                self.assertEqual(as_utc(value).utcoffset(), timedelta(0),
                                 where)

    def test_the_page_shows_those_very_rows_in_the_configured_zone(self):
        # The pairing that makes the assertion above load-bearing rather than
        # trivially true. If nothing anywhere converted, every test in this
        # class would still pass -- so the SAME run is checked from the other
        # end: the stored instant is UTC, the page's is +02:00, and the two
        # are the same instant.
        self._seed()
        self._mute()
        captured = self._run_main()

        stored = self._stamps()[("mute", "at")][0]
        shown = captured.kwargs["muted"]()[0]["at"]
        self.assertTrue(stored.endswith("+00:00"), stored)
        self.assertTrue(shown.endswith("+02:00"), shown)
        self.assertNotEqual(stored, shown)
        self.assertEqual(as_utc(shown), as_utc(stored))

    def _mute(self):
        store = Store(self.db_path)
        try:
            store.mute("scene", "1", reason="never identifiable")
        finally:
            store.close()


class TheScheduledJobsAreNamedForWhatTheyCover(unittest.TestCase):
    """The three names are an interface, not an implementation detail.

    They are what an operator writes into a schedule file, and
    `schedule.resolve` refuses an override naming a producer that is not
    registered -- at START-UP, before there is a page on which to read the
    refusal. So renaming one is a breaking change to somebody else's file,
    and the names are pinned here as literals on purpose.

    `ALL_PRODUCERS` above is deliberately derived from the modules that
    define these names, so that a rename cannot leave the assertions in this
    file testing a schedule that no longer has them. That is right for what
    those assertions are for, and it is exactly why it cannot also pin the
    names: both sides would move together, and a rename would pass in
    silence. Literals here, once, and derived everywhere else.
    """

    def test_the_three_scheduled_jobs_have_the_names_a_schedule_file_uses(self):
        self.assertEqual(ALL_PRODUCERS,
                         ["performer-scan", "scene-scan", "tag-scan"])

    def test_the_example_schedule_names_jobs_that_are_actually_registered(self):
        """HARM: the example is what an operator copies. One naming a job
        that no longer exists does not degrade -- `resolve` refuses it and
        the process never comes up, so the first thing the rename does to
        somebody who followed the documentation is a crash loop.

        Compared as a whole set rather than key by key: a key left behind is
        the failure, and a check for the keys that should be there cannot
        see one that should not.
        """
        with open(os.path.join("config", "schedule.example.json"),
                  encoding="utf-8") as fh:
            example = json.load(fh)
        self.assertEqual(sorted(example), ALL_PRODUCERS)

    def test_the_migration_translates_old_names_into_jobs_that_exist(self):
        """HARM: `load_schedule` exists so an operator's existing file does
        not crash-loop the process on the release that renames a job. A map
        entry pointing at a name nothing registers translates one refusal
        into another and reports it as a successful migration, which is worse
        than not migrating at all -- the operator is told to stop worrying
        about the very name that is still stopping the process.

        The whole set, and equality rather than "each of these is
        registered": every scheduled job here was renamed, so the map's
        targets are exactly the registry. A job added later that never had an
        old name fails this on purpose -- adding one is the moment to decide
        whether it needs carrying, and this is the only place that question
        gets asked.

        Derived on both sides deliberately, unlike
        `test_the_three_scheduled_jobs_have_the_names_a_schedule_file_uses`
        above: that one pins the names as literals, and the literal OLD names
        are pinned in tests/test_config.py. What is left for this to check is
        that the two sets agree, which neither literal can see.
        """
        self.assertEqual(sorted(set(RENAMED_JOBS.values())), ALL_PRODUCERS)


class _ItemsSpy:
    """Wraps a real `Store`, recording every call to `items` -- the method
    `waiting_counts` used to be handed the (already fetched) result of, and
    must never call again now that it counts through `counts_by_subject_type`
    instead. A timing assertion is what a slow machine trips and a fast one
    hides; this is a call-shape assertion instead, which neither can dodge.
    """

    def __init__(self, store):
        self._store = store
        self.items_calls = 0

    def items(self, *args, **kwargs):
        self.items_calls += 1
        return self._store.items(*args, **kwargs)

    def counts_by_subject_type(self, *args, **kwargs):
        return self._store.counts_by_subject_type(*args, **kwargs)


class WhatIsWaitingIsCountedPerInbox(unittest.TestCase):
    """`waiting_counts` turns a store's proposals into the numbers beside the
    links on the summary page."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._dir, "waiting.sqlite3"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _add(self, subject_type, subject_id, state=None, folder="library"):
        """One proposal, real enough to go through the store's own state
        machine -- `state=None` leaves it at the store's own default (`new`,
        i.e. still waiting); any other name is reached the same way a
        reviewer's decision reaches it in production (`mark_applied`,
        `dismiss`, `mute`), not by writing the column directly."""
        fp = self.store.record(
            folder=folder, subject_type=subject_type, subject_id=subject_id,
            summary="a proposal", payload={"invented": subject_id},
            producer="test-producer")
        if state == "applied":
            self.store.mark_applied(fp)
        elif state == "dismissed":
            self.store.dismiss(fp)
        elif state == "muted":
            self.store.mute(subject_type, subject_id)
        return fp

    def test_every_subject_type_this_package_declares_has_a_heading(self):
        """Discovered by IMPORT, not by a list written here.

        A list would be a second copy of `WAITING_SECTIONS` that has to be
        edited alongside it, so the two would agree by construction and this
        would prove only that the code agrees with itself. Walking the package
        makes the two sides independent: a producer added tomorrow declares its
        `SUBJECT_TYPE` in its own module and turns up here with no edit.

        HARM: an unmapped type lands on the summary under a heading nobody
        designed, named after the raw type. That fallback is deliberate -- it
        keeps the page up (see `waiting_counts`) -- and this is the guard that
        stops it ever being reached in a shipped release.
        """
        declared = {}
        for module in pkgutil.iter_modules(cronicled.__path__):
            subject = getattr(
                importlib.import_module("cronicled." + module.name),
                "SUBJECT_TYPE", None)
            if isinstance(subject, str):
                declared[module.name] = subject
        # A discovery that found nothing would make the rest of this vacuous.
        self.assertGreaterEqual(len(declared), 6, declared)

        covered = {s for _n, subjects in WAITING_SECTIONS for s in subjects}
        self.assertEqual(sorted(set(declared.values())), sorted(covered),
                         "declared by %r" % (declared,))

    def test_no_two_headings_claim_the_same_subject_type(self):
        # The other direction, and it is not symmetric with the one above: a
        # type listed twice still passes a set comparison while its proposals
        # are counted under whichever heading the dict comprehension reached
        # last -- resolved by iteration order, silently.
        covered = [s for _n, subjects in WAITING_SECTIONS for s in subjects]
        self.assertEqual(sorted(covered), sorted(set(covered)))

    def test_six_subject_types_land_under_three_headings(self):
        # Every producer this project registers, one of each, so a subject
        # type routed to the wrong heading -- or to none -- is visible as a
        # number in the wrong place rather than as a total that still adds up.
        self._add(scan.SUBJECT_TYPE, "1")
        self._add(tags.SUBJECT_TYPE, "2")
        self._add(tag_descriptions.SUBJECT_TYPE, "3")
        self._add(performer_tags.SUBJECT_TYPE, "4")
        self._add(tag_hygiene.SUBJECT_TYPE, "5")
        self._add(descriptions.SUBJECT_TYPE, "6")
        counts = waiting_counts(self.store)
        self.assertEqual(counts, {"scenes": 1, "tags": 4, "performers": 1})

    def test_it_accumulates_rather_than_recording_that_there_was_one(self):
        # Asymmetric and greater than one: `+= 1` and `= 1` agree on a fixture
        # of one, and the two headings differ so "counted them all into the
        # first bucket" cannot pass either.
        for i in range(3):
            self._add(scan.SUBJECT_TYPE, "scene-%d" % i)
        for i in range(5):
            self._add(tags.SUBJECT_TYPE, "tag-%d" % i)
        counts = waiting_counts(self.store)
        self.assertEqual(counts, {"scenes": 3, "tags": 5, "performers": 0})

    def test_a_decision_already_made_is_not_still_waiting(self):
        # A number that never comes down is a number a reader stops believing.
        self._add(scan.SUBJECT_TYPE, "a", state="applied")
        self._add(scan.SUBJECT_TYPE, "b", state="applied")
        self._add(scan.SUBJECT_TYPE, "c")
        counts = waiting_counts(self.store)
        self.assertEqual(counts, {"scenes": 1, "tags": 0, "performers": 0})

    def test_a_reviewers_own_dismissal_is_not_still_waiting(self):
        # Same shape as the `applied` guard above, for the other two states
        # `Store.items()`'s default view has always hidden: a reviewer's own
        # rejection is not outstanding work either.
        self._add(scan.SUBJECT_TYPE, "a", state="dismissed")
        self._add(scan.SUBJECT_TYPE, "b")
        counts = waiting_counts(self.store)
        self.assertEqual(counts, {"scenes": 1, "tags": 0, "performers": 0})

    def test_a_muted_subject_is_not_still_waiting(self):
        self._add(scan.SUBJECT_TYPE, "a", state="muted")
        self._add(scan.SUBJECT_TYPE, "b")
        counts = waiting_counts(self.store)
        self.assertEqual(counts, {"scenes": 1, "tags": 0, "performers": 0})

    def test_every_heading_is_reported_even_at_zero(self):
        # An absent heading and one reading zero are different claims: the
        # first says nothing about that inbox, the second says it is clear.
        self.assertEqual(waiting_counts(self.store),
                         {"scenes": 0, "tags": 0, "performers": 0})

    def test_a_subject_no_heading_claims_gets_one_of_its_own(self):
        # HARM: a producer added without an entry here would otherwise be
        # counted nowhere, and the summary would report an empty install while
        # a full inbox sat behind the link -- the exact failure this page is
        # supposed to catch. Ugly and visible beats invisible.
        self._add("something-new", "a")
        self._add("something-new", "b")
        self._add(scan.SUBJECT_TYPE, "c")
        counts = waiting_counts(self.store)
        self.assertEqual(counts, {"scenes": 1, "tags": 0, "performers": 0,
                                  "something-new": 2})

    def test_the_page_counts_rather_than_fetches(self):
        # The whole point of this ticket: `items()` decodes every payload
        # just to have the count throw the result away immediately, and
        # scales with the size of the backlog rather than the size of the
        # answer. Asserted on the call actually made, not on elapsed time --
        # a threshold a slow machine trips and a fast one hides.
        self._add(scan.SUBJECT_TYPE, "a")
        self._add(tags.SUBJECT_TYPE, "b", state="dismissed")
        spy = _ItemsSpy(self.store)
        waiting_counts(spy)
        self.assertEqual(spy.items_calls, 0)


class TheSummaryIsWiredToTheStoreAndTheLoop(_Base):
    """`serve` receives a callable, and a wiring mistake is invisible until
    something invokes it -- so every assertion here calls it."""

    def _served(self):
        captured = _CapturedServe()
        with patch.dict(os.environ, {ZONE_ENV_VAR: ZONE_NAME}):
            with patch("cronicled.__main__.serve", captured):
                with redirect_stdout(io.StringIO()):
                    main(["--db", self.db_path])
        return captured.kwargs

    def _seed_runs(self, *runs):
        """`(job, trigger, started, counts)` each, through the real store."""
        store = Store(self.db_path)
        try:
            for job, trigger, started, counts in runs:
                run_id = store.start_run(job, trigger=trigger, at=started)
                store.finish_run(run_id, outcome="completed", counts=counts,
                                 at=started)
        finally:
            store.close()

    def test_each_jobs_last_run_reaches_the_page_in_the_configured_zone(self):
        self._seed_runs(
            ("tag-scan", "manual", "2026-01-15T00:30:00+00:00",
             {"recorded": 1}),
            ("scene-scan", "scheduled", "2026-07-15T00:30:00+00:00",
             {"recorded": 4}))

        view = self._served()["summary"]()

        # Both sides of a daylight-saving transition in one assertion: an
        # offset applied as a constant gets exactly one of the two right.
        self.assertEqual(
            [(j["job"], j["trigger"], j["started"], j["counts"])
             for j in view["jobs"]],
            [("scene-scan", "scheduled", "2026-07-15 02:30", {"recorded": 4}),
             ("tag-scan", "manual", "2026-01-15 01:30", {"recorded": 1})])
        self.assertEqual(view["zone"], ZONE_NAME)

    def test_a_quiet_job_is_not_pushed_off_the_end_by_a_busy_one(self):
        # HARM: `recent_runs`'s own default is twenty rows. An afternoon of
        # pressing Scan would then push the nightly tag pass out of the read
        # entirely, and the page would answer "did it run?" with silence --
        # which reads exactly like a pass that never ran.
        self._seed_runs(("tag-scan", "scheduled",
                         "2026-01-15T00:30:00+00:00", {"recorded": 1}))
        self._seed_runs(*[("scene-scan", "manual",
                           "2026-07-15T00:%02d:00+00:00" % n, {"recorded": n})
                          for n in range(30)])

        view = self._served()["summary"]()

        self.assertEqual(sorted(j["job"] for j in view["jobs"]),
                         ["scene-scan", "tag-scan"])

    def test_what_is_waiting_is_counted_from_the_store_it_was_given(self):
        self._seed(subject_id="1")
        self._seed(subject_id="2")
        self._seed_merge()
        self._seed_reconcile()
        self._seed_unused()

        self.assertEqual(self._served()["summary"]()["waiting"],
                         {"scenes": 2, "tags": 3, "performers": 0})

    def test_an_install_with_nothing_scheduled_still_gets_a_summary(self):
        # `schedule_status` is `None` without a media server -- the summary is
        # not, because scans still run by hand and "what did the last one
        # find" is the question this page exists to answer either way.
        kwargs = self._served()
        self.assertIsNone(kwargs["schedule_status"])
        self.assertIsNone(kwargs["summary"]()["schedule"])
        self.assertEqual(kwargs["summary"]()["jobs"], [])


class TheSummaryShowsTheLaterOfTwoRunsInOneSecond(unittest.TestCase):
    """The whole chain the "each job's last run" rule rests on.

    `_utcnow` records to the second, so two runs of one job started in the same
    second carry an identical `started`. `to_summary_view` takes the FIRST row
    naming each job, which is the latest one only because `recent_runs` breaks
    that tie by arrival -- ordering on `started` alone leaves it to SQLite,
    which returns the OLDER row first. Without the tiebreak this page reports
    the earlier of the two as though it were the latest, and a stale summary
    looks exactly like a healthy one.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.store = Store(os.path.join(self._dir, "runs.sqlite3"))
        self.addCleanup(self.store.close)

    def test_the_second_of_two_runs_in_one_second_is_the_one_shown(self):
        same = "2026-03-01T04:00:00+00:00"
        first = self.store.start_run("scene-scan", trigger="scheduled",
                                     at=same)
        self.store.finish_run(first, outcome="completed",
                              counts={"recorded": 1}, at=same)
        second = self.store.start_run("scene-scan", trigger="manual", at=same)
        self.store.finish_run(second, outcome="completed",
                              counts={"recorded": 7}, at=same)

        rows = self.store.recent_runs()
        # The collision is real rather than assumed: without this the test
        # would still pass on a clock that happened to tick between the two,
        # and would then be proving nothing about the tie at all.
        self.assertEqual([r["started"] for r in rows], [same, same])

        view = to_summary_view(rows, {}, None, zone=ZONE)
        self.assertEqual(
            [(j["id"], j["trigger"], j["counts"]) for j in view["jobs"]],
            [(second, "manual", {"recorded": 7})])


if __name__ == "__main__":
    unittest.main()
