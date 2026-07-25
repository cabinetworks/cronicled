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
