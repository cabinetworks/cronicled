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
from datetime import datetime, timezone

from cronicled.store import SCHEMA_VERSION, SchemaVersionError, Store, fingerprint


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
             "reason": "never identifiable", "at": "2026-07-01T00:00:00"}])

    def test_nothing_muted_is_an_empty_list(self):
        self.assertEqual(self.store.mutes(), [])

    def test_unmuting_removes_it_from_the_listing(self):
        self.store.mute("scene", "1")
        self.store.unmute("scene", "1")
        self.assertEqual(self.store.mutes(), [])


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
             "at": "2026-07-01T00:00:00"}])

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
