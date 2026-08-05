"""Filling in a performer record that has almost nothing on it.

Every performer name, alias and stash-box id in this file is invented; none
belongs to anybody.
"""
import os
import shutil
import tempfile
import threading
import unittest

from cronicled.enrichment import (
    FIELDS, IMAGE_FIELD, SUBJECT_TYPE, Candidate, EnrichmentProducer,
    FieldConflict, _lacks_image, enrich_one, matches_name, merge_candidates,
    missing_fields, proposal,
)
from cronicled.jobs import JobRunner
from cronicled.stash import Stash
from cronicled.store import Store


# -- fixtures ---------------------------------------------------------------- #

def _performer(id="7", name="Wren Alderly", stash_ids=(), image_path=None,
              **overrides):
    """A performer row shaped exactly as `Stash.performers_for_enrichment`
    returns one -- every `Stash.ENRICHMENT_FIELDS` entry blank unless
    overridden, plus `image_path` carrying the default-placeholder marker
    unless a real one is given."""
    row = {"id": id, "name": name, "stash_ids": list(stash_ids),
          "image_path": image_path or (
              "http://example.test/performer/%s/image?default=true" % id)}
    for field in Stash.ENRICHMENT_FIELDS:
        row[field] = [] if field in ("alias_list", "urls") else None
    row.update(overrides)
    return row


def _box_candidate(label="stash-box", name="Wren Alderly", aliases=(),
                   **fields):
    return Candidate(label=label, name=name, aliases=tuple(aliases),
                     fields=fields)


class _Stash:
    """A media server holding exactly the performers it was given, and
    refusing every other call -- the same discipline `test_descriptions.py`'s
    own `_Performers` double applies, for the same reason: this producer
    reads one whole-library list and proposes; it never writes."""

    def __init__(self, rows):
        self.rows = list(rows)

    def performers_for_enrichment(self):
        return list(self.rows)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the enrichment pass called %r on the media server; it "
                "reads one whole-library list and proposes, it never "
                "writes" % (name,))
        return refuse


class _Box:
    """A stash-box holding exactly the profiles/search results it was given.

    `profiles` maps a stash-box performer id to the `Candidate`-shaped dict
    `StashBox.performer_profile` returns (or is absent, for an id the source
    does not recognise). `searches` maps a NAME to the list of such dicts
    `StashBox.search_performers` returns for it -- an unlisted name answers
    an empty list, the box's own ordinary "nothing here" rather than an
    error.
    """

    def __init__(self, profiles=None, searches=None, url="https://box.test"):
        self.profiles = profiles or {}
        self.searches = searches or {}
        self.url = url
        self.profile_calls = []
        self.search_calls = []

    def performer_profile(self, performer_id):
        self.profile_calls.append(performer_id)
        return self.profiles.get(performer_id)

    def search_performers(self, name):
        self.search_calls.append(name)
        return self.searches.get(name, [])


def _box_row(id, name, aliases=(), **fields):
    """One `_performer_from_box`-shaped dict, the shape `_Box` hands back."""
    return {"id": id, "name": name, "aliases": list(aliases), "fields": fields}


class _Ctx:
    """What a producer is handed as `ctx` -- the same double
    `test_descriptions.py` uses, and for the same reason: `JobRunner`'s own
    `state.message` keeps only the LAST line, so a test about what a job
    REPORTS must read the same field production does."""

    def __init__(self):
        self.message = ""
        self.messages = []
        self._lock = threading.Lock()

    def log(self, message):
        with self._lock:
            self.message = message
            self.messages.append(message)


# -- the image marker --------------------------------------------------------- #

class LacksImage(unittest.TestCase):
    def test_the_default_marker_means_no_real_image(self):
        self.assertTrue(_lacks_image(
            "http://example.test/performer/7/image?default=true"))

    def test_a_real_image_has_no_default_marker(self):
        # Asserted in the OTHER direction too: a check inverted here reads as
        # "every performer needs an image" rather than as silence -- see this
        # module's own docstring.
        self.assertFalse(_lacks_image(
            "http://example.test/performer/7/image"))

    def test_a_missing_or_empty_path_lacks_an_image(self):
        self.assertTrue(_lacks_image(None))
        self.assertTrue(_lacks_image(""))

    def test_default_true_elsewhere_in_the_url_still_counts(self):
        # The marker is a query-string fact, not a fact about position.
        self.assertTrue(_lacks_image(
            "http://example.test/image?t=123&default=true&x=1"))


# -- which fields are missing ------------------------------------------------- #

class MissingFields(unittest.TestCase):
    def test_a_completely_bare_performer_is_missing_every_field(self):
        self.assertEqual(set(missing_fields(_performer())), set(FIELDS))

    def test_a_performer_with_everything_filled_is_missing_nothing(self):
        filled = _performer(
            image_path="http://example.test/i.jpg",
            details="Bio.", disambiguation="the elder", piercings="ears",
            tattoos="one, forearm", eye_color="hazel", country="Freedonia",
            gender="FEMALE", measurements="34C-24-36", career_length="2015-",
            birthdate="1990-01-01", ethnicity="not stated",
            alias_list=["Wren A."], urls=["https://example.test/wren"],
            height_cm=170)
        self.assertEqual(missing_fields(filled), ())

    def test_a_performer_who_already_has_an_image_is_never_flagged_for_one(self):
        # HARM: proposing an image for a performer who already has one is the
        # acceptance rule this project's own brief singles out -- a wrong
        # photo next to a name a person recognises is worse than no photo.
        row = _performer(image_path="http://example.test/i.jpg")
        self.assertNotIn(IMAGE_FIELD, missing_fields(row))

    def test_an_empty_list_field_is_missing_but_a_populated_one_is_not(self):
        bare = _performer()
        self.assertIn("alias_list", missing_fields(bare))
        filled = _performer(alias_list=["Wren A."])
        self.assertNotIn("alias_list", missing_fields(filled))

    def test_an_empty_string_is_missing_same_as_none(self):
        self.assertIn("details", missing_fields(_performer(details="")))

    def test_only_the_blank_fields_come_back_not_the_filled_ones(self):
        row = _performer(gender="FEMALE", country="Freedonia")
        got = missing_fields(row)
        self.assertNotIn("gender", got)
        self.assertNotIn("country", got)
        self.assertIn("details", got)


# -- matching a name, never scoring one --------------------------------------- #

class MatchesName(unittest.TestCase):
    def test_an_exact_name_match(self):
        self.assertTrue(matches_name(
            "Wren Alderly", _box_candidate(name="Wren Alderly")))

    def test_case_and_accent_folding_still_counts_as_exact(self):
        self.assertTrue(matches_name(
            "Wren Alderly", _box_candidate(name="WREN ALDERLY")))

    def test_a_match_through_an_alias(self):
        self.assertTrue(matches_name(
            "Wren Alderly",
            _box_candidate(name="W. Alderly", aliases=["Wren Alderly"])))

    def test_a_name_that_is_a_substring_of_another_does_not_match(self):
        # The exact trap this module's docstring names: "Wren" must never be
        # read as evidence for "Duchess Wren", or the reverse.
        self.assertFalse(matches_name(
            "Wren", _box_candidate(name="Duchess Wren")))
        self.assertFalse(matches_name(
            "Duchess Wren", _box_candidate(name="Wren")))

    def test_a_merely_similar_name_does_not_match(self):
        self.assertFalse(matches_name(
            "Wren Alderly", _box_candidate(name="Wren Alderley")))

    def test_an_empty_performer_name_matches_nothing(self):
        self.assertFalse(matches_name("", _box_candidate(name="Wren Alderly")))
        self.assertFalse(matches_name(
            "", _box_candidate(name="", aliases=[""])))


# -- merging what several matching candidates offer --------------------------- #

class MergeCandidates(unittest.TestCase):
    def test_a_single_candidates_fields_are_used_outright(self):
        c = _box_candidate(gender="FEMALE", country="Freedonia")
        fields, conflicts = merge_candidates([c], ("gender", "country"))
        self.assertEqual(fields, {"gender": "FEMALE", "country": "Freedonia"})
        self.assertEqual(conflicts, ())

    def test_two_candidates_offering_the_same_value_is_agreement(self):
        a = _box_candidate(label="box-a", gender="FEMALE")
        b = _box_candidate(label="box-b", gender="female")
        fields, conflicts = merge_candidates([a, b], ("gender",))
        self.assertEqual(fields, {"gender": "FEMALE"})
        self.assertEqual(conflicts, ())

    def test_two_candidates_offering_different_values_is_a_refusal_naming_both(self):
        a = _box_candidate(label="box-a", gender="FEMALE")
        b = _box_candidate(label="box-b", gender="NON_BINARY")
        fields, conflicts = merge_candidates([a, b], ("gender",))
        self.assertNotIn("gender", fields)
        self.assertEqual(conflicts,
                         (FieldConflict(field="gender",
                                        offers=(("box-a", "FEMALE"),
                                                ("box-b", "NON_BINARY"))),))

    def test_a_field_only_one_candidate_offers_is_still_proposed(self):
        a = _box_candidate(label="box-a", gender="FEMALE")
        b = _box_candidate(label="box-b")  # says nothing about gender
        fields, conflicts = merge_candidates([a, b], ("gender",))
        self.assertEqual(fields, {"gender": "FEMALE"})
        self.assertEqual(conflicts, ())

    def test_a_field_nobody_offers_is_simply_absent(self):
        a = _box_candidate(label="box-a")
        fields, conflicts = merge_candidates([a], ("gender",))
        self.assertNotIn("gender", fields)
        self.assertEqual(conflicts, ())

    def test_agreeing_and_conflicting_fields_are_independent(self):
        # A conflict on one field must never suppress a field the same
        # candidates agree on.
        a = _box_candidate(label="box-a", gender="FEMALE", country="Freedonia")
        b = _box_candidate(label="box-b", gender="NON_BINARY",
                           country="Freedonia")
        fields, conflicts = merge_candidates([a, b], ("gender", "country"))
        self.assertEqual(fields, {"country": "Freedonia"})
        self.assertEqual([c.field for c in conflicts], ["gender"])

    def test_list_fields_agree_regardless_of_order(self):
        a = _box_candidate(label="box-a", alias_list=["Wren A.", "W. Alderly"])
        b = _box_candidate(label="box-b", alias_list=["W. Alderly", "Wren A."])
        fields, conflicts = merge_candidates([a, b], ("alias_list",))
        self.assertEqual(set(fields["alias_list"]), {"Wren A.", "W. Alderly"})
        self.assertEqual(conflicts, ())

    def test_list_fields_can_still_conflict(self):
        a = _box_candidate(label="box-a", urls=["https://a.test/wren"])
        b = _box_candidate(label="box-b", urls=["https://b.test/wren"])
        fields, conflicts = merge_candidates([a, b], ("urls",))
        self.assertNotIn("urls", fields)
        self.assertEqual([c.field for c in conflicts], ["urls"])

    def test_the_proposed_value_is_the_first_offers_own_text(self):
        # `_field_key` decides AGREEMENT, never what gets carried -- two
        # spellings that normalise the same must not silently become a third,
        # reconstructed spelling nobody actually offered.
        a = _box_candidate(label="box-a", gender="Female")
        b = _box_candidate(label="box-b", gender="FEMALE")
        fields, _ = merge_candidates([a, b], ("gender",))
        self.assertEqual(fields["gender"], "Female")


# -- the id-then-name fallback ------------------------------------------------- #

class EnrichOne(unittest.TestCase):
    def test_no_box_configured_offers_nothing(self):
        fields, conflicts, source = enrich_one(
            _performer(), None, wanted=("gender",))
        self.assertEqual(fields, {})
        self.assertIsNone(source)

    def test_an_id_for_this_boxs_endpoint_is_looked_up_by_id(self):
        box = _Box(profiles={"bx-1": _box_row(
            "bx-1", "Wren Alderly", gender="FEMALE")})
        performer = _performer(stash_ids=[
            {"endpoint": "https://box.test", "stash_id": "bx-1"}])

        fields, conflicts, source = enrich_one(
            performer, box, wanted=("gender",))

        self.assertEqual(fields, {"gender": "FEMALE"})
        self.assertEqual(source, "stash-box (by id)")
        self.assertEqual(box.profile_calls, ["bx-1"])
        self.assertEqual(box.search_calls, [])

    def test_the_id_path_is_tried_before_a_name_search(self):
        # Mutating the order so the search runs first must fail this test --
        # the id path is stronger evidence and the acceptance list is
        # explicit that this ordering is load-bearing.
        box = _Box(
            profiles={"bx-1": _box_row("bx-1", "Wren Alderly", gender="FEMALE")},
            searches={"Wren Alderly": [_box_row(
                "bx-2", "Wren Alderly", gender="NON_BINARY")]})
        performer = _performer(stash_ids=[
            {"endpoint": "https://box.test", "stash_id": "bx-1"}])

        fields, _, source = enrich_one(performer, box, wanted=("gender",))

        self.assertEqual(fields, {"gender": "FEMALE"})
        self.assertEqual(source, "stash-box (by id)")
        self.assertEqual(box.search_calls, [],
                         "a name search ran even though the id path resolved")

    def test_an_id_for_a_different_endpoint_is_not_used(self):
        box = _Box(profiles={"bx-1": _box_row(
            "bx-1", "Wren Alderly", gender="FEMALE")},
            searches={"Wren Alderly": [_box_row(
                "bx-2", "Wren Alderly", gender="NON_BINARY")]})
        performer = _performer(stash_ids=[
            {"endpoint": "https://other-box.test", "stash_id": "bx-1"}])

        fields, _, source = enrich_one(performer, box, wanted=("gender",))

        self.assertEqual(fields, {"gender": "NON_BINARY"})
        self.assertEqual(source, "stash-box (by name)")
        self.assertEqual(box.profile_calls, [])

    def test_a_stale_id_the_box_no_longer_recognises_falls_through_to_a_search(self):
        box = _Box(profiles={}, searches={"Wren Alderly": [_box_row(
            "bx-2", "Wren Alderly", gender="NON_BINARY")]})
        performer = _performer(stash_ids=[
            {"endpoint": "https://box.test", "stash_id": "bx-gone"}])

        fields, _, source = enrich_one(performer, box, wanted=("gender",))

        self.assertEqual(fields, {"gender": "NON_BINARY"})
        self.assertEqual(source, "stash-box (by name)")

    def test_no_id_searches_by_name(self):
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly", gender="NON_BINARY")]})

        fields, _, source = enrich_one(
            _performer(name="Wren Alderly"), box, wanted=("gender",))

        self.assertEqual(fields, {"gender": "NON_BINARY"})
        self.assertEqual(source, "stash-box (by name)")

    def test_a_search_result_that_is_only_a_substring_match_is_ignored(self):
        # "Wren" must never be read as evidence for "Duchess Wren".
        box = _Box(searches={"Duchess Wren": [
            _box_row("bx-2", "Wren", gender="FEMALE")]})

        fields, _, source = enrich_one(
            _performer(name="Duchess Wren"), box, wanted=("gender",))

        self.assertEqual(fields, {})
        self.assertIsNone(source)

    def test_no_search_results_at_all_offers_nothing(self):
        box = _Box(searches={})

        fields, _, source = enrich_one(
            _performer(name="Nobody Like This"), box, wanted=("gender",))

        self.assertEqual(fields, {})
        self.assertIsNone(source)

    def test_two_exact_matches_from_one_search_that_agree_corroborate(self):
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly", gender="FEMALE"),
            _box_row("bx-3", "Wren Alderly", gender="FEMALE")]})

        fields, conflicts, source = enrich_one(
            _performer(name="Wren Alderly"), box, wanted=("gender",))

        self.assertEqual(fields, {"gender": "FEMALE"})
        self.assertEqual(conflicts, ())

    def test_two_exact_matches_from_one_search_that_disagree_refuse_that_field(self):
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly", gender="FEMALE"),
            _box_row("bx-3", "Wren Alderly", gender="NON_BINARY")]})

        fields, conflicts, source = enrich_one(
            _performer(name="Wren Alderly"), box, wanted=("gender",))

        self.assertNotIn("gender", fields)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].field, "gender")


# -- the proposal payload ------------------------------------------------------ #

class Proposal(unittest.TestCase):
    def test_the_payload_carries_the_fields_and_their_source(self):
        row = proposal(_performer(id="7", name="Wren Alderly"),
                       {"gender": "FEMALE"}, "stash-box (by id)",
                       folder="library")
        self.assertEqual(row["subject_type"], SUBJECT_TYPE)
        self.assertEqual(row["subject_id"], "7")
        self.assertEqual(row["payload"], {
            "name": "Wren Alderly",
            "source": "stash-box (by id)",
            "fields": {"gender": "FEMALE"},
        })
        self.assertIsNone(row["confidence"])


# -- the producer, through the real store ------------------------------------- #

class ThroughTheRealRunnerAndStore(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.store = Store(os.path.join(self._dir, "cronicled.sqlite3"))
        self.addCleanup(self.store.close)
        self.runner = JobRunner(self.store)

    def _run(self, rows, box, limit=10):
        producer = EnrichmentProducer(_Stash(rows), box, every=60, limit=limit)
        self.runner.reregister(producer)
        job = self.runner.start(producer.name, trigger="manual")
        self.assertTrue(self.runner.wait(job.id, 10))
        return self.runner.job(job.id)

    def test_a_bare_performer_reaches_the_store_as_one_proposal(self):
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly", gender="FEMALE")]})

        job = self._run([_performer(id="7", name="Wren Alderly")], box)

        self.assertEqual(job.state, "done", job.traceback)
        items = self.store.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["subject_type"], SUBJECT_TYPE)
        self.assertEqual(items[0]["subject_id"], "7")
        self.assertEqual(items[0]["payload"]["fields"], {"gender": "FEMALE"})

    def test_a_performer_with_a_real_image_is_never_proposed_an_image(self):
        # Asserted on the STORE, not on a count -- the acceptance rule this
        # project's own brief singles out.
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly",
                     image="https://example.test/found.jpg")]})
        row = _performer(id="7", name="Wren Alderly",
                         image_path="https://example.test/already-there.jpg")

        self._run([row], box)

        items = self.store.items()
        self.assertEqual(len(items), 0,
                         "a performer with a real image already must never "
                         "be proposed anything for it")

    def test_a_performer_with_nothing_missing_is_never_proposed_for(self):
        box = _Box()
        filled = _performer(
            id="7", image_path="http://example.test/i.jpg", details="Bio.",
            disambiguation="the elder", piercings="ears", tattoos="one",
            eye_color="hazel", country="Freedonia", gender="FEMALE",
            measurements="34C-24-36", career_length="2015-",
            birthdate="1990-01-01", ethnicity="not stated",
            alias_list=["Wren A."], urls=["https://example.test/wren"],
            height_cm=170)

        self._run([filled], box)

        self.assertEqual(self.store.items(), [])

    def test_a_muted_performer_is_not_proposed_again(self):
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly", gender="FEMALE")]})
        self.store.mute(SUBJECT_TYPE, "7", reason="not this one")

        job = self._run([_performer(id="7", name="Wren Alderly")], box)

        self.assertEqual(job.recorded, 0)
        self.assertEqual(self.store.items(), [])

    def test_a_second_run_over_the_same_library_adds_no_second_row(self):
        box = _Box(searches={"Wren Alderly": [
            _box_row("bx-2", "Wren Alderly", gender="FEMALE")]})
        rows = [_performer(id="7", name="Wren Alderly")]

        self._run(rows, box)
        self._run(rows, box)

        self.assertEqual(len(self.store.items()), 1)

    def test_the_limit_bounds_how_many_performers_spend_a_box_lookup(self):
        box = _Box(searches={
            "Wren Alderly": [_box_row("bx-2", "Wren Alderly", gender="FEMALE")],
            "Ivy Marchetti": [_box_row("bx-3", "Ivy Marchetti", gender="FEMALE")],
        })
        rows = [_performer(id="7", name="Wren Alderly"),
               _performer(id="8", name="Ivy Marchetti")]

        job = self._run(rows, box, limit=1)

        self.assertEqual(job.recorded, 1)
        self.assertEqual(len(box.search_calls), 1)

    def test_no_box_configured_proposes_nothing_and_does_not_fail(self):
        job = self._run([_performer(id="7", name="Wren Alderly")], None)

        self.assertEqual(job.state, "done", job.traceback)
        self.assertEqual(self.store.items(), [])
        self.assertIn("no stash-box is configured", job.message)


class EnrichmentProducerRequiresACadenceOrALimit(unittest.TestCase):
    def test_a_missing_limit_is_refused_rather_than_defaulted(self):
        with self.assertRaises(ValueError):
            EnrichmentProducer(_Stash([]), None, limit=None, every=60)

    def test_a_producer_with_no_cadence_is_refused_by_the_scheduler(self):
        from cronicled.schedule import resolve
        producer = EnrichmentProducer(_Stash([]), None, limit=10)
        with self.assertRaises(ValueError):
            resolve([producer])

    def test_an_explicit_disable_satisfies_the_scheduler_with_no_cadence(self):
        from cronicled.schedule import resolve
        producer = EnrichmentProducer(_Stash([]), None, limit=10)
        entries = resolve([producer], overrides={producer.name: {"enabled": False}})
        self.assertFalse(entries[producer.name].enabled)


if __name__ == "__main__":
    unittest.main()
