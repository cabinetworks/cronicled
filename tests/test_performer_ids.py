"""`cronicled.performer_ids` turns the media server's OWN performer records
into a {name: stash-box performer id} mapping. No test here opens a socket:
`_FakeStash` is a small double exposing only `performers_with_stash_ids`.
"""
import unittest

from cronicled.performer_ids import (
    DerivedPerformerIds, derive_performer_ids, merge_performer_ids)

ENDPOINT = "https://stashdb.example.test/graphql"
OTHER_ENDPOINT = "https://otherbox.example.test/graphql"


def performer(name, stash_ids=()):
    return {"id": name, "name": name, "stash_ids": list(stash_ids)}


def stash_id(endpoint, sid):
    return {"endpoint": endpoint, "stash_id": sid}


class _FakeStash:
    def __init__(self, performers):
        self._performers = list(performers)

    def performers_with_stash_ids(self):
        return list(self._performers)


class Derivation(unittest.TestCase):
    def test_a_performer_linked_at_the_endpoint_resolves(self):
        stash = _FakeStash([
            performer("Velvet Crane", [stash_id(ENDPOINT, "pf-1")]),
        ])

        derived = derive_performer_ids(stash, ENDPOINT)

        self.assertEqual(derived.ids, {"Velvet Crane": "pf-1"})
        self.assertEqual(derived.ambiguous, {})

    def test_a_performer_with_no_stash_ids_is_ignored(self):
        stash = _FakeStash([performer("Velvet Crane", [])])

        derived = derive_performer_ids(stash, ENDPOINT)

        self.assertEqual(derived.ids, {})
        self.assertEqual(derived.ambiguous, {})

    def test_a_stash_id_at_a_different_endpoint_is_ignored(self):
        stash = _FakeStash([
            performer("Velvet Crane", [stash_id(OTHER_ENDPOINT, "pf-9")]),
        ])

        derived = derive_performer_ids(stash, ENDPOINT)

        self.assertEqual(derived.ids, {})
        self.assertEqual(derived.ambiguous, {})

    def test_two_performers_sharing_a_name_with_different_ids_is_ambiguous(self):
        # Two real people can share a stage name -- this must be REPORTED,
        # not resolved by whichever record the server happened to list
        # first (mutation target: acceptance criteria's "ambiguous name
        # resolves to the first candidate").
        stash = _FakeStash([
            performer("Ivy Thorn", [stash_id(ENDPOINT, "pf-1")]),
            performer("Ivy Thorn", [stash_id(ENDPOINT, "pf-2")]),
        ])

        derived = derive_performer_ids(stash, ENDPOINT)

        self.assertEqual(derived.ids, {})
        self.assertEqual(derived.ambiguous, {"Ivy Thorn": ("pf-1", "pf-2")})

    def test_two_performer_records_agreeing_on_the_same_id_are_not_ambiguous(self):
        # A duplicate performer entry pointing at the same external id is
        # not a disagreement -- there is only one answer between them.
        stash = _FakeStash([
            performer("Ivy Thorn", [stash_id(ENDPOINT, "pf-1")]),
            performer("Ivy Thorn", [stash_id(ENDPOINT, "pf-1")]),
        ])

        derived = derive_performer_ids(stash, ENDPOINT)

        self.assertEqual(derived.ids, {"Ivy Thorn": "pf-1"})
        self.assertEqual(derived.ambiguous, {})

    def test_an_ambiguous_name_never_also_appears_in_ids(self):
        stash = _FakeStash([
            performer("Ivy Thorn", [stash_id(ENDPOINT, "pf-1")]),
            performer("Ivy Thorn", [stash_id(ENDPOINT, "pf-2")]),
            performer("Velvet Crane", [stash_id(ENDPOINT, "pf-3")]),
        ])

        derived = derive_performer_ids(stash, ENDPOINT)

        self.assertNotIn("Ivy Thorn", derived.ids)
        self.assertEqual(derived.ids, {"Velvet Crane": "pf-3"})
        self.assertEqual(set(derived.ambiguous), {"Ivy Thorn"})

    def test_an_empty_server_derives_nothing(self):
        derived = derive_performer_ids(_FakeStash([]), ENDPOINT)

        self.assertEqual(derived.ids, {})
        self.assertEqual(derived.ambiguous, {})


class Merge(unittest.TestCase):
    def test_a_derived_only_name_passes_through(self):
        derived = DerivedPerformerIds({"Velvet Crane": "pf-1"}, {})

        ids, unresolved = merge_performer_ids({}, derived)

        self.assertEqual(ids, {"Velvet Crane": "pf-1"})
        self.assertEqual(unresolved, {})

    def test_a_manual_only_name_passes_through(self):
        derived = DerivedPerformerIds({}, {})

        ids, unresolved = merge_performer_ids({"Velvet Crane": "pf-1"}, derived)

        self.assertEqual(ids, {"Velvet Crane": "pf-1"})
        self.assertEqual(unresolved, {})

    def test_a_manual_entry_wins_over_a_conflicting_derived_one(self):
        derived = DerivedPerformerIds({"Velvet Crane": "pf-1"}, {})

        ids, unresolved = merge_performer_ids({"Velvet Crane": "pf-9"}, derived)

        self.assertEqual(ids, {"Velvet Crane": "pf-9"})
        self.assertEqual(unresolved, {})

    def test_a_manual_entry_settles_a_derived_ambiguity(self):
        # An operator naming one of the two ids a name was ambiguous between
        # is a deliberate human decision, not an iteration-order guess, so
        # it is honoured and the name is no longer reported as unresolved.
        derived = DerivedPerformerIds({}, {"Ivy Thorn": ("pf-1", "pf-2")})

        ids, unresolved = merge_performer_ids({"Ivy Thorn": "pf-2"}, derived)

        self.assertEqual(ids, {"Ivy Thorn": "pf-2"})
        self.assertEqual(unresolved, {})

    def test_an_unsettled_ambiguity_is_reported_and_left_out_of_ids(self):
        derived = DerivedPerformerIds({}, {"Ivy Thorn": ("pf-1", "pf-2")})

        ids, unresolved = merge_performer_ids({}, derived)

        self.assertNotIn("Ivy Thorn", ids)
        self.assertEqual(unresolved, {"Ivy Thorn": ("pf-1", "pf-2")})

    def test_an_unrelated_manual_entry_does_not_settle_someone_elses_ambiguity(self):
        derived = DerivedPerformerIds({}, {"Ivy Thorn": ("pf-1", "pf-2")})

        ids, unresolved = merge_performer_ids({"Velvet Crane": "pf-3"}, derived)

        self.assertEqual(ids, {"Velvet Crane": "pf-3"})
        self.assertEqual(unresolved, {"Ivy Thorn": ("pf-1", "pf-2")})


if __name__ == "__main__":
    unittest.main()
