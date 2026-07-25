"""The store keeps proposed changes between runs. Its fingerprint is what stops a
nightly producer from turning the inbox into noise on its second night."""
import json
import os
import tempfile
import unittest

from cronicled.store import Store, fingerprint


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


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._dir, "s.db"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.store.close()
        import shutil
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
