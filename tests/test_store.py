"""The store keeps proposed changes between runs. Its fingerprint is what stops a
nightly producer from turning the inbox into noise on its second night."""
import gc
import json
import os
import shutil
import sqlite3
import tempfile
import unicodedata
import unittest
import uuid
from datetime import datetime, timezone

from cronicled.store import (RUN_HISTORY_LIMIT, RUN_OUTCOME_INTERRUPTED,
                             RUN_TRIGGERS, SCHEMA_VERSION, SchemaVersionError,
                             Store, fingerprint)


class Fingerprint(unittest.TestCase):
    def test_key_order_does_not_change_the_fingerprint(self):
        # payloads are built by producers in whatever order is convenient; two
        # semantically identical payloads must be one proposal, not two
        a = fingerprint("f", "scene", "1", {"title": "Copper Kettle", "date": "2024-01-01"})
        b = fingerprint("f", "scene", "1", {"date": "2024-01-01", "title": "Copper Kettle"})
        self.assertEqual(a, b)

    def test_nested_key_order_does_not_change_it_either(self):
        a = fingerprint("f", "scene", "1", {"x": {"a": 1, "b": 2}})
        b = fingerprint("f", "scene", "1", {"x": {"b": 2, "a": 1}})
        self.assertEqual(a, b)

    def test_a_changed_value_changes_the_fingerprint(self):
        a = fingerprint("f", "scene", "1", {"title": "Copper Kettle"})
        b = fingerprint("f", "scene", "1", {"title": "Harbour Lights"})
        self.assertNotEqual(a, b)

    def test_a_different_subject_changes_it(self):
        a = fingerprint("f", "scene", "1", {"title": "Copper Kettle"})
        b = fingerprint("f", "scene", "2", {"title": "Copper Kettle"})
        self.assertNotEqual(a, b)

    def test_a_different_folder_changes_it(self):
        a = fingerprint("f", "scene", "1", {"title": "Copper Kettle"})
        b = fingerprint("g", "scene", "1", {"title": "Copper Kettle"})
        self.assertNotEqual(a, b)

    def test_composed_and_decomposed_unicode_hash_identically(self):
        # a filesystem commonly hands back a decomposed form while a title
        # from an API is composed; those are the same human-identical title
        composed = "café"                        # "é" as one codepoint
        decomposed = unicodedata.normalize("NFD", composed)  # "e" + combining acute
        self.assertNotEqual(composed, decomposed)     # sanity: genuinely different strings
        a = fingerprint("f", "scene", "1", {"title": composed})
        b = fingerprint("f", "scene", "1", {"title": decomposed})
        self.assertEqual(a, b)

    def test_composed_and_decomposed_unicode_hash_identically_when_nested(self):
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)
        a = fingerprint("f", "scene", "1", {"x": {"title": composed}})
        b = fingerprint("f", "scene", "1", {"x": {"title": decomposed}})
        self.assertEqual(a, b)

    def test_int_and_float_of_the_same_value_are_different_fingerprints(self):
        # numeric type is not coerced: collapsing 1 and 1.0 would mean
        # guessing they're the same logical value in an opaque payload
        a = fingerprint("f", "scene", "1", {"n": 1})
        b = fingerprint("f", "scene", "1", {"n": 1.0})
        self.assertNotEqual(a, b)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._dir, "s.db"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _record(self, subject_id="1", payload=None, folder="scene-matches"):
        return self.store.record(folder=folder, subject_type="scene",
                                 subject_id=subject_id, summary="a proposal",
                                 payload=payload or {"title": "Copper Kettle"},
                                 producer="test-producer", confidence=0.9)


class Recording(_StoreCase):
    def test_a_recorded_proposal_can_be_read_back(self):
        fp = self._record()
        items = self.store.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fingerprint"], fp)
        self.assertEqual(items[0]["state"], "new")
        self.assertEqual(items[0]["payload"], {"title": "Copper Kettle"})

    def test_recording_the_same_proposal_twice_keeps_one_row(self):
        # this is the property the nightly producer depends on
        self._record(); self._record()
        self.assertEqual(len(self.store.items()), 1)

    def test_recording_again_touches_last_seen(self):
        fp = self._record()
        first = self.store.items()[0]["last_seen_at"]
        self.store.record(folder="scene-matches", subject_type="scene",
                          subject_id="1", summary="a proposal",
                          payload={"title": "Copper Kettle"},
                          producer="test-producer", confidence=0.9,
                          now="2099-01-01T00:00:00")
        self.assertNotEqual(self.store.items()[0]["last_seen_at"], first)

    def test_a_changed_payload_is_a_different_proposal(self):
        self._record(payload={"title": "Copper Kettle"})
        self._record(payload={"title": "Harbour Lights"})
        self.assertEqual(len(self.store.items()), 2)

    def test_payload_survives_a_round_trip_unchanged(self):
        payload = {"a": [1, 2, {"b": None}], "c": True, "d": 1.5}
        self._record(payload=payload)
        self.assertEqual(self.store.items()[0]["payload"], payload)


class ReviewerDecisionsSurvive(_StoreCase):
    def test_a_scheduled_rerun_does_not_reset_seen(self):
        # a reviewer's progress is real work; a nightly producer must not undo it
        fp = self._record()
        self.store.mark_seen(fp)
        self._record()                      # the producer runs again
        self.assertEqual(self.store.items()[0]["state"], "seen")

    def test_a_dismissed_proposal_does_not_come_back(self):
        fp = self._record()
        self.store.dismiss(fp, reason="wrong match")
        self._record()                      # the producer offers it again
        self.assertEqual(self.store.items(), [])

    def test_muting_a_subject_blocks_future_proposals_about_it(self):
        self.store.mute("scene", "1", reason="never identifiable")
        self._record(subject_id="1")
        self.assertEqual(self.store.items(), [])

    def test_muting_one_subject_does_not_block_another(self):
        self.store.mute("scene", "1")
        self._record(subject_id="2")
        self.assertEqual(len(self.store.items()), 1)

    def test_dismissing_one_proposal_leaves_a_better_one_possible(self):
        # dismiss rejects THIS proposal, not the subject
        first = self._record(payload={"title": "Copper Kettle"})
        self.store.dismiss(first)
        self._record(payload={"title": "Harbour Lights"})
        self.assertEqual(len(self.store.items()), 1)


class RejectedRowsPersist(_StoreCase):
    # dismissed/muted are STATES, not deletions: a reviewer needs to see
    # what they rejected, undo it, or audit it later
    def test_a_dismissed_proposal_is_still_retrievable_with_its_content(self):
        fp = self._record(payload={"title": "Copper Kettle"})
        self.store.dismiss(fp, reason="wrong match")
        items = self.store.items(state="dismissed")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fingerprint"], fp)
        self.assertEqual(items[0]["summary"], "a proposal")
        self.assertEqual(items[0]["payload"], {"title": "Copper Kettle"})

    def test_items_with_no_filter_excludes_dismissed_and_muted(self):
        dismissed_fp = self._record(subject_id="1")
        self.store.dismiss(dismissed_fp)
        self._record(subject_id="2")
        self.store.mute("scene", "2")          # marks the existing row muted
        self.assertEqual(self.store.items(), [])

    def test_counts_excludes_dismissed_and_muted(self):
        dismissed_fp = self._record(subject_id="1")
        self.store.dismiss(dismissed_fp)
        self._record(subject_id="2")
        self.assertEqual(self.store.counts(), {"new": 1})

    def test_muting_a_subject_with_no_existing_item_still_blocks_future_records(self):
        # proves the mute table blocks by itself, independent of any row state
        self.store.mute("scene", "99", reason="never identifiable")
        self._record(subject_id="99")
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.store.counts(), {})

    def test_rerecording_a_dismissed_proposal_does_not_resurrect_or_reset_state(self):
        fp = self._record()
        self.store.dismiss(fp, reason="wrong match")
        self._record()                      # the producer offers it again
        self.assertEqual(self.store.items(), [])
        dismissed = self.store.items(state="dismissed")
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["fingerprint"], fp)
        self.assertEqual(dismissed[0]["state"], "dismissed")


class States(_StoreCase):
    def test_applied_records_the_undo_snapshot_and_a_resolution_time(self):
        fp = self._record()
        self.store.mark_applied(fp, prior_state={"title": "Old Title"})
        item = self.store.items(state="applied")[0]
        self.assertEqual(item["prior_state"], {"title": "Old Title"})
        self.assertIsNotNone(item["resolved_at"])

    def test_failed_records_why(self):
        fp = self._record()
        self.store.mark_failed(fp, error="server refused the name")
        item = self.store.items(state="failed")[0]
        self.assertIn("refused", item["error"])

    def test_marking_an_unknown_fingerprint_raises(self):
        # silently doing nothing would hide a real bug in a caller
        with self.assertRaises(KeyError):
            self.store.mark_seen("nosuchfingerprint")


class RejectionDoesNotOverwriteATerminalResolution(_StoreCase):
    # applied/failed plus resolved_at record that a real change already
    # happened (or was attempted) and when; a later dismiss/mute must not
    # erase that just because a reviewer also rejects the fingerprint/subject
    def test_dismissing_an_applied_proposal_preserves_its_state_and_timestamp(self):
        fp = self._record()
        self.store.mark_applied(fp, prior_state={"title": "Old Title"}, now="2020-01-01T00:00:00")
        before = self.store.items(state="applied")[0]

        self.store.dismiss(fp, reason="too late, already applied", now="2020-06-01T00:00:00")

        after = self.store.items(state="applied")[0]
        self.assertEqual(after["state"], "applied")
        self.assertEqual(after["resolved_at"], before["resolved_at"])
        self.assertEqual(after["prior_state"], {"title": "Old Title"})
        # the rejection still stuck: re-recording does not create a new row
        # or move this one back to "new"
        self._record()
        self.assertEqual(len(self.store.items(state="applied")), 1)
        self.assertEqual(len(self.store.items(state="new")), 0)

    def test_muting_a_failed_proposals_subject_preserves_its_state_and_timestamp(self):
        fp = self._record(subject_id="7")
        self.store.mark_failed(fp, error="server refused the name", now="2020-01-01T00:00:00")
        before = self.store.items(state="failed")[0]

        self.store.mute("scene", "7", reason="never identifiable", now="2020-06-01T00:00:00")

        after = self.store.items(state="failed")[0]
        self.assertEqual(after["state"], "failed")
        self.assertEqual(after["resolved_at"], before["resolved_at"])
        self.assertEqual(after["error"], "server refused the name")
        # the mute still blocks a later proposal for the same subject
        self._record(subject_id="7")
        self.assertEqual(len(self.store.items(state="failed")), 1)
        self.assertEqual(len(self.store.items(state="new")), 0)


class Superseding(_StoreCase):
    """Ticket 86: an explicit way to retire a stale proposal and free its
    file for the next scan, without recording a rejection nobody made."""

    def test_a_new_proposal_moves_to_superseded_and_is_hidden_by_default(self):
        fp = self._record()
        self.store.supersede(fp)
        self.assertEqual(self.store.items(), [])
        superseded = self.store.items(state="superseded")
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["fingerprint"], fp)
        self.assertEqual(superseded[0]["state"], "superseded")

    def test_superseding_does_not_record_a_dismissal(self):
        fp = self._record()
        self.store.supersede(fp)
        self.assertEqual(self.store.items(state="dismissed"), [])
        self.assertEqual(self.store.items(state="superseded")[0]["state"],
                         "superseded")

    def test_superseding_does_not_block_a_rerecord_of_the_same_payload(self):
        # The clearest proof this is not a dismissal in disguise: `record()`
        # checks the `dismissal` table before writing, so if superseding
        # routed through `dismiss` internally, re-recording the IDENTICAL
        # payload below would be silently dropped -- exactly the block a
        # dismissal exists to apply, and precisely the decision this action
        # must never make on a person's behalf. `last_seen_at` only ever
        # changes on a genuine upsert, so it is what proves the write
        # actually happened rather than being swallowed.
        payload = {"title": "Copper Kettle"}
        fp = self._record(payload=payload)
        self.store.supersede(fp)
        before = self.store.items(state="superseded")[0]["last_seen_at"]

        again = self.store.record(
            folder="scene-matches", subject_type="scene", subject_id="1",
            summary="a proposal", payload=payload, producer="test-producer",
            confidence=0.9, now="2099-01-01T00:00:00")

        self.assertEqual(again, fp)
        after = self.store.items(state="superseded")[0]["last_seen_at"]
        self.assertNotEqual(after, before)

    def test_superseding_an_applied_proposal_leaves_its_state_and_snapshot_alone(self):
        fp = self._record()
        self.store.mark_applied(fp, prior_state={"title": "Old Title"},
                                now="2020-01-01T00:00:00")
        before = self.store.items(state="applied")[0]

        self.store.supersede(fp, now="2020-06-01T00:00:00")

        after = self.store.items(state="applied")[0]
        self.assertEqual(after["state"], "applied")
        self.assertEqual(after["resolved_at"], before["resolved_at"])
        self.assertEqual(after["prior_state"], {"title": "Old Title"})

    def test_superseding_a_failed_proposal_leaves_its_state_and_error_alone(self):
        fp = self._record(subject_id="7")
        self.store.mark_failed(fp, error="server refused the name",
                               now="2020-01-01T00:00:00")
        before = self.store.items(state="failed")[0]

        self.store.supersede(fp, now="2020-06-01T00:00:00")

        after = self.store.items(state="failed")[0]
        self.assertEqual(after["state"], "failed")
        self.assertEqual(after["resolved_at"], before["resolved_at"])
        self.assertEqual(after["error"], "server refused the name")

    def test_an_applied_proposal_is_freed_in_the_supersede_table_despite_its_state(self):
        # This is the case `item.state` alone can never answer: an applied
        # row's own state never changes (see the test above), so the ONLY
        # place a scan can learn its file is free again is this table.
        fp = self._record(subject_id="7")
        self.store.mark_applied(fp, prior_state={"title": "x"})
        self.assertEqual(self.store.superseded_fingerprints(), set())

        self.store.supersede(fp)

        self.assertEqual(self.store.superseded_fingerprints(), {fp})

    def test_superseding_an_unknown_fingerprint_raises(self):
        # Unlike dismiss/mute, there is no "pre-emptive supersede" -- this
        # describes something happening to an already-recorded proposal.
        with self.assertRaises(KeyError):
            self.store.supersede("nosuchfingerprint")

    def test_nothing_superseded_is_an_empty_set(self):
        self.assertEqual(self.store.superseded_fingerprints(), set())


class ConfidenceValidation(_StoreCase):
    def test_confidence_above_one_raises(self):
        with self.assertRaises(ValueError):
            self.store.record(folder="scene-matches", subject_type="scene",
                              subject_id="1", summary="a proposal",
                              payload={"title": "x"}, producer="test-producer",
                              confidence=57.3)

    def test_confidence_below_zero_raises(self):
        with self.assertRaises(ValueError):
            self.store.record(folder="scene-matches", subject_type="scene",
                              subject_id="1", summary="a proposal",
                              payload={"title": "x"}, producer="test-producer",
                              confidence=-3)

    def test_confidence_boundary_values_are_allowed(self):
        self.store.record(folder="scene-matches", subject_type="scene",
                          subject_id="1", summary="a proposal",
                          payload={"title": "x"}, producer="test-producer",
                          confidence=0)
        self.store.record(folder="scene-matches", subject_type="scene",
                          subject_id="2", summary="a proposal",
                          payload={"title": "y"}, producer="test-producer",
                          confidence=1)
        self.assertEqual(len(self.store.items()), 2)

    def test_confidence_none_is_allowed(self):
        self.store.record(folder="scene-matches", subject_type="scene",
                          subject_id="1", summary="a proposal",
                          payload={"title": "x"}, producer="test-producer",
                          confidence=None)
        self.assertIsNone(self.store.items()[0]["confidence"])


class SingleInstancePerFile(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._dir, ignore_errors=True))

    def test_opening_a_second_store_on_the_same_path_raises(self):
        path = os.path.join(self._dir, "s.db")
        first = Store(path)
        self.addCleanup(first.close)
        with self.assertRaises(RuntimeError):
            Store(path)

    def test_a_path_can_be_reopened_after_close(self):
        path = os.path.join(self._dir, "s.db")
        first = Store(path)
        first.close()
        second = Store(path)          # must not raise
        second.close()

    def test_a_symlink_to_an_open_database_also_raises(self):
        # os.path.abspath alone normalises "." and ".." but not symlinks;
        # a symlink to an already-open file is still the same file, and the
        # guard exists to stop two handles on one file, not two spellings
        real_path = os.path.join(self._dir, "real.db")
        link_path = os.path.join(self._dir, "link.db")
        first = Store(real_path)
        self.addCleanup(first.close)
        try:
            os.symlink(real_path, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        with self.assertRaises(RuntimeError):
            Store(link_path)

    def test_a_collected_store_releases_its_path(self):
        # dropping the last reference without calling close() (an exception
        # on some path that skips a `with` block, say) must not lock the
        # path forever - there would be no route back short of restarting
        path = os.path.join(self._dir, "s.db")

        def make_and_drop():
            Store(path)              # never closed; reference dies on return

        make_and_drop()
        gc.collect()
        second = Store(path)         # must not raise: collection released it
        second.close()


class Concurrency(_StoreCase):
    def test_concurrent_writers_do_not_lose_or_corrupt_rows(self):
        # the job runner writes from a background thread while the interface reads
        import threading
        errors = []

        def writer(start):
            try:
                for i in range(start, start + 25):
                    self._record(subject_id="w%d" % i)
            except Exception as e:      # noqa: BLE001 - the test is the assertion
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n * 25,)) for n in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.items(limit=1000)), 100)

    def test_reads_during_writes_do_not_raise(self):
        import threading
        stop = threading.Event()
        errors = []

        def reader():
            try:
                while not stop.is_set():
                    self.store.counts()
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        r = threading.Thread(target=reader); r.start()
        for i in range(50):
            self._record(subject_id="c%d" % i)
        stop.set(); r.join()
        self.assertEqual(errors, [])


class Reads(_StoreCase):
    def test_filters_by_folder_and_state(self):
        a = self._record(subject_id="1", folder="scene-matches")
        self._record(subject_id="2", folder="cleanups")
        self.store.mark_seen(a)
        self.assertEqual(len(self.store.items(folder="scene-matches")), 1)
        self.assertEqual(len(self.store.items(state="new")), 1)

    def test_paginates(self):
        for i in range(5):
            self._record(subject_id=str(i))
        self.assertEqual(len(self.store.items(limit=2)), 2)
        self.assertEqual(len(self.store.items(limit=2, offset=4)), 1)

    def test_counts_by_state(self):
        a = self._record(subject_id="1")
        self._record(subject_id="2")
        self.store.mark_seen(a)
        self.assertEqual(self.store.counts(), {"new": 1, "seen": 1})


class TheOrderIsAStableTotalOrder(unittest.TestCase):
    """A bounded page's own reachability depends on `items()`'s order being
    a genuine total order -- see `items()`'s own docstring for why
    `created_at, fingerprint` is that (oldest first, ties broken by a
    fingerprint that is itself fixed for a row's whole life). The risk this
    pins is specifically the coarse resolution of `created_at`: `_utcnow()`
    only has one-second precision (see the module docstring above
    `record_run`), so a batch of proposals recorded inside one second --
    ordinary for a scan -- ties on `created_at` entirely and the ORDER BY's
    second column is the only thing keeping their relative order fixed. A
    fixture that gives every row a distinct timestamp (as most of this
    file's fixtures do, via `now=`) cannot exercise this at all.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._dir, "s.db"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_rows_sharing_one_created_at_still_come_back_in_one_fixed_order(self):
        # The SAME `now=` for every row -- forcing the exact tie a fast
        # scan produces in production (every proposal from one run landing
        # in the same `_utcnow()` second), deterministically rather than
        # hoping this process is fast enough for it to happen on its own.
        fps = [self.store.record(
            folder="library", subject_type="scene", subject_id=str(i),
            summary="s", payload={"i": i}, producer="p",
            now="2026-07-01T00:00:00")
            for i in range(50)]
        first_read = [i["fingerprint"] for i in self.store.items()]
        second_read = [i["fingerprint"] for i in self.store.items()]
        self.assertEqual(first_read, second_read)
        # And the order IS the fingerprint order -- not merely "some order
        # SQLite happens to repeat" -- which is what `, fingerprint` in the
        # `ORDER BY` clause is actually for.
        self.assertEqual(first_read, sorted(fps))

    def test_the_same_holds_with_pagination(self):
        whole_ids = [self.store.record(
            folder="library", subject_type="scene", subject_id=str(i),
            summary="s", payload={"i": i}, producer="p",
            now="2026-07-01T00:00:00")
            for i in range(250)]
        whole = [i["fingerprint"] for i in self.store.items(limit=10_000)]
        self.assertEqual(whole, sorted(whole_ids))
        first_page = [i["fingerprint"]
                     for i in self.store.items(limit=100, offset=0)]
        second_page = [i["fingerprint"]
                      for i in self.store.items(limit=100, offset=100)]
        self.assertEqual(first_page, whole[:100])
        self.assertEqual(second_page, whole[100:200])


class ItemCount(_StoreCase):
    """`item_count` answers the same question `len(items(...))` would, for a
    caller that cannot afford to fetch a page's whole population just to
    learn its size -- the reason a bounded page exists at all. Every test
    here uses a population LARGER than any page a caller of this method
    would actually render, on purpose: a fixture the size of one page cannot
    tell "the true total" apart from "the size of what was fetched", which
    is exactly the gap this method exists to keep visible.
    """

    def test_matches_len_of_items_with_no_filter(self):
        for i in range(250):
            self._record(subject_id=str(i))
        self.assertEqual(self.store.item_count(), 250)
        self.assertEqual(self.store.item_count(),
                         len(self.store.items(limit=10_000)))

    def test_unaffected_by_limit_and_offset_items_would_take(self):
        # The whole point: a page fetches two rows and still learns the
        # population is 250, not 2.
        for i in range(250):
            self._record(subject_id=str(i))
        self.assertEqual(len(self.store.items(limit=2)), 2)
        self.assertEqual(self.store.item_count(), 250)

    def test_respects_the_state_filter(self):
        for i in range(250):
            fp = self._record(subject_id=str(i))
            if i < 60:
                self.store.mark_seen(fp)
        self.assertEqual(self.store.item_count(state="seen"), 60)
        self.assertEqual(self.store.item_count(state="new"), 190)

    def test_respects_the_subject_types_filter(self):
        for i in range(250):
            self.store.record(folder="library", subject_type="scene",
                              subject_id="scene-%d" % i, summary="s",
                              payload={"i": i}, producer="p")
        for i in range(30):
            self.store.record(folder="library", subject_type="performer",
                              subject_id="perf-%d" % i, summary="s",
                              payload={"i": i}, producer="p")
        self.assertEqual(
            self.store.item_count(subject_types=("scene",)), 250)
        self.assertEqual(
            self.store.item_count(subject_types=("performer",)), 30)
        self.assertEqual(
            self.store.item_count(subject_types=()), 0)

    def test_exclude_states_widens_the_default_hidden_set(self):
        for i in range(250):
            fp = self._record(subject_id=str(i))
            if i < 40:
                self.store.mark_applied(fp)
        # Without `exclude_states`, an applied row is still counted (only
        # dismissed/muted/superseded/gone are hidden by default).
        self.assertEqual(self.store.item_count(), 250)
        self.assertEqual(
            self.store.item_count(exclude_states=("applied",)), 210)

    def test_matches_items_of_the_same_call_when_exclude_states_is_used(self):
        for i in range(250):
            fp = self._record(subject_id=str(i))
            if i < 40:
                self.store.mark_applied(fp)
        self.assertEqual(
            self.store.item_count(exclude_states=("applied",)),
            len(self.store.items(limit=10_000,
                                 exclude_states=("applied",))))

    def test_exclude_states_together_with_an_explicit_state_raises(self):
        # A caller asking for exactly one state AND naming states to
        # exclude has written a self-contradictory request -- see
        # `Store._item_clauses`'s own docstring for why this refuses rather
        # than silently picking one half to honour.
        with self.assertRaises(ValueError):
            self.store.item_count(state="new", exclude_states=("applied",))
        with self.assertRaises(ValueError):
            self.store.items(state="new", exclude_states=("applied",))


class Has(_StoreCase):
    def test_true_for_a_recorded_proposal(self):
        fp = self._record()
        self.assertTrue(self.store.has(fp))

    def test_false_for_one_never_recorded(self):
        self.assertFalse(self.store.has("nosuchfingerprint"))

    def test_false_for_a_dismissed_proposal(self):
        fp = self._record()
        self.store.dismiss(fp, reason="wrong match")
        self.assertFalse(self.store.has(fp))

    def test_false_for_a_muted_subjects_proposal(self):
        fp = self._record(subject_id="1")
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertFalse(self.store.has(fp))


class MutedSubjects(_StoreCase):
    """`muted_subjects()` answers from the `mute` table.

    That is the whole point of the read: `mute` accepts a subject that has
    never had a proposal, so any answer derived from `item` rows cannot see a
    pre-emptive mute. A caller that asks before spending a lookup — a scan
    choosing its batch — needs the answer the store itself will use when it
    later refuses the proposal.

    Every assertion below compares the WHOLE set, not membership: a read that
    reported an extra subject would starve a scan of files nothing was ever
    decided about, and membership assertions cannot see that.
    """

    def test_a_subject_muted_before_any_proposal_is_reported(self):
        """The case no other public read can observe. `items(state='muted')`
        sees a mute only through the row it moved; there is no row here."""
        self.store.mute("scene", "7", reason="never identifiable")
        self.assertEqual(self.store.muted_subjects(), {("scene", "7")})

    def test_a_subject_muted_after_a_proposal_is_reported_too(self):
        self._record(subject_id="1")
        self.store.mute("scene", "1")
        self.assertEqual(self.store.muted_subjects(), {("scene", "1")})

    def test_an_unmuted_subject_is_not_reported(self):
        self._record(subject_id="1")
        self.assertEqual(self.store.muted_subjects(), set())

    def test_the_answer_does_not_depend_on_an_item_row_existing(self):
        """Two mutes, identical but for whether a proposal preceded them, are
        reported identically. Pinned together so a read that quietly answered
        from `item` rows could not pass by covering only the common case."""
        self._record(subject_id="1")
        self.store.mute("scene", "1")
        self.store.mute("scene", "2")
        self.assertEqual(self.store.muted_subjects(),
                         {("scene", "1"), ("scene", "2")})

    def test_the_subject_type_is_part_of_the_answer(self):
        """Subject ids are only unique within a type, so a muted performer "1"
        must not read as a muted scene "1"."""
        self.store.mute("performer", "1")
        self.assertEqual(self.store.muted_subjects(), {("performer", "1")})

    def test_a_dismissed_proposal_does_not_mute_its_subject(self):
        """Dismissal rejects one proposal; a better one for the same subject
        may still arrive. Reporting it as muted would stop it being looked at
        at all — the opposite of what dismissal means."""
        fp = self._record(subject_id="1")
        self.store.dismiss(fp, reason="wrong match")
        self.assertEqual(self.store.muted_subjects(), set())

    def test_an_integer_subject_id_reads_back_as_a_string(self):
        """The caller-facing contract: ids come back as strings whatever they
        went in as, so a caller comparing `str(id)` from an API gets a match
        rather than a set it can never hit.

        This pins the contract, not any one mechanism for it — `mute`'s own
        `str()` and the column's TEXT affinity each deliver it independently,
        so removing either alone leaves this passing.
        """
        self.store.mute("scene", 7)
        self.assertEqual(self.store.muted_subjects(), {("scene", "7")})

    def test_nothing_muted_is_an_empty_set(self):
        self.assertEqual(self.store.muted_subjects(), set())


class Unmuting(_StoreCase):
    """Reversing `mute` — a person changing their own mind about a
    subject's FUTURE, never a scan overruling one. `record()`/`select()`
    are the only things that ever read the `mute` table again, and neither
    is called by `unmute` itself.
    """

    def test_an_unmuted_subject_no_longer_blocks_a_record(self):
        self.store.mute("scene", "1", reason="never identifiable")
        self.store.unmute("scene", "1")
        self._record(subject_id="1")
        self.assertEqual(len(self.store.items()), 1)

    def test_an_unmuted_subject_is_gone_from_muted_subjects(self):
        self.store.mute("scene", "1")
        self.store.unmute("scene", "1")
        self.assertEqual(self.store.muted_subjects(), set())

    def test_unmuting_one_subject_leaves_another_muted(self):
        self.store.mute("scene", "1")
        self.store.mute("scene", "2")
        self.store.unmute("scene", "1")
        self.assertEqual(self.store.muted_subjects(), {("scene", "2")})

    def test_unmuting_a_subject_that_was_never_muted_is_not_an_error(self):
        self.store.unmute("scene", "no-such-subject")  # must not raise

    def test_an_integer_subject_id_still_unmutes_the_string_form(self):
        self.store.mute("scene", 7)
        self.store.unmute("scene", 7)
        self.assertEqual(self.store.muted_subjects(), set())

    def test_unmuting_does_not_clear_a_dismissal_on_the_same_fingerprint(self):
        # HARM: the two rejections are different things and must stay apart.
        # A subject dismissed and then separately muted (`dismiss`/`mute`
        # are free to move a row between each other's states) must keep its
        # dismissal after an unmute -- unmute touches the `mute` table only.
        #
        # Dismissed and muted PRE-EMPTIVELY -- before any `item` row exists
        # -- deliberately: with an existing row, `record()`'s ON CONFLICT
        # path only ever touches `last_seen_at`, never `state`, so a stale
        # row already sitting in `state='muted'` would keep `items()` empty
        # regardless of whether the `dismissal` table was wrongly cleared,
        # and the test would pass for the wrong reason. Pre-emptively, the
        # `dismissal` table is the ONLY thing that can still block the
        # first-ever INSERT for this fingerprint.
        payload = {"title": "Copper Kettle"}
        fp = fingerprint("scene-matches", "scene", "1", payload)
        self.store.dismiss(fp, reason="wrong match")
        self.store.mute("scene", "1", reason="never identifiable")
        self.store.unmute("scene", "1")
        self._record(subject_id="1", payload=payload)
        self.assertEqual(self.store.items(), [],
                         "the dismissal must still block a re-record")


class Mutes(_StoreCase):
    """`mutes()` — the reason/timestamp `muted_subjects()` deliberately
    leaves out, for showing a person what is currently muted."""

    def test_reports_the_reason_and_when(self):
        self.store.mute("scene", "1", reason="never identifiable",
                        now="2026-07-01T00:00:00")
        self.assertEqual(self.store.mutes(), [
            {"subject_type": "scene", "subject_id": "1",
             "reason": "never identifiable", "at": "2026-07-01T00:00:00",
             "item": None}])

    def test_nothing_muted_is_an_empty_list(self):
        self.assertEqual(self.store.mutes(), [])

    def test_unmuting_removes_it_from_the_listing(self):
        self.store.mute("scene", "1")
        self.store.unmute("scene", "1")
        self.assertEqual(self.store.mutes(), [])

    def test_a_subject_muted_with_no_proposal_ever_recorded_has_no_item(self):
        # The genuine exception ticket 97 names: muting ahead of any scan
        # ever finding the subject. No `item` row exists at all here, so
        # there is nothing to recover -- `item` must say so honestly
        # rather than inventing something.
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertIsNone(self.store.mutes()[0]["item"])

    def test_a_muted_subjects_payload_is_recovered_from_its_proposal(self):
        # The ordinary case: `mute` never deletes the `item` row it
        # blocked, it only changes its state (see `mute`'s docstring), so
        # the payload recorded against the proposal is still there to show.
        payload = {"path": "/library/Nine Winters/reel.mp4", "title": "X"}
        self._record(subject_id="1", payload=payload)
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertEqual(self.store.mutes()[0]["item"]["payload"], payload)

    def test_a_muted_subjects_whole_item_is_recovered_not_just_its_payload(self):
        # HARM: the Muted section builds its row with the SAME builders the
        # Dismissed section uses, and those read the fingerprint, the state
        # and the subject type as well as the payload. A projection here
        # would leave the row builder to invent the rest -- store state
        # fabricated on a page whose controls write to a library.
        #
        # Asserted as the WHOLE dict against the row `items()` returns for
        # the same proposal, not field by field: a column dropped from the
        # recovery is exactly the drift this must catch, and naming three
        # fields would not see a fourth go missing.
        fp = self._record(subject_id="1",
                          payload={"path": "/library/x/reel.mp4"})
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertEqual(self.store.mutes()[0]["item"],
                         self.store.items(state="muted")[0])
        self.assertEqual(self.store.mutes()[0]["item"]["fingerprint"], fp)

    def test_a_recovered_items_prior_state_is_decoded_like_items_decodes_it(self):
        # `prior_state` is the other JSON column, and a muted row that
        # reached the page carrying a JSON STRING where a dict was expected
        # fails at the first index rather than anywhere a reader would look.
        fp = self._record(subject_id="1",
                          payload={"path": "/library/x/reel.mp4"})
        self.store.mark_applied(fp, prior_state={"title": "what was there"})
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertEqual(self.store.mutes()[0]["item"]["prior_state"],
                         {"title": "what was there"})

    def test_recovers_the_most_recently_seen_proposal_when_more_than_one_exists(self):
        # A subject can carry more than one `item` row -- successive
        # proposals under different payloads before it was ever muted.
        # `mutes()` must not pick one arbitrarily.
        older = {"path": "/library/x/older.mp4", "title": "Older"}
        newer = {"path": "/library/x/newer.mp4", "title": "Newer"}
        self._record(subject_id="1", payload=older)
        self.store.record(folder="scene-matches", subject_type="scene",
                          subject_id="1", summary="a proposal",
                          payload=newer, producer="test-producer",
                          confidence=0.9, now="2099-01-01T00:00:00")
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertEqual(self.store.mutes()[0]["item"]["payload"], newer)

    def test_showing_the_richer_row_leaves_unmuting_restoring_it_exactly_as_before(self):
        # The Muted section now recovers the whole item so it can draw what
        # a dismissed row draws. That is a READ, and it must not have
        # changed what Unmute does: the standing block goes, and the row it
        # hid comes back to `new` -- decidable again, not stuck hidden with
        # the page reporting it restored.
        self._record(subject_id="1", payload={"path": "/library/x/reel.mp4"})
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertIsNotNone(self.store.mutes()[0]["item"])

        self.store.unmute("scene", "1")

        self.assertEqual(self.store.mutes(), [])
        self.assertEqual([i["state"] for i in self.store.items()], ["new"])

    def test_a_muted_subjects_payload_is_recovered_even_when_the_row_was_applied(self):
        # `mute` deliberately leaves a terminal (`applied`/`failed`) row's
        # own `state` untouched -- see `mute`'s docstring -- but the payload
        # is still sitting in the `item` table regardless, and this must
        # still find it rather than only looking at rows `mute` itself
        # flipped to `state='muted'`.
        fp = self._record(subject_id="1",
                          payload={"path": "/library/x/reel.mp4"})
        self.store.mark_applied(fp)
        self.store.mute("scene", "1", reason="never identifiable")
        self.assertEqual(self.store.mutes()[0]["item"]["payload"],
                         {"path": "/library/x/reel.mp4"})


class Undismissing(_StoreCase):
    def test_an_undismissed_proposal_is_visible_again(self):
        fp = self._record()
        self.store.dismiss(fp, reason="wrong match")
        self.store.undismiss(fp)
        self.assertEqual([i["fingerprint"] for i in self.store.items()], [fp])

    def test_an_undismissed_proposal_no_longer_blocks_a_rerecord(self):
        fp = self._record()
        self.store.dismiss(fp, reason="wrong match")
        self.store.undismiss(fp)
        self._record()  # the producer re-records the identical proposal
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(self.store.items()[0]["state"], "new")

    def test_undismissing_a_fingerprint_never_dismissed_is_not_an_error(self):
        self.store.undismiss("nosuchfingerprint")  # must not raise

    def test_undismissing_does_not_clear_a_mute_on_the_same_subject(self):
        # HARM: symmetric to `Unmuting`'s test above.
        fp = self._record(subject_id="1")
        self.store.mute("scene", "1", reason="never identifiable")
        self.store.dismiss(fp, reason="wrong match")
        self.store.undismiss(fp)
        self.assertEqual(self.store.muted_subjects(), {("scene", "1")})

    def test_undismissing_a_row_that_was_since_muted_leaves_it_muted(self):
        # dismiss then mute moves the row to 'muted' (mute wins the second
        # move -- see `dismiss`'s docstring). Undismiss must not drag a
        # row that is now muted back to visible just because it passed
        # through 'dismissed' on the way there.
        fp = self._record(subject_id="1")
        self.store.dismiss(fp, reason="wrong match")
        self.store.mute("scene", "1", reason="never identifiable")
        self.store.undismiss(fp)
        item = next(i for i in self.store.items(state="muted")
                   if i["fingerprint"] == fp)
        self.assertEqual(item["state"], "muted")


class Refusals(_StoreCase):
    """Recording and listing a standing refusal — see the block above
    `Store.record_refusal` for why this is keyed by subject rather than
    reusing the `item` table's fingerprint."""

    def test_records_and_lists_a_refusal(self):
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "too close to call",
                                  now="2026-07-01T00:00:00")
        self.assertEqual(self.store.refusals(), [
            {"subject_type": "scene", "subject_id": "1",
             "path": "/library/x/clip.mp4", "reason": "too close to call",
             "at": "2026-07-01T00:00:00", "stores": []}])

    def test_nothing_refused_is_an_empty_list(self):
        self.assertEqual(self.store.refusals(), [])

    def test_recording_it_again_replaces_rather_than_accumulates(self):
        # HARM: reusing a fingerprint-shaped identity here -- one that
        # changes whenever the score or runners-up do, as `item`'s does --
        # would grow a fresh row every night the same file stays
        # unresolved. Keyed by subject, a second refusal for the SAME
        # subject overwrites the first rather than adding to it.
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "a tie", now="2026-07-01T00:00:00")
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "a closer tie", now="2026-07-02T00:00:00")
        self.assertEqual(len(self.store.refusals()), 1)
        self.assertEqual(self.store.refusals()[0]["reason"], "a closer tie")
        self.assertEqual(self.store.refusals()[0]["at"], "2026-07-02T00:00:00")

    def test_a_refusal_for_one_subject_does_not_touch_another(self):
        self.store.record_refusal("scene", "1", "/a.mp4", "a tie")
        self.store.record_refusal("scene", "2", "/b.mp4", "a tie")
        self.assertEqual(len(self.store.refusals()), 2)

    def test_a_proposal_for_the_same_subject_clears_its_refusal(self):
        # A refusal is transient: the moment the subject actually produces a
        # proposal, the stale "refused" verdict must not sit beside it.
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4", "a tie")
        self._record(subject_id="1")
        self.assertEqual(self.store.refusals(), [])

    def test_a_proposal_for_a_different_subject_does_not_clear_it(self):
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4", "a tie")
        self._record(subject_id="2")
        self.assertEqual(len(self.store.refusals()), 1)

    def test_paginates_in_the_same_order_as_an_unpaginated_read(self):
        for i in range(250):
            self.store.record_refusal(
                "scene", str(i), "/library/%d.mp4" % i, "a tie",
                now="2026-07-01T00:00:%02d" % (i % 60))
        whole = self.store.refusals()
        self.assertEqual(len(whole), 250)
        first_page = self.store.refusals(limit=100)
        second_page = self.store.refusals(limit=100, offset=100)
        third_page = self.store.refusals(limit=100, offset=200)
        self.assertEqual(first_page, whole[:100])
        self.assertEqual(second_page, whole[100:200])
        self.assertEqual(third_page, whole[200:])

    def test_refusal_count_matches_len_of_an_unpaginated_read(self):
        for i in range(250):
            self.store.record_refusal(
                "scene", str(i), "/library/%d.mp4" % i, "a tie")
        self.assertEqual(self.store.refusal_count(), 250)

    def test_refusal_count_is_unaffected_by_limit(self):
        for i in range(250):
            self.store.record_refusal(
                "scene", str(i), "/library/%d.mp4" % i, "a tie")
        self.assertEqual(len(self.store.refusals(limit=5)), 5)
        self.assertEqual(self.store.refusal_count(), 250)

    def test_refusal_count_excludes_a_subject_marked_gone(self):
        for i in range(250):
            self.store.record_refusal(
                "scene", str(i), "/library/%d.mp4" % i, "a tie")
        for i in range(40):
            self.store.mark_gone("scene", str(i))
        self.assertEqual(self.store.refusal_count(), 210)
        self.assertEqual(len(self.store.refusals(limit=10_000)), 210)

    # -- what every store returned, kept as values ------------------------- #

    ALPHA = {"store": "alpha", "rows": 40, "score": 0.342,
             "title": "Evening Ritual",
             "url": "https://alpha.example/clip/evening-ritual", "error": None}
    BETA = {"store": "beta", "rows": 0, "score": None, "title": None,
            "url": None, "error": None}
    GAMMA = {"store": "gamma", "rows": None, "score": None, "title": None,
             "url": None, "error": "TimeoutError: timed out"}

    def test_the_stores_round_trip_as_the_list_of_dicts_that_went_in(self):
        """Asserted as the whole list of whole dicts. This is the record the
        Refused section is built from, and a check that one store survived
        passes while the other two are lost on the way through SQLite."""
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "alpha: nothing above the threshold",
                                  stores=[self.ALPHA, self.BETA, self.GAMMA],
                                  now="2026-07-01T00:00:00")
        self.assertEqual(self.store.refusals()[0]["stores"],
                         [self.ALPHA, self.BETA, self.GAMMA])

    def test_a_score_comes_back_as_a_number_not_as_text(self):
        """The half of this the prose `reason` cannot give: a score readable
        only by parsing a sentence is not stored. TEXT is what the column
        holds, so a round trip that lost the JSON decoding would hand back
        the string "0.342" and every arithmetic on it would still `assertIn`
        successfully somewhere."""
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "alpha: nothing above the threshold",
                                  stores=[self.ALPHA])
        score = self.store.refusals()[0]["stores"][0]["score"]
        self.assertIsInstance(score, float)
        self.assertEqual(score, 0.342)

    def test_recording_it_again_replaces_the_stores_it_recorded(self):
        """HARM: a refusal is re-recorded every night the file stays
        unresolved. Stores that accumulated would grow without bound, and a
        reader would be shown last week's distribution beside this week's
        reason. One examination cannot see that -- this runs two."""
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "a tie", stores=[self.ALPHA, self.BETA])
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "a closer tie", stores=[self.GAMMA])

        self.assertEqual(self.store.refusals()[0]["stores"], [self.GAMMA])

    def test_the_recorded_order_is_the_callers_and_is_not_re_sorted(self):
        """`scan._store_reports` orders by score with the scores in front of
        it. A second ordering rule here would be free to disagree, so there
        is none: the list comes back exactly as it went in. The fixture is
        deliberately NOT in name order, so a re-sort by name is visible."""
        given = [self.GAMMA, self.ALPHA, self.BETA]
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "a tie", stores=given)

        self.assertEqual([s["store"]
                          for s in self.store.refusals()[0]["stores"]],
                         ["gamma", "alpha", "beta"])

    def test_a_refusal_recorded_with_no_stores_reads_back_an_empty_list(self):
        """The honest shape for a refusal no store search stands behind -- a
        creator that never resolved. Not `None`: the field is a list of what
        was searched, and nothing was."""
        self.store.record_refusal("scene", "1", "/library/x/clip.mp4",
                                  "creator unresolved")
        self.assertEqual(self.store.refusals()[0]["stores"], [])


class ProducerRuns(_StoreCase):
    """When each producer last ran.

    This is the whole reason the record is in the store rather than in a
    scheduler's memory: in memory, every process restart makes every producer
    due at once, so a nightly full-library scrape runs on every deploy.
    """

    def test_a_producer_that_has_never_run_reports_nothing(self):
        """`None`, not an error: "never" is the ordinary state of a producer on
        the first tick after it is added, not a caller mistake."""
        self.assertIsNone(self.store.last_run("nightly-scrape"))

    def test_a_recorded_run_reads_back_what_was_recorded(self):
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        self.assertEqual(self.store.last_run("nightly-scrape"),
                         "2026-07-26T02:00:00+00:00")

    def test_recording_again_replaces_rather_than_accumulates(self):
        """A producer has *a* last run, not a history. The whole-dict assertion
        is the one that can see accumulation — `last_run` alone would pass on a
        table with two rows as long as the query happened to pick the right
        one."""
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        self.store.record_run("nightly-scrape", at="2026-07-27T02:00:00+00:00")
        self.assertEqual(self.store.last_run("nightly-scrape"),
                         "2026-07-27T02:00:00+00:00")
        self.assertEqual(self.store.runs(),
                         {"nightly-scrape": "2026-07-27T02:00:00+00:00"})

    def test_recording_an_earlier_time_replaces_too(self):
        """The store records what it was told; it does not keep the maximum.
        Preferring the larger value would mean interpreting the timestamp, and
        an operator correcting a run stamped by a skewed clock would find the
        store quietly keeping the value they are trying to replace."""
        self.store.record_run("nightly-scrape", at="2026-07-27T02:00:00+00:00")
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        self.assertEqual(self.store.last_run("nightly-scrape"),
                         "2026-07-26T02:00:00+00:00")

    def test_the_time_defaults_to_now(self):
        """Bracketed rather than compared to a fixture, because the point is
        that an omitted `at` is a real UTC clock reading and not, say, the
        empty string or the epoch."""
        before = datetime.now(timezone.utc).replace(microsecond=0)
        self.store.record_run("nightly-scrape")
        after = datetime.now(timezone.utc)
        recorded = datetime.fromisoformat(self.store.last_run("nightly-scrape"))
        self.assertIsNotNone(recorded.tzinfo)
        self.assertLessEqual(before, recorded)
        self.assertLessEqual(recorded, after)

    def test_one_producers_run_does_not_answer_for_another(self):
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        self.assertIsNone(self.store.last_run("hourly-tags"))

    def test_nothing_has_run_is_an_empty_mapping(self):
        self.assertEqual(self.store.runs(), {})

    def test_runs_answers_for_every_producer_in_one_call(self):
        """The scheduler asks about every producer it knows on every tick, so
        the read is a mapping rather than N lookups. Asserted as a whole dict:
        a read that reported a producer that had never run would make it look
        not-due, silently skipping it."""
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        self.store.record_run("hourly-tags", at="2026-07-26T09:00:00+00:00")
        self.assertEqual(self.store.runs(), {
            "nightly-scrape": "2026-07-26T02:00:00+00:00",
            "hourly-tags": "2026-07-26T09:00:00+00:00",
        })

    def test_a_producer_that_has_never_run_is_simply_absent(self):
        """Absent, not an error and not a `None` entry — a caller iterating the
        mapping is looking at producers with a run to compare against."""
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        runs = self.store.runs()
        self.assertNotIn("hourly-tags", runs)
        self.assertEqual(runs, {"nightly-scrape": "2026-07-26T02:00:00+00:00"})

    def test_recording_a_run_is_not_a_proposal(self):
        """The two tables are independent: a run must not appear in the inbox,
        and must not disturb the counts a badge reads."""
        self.store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.store.counts(), {})


class ProducerRunsSurviveARestart(unittest.TestCase):
    """The property the table exists for, across a real close and reopen.

    Everything in `ProducerRuns` would pass just as well against a dict held on
    the instance; only this can tell the two apart.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def test_a_run_recorded_before_a_restart_is_still_known_after_it(self):
        with Store(self.path) as store:
            store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.last_run("nightly-scrape"),
                             "2026-07-26T02:00:00+00:00")
            self.assertEqual(store.runs(),
                             {"nightly-scrape": "2026-07-26T02:00:00+00:00"})


class RenamedProducerRunsSurviveTheRename(unittest.TestCase):
    """A run recorded under a job's OLD name must answer `last_run`/`runs` for
    its current one, so a rename cannot make the scheduler read a producer
    that has genuinely run for years as never-run.

    Every fixture here writes `producer_run` directly with a raw connection,
    the same way `RunTableAddedOnAnExistingDatabase` and
    `SchemaAdditionOnAnExistingDatabase` emulate a database an earlier build
    left behind — `Store` itself never writes the old name, so seeding it any
    other way would not be testing what a real upgraded deployment has on
    disk.

    Literal old name, literal new name, one test each for the three real
    renames — deriving either side from `RENAMED_JOBS` would move both halves
    together, and a map emptied to `{}` or repointed would stay green.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def _seed(self, producer, at):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS producer_run "
            "(producer TEXT PRIMARY KEY, at TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO producer_run VALUES (?, ?)", (producer, at))
        connection.commit()
        connection.close()

    def _seed_both(self, old, current, old_at, current_at):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS producer_run "
            "(producer TEXT PRIMARY KEY, at TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO producer_run VALUES (?, ?)", (old, old_at))
        connection.execute(
            "INSERT INTO producer_run VALUES (?, ?)", (current, current_at))
        connection.commit()
        connection.close()

    def _raw_producer_runs(self):
        connection = sqlite3.connect(self.path)
        rows = connection.execute(
            "SELECT producer, at, rowid FROM producer_run").fetchall()
        connection.close()
        return rows

    # -- the three real renames, each pinned by literals on both sides ---- #

    def test_a_run_under_the_old_scene_name_is_read_under_the_new_one(self):
        self._seed("nightly-library-scan", "2026-07-26T02:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.last_run("scene-scan"),
                             "2026-07-26T02:00:00+00:00")
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-07-26T02:00:00+00:00"})

    def test_a_run_under_the_old_performer_name_is_read_under_the_new_one(self):
        self._seed("performer-descriptions", "2026-07-26T02:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.last_run("performer-scan"),
                             "2026-07-26T02:00:00+00:00")
            self.assertEqual(store.runs(),
                             {"performer-scan": "2026-07-26T02:00:00+00:00"})

    def test_a_run_under_the_old_tag_name_is_read_under_the_new_one(self):
        self._seed("tag-merge", "2026-07-26T02:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.last_run("tag-scan"),
                             "2026-07-26T02:00:00+00:00")
            self.assertEqual(store.runs(),
                             {"tag-scan": "2026-07-26T02:00:00+00:00"})

    # -- the ordinary cases the migration must not disturb ----------------- #

    def test_a_run_already_under_the_current_name_is_unchanged(self):
        self._seed("scene-scan", "2026-07-26T02:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-07-26T02:00:00+00:00"})

    def test_a_producer_with_no_history_under_either_name_stays_never_run(self):
        # Guards the direction opposite the main fix: the migration must not
        # invent a row for a renamed producer that has genuinely never run,
        # which would make it look like it HAS and defeat the "still due
        # immediately" guarantee `cronicled.schedule.due` provides.
        with Store(self.path) as store:
            self.assertIsNone(store.last_run("scene-scan"))
            self.assertIsNone(store.last_run("performer-scan"))
            self.assertIsNone(store.last_run("tag-scan"))
            self.assertEqual(store.runs(), {})

    # -- a history recorded under both names, e.g. an upgrade then a
    # rollback then a second upgrade ---------------------------------------

    def test_when_the_old_name_ran_more_recently_it_wins(self):
        self._seed_both("nightly-library-scan", "scene-scan",
                        old_at="2026-07-27T09:00:00+00:00",
                        current_at="2026-07-20T03:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-07-27T09:00:00+00:00"})

    def test_when_the_current_name_ran_more_recently_it_wins(self):
        # The reverse direction, pinned separately: a fixture whose winner is
        # always the same side cannot tell "later wins" from "old always
        # wins" or "new always wins".
        self._seed_both("nightly-library-scan", "scene-scan",
                        old_at="2026-07-20T03:00:00+00:00",
                        current_at="2026-07-27T09:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-07-27T09:00:00+00:00"})

    def test_the_collision_leaves_exactly_one_row_not_two(self):
        self._seed_both("nightly-library-scan", "scene-scan",
                        old_at="2026-07-27T09:00:00+00:00",
                        current_at="2026-07-20T03:00:00+00:00")
        with Store(self.path):
            pass
        rows = self._raw_producer_runs()
        self.assertEqual([r[0] for r in rows], ["scene-scan"])

    # -- a reading on one side that cannot be read as a moment at all ------- #
    #
    # `record_run` stores whatever it is given, so either side of a collision
    # could hold something that is not a usable timestamp -- see
    # `cronicled.schedule.due`'s own tolerance for this. Recency cannot be
    # compared against nothing, so the side that DOES parse is kept, rather
    # than losing real information to a value that is not a moment at all.

    def test_an_unreadable_old_reading_loses_to_a_readable_current_one(self):
        self._seed_both("nightly-library-scan", "scene-scan",
                        old_at="not-a-timestamp",
                        current_at="2026-07-20T03:00:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-07-20T03:00:00+00:00"})

    def test_an_unreadable_current_reading_loses_to_a_readable_old_one(self):
        self._seed_both("nightly-library-scan", "scene-scan",
                        old_at="2026-07-20T03:00:00+00:00",
                        current_at="not-a-timestamp")
        with Store(self.path) as store:
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-07-20T03:00:00+00:00"})

    # -- idempotence: opening a second time must rewrite nothing ------------ #

    def test_migrating_a_second_time_changes_nothing(self):
        self._seed("nightly-library-scan", "2026-07-26T02:00:00+00:00")
        with Store(self.path):
            pass
        first = self._raw_producer_runs()
        with Store(self.path):
            pass
        second = self._raw_producer_runs()
        # Whole rows, including `rowid`: identical `at` values with a
        # different `rowid` would mean the row was deleted and reinserted on
        # the second pass -- unobservable through `last_run` alone, since the
        # value did not change, but exactly the "rewrites anything" this must
        # not do.
        self.assertEqual(second, first)
        self.assertEqual([r[0] for r in first], ["scene-scan"])

    def test_migrating_a_collision_a_second_time_changes_nothing_either(self):
        self._seed_both("nightly-library-scan", "scene-scan",
                        old_at="2026-07-27T09:00:00+00:00",
                        current_at="2026-07-20T03:00:00+00:00")
        with Store(self.path):
            pass
        first = self._raw_producer_runs()
        with Store(self.path):
            pass
        second = self._raw_producer_runs()
        self.assertEqual(second, first)


class _RunLogCase(_StoreCase):
    """Shared fixture for the run log: distinct, increasing start times.

    Every timestamp is distinct on purpose. `_utcnow` has one-second
    resolution, so a fixture that let the clock supply the times would hand
    the ordering rules a column that cannot separate the rows, and a test of
    "newest first" that cannot tell newest from oldest proves nothing.
    """

    def _at(self, n):
        """The nth timestamp in an increasing sequence, one second apart."""
        return "2026-03-01T%02d:%02d:%02d+00:00" % (
            n // 3600, n // 60 % 60, n % 60)

    def _fill(self, count, job="scene-scan", first=0):
        """Start and finish `count` runs; return their ids, oldest first.

        `first` offsets the timestamps, so a test can put rows of its own
        before or after the block without any of them colliding.
        """
        ids = []
        for n in range(first, first + count):
            run_id = self.store.start_run(job, trigger="scheduled",
                                          at=self._at(n))
            self.store.finish_run(run_id, outcome="completed", at=self._at(n))
            ids.append(run_id)
        return ids


class TheRunLog(_RunLogCase):
    """One row per run, kept.

    `producer_run` answers the scheduler's question -- how long ago -- with an
    upsert, so it holds one row per producer however many times it runs. That
    is the right answer for a scheduler and the wrong one for a person asking
    "did last night's pass run, and what did it find", because the answer to
    the second is made of the runs the upsert threw away.
    """

    def test_two_runs_of_one_job_are_two_rows(self):
        """The property the second table exists for. One examination cannot
        tell an insert from an upsert -- both leave a row that reads back
        correctly. Only the second run can."""
        first = self.store.start_run("scene-scan", trigger="scheduled")
        self.store.finish_run(first, outcome="completed", counts={"proposed": 2})
        second = self.store.start_run("scene-scan", trigger="manual")
        self.store.finish_run(second, outcome="completed", counts={"proposed": 3})
        rows = self.store.recent_runs()
        # The id identifies the RUN, not the job. Asserted on its own because
        # the list comparison below cannot see two ids that are equal -- it
        # would compare a pair of identical strings against itself and pass.
        self.assertNotEqual(first, second)
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual([r["trigger"] for r in rows], ["manual", "scheduled"])
        self.assertEqual([r["counts"] for r in rows],
                         [{"proposed": 3}, {"proposed": 2}])

    def test_two_runs_inside_one_second_are_still_ordered_by_arrival(self):
        """`_utcnow` has one-second resolution, so two runs of one job started
        in the same second carry an identical `started`. Ordering on that
        column alone leaves the tie to SQLite, which resolves it oldest-first
        -- the wrong end. Given explicitly here rather than left to the clock,
        so the collision happens every time rather than almost every time."""
        same = "2026-03-01T04:00:00+00:00"
        first = self.store.start_run("scene-scan", trigger="scheduled", at=same)
        second = self.store.start_run("scene-scan", trigger="scheduled", at=same)
        self.assertEqual([r["id"] for r in self.store.recent_runs()],
                         [second, first])

    def test_a_finished_run_reads_back_whole(self):
        """Asserted as a whole dict rather than field by field: a field-by-field
        check cannot see a field that should not be there, and this row is
        handed straight to a page that renders what it is given."""
        run_id = self.store.start_run("scene-scan", trigger="scheduled",
                                      at="2026-03-01T03:00:00+00:00")
        self.store.finish_run(run_id, outcome="completed",
                              counts={"proposed": 4, "refused": 9},
                              at="2026-03-01T03:04:00+00:00")
        self.assertEqual(self.store.recent_runs(), [{
            "id": run_id,
            "job": "scene-scan",
            "trigger": "scheduled",
            "started": "2026-03-01T03:00:00+00:00",
            "finished": "2026-03-01T03:04:00+00:00",
            "outcome": "completed",
            "counts": {"proposed": 4, "refused": 9},
            "error": None,
        }])

    def test_a_failed_run_is_recorded_with_its_error(self):
        """A failed run is recorded exactly as a completed one is. "Did last
        night's scan run?" is the question this log exists to answer, and a log
        of successes answers the opposite one -- it makes a job that has failed
        every night for a week look like one nobody ever scheduled."""
        run_id = self.store.start_run("tag-scan", trigger="scheduled",
                                      at="2026-03-01T03:00:00+00:00")
        self.store.finish_run(run_id, outcome="failed",
                              error="the store refused",
                              at="2026-03-01T03:00:01+00:00")
        self.assertEqual(self.store.recent_runs(), [{
            "id": run_id,
            "job": "tag-scan",
            "trigger": "scheduled",
            "started": "2026-03-01T03:00:00+00:00",
            "finished": "2026-03-01T03:00:01+00:00",
            "outcome": "failed",
            "counts": {},
            "error": "the store refused",
        }])

    def test_an_open_run_is_visible_with_nothing_filled_in(self):
        """A run that has been started and not finished -- one still going, or
        one whose process died mid-run -- is a row a reader can see, not an
        absence they have to infer from a gap in the list."""
        run_id = self.store.start_run("tag-scan", trigger="manual",
                                      at="2026-03-01T02:00:00+00:00")
        self.assertEqual(self.store.recent_runs(), [{
            "id": run_id,
            "job": "tag-scan",
            "trigger": "manual",
            "started": "2026-03-01T02:00:00+00:00",
            "finished": None,
            "outcome": None,
            "counts": {},
            "error": None,
        }])

    def test_the_start_time_defaults_to_now(self):
        """Bracketed rather than compared to a fixture, because the point is
        that an omitted `at` is a real UTC clock reading and not the empty
        string or the epoch."""
        before = datetime.now(timezone.utc).replace(microsecond=0)
        self.store.start_run("scene-scan", trigger="scheduled")
        after = datetime.now(timezone.utc)
        started = datetime.fromisoformat(self.store.recent_runs()[0]["started"])
        self.assertIsNotNone(started.tzinfo)
        self.assertLessEqual(before, started)
        self.assertLessEqual(started, after)

    def test_the_finish_time_defaults_to_now(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        run_id = self.store.start_run("scene-scan", trigger="scheduled")
        self.store.finish_run(run_id, outcome="completed")
        after = datetime.now(timezone.utc)
        finished = datetime.fromisoformat(
            self.store.recent_runs()[0]["finished"])
        self.assertIsNotNone(finished.tzinfo)
        self.assertLessEqual(before, finished)
        self.assertLessEqual(finished, after)

    def test_runs_of_different_jobs_are_all_kept(self):
        first = self.store.start_run("scene-scan", trigger="scheduled",
                                     at=self._at(0))
        second = self.store.start_run("tag-scan", trigger="scheduled",
                                      at=self._at(1))
        self.assertEqual([(r["id"], r["job"]) for r in self.store.recent_runs()],
                         [(second, "tag-scan"), (first, "scene-scan")])

    def test_a_run_with_no_job_is_refused_rather_than_stored(self):
        """A row nobody can attribute is worse than no row: a reader groups
        these by job, and a missing one renders as a blank line that looks
        like a run of something. Refused at the column, and the store is
        usable afterwards rather than left holding a half-written row."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.start_run(None, trigger="scheduled")
        self.assertEqual(self.store.recent_runs(), [])
        run_id = self.store.start_run("scene-scan", trigger="scheduled")
        self.assertEqual([r["id"] for r in self.store.recent_runs()], [run_id])

    def test_a_run_is_not_a_proposal(self):
        """The log and the inbox are independent: a run must not appear in the
        inbox, and must not disturb the counts a badge reads."""
        run_id = self.store.start_run("scene-scan", trigger="scheduled")
        self.store.finish_run(run_id, outcome="completed",
                              counts={"proposed": 3})
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.store.counts(), {})


class TheTriggerIsChecked(_RunLogCase):
    """How a run began is stored, not inferred -- and only a value a reader can
    interpret is allowed in."""

    def test_an_unknown_trigger_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.store.start_run("scene-scan", trigger="cron")
        self.assertIn("cron", str(caught.exception))

    def test_a_refused_trigger_writes_nothing(self):
        """Refused before the insert, not after it. A row carrying a trigger
        nobody can interpret is worse than no row: the summary renders what it
        is given."""
        with self.assertRaises(ValueError):
            self.store.start_run("scene-scan", trigger="cron")
        self.assertEqual(self.store.recent_runs(), [])

    def test_a_scheduled_trigger_is_accepted(self):
        """The permissive side of the guard, pinned separately. A check that
        drifted too strict would refuse every real run, and nothing asserting
        only the refusal could see it."""
        self.store.start_run("scene-scan", trigger="scheduled")
        self.assertEqual(self.store.recent_runs()[0]["trigger"], "scheduled")

    def test_a_manual_trigger_is_accepted(self):
        self.store.start_run("scene-scan", trigger="manual")
        self.assertEqual(self.store.recent_runs()[0]["trigger"], "manual")

    def test_the_message_names_what_is_allowed(self):
        """A refusal a caller cannot act on costs the same as one they can."""
        with self.assertRaises(ValueError) as caught:
            self.store.start_run("scene-scan", trigger="")
        for allowed in RUN_TRIGGERS:
            self.assertIn(allowed, str(caught.exception))


class RunRetention(_RunLogCase):
    """The log is bounded, and says what the bound dropped.

    Silent truncation reads as "this is everything" when it is not: a reader
    who cannot see that the list was cut concludes the missing runs never
    happened, which is the opposite of what the log is for.
    """

    def test_nothing_is_evicted_at_the_bound(self):
        """The permissive side. Eviction that started one row early would drop
        a run nobody asked it to, and a test that only ever looks past the
        bound cannot see it."""
        ids = self._fill(RUN_HISTORY_LIMIT)
        self.assertEqual([r["id"] for r in self.store.recent_runs(limit=10_000)],
                         list(reversed(ids)))
        self.assertEqual(self.store.runs_evicted(), 0)

    def test_retention_keeps_the_newest_and_drops_the_oldest(self):
        """Which rows survive, not merely how many. A count alone is satisfied
        by eviction from either end, and dropping the newest is the failure
        that would make the page answer "did last night's pass run" with last
        month's."""
        ids = self._fill(RUN_HISTORY_LIMIT + 3)
        self.assertEqual([r["id"] for r in self.store.recent_runs(limit=10_000)],
                         list(reversed(ids[3:])))

    def test_retention_says_how_many_it_dropped(self):
        self._fill(RUN_HISTORY_LIMIT + 3)
        self.assertEqual(self.store.runs_evicted(), 3)

    def test_a_run_left_open_is_not_evicted_by_the_ones_that_finish_around_it(self):
        """The row a worker's `finally` is about to close must still be there
        when it gets there.

        Reachable, not hypothetical: a job open while `RUN_HISTORY_LIMIT`
        others complete would have had its row deleted underneath it, and
        `finish_run` would then have matched nothing and closed nothing --
        silently, because it is called from the worker's `finally`, where
        raising would replace the producer's own exception with a store error.
        The case is removed rather than reported: an unfinished run is by
        definition still happening, and a log that drops the run in progress
        answers "what has happened" with the one thing that has not.
        """
        open_id = self.store.start_run("scene-scan", trigger="manual",
                                       at=self._at(0))
        self._fill(RUN_HISTORY_LIMIT + 3, first=1)

        # Asserted as a whole row rather than by presence alone: what a reader
        # needs to see is a run visibly still going -- nothing filled in yet --
        # rather than merely an id that is somewhere in the list.
        still_open = [r for r in self.store.recent_runs(limit=10_000)
                      if r["id"] == open_id]
        self.assertEqual(still_open, [{
            "id": open_id, "job": "scene-scan", "trigger": "manual",
            "started": self._at(0), "finished": None, "outcome": None,
            "counts": {}, "error": None}])
        # And closing it is a real write, not the silent no-op an already
        # deleted row would have made of it: the row is there to be closed,
        # so this call finds it and retention then judges it exactly as it
        # judges every other finished row -- by age. It is the oldest, and
        # the log is full, so this is the call that drops it, and the count
        # says so.
        before = self.store.runs_evicted()
        self.store.finish_run(open_id, outcome="completed", at=self._at(9000))
        self.assertEqual(self.store.runs_evicted(), before + 1)

    def test_a_run_open_across_a_turnover_of_the_whole_log_still_reads_back(self):
        """The other half: closing it writes what it was given.

        The run above is the oldest row there is, so its own close is the
        call that retires it and nothing can be read back afterwards. Here it
        sits inside the surviving window instead, with the log turning over
        well past its bound around it, so the close can be asserted whole.
        """
        self._fill(300)
        open_id = self.store.start_run("tag-scan", trigger="manual",
                                       at=self._at(300))
        self._fill(400, first=301)
        # The log really did turn over past its bound while that row was open.
        self.assertGreater(self.store.runs_evicted(), 0)

        self.store.finish_run(open_id, outcome="failed",
                              counts={"recorded": 2, "skipped": 1},
                              error="the box refused", at=self._at(9000))

        closed = [r for r in self.store.recent_runs(limit=10_000)
                  if r["id"] == open_id]
        self.assertEqual(closed, [{
            "id": open_id, "job": "tag-scan", "trigger": "manual",
            "started": self._at(300), "finished": self._at(9000),
            "outcome": "failed", "counts": {"recorded": 2, "skipped": 1},
            "error": "the box refused"}])

    def test_open_runs_do_not_spend_the_bound_the_finished_ones_are_kept_under(self):
        """Open rows are out of the count as well as out of the deletion.

        Excluding them from the DELETE alone would leave them consuming the
        bound, so a backlog of open runs -- what a restart leaves behind --
        would evict finished runs to make room for runs that have not
        produced an answer yet, which is the wrong half to throw away.
        """
        finished = self._fill(RUN_HISTORY_LIMIT)
        open_ids = [self.store.start_run("scene-scan", trigger="manual",
                                         at=self._at(500 + n))
                    for n in range(3)]
        last = self.store.start_run("scene-scan", trigger="scheduled",
                                    at=self._at(600))
        self.store.finish_run(last, outcome="completed", at=self._at(600))

        # Exactly one row over the bound, so exactly the oldest FINISHED run
        # goes and nothing else does -- which is what makes this a claim
        # about which rows survive rather than only about how many.
        self.assertEqual(
            [r["id"] for r in self.store.recent_runs(limit=10_000)],
            [last] + list(reversed(open_ids)) + list(reversed(finished[1:])))
        self.assertEqual(self.store.runs_evicted(), 1)

    def test_the_boundary_falling_inside_a_tied_second_drops_the_earlier_arrivals(self):
        """`started` has one-second resolution, so the edge of the bound can
        land in the middle of a tie. Ordering the eviction by `started` alone
        leaves which of the tied rows survives to SQLite, and SQLite keeps the
        one that arrived FIRST -- dropping two runs that happened after the
        one it kept."""
        tied = self._at(0)
        tied_ids = []
        for _ in range(3):
            run_id = self.store.start_run("scene-scan", trigger="scheduled",
                                          at=tied)
            self.store.finish_run(run_id, outcome="completed", at=tied)
            tied_ids.append(run_id)
        # Two rows over the bound, so exactly two of the three tied rows go
        # and the third stays -- which is what makes "the earlier arrivals"
        # an observable claim rather than "all of them".
        self._fill(RUN_HISTORY_LIMIT - 1, first=1)
        kept = {r["id"] for r in self.store.recent_runs(limit=10_000)}
        self.assertEqual(len(kept), RUN_HISTORY_LIMIT)
        self.assertEqual(self.store.runs_evicted(), 2)
        self.assertNotIn(tied_ids[0], kept)
        self.assertNotIn(tied_ids[1], kept)
        self.assertIn(tied_ids[2], kept)

    def test_evictions_from_separate_calls_add_up(self):
        """The other half of the same rule: a later drop adds to the total
        rather than restating it."""
        self._fill(RUN_HISTORY_LIMIT + 1)
        self.assertEqual(self.store.runs_evicted(), 1)
        run_id = self.store.start_run("scene-scan", trigger="scheduled",
                                      at=self._at(9000))
        self.store.finish_run(run_id, outcome="completed", at=self._at(9000))
        self.assertEqual(self.store.runs_evicted(), 2)

    def test_nothing_dropped_is_zero_not_missing(self):
        self.assertEqual(self.store.runs_evicted(), 0)

    def test_the_bound_covers_a_meaningful_stretch_of_nightly_runs(self):
        """A property of the bound rather than a copy of it. Restating the
        number here would move with it and prove only that the code agrees
        with itself; what actually matters is that the log can still answer
        "has this been failing all week". A bound of a handful of rows would
        be a log that forgets faster than anyone looks at it, and one of zero
        would evict every run the moment it finished."""
        self.assertGreaterEqual(RUN_HISTORY_LIMIT, 90)


class ClosingRunsInterruptedByARestart(_RunLogCase):
    """`close_interrupted_runs` -- the fix for the residual `finish_run`
    documents: nothing else ever closes a row left open by a process that
    has stopped existing.
    """

    def _open_foreign(self, job="scene-scan", trigger="scheduled", at=None):
        """Open a row directly against the connection, bypassing
        `start_run`'s own bookkeeping of what THIS `Store` instance opened.

        `_RunLogCase` shares one `self.store` across a whole test, so a row
        meant to stand in for a DIFFERENT, dead process's leftover must not
        go through `self.store.start_run` -- that would record it as this
        instance's own and defeat the very thing under test. This is the
        fixture's substitute for the real scenario, which
        `AnOrphanedRunIsClosedOnlyByTheNextProcessToOpenTheStore` covers with
        an actual second process.
        """
        run_id = str(uuid.uuid4())
        when = at if at is not None else self._at(0)
        self.store._conn.execute(
            "INSERT INTO run (id, job, trigger, started) VALUES (?, ?, ?, ?)",
            (run_id, job, trigger, when))
        self.store._conn.commit()
        return run_id

    def test_a_row_open_at_startup_is_closed_as_interrupted(self):
        orphan = self._open_foreign(at=self._at(0))
        n = self.store.close_interrupted_runs(at=self._at(100))
        self.assertEqual(n, 1)
        # The whole row, not a sampled field: a mutation recording the
        # outcome as "completed" or "failed" instead of the new value would
        # still leave `finished` set, so asserting presence alone could not
        # catch it.
        self.assertEqual(self.store.recent_runs(), [{
            "id": orphan, "job": "scene-scan", "trigger": "scheduled",
            "started": self._at(0), "finished": self._at(100),
            "outcome": RUN_OUTCOME_INTERRUPTED, "counts": {}, "error": None}])

    def test_the_outcome_is_neither_completed_nor_failed(self):
        # `RUN_OUTCOME_INTERRUPTED` itself, asserted directly, so a rename of
        # the constant to either existing outcome is caught even if some
        # other test's fixture happened to use the same literal.
        self.assertNotIn(RUN_OUTCOME_INTERRUPTED, ("completed", "failed"))
        orphan = self._open_foreign()
        self.store.close_interrupted_runs()
        row = self.store.recent_runs()[0]
        self.assertEqual(row["id"], orphan)
        self.assertNotEqual(row["outcome"], "completed")
        self.assertNotEqual(row["outcome"], "failed")

    def test_every_row_left_open_is_counted_and_closed(self):
        orphans = [self._open_foreign(at=self._at(n)) for n in range(3)]
        n = self.store.close_interrupted_runs(at=self._at(100))
        self.assertEqual(n, 3)
        closed = {r["id"]: r["outcome"] for r in self.store.recent_runs()}
        for orphan in orphans:
            self.assertEqual(closed[orphan], RUN_OUTCOME_INTERRUPTED)

    def test_a_run_this_process_itself_started_is_never_closed_by_this_call(
            self):
        """The mutation that turns the fix into an outage: closing a run the
        CURRENT process opened.

        The order here is deliberate -- `start_run` first, THEN the
        reconciliation -- because that is the shape of the one mutation that
        matters: reconciliation wired to fire again after this process has
        already started work of its own (a per-run cadence, or simply called
        twice) rather than exactly once before anything is opened. A rule
        keyed only on "is this row open" cannot tell such a row from a dead
        process's leftover; this one is keyed on THIS instance's own record
        of what it opened, which a late or repeated call cannot fool.
        """
        current = self.store.start_run("scene-scan", trigger="scheduled")
        self.store.close_interrupted_runs()
        row = self.store.recent_runs()[0]
        self.assertEqual(row["id"], current)
        self.assertIsNone(row["finished"])
        self.assertIsNone(row["outcome"])

    def test_a_run_this_process_started_survives_even_alongside_a_real_orphan(
            self):
        # The two rows are otherwise indistinguishable -- same job, both
        # open -- so this is a claim about WHICH one closes, not merely how
        # many do.
        orphan = self._open_foreign(at=self._at(0))
        current = self.store.start_run("scene-scan", trigger="scheduled",
                                       at=self._at(1))
        n = self.store.close_interrupted_runs(at=self._at(100))
        self.assertEqual(n, 1)
        rows = {r["id"]: r for r in self.store.recent_runs()}
        self.assertEqual(rows[orphan]["outcome"], RUN_OUTCOME_INTERRUPTED)
        self.assertIsNone(rows[current]["outcome"])
        self.assertIsNone(rows[current]["finished"])

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self._open_foreign(at=self._at(0))
        first = self.store.close_interrupted_runs(at=self._at(100))
        self.assertEqual(first, 1)
        before = self.store.recent_runs()
        second = self.store.close_interrupted_runs(at=self._at(200))
        self.assertEqual(second, 0)
        self.assertEqual(self.store.recent_runs(), before)

    def test_running_it_twice_changes_nothing_with_a_current_run_open_too(
            self):
        # The other branch of the same method: at least one id THIS instance
        # opened is on record, which takes a different query path (the `id
        # NOT IN (...)` exclusion) than the case above, where none is.
        self._open_foreign(at=self._at(0))
        current = self.store.start_run("scene-scan", trigger="scheduled",
                                       at=self._at(1))
        first = self.store.close_interrupted_runs(at=self._at(100))
        self.assertEqual(first, 1)
        before = self.store.recent_runs()
        second = self.store.close_interrupted_runs(at=self._at(200))
        self.assertEqual(second, 0)
        self.assertEqual(self.store.recent_runs(), before)
        still_open = [r for r in before if r["id"] == current][0]
        self.assertIsNone(still_open["finished"])

    def test_nothing_open_is_a_no_op(self):
        run_id = self.store.start_run("scene-scan", trigger="scheduled")
        self.store.finish_run(run_id, outcome="completed")
        n = self.store.close_interrupted_runs()
        self.assertEqual(n, 0)
        self.assertEqual(self.store.recent_runs()[0]["outcome"], "completed")

    def test_a_finished_row_is_left_exactly_as_it_was(self):
        run_id = self.store.start_run("scene-scan", trigger="manual",
                                      at=self._at(0))
        self.store.finish_run(run_id, outcome="failed", error="broke",
                              counts={"recorded": 2}, at=self._at(5))
        before = self.store.recent_runs()
        self.store.close_interrupted_runs(at=self._at(100))
        self.assertEqual(self.store.recent_runs(), before)

    def test_a_closed_interrupted_row_becomes_an_ordinary_retention_candidate(
            self):
        """Once closed here, the row is finished, and retention cannot tell
        it apart from any other finished row -- see `Store.finish_run`'s own
        eviction, which this method deliberately leaves untouched and lets
        the next real close apply on its own cadence."""
        self._fill(RUN_HISTORY_LIMIT - 1)
        self._open_foreign(at=self._at(RUN_HISTORY_LIMIT - 1))
        self.store.close_interrupted_runs(at=self._at(RUN_HISTORY_LIMIT))
        # Exactly at the bound -- nothing to evict yet.
        self.assertEqual(self.store.runs_evicted(), 0)
        run_id = self.store.start_run("scene-scan", trigger="scheduled",
                                      at=self._at(9000))
        self.store.finish_run(run_id, outcome="completed", at=self._at(9000))
        self.assertEqual(self.store.runs_evicted(), 1)


class AnOrphanedRunIsClosedOnlyByTheNextProcessToOpenTheStore(
        unittest.TestCase):
    """The literal scenario: one process opens a run and disappears; a later
    one, on the same database file, is the one that closes it."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def test_a_row_an_earlier_process_left_open_is_closed_by_a_fresh_store(
            self):
        with Store(self.path) as dead_process:
            orphan = dead_process.start_run(
                "scene-scan", trigger="scheduled",
                at="2026-03-01T01:06:40+00:00")
            # No finish_run -- the process is gone, not merely slow.
        with Store(self.path) as new_process:
            n = new_process.close_interrupted_runs(
                at="2026-03-01T02:01:13+00:00")
            self.assertEqual(n, 1)
            self.assertEqual(new_process.recent_runs(), [{
                "id": orphan, "job": "scene-scan", "trigger": "scheduled",
                "started": "2026-03-01T01:06:40+00:00",
                "finished": "2026-03-01T02:01:13+00:00",
                "outcome": RUN_OUTCOME_INTERRUPTED, "counts": {},
                "error": None}])

    def test_a_run_the_new_process_starts_itself_is_unaffected(self):
        with Store(self.path) as dead_process:
            dead_process.start_run("scene-scan", trigger="scheduled",
                                   at="2026-03-01T01:06:40+00:00")
        with Store(self.path) as new_process:
            new_process.close_interrupted_runs(
                at="2026-03-01T02:01:13+00:00")
            current = new_process.start_run(
                "scene-scan", trigger="scheduled",
                at="2026-03-01T02:01:20+00:00")
            row = [r for r in new_process.recent_runs()
                  if r["id"] == current][0]
            self.assertIsNone(row["finished"])
            self.assertIsNone(row["outcome"])


class ReadingRecentRuns(_RunLogCase):
    def test_recent_runs_defaults_to_twenty(self):
        ids = self._fill(25)
        self.assertEqual([r["id"] for r in self.store.recent_runs()],
                         list(reversed(ids))[:20])

    def test_recent_runs_honours_an_explicit_limit(self):
        ids = self._fill(25)
        self.assertEqual([r["id"] for r in self.store.recent_runs(limit=3)],
                         list(reversed(ids))[:3])

    def test_no_runs_is_an_empty_list(self):
        self.assertEqual(self.store.recent_runs(), [])


class TheRunLogDoesNotDisturbTheScheduler(_RunLogCase):
    """Two tables, two questions. The scheduler's answer must not move because
    the log gained a row, and must not be evicted because the log is bounded.
    """

    def test_the_scheduler_read_is_unchanged_by_the_log(self):
        self.store.record_run("scene-scan", at="2026-01-01T00:00:00+00:00")
        run_id = self.store.start_run("scene-scan", trigger="manual",
                                      at="2026-03-01T00:00:00+00:00")
        self.store.finish_run(run_id, outcome="completed")
        self.assertEqual(self.store.runs(),
                         {"scene-scan": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(self.store.last_run("scene-scan"),
                         "2026-01-01T00:00:00+00:00")

    def test_the_scheduler_answer_outlives_the_bound(self):
        """The log is evicted; `producer_run` is not. A shared table would have
        to pick one lifetime, and the scheduler losing its last-run answer
        makes every job due at once."""
        self.store.record_run("scene-scan", at="2026-01-01T00:00:00+00:00")
        self._fill(RUN_HISTORY_LIMIT + 3)
        self.assertEqual(self.store.runs(),
                         {"scene-scan": "2026-01-01T00:00:00+00:00"})

    def test_the_log_is_not_written_by_the_scheduler_read(self):
        self.store.record_run("scene-scan", at="2026-01-01T00:00:00+00:00")
        self.assertEqual(self.store.recent_runs(), [])


class TheRunLogSurvivesARestart(unittest.TestCase):
    """The property the table exists for, across a real close and reopen.

    Everything above would pass just as well against a list held on the
    instance; only this can tell the two apart.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def test_a_run_recorded_before_a_restart_is_still_there_after_it(self):
        with Store(self.path) as store:
            run_id = store.start_run("scene-scan", trigger="scheduled",
                                     at="2026-03-01T03:00:00+00:00")
            store.finish_run(run_id, outcome="completed",
                             counts={"proposed": 2},
                             at="2026-03-01T03:05:00+00:00")
        with Store(self.path) as store:
            self.assertEqual(store.recent_runs(), [{
                "id": run_id,
                "job": "scene-scan",
                "trigger": "scheduled",
                "started": "2026-03-01T03:00:00+00:00",
                "finished": "2026-03-01T03:05:00+00:00",
                "outcome": "completed",
                "counts": {"proposed": 2},
                "error": None,
            }])

    def test_the_eviction_count_survives_a_restart(self):
        """The rows it counts are gone, so nothing can recount them. A total
        held in memory would reset to zero on every restart and report a
        complete list that is not one."""
        with Store(self.path) as store:
            for n in range(RUN_HISTORY_LIMIT + 2):
                run_id = store.start_run(
                    "scene-scan", trigger="scheduled",
                    at="2026-03-01T%02d:%02d:%02d+00:00" % (
                        n // 3600, n // 60 % 60, n % 60))
                store.finish_run(run_id, outcome="completed")
            self.assertEqual(store.runs_evicted(), 2)
        with Store(self.path) as store:
            self.assertEqual(store.runs_evicted(), 2)


class RunTableAddedOnAnExistingDatabase(unittest.TestCase):
    """A database written before the run table existed gains it on open, and
    keeps everything already in it.

    The run log is a new TABLE, not a new column, so `CREATE TABLE IF NOT
    EXISTS` carries it and `SCHEMA_VERSION` is deliberately not bumped: a bump
    would make every build predating this table refuse a database it can still
    read correctly.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "old.sqlite3")
        self.addCleanup(shutil.rmtree, self._dir, True)
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE producer_run (producer TEXT PRIMARY KEY, "
            "at TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO producer_run VALUES ('scene-scan', "
            "'2026-01-01T00:00:00+00:00')")
        connection.commit()
        connection.close()

    def test_the_older_database_still_opens_and_keeps_its_rows(self):
        with Store(self.path) as store:
            self.assertEqual(store.runs(),
                             {"scene-scan": "2026-01-01T00:00:00+00:00"})

    def test_its_run_log_starts_empty_rather_than_missing(self):
        with Store(self.path) as store:
            self.assertEqual(store.recent_runs(), [])
            self.assertEqual(store.runs_evicted(), 0)

    def test_it_can_be_written_to_at_once(self):
        with Store(self.path) as store:
            run_id = store.start_run("scene-scan", trigger="scheduled",
                                     at="2026-03-01T03:00:00+00:00")
            store.finish_run(run_id, outcome="completed",
                             at="2026-03-01T03:01:00+00:00")
            self.assertEqual([r["id"] for r in store.recent_runs()], [run_id])

    def test_the_schema_version_is_not_bumped_by_the_new_table(self):
        with Store(self.path):
            pass
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        stamped = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(stamped, SCHEMA_VERSION)


class SchemaAdditionOnAnExistingDatabase(unittest.TestCase):
    """A database written before `producer_run` existed gains it on open, and
    keeps everything already in it.

    The store's spec defers migrations on the grounds that there is no user
    data to preserve. There now is — proposals, dismissals and mutes, on
    anyone who has run this — so adding a table is the moment that deferral
    has to be checked rather than quietly extended. What makes it safe is that
    the whole schema is re-applied at every open and every statement in it is
    `IF NOT EXISTS`, so the script is a no-op for what is already there.

    The older database is emulated by dropping `producer_run` from one the
    current code created, which leaves exactly the tables the previous code
    created. That equivalence was checked against a database actually built by
    the previous code: item, dismissal and mute rows came back identical and
    `PRAGMA integrity_check` reported ok.

    The claim is only about *additive* change. Nothing here says anything about
    altering or dropping a column that already holds data; that would need a
    version table, and this does not provide one.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.snapshot, self.dismissed_fp = self._make_database_without_the_table()

    def _make_database_without_the_table(self):
        with Store(self.path) as store:
            seen = store.record(folder="scene-matches", subject_type="scene",
                                subject_id="1", summary="a proposal",
                                payload={"title": "Copper Kettle"},
                                producer="nightly-scrape", confidence=0.9)
            store.mark_seen(seen)
            store.record(folder="tag-matches", subject_type="scene",
                         subject_id="3", summary="a third proposal",
                         payload={"tags": ["kettle"]}, producer="hourly-tags")
            rejected = store.record(folder="scene-matches",
                                    subject_type="scene", subject_id="2",
                                    summary="another proposal",
                                    payload={"title": "Harbour Lights"},
                                    producer="nightly-scrape")
            store.dismiss(rejected, reason="wrong match")
            store.mute("scene", "9", reason="never identifiable")
            snapshot = {
                "visible": store.items(),
                "dismissed": store.items(state="dismissed"),
                "muted_rows": store.items(state="muted"),
                "muted_subjects": sorted(store.muted_subjects()),
                "counts": store.counts(),
            }
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE producer_run")
        connection.commit()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        connection.close()
        # the emulation is worth nothing if the table is still there
        self.assertNotIn("producer_run", tables)
        return snapshot, rejected

    def test_the_missing_table_is_created_on_open(self):
        with Store(self.path) as store:
            self.assertIsNone(store.last_run("nightly-scrape"))
            store.record_run("nightly-scrape", at="2026-07-26T02:00:00+00:00")
            self.assertEqual(store.runs(),
                             {"nightly-scrape": "2026-07-26T02:00:00+00:00"})

    def test_every_existing_row_survives_the_addition_unchanged(self):
        """Whole rows, every state, not a sampled field: a column blanked or an
        extra row invented by the reopen is exactly what this has to see."""
        with Store(self.path) as store:
            after = {
                "visible": store.items(),
                "dismissed": store.items(state="dismissed"),
                "muted_rows": store.items(state="muted"),
                "muted_subjects": sorted(store.muted_subjects()),
                "counts": store.counts(),
            }
        self.assertEqual(after, self.snapshot)

    def test_the_surviving_rows_are_not_empty(self):
        """Guards the test above from passing by comparing nothing to nothing —
        a reopen that lost every row would satisfy an equality of two empty
        snapshots if the fixture had silently failed to write."""
        self.assertEqual(len(self.snapshot["visible"]), 2)
        self.assertEqual(len(self.snapshot["dismissed"]), 1)
        self.assertEqual(self.snapshot["muted_subjects"], [("scene", "9")])
        self.assertEqual(self.snapshot["counts"], {"seen": 1, "new": 1})

    def test_a_dismissal_still_blocks_the_proposal_it_rejected(self):
        """Rows surviving is not enough — a reviewer's decision has to keep
        acting. A dismissal that stopped blocking would resurrect the rejected
        proposal on the producer's next run."""
        with Store(self.path) as store:
            again = store.record(folder="scene-matches", subject_type="scene",
                                 subject_id="2", summary="another proposal",
                                 payload={"title": "Harbour Lights"},
                                 producer="nightly-scrape")
            self.assertEqual(again, self.dismissed_fp)
            self.assertFalse(store.has(again))

    def test_a_mute_still_blocks_its_subject(self):
        with Store(self.path) as store:
            store.record(folder="scene-matches", subject_type="scene",
                         subject_id="9", summary="about a muted subject",
                         payload={"title": "Anything At All"},
                         producer="nightly-scrape")
            self.assertEqual(store.items(), self.snapshot["visible"])


class MarkingASubjectGone(_StoreCase):
    """A subject the media server no longer holds.

    Every assertion here guards the same harm from a different side: mutes,
    dismissals and refusals exist so a scan cannot overrule a person, and the
    controls this hides would each write to an id the server does not have.
    What must NOT happen is any of them being destroyed on the way.
    """

    def _refuse(self, subject_id="1"):
        self.store.record_refusal("scene", subject_id, "/l/%s.mp4" % subject_id,
                                  "nothing over the threshold")

    # -- the state on an item row ------------------------------------------ #

    def test_a_marked_row_leaves_the_default_view(self):
        self._record(subject_id="1")
        self._record(subject_id="2")
        self.store.mark_gone("scene", "1")
        self.assertEqual([item["subject_id"] for item in self.store.items()],
                         ["2"])

    def test_and_is_still_readable_by_asking_for_that_state(self):
        # "Marked, not removed" is the whole decision. A row that left every
        # list including its own is a row that was deleted with extra steps.
        fp = self._record(subject_id="1")
        self.store.mark_gone("scene", "1")
        rows = self.store.items(state="gone")
        self.assertEqual([row["fingerprint"] for row in rows], [fp])
        self.assertEqual(rows[0]["payload"], {"title": "Copper Kettle"})

    def test_the_badge_count_stops_counting_it(self):
        self._record(subject_id="1")
        self._record(subject_id="2")
        self._record(subject_id="3")
        self.store.mark_gone("scene", "2")
        self.assertEqual(self.store.counts(), {"new": 2})

    def test_only_the_named_subject_is_touched(self):
        # Asymmetric on purpose: with one subject in the fixture, a WHERE
        # clause dropped altogether looks identical to one that works.
        keep_a = self._record(subject_id="1")
        keep_b = self._record(subject_id="2")
        self._record(subject_id="3")
        self.store.mark_gone("scene", "3")
        self.assertEqual(
            sorted(item["fingerprint"] for item in self.store.items()),
            sorted([keep_a, keep_b]))

    def test_a_subject_of_another_type_with_the_same_id_is_not_touched(self):
        # HARM: subject ids are per-kind. A tag numbered 1 and a scene
        # numbered 1 are unrelated, and a sweep of deleted SCENES that also
        # hid tags would take out a population it never looked at.
        self.store.record(folder="tag-matches", subject_type="tag-cluster",
                          subject_id="1", summary="a cluster",
                          payload={"key": "kettle"}, producer="tags")
        self._record(subject_id="1")
        self.store.mark_gone("scene", "1")
        self.assertEqual([item["subject_type"] for item in self.store.items()],
                         ["tag-cluster"])

    def test_an_int_subject_id_marks_the_same_row_a_string_one_does(self):
        # HARM: ids arrive from an API as whatever the API sends. A store that
        # keyed one row by "1" and another by 1 would mark neither.
        self._record(subject_id="1")
        self.store.mark_gone("scene", 1)
        self.assertEqual(self.store.items(), [])

    # -- what it must not destroy ------------------------------------------ #

    def test_an_applied_rows_snapshot_and_resolution_time_are_untouched(self):
        # HARM: `prior_state` is the ONLY record of what an approve wrote to
        # the library, and `resolved_at` of when. Clearing either -- as a
        # "terminal state" transition plausibly would -- destroys the audit
        # trail for a write that really happened, and the file being gone is
        # exactly when that record matters most. Whole shape, not sampled
        # fields: an assertion naming `prior_state` alone would not notice
        # `resolved_at` being stamped over.
        fp = self._record(subject_id="1")
        self.store.mark_applied(fp, prior_state={"title": "was"},
                                now="2020-05-05T00:00:00")
        before = self.store.items(state="applied")[0]
        self.store.mark_gone("scene", "1")
        after = self.store.items(state="gone")[0]
        self.assertEqual(after, dict(before, state="gone"))

    def test_a_dismissal_still_blocks_the_fingerprint_it_was_made_against(self):
        # HARM: the `dismissal` table is what makes a rejection stick. Pruning
        # it here would let the identical proposal be recorded again the
        # moment anything re-recorded it.
        fp = self._record(subject_id="1")
        self.store.dismiss(fp, reason="wrong match")
        self.store.mark_gone("scene", "1")
        self.assertEqual(self.store.items(state="dismissed"), [])
        self._record(subject_id="1")
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.store.items(state="gone")[0]["fingerprint"], fp)

    def test_a_mute_still_blocks_the_subject_it_was_made_against(self):
        # HARM: the same, one table over. `muted_subjects` is what `record`
        # and `scan.select` both consult; losing the row would let a scan
        # start proposing a subject a person told it to stop offering.
        self.store.mute("scene", "1", reason="never identifiable")
        self.store.mark_gone("scene", "1")
        self.assertEqual(self.store.mutes(), [])
        self.assertEqual(self.store.muted_subjects(), {("scene", "1")})

    def test_a_refusal_is_hidden_and_kept(self):
        self._refuse("1")
        self.store.mark_gone("scene", "1")
        self.assertEqual(self.store.refusals(), [])
        rows = self.store._conn.execute(
            "SELECT subject_id FROM refusal").fetchall()
        self.assertEqual(rows, [("1",)])

    def test_a_superseded_row_is_hidden_too(self):
        # The fifth table. `supersede` frees a subject for the next scan to
        # look at, and there is nothing left to look at -- but the row must
        # still be readable, like every other kind.
        fp = self._record(subject_id="1")
        self.store.supersede(fp)
        self.store.mark_gone("scene", "1")
        self.assertEqual(self.store.items(state="superseded"), [])
        self.assertEqual(self.store.superseded_fingerprints(), {fp})

    def test_the_other_subjects_mute_and_refusal_are_left_alone(self):
        # The counterpart of `test_only_the_named_subject_is_touched`, for the
        # two tables whose filtering is a subquery rather than a state.
        self.store.mute("scene", "1")
        self.store.mute("scene", "2")
        self._refuse("3")
        self._refuse("4")
        self.store.mark_gone("scene", "1")
        self.store.mark_gone("scene", "4")
        self.assertEqual([m["subject_id"] for m in self.store.mutes()], ["2"])
        self.assertEqual([r["subject_id"] for r in self.store.refusals()], ["3"])

    def test_a_mute_and_a_refusal_for_a_present_subject_survive_a_sweep(self):
        # The direction the expensive mistake would break: marking one subject
        # must not empty either list.
        self.store.mute("scene", "5", reason="never identifiable")
        self._refuse("6")
        self.store.mark_gone("scene", "7")
        self.assertEqual(len(self.store.mutes()), 1)
        self.assertEqual(len(self.store.refusals()), 1)

    # -- the return value and the recorded moment -------------------------- #

    def test_it_reports_the_first_marking_and_not_the_second(self):
        # HARM: a sweep that counted re-confirmations would report a library's
        # whole standing gone population as newly found, every night.
        self.assertIs(self.store.mark_gone("scene", "1"), True)
        self.assertIs(self.store.mark_gone("scene", "1"), False)

    def test_the_recorded_moment_is_when_it_was_first_noticed(self):
        self.store.mark_gone("scene", "1", now="2020-01-01T00:00:00")
        self.store.mark_gone("scene", "1", now="2030-09-09T00:00:00")
        rows = self.store._conn.execute("SELECT at FROM gone").fetchall()
        self.assertEqual(rows, [("2020-01-01T00:00:00",)])

    def test_marking_again_still_hides_a_row_recorded_since(self):
        # A row that arrived between two sweeps must not stay visible just
        # because the subject was already in the table.
        self.store.mark_gone("scene", "1")
        self._record(subject_id="1")
        self.assertIs(self.store.mark_gone("scene", "1"), False)
        self.assertEqual(self.store.items(), [])

    # -- what the sweep reads ---------------------------------------------- #

    def test_subject_ids_unions_every_table_that_keys_by_subject(self):
        # HARM: a sweep reading proposals alone leaves mutes and refusals for
        # deleted files sitting in their lists forever -- exactly the defect
        # this exists to fix, half-fixed.
        self._record(subject_id="1")
        self.store.mute("scene", "2")
        self._refuse("3")
        self.assertEqual(self.store.subject_ids("scene"), {"1", "2", "3"})

    def test_subject_ids_answers_for_the_kind_it_was_asked_about(self):
        self._record(subject_id="1")
        self.store.record(folder="tag-matches", subject_type="tag-cluster",
                          subject_id="99", summary="a cluster",
                          payload={"key": "kettle"}, producer="tags")
        self.store.mute("tag-cluster", "98")
        self.assertEqual(self.store.subject_ids("scene"), {"1"})
        self.assertEqual(self.store.subject_ids("tag-cluster"), {"98", "99"})

    def test_subject_ids_still_names_a_subject_already_marked_gone(self):
        # It reports what the store HOLDS. Filtering here instead of at
        # `mark_gone` would make "newly marked" unanswerable without a race.
        self._record(subject_id="1")
        self.store.mark_gone("scene", "1")
        self.assertEqual(self.store.subject_ids("scene"), {"1"})


class GoneTableAddedOnAnExistingDatabase(unittest.TestCase):
    """The additive-change guarantee the two classes below check for
    `producer_run` and `supersede`, now for `gone`: a database written before
    this ticket gains the table on open and keeps what was already in it."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)
        with Store(self.path) as store:
            self.fp = store.record(
                folder="scene-matches", subject_type="scene", subject_id="1",
                summary="a proposal", payload={"title": "Copper Kettle"},
                producer="nightly-scrape")
            store.mute("scene", "2", reason="never identifiable")
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE gone")
        connection.commit()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        connection.close()
        self.assertNotIn("gone", tables)

    def test_the_missing_table_is_created_on_open(self):
        with Store(self.path) as store:
            self.assertIs(store.mark_gone("scene", "1"), True)
            self.assertEqual(store.items(), [])

    def test_the_pre_existing_rows_survive_the_addition(self):
        with Store(self.path) as store:
            self.assertEqual([item["fingerprint"] for item in store.items()],
                             [self.fp])
            self.assertEqual([m["subject_id"] for m in store.mutes()], ["2"])


class SupersedeTableAddedOnAnExistingDatabase(unittest.TestCase):
    """The same additive-change guarantee `SchemaAdditionOnAnExistingDatabase`
    checks for `producer_run`, now for `supersede`: a database written before
    this ticket gains the table on open and keeps everything already in it."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)
        with Store(self.path) as store:
            self.fp = store.record(
                folder="scene-matches", subject_type="scene", subject_id="1",
                summary="a proposal", payload={"title": "Copper Kettle"},
                producer="nightly-scrape")
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE supersede")
        connection.commit()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        connection.close()
        self.assertNotIn("supersede", tables)

    def test_the_missing_table_is_created_on_open(self):
        with Store(self.path) as store:
            self.assertEqual(store.superseded_fingerprints(), set())
            store.supersede(self.fp)
            self.assertEqual(store.superseded_fingerprints(), {self.fp})

    def test_the_pre_existing_row_survives_the_addition(self):
        with Store(self.path) as store:
            self.assertEqual(len(store.items()), 1)
            self.assertEqual(store.items()[0]["fingerprint"], self.fp)


class RefusalColumnAddedOnAnExistingDatabase(unittest.TestCase):
    """`refusal.stores` on a database written before that column existed.

    The additive guarantee the two classes above rely on -- re-apply the whole
    schema, every statement `IF NOT EXISTS` -- does NOT reach a new COLUMN:
    `CREATE TABLE IF NOT EXISTS refusal` is skipped whole on a database that
    already has the table, so the column named inside it never appears.
    `_add_missing_columns` is what does, and this is where a live database
    either gains the column or fails to open.

    The older shape is written out by hand below rather than derived from
    `store.SCHEMA`: a fixture built by the code it is meant to constrain moves
    whenever that code does, and would go on passing after the column was
    quietly dropped from the schema again.
    """

    # Exactly the `refusal` table as it shipped, before `stores`.
    _SHAPE_BEFORE = """
        CREATE TABLE refusal (
            subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
            path TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
            PRIMARY KEY (subject_type, subject_id))
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)
        with Store(self.path) as store:
            self.fp = store.record(
                folder="scene-matches", subject_type="scene", subject_id="1",
                summary="a proposal", payload={"title": "Copper Kettle"},
                producer="nightly-scrape")
            store.mute("scene", "9", reason="never identifiable")
        self._rewind_the_refusal_table()

    def _rewind_the_refusal_table(self):
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE refusal")
        connection.execute(self._SHAPE_BEFORE)
        connection.execute(
            "INSERT INTO refusal (subject_type, subject_id, path, reason, at) "
            "VALUES ('scene', '2', '/library/x/clip.mp4', "
            "'alpha: nothing above the threshold (0.70)', "
            "'2026-07-01T00:00:00')")
        connection.commit()
        columns = self._columns(connection)
        connection.close()
        # The emulation is worth nothing if the column is already there.
        self.assertNotIn("stores", columns)

    def _columns(self, connection):
        return {row[1] for row in connection.execute(
            "PRAGMA table_info(refusal)")}

    def _read_columns(self):
        connection = sqlite3.connect(self.path)
        try:
            return self._columns(connection)
        finally:
            connection.close()

    def _read_version(self):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    def test_the_database_opens_and_gains_the_column(self):
        """The headline: an operator's live database must not be a file this
        build refuses. Asserted on the table's own columns, because a Store
        that opened would not by itself prove the column arrived -- every
        later read would fail instead."""
        with Store(self.path):
            pass
        self.assertIn("stores", self._read_columns())

    def test_a_refusal_written_before_the_column_reads_back_whole(self):
        """The whole row. A refusal recorded before the column existed keeps
        its path, reason and timestamp, and reports the honest thing about
        the stores nobody recorded: an empty list, never a NULL a caller has
        to guess at."""
        with Store(self.path) as store:
            self.assertEqual(store.refusals(), [
                {"subject_type": "scene", "subject_id": "2",
                 "path": "/library/x/clip.mp4",
                 "reason": "alpha: nothing above the threshold (0.70)",
                 "at": "2026-07-01T00:00:00", "stores": []}])

    def test_a_refusal_recorded_after_the_upgrade_keeps_its_stores(self):
        entry = {"store": "alpha", "rows": 40, "score": 0.342,
                 "title": "Evening Ritual",
                 "url": "https://alpha.example/clip/evening-ritual",
                 "error": None}
        with Store(self.path) as store:
            store.record_refusal("scene", "3", "/library/x/other.mp4",
                                 "alpha: nothing above the threshold",
                                 stores=[entry], now="2026-07-02T00:00:00")
            recorded = {r["subject_id"]: r["stores"] for r in store.refusals()}
        self.assertEqual(recorded, {"2": [], "3": [entry]})

    def test_the_other_tables_are_untouched_by_the_addition(self):
        with Store(self.path) as store:
            self.assertEqual([i["fingerprint"] for i in store.items()],
                             [self.fp])
            self.assertEqual(store.muted_subjects(), {("scene", "9")})

    def test_reopening_twice_does_not_try_to_add_the_column_again(self):
        """`ALTER TABLE ... ADD COLUMN` is not idempotent on its own -- SQLite
        raises on a duplicate name -- so the second open is where a missing
        "does it already have it" check turns every subsequent start into an
        error."""
        with Store(self.path):
            pass
        with Store(self.path):     # must not raise
            pass
        self.assertIn("stores", self._read_columns())

    def test_the_addition_does_not_bump_the_version_stamp(self):
        """An added column with a default is invisible to code that does not
        name it, so bumping would make every earlier build refuse a database
        it can still read correctly -- a cost with nothing bought. The stamp
        is for the change that CANNOT be carried this way."""
        with Store(self.path):
            pass
        self.assertEqual(self._read_version(), SCHEMA_VERSION)

    def test_a_database_from_the_future_is_refused_before_it_is_altered(self):
        """The version guard runs FIRST. Adding a column to a shape this
        build cannot vouch for is a write into someone else's schema, and the
        refusal exists precisely to stop this code acting on one."""
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 1,))
        connection.commit()
        connection.close()

        with self.assertRaises(SchemaVersionError):
            Store(self.path)

        self.assertNotIn("stores", self._read_columns())


class SchemaVersioning(unittest.TestCase):
    """`PRAGMA user_version` is the marker a future non-additive change gets
    to branch on. It is not itself a migration mechanism -- nothing here
    knows how to carry a version 1 database forward to a version 2 shape --
    so what these tests pin is narrower: a fresh database gets stamped, a
    stamped one reopens without complaint, and a version this code does not
    recognise is refused rather than silently reinterpreted."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s.db")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def _read_version(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def _write_version(self, value):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA user_version = %d" % value)
            conn.commit()
        finally:
            conn.close()

    def test_a_freshly_created_database_is_stamped_with_the_current_version(self):
        with Store(self.path):
            pass
        self.assertEqual(self._read_version(), SCHEMA_VERSION)

    def test_reopening_a_stamped_database_does_not_raise(self):
        with Store(self.path):
            pass
        with Store(self.path):     # must not raise
            pass
        self.assertEqual(self._read_version(), SCHEMA_VERSION)

    def test_a_database_built_before_the_marker_existed_is_stamped_on_open(self):
        # Emulates exactly the population this ticket is about: a database
        # already holding a person's decisions, from before this constant
        # existed, so its user_version reads 0 -- indistinguishable from a
        # brand new file. Reopening it must stamp it, not disturb it.
        with Store(self.path) as store:
            fp = store.record(folder="scene-matches", subject_type="scene",
                              subject_id="1", summary="a proposal",
                              payload={"title": "Copper Kettle"},
                              producer="nightly-scrape", confidence=0.9)
            store.dismiss(fp, reason="wrong match")
        self._write_version(0)     # simulate: never stamped by older code
        with Store(self.path) as store:
            self.assertEqual(store.items(state="dismissed")[0]["fingerprint"], fp)
        self.assertEqual(self._read_version(), SCHEMA_VERSION)

    def test_a_newer_schema_version_refuses_to_open(self):
        with Store(self.path):
            pass
        self._write_version(SCHEMA_VERSION + 1)
        with self.assertRaises(SchemaVersionError):
            Store(self.path)

    def test_an_older_stamped_schema_version_refuses_to_open(self):
        # There is no version below the current one that this code was ever
        # built to understand -- 0 is the one exception, meaning "never
        # stamped", not "version zero of the schema" -- so anything else
        # smaller must refuse exactly as a larger one does.
        with Store(self.path):
            pass
        self._write_version(SCHEMA_VERSION - 1 if SCHEMA_VERSION > 1 else -1)
        with self.assertRaises(SchemaVersionError):
            Store(self.path)

    def test_the_refusal_names_both_versions(self):
        with Store(self.path):
            pass
        self._write_version(SCHEMA_VERSION + 1)
        with self.assertRaises(SchemaVersionError) as ctx:
            Store(self.path)
        message = str(ctx.exception)
        self.assertIn(str(SCHEMA_VERSION + 1), message)
        self.assertIn(str(SCHEMA_VERSION), message)

    def test_a_refused_open_releases_the_path_for_a_later_retry(self):
        # The single-instance-per-file guard (see `Store`'s class docstring)
        # must not mistake a refused-to-open Store for one still holding the
        # path -- otherwise fixing the file wouldn't be enough; the process
        # would also have to restart.
        with Store(self.path):
            pass
        self._write_version(SCHEMA_VERSION + 1)
        with self.assertRaises(SchemaVersionError):
            Store(self.path)
        self._write_version(SCHEMA_VERSION)
        with Store(self.path):     # must not raise "already open"
            pass


class UnmuteBringsTheRowBack(unittest.TestCase):
    """Lifting the block is only half of an unmute.

    Removing the `mute` row alone left the `item` still sitting in
    `state = 'muted'`, which `items()` filters out -- so a person clicked
    Unmute, it worked, and the page redrew completely unchanged. That is the
    same shape as an undo that recorded nothing: an action that works and
    looks broken, which gets pressed again.

    `undismiss` already restored its row; only `unmute` did not. The two are
    symmetric now.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.store = Store(os.path.join(self._dir, "s.sqlite3"))
        self.addCleanup(self.store.close)

    def _record(self, subject_id):
        return self.store.record(
            "library", "scene", subject_id, "summary",
            {"path": "/library/%s.mp4" % subject_id,
             "creator": {"name": "Nine Winters", "source": "folder",
                         "competing": None, "rejected_folder": None},
             "candidate": {"id": "c-1", "title": "A Title", "image": None},
             "score": 0.9, "runners_up": []},
            producer="test", confidence=0.9)

    def test_an_unmuted_proposal_is_visible_again(self):
        self._record("7")
        self.store.mute("scene", "7")
        self.assertEqual(len(self.store.items()), 0)
        self.store.unmute("scene", "7")
        self.assertEqual(
            len(self.store.items()), 1,
            "unmute lifted the block but left the row hidden, so the page "
            "would redraw unchanged")

    def test_an_applied_row_keeps_its_state_through_mute_and_unmute(self):
        # Muting deliberately leaves a terminal row alone -- it does not
        # un-apply a write that already happened -- so there is nothing for
        # unmute to restore. Forcing it to `new` would offer a fresh Approve
        # for something already written to the library.
        fp = self._record("8")
        self.store.mark_applied(fp, prior_state={"title": "was"})
        self.store.mute("scene", "8")
        self.store.unmute("scene", "8")
        self.assertEqual([i["state"] for i in self.store.items(state="applied")],
                         ["applied"])

    def test_unmuting_does_not_lift_a_separate_dismissal(self):
        # Different rejections. Reversing one must not quietly reverse the
        # other.
        fp = self._record("9")
        self.store.dismiss(fp)
        self.store.mute("scene", "9")
        self.store.unmute("scene", "9")
        self.assertEqual(len(self.store.items()), 0)


class SubjectTypeFiltering(_StoreCase):
    """`items()` and `counts()` narrowing to a caller-given tuple of subject
    types -- how an inbox page (see `cronicled.web.inboxes`) asks the store
    for only the rows it owns, instead of every subject type in the
    database.
    """

    def _tag(self, subject_id):
        return self.store.record(folder="tag-matches", subject_type="tag",
                                 subject_id=subject_id, summary="a tag",
                                 payload={"key": "kettle"}, producer="tags")

    def _cluster(self, subject_id):
        return self.store.record(folder="tag-matches",
                                 subject_type="tag-cluster",
                                 subject_id=subject_id, summary="a cluster",
                                 payload={"key": "kettle"}, producer="tags")

    def test_items_filters_to_the_given_subject_types(self):
        self._tag("t1")
        self._cluster("c1")
        self._record(subject_id="s1")  # subject_type="scene"
        got = {(i["subject_type"], i["subject_id"])
               for i in self.store.items(subject_types=("tag", "tag-cluster"))}
        self.assertEqual(got, {("tag", "t1"), ("tag-cluster", "c1")})

    def test_items_with_no_subject_types_is_unchanged(self):
        # The existing callers pass nothing and must keep seeing everything.
        self._tag("t1")
        self._record(subject_id="s1")
        self.assertEqual(len(self.store.items()), 2)

    def test_an_empty_subject_type_tuple_selects_nothing_in_items(self):
        # An empty tuple is a real, distinct request -- "select nothing" --
        # not "no filter given". Falling through to truthiness here would
        # make an inbox with no subject types (a bug elsewhere) silently see
        # every row instead of none.
        self._tag("t1")
        self.assertEqual(self.store.items(subject_types=()), [])

    def test_counts_filters_to_the_given_subject_types(self):
        self._tag("t1")
        self._record(subject_id="s1")  # subject_type="scene"
        self.assertEqual(self.store.counts(subject_types=("tag",)),
                         {"new": 1})

    def test_counts_with_no_subject_types_is_unchanged(self):
        self._tag("t1")
        self._record(subject_id="s1")
        self.assertEqual(self.store.counts(), {"new": 2})

    def test_an_empty_subject_type_tuple_selects_nothing_in_counts(self):
        self._tag("t1")
        self.assertEqual(self.store.counts(subject_types=()), {})

    def test_subject_type_filter_combines_with_folder(self):
        # Both narrowings apply at once, not one silently overriding the
        # other. A scene sharing the tag's own folder makes the folder
        # filter alone insufficient to produce the expected set, so this
        # can only pass if the subject_type filter is doing real work too --
        # a fixture where the folder filter alone gave the same answer would
        # let a dropped subject_type clause hide behind it.
        self._tag("t1")
        self.store.record(folder="tag-matches", subject_type="scene",
                          subject_id="s-same-folder", summary="a scene",
                          payload={"title": "invented placeholder title"},
                          producer="scan")
        self.store.record(folder="scene-matches", subject_type="tag",
                          subject_id="t2", summary="a tag",
                          payload={"key": "other"}, producer="tags")
        got = {i["subject_id"] for i in
               self.store.items(folder="tag-matches", subject_types=("tag",))}
        self.assertEqual(got, {"t1"})


class CountsGroupedBySubjectType(_StoreCase):
    """`counts_by_subject_type` is what `cronicled.__main__.waiting_counts`
    uses to find a subject type nobody named up front: unlike `counts()`,
    which only answers about subject types the caller already knows to ask
    about, this groups by subject type itself, so an uninvited one still
    turns up as a key in the result.
    """

    def _tag(self, subject_id):
        return self.store.record(folder="tag-matches", subject_type="tag",
                                 subject_id=subject_id, summary="a tag",
                                 payload={"key": "kettle"}, producer="tags")

    def test_groups_by_subject_type_not_by_state(self):
        self._record(subject_id="s1")
        self._record(subject_id="s2")
        self._tag("t1")
        self.assertEqual(self.store.counts_by_subject_type(),
                         {"scene": 2, "tag": 1})

    def test_excludes_dismissed_and_muted(self):
        dismissed_fp = self._record(subject_id="1")
        self.store.dismiss(dismissed_fp)
        self._record(subject_id="2")
        self.store.mute("scene", "2")
        self._record(subject_id="3")
        self.assertEqual(self.store.counts_by_subject_type(), {"scene": 1})

    def test_excludes_applied_even_though_counts_would_include_it(self):
        # The one place this deliberately diverges from `counts()`: an
        # applied proposal is a decision already made, not still waiting, so
        # it is dropped here in the query itself rather than left for the
        # caller to subtract state by state.
        applied_fp = self._record(subject_id="1")
        self.store.mark_applied(applied_fp)
        self._record(subject_id="2")
        self.assertEqual(self.store.counts(), {"applied": 1, "new": 1})
        self.assertEqual(self.store.counts_by_subject_type(), {"scene": 1})

    def test_an_unmapped_subject_type_still_appears_as_its_own_key(self):
        # No `subject_types` argument was given, and nothing here has to be
        # named in advance -- unlike `counts(subject_types=...)`, which can
        # only ever answer about the types it was asked about.
        self._record(subject_id="s1")
        self.store.record(folder="library", subject_type="something-new",
                          subject_id="x1", summary="s", payload={},
                          producer="test")
        self.assertEqual(self.store.counts_by_subject_type(),
                         {"scene": 1, "something-new": 1})

    def test_scoped_to_a_folder(self):
        self._record(subject_id="s1", folder="scene-matches")
        self.store.record(folder="cleanups", subject_type="scene",
                          subject_id="s2", summary="s",
                          payload={"title": "elsewhere"}, producer="test")
        self.assertEqual(
            self.store.counts_by_subject_type(folder="scene-matches"),
            {"scene": 1})

    def test_an_empty_store_reports_nothing(self):
        self.assertEqual(self.store.counts_by_subject_type(), {})
