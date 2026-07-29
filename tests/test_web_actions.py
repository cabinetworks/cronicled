import threading
import unittest

from cronicled.adapters.base import SiteAdapter
from cronicled.jobs import JobRejected, JobRunner
from cronicled.store import Store
from cronicled.tags import cluster_tags
from cronicled.tags import proposal as tag_proposal
from cronicled.web.actions import Actions, ApplyFailed, UnknownProposal

WAIT = 10


class _FakeStash:
    def __init__(self, prior=None, fail=False):
        self.calls = []
        self._prior = prior if prior is not None else {"title": "old"}
        self._fail = fail

    def apply_scene(self, scene_id, match):
        self.calls.append(("apply", scene_id))
        if self._fail:
            raise RuntimeError("server said no")
        return {"prior": self._prior}

    def revert_scene(self, scene_id, prior):
        # Refuses an empty snapshot exactly as the real client does, and
        # BEFORE recording the call. A double that is more forgiving than the
        # thing it stands in for turns a missing guard into a passing test:
        # were the caller's own check dropped, a forgiving fake would return
        # cheerfully here while production raised.
        if not prior:
            raise ValueError(
                "cannot revert scene %s: snapshot is missing or empty"
                % scene_id)
        self.calls.append(("revert", scene_id, prior))
        return {}


class _FakeStore:
    def __init__(self, item, dismissed_item=None, muted_subjects=()):
        self.item = item
        # A separate slot from `item`: `items(state="dismissed")` must
        # answer for a row `items(state=None)` would never return (the
        # store's own default view excludes it) -- collapsing the two into
        # one attribute would make `undismiss` indistinguishable from
        # `dismiss`/`mute`/`undo`, none of which ever read the hidden view.
        self.dismissed_item = dismissed_item
        self._muted_subjects = set(muted_subjects)
        self.calls = []

    def items(self, folder=None, state=None, limit=None, offset=0):
        if state == "dismissed":
            return [self.dismissed_item] if self.dismissed_item else []
        return [self.item] if self.item else []

    def mark_applied(self, fp, prior_state=None):
        self.calls.append(("applied", fp, prior_state))

    def mark_failed(self, fp, error):
        self.calls.append(("failed", fp, error))

    def mark_reverted(self, fp):
        self.calls.append(("reverted", fp))

    def dismiss(self, fp, reason=None):
        self.calls.append(("dismissed", fp, reason))

    def mute(self, subject_type, subject_id, reason=None):
        self.calls.append(("muted", subject_type, subject_id, reason))

    def undismiss(self, fp):
        self.calls.append(("undismissed", fp))

    def unmute(self, subject_type, subject_id):
        self.calls.append(("unmuted", subject_type, subject_id))

    def supersede(self, fp):
        self.calls.append(("superseded", fp))

    def muted_subjects(self):
        return self._muted_subjects


def _item(candidate=None, **over):
    # `candidate` is its own parameter, not folded into `**over`: it lives
    # under `payload`, and `item.update(over)` only ever touches the top
    # level. Defaults to a candidate with no cover, matching every existing
    # caller here that never mentions one.
    item = {"fingerprint": "fp-1", "state": "new", "subject_type": "scene",
            "subject_id": "42", "prior_state": None,
            "payload": {"path": "/l/a.mp4",
                        "creator": {"name": "N", "source": "folder",
                                    "competing": None,
                                    "rejected_folder": None},
                        "candidate": (candidate if candidate is not None
                                     else {"id": "c-1", "title": "T",
                                          "image": None}),
                        "score": 0.81, "runners_up": []}}
    item.update(over)
    return item


class Approve(unittest.TestCase):
    def test_stores_the_snapshot_the_apply_returned(self):
        store, stash = _FakeStore(_item()), _FakeStash(prior={"title": "was"})
        Actions(store, stash).approve("fp-1")
        self.assertEqual(store.calls,
                         [("applied", "fp-1", {"title": "was"})])

    def test_a_failed_apply_is_recorded_as_failed_not_applied(self):
        # Marking it applied would offer an undo for a write that never
        # happened, and revert_scene would then restore a snapshot that does
        # not describe anything.
        store, stash = _FakeStore(_item()), _FakeStash(fail=True)
        with self.assertRaises(ApplyFailed):
            Actions(store, stash).approve("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["failed"])

    def test_an_unknown_fingerprint_raises_rather_than_silently_doing_nothing(self):
        with self.assertRaises(UnknownProposal):
            Actions(_FakeStore(None), _FakeStash()).approve("fp-nope")


class Undo(unittest.TestCase):
    def test_reverts_with_the_stored_snapshot(self):
        item = _item(state="applied", prior_state={"title": "was"})
        store, stash = _FakeStore(item), _FakeStash()
        Actions(store, stash).undo("fp-1")
        self.assertEqual(stash.calls,
                         [("revert", "42", {"title": "was"})])

    def test_refuses_when_there_is_no_snapshot(self):
        # revert_scene raises on an empty snapshot by design. Reaching it with
        # one would turn a refusal a person can act on into a stack trace.
        item = _item(state="applied", prior_state=None)
        store, stash = _FakeStore(item), _FakeStash()
        with self.assertRaises(ValueError):
            Actions(store, stash).undo("fp-1")
        self.assertEqual(stash.calls, [])

    def test_a_plain_revert_reports_a_clean_reversal(self):
        # No cover was ever written by this proposal's apply, so there is
        # nothing revert_scene's snapshot-based restore leaves behind --
        # "reverted" is the whole truth here.
        item = _item(state="applied", prior_state={"title": "was"},
                     candidate={"id": "c-1", "title": "T", "image": None})
        store, stash = _FakeStore(item), _FakeStash()
        self.assertEqual(Actions(store, stash).undo("fp-1"), "reverted")

    def test_reverting_a_proposal_that_wrote_a_cover_names_the_residual(self):
        # HARM: `revert_scene` restores everything ITS OWN snapshot
        # describes and, on its own terms, reports a plain success -- but
        # that snapshot never held the scene's prior cover in the first
        # place (see `Stash.apply_scene`'s docstring), so a bare "reverted"
        # here would tell whoever reads it that this call undid the whole
        # apply. It did not: the cover this proposal's apply wrote is
        # exactly as unrestored as it was before this call.
        item = _item(state="applied", prior_state={"title": "was"},
                     candidate={"id": "c-1", "title": "T",
                               "image": "data:image/jpeg;base64,cover"})
        store, stash = _FakeStore(item), _FakeStash()
        result = Actions(store, stash).undo("fp-1")
        self.assertNotEqual(result, "reverted")
        self.assertIn("cannot be restored", result)
        # The revert itself still has to run -- reporting the residual
        # must not come at the cost of skipping the actual restore.
        self.assertEqual(stash.calls,
                         [("revert", "42", {"title": "was"})])


class NoStashConfigured(unittest.TestCase):
    # cronicled/__main__.py starts the inbox with `stash=None` when no
    # `--server` was given (see its module docstring). Approve and Undo are
    # the two actions that write to a media server, so those two must refuse
    # with a clear, specific message rather than an AttributeError on `None`.

    def test_approve_refuses_clearly_and_records_the_row_as_failed(self):
        store = _FakeStore(_item())
        with self.assertRaises(ApplyFailed) as ctx:
            Actions(store, None).approve("fp-1")
        self.assertIn("no media server is configured", str(ctx.exception))
        # Same invariant as a real apply failure: never left as "new" with
        # no record of what happened, and never marked "applied".
        self.assertEqual([c[0] for c in store.calls], ["failed"])

    def test_undo_refuses_clearly_rather_than_an_attribute_error(self):
        item = _item(state="applied", prior_state={"title": "was"})
        store = _FakeStore(item)
        with self.assertRaises(RuntimeError) as ctx:
            Actions(store, None).undo("fp-1")
        self.assertIn("no media server is configured", str(ctx.exception))
        # No store mutation on this path -- undo only ever writes through
        # the stash, and there is none configured to have written through.
        self.assertEqual(store.calls, [])


class Reject(unittest.TestCase):
    # Both of these assert the call WHOLE, reason string included. Sampling
    # the first element, or slicing the tuple short, leaves the reason free to
    # drift: exchanging the two reason texts while still calling the right
    # store method passed every assertion here before this change. That text
    # is durable -- it is what a person reads months later to learn why a
    # proposal was rejected -- so a dismissal recorded as "muted from the
    # inbox" misinforms them about a decision they can no longer remember.

    def test_dismiss_rejects_this_proposal_only(self):
        store = _FakeStore(_item())
        Actions(store, _FakeStash()).dismiss("fp-1")
        self.assertEqual(store.calls,
                         [("dismissed", "fp-1", "dismissed from the inbox")])

    def test_mute_rejects_the_subject_and_passes_its_identity_whole(self):
        store = _FakeStore(_item())
        Actions(store, _FakeStash()).mute("fp-1")
        self.assertEqual(store.calls,
                         [("muted", "scene", "42", "muted from the inbox")])

    def test_the_two_rejections_do_not_share_a_reason(self):
        # Named apart because they mean different things to whoever reads them
        # later: one says a better proposal for this file may still arrive,
        # the other says stop offering this file at all. A catch-all covering
        # both would satisfy each test above on its own.
        dismissed, muted = _FakeStore(_item()), _FakeStore(_item())
        Actions(dismissed, _FakeStash()).dismiss("fp-1")
        Actions(muted, _FakeStash()).mute("fp-1")
        self.assertNotEqual(dismissed.calls[0][-1], muted.calls[0][-1])
        self.assertIn("dismiss", dismissed.calls[0][-1])
        self.assertIn("mute", muted.calls[0][-1])


class Refresh(unittest.TestCase):
    # Ticket 86: an explicit, per-row way to supersede a stale proposal and
    # free its file for the next scan to examine again.

    def test_refresh_supersedes_this_fingerprint(self):
        store = _FakeStore(_item())
        Actions(store, _FakeStash()).refresh("fp-1")
        self.assertEqual(store.calls, [("superseded", "fp-1")])

    def test_refresh_never_dismisses(self):
        # Superseding must not be a rejection recorded on the person's
        # behalf -- see `Store.supersede`'s docstring. A mutation routing
        # `refresh` through `dismiss` instead would still redraw the page,
        # so this has to check the store call itself, not just the return
        # value.
        store = _FakeStore(_item())
        Actions(store, _FakeStash()).refresh("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["superseded"])

    def test_refresh_reaches_an_applied_rows_fingerprint_too(self):
        # The one case ticket 86 is actually about: an applied row has no
        # other path off the block it leaves in `scan.select`.
        item = _item(state="applied", prior_state={"title": "was"})
        store = _FakeStore(item)
        Actions(store, _FakeStash()).refresh("fp-1")
        self.assertEqual(store.calls, [("superseded", "fp-1")])

    def test_an_unknown_fingerprint_raises_rather_than_silently_doing_nothing(self):
        with self.assertRaises(UnknownProposal):
            Actions(_FakeStore(None), _FakeStash()).refresh("fp-nope")


class _RunnerSpy:
    """Records every call a scan would make through it. Standing in for the
    real `JobRunner` in exactly the tests that must prove `unmute`/
    `undismiss` never reach one -- ticket 75's whole point is that reversing
    a rejection must never look like asking for a scan."""

    def __init__(self):
        self.calls = []

    def register(self, producer):
        self.calls.append(("register", producer))

    def start(self, name):
        self.calls.append(("start", name))
        return None


class Undismiss(unittest.TestCase):
    def test_reverses_the_dismissal_of_the_named_proposal(self):
        store = _FakeStore(item=None, dismissed_item=_item(state="dismissed"))
        Actions(store, _FakeStash()).undismiss("fp-1")
        self.assertEqual(store.calls, [("undismissed", "fp-1")])

    def test_an_unknown_fingerprint_raises_rather_than_silently_doing_nothing(self):
        # Mirrors approve/dismiss/mute/undo's own reasoning: a no-op here is
        # indistinguishable from a success, and a doubled click on an
        # already-undismissed row must not look like it worked twice.
        store = _FakeStore(item=None, dismissed_item=None)
        with self.assertRaises(UnknownProposal):
            Actions(store, _FakeStash()).undismiss("fp-1")
        self.assertEqual(store.calls, [])

    def test_a_row_that_is_currently_visible_not_dismissed_is_unknown(self):
        # HARM: `_find` (what approve/dismiss/mute/undo use) only ever
        # searches the VISIBLE set, which by definition never contains a
        # dismissed row. If `undismiss` reused `_find` here it could never
        # find the very thing it exists to reverse -- it has to search
        # `items(state="dismissed")` instead.
        store = _FakeStore(item=_item(state="new"), dismissed_item=None)
        with self.assertRaises(UnknownProposal):
            Actions(store, _FakeStash()).undismiss("fp-1")
        self.assertEqual(store.calls, [])

    def test_does_not_trigger_a_scan_or_a_lookup(self):
        store = _FakeStore(item=None, dismissed_item=_item(state="dismissed"))
        stash = _FakeStash()
        runner = _RunnerSpy()
        Actions(store, stash, runner=runner, adapters={"only": object()}).undismiss("fp-1")
        self.assertEqual(stash.calls, [])
        self.assertEqual(runner.calls, [])


class Unmute(unittest.TestCase):
    def test_reverses_the_mute_on_the_named_subject(self):
        store = _FakeStore(item=None, muted_subjects={("scene", "42")})
        Actions(store, _FakeStash()).unmute("scene", "42")
        self.assertEqual(store.calls, [("unmuted", "scene", "42")])

    def test_an_unmuted_subject_raises_rather_than_silently_doing_nothing(self):
        store = _FakeStore(item=None, muted_subjects=set())
        with self.assertRaises(UnknownProposal):
            Actions(store, _FakeStash()).unmute("scene", "42")
        self.assertEqual(store.calls, [])

    def test_does_not_trigger_a_scan_or_a_lookup(self):
        # HARM (the reason acceptance calls this out by name): a click that
        # spends a third party's rate limit without looking like a scan is a
        # surprise, and this is the one control that must never cause one.
        store = _FakeStore(item=None, muted_subjects={("scene", "42")})
        stash = _FakeStash()
        runner = _RunnerSpy()
        Actions(store, stash, runner=runner, adapters={"only": object()}).unmute(
            "scene", "42")
        self.assertEqual(stash.calls, [])
        self.assertEqual(runner.calls, [])

    def test_only_the_named_subject_is_unmuted_not_every_muted_subject(self):
        # HARM: "no bulk actions" -- unmuting must act on exactly the
        # subject asked about, never on every muted subject at once.
        store = _FakeStore(item=None,
                           muted_subjects={("scene", "1"), ("scene", "2")})
        Actions(store, _FakeStash()).unmute("scene", "1")
        self.assertEqual(store.calls, [("unmuted", "scene", "1")])


class _Adapter(SiteAdapter):
    """The minimum `build_producer` needs off a site adapter: a scraper id
    to search under, no censorship map, and `catalog_resolvable=False` --
    so `examine` never asks `owner_of` anything (it would raise if it did),
    and resolves each file's creator from its own name and folder alone.
    None of these fixture scenes name anything a real adapter would
    recognise, so every one of them ends up muted, never proposed.

    It inherits `search_query` rather than restating it: the per-title
    fallback phrases its query through that method, and a double carrying
    its own copy of the phrasing could agree with itself while disagreeing
    with the adapter it stands in for."""
    name = "test-adapter"
    scraper_id = "scraper-test"
    censorship = {}
    catalog_resolvable = False

    def owner_of(self, result):
        raise AssertionError(
            "catalog_resolvable is False; owner_of must not be called")


class _ScanStash:
    """Everything a scan wired through `Actions.scan` may touch: one read to
    enumerate the library, one read per query to the configured scraper,
    and one read per proposal to enrich the winning candidate's own URL.
    `apply_scene`/`revert_scene` -- the two ways a media server is actually
    WRITTEN to -- raise rather than quietly succeeding: a scan reaching
    either is exactly the regression this class exists to catch."""

    def __init__(self, scenes=()):
        self._scenes = list(scenes)
        self.calls = []

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", limit))
        scenes = self._scenes if limit is None else self._scenes[:limit]
        return len(self._scenes), list(scenes)

    def scrape_scenes_by_query(self, scraper_id, query):
        self.calls.append(("scrape_scenes_by_query", scraper_id, query))
        return []

    def scrape_scene_url(self, url):
        # Every scene this fixture set builds ends up muted (see
        # `_library_scene`), so `examine` never picks a winner and this is
        # never actually called; it exists only so `build_producer` can
        # read `stash.scrape_scene_url` off this fake the same way it reads
        # it off a real `Stash`.
        self.calls.append(("scrape_scene_url", url))
        return None

    def apply_scene(self, *args, **kwargs):
        raise AssertionError("a scan must never write to the media server")

    def revert_scene(self, *args, **kwargs):
        raise AssertionError("a scan must never write to the media server")


class _BlockingScanStash(_ScanStash):
    """Blocks `unorganized_scenes` on a gate the test controls, so a scan
    job can be held genuinely `running` for as long as a test needs --
    deterministically, rather than by racing a real scan to finish before
    the test's next line runs."""

    def __init__(self, gate):
        super().__init__(scenes=())
        self._gate = gate

    def unorganized_scenes(self, limit):
        self._gate.wait(WAIT)
        return super().unorganized_scenes(limit)


def _library_scene(sid):
    # A path naming no real creator or store this fixture set defines --
    # every scene here is expected to end up muted (no candidates, or an
    # unresolved creator), never proposed.
    return {"id": str(sid),
            "files": [{"path": "/library/Unresolved Corner/clip-%s.mp4" % sid}]}


class ScanNotConfigured(unittest.TestCase):
    # `approve`/`undo` already refuse this clearly for a missing `stash`;
    # `scan` needs a runner AND an adapter as well, and must refuse just as
    # clearly rather than an AttributeError on `None`.

    def test_refuses_clearly_with_no_runner_or_adapter_configured(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(RuntimeError) as ctx:
            Actions(store, None).scan(5)
        self.assertIn("adapter", str(ctx.exception))

    def test_refuses_clearly_with_no_stash_configured(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        runner = JobRunner(store)
        self.addCleanup(runner.close)
        actions = Actions(store, None, runner=runner, adapters={"only": _Adapter()})
        with self.assertRaises(RuntimeError) as ctx:
            actions.scan(5)
        self.assertIn("no media server is configured", str(ctx.exception))


class Scan(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.runner = JobRunner(self.store)
        self.addCleanup(self.runner.close)

    def test_the_limit_reaches_the_job(self):
        # HARM: a `limit` that stops at the HTTP layer and never reaches the
        # producer would scan the whole library regardless of what was
        # asked for -- this is the one place that can actually be checked,
        # since `scan.select` logs how many of how many it took.
        stash = _ScanStash([_library_scene(1), _library_scene(2),
                            _library_scene(3)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()})
        job = actions.scan(1)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        finished = self.runner.job(job.id)
        self.assertIn("selected 1 of 3", finished.message)

    def test_never_writes_to_the_media_server(self):
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()})
        job = actions.scan(10)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        self.assertGreater(len(stash.calls), 0)
        for call in stash.calls:
            self.assertIn(call[0],
                         ("unorganized_scenes", "scrape_scenes_by_query"))

    def test_two_scans_in_turn_both_start(self):
        # HARM: `ScanProducer.name` is a fixed class attribute
        # ("library-scan"), and `scan` now reuses it as-is across calls --
        # via `JobRunner.reregister`, which replaces rather than refuses --
        # so this must not raise "already registered" on the second call.
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()})
        first = actions.scan(1)
        self.assertTrue(self.runner.wait(first.id, WAIT))
        second = actions.scan(1)  # must not raise
        self.assertTrue(self.runner.wait(second.id, WAIT))
        self.assertNotEqual(first.id, second.id)

    def test_repeated_scans_do_not_grow_the_producer_registry(self):
        # The regression this control used to accept: a fresh generated name
        # per scan meant the runner's producer registry -- which has no
        # eviction of its own -- grew by one small object every time a
        # person clicked Scan, for the life of the process. `reregister`
        # replaces the one entry instead, so the registry stays at exactly
        # one producer no matter how many scans have run.
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()})
        for _ in range(3):
            job = actions.scan(1)
            self.assertTrue(self.runner.wait(job.id, WAIT))
        self.assertEqual(len(self.runner.producers()), 1)
        self.assertEqual(self.runner.producers()[0].name, "library-scan")

    def test_repeated_scans_share_one_recognisable_producer_name(self):
        # The other half of the same complaint: a generated name per scan
        # made the job history read as a different producer every time,
        # instead of one recurring activity a person could recognise.
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()})
        names = []
        for _ in range(3):
            job = actions.scan(1)
            self.assertTrue(self.runner.wait(job.id, WAIT))
            names.append(self.runner.job(job.id).producer)
        self.assertEqual(names, ["library-scan"] * 3)

    def test_a_second_scan_while_one_runs_is_refused_not_swallowed(self):
        gate = threading.Event()
        actions = Actions(self.store, _BlockingScanStash(gate),
                          runner=self.runner, adapters={"only": _Adapter()})
        first = actions.scan(1)
        try:
            with self.assertRaises(JobRejected):
                actions.scan(1)
        finally:
            gate.set()
            self.runner.wait(first.id, WAIT)


class ScanStatus(unittest.TestCase):
    def test_none_when_scanning_is_not_configured(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        self.assertIsNone(Actions(store, None).scan_status())

    def test_none_before_any_scan_has_run(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        runner = JobRunner(store)
        self.addCleanup(runner.close)
        actions = Actions(store, _ScanStash([]), runner=runner,
                          adapters={"only": _Adapter()})
        self.assertIsNone(actions.scan_status())

    def test_reports_the_most_recently_started_scan(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        runner = JobRunner(store)
        self.addCleanup(runner.close)
        actions = Actions(store, _ScanStash([_library_scene(1)]),
                          runner=runner, adapters={"only": _Adapter()})
        job = actions.scan(1)
        self.assertTrue(runner.wait(job.id, WAIT))
        status = actions.scan_status()
        self.assertEqual(status.id, job.id)


if __name__ == "__main__":
    unittest.main()


class UndoLeavesATrace(unittest.TestCase):
    """An undo used to change nothing in the store. The revert happened on the
    media server, the row stayed `applied`, and the page went on offering an
    Undo button -- so a person clicked it, it worked, the page redrew
    identically, and the only reasonable conclusion was that it had not
    worked. Reported exactly that way in use.
    """

    def _applied(self):
        return _item(state="applied", prior_state={"title": "was"})

    def test_a_successful_undo_is_recorded(self):
        store, stash = _FakeStore(self._applied()), _FakeStash()
        Actions(store, stash).undo("fp-1")
        self.assertIn(("reverted", "fp-1"), store.calls)

    def test_nothing_is_recorded_when_the_revert_fails(self):
        # Ordering: a row claiming the write was taken back while it is still
        # applied on the server is worse than one that says nothing.
        class _Failing(_FakeStash):
            def revert_scene(self, scene_id, prior):
                raise RuntimeError("server said no")

        store = _FakeStore(self._applied())
        with self.assertRaises(RuntimeError):
            Actions(store, _Failing()).undo("fp-1")
        self.assertEqual(store.calls, [])

    def test_nothing_is_recorded_when_there_is_no_snapshot(self):
        store = _FakeStore(_item(state="applied", prior_state=None))
        with self.assertRaises(ValueError):
            Actions(store, _FakeStash()).undo("fp-1")
        self.assertEqual(store.calls, [])


# -- description proposals ------------------------------------------------- #


class _FakeDescriptionStash:
    """A media server holding one performer's description.

    It carries the real client's LIMITATIONS, not just its interface:
    `apply_performer_description` refuses when the current text is not what
    the caller expected, and `revert_performer_description` refuses a
    snapshot that is missing or carries no description -- both exactly as
    `cronicled.stash.Stash` does. A double that wrote regardless would turn
    the missing guard into a passing test.

    `apply_scene` and `revert_scene` raise on sight. The dispatch under test
    is which write a proposal's subject type reaches, and a double that
    answered both would be unable to tell a wrong dispatch from a right one.
    """

    def __init__(self, details="<p>Before.</p>"):
        self.details = details
        self.calls = []

    def apply_performer_description(self, performer_id, description, *,
                                    expected):
        if self.details != expected:
            raise RuntimeError(
                "performer %s's description is not the text this proposal was "
                "made from" % performer_id)
        prior = self.details
        self.details = description
        self.calls.append(("apply", performer_id, description))
        return {"prior": {"details": prior}}

    def revert_performer_description(self, performer_id, prior):
        if not prior or "details" not in prior:
            raise ValueError(
                "cannot revert performer %s: snapshot is missing, empty, or "
                "carries no description" % performer_id)
        self.details = prior["details"]
        self.calls.append(("revert", performer_id, prior))
        return {"details": prior["details"]}

    def apply_scene(self, *args, **kwargs):
        raise AssertionError(
            "a description proposal reached the scene apply path")

    def revert_scene(self, *args, **kwargs):
        raise AssertionError(
            "a description proposal reached the scene revert path")


def _description_item(**over):
    item = {"fingerprint": "fp-d", "state": "new",
            "subject_type": "performer", "subject_id": "7",
            "prior_state": None,
            "payload": {"name": "Wren Alderly", "field": "details",
                        "faults": ["markup"],
                        "original": "<p>Before.</p>",
                        "cleaned": "Before."}}
    item.update(over)
    return item


# -- tag-merge proposals ---------------------------------------------------- #


class _MergingStash(_FakeStash):
    """Adds the one write a tag merge makes, and keeps the real client's
    limitations while doing it.

    `Stash.merge_tags` takes `(destination_id, source_ids, aliases=None)` and
    coerces every id to a string on the way out. Recording the call verbatim
    is what lets a test assert the WHOLE argument set -- an `aliases` value
    slipped in would replace the destination's entire alias list on a real
    server, and a check of only the ids could not see it arrive.
    """

    def __init__(self, fail=False):
        _FakeStash.__init__(self)
        self._fail_merge = fail
        self.merges = []

    def merge_tags(self, destination_id, source_ids, aliases=None):
        self.merges.append((destination_id, list(source_ids), aliases))
        if self._fail_merge:
            raise RuntimeError("server said no")
        return {"id": destination_id, "name": "x", "aliases": []}


def _merge_item(tags=None, **over):
    """One tag-merge item, built through `cronicled.tags`' own producer path
    rather than hand-written, so it cannot describe a payload shape nothing
    ever emits."""
    if tags is None:
        tags = [{"id": "1", "name": "Velvet Crane", "aliases": [],
                 "scene_count": 12},
                {"id": "9", "name": "VelvetCrane", "aliases": [],
                 "scene_count": 4}]
    built = tag_proposal(cluster_tags(tags)[0], "library")
    item = {"fingerprint": "fp-m", "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "prior_state": None,
            "payload": built["payload"]}
    item.update(over)
    return item


class ApproveADescription(unittest.TestCase):
    def test_it_writes_the_cleaned_text_through_the_description_path(self):
        store, stash = _FakeStore(_description_item()), _FakeDescriptionStash()

        self.assertEqual(Actions(store, stash).approve("fp-d"), "applied")

        self.assertEqual(stash.calls, [("apply", "7", "Before.")])
        self.assertEqual(stash.details, "Before.")

    def test_the_snapshot_stored_is_the_text_the_write_replaced(self):
        store, stash = _FakeStore(_description_item()), _FakeDescriptionStash()

        Actions(store, stash).approve("fp-d")

        self.assertEqual(store.calls,
                         [("applied", "fp-d", {"details": "<p>Before.</p>"})])

    def test_a_description_edited_since_the_scan_fails_and_writes_nothing(self):
        store = _FakeStore(_description_item())
        stash = _FakeDescriptionStash(details="Rewritten by hand.")

        with self.assertRaises(ApplyFailed):
            Actions(store, stash).approve("fp-d")

        self.assertEqual(stash.calls, [])
        self.assertEqual(stash.details, "Rewritten by hand.")
        # Recorded as FAILED, never applied: an applied row offers an undo,
        # and there is nothing to undo.
        self.assertEqual([c[0] for c in store.calls], ["failed"])


class UndoADescription(unittest.TestCase):
    def test_it_restores_the_exact_prior_text_including_whitespace(self):
        # The whole field, whitespace and all. A revert asserted as "the tag
        # is back" would pass for one that restored a tidied approximation of
        # what was there, which is a third state nobody chose.
        prior = {"details": "  <p>Before.</p>\n\n  and a second line  "}
        store = _FakeStore(_description_item(
            state="applied", prior_state=prior))
        stash = _FakeDescriptionStash(details="Before.\n\nand a second line")

        self.assertEqual(Actions(store, stash).undo("fp-d"), "reverted")

        self.assertEqual(stash.details,
                         "  <p>Before.</p>\n\n  and a second line  ")
        self.assertEqual(stash.calls, [("revert", "7", prior)])
        self.assertEqual(store.calls, [("reverted", "fp-d")])

    def test_an_applied_description_round_trips_back_to_where_it_started(self):
        # Approve then Undo, through the same objects, asserting the field
        # itself rather than the calls: the undo has to be reversible in fact,
        # not merely called.
        before = "<p>Marsh &amp; Holloway.</p>"
        stash = _FakeDescriptionStash(details=before)
        item = _description_item(payload={
            "name": "Wren Alderly", "field": "details",
            "faults": ["markup", "entity"],
            "original": before, "cleaned": "Marsh & Holloway."})
        store = _FakeStore(item)
        actions = Actions(store, stash)

        actions.approve("fp-d")
        self.assertEqual(stash.details, "Marsh & Holloway.")
        # What the store recorded is what an undo is later handed.
        snapshot = store.calls[-1][2]
        item["state"] = "applied"
        item["prior_state"] = snapshot

        actions.undo("fp-d")

        self.assertEqual(stash.details, before)

    def test_an_applied_row_with_no_snapshot_refuses_rather_than_reverting(self):
        store = _FakeStore(_description_item(state="applied", prior_state=None))
        stash = _FakeDescriptionStash()

        with self.assertRaises(ValueError):
            Actions(store, stash).undo("fp-d")

        self.assertEqual(stash.calls, [])


_THREE_SPELLINGS = [
    {"id": "1", "name": "IvyMayKingsley", "aliases": [], "scene_count": 1},
    {"id": "2", "name": "Ivy MayKingsley", "aliases": [], "scene_count": 2},
    {"id": "3", "name": "Ivy May Kingsley", "aliases": [], "scene_count": 3},
]


class ApproveAMerge(unittest.TestCase):
    def test_it_merges_the_losing_spellings_into_the_survivor(self):
        # The WHOLE call, `aliases` included. `Stash.merge_tags`'s `aliases`
        # REPLACES the destination's alias list, and the only list this could
        # pass is whatever the proposal captured when it was made -- days ago
        # -- so passing one would silently delete every alias added since.
        # An assertion on the ids alone could not see one appear.
        store, stash = _FakeStore(_merge_item()), _MergingStash()

        self.assertEqual(Actions(store, stash).approve("fp-m"), "merged")

        self.assertEqual(stash.merges, [("1", ["9"], None)])

    def test_it_records_no_undo_snapshot(self):
        # THE enforcement of the irreversibility decision, not a description
        # of it: with no `prior_state` in the store, no row anywhere can ever
        # offer an undo it cannot perform.
        store, stash = _FakeStore(_merge_item()), _MergingStash()

        Actions(store, stash).approve("fp-m")

        self.assertEqual(store.calls, [("applied", "fp-m", None)])

    def test_it_never_touches_the_scene_apply_path(self):
        # A merge routed through `apply_scene` would send a cluster key where
        # a scene id belongs, and the payload has no `candidate` for it to
        # read. Asserted as "no scene call was made" rather than as an
        # absence of an exception.
        store, stash = _FakeStore(_merge_item()), _MergingStash()

        Actions(store, stash).approve("fp-m")

        self.assertEqual(stash.calls, [])

    def test_an_undecided_cluster_is_refused_and_nothing_is_written(self):
        # Three spellings do not say which survives. Picking one here would
        # be the silent resolution the whole clustering rule refuses, done at
        # the moment it is most expensive.
        store = _FakeStore(_merge_item(tags=_THREE_SPELLINGS))
        stash = _MergingStash()

        with self.assertRaisesRegex(ValueError, "no agreed surviving"):
            Actions(store, stash).approve("fp-m")

        self.assertEqual(stash.merges, [])
        # Not recorded as failed either: nothing was attempted, so the
        # proposal is still exactly as open as it was.
        self.assertEqual(store.calls, [])

    def test_a_failed_merge_is_recorded_as_failed_not_applied(self):
        store, stash = _FakeStore(_merge_item()), _MergingStash(fail=True)

        with self.assertRaises(ApplyFailed):
            Actions(store, stash).approve("fp-m")

        self.assertEqual([c[0] for c in store.calls], ["failed"])

    def test_it_refuses_without_a_configured_media_server(self):
        store = _FakeStore(_merge_item())

        with self.assertRaises(ApplyFailed):
            Actions(store, None).approve("fp-m")

        self.assertEqual([c[0] for c in store.calls], ["failed"])


class UndoAMerge(unittest.TestCase):
    def test_it_refuses_and_says_the_merge_cannot_be_reversed(self):
        # Refused with the REASON. "No snapshot was stored for it" -- the
        # generic answer below this branch -- reads as an omission somebody
        # could go and fix, and there is nothing to fix: a merge destroys the
        # record an undo would need.
        item = _merge_item(state="applied")
        store, stash = _FakeStore(item), _MergingStash()

        with self.assertRaises(ValueError) as caught:
            Actions(store, stash).undo("fp-m")

        self.assertIn("cannot be undone", str(caught.exception))
        self.assertEqual(store.calls, [])
        self.assertEqual(stash.calls, [])

    def test_a_merge_that_somehow_carried_a_snapshot_is_still_refused(self):
        # The subject type decides, not the presence of a snapshot. A merge
        # whose row had acquired a `prior_state` from anywhere would
        # otherwise be replayed through `revert_scene` against a cluster key.
        item = _merge_item(state="applied", prior_state={"title": "was"})
        store, stash = _FakeStore(item), _MergingStash()

        with self.assertRaises(ValueError):
            Actions(store, stash).undo("fp-m")

        self.assertEqual(stash.calls, [])


class DismissAndMuteAMerge(unittest.TestCase):
    """The two store-only decisions, which need no special case at all --
    asserted so a later "merges are different" refactor cannot quietly take
    a person's ability to reject one."""

    def test_a_merge_can_be_dismissed(self):
        store = _FakeStore(_merge_item())
        self.assertEqual(Actions(store, None).dismiss("fp-m"), "dismissed")
        self.assertEqual([c[0] for c in store.calls], ["dismissed"])

    def test_muting_a_merge_keys_on_the_cluster_not_on_a_tag(self):
        store = _FakeStore(_merge_item())

        Actions(store, None).mute("fp-m")

        self.assertEqual(store.calls,
                         [("muted", "tag-cluster", "velvetcrane",
                           "muted from the inbox")])
