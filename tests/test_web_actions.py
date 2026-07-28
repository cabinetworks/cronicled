import threading
import unittest

from cronicled.jobs import JobRejected, JobRunner
from cronicled.store import Store
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
    def __init__(self, item):
        self.item = item
        self.calls = []

    def items(self, folder=None, state=None, limit=None, offset=0):
        return [self.item] if self.item else []

    def mark_applied(self, fp, prior_state=None):
        self.calls.append(("applied", fp, prior_state))

    def mark_failed(self, fp, error):
        self.calls.append(("failed", fp, error))

    def dismiss(self, fp, reason=None):
        self.calls.append(("dismissed", fp, reason))

    def mute(self, subject_type, subject_id, reason=None):
        self.calls.append(("muted", subject_type, subject_id, reason))


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


class _Adapter:
    """The minimum `build_producer` needs off a site adapter: a scraper id
    to search under, no censorship map, and `catalog_resolvable=False` --
    so `examine` never asks `owner_of` anything (it would raise if it did),
    and resolves each file's creator from its own name and folder alone.
    None of these fixture scenes name anything a real adapter would
    recognise, so every one of them ends up muted, never proposed."""
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
        actions = Actions(store, None, runner=runner, adapter=_Adapter())
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
                          adapter=_Adapter())
        job = actions.scan(1)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        finished = self.runner.job(job.id)
        self.assertIn("selected 1 of 3", finished.message)

    def test_never_writes_to_the_media_server(self):
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapter=_Adapter())
        job = actions.scan(10)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        self.assertGreater(len(stash.calls), 0)
        for call in stash.calls:
            self.assertIn(call[0],
                         ("unorganized_scenes", "scrape_scenes_by_query"))

    def test_two_scans_in_turn_both_start(self):
        # HARM: `ScanProducer.name` is a fixed class attribute
        # ("library-scan"); reusing it across calls to `JobRunner.register`
        # would make every scan after the first one ever run against this
        # runner refuse with "already registered", not "busy".
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapter=_Adapter())
        first = actions.scan(1)
        self.assertTrue(self.runner.wait(first.id, WAIT))
        second = actions.scan(1)  # must not raise
        self.assertTrue(self.runner.wait(second.id, WAIT))
        self.assertNotEqual(first.id, second.id)

    def test_a_second_scan_while_one_runs_is_refused_not_swallowed(self):
        gate = threading.Event()
        actions = Actions(self.store, _BlockingScanStash(gate),
                          runner=self.runner, adapter=_Adapter())
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
                          adapter=_Adapter())
        self.assertIsNone(actions.scan_status())

    def test_reports_the_most_recently_started_scan(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        runner = JobRunner(store)
        self.addCleanup(runner.close)
        actions = Actions(store, _ScanStash([_library_scene(1)]),
                          runner=runner, adapter=_Adapter())
        job = actions.scan(1)
        self.assertTrue(runner.wait(job.id, WAIT))
        status = actions.scan_status()
        self.assertEqual(status.id, job.id)


if __name__ == "__main__":
    unittest.main()
