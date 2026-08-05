import threading
import unittest

from cronicled.adapters.base import SiteAdapter
from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.jobs import JobRejected, JobRunner
from cronicled.performer_tags import index_performers, match_tag
from cronicled.performer_tags import proposal as reconcile_proposal
from cronicled.scan import Identified, fingerprint_outcome
from cronicled.stash import Stash
from cronicled.store import Store
from cronicled.tag_descriptions import Found
from cronicled.tag_descriptions import proposal as tag_description_proposal
from cronicled.tag_hygiene import proposal as tag_hygiene_proposal
from cronicled.tags import cluster_tags
from cronicled.tags import proposal as tag_proposal
from cronicled.web.actions import (Actions, ApplyFailed, BatchResult,
                                   BulkApplyResult, UnknownProposal)
from tests.test_stash import CATALOGUE, LINK, OTHER_LINK, _MutableScene

WAIT = 10


class _FakeStash:
    def __init__(self, prior=None, fail=False):
        self.calls = []
        self._prior = prior if prior is not None else {"title": "old"}
        self._fail = fail

    def apply_scene(self, scene_id, match, drop_tag_ids=()):
        # `drop_tag_ids` is recorded, not acted on: this fake has no scene
        # state to drop a tag out of, unlike `_MarkerAwareStash` below, whose
        # whole point is to model that write. Every test built on THIS fake
        # constructs `Actions` with no marker configured, so the argument
        # this call ever actually receives is `()` -- see `Approve
        # .test_with_no_marker_configured_the_write_is_unchanged` for the one
        # place that is asserted rather than assumed.
        self.calls.append(("apply", scene_id, tuple(drop_tag_ids)))
        if self._fail:
            raise RuntimeError("server said no")
        return {"prior": self._prior}

    def tag_id_by_name(self, name):
        # Never legitimately called by any test built on this fake: every one
        # constructs `Actions` with no marker, and `Actions._marker_tag_ids`
        # returns `()` without reaching the stash at all in that case. Raising
        # here (rather than answering something plausible) is what would turn
        # a mutation that skipped the `self._marker is None` guard into a
        # loud failure instead of a silently-passing extra network read.
        raise AssertionError(
            "tag_id_by_name was called with no marker configured")

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
        rows = [self.item] if self.item else []
        if state is not None:
            # A row is returned for the state it is ACTUALLY in, never for
            # whichever state was asked about. Answering every explicit
            # `state=` with the one item made this double strictly more
            # capable than `Store.items`, and that is the shape that turns a
            # missing feature into a passing test: `undo`'s check for a
            # subject the media server no longer holds asks
            # `items(state=GONE)`, and a double that answered it with a `new`
            # row would report every proposal in the suite as gone.
            return [row for row in rows if row["state"] == state]
        # `state=None` is the store's default view, which hides exactly the
        # states `Store._HIDDEN_STATES` names. Read off the store rather than
        # listed again here: a second copy of that tuple would be free to
        # drift, and it would drift towards this double showing `undo` a row
        # the real one never would.
        return [row for row in rows
                if row["state"] not in Store._HIDDEN_STATES]

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


class UndoASubjectTheServerNoLongerHolds(unittest.TestCase):
    """An applied proposal whose scene has since been deleted.

    The write really happened and the snapshot really does describe the scene
    as it was -- there is simply nothing left to restore it onto. The row is
    kept (it is the only record of what was written), the button is gone from
    the page, and a request that arrives anyway has to be refused with a
    reason a person can read rather than with a stack trace against an id the
    server does not have.
    """

    def test_it_refuses_and_names_the_missing_scene(self):
        item = _item(state="gone", prior_state={"title": "was"})
        store, stash = _FakeStore(item), _FakeStash()

        with self.assertRaises(ValueError) as caught:
            Actions(store, stash).undo("fp-1")

        message = str(caught.exception)
        # The subject, so the refusal names WHICH file, and the path, because
        # that is what a person recognises it by. Both, not either: a message
        # naming only the fingerprint is the obscure failure this replaces.
        self.assertIn("scene", message)
        self.assertIn("42", message)
        self.assertIn("/l/a.mp4", message)
        self.assertIn("no longer on the media server", message)

    def test_nothing_is_written_to_the_server_or_the_store(self):
        # HARM: reaching `revert_scene` writes to an id the server does not
        # have, and recording a revert that never happened would leave the one
        # record of the original write claiming it was taken back.
        item = _item(state="gone", prior_state={"title": "was"})
        store, stash = _FakeStore(item), _FakeStash()

        with self.assertRaises(ValueError):
            Actions(store, stash).undo("fp-1")

        self.assertEqual(stash.calls, [])
        self.assertEqual(store.calls, [])

    def test_a_proposal_whose_scene_is_still_there_still_undoes(self):
        # The other direction, and the reason this cannot be a blanket
        # refusal: an ordinary applied row must go on reverting.
        item = _item(state="applied", prior_state={"title": "was"})
        store, stash = _FakeStore(item), _FakeStash()

        self.assertEqual(Actions(store, stash).undo("fp-1"), "reverted")
        self.assertEqual(stash.calls, [("revert", "42", {"title": "was"})])

    def test_the_refusal_is_not_the_no_snapshot_one(self):
        # HARM: "no snapshot was stored for it" reads as an omission somebody
        # could go and fix. The snapshot is right there; the file is not.
        item = _item(state="gone", prior_state={"title": "was"})

        with self.assertRaises(ValueError) as caught:
            Actions(_FakeStore(item), _FakeStash()).undo("fp-1")

        self.assertNotIn("no snapshot", str(caught.exception))

    def test_a_payload_with_no_path_still_names_the_subject(self):
        # HARM: a description proposal's payload carries no `path`. Indexing
        # it would turn this refusal into a KeyError -- the obscure failure
        # again, arriving through the branch added to prevent it.
        item = _item(state="gone", prior_state={"title": "was"},
                     subject_type="performer-description",
                     payload={"cleaned": "text", "original": "was"})

        with self.assertRaises(ValueError) as caught:
            Actions(_FakeStore(item), _FakeStash()).undo("fp-1")

        self.assertIn("performer-description 42", str(caught.exception))


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

    def start(self, name, *, trigger):
        self.calls.append(("start", name, trigger))
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
    either is exactly the regression this class exists to catch.

    `organized` names which of `scenes` carry the organized flag, and
    `tag_ids` is this installation's own tag-name-to-id map. Both reads below
    filter the one library table the way the server's own `scene_filter`
    does: an organized scene is invisible to `unorganized_scenes` here
    exactly as it is there, which is the whole reason a marker tag exists.
    """

    def __init__(self, scenes=(), organized=(), tag_ids=None, performers=None):
        self._scenes = list(scenes)
        self._organized = {str(scene_id) for scene_id in organized}
        self._tag_ids = dict(tag_ids or {})
        # No performer carries an alias by default -- an empty library, the
        # same answer every other read here gives for a fixture set that
        # never mentions performers at all.
        self._performers = list(performers or [])
        self.calls = []

    def _page(self, scenes, limit):
        return len(scenes), list(scenes if limit is None else scenes[:limit])

    def performers_with_aliases(self):
        self.calls.append(("performers_with_aliases",))
        return [dict(row) for row in self._performers]

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", limit))
        return self._page([s for s in self._scenes
                           if s["id"] not in self._organized], limit)

    def tag_id_by_name(self, name):
        self.calls.append(("tag_id_by_name", name))
        return self._tag_ids.get(name)

    def tagged_scenes(self, tag_id, limit):
        self.calls.append(("tagged_scenes", tag_id, limit))
        return self._page(
            [s for s in self._scenes
             if any(tag["id"] == tag_id for tag in s.get("tags", []))], limit)

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
                         ("unorganized_scenes", "scrape_scenes_by_query",
                          "performers_with_aliases"))

    def test_a_scan_from_the_page_is_recorded_as_a_manual_run(self):
        # HARM: this control is the button, and the scheduler's pass is the
        # other caller of the same producer. Filing a click as "scheduled"
        # would let a scan somebody ran at noon answer "did last night's
        # pass run" -- the one question the log exists for -- with a yes.
        stash = _ScanStash([_library_scene(1)])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()})
        job = actions.scan(1)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        rows = self.store.recent_runs()
        self.assertEqual([(r["job"], r["trigger"]) for r in rows],
                         [("library-scan", "manual")])

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


class TheConfiguredAliasesReachAScanStartedFromThePage(unittest.TestCase):
    """The button on the page is the path that mattered and the path that was
    broken: it called `build_producer` with `limit` and nothing else, so the
    resolver was handed `None` on every scan a person ever started, and
    configuring an alias did nothing.

    Started HERE, through `Actions.scan`, and not by calling `build_producer`
    with a map of this test's own -- that call would have passed against the
    broken code, which is exactly why the defect survived. The adapter is a
    real `DeclarativeAdapter` built from the spec shape an `adapters.json`
    entry has, for the same reason.
    """

    ALIASES = {"vcrane": "Velvet Crane"}

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.runner = JobRunner(self.store)
        self.addCleanup(self.runner.close)

    def _adapters(self, aliases):
        return {"only": DeclarativeAdapter(
            {"name": "only", "scraper_id": "scraper-only",
             "owner_source": "none", "catalog_resolvable": False,
             "title_match_counts_as_ownership": False,
             "aliases": dict(aliases)})}

    def _scan(self, aliases):
        # Filed under the abbreviation, so only the alias can turn this
        # folder into a creator's name. The filename names nobody, so the
        # folder is the only candidate either way.
        stash = _ScanStash([{"id": "1", "files": [
            {"path": "/library/VCrane/clip-1.mp4"}]}])
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters=self._adapters(aliases))
        job = actions.scan(10)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        return [call[2] for call in stash.calls
                if call[0] == "scrape_scenes_by_query"]

    def test_the_scan_searches_under_the_name_the_alias_declares(self):
        # HARM: the query a scan spends is the resolver's answer made
        # visible. Searching for "VCrane" asks a store about a creator it has
        # never heard of, so every file filed that way goes unmatched and is
        # muted -- and the operator who added the alias to fix exactly that
        # sees no change and cannot tell it was ignored rather than wrong.
        # Every query the run spent, in order: the per-creator pass, then the
        # per-title fallback for a file that pass could not resolve. Both are
        # phrased from the resolved name, so both are asserted -- a fallback
        # that reverted to the folder's own spelling would be a half-applied
        # alias, and a check on one query could not see it.
        self.assertEqual(self._scan(self.ALIASES),
                         ["Velvet Crane", "velvet crane clip-1"])

    def test_without_the_alias_the_same_scan_searches_the_folder_name(self):
        # The control: the only difference between the two is the map in
        # `adapters.json`. Without it the folder's own spelling is what
        # reaches the store, which is the behaviour the alias exists to
        # change -- and is what this scan did with an alias configured.
        self.assertEqual(self._scan({}), ["VCrane", "vcrane clip-1"])


class TheMarkerTagReachesAScanStartedFromThePage(unittest.TestCase):
    """The button on the page is the path a configured value goes missing on:
    `Actions.scan` builds its own producer per click, so anything the entry
    point read and did not hand over is simply absent from every scan a
    person ever starts. That is not hypothetical -- the alias map spent a
    whole ticket in exactly this state (see the class above), and a marker
    that only the scheduled scan honoured would be the same defect with a
    quieter symptom: the nightly pass and the button would look at different
    halves of the library.

    Started HERE, through `Actions.scan`, rather than by calling
    `build_producer` with a marker of this test's own -- that call would pass
    against the broken wiring, which is why the alias defect survived.
    """

    MARKER = "inferred-metadata"

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.runner = JobRunner(self.store)
        self.addCleanup(self.runner.close)

    def _scan(self, **kwargs):
        # One scene, ORGANIZED, carrying the marker: invisible to the
        # unorganized read, so the only way it reaches the scan is the
        # marker.
        scene = dict(_library_scene(1), tags=[{"id": "t-7",
                                               "name": self.MARKER}])
        stash = _ScanStash([scene], organized=("1",),
                           tag_ids={self.MARKER: "t-7"})
        actions = Actions(self.store, stash, runner=self.runner,
                          adapters={"only": _Adapter()}, **kwargs)
        job = actions.scan(10)
        self.assertTrue(self.runner.wait(job.id, WAIT))
        return self.runner.job(job.id).message

    def test_a_marked_organized_file_is_scanned_from_the_page(self):
        self.assertIn("selected 1 of 1", self._scan(marker=self.MARKER))

    def test_without_the_marker_the_same_file_is_not_offered_at_all(self):
        # The control: the only difference between the two is the value the
        # entry point read out of scan.json.
        self.assertIn("selected 0 of 0", self._scan())


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


# -- tag-description proposals ---------------------------------------------- #
#
# Every tag name and description below is invented.

TAG_DESCRIPTION = "Scenes lit only by a hand-carried lamp."
OTHER_TAG_DESCRIPTION = "Filmed aboard a working passenger boat."


class _FakeTagDescriptionStash:
    """A media server holding one tag's description.

    It carries the real client's LIMITATIONS: `apply_tag_description` refuses
    when the current text is not what the caller expected, and
    `revert_tag_description` refuses a snapshot that is missing or carries no
    description -- both exactly as `cronicled.stash.Stash` does. A double that
    wrote regardless would turn a missing guard into a passing test.

    Every other write raises on sight. The dispatch under test is which write
    a proposal's subject type reaches, and a double that answered them all
    would be unable to tell a wrong dispatch from a right one.
    """

    def __init__(self, description=None):
        self.description = description
        self.calls = []

    def apply_tag_description(self, tag_id, description, *, expected):
        if self.description != expected:
            raise RuntimeError(
                "tag %s's description is not the text this proposal was made "
                "against" % tag_id)
        prior = self.description
        self.description = description
        self.calls.append(("apply", tag_id, description))
        return {"prior": {"description": prior}}

    def revert_tag_description(self, tag_id, prior):
        if not prior or "description" not in prior:
            raise ValueError(
                "cannot revert tag %s: snapshot is missing, empty, or carries "
                "no description" % tag_id)
        self.description = prior["description"]
        self.calls.append(("revert", tag_id, prior))
        return {"description": prior["description"]}

    def apply_scene(self, *args, **kwargs):
        raise AssertionError(
            "a tag-description proposal reached the scene apply path")

    def revert_scene(self, *args, **kwargs):
        raise AssertionError(
            "a tag-description proposal reached the scene revert path")

    def apply_performer_description(self, *args, **kwargs):
        raise AssertionError(
            "a tag-description proposal reached the performer apply path")

    def revert_performer_description(self, *args, **kwargs):
        raise AssertionError(
            "a tag-description proposal reached the performer revert path")


def _tag_description_item(**over):
    """One tag-description item, built through
    `cronicled.tag_descriptions.proposal` rather than hand-written, so no test
    here can describe a payload shape nothing ever emits."""
    built = tag_description_proposal(
        {"id": "7", "name": "Lantern Work", "aliases": [],
         "description": None, "scene_count": 3},
        Found(description=TAG_DESCRIPTION, box="first"), folder="library")
    item = {"fingerprint": "fp-t", "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "prior_state": None,
            "payload": built["payload"]}
    item.update(over)
    return item


class ApproveATagDescription(unittest.TestCase):
    def test_it_writes_the_sources_text_through_the_tag_path(self):
        store, stash = _FakeStore(_tag_description_item()), \
            _FakeTagDescriptionStash()

        self.assertEqual(Actions(store, stash).approve("fp-t"), "applied")

        self.assertEqual(stash.calls, [("apply", "7", TAG_DESCRIPTION)])
        self.assertEqual(stash.description, TAG_DESCRIPTION)

    def test_the_snapshot_stored_is_what_the_write_replaced(self):
        store, stash = _FakeStore(_tag_description_item()), \
            _FakeTagDescriptionStash()

        Actions(store, stash).approve("fp-t")

        self.assertEqual(store.calls,
                         [("applied", "fp-t", {"description": None})])

    def test_a_tag_described_since_the_pass_is_refused_not_overwritten(self):
        # HARM: a sentence somebody wrote replaced by one lifted from a
        # third-party index, with nothing recording that it happened. The
        # `expected` value is the payload's `original` VERBATIM -- normalised
        # to `""` on the way through, it would fail against `None` and refuse
        # every legitimate apply instead.
        store = _FakeStore(_tag_description_item())
        stash = _FakeTagDescriptionStash(description=OTHER_TAG_DESCRIPTION)

        with self.assertRaises(ApplyFailed):
            Actions(store, stash).approve("fp-t")

        self.assertEqual(stash.description, OTHER_TAG_DESCRIPTION)
        self.assertEqual([c[0] for c in store.calls], ["failed"])


class UndoATagDescription(unittest.TestCase):
    def test_it_writes_back_exactly_what_the_snapshot_holds(self):
        item = _tag_description_item(state="applied",
                                     prior_state={"description": None})
        store, stash = _FakeStore(item), \
            _FakeTagDescriptionStash(description=TAG_DESCRIPTION)

        self.assertEqual(Actions(store, stash).undo("fp-t"), "reverted")

        self.assertEqual(stash.calls,
                         [("revert", "7", {"description": None})])
        self.assertIsNone(stash.description)

    def test_it_is_recorded_only_after_the_revert_succeeds(self):
        item = _tag_description_item(state="applied",
                                     prior_state={"description": None})
        store, stash = _FakeStore(item), _FakeTagDescriptionStash()

        Actions(store, stash).undo("fp-t")

        self.assertEqual(store.calls, [("reverted", "fp-t")])

    def test_an_applied_row_with_no_snapshot_refuses(self):
        # HARM: reaching `revert_tag_description` with an empty snapshot is
        # still refused, but as a stack trace rather than as a refusal naming
        # the proposal.
        item = _tag_description_item(state="applied", prior_state=None)
        store, stash = _FakeStore(item), _FakeTagDescriptionStash()

        with self.assertRaises(ValueError):
            Actions(store, stash).undo("fp-t")

        self.assertEqual(stash.calls, [])


# -- tag-merge proposals ---------------------------------------------------- #


class _MergingStash(_FakeStash):
    """Adds the one write a tag merge makes, and keeps the real client's
    limitations while doing it.

    `Stash.merge_tags` takes `(destination_id, source_ids, aliases=None,
    description=None)` and coerces every id to a string on the way out.
    Recording the call verbatim is what lets a test assert the WHOLE argument
    set -- an `aliases` value slipped in would replace the destination's whole
    alias list on a real server, and a check of only the ids could not see it
    arrive. `description` is recorded for the same reason and is the sharper
    one: a merge is where the only copy of a description gets deleted, so both
    "it was carried over" and "it was not" have to be visible here rather than
    inferred from the merge having happened.
    """

    def __init__(self, fail=False):
        _FakeStash.__init__(self)
        self._fail_merge = fail
        self.merges = []

    def merge_tags(self, destination_id, source_ids, aliases=None,
                   description=None):
        self.merges.append((destination_id, list(source_ids), aliases,
                            description))
        if self._fail_merge:
            raise RuntimeError("server said no")
        return {"id": destination_id, "name": "x", "aliases": []}


def _merge_item(tags=None, **over):
    """One tag-merge item, built through `cronicled.tags`' own producer path
    rather than hand-written, so it cannot describe a payload shape nothing
    ever emits."""
    if tags is None:
        tags = [{"id": "1", "name": "Velvet Crane", "aliases": [], "description": None,
                 "scene_count": 12},
                {"id": "9", "name": "VelvetCrane", "aliases": [], "description": None,
                 "scene_count": 4}]
    built = tag_proposal(cluster_tags(tags)[0], "library", [])
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
    {"id": "1", "name": "IvyMayKingsley", "aliases": [], "description": None, "scene_count": 1},
    {"id": "2", "name": "Ivy MayKingsley", "aliases": [], "description": None, "scene_count": 2},
    {"id": "3", "name": "Ivy May Kingsley", "aliases": [], "description": None, "scene_count": 3},
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

        self.assertEqual(stash.merges, [("1", ["9"], None, None)])

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

    def test_it_carries_a_description_off_a_spelling_it_is_about_to_delete(self):
        # HARM: without this the merge deletes the only tag carrying the
        # text, and nothing anywhere records what it said.
        item = _merge_item(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": None, "scene_count": 12},
            {"id": "9", "name": "LanternWork", "aliases": [],
             "description": TAG_DESCRIPTION, "scene_count": 4}])
        store, stash = _FakeStore(item), _MergingStash()

        Actions(store, stash).approve("fp-m")

        self.assertEqual(stash.merges,
                         [("1", ["9"], None, TAG_DESCRIPTION)])

    def test_two_differing_descriptions_carry_neither(self):
        # Not the destination's, not the longer, not the first. Both are on
        # the row for a person to read; nothing here picks between them.
        item = _merge_item(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": TAG_DESCRIPTION, "scene_count": 12},
            {"id": "9", "name": "LanternWork", "aliases": [],
             "description": OTHER_TAG_DESCRIPTION, "scene_count": 4}])
        store, stash = _FakeStore(item), _MergingStash()

        Actions(store, stash).approve("fp-m")

        self.assertEqual(stash.merges, [("1", ["9"], None, None)])

    def test_a_merge_recorded_before_descriptions_existed_still_merges(self):
        # HARM: those proposals are in the store. Indexed, the missing block
        # turns every one of them into a failed apply.
        item = _merge_item()
        del item["payload"]["description"]
        store, stash = _FakeStore(item), _MergingStash()

        self.assertEqual(Actions(store, stash).approve("fp-m"), "merged")

        self.assertEqual(stash.merges, [("1", ["9"], None, None)])

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


# -- the catalogue link, from the recorded payload to the server ----------- #
#
# Driven end to end, through a real `Stash` over a fake server that actually
# holds scene state, because the two ends of this live in different modules:
# the scan records which box identified the file and that box's endpoint, and
# only the update the server receives says whether anything was linked. A
# double for `Stash` here could only ever show that `Actions` called it.
#
# `example.invalid` is reserved by RFC 2606 and can never resolve.

BOX_PATH = "/library/Ivy Kingsley/Winter Ledger.mp4"
BOX_CANDIDATE = {"title": "Winter Ledger", "image": None}


def _fingerprint_payload(remote_site_id="r-77", candidate=None):
    """The payload a fingerprint-identified proposal is recorded with, built
    by the producer that really builds it.

    Written out by hand it would prove nothing: the producer and the applier
    have to agree about which keys carry the box's endpoint and its id, and
    an invented payload can only ever agree with whichever of them it was
    copied from.
    """
    return fingerprint_outcome(
        {"id": "s1", "files": [{"path": BOX_PATH}]},
        Identified(box="north-box", endpoint=CATALOGUE,
                   candidate=BOX_CANDIDATE if candidate is None else candidate,
                   remote_site_id=remote_site_id),
        folder="library").proposal["payload"]


class CatalogueLinkOnApprove(unittest.TestCase):

    def _approve(self, payload, stash_ids=()):
        server = _MutableScene(stash_ids=list(stash_ids))
        stash = Stash("http://example.test", "k", transport=server.transport)
        item = _item(payload=payload, subject_id="s1")
        store = _FakeStore(item)
        Actions(store, stash).approve("fp-1")
        return server, store, item

    def test_the_scene_after_approving_an_identified_proposal_is_exactly_this(self):
        # The WHOLE scene, not the one field this ticket adds. A field
        # slipped into a scene update past a green suite has happened here
        # before, and an unlisted `rating100` would blank the rating on every
        # scene an apply touches.
        server, _, _ = self._approve(_fingerprint_payload())

        self.assertEqual(server.snapshot(), {
            "title": "Winter Ledger", "details": None, "date": None,
            "urls": [], "organized": True, "rating100": None, "code": None,
            "director": None, "stash_ids": [LINK], "studio_id": None,
            "performer_ids": [], "tag_ids": []})

    def test_the_link_names_the_endpoint_the_payload_carries(self):
        # Stated on its own terms so it survives the shape above being
        # rewritten: this is the pair the box's answer stands for, and it is
        # the whole point of the ticket.
        payload = _fingerprint_payload()
        server, _, _ = self._approve(payload)

        self.assertEqual(server.snapshot()["stash_ids"],
                         [{"endpoint": payload["endpoint"],
                           "stash_id": payload["remote_site_id"]}])

    def test_a_box_that_named_no_id_links_the_scene_to_nothing(self):
        # HARM: such a box has still identified the file and the proposal is
        # still applied -- but an entry with a null id claims a catalogue
        # record that does not exist, and a later re-scrape would start from
        # it. No id means no link, never a link to nothing.
        server, _, _ = self._approve(_fingerprint_payload(remote_site_id=None))

        self.assertEqual(server.snapshot()["stash_ids"], [])

    def test_a_scored_proposal_leaves_the_scenes_own_links_alone(self):
        # A site scraper is not a catalogue endpoint, so a text-matched
        # proposal has nothing to link -- and must not write an empty list
        # over the links the scene already had.
        server, _, _ = self._approve(_item()["payload"],
                                     stash_ids=[OTHER_LINK])

        self.assertEqual(server.snapshot()["stash_ids"], [OTHER_LINK])

    def test_a_link_the_candidate_itself_carried_is_kept_beside_the_new_one(self):
        # Nothing produces such a candidate today. If a scraper ever does,
        # replacing its link with this one would discard a link the proposal
        # was made with, which is the failure the merge exists to prevent.
        server, _, _ = self._approve(_fingerprint_payload(
            candidate=dict(BOX_CANDIDATE, stash_ids=[OTHER_LINK])))

        self.assertEqual(server.snapshot()["stash_ids"], [OTHER_LINK, LINK])

    def test_undo_takes_the_link_back_off_the_scene(self):
        # A link written and not reverted is a change the operator cannot
        # take back. Driven through the snapshot the STORE was handed, not
        # the one the apply returned, because that is the only one an undo
        # days later has.
        server = _MutableScene(stash_ids=[OTHER_LINK])
        stash = Stash("http://example.test", "k", transport=server.transport)
        item = _item(payload=_fingerprint_payload(), subject_id="s1")
        store = _FakeStore(item)
        actions = Actions(store, stash)
        before = server.snapshot()

        actions.approve("fp-1")
        self.assertEqual(server.snapshot()["stash_ids"], [OTHER_LINK, LINK])
        item["state"], item["prior_state"] = "applied", store.calls[-1][2]

        self.assertEqual(actions.undo("fp-1"), "reverted")
        self.assertEqual(server.snapshot(), before)

    def test_undo_restores_a_scene_that_had_carried_no_links(self):
        # The empty case: `[]` and "absent" look alike everywhere except in
        # the restore input, so an undo that dropped the field would leave
        # the link in place and still report success.
        server = _MutableScene(stash_ids=[])
        stash = Stash("http://example.test", "k", transport=server.transport)
        item = _item(payload=_fingerprint_payload(), subject_id="s1")
        store = _FakeStore(item)
        actions = Actions(store, stash)
        before = server.snapshot()

        actions.approve("fp-1")
        self.assertEqual(server.snapshot()["stash_ids"], [LINK])
        item["state"], item["prior_state"] = "applied", store.calls[-1][2]

        actions.undo("fp-1")
        self.assertEqual(server.snapshot(), before)


class _FakeMultiItemStore:
    """A store holding several proposals at once, keyed by fingerprint --
    the shape `bulk_apply_tag_descriptions`/`batch_apply` need and
    `_FakeStore` above cannot offer (it holds exactly one proposal).
    `mark_applied`/`mark_failed`/`dismiss`/`mute`/`supersede` all mutate the
    held item's OWN `state`, the same as the real `Store`, so a fingerprint
    looked up later -- by a later row in the same batch, or by a later
    assertion -- sees what an earlier one actually did, not a frozen
    snapshot from before the call started.
    """

    # The same two states the real `Store.dismiss`/`mute`/`supersede`
    # protect from being overwritten (see their own docstrings): a
    # `dismiss`/`mute` reaching an already-applied or -failed row must not
    # erase the fact (and the resolution) that a real write happened or was
    # attempted. `batch_apply`'s own guard already keeps `dismiss`/`mute`
    # from reaching an applied row at all -- this mirrors the real store's
    # limitation anyway, so a double that is MORE forgiving than the real
    # collaborator can never be the reason a test here passes.
    _TERMINAL_STATES = ("applied", "failed")

    def __init__(self, items):
        self._items = {item["fingerprint"]: dict(item) for item in items}
        self.calls = []

    def items(self, folder=None, state=None, limit=None, offset=0):
        rows = list(self._items.values())
        if state is not None:
            return [row for row in rows if row["state"] == state]
        return [row for row in rows
                if row["state"] not in Store._HIDDEN_STATES]

    def item(self, fp):
        return self._items[fp]

    def mark_applied(self, fp, prior_state=None):
        self.calls.append(("applied", fp, prior_state))
        self._items[fp]["state"] = "applied"
        self._items[fp]["prior_state"] = prior_state

    def mark_failed(self, fp, error):
        self.calls.append(("failed", fp, error))
        self._items[fp]["state"] = "failed"
        self._items[fp]["error"] = error

    def dismiss(self, fp, reason=None):
        self.calls.append(("dismissed", fp, reason))
        if self._items[fp]["state"] not in self._TERMINAL_STATES:
            self._items[fp]["state"] = "dismissed"

    def mute(self, subject_type, subject_id, reason=None):
        self.calls.append(("muted", subject_type, subject_id, reason))
        for item in self._items.values():
            if (item["subject_type"], item["subject_id"]) == (
                    subject_type, subject_id):
                if item["state"] not in self._TERMINAL_STATES:
                    item["state"] = "muted"

    def supersede(self, fp):
        self.calls.append(("superseded", fp))
        if self._items[fp]["state"] not in self._TERMINAL_STATES:
            self._items[fp]["state"] = "superseded"


class _FakeMultiTagStash:
    """A media server holding several tags' descriptions at once, and
    nothing else -- every other write raises on sight, the multi-row sibling
    of `_FakeTagDescriptionStash` above and for the same reason: the
    dispatch under test is which write a proposal's subject type reaches,
    and a double that answered every kind of write would be unable to tell a
    wrong dispatch from a right one. That matters MORE here than for a
    single-row test: this is exactly the double a broken additive-only guard
    would need to be caught by, so it must not offer a forgiving path for a
    delete, a merge, a reconcile or a scene write to land on unnoticed.
    """

    def __init__(self, descriptions=None, raise_for=()):
        self.descriptions = dict(descriptions or {})
        self._raise_for = set(raise_for)
        self.calls = []

    def apply_tag_description(self, tag_id, description, *, expected):
        if tag_id in self._raise_for:
            raise RuntimeError("the media server refused tag %s" % tag_id)
        if self.descriptions.get(tag_id) != expected:
            raise RuntimeError(
                "tag %s's description is not the text this proposal was "
                "made against" % tag_id)
        prior = self.descriptions.get(tag_id)
        self.descriptions[tag_id] = description
        self.calls.append(("apply", tag_id, description))
        return {"prior": {"description": prior}}

    def apply_scene(self, *args, **kwargs):
        raise AssertionError(
            "a bulk tag-description batch reached the scene apply path")

    def delete_tag(self, *args, **kwargs):
        raise AssertionError(
            "a bulk tag-description batch reached the tag-delete path -- "
            "this is exactly what 'no bulk delete' must never allow")

    def merge_tags(self, *args, **kwargs):
        raise AssertionError(
            "a bulk tag-description batch reached the tag-merge path")

    def reconcile_tag_to_performer(self, *args, **kwargs):
        raise AssertionError(
            "a bulk tag-description batch reached the reconcile path")

    def apply_performer_description(self, *args, **kwargs):
        raise AssertionError(
            "a bulk tag-description batch reached the performer-description "
            "path")


def _bulk_tag_item(fp, tag_id, name, *, description=None, box="first"):
    """One tag-description item, built through
    `cronicled.tag_descriptions.proposal` -- the real population this whole
    action is scoped to -- rather than hand-written, so no test here can
    describe a payload shape nothing ever emits. `description=None` is the
    whole measured population (every sampled `original` was empty);
    `description="..."` builds the one row this action must refuse."""
    built = tag_description_proposal(
        {"id": tag_id, "name": name, "aliases": [], "description": description,
         "scene_count": 3},
        Found(description="a description from a stash-box", box=box),
        folder="library")
    return {"fingerprint": fp, "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "prior_state": None,
            "payload": built["payload"]}


def _hygiene_item(fp, tag_id="9", name="Rare Tag"):
    """One tag-deletion item, built through `cronicled.tag_hygiene.proposal`
    -- used here only to prove `bulk_apply_tag_descriptions` refuses it,
    never to apply it."""
    built = tag_hygiene_proposal(
        {"id": tag_id, "name": name, "scene_count": 0}, folder="library")
    return {"fingerprint": fp, "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "prior_state": None,
            "payload": built["payload"]}


class BulkApplyTagDescriptions(unittest.TestCase):
    """Acceptance: the applied set is exactly the rows submitted, no more
    and no fewer. Assert the WHOLE set of written ids, never a count and
    never a sampled member -- an unlisted id slipping through past a
    field-by-field or count-only assertion is exactly the shape that has
    cost this project a corrupted library before."""

    def test_the_whole_submitted_set_is_applied_and_nothing_else(self):
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-1", "1", "Lantern Work"),
            _bulk_tag_item("fp-2", "2", "Passenger Boat"),
            _bulk_tag_item("fp-3", "3", "Hand-Carried Lamp"),
        ])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-1", "fp-2", "fp-3"])

        self.assertIsInstance(result, BulkApplyResult)
        self.assertEqual(result.requested, ("fp-1", "fp-2", "fp-3"))
        self.assertEqual(result.applied, ("fp-1", "fp-2", "fp-3"))
        self.assertEqual(result.failed, ())
        self.assertTrue(result.complete)
        # The WHOLE set of store writes, in order -- not a count, not one
        # sampled call.
        self.assertEqual(
            store.calls,
            [("applied", "fp-1", {"description": None}),
             ("applied", "fp-2", {"description": None}),
             ("applied", "fp-3", {"description": None})])
        self.assertEqual(
            stash.calls,
            [("apply", "1", "a description from a stash-box"),
             ("apply", "2", "a description from a stash-box"),
             ("apply", "3", "a description from a stash-box")])

    def test_a_row_not_in_the_submitted_set_is_left_untouched(self):
        # HARM: a filter re-evaluated between render and apply reaches rows
        # nobody looked at. `fp-2` qualifies for this action exactly as much
        # as `fp-1` does (same shape, same empty original) and must still be
        # left completely alone when only `fp-1` is submitted.
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-1", "1", "Lantern Work"),
            _bulk_tag_item("fp-2", "2", "Passenger Boat"),
        ])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(["fp-1"])

        self.assertEqual(result.applied, ("fp-1",))
        self.assertEqual(store.calls,
                         [("applied", "fp-1", {"description": None})])
        self.assertEqual(stash.calls,
                         [("apply", "1", "a description from a stash-box")])
        self.assertEqual(store.item("fp-2")["state"], "new")

    def test_an_unknown_fingerprint_is_reported_as_a_failure_not_a_crash(self):
        store = _FakeMultiItemStore(
            [_bulk_tag_item("fp-1", "1", "Lantern Work")])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-1", "fp-missing"])

        self.assertEqual(result.applied, ("fp-1",))
        self.assertEqual([f["fingerprint"] for f in result.failed],
                         ["fp-missing"])
        self.assertFalse(result.complete)


class BulkApplyTagDescriptionsGuard(unittest.TestCase):
    """The additive-only guard: the single most important check in the
    whole feature, so every one of its two conditions gets its own hostile
    test, each isolating exactly the one row that must be refused. Mutating
    either half of the guard (`item["subject_type"] != TAG_DESCRIPTION
    _SUBJECT` or `item["payload"]["original"]`) so everything qualifies
    must fail at least one test below."""

    def test_a_tag_already_carrying_a_description_is_refused_not_overwritten(self):
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-empty", "1", "Lantern Work"),
            _bulk_tag_item("fp-full", "2", "Passenger Boat",
                          description="somebody already wrote this"),
        ])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-empty", "fp-full"])

        self.assertEqual(result.applied, ("fp-empty",))
        self.assertEqual([f["fingerprint"] for f in result.failed],
                         ["fp-full"])
        # Only the empty one was ever handed to the media server.
        self.assertEqual(stash.calls,
                         [("apply", "1", "a description from a stash-box")])
        self.assertEqual(store.item("fp-full")["state"], "new")

    def test_a_scene_proposal_is_refused_and_never_reaches_the_scene_path(self):
        store = _FakeMultiItemStore(
            [_item(), _bulk_tag_item("fp-t", "1", "Lantern Work")])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-1", "fp-t"])

        self.assertEqual(result.applied, ("fp-t",))
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-1"])
        self.assertEqual(store.item("fp-1")["state"], "new")

    def test_a_tag_merge_proposal_is_refused_and_never_reaches_the_merge_path(self):
        merge_item = _merge_item()
        store = _FakeMultiItemStore(
            [merge_item, _bulk_tag_item("fp-t", "1", "Lantern Work")])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            [merge_item["fingerprint"], "fp-t"])

        self.assertEqual(result.applied, ("fp-t",))
        self.assertEqual([f["fingerprint"] for f in result.failed],
                         [merge_item["fingerprint"]])
        self.assertEqual(store.item(merge_item["fingerprint"])["state"], "new")

    def test_a_tag_deletion_proposal_is_refused_and_never_deletes_anything(self):
        # This IS "no bulk delete", exercised through this action: the guard
        # must refuse a `tag-unused` fingerprint outright. If it did not,
        # `_FakeMultiTagStash.delete_tag` above raises `AssertionError`
        # rather than deleting anything -- so a broken guard fails LOUDLY
        # here, not by quietly succeeding against a double that also knows
        # how to delete.
        store = _FakeMultiItemStore(
            [_hygiene_item("fp-h"), _bulk_tag_item("fp-t", "1", "Lantern Work")])
        stash = _FakeMultiTagStash()

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-h", "fp-t"])

        self.assertEqual(result.applied, ("fp-t",))
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-h"])
        self.assertEqual(store.item("fp-h")["state"], "new")


class BulkApplyPartialFailure(unittest.TestCase):
    def test_one_failing_write_is_reported_as_partial_not_success(self):
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-ok", "1", "Lantern Work"),
            _bulk_tag_item("fp-bad", "2", "Passenger Boat"),
        ])
        stash = _FakeMultiTagStash(raise_for={"2"})

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-ok", "fp-bad"])

        self.assertFalse(result.complete)
        self.assertEqual(result.applied, ("fp-ok",))
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0]["fingerprint"], "fp-bad")
        self.assertIn("could not apply", result.failed[0]["reason"])
        self.assertEqual(store.item("fp-ok")["state"], "applied")
        self.assertEqual(store.item("fp-bad")["state"], "failed")

    def test_every_other_row_is_still_attempted_after_one_failure(self):
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-bad", "1", "Lantern Work"),
            _bulk_tag_item("fp-ok", "2", "Passenger Boat"),
        ])
        stash = _FakeMultiTagStash(raise_for={"1"})

        result = Actions(store, stash).bulk_apply_tag_descriptions(
            ["fp-bad", "fp-ok"])

        self.assertEqual(result.applied, ("fp-ok",))
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-bad"])


# ===========================================================================
# batch_apply -- the general, ticked-selection sibling of
# bulk_apply_tag_descriptions above. See that section's own fixtures
# (`_item`, `_description_item`, `_bulk_tag_item`, `_merge_item`,
# `_hygiene_item`, `_FakeMultiItemStore`) for scene, performer-description,
# tag-description, tag-merge and tag-deletion proposals; only the
# reconciliation fixture and a wider stash double are new here.
# ===========================================================================


def _reconcile_item(fp="fp-r", **over):
    """One tag/performer-reconciliation item, built through
    `cronicled.performer_tags`'s own producer path rather than hand-written,
    so it cannot describe a payload shape nothing ever emits -- used in the
    batch tests below only to prove the subject-type guard refuses it,
    never to reconcile anything."""
    tag_row = {"id": "50", "name": "Rare Bird Society", "aliases": [],
              "description": None, "scene_count": 3}
    performer_row = {"id": "9", "name": "Rare Bird Society", "alias_list": []}
    matches = match_tag(tag_row, index_performers([performer_row]))
    built = reconcile_proposal(tag_row, matches, ["sc-1", "sc-2"],
                               folder="library")
    item = {"fingerprint": fp, "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "prior_state": None,
            "payload": built["payload"]}
    item.update(over)
    return item


class _FakeMultiKindStash:
    """A media server able to answer every apply path `batch_apply` may
    LEGITIMATELY reach -- scene, performer-description, tag-description --
    and nothing else. `merge_tags`/`delete_tag`/`reconcile_tag_to_performer`
    all raise on sight: the guard under test in the section below is exactly
    what must stop a merge/reconcile/deletion fingerprint from ever reaching
    them, and a double that answered them anyway could not tell a guard that
    fired from one that quietly let the write through -- the same reasoning
    `_FakeMultiTagStash` above applies to `bulk_apply_tag_descriptions`,
    widened here to the three subject types this wider action may touch.
    """

    def __init__(self, prior=None, tag_descriptions=None,
                performer_descriptions=None, fail_scene_ids=(),
                fail_tag_ids=()):
        self.calls = []
        self._prior = prior if prior is not None else {"title": "old"}
        self.tag_descriptions = dict(tag_descriptions or {})
        self.performer_descriptions = dict(performer_descriptions or {})
        self._fail_scene_ids = set(fail_scene_ids)
        self._fail_tag_ids = set(fail_tag_ids)

    def apply_scene(self, scene_id, match, drop_tag_ids=()):
        if scene_id in self._fail_scene_ids:
            raise RuntimeError("the media server refused scene %s" % scene_id)
        self.calls.append(("apply-scene", scene_id))
        return {"prior": self._prior}

    def tag_id_by_name(self, name):
        # No test built on this fake configures a marker -- see
        # `_FakeStash.tag_id_by_name` above for why this raises rather than
        # answering something plausible.
        raise AssertionError(
            "tag_id_by_name was called with no marker configured")

    def apply_performer_description(self, performer_id, description, *,
                                    expected):
        if self.performer_descriptions.get(performer_id) != expected:
            raise RuntimeError(
                "performer %s's description is not the text this proposal "
                "was made from" % performer_id)
        prior = self.performer_descriptions.get(performer_id)
        self.performer_descriptions[performer_id] = description
        self.calls.append(("apply-performer", performer_id, description))
        return {"prior": {"details": prior}}

    def apply_tag_description(self, tag_id, description, *, expected):
        if tag_id in self._fail_tag_ids:
            raise RuntimeError("the media server refused tag %s" % tag_id)
        if self.tag_descriptions.get(tag_id) != expected:
            raise RuntimeError(
                "tag %s's description is not the text this proposal was "
                "made against" % tag_id)
        prior = self.tag_descriptions.get(tag_id)
        self.tag_descriptions[tag_id] = description
        self.calls.append(("apply-tag", tag_id, description))
        return {"prior": {"description": prior}}

    def merge_tags(self, *args, **kwargs):
        raise AssertionError(
            "a batch verdict reached the tag-merge path -- the subject-type "
            "guard must refuse a tag-cluster fingerprint before this")

    def delete_tag(self, *args, **kwargs):
        raise AssertionError(
            "a batch verdict reached the tag-delete path -- this is exactly "
            "what 'no bulk delete' must never allow")

    def reconcile_tag_to_performer(self, *args, **kwargs):
        raise AssertionError(
            "a batch verdict reached the reconcile path -- the subject-type "
            "guard must refuse a tag-performer fingerprint before this")


class BatchApplyExplicitSelection(unittest.TestCase):
    """Acceptance: the set `batch_apply` acts on is exactly the fingerprints
    submitted, in the order submitted, whichever verdict -- never a filter
    re-evaluated against the store. Assert the WHOLE set of store/media-server
    calls, never a count and never a sampled member: this project has had an
    unlisted field slip past a field-by-field assertion before, and in a
    module like this one that would mean a row nobody ticked getting
    written."""

    def test_approve_writes_exactly_the_submitted_mixed_batch_and_nothing_else(self):
        scene = _item(fingerprint="fp-scene")
        description = _description_item()
        tag = _bulk_tag_item("fp-tag", "1", "Lantern Work")
        untouched_tag = _bulk_tag_item("fp-other-tag", "2", "Passenger Boat")
        store = _FakeMultiItemStore([scene, description, tag, untouched_tag])
        stash = _FakeMultiKindStash(
            performer_descriptions={"7": "<p>Before.</p>"})

        result = Actions(store, stash).batch_apply(
            "approve", ["fp-scene", "fp-d", "fp-tag"])

        self.assertIsInstance(result, BatchResult)
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(result.requested, ("fp-scene", "fp-d", "fp-tag"))
        self.assertEqual(result.applied, ("fp-scene", "fp-d", "fp-tag"))
        self.assertEqual(result.failed, ())
        self.assertTrue(result.complete)
        # The WHOLE set of writes the media server actually received, in
        # order -- never a count, never one sampled call.
        self.assertEqual(
            stash.calls,
            [("apply-scene", "42"),
             ("apply-performer", "7", "Before."),
             ("apply-tag", "1", "a description from a stash-box")])
        # The row never named in the submitted set is untouched.
        self.assertEqual(store.item("fp-other-tag")["state"], "new")

    def test_a_row_not_in_the_submitted_set_is_left_completely_untouched(self):
        # HARM: a filter re-evaluated between render and apply reaches rows
        # nobody ticked. `fp-b` qualifies for `dismiss` exactly as much as
        # `fp-a` does and must stay exactly as it was when only `fp-a` is
        # submitted.
        store = _FakeMultiItemStore(
            [_item(fingerprint="fp-a"),
             _item(fingerprint="fp-b", subject_id="43")])

        result = Actions(store, _FakeStash()).batch_apply("dismiss", ["fp-a"])

        self.assertEqual(result.applied, ("fp-a",))
        self.assertEqual(
            store.calls, [("dismissed", "fp-a", "dismissed from the inbox")])
        self.assertEqual(store.item("fp-b")["state"], "new")

    def test_an_unknown_fingerprint_is_reported_as_a_failure_not_a_crash(self):
        store = _FakeMultiItemStore([_item(fingerprint="fp-a")])

        result = Actions(store, _FakeStash()).batch_apply(
            "mute", ["fp-a", "fp-missing"])

        self.assertEqual(result.applied, ("fp-a",))
        self.assertEqual([f["fingerprint"] for f in result.failed],
                         ["fp-missing"])
        self.assertFalse(result.complete)


class BatchApplySubjectTypeGuard(unittest.TestCase):
    """The subject-type guard: the single most important check in this
    feature. Without it, a crafted (or merely stale) fingerprint could ride
    a batch call into `approve`'s own merge/reconcile/delete dispatch --
    exactly the three writes this project chose never to offer in bulk. Each
    of the three gets its own hostile test isolating exactly the one row
    that must be refused, mirroring `BulkApplyTagDescriptionsGuard` above.
    Mutating the guard (`item["subject_type"] not in _BATCH_SUBJECT_TYPES`)
    so every subject type qualifies must fail every test below."""

    def test_a_tag_merge_fingerprint_is_refused_and_never_reaches_the_merge_path(self):
        merge_item = _merge_item()
        store = _FakeMultiItemStore(
            [merge_item, _bulk_tag_item("fp-t", "1", "Lantern Work")])
        stash = _FakeMultiKindStash()

        result = Actions(store, stash).batch_apply(
            "approve", [merge_item["fingerprint"], "fp-t"])

        self.assertEqual(result.applied, ("fp-t",))
        self.assertEqual([f["fingerprint"] for f in result.failed],
                         [merge_item["fingerprint"]])
        self.assertEqual(store.item(merge_item["fingerprint"])["state"], "new")

    def test_a_reconcile_fingerprint_is_refused_and_never_reaches_the_reconcile_path(self):
        reconcile_item = _reconcile_item()
        store = _FakeMultiItemStore(
            [reconcile_item, _bulk_tag_item("fp-t", "1", "Lantern Work")])
        stash = _FakeMultiKindStash()

        result = Actions(store, stash).batch_apply(
            "approve", [reconcile_item["fingerprint"], "fp-t"])

        self.assertEqual(result.applied, ("fp-t",))
        self.assertEqual([f["fingerprint"] for f in result.failed],
                         [reconcile_item["fingerprint"]])
        self.assertEqual(
            store.item(reconcile_item["fingerprint"])["state"], "new")

    def test_a_tag_deletion_fingerprint_is_refused_and_never_deletes_anything(self):
        # This IS "no bulk delete", exercised through this action: if the
        # guard did not refuse it, `_FakeMultiKindStash.delete_tag` above
        # raises `AssertionError` rather than deleting anything -- so a
        # broken guard fails LOUDLY here, not by quietly succeeding against
        # a double that also knows how to delete.
        store = _FakeMultiItemStore(
            [_hygiene_item("fp-h"), _bulk_tag_item("fp-t", "1", "Lantern Work")])
        stash = _FakeMultiKindStash()

        result = Actions(store, stash).batch_apply("approve", ["fp-h", "fp-t"])

        self.assertEqual(result.applied, ("fp-t",))
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-h"])
        self.assertEqual(store.item("fp-h")["state"], "new")

    def test_the_guard_refuses_a_disallowed_subject_type_for_every_verdict(self):
        # Not only `approve`: `dismiss`/`mute`/`refresh` reaching a subject
        # type this action may never touch must be refused on the same
        # terms, not merely happen to be harmless for those three because
        # nothing else caught it.
        for verdict in ("dismiss", "mute", "refresh"):
            with self.subTest(verdict=verdict):
                store = _FakeMultiItemStore([_hygiene_item("fp-h")])

                result = Actions(store, _FakeStash()).batch_apply(
                    verdict, ["fp-h"])

                self.assertEqual(result.applied, ())
                self.assertEqual(
                    [f["fingerprint"] for f in result.failed], ["fp-h"])
                self.assertEqual(store.calls, [])
                self.assertEqual(store.item("fp-h")["state"], "new")


class BatchApplyAppliedStateGuard(unittest.TestCase):
    """`approve`/`dismiss`/`mute` refuse a row already `applied`; `refresh`
    is deliberately exempt (ticket 86: an applied row has no other way off
    the block it leaves in `scan.select`). Both halves of that asymmetry are
    pinned -- testing only the restrictive side would miss a mutation that
    widened the exemption to every verdict, or narrowed it to none, the same
    "only one side of a guard is pinned" gap this project has been caught by
    before."""

    def test_approve_refuses_an_already_applied_row(self):
        store = _FakeMultiItemStore(
            [_item(fingerprint="fp-a", state="applied",
                  prior_state={"title": "was"})])

        result = Actions(store, _FakeStash()).batch_apply("approve", ["fp-a"])

        self.assertEqual(result.applied, ())
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-a"])
        self.assertIn("already applied", result.failed[0]["reason"])

    def test_dismiss_refuses_an_already_applied_row(self):
        store = _FakeMultiItemStore(
            [_item(fingerprint="fp-a", state="applied",
                  prior_state={"title": "was"})])

        result = Actions(store, _FakeStash()).batch_apply("dismiss", ["fp-a"])

        self.assertEqual(result.applied, ())
        self.assertEqual(store.calls, [])

    def test_mute_refuses_an_already_applied_row(self):
        store = _FakeMultiItemStore(
            [_item(fingerprint="fp-a", state="applied",
                  prior_state={"title": "was"})])

        result = Actions(store, _FakeStash()).batch_apply("mute", ["fp-a"])

        self.assertEqual(result.applied, ())
        self.assertEqual(store.calls, [])

    def test_refresh_still_reaches_an_already_applied_row(self):
        # The one case ticket 86 exists for. If this fell to the same
        # exclusion as the other three, an applied row would be left with no
        # way off the block it leaves in `scan.select` -- exactly the dead
        # end that ticket closed for a single row.
        store = _FakeMultiItemStore(
            [_item(fingerprint="fp-a", state="applied",
                  prior_state={"title": "was"})])

        result = Actions(store, _FakeStash()).batch_apply("refresh", ["fp-a"])

        self.assertEqual(result.applied, ("fp-a",))
        self.assertEqual(store.calls, [("superseded", "fp-a")])


class BatchApplyPartialFailure(unittest.TestCase):
    def test_one_failing_approve_is_reported_as_partial_not_success(self):
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-ok", "1", "Lantern Work"),
            _bulk_tag_item("fp-bad", "2", "Passenger Boat"),
        ])
        stash = _FakeMultiKindStash(fail_tag_ids={"2"})

        result = Actions(store, stash).batch_apply(
            "approve", ["fp-ok", "fp-bad"])

        self.assertFalse(result.complete)
        self.assertEqual(result.applied, ("fp-ok",))
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0]["fingerprint"], "fp-bad")
        self.assertIn("could not apply", result.failed[0]["reason"])
        self.assertEqual(store.item("fp-ok")["state"], "applied")
        self.assertEqual(store.item("fp-bad")["state"], "failed")

    def test_every_other_row_is_still_attempted_after_one_failure(self):
        store = _FakeMultiItemStore([
            _bulk_tag_item("fp-bad", "1", "Lantern Work"),
            _bulk_tag_item("fp-ok", "2", "Passenger Boat"),
        ])
        stash = _FakeMultiKindStash(fail_tag_ids={"1"})

        result = Actions(store, stash).batch_apply(
            "approve", ["fp-bad", "fp-ok"])

        self.assertEqual(result.applied, ("fp-ok",))
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-bad"])

    def test_an_unexpected_exception_from_a_single_row_does_not_abort_the_batch(self):
        # Neither `dismiss` nor `mute` is documented to raise anything but
        # `UnknownProposal` -- this proves the batch survives one that does
        # anyway, rather than only ever exercising the two documented,
        # narrower failure paths.
        class _ExplodingStore(_FakeMultiItemStore):
            def mute(self, subject_type, subject_id, reason=None):
                if subject_id == "explode":
                    raise RuntimeError("the database vanished")
                super().mute(subject_type, subject_id, reason=reason)

        store = _ExplodingStore([
            _item(fingerprint="fp-bad", subject_id="explode"),
            _item(fingerprint="fp-ok", subject_id="43"),
        ])

        result = Actions(store, _FakeStash()).batch_apply(
            "mute", ["fp-bad", "fp-ok"])

        self.assertEqual(result.applied, ("fp-ok",))
        self.assertEqual([f["fingerprint"] for f in result.failed], ["fp-bad"])
        self.assertIn("RuntimeError", result.failed[0]["reason"])


class BatchApplyUnknownVerdict(unittest.TestCase):
    def test_an_unrecognised_verdict_raises_rather_than_silently_doing_nothing(self):
        store = _FakeMultiItemStore([_item()])
        with self.assertRaises(ValueError):
            Actions(store, _FakeStash()).batch_apply("delete", ["fp-1"])


# -- the marker tag: approve takes it off, nothing else does --------------- #
#
# These go through the REAL `Stash.apply_scene`/`revert_scene`, not
# `_FakeStash`, because the acceptance this ticket cares about most --
# "assert the whole set of tags written, not that the marker is absent" --
# is a claim about what `Stash` actually puts in a sceneUpdate's `tag_ids`
# once `Actions.approve` hands it a `drop_tag_ids`. A hand-rolled double for
# `apply_scene` could only ever echo back whatever this suite decided to put
# in it; the real merge/drop logic is what could genuinely blank every tag
# instead of the one meant to go, and that logic already has its own direct
# tests in test_stash.py -- what is untested until now is that `approve`
# actually reaches it with the right id.

MARKER = "needs review"


class _MarkerAwareTransport:
    """Fake GraphQL transport serving exactly the three operations these
    tests need: `findScene` (the one read `apply_scene` takes its snapshot
    from), `findTags(tag_filter:...)` -- the shape `Stash.tag_id_by_name`
    queries with, distinct from the `findTags(filter:...)` find-or-create
    shape `tests/test_stash.py`'s own fakes already answer -- and
    `sceneUpdate`. Every fixture scene here carries no performers and no
    proposal here names a tag to ADD, so `apply_scene`'s find-or-create path
    is never exercised and does not need modelling.

    Records the LAST `sceneUpdate` input verbatim (`scene_update_input`) and
    every tag name asked about (`tag_lookups`), so a test can assert on
    both what was written and whether the marker was even looked up.
    """

    def __init__(self, existing_tags, tag_registry, fail_lookup=False,
                fail_write=False):
        self.existing = {
            "id": "42", "title": "T", "details": None, "date": None,
            "urls": [], "organized": False, "rating100": None,
            "code": None, "director": None, "stash_ids": [],
            "studio": None, "performers": [],
            "tags": [dict(t) for t in existing_tags],
        }
        self._tag_registry = dict(tag_registry)
        self._fail_lookup = fail_lookup
        self._fail_write = fail_write
        self.scene_update_input = None
        self.tag_lookups = []

    def __call__(self, body, timeout):
        q = body["query"]
        if "tag_filter" in q:
            if self._fail_lookup:
                return {"errors": [{"message": "tag lookup exploded"}]}
            name = body["variables"]["f"]["name"]["value"]
            self.tag_lookups.append(name)
            tag_id = self._tag_registry.get(name)
            rows = ([{"id": tag_id, "name": name, "scene_count": 0}]
                    if tag_id is not None else [])
            return {"data": {"findTags": {"tags": rows}}}
        if "findScene(" in q:
            return {"data": {"findScene": self.existing}}
        if "sceneUpdate" in q:
            if self._fail_write:
                return {"errors": [{"message": "the server refused the write"}]}
            self.scene_update_input = body["variables"]["in"]
            return {"data": {"sceneUpdate":
                             {"id": self.scene_update_input["id"]}}}
        raise AssertionError(
            "marker test transport does not recognize query: %s" % q)


class _MarkerGuardStash:
    """Raises on any write or lookup a marker-removal mutation could reach
    for -- used to prove a verdict that must never touch the marker really
    does not reach the stash at all, rather than merely producing no
    OBSERVABLE difference today. A mutation that added a marker-removal call
    to `dismiss`/`mute`/`refresh` would hit one of these and fail loudly
    instead of leaving the row's own state untouched by coincidence."""

    def apply_scene(self, *args, **kwargs):
        raise AssertionError(
            "this verdict must never write to the media server at all")

    def tag_id_by_name(self, name):
        raise AssertionError(
            "this verdict must never look up the marker tag at all")


class ApproveRemovesTheMarkerTag(unittest.TestCase):
    def test_approve_drops_only_the_marker_from_the_written_tag_set(self):
        transport = _MarkerAwareTransport(
            existing_tags=[{"id": "77", "name": MARKER},
                          {"id": "5", "name": "Keep This One"}],
            tag_registry={MARKER: "77"})
        stash = Stash("http://example.test", "k", transport=transport)
        store = _FakeStore(_item(subject_id="42"))

        Actions(store, stash, marker=MARKER).approve("fp-1")

        # The WHOLE set actually written, not merely that the marker's id is
        # missing from it -- an apply that dropped every tag from the scene
        # would also satisfy "the marker is gone", and be the far worse bug.
        self.assertEqual(transport.scene_update_input["tag_ids"], ["5"])

    def test_a_scene_not_carrying_the_marker_writes_its_tags_unchanged(self):
        # Nothing to drop: the marker tag exists on the server but this
        # scene never had it, so the write must look exactly as it would
        # with no marker configured at all.
        transport = _MarkerAwareTransport(
            existing_tags=[{"id": "5", "name": "Keep This One"}],
            tag_registry={MARKER: "77"})
        stash = Stash("http://example.test", "k", transport=transport)
        store = _FakeStore(_item(subject_id="42"))

        Actions(store, stash, marker=MARKER).approve("fp-1")

        self.assertNotIn("tag_ids", transport.scene_update_input or {})

    def test_a_failed_tag_lookup_fails_the_whole_apply(self):
        # New code this ticket adds: resolving the marker's id is itself a
        # network call, and its failure must read exactly like any other
        # apply failure -- recorded failed, never applied, and the scene
        # never written to at all.
        transport = _MarkerAwareTransport(
            existing_tags=[{"id": "77", "name": MARKER}],
            tag_registry={MARKER: "77"}, fail_lookup=True)
        stash = Stash("http://example.test", "k", transport=transport)
        store = _FakeStore(_item(subject_id="42"))

        with self.assertRaises(ApplyFailed):
            Actions(store, stash, marker=MARKER).approve("fp-1")

        self.assertEqual([c[0] for c in store.calls], ["failed"])
        self.assertIsNone(transport.scene_update_input)

    def test_a_failed_write_is_not_reported_as_a_successful_apply(self):
        # The marker's removal travels in the SAME sceneUpdate as the
        # metadata -- there is no separate "now remove the tag" call to
        # half-fail. A failure of that one write must still read as a
        # failed apply, not as a success that quietly kept the marker.
        transport = _MarkerAwareTransport(
            existing_tags=[{"id": "77", "name": MARKER},
                          {"id": "5", "name": "Keep This One"}],
            tag_registry={MARKER: "77"}, fail_write=True)
        stash = Stash("http://example.test", "k", transport=transport)
        store = _FakeStore(_item(subject_id="42"))

        with self.assertRaises(ApplyFailed):
            Actions(store, stash, marker=MARKER).approve("fp-1")

        self.assertEqual([c[0] for c in store.calls], ["failed"])


class NoMarkerConfiguredApproveIsUnaffected(unittest.TestCase):
    def test_approve_never_looks_up_or_drops_anything(self):
        # Acceptance: a deployment with no marker configured (the default --
        # `Actions(store, stash)` names none) behaves exactly as it did
        # before this ticket. A mutation that reached for the marker tag
        # regardless of configuration would either call
        # `tag_id_by_name` (recorded in `tag_lookups`) or write a `tag_ids`
        # array this scene never asked for -- either is caught below.
        transport = _MarkerAwareTransport(
            existing_tags=[{"id": "77", "name": MARKER},
                          {"id": "5", "name": "Keep This One"}],
            tag_registry={MARKER: "77"})
        stash = Stash("http://example.test", "k", transport=transport)
        store = _FakeStore(_item(subject_id="42"))

        Actions(store, stash).approve("fp-1")

        self.assertEqual(transport.tag_lookups, [])
        self.assertNotIn("tag_ids", transport.scene_update_input or {})


class DismissMuteAndRefreshLeaveTheMarkerAlone(unittest.TestCase):
    """Acceptance: only approve removes the marker. A dismissal is "not this
    candidate", not "this file is settled"; mute and refresh mean something
    else again -- see this ticket's own brief for why dropping the marker on
    any of the three would hide a file from every future pass. Each test
    uses `_MarkerGuardStash`, which raises if the verdict under test reaches
    for the stash at all, so a mutation that added marker-removal to one of
    these fails loudly rather than passing because today's code happens to
    produce no visible difference."""

    def test_dismiss_never_touches_the_stash(self):
        store = _FakeStore(_item(subject_id="42"))
        Actions(store, _MarkerGuardStash(), marker=MARKER).dismiss("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["dismissed"])

    def test_mute_never_touches_the_stash(self):
        store = _FakeStore(_item(subject_id="42"))
        Actions(store, _MarkerGuardStash(), marker=MARKER).mute("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["muted"])

    def test_refresh_never_touches_the_stash(self):
        store = _FakeStore(_item(subject_id="42"))
        Actions(store, _MarkerGuardStash(), marker=MARKER).refresh("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["superseded"])


class UndoRestoresTheMarkerTag(unittest.TestCase):
    def test_undoing_an_approve_puts_the_marker_back_on_the_scene(self):
        transport = _MarkerAwareTransport(
            existing_tags=[{"id": "77", "name": MARKER},
                          {"id": "5", "name": "Keep This One"}],
            tag_registry={MARKER: "77"})
        stash = Stash("http://example.test", "k", transport=transport)
        approve_store = _FakeStore(_item(subject_id="42"))

        Actions(approve_store, stash, marker=MARKER).approve("fp-1")

        # The approve itself dropped the marker from what was WRITTEN...
        self.assertEqual(transport.scene_update_input["tag_ids"], ["5"])
        # ...but the snapshot recorded for undo is built from the read
        # taken BEFORE the drop, so it still names the marker. That is what
        # makes the undo below possible at all -- if a future change moved
        # the snapshot to be taken after the drop, this is where it would
        # be caught, before undo ever got a chance to fail.
        prior = next(c[2] for c in approve_store.calls if c[0] == "applied")
        self.assertEqual(sorted(prior["tag_ids"]), ["5", "77"])

        undo_store = _FakeStore(
            _item(subject_id="42", state="applied", prior_state=prior))
        Actions(undo_store, stash, marker=MARKER).undo("fp-1")

        # The whole set restored, marker included -- not merely "the marker
        # is present somewhere", which a partial restore could also satisfy.
        self.assertEqual(sorted(transport.scene_update_input["tag_ids"]),
                         ["5", "77"])
