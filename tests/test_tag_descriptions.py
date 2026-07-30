"""Descriptions a stash-box holds for tags a library leaves blank.

Every tag name, alias and description in this file is invented. The tags this
was measured against are a real library's and none of them, nor any sentence
from any real source, appears here or anywhere in this repository.
"""
import unittest

from cronicled.tag_descriptions import (SUBJECT_TYPE, BoxIndex, Counts, Found,
                                        MergeDescription, find_description,
                                        has_description, index_box,
                                        merge_description, proposal)

FOLDER = "library"

# Two descriptions that are visibly different sentences, so no assertion here
# can pass by comparing a value against itself.
LANTERN = "Scenes lit only by a hand-carried lamp."
FERRY = "Filmed aboard a working passenger boat."


def box_tag(name, description=LANTERN, aliases=()):
    """One row shaped exactly as `StashBox.all_tags` returns it -- `id`,
    `name`, `description` and `aliases`, and nothing else. Written from that
    method's own selection set rather than from what the indexer happens to
    read, so a field the query stops asking for is a failure here."""
    return {"id": "b-" + name, "name": name, "description": description,
            "aliases": list(aliases)}


def tag(id, name, description=None, scene_count=0):
    """One row shaped exactly as `Stash.all_tags` returns it. `description`
    defaults to `None` -- what the server really returns for a tag nobody has
    described, never `""`."""
    return {"id": str(id), "name": name, "aliases": [],
            "description": description, "scene_count": scene_count}


class HasDescription(unittest.TestCase):
    def test_a_tag_the_server_returned_none_for_has_none(self):
        self.assertFalse(has_description(tag(1, "Lantern Work")))

    def test_a_tag_with_text_has_one(self):
        self.assertTrue(has_description(tag(1, "Lantern Work", LANTERN)))

    def test_whitespace_alone_is_not_a_description(self):
        # A field holding a space is not a description of anything, and
        # counting it as one would drop the tag out of the backlog for ever
        # on the strength of a stray character.
        self.assertFalse(has_description(tag(1, "Lantern Work", "  \n ")))

    def test_a_row_the_client_did_not_select_the_field_on_raises(self):
        # Read with a default, a malformed row would answer "already
        # described" and vanish from every count without anything saying so.
        with self.assertRaises(KeyError):
            has_description({"id": "1", "name": "Lantern Work"})


class IndexingOneSource(unittest.TestCase):
    """What a source's catalogue becomes, and what it deliberately does not."""

    def test_a_tag_is_found_by_its_name(self):
        index = index_box("first", [box_tag("Lantern Work")])

        self.assertEqual(index.keys, {"lanternwork": LANTERN})
        self.assertEqual(index.name, "first")

    def test_a_tag_is_found_by_the_sources_alias_for_it(self):
        # THE rule that took coverage from a name-only match to most of what
        # this module finds. A test using only name fixtures would leave a
        # names-only implementation looking correct.
        index = index_box("first", [box_tag("Lantern Work",
                                            aliases=["Lamplight"])])

        self.assertEqual(index.keys["lamplight"], LANTERN)

    def test_a_name_is_matched_through_the_projects_normalised_form(self):
        # Spacing, case and punctuation are what a library's spellings differ
        # by; an index keyed on the raw string finds almost none of them.
        index = index_box("first", [box_tag("Lantern Work")])

        self.assertEqual(find_description("lantern-work", [index]).description,
                         LANTERN)
        self.assertEqual(find_description("LanternWork", [index]).description,
                         LANTERN)

    def test_a_tag_the_source_does_not_describe_contributes_nothing(self):
        # An entry mapping a key to an empty description is indistinguishable
        # from a key nobody claimed, and would crowd out a later source that
        # really does describe the tag.
        index = index_box("first", [box_tag("Lantern Work", description=None),
                                    box_tag("Brass Ferry", description="")])

        self.assertEqual(index.keys, {})

    def test_a_source_description_of_whitespace_alone_contributes_nothing(self):
        # `has_description` already refuses whitespace on the LIBRARY side;
        # this is the same rule on the SOURCE side, and it is the side that
        # decides what gets written. Indexed, a key mapping to "  " is a
        # `Found` like any other, so the pass proposes -- and a reviewer
        # approves -- filling a blank field with a space, which then reads as
        # described for ever and crowds out the source that has a sentence.
        index = index_box("first", [box_tag("Lantern Work", description="  \n"),
                                    box_tag("Brass Ferry", description="\t")])

        self.assertEqual(index.keys, {})

    def test_an_undescribed_tags_aliases_do_not_shadow_a_described_one(self):
        index = index_box("first", [
            box_tag("Lantern Work", description=None, aliases=["Lamplight"]),
            box_tag("Lamplight", description=FERRY)])

        self.assertEqual(index.keys["lamplight"], FERRY)

    def test_a_name_that_normalises_to_nothing_is_not_a_key(self):
        # Every punctuation-only name reduces to the same empty key, so
        # indexing them would answer every punctuation-only library tag with
        # whichever of them was seen last.
        index = index_box("first", [box_tag("!!!")])

        self.assertEqual(index.keys, {})

    def test_two_tags_claiming_one_key_with_the_same_text_is_agreement(self):
        # Two spellings of one idea. Nothing is ambiguous, so nothing is
        # dropped: dropping here would lose a perfectly good description to a
        # rule written for a case that did not arise.
        index = index_box("first", [box_tag("Lantern Work"),
                                    box_tag("LanternWork")])

        self.assertEqual(index.keys, {"lanternwork": LANTERN})
        self.assertEqual(index.ambiguous, ())

    def test_two_tags_claiming_one_key_with_different_text_are_dropped(self):
        # The source is saying two things and nothing here can say which.
        # Whichever this picked would be picked by iteration order -- so the
        # key stops existing rather than being resolved.
        index = index_box("first", [box_tag("Lantern Work"),
                                    box_tag("LanternWork", description=FERRY)])

        self.assertEqual(index.keys, {})
        self.assertEqual(index.ambiguous, ("lanternwork",))
        self.assertIsNone(find_description("Lantern Work", [index]))

    def test_an_alias_colliding_with_another_tags_name_is_dropped(self):
        # The shape this really takes in a catalogue: one tag's alias is
        # another tag's name.
        index = index_box("first", [
            box_tag("Lantern Work", aliases=["Brass Ferry"]),
            box_tag("Brass Ferry", description=FERRY)])

        self.assertNotIn("brassferry", index.keys)
        self.assertEqual(index.keys["lanternwork"], LANTERN)

    def test_a_dropped_key_is_never_reclaimed_by_a_later_tag(self):
        # Re-adding it would make the answer depend on how many tags claimed
        # the key and in what order -- the resolution by iteration order this
        # dropped it to avoid, arrived at the long way round.
        index = index_box("first", [
            box_tag("Lantern Work"),
            box_tag("LanternWork", description=FERRY),
            box_tag("lantern work", description="A third reading.")])

        self.assertEqual(index.keys, {})


class WhichSourceAnswers(unittest.TestCase):
    """Configured order, first hit wins."""

    def setUp(self):
        # Deliberately ASYMMETRIC: reversing these two changes both which
        # description comes back for the shared tag AND which source is
        # named, and adds a tag the first source does not hold. A fixture
        # whose reversal looked the same could not detect an order mutation
        # at all.
        self.first = index_box("first", [box_tag("Lantern Work")])
        self.second = index_box("second", [
            box_tag("Lantern Work", description=FERRY),
            box_tag("Slate Harbour", description="Shot on a stone quay.")])
        self.indexes = [self.first, self.second]

    def test_the_first_configured_source_wins(self):
        self.assertEqual(find_description("Lantern Work", self.indexes),
                         Found(description=LANTERN, box="first"))

    def test_reversing_the_configured_order_reverses_the_answer(self):
        # The other half. Without it, "first wins" and "last wins" both
        # satisfy the assertion above for a one-source-deep fixture.
        self.assertEqual(find_description("Lantern Work",
                                          list(reversed(self.indexes))),
                         Found(description=FERRY, box="second"))

    def test_a_later_source_answers_what_the_first_has_nothing_for(self):
        # Measured, the later sources add a few dozen tags between them. They
        # are asked because a differently configured library could invert
        # that, and this is what fails if only the first is ever consulted.
        self.assertEqual(find_description("Slate Harbour", self.indexes),
                         Found(description="Shot on a stone quay.",
                               box="second"))

    def test_a_tag_no_source_describes_gets_no_answer_at_all(self):
        # NOT a derived one, not one composed from the name, not a related
        # tag's text. `None` is the whole answer.
        self.assertIsNone(find_description("Copper Kettle", self.indexes))

    def test_a_name_that_normalises_to_nothing_matches_nothing(self):
        self.assertIsNone(find_description("!!!", self.indexes))

    def test_no_configured_source_answers_nothing(self):
        self.assertIsNone(find_description("Lantern Work", []))


class ProposalShape(unittest.TestCase):
    def test_the_whole_proposal(self):
        # The WHOLE dict, not sampled fields. An extra key is an extra key in
        # the fingerprint, which re-proposes every tag in the library once,
        # and a field-by-field check cannot see one arrive. A `source_box`
        # that silently defaulted is likewise indistinguishable from one that
        # was set.
        built = proposal(tag(7, "Lantern Work"),
                         Found(description=LANTERN, box="first"),
                         folder=FOLDER)

        self.assertEqual(built, {
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "7",
            "summary": "Lantern Work: first has a description for this tag",
            "confidence": None,
            "payload": {
                "name": "Lantern Work",
                "field": "description",
                "original": None,
                "description": LANTERN,
                "source_box": "first",
            },
        })

    def test_the_original_is_the_servers_own_value_not_a_normalised_copy(self):
        # It is what the apply compares the server against. Normalised to
        # `""` it would fail that comparison against the very state it was
        # recorded from, and every proposal would refuse itself.
        built = proposal(tag(7, "Lantern Work"),
                         Found(description=LANTERN, box="first"),
                         folder=FOLDER)

        self.assertIsNone(built["payload"]["original"])

    def test_its_subject_is_the_tag_and_not_the_cluster(self):
        # Muting "stop describing this tag" and muting "stop proposing that
        # these two spellings are one tag" are two decisions about two
        # things; one subject type would make either mute silence both.
        self.assertEqual(SUBJECT_TYPE, "tag")


class MergeCarriesDescriptions(unittest.TestCase):
    """A merge deletes spellings. What their descriptions said must not go
    with them without anybody being told."""

    def setUp(self):
        self.source = index_box("first", [box_tag("Lantern Work")])

    def _members(self, *pairs):
        return [tag(n + 1, name, description)
                for n, (name, description) in enumerate(pairs)]

    def test_the_survivor_inherits_the_only_description_in_the_cluster(self):
        members = self._members(("Lantern Work", None),
                                ("LanternWork", FERRY))

        got = merge_description(members, members[0], [])

        self.assertEqual(got, MergeDescription(text=FERRY,
                                               from_tag="LanternWork"))

    def test_a_description_the_survivor_already_carries_needs_no_carrying(self):
        members = self._members(("Lantern Work", FERRY),
                                ("LanternWork", None))

        self.assertEqual(merge_description(members, members[0], []),
                         MergeDescription())

    def test_two_spellings_describing_it_the_same_way_is_agreement(self):
        # Not a conflict, and reporting it as one puts a decision in front of
        # somebody who has nothing to decide.
        members = self._members(("Lantern Work", FERRY),
                                ("LanternWork", FERRY))

        self.assertEqual(merge_description(members, members[0], []),
                         MergeDescription())

    def test_two_different_descriptions_are_both_reported_and_neither_wins(self):
        # Not the destination's, not the longer, not the first. A test
        # asserting a single winner would pass under every one of those.
        members = self._members(("Lantern Work", LANTERN),
                                ("LanternWork", FERRY))

        got = merge_description(members, members[0], [])

        self.assertIsNone(got.text)
        self.assertIsNone(got.from_tag)
        self.assertEqual(got.conflicting, (
            {"name": "Lantern Work", "description": LANTERN},
            {"name": "LanternWork", "description": FERRY}))

    def test_a_conflict_is_reported_whichever_spelling_survives(self):
        # The survivor's own text is not privileged, so the same two
        # spellings conflict in either direction. A rule that quietly took
        # the destination's would answer `text` here and pass a test that
        # only looked at the loser.
        members = self._members(("Lantern Work", LANTERN),
                                ("LanternWork", FERRY))

        for survivor in (members[0], members[1]):
            with self.subTest(survivor=survivor["name"]):
                got = merge_description(members, survivor, [])
                self.assertIsNone(got.text)
                self.assertEqual(len(got.conflicting), 2)

    def test_a_cluster_nobody_describes_takes_one_from_a_source(self):
        members = self._members(("Lantern Work", None), ("LanternWork", None))

        self.assertEqual(merge_description(members, members[0], [self.source]),
                         MergeDescription(text=LANTERN, from_box="first"))

    def test_the_source_is_asked_under_the_surviving_spelling(self):
        # It is the only spelling that will exist afterwards, and so the only
        # one a description would end up attached to.
        members = self._members(("Slate Harbour", None),
                                ("SlateHarbour", None))

        self.assertEqual(merge_description(members, members[0], [self.source]),
                         MergeDescription())

    def test_a_source_is_never_asked_when_a_spelling_already_describes_it(self):
        # A person in this library wrote one. A source's sentence must not
        # displace it, and must not appear beside it as a second option.
        members = self._members(("Lantern Work", FERRY),
                                ("LanternWork", None))

        self.assertEqual(merge_description(members, members[1],
                                           [self.source]),
                         MergeDescription(text=FERRY,
                                          from_tag="Lantern Work"))

    def test_an_undecided_cluster_carries_nothing_and_asks_nobody(self):
        # No spelling is being deleted, so no description is at risk, and
        # offering text for a survivor nothing has chosen would be proposing
        # a write to a tag nobody has named.
        members = self._members(("Lantern Work", None),
                                ("LanternWork", FERRY))

        self.assertEqual(merge_description(members, None, [self.source]),
                         MergeDescription())

    def test_the_payload_is_written_whole_every_time(self):
        # Every key, always, so a reader never has to tell "this pass had
        # nothing to say" from "this key was not written".
        self.assertEqual(MergeDescription().as_payload(),
                         {"text": None, "from_tag": None, "from_box": None,
                          "conflicting": []})


class CountsIdentity(unittest.TestCase):
    def test_the_four_reasons_account_for_every_tag(self):
        # Asserted as an identity rather than field by field: a tag that
        # vanished for a reason nobody named is a failure no per-field check
        # can see.
        counts = Counts(total=10, described=2, clustered=3, outstanding=4,
                        beyond_reach=1, boxes_unread=0)

        self.assertEqual(counts.total, counts.described + counts.clustered
                         + counts.outstanding + counts.beyond_reach)


class IndexShape(unittest.TestCase):
    def test_an_index_carries_the_sources_name_with_its_keys(self):
        # `find_description` reads the name off the index it matched in, so
        # the two cannot be carried separately and mismatched.
        index = BoxIndex(name="first", keys={"lanternwork": LANTERN},
                         ambiguous=())

        self.assertEqual(find_description("Lantern Work", [index]).box, "first")
