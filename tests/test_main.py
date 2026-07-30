import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cronicled.__main__ import build_scheduler, main
from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.artist import Aliases
from cronicled.config import CONFIG_DIR_ENV_VAR, ZONE_ENV_VAR
from cronicled.descriptions import PRODUCER_NAME as DESCRIPTION_PRODUCER_NAME
from cronicled.jobs import JobRunner
from cronicled.performer_tags import index_performers, match_tag
from cronicled.performer_tags import proposal as reconcile_proposal
from cronicled.runscan import SCHEDULED_SCAN_NAME, build_producer
from cronicled.scan import ScanProducer
from cronicled.schedule import Entry, as_utc, due, resolve
from cronicled.stash import Stash
from cronicled.store import Store
from cronicled.tag_hygiene import NO_SCENES, ONE_SCENE
from cronicled.tag_hygiene import proposal as unused_proposal
from cronicled.tags import TagMergeProducer, cluster_tags
from cronicled.tags import proposal as tag_proposal
from cronicled.web.actions import Actions

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


if __name__ == "__main__":
    unittest.main()
