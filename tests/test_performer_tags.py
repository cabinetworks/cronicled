"""Tags that are really a performer, and the reconciliation proposed for them.

Every name here is invented. The tags this was written against were real
people's names and none of them appear in this file or anywhere else in this
repository.
"""
import unittest

from cronicled import performer_tags
from cronicled.performer_tags import (AMBIGUOUS, COUNTS_COVER, SUBJECT_TYPE,
                                      Counts, Match, index_performers,
                                      match_tag, narrowings, proposal)
from cronicled.stash import Stash, StashError
from cronicled.store import Store
from cronicled.tag_descriptions import SUBJECT_TYPE as TAG_DESCRIPTION_SUBJECT
from cronicled.tags import SUBJECT_TYPE as CLUSTER_SUBJECT, TagMergeProducer
from cronicled.web.actions import Actions, ApplyFailed
from cronicled.web.rows import ReconcileRow, to_reconcile_row

FOLDER = "library"

# Invented people. Chosen so no two normalise to the same form by accident and
# so every assertion below compares two visibly different strings.
ASHGROVE = "Delia Ashgrove"
QUILL = "Marlowe Quill"
FENN = "Tobias Fenn"
WREN = "Hollis Wren"

# An invented tag that is a real content tag: nobody's name, so nothing here
# may ever match it.
LANTERN = "Lantern Drift"


def tag(id, name, scene_count=0, aliases=(), description=None):
    """One row shaped exactly as `Stash.all_tags` returns it, written from that
    method's own selection set rather than from what this module happens to
    read -- a field the client stops selecting is a failure here."""
    return {"id": str(id), "name": name, "aliases": list(aliases),
            "description": description, "scene_count": scene_count}


def performer(id, name, aliases=()):
    """One row shaped exactly as `Stash.performers_with_aliases` returns it.

    `alias_list`, which is what the server calls a PERFORMER's other spellings
    -- not `aliases`, which is a tag's and a studio's. A double spelling it the
    other way would let this module read a field production never receives.
    """
    return {"id": str(id), "name": name, "alias_list": list(aliases)}


def scene_row(id, performers=(), tags=()):
    """One row shaped as `Stash.tagged_scenes` returns it -- every field its
    query selects, so a test cannot pass against a thinner row than production
    hands over."""
    return {"id": str(id), "title": None, "date": None,
            "files": [{"basename": "%s.mp4" % id, "path": "/m/%s.mp4" % id}],
            "studio": None,
            "performers": [{"id": str(p), "name": "p-%s" % p}
                           for p in performers],
            "tags": [{"id": str(t), "name": "t-%s" % t} for t in tags]}


class FakeCtx:
    """What the runner gives a producer: somewhere to log progress.

    Keeps only the LAST message, because that is all the real collaborator
    keeps -- `JobRunner._log` assigns one field and holds no history. A double
    that accumulated a list would let an assertion about a count pass against a
    line no operator can ever read.
    """

    def __init__(self):
        self.message = None

    def log(self, message):
        self.message = message


class FakeStash:
    """The four reads the tag pass makes, and nothing else.

    Any other attribute refuses: the pass proposes and never writes, so a write
    introduced into it shows up here as a failure rather than as a silently
    tolerated call.

    `tagged_scenes` answers the `(count, scenes)` PAIR the real one does, and
    the count it reports is the length of the rows it is handing back --
    matching the real method's per_page -1 read, where the two agree. A double
    answering a bare list would let the pass read the wrong half of the real
    answer and still pass.
    """

    def __init__(self, tags=(), performers=(), scenes=None, boxes=()):
        self._tags = list(tags)
        self._performers = list(performers)
        self._scenes = dict(scenes or {})
        self._boxes = list(boxes)
        self.calls = []

    def all_tags(self):
        self.calls.append(("all_tags",))
        return list(self._tags)

    def stash_box_credentials(self):
        self.calls.append(("stash_box_credentials",))
        return list(self._boxes)

    def performers_with_aliases(self):
        self.calls.append(("performers_with_aliases",))
        return list(self._performers)

    def tagged_scenes(self, tag_id, limit):
        self.calls.append(("tagged_scenes", tag_id, limit))
        rows = self._scenes.get(tag_id, [])
        return len(rows), list(rows)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the tag pass called %r on the media server; it reads and "
                "proposes, it never writes" % (name,))
        return refuse


def _producer(stash, store, folder=FOLDER):
    return TagMergeProducer(stash, store=store, folder=folder, every=86400)


def _reconciliations(stash, store, folder=FOLDER):
    """Only the reconciliation proposals the pass yields, and its counts."""
    return _producer(stash, store, folder)._reconcile(
        stash.all_tags(), [])


# -- what counts as a match ------------------------------------------------ #

class Matching(unittest.TestCase):
    def test_a_tag_matching_a_performers_name_is_found(self):
        index = index_performers([performer(1, ASHGROVE)])

        self.assertEqual(match_tag(tag(9, ASHGROVE), index),
                         (Match(performer={"id": "1", "name": ASHGROVE},
                                alias=None),))

    def test_a_tag_matching_a_performers_alias_is_found(self):
        # HARM: this is the case that motivated the ticket. Measured against a
        # real library, matching performer NAMES alone found far fewer tags
        # than names plus aliases -- an implementation that indexed names only
        # would miss most of what this exists to find, and would look like it
        # was working.
        index = index_performers([performer(1, QUILL, aliases=[ASHGROVE])])

        self.assertEqual(match_tag(tag(9, ASHGROVE), index),
                         (Match(performer={"id": "1", "name": QUILL},
                                alias=ASHGROVE),))

    def test_a_tag_matching_nothing_matches_nothing(self):
        # The other direction of the same rule, pinned so a widened match --
        # containment, a plural strip, an edit distance -- fails here. A match
        # authorises a write to every scene carrying the tag.
        index = index_performers([performer(1, ASHGROVE, aliases=[QUILL])])

        for name in (LANTERN, "Ashgrove", "Delia Ashgroves",
                     "Delia Ashgrove Reel", FENN):
            with self.subTest(name=name):
                self.assertEqual(match_tag(tag(9, name), index), ())

    def test_case_spacing_and_punctuation_do_not_stop_a_match(self):
        index = index_performers([performer(1, ASHGROVE)])

        for name in ("DeliaAshgrove", "DELIA  ASHGROVE", "delia-ashgrove",
                     "Delia.Ashgrove"):
            with self.subTest(name=name):
                self.assertEqual(
                    [m.performer["id"] for m in match_tag(tag(9, name), index)],
                    ["1"])

    def test_a_tag_whose_name_normalises_to_nothing_matches_nothing(self):
        # HARM: every punctuation-only name reduces to the same empty key, so a
        # match on it would answer with whichever performers happened to share
        # that key -- an attribution made on having no letters in common.
        index = index_performers([performer(1, ASHGROVE)])

        self.assertEqual(match_tag(tag(9, "!!!"), index), ())

    def test_a_performer_surface_that_normalises_to_nothing_is_not_keyed(self):
        # HARM: two performers whose punctuation-only aliases both reduce to
        # the empty key would be reported as two performers answering to one
        # tag -- an ambiguity manufactured out of nothing.
        index = index_performers([performer(1, ASHGROVE, aliases=["???"]),
                                  performer(2, QUILL, aliases=["!!!"])])

        self.assertNotIn("", index)
        self.assertEqual(sorted(index), ["deliaashgrove", "marlowequill"])

    def test_a_name_match_beats_an_alias_of_the_same_performer(self):
        # One performer either way, so the only thing at stake is which surface
        # the proposal quotes -- and "this tag is their name" is the stronger
        # thing to show a reviewer than "this tag is one of their aliases".
        index = index_performers(
            [performer(1, ASHGROVE, aliases=["Delia-Ashgrove"])])

        self.assertEqual(match_tag(tag(9, ASHGROVE), index),
                         (Match(performer={"id": "1", "name": ASHGROVE},
                                alias=None),))

    def test_one_performer_claiming_a_key_twice_is_not_two_performers(self):
        index = index_performers(
            [performer(1, ASHGROVE, aliases=["DeliaAshgrove", ASHGROVE])])

        self.assertEqual(len(match_tag(tag(9, ASHGROVE), index)), 1)

    def test_two_performers_answering_to_one_name_are_both_reported(self):
        # HARM: taking one of them -- the first encountered, the one with more
        # scenes, the alphabetically first -- attaches every scene carrying
        # this tag to a person who may not be in any of them, and the row would
        # read exactly like an ordinary confident match.
        #
        # The fixture is deliberately ASYMMETRIC: one performer matches by
        # NAME and the other through an ALIAS, so the two entries differ
        # visibly and reversing the input cannot produce the same tuple by
        # coincidence. A symmetric fixture cannot detect an ordering rule.
        forwards = index_performers([performer(1, ASHGROVE),
                                     performer(2, QUILL, aliases=[ASHGROVE])])
        backwards = index_performers([performer(2, QUILL, aliases=[ASHGROVE]),
                                      performer(1, ASHGROVE)])
        expected = (Match(performer={"id": "1", "name": ASHGROVE}, alias=None),
                    Match(performer={"id": "2", "name": QUILL},
                          alias=ASHGROVE))

        self.assertEqual(match_tag(tag(9, ASHGROVE), forwards), expected)
        self.assertEqual(match_tag(tag(9, ASHGROVE), backwards), expected)

    def test_matches_are_ordered_by_name_then_id_not_by_arrival(self):
        # Deliberately over-determined the other way: the two performers' names
        # sort OPPOSITE to their ids, so name order, id order and either
        # arrival order are four different answers and only one of them passes.
        # WREN ("Hollis ...") carries the LATER id; FENN ("Tobias ...") the
        # earlier.
        early_id_late_name = performer(1, FENN, aliases=[ASHGROVE])
        late_id_early_name = performer(2, WREN, aliases=[ASHGROVE])
        forwards = index_performers([early_id_late_name, late_id_early_name])
        backwards = index_performers([late_id_early_name, early_id_late_name])

        for index in (forwards, backwards):
            self.assertEqual(
                [(m.performer["name"], m.performer["id"])
                 for m in match_tag(tag(9, ASHGROVE), index)],
                [(WREN, "2"), (FENN, "1")])


# -- the proposal ---------------------------------------------------------- #

class TheProposal(unittest.TestCase):
    def _one(self, tag_row, performers, scenes):
        index = index_performers(performers)
        return proposal(tag_row, match_tag(tag_row, index), scenes,
                        folder=FOLDER)

    def test_the_whole_proposal_for_a_name_match(self):
        # The WHOLE dict, not a field at a time: an added key is as much a
        # change to what a reviewer approves as a changed one, and a payload
        # this project asserted field-by-field once let an unlisted key
        # through that would have blanked a rating on every scene it touched.
        got = self._one(tag(7, ASHGROVE, scene_count=2),
                        [performer(1, ASHGROVE)], ["sc-1", "sc-2"])

        self.assertEqual(got, {
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "7",
            "summary": "%s: also the performer %s, on 2 scenes"
                       % (ASHGROVE, ASHGROVE),
            "confidence": None,
            "payload": {
                "tag": {"name": ASHGROVE},
                "performer": {"id": "1", "name": ASHGROVE},
                "alias": None,
                "matches": [{"performer": {"id": "1", "name": ASHGROVE},
                             "alias": None}],
                "ambiguous": None,
                "scenes": ["sc-1", "sc-2"],
                "counts_cover": COUNTS_COVER,
            },
        })

    def test_the_whole_proposal_for_an_alias_match_names_the_alias(self):
        # HARM: "a name match is evidence, not proof" is the whole basis for
        # this being reviewed rather than automatic, and the alias is half of
        # what a reviewer weighs -- a tag that is a performer's NAME and a tag
        # that is one of eleven aliases somebody typed for them are different
        # strengths of evidence.
        got = self._one(tag(7, ASHGROVE), [performer(1, QUILL, aliases=[ASHGROVE])],
                        ["sc-1"])

        self.assertEqual(got["payload"], {
            "tag": {"name": ASHGROVE},
            "performer": {"id": "1", "name": QUILL},
            "alias": ASHGROVE,
            "matches": [{"performer": {"id": "1", "name": QUILL},
                         "alias": ASHGROVE}],
            "ambiguous": None,
            "scenes": ["sc-1"],
            "counts_cover": COUNTS_COVER,
        })
        self.assertEqual(
            got["summary"],
            "%s: also the performer %s, through their alias %r, on 1 scenes"
            % (ASHGROVE, QUILL, ASHGROVE))

    def test_two_performers_leave_the_payload_with_no_performer_at_all(self):
        # HARM: a payload naming one of the two would let the row offer
        # Approve, and `Actions` would write to every scene on the strength of
        # a choice nothing made.
        got = self._one(tag(7, ASHGROVE),
                        [performer(1, ASHGROVE),
                         performer(2, QUILL, aliases=[ASHGROVE])],
                        ["sc-1", "sc-2", "sc-3"])

        self.assertEqual(got["payload"], {
            "tag": {"name": ASHGROVE},
            "performer": None,
            "alias": None,
            "matches": [{"performer": {"id": "1", "name": ASHGROVE},
                         "alias": None},
                        {"performer": {"id": "2", "name": QUILL},
                         "alias": ASHGROVE}],
            "ambiguous": AMBIGUOUS,
            "scenes": ["sc-1", "sc-2", "sc-3"],
            "counts_cover": COUNTS_COVER,
        })

    def test_one_person_reached_two_ways_is_a_confident_match_not_a_finding(self):
        """No change needed at `_summary`'s `len(matches) > 1`: agreement can
        never reach it, because `index_performers` keys each surface's match
        by the PERFORMER's id, so one person found through their name and
        again through an alias is one `Match` before anything is counted.

        The fixture is that person: their name and an alias of theirs both
        normalise to this tag's key, so two surfaces genuinely claim it and
        only the deduplication makes them one. Without it this reads as "2
        performers answer to this name" -- an ambiguity manufactured out of
        one person, sent to a reviewer who has nothing to decide, and
        blocking the apply that `web.actions` refuses for a null performer.
        """
        got = self._one(tag(7, ASHGROVE),
                        [performer(1, ASHGROVE, aliases=["Delia-Ashgrove"])],
                        ["sc-1", "sc-2", "sc-3"])

        self.assertEqual(got["summary"],
                         "%s: also the performer %s, on 3 scenes"
                         % (ASHGROVE, ASHGROVE))
        self.assertEqual(got["payload"]["performer"],
                         {"id": "1", "name": ASHGROVE})
        self.assertIsNone(got["payload"]["ambiguous"])

    def test_the_summary_names_both_performers_and_the_count(self):
        got = self._one(tag(7, ASHGROVE),
                        [performer(1, ASHGROVE),
                         performer(2, QUILL, aliases=[ASHGROVE])],
                        ["sc-1", "sc-2", "sc-3"])

        self.assertEqual(
            got["summary"],
            "%s: 2 performers answer to this name (%s, %s), on 3 scenes -- "
            "nothing here can say which" % (ASHGROVE, ASHGROVE, QUILL))

    def test_the_three_summaries_are_three_different_sentences(self):
        # Collapsing them into one catch-all phrasing that mentioned the
        # performer and the count is a mutation that would satisfy every
        # assertion above about "the summary names the performer" while
        # telling a reviewer nothing about which of the three they have.
        name = self._one(tag(7, ASHGROVE), [performer(1, ASHGROVE)], ["s"])
        alias = self._one(tag(7, ASHGROVE),
                          [performer(1, QUILL, aliases=[ASHGROVE])], ["s"])
        ambiguous = self._one(tag(7, ASHGROVE),
                             [performer(1, ASHGROVE),
                              performer(2, QUILL, aliases=[ASHGROVE])], ["s"])
        summaries = [name["summary"], alias["summary"], ambiguous["summary"]]

        self.assertEqual(len(set(summaries)), 3, summaries)

    def test_the_count_in_the_summary_is_the_scenes_it_will_touch(self):
        # HARM: the blast radius is the number that decides whether this write
        # is safe. A summary quoting the tag's own recorded count instead would
        # be a number nothing here measured.
        got = self._one(tag(7, ASHGROVE, scene_count=41),
                        [performer(1, ASHGROVE)], ["sc-1", "sc-2", "sc-3"])

        self.assertIn("on 3 scenes", got["summary"])
        self.assertNotIn("41", got["summary"])

    def test_the_scenes_are_recorded_in_order_as_text(self):
        got = self._one(tag(7, ASHGROVE), [performer(1, ASHGROVE)], [9, 8, 7])

        self.assertEqual(got["payload"]["scenes"], ["9", "8", "7"])

    def test_a_proposal_with_no_match_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            proposal(tag(7, LANTERN), (), ["sc-1"], folder=FOLDER)
        self.assertIn(LANTERN, str(caught.exception))

    def test_a_proposal_with_no_scenes_is_refused(self):
        # HARM: a blast radius of nothing reads on the page exactly like a
        # reconciliation that is safe because it is small.
        index = index_performers([performer(1, ASHGROVE)])
        with self.assertRaises(ValueError) as caught:
            proposal(tag(7, ASHGROVE), match_tag(tag(7, ASHGROVE), index), [],
                     folder=FOLDER)
        self.assertIn("no scenes", str(caught.exception))


# -- what the pass proposes ------------------------------------------------ #

class ThePass(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_a_tag_matching_a_performers_alias_is_proposed_end_to_end(self):
        stash = FakeStash(tags=[tag(7, ASHGROVE, scene_count=2)],
                          performers=[performer(1, QUILL, aliases=[ASHGROVE])],
                          scenes={"7": [scene_row("sc-1"), scene_row("sc-2")]})

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual([p["subject_id"] for p in proposals], ["7"])
        self.assertEqual(proposals[0]["payload"]["performer"],
                         {"id": "1", "name": QUILL})
        self.assertEqual(proposals[0]["payload"]["alias"], ASHGROVE)
        self.assertEqual(counts.matched, 1)
        self.assertEqual(counts.outstanding, 1)

    def test_a_tag_matching_no_performer_produces_no_proposal(self):
        stash = FakeStash(tags=[tag(7, LANTERN, scene_count=2)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual(proposals, [])
        self.assertEqual(counts.matched, 0)
        self.assertEqual(counts.outstanding, 0)

    def test_the_count_is_the_scenes_that_carry_the_tag_not_the_tags_own(self):
        # HARM: the tag's recorded `scene_count` and the scenes a
        # reconciliation would actually write to are two different numbers --
        # the server's count is computed elsewhere, and can include what this
        # write will not touch. The blast radius has to be the set this pass
        # measured, and the payload carries that set so the number shown and
        # the scenes it describes cannot drift apart.
        stash = FakeStash(tags=[tag(7, ASHGROVE, scene_count=41)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1"), scene_row("sc-2"),
                                        scene_row("sc-3")]})

        proposals, _ = _reconciliations(stash, self.store)

        self.assertEqual(proposals[0]["payload"]["scenes"],
                         ["sc-1", "sc-2", "sc-3"])
        self.assertIn("on 3 scenes", proposals[0]["summary"])

    def test_the_whole_library_of_scenes_is_asked_for_not_a_page(self):
        # HARM: `tagged_scenes`' `limit` is required and a number truncates the
        # worklist. A blast radius counted off a truncated read under-reports
        # exactly the tags that need review most.
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})

        _reconciliations(stash, self.store)

        self.assertIn(("tagged_scenes", "7", None), stash.calls)

    def test_a_matched_tag_on_no_scenes_is_counted_and_not_proposed(self):
        # HARM: this is also what stops an applied reconciliation coming back
        # every night -- the apply leaves the tag on no scenes.
        stash = FakeStash(tags=[tag(7, ASHGROVE, scene_count=3)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={})

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual(proposals, [])
        self.assertEqual((counts.matched, counts.unused, counts.outstanding),
                         (1, 1, 0))

    def test_a_tag_with_a_live_proposal_is_not_proposed_again(self):
        # HARM: a payload carrying scene ids changes whenever a scene is tagged
        # or untagged, so a moved set is a different fingerprint and a SECOND
        # row offering to write to thousands of scenes. Pinned with the tag
        # still ON its scenes, so the empty-scene rule above cannot be what
        # makes this pass.
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})
        first, _ = _reconciliations(stash, self.store)
        self.store.record(producer="tag-merge", **first[0])

        again, counts = _reconciliations(stash, self.store)

        self.assertEqual(again, [])
        self.assertEqual(
            (counts.matched, counts.already_proposed, counts.outstanding),
            (1, 1, 0))

    def test_a_muted_tag_is_not_proposed_again(self):
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})
        self.store.mute(SUBJECT_TYPE, "7", reason="not that person")

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual(proposals, [])
        self.assertEqual((counts.matched, counts.muted, counts.outstanding),
                         (1, 1, 0))

    def test_muting_a_description_for_a_tag_does_not_silence_this(self):
        # HARM: one subject type shared with `tag_descriptions` would make
        # "stop offering me a description for this tag" also mean "stop telling
        # me it is a performer" -- two different standing decisions, silenced
        # by one click on the wrong one.
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})
        self.store.mute(TAG_DESCRIPTION_SUBJECT, "7", reason="no description")
        self.store.mute(CLUSTER_SUBJECT, "deliaashgrove", reason="no merge")

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual([p["subject_id"] for p in proposals], ["7"])
        self.assertEqual(counts.muted, 0)

    def test_a_proposal_for_another_folder_does_not_suppress_this_one(self):
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})
        first, _ = _reconciliations(stash, self.store)
        elsewhere = dict(first[0], folder="other")
        self.store.record(producer="tag-merge", **elsewhere)

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual([p["subject_id"] for p in proposals], ["7"])
        self.assertEqual(counts.already_proposed, 0)

    def test_a_tag_with_a_duplicate_spelling_is_left_to_its_merge(self):
        # HARM: a merge DELETES the losing spellings. A reconciliation approved
        # after one would name a tag id the server no longer has; one approved
        # before it would move the associations out from under the merge a
        # reviewer is looking at in the row above.
        tags = [tag(7, ASHGROVE), tag(8, "DeliaAshgrove")]
        stash = FakeStash(tags=tags, performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")],
                                  "8": [scene_row("sc-2")]})
        producer = _producer(stash, self.store)
        from cronicled.tags import cluster_tags

        proposals, counts = producer._reconcile(tags, cluster_tags(tags))

        self.assertEqual(proposals, [])
        self.assertEqual((counts.matched, counts.clustered, counts.outstanding),
                         (2, 2, 0))

    def test_a_tag_matching_two_performers_is_proposed_as_a_finding(self):
        stash = FakeStash(
            tags=[tag(7, ASHGROVE)],
            performers=[performer(1, ASHGROVE),
                        performer(2, QUILL, aliases=[ASHGROVE])],
            scenes={"7": [scene_row("sc-1")]})

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual(len(proposals), 1)
        self.assertIsNone(proposals[0]["payload"]["performer"])
        self.assertEqual(counts.ambiguous, 1)
        self.assertEqual(counts.outstanding, 1)

    def test_a_tag_one_person_answers_to_twice_is_not_counted_ambiguous(self):
        """No change needed at `_reconcile`'s `len(matches) > 1`: it is a
        TALLY, not a refusal -- an ambiguous tag is still proposed, with the
        performer left null for a person to name -- and agreement cannot
        reach it anyway, because `index_performers` has already folded one
        person's two surfaces into one `Match`.

        The fixture is deliberately asymmetric and larger than one: three
        matched tags of which TWO are genuinely ambiguous and one is the same
        person reached through their name and an alias. A fixture of one
        cannot tell `ambiguous += 1` from `ambiguous = 1`, and one where every
        tag is ambiguous cannot tell the condition from its inverse.
        """
        vale = "Perrin Vale"
        stash = FakeStash(
            tags=[tag(7, ASHGROVE), tag(8, FENN), tag(9, vale)],
            performers=[performer(1, ASHGROVE),
                        performer(2, QUILL, aliases=[ASHGROVE]),
                        performer(3, FENN),
                        performer(4, WREN, aliases=[FENN]),
                        performer(5, vale, aliases=["Perrin-Vale"])],
            scenes={"7": [scene_row("sc-1")], "8": [scene_row("sc-2")],
                    "9": [scene_row("sc-3")]})

        proposals, counts = _reconciliations(stash, self.store)

        self.assertEqual(counts.outstanding, 3)
        self.assertEqual(counts.ambiguous, 2)
        agreed = [p for p in proposals if p["subject_id"] == "9"]
        self.assertEqual(agreed[0]["payload"]["performer"],
                         {"id": "5", "name": vale})

    def test_the_performers_are_read_once_however_many_tags_match(self):
        # HARM: a lookup per tag is one request per tag against an answer one
        # read already holds -- thousands of them, on a nightly pass.
        stash = FakeStash(
            tags=[tag(7, ASHGROVE), tag(8, QUILL), tag(9, LANTERN)],
            performers=[performer(1, ASHGROVE), performer(2, QUILL)],
            scenes={"7": [scene_row("sc-1")], "8": [scene_row("sc-2")]})

        _reconciliations(stash, self.store)

        self.assertEqual(
            len([c for c in stash.calls if c[0] == "performers_with_aliases"]),
            1)

    def test_only_the_surviving_tags_scenes_are_read(self):
        # The scene read is the only per-tag request this half makes, and every
        # narrowing above it costs nothing: a muted tag is never asked about.
        stash = FakeStash(
            tags=[tag(7, ASHGROVE), tag(8, QUILL), tag(9, LANTERN)],
            performers=[performer(1, ASHGROVE), performer(2, QUILL)],
            scenes={"7": [scene_row("sc-1")], "8": [scene_row("sc-2")]})
        self.store.mute(SUBJECT_TYPE, "8", reason="not that person")

        _reconciliations(stash, self.store)

        self.assertEqual([c for c in stash.calls if c[0] == "tagged_scenes"],
                         [("tagged_scenes", "7", None)])


class TheCounts(unittest.TestCase):
    """Why a night's matched tags became the reconciliations it proposed."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _library(self):
        """A library with a DIFFERENT number of tags in every outcome -- 2
        clustered, 3 muted, 4 already proposed, 5 on no scenes, 6 proposed, 2 of
        those 6 naming two performers.

        Deliberately asymmetric, and every count deliberately above ONE. A
        fixture with a single tag in a bucket cannot tell an accumulation from
        an assignment -- `n += 1` and `n = 1` agree at one -- and this project
        has had a counting mutation survive for exactly that reason. It also
        cannot tell one field from another that happens to hold the same
        number, which is why no two of these are equal.
        """
        tags, performers, scenes = [], [], {}

        def add(id, name, on_scenes=1, rival=None):
            tags.append(tag(id, name, scene_count=on_scenes))
            performers.append(performer("p" + id, name))
            if rival is not None:
                # A second performer answering to the same name through an
                # alias -- which is what makes this tag a finding rather than a
                # reconciliation.
                performers.append(performer("r" + id, rival, aliases=[name]))
            if on_scenes:
                scenes[id] = [scene_row("sc-%s-%d" % (id, n))
                              for n in range(on_scenes)]

        # 2 clustered: one name written two ways, both matching one performer.
        tags.append(tag("c1", "Bracken Vale", scene_count=1))
        tags.append(tag("c2", "BrackenVale", scene_count=1))
        performers.append(performer("pc", "Bracken Vale"))
        scenes["c1"] = [scene_row("sc-c1")]
        scenes["c2"] = [scene_row("sc-c2")]
        # 3 muted
        for n in range(3):
            add("m%d" % n, "Corvin Slate %d" % n)
        # 4 already proposed
        for n in range(4):
            add("a%d" % n, "Aster Pike %d" % n)
        # 5 on no scenes
        for n in range(5):
            add("u%d" % n, "Umber Finch %d" % n, on_scenes=0)
        # 6 proposed, two of which name two performers each
        for n in range(6):
            add("s%d" % n, "Selby Marsh %d" % n,
                rival=("Rowan Teasel %d" % n) if n < 2 else None)
        # 7 tags nothing matches at all, which must not be in `matched`
        for n in range(7):
            tags.append(tag("n%d" % n, "%s %d" % (LANTERN, n), scene_count=1))
        return tags, performers, scenes

    def test_the_identity_holds_over_every_outcome_at_once(self):
        tags, performers, scenes = self._library()
        stash = FakeStash(tags=tags, performers=performers, scenes=scenes)
        producer = _producer(stash, self.store)
        from cronicled.tags import cluster_tags
        clusters = cluster_tags(tags)
        for n in range(3):
            self.store.mute(SUBJECT_TYPE, "m%d" % n, reason="not that person")
        for n in range(4):
            self.store.record(
                folder=FOLDER, subject_type=SUBJECT_TYPE, subject_id="a%d" % n,
                summary="already", payload={"whatever": n},
                producer="tag-merge")

        proposals, counts = producer._reconcile(tags, clusters)

        self.assertEqual(counts, Counts(
            matched=20, clustered=2, muted=3, already_proposed=4, unused=5,
            outstanding=6, ambiguous=2))
        self.assertEqual(
            counts.matched,
            counts.clustered + counts.muted + counts.already_proposed
            + counts.unused + counts.outstanding)
        self.assertEqual(len(proposals), 6)
        self.assertEqual(
            len([p for p in proposals if p["payload"]["performer"] is None]),
            counts.ambiguous)


class TheClosingLine(unittest.TestCase):
    """What an operator can actually read after the pass finishes.

    `JobRunner._log` keeps ONE message field, so every line a producer writes
    before its last is overwritten before anybody sees it. A count reported
    only where it is computed is a number nobody can ever read.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _run(self, stash):
        ctx = FakeCtx()
        list(_producer(stash, self.store).produce(ctx))
        return ctx.message

    def test_the_closing_line_carries_the_performer_half(self):
        stash = FakeStash(tags=[tag(7, ASHGROVE), tag(8, LANTERN)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})

        message = self._run(stash)

        self.assertIn("1 tags also name a performer", message)
        self.assertIn("1 reconciliations proposed", message)

    def test_the_closing_line_still_carries_the_other_two_halves(self):
        # HARM: three halves share one message field. A line that reported the
        # newest one by replacing the closing line would silently delete the
        # merge and description tallies from every run.
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})

        message = self._run(stash)

        self.assertIn("1 tags, 0 clusters, 0 proposed", message)
        self.assertIn("descriptions proposed", message)
        self.assertIn("tags also name a performer", message)

    def test_suppressed_findings_do_not_read_as_nothing_to_do(self):
        # HARM: "0 reconciliations proposed" reads identically for a library
        # with no mis-filed tags and for one whose every finding a reviewer has
        # already muted, and those call for opposite responses.
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})
        self.store.mute(SUBJECT_TYPE, "7", reason="not that person")

        message = self._run(stash)

        self.assertIn("0 reconciliations proposed", message)
        self.assertIn("1 muted", message)

    def test_a_finding_nobody_can_approve_is_named_in_the_line(self):
        stash = FakeStash(
            tags=[tag(7, ASHGROVE)],
            performers=[performer(1, ASHGROVE),
                        performer(2, QUILL, aliases=[ASHGROVE])],
            scenes={"7": [scene_row("sc-1")]})

        message = self._run(stash)

        self.assertIn("1 of them name two or more performers", message)

    def test_nothing_ambiguous_says_nothing_about_it(self):
        # A permanent "0 of them name two performers" is noise, and a line that
        # is mostly noise stops being read.
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})

        self.assertNotIn("name two or more performers", self._run(stash))

    def test_the_pass_yields_the_reconciliations_it_proposes(self):
        stash = FakeStash(tags=[tag(7, ASHGROVE)],
                          performers=[performer(1, ASHGROVE)],
                          scenes={"7": [scene_row("sc-1")]})

        yielded = list(_producer(stash, self.store).produce(FakeCtx()))

        self.assertEqual([p["subject_type"] for p in yielded], [SUBJECT_TYPE])


class TheNarrowings(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_only_this_modules_own_subject_is_read_from_either_table(self):
        self.store.mute(SUBJECT_TYPE, "7", reason="mine")
        self.store.mute(TAG_DESCRIPTION_SUBJECT, "8", reason="not mine")
        self.store.record(folder=FOLDER, subject_type=SUBJECT_TYPE,
                          subject_id="9", summary="mine", payload={"a": 1},
                          producer="tag-merge")
        self.store.record(folder=FOLDER, subject_type=CLUSTER_SUBJECT,
                          subject_id="10", summary="not mine", payload={"a": 2},
                          producer="tag-merge")

        muted, proposed = narrowings(self.store, FOLDER)

        self.assertEqual(muted, {"7"})
        self.assertEqual(proposed, {"9"})


# -- the write, and taking it back ----------------------------------------- #

class _ReconcileTransport:
    """Fake transport for the reconciliation write paths. Opens no socket.

    Answers the ONE read the client makes -- `findScenes` for the worklist,
    whose rows carry each scene's performers and tags -- and records every
    request verbatim. It deliberately does NOT answer `findScene`: a return to
    a read per scene is the defect this path was rewritten to remove, and here
    it raises rather than quietly costing two requests a scene again.

    Recording the whole bulk input is the point. `BulkUpdateIdMode` offers
    `SET` beside `ADD` and `REMOVE`, and a `SET` carrying one performer id
    strips every OTHER performer from every scene in the batch. A test that
    asserted only that a bulk update happened would pass on it.

    `fail_on` is a scene id whose whole batch is refused, so a partial run can
    be driven rather than assumed.
    """

    def __init__(self, worklist=(), fail_on=None):
        self.calls = []
        self.worklist = list(worklist)
        self.fail_on = fail_on

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        if "findScenes(" in q:
            return {"data": {"findScenes": {"count": len(self.worklist),
                                            "scenes": self.worklist}}}
        if "bulkSceneUpdate(" in q:
            ids = variables["in"]["ids"]
            if self.fail_on is not None and self.fail_on in ids:
                return {"errors": [{"message": "the server said no"}]}
            return {"data": {"bulkSceneUpdate": [{"id": i} for i in ids]}}
        raise AssertionError("test transport does not recognize query: %s" % q)

    def updates(self):
        """Every bulk update sent, as its whole input dictionary."""
        return [v["in"] for q, v in self.calls if "bulkSceneUpdate(" in q]

    def reads(self):
        return [v for q, v in self.calls if "findScenes(" in q]

    def mutations(self):
        return [q for q, _ in self.calls if q.lstrip().startswith("mutation")]


def _stash(transport):
    return Stash("http://example.test", "k", transport=transport)


# Enough scenes to cross a chunk boundary twice and leave a short tail, so a
# fixture cannot pass by accident on an implementation that happens to send one
# request, and the last chunk is visibly a different size from the first two.
_MANY = 450


def _many_rows(already=()):
    """`_MANY` worklist rows carrying the tag, `already` of them also carrying
    the performer already."""
    return [scene_row("sc-%03d" % n, performers=(["p-1"] if n in already
                                                 else []), tags=["t-7"])
            for n in range(_MANY)]


class TheWrite(unittest.TestCase):
    def test_one_read_and_one_write_carry_the_whole_reconciliation(self):
        t = _ReconcileTransport(
            worklist=[scene_row("sc-1", performers=["p-9"],
                                tags=["t-7", "t-4"]),
                      scene_row("sc-2", tags=["t-7"])])

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        # The WHOLE input, so an added key -- `organized`, a rating, a title --
        # fails here, and so does either mode drifting to `SET`. `apply_scene`
        # sets `organized: True` on everything it writes, which would be a
        # second, unasked-for write to every scene this touches; `SET` on
        # either field would empty the other one on all of them.
        self.assertEqual(t.updates(), [
            {"ids": ["sc-1", "sc-2"],
             "performer_ids": {"ids": ["p-1"], "mode": "ADD"},
             "tag_ids": {"ids": ["t-7"], "mode": "REMOVE"}},
        ])
        self.assertEqual(got["prior"], {
            "tag_id": "t-7", "performer_id": "p-1",
            "attached": ["sc-1", "sc-2"], "untagged": ["sc-1", "sc-2"]})
        self.assertEqual(got["failures"], [])
        self.assertEqual(got["skipped"], [])

    def test_the_requests_do_not_grow_with_the_number_of_scenes(self):
        # HARM: the loop this replaced issued a read and a write per scene --
        # roughly 8000 requests on the library that motivated the feature. A
        # test asserting "some bulk call happened" passes with the per-scene
        # path still there beside it.
        for count in (1, 3, 6):
            with self.subTest(scenes=count):
                t = _ReconcileTransport(
                    worklist=[scene_row("sc-%d" % n, tags=["t-7"])
                              for n in range(count)])

                _stash(t).reconcile_tag_to_performer("t-7", "p-1")

                self.assertEqual(len(t.calls), 2)

    def test_the_tag_itself_is_not_deleted(self):
        # HARM: a tag may legitimately share a name with a performer -- a
        # studio and its owner, a series named after its creator -- and
        # deleting on a name match destroys a distinction nothing here can see.
        # It is also the only half of this write that cannot be taken back.
        t = _ReconcileTransport(worklist=[scene_row("sc-1", tags=["t-7"])])

        _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        self.assertEqual(len(t.mutations()), 1)
        self.assertIn("bulkSceneUpdate(", t.mutations()[0])
        for query, _ in t.calls:
            self.assertNotIn("tagDestroy", query)
            self.assertNotIn("tagsMerge", query)

    def test_a_scene_already_carrying_the_performer_is_untagged_not_attached(self):
        # HARM: the snapshot's two halves are what make the undo exact. A scene
        # that already had the performer, recorded as attached, would have that
        # performer removed by an undo -- taking away somebody else's work.
        t = _ReconcileTransport(
            worklist=[scene_row("sc-1", performers=["p-1"], tags=["t-7"]),
                      scene_row("sc-2", tags=["t-7"])])

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        # Both scenes are still written to: ADD of a performer a scene has is a
        # no-op, and the tag has to come off both.
        self.assertEqual([u["ids"] for u in t.updates()], [["sc-1", "sc-2"]])
        self.assertEqual(got["prior"]["attached"], ["sc-2"])
        self.assertEqual(got["prior"]["untagged"], ["sc-1", "sc-2"])

    def test_a_scene_the_read_does_not_report_as_tagged_is_skipped_not_written(self):
        # HARM: REMOVE of a tag a scene lacks is a no-op, but ADD of the
        # performer is not. A row that came back without the tag on it -- a
        # filter that means something other than what was assumed, a scene
        # untagged between the query and the reply -- would have the performer
        # attached on the strength of a filter nobody checked, and the snapshot
        # would then have an undo detach it again from a scene this never
        # should have touched.
        t = _ReconcileTransport(
            worklist=[scene_row("sc-1", tags=["t-4"]),
                      scene_row("sc-2", tags=["t-7"])])

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        self.assertEqual([u["ids"] for u in t.updates()], [["sc-2"]])
        self.assertEqual(got["skipped"], ["sc-1"])
        self.assertEqual(got["prior"]["untagged"], ["sc-2"])
        self.assertEqual(got["prior"]["attached"], ["sc-2"])
        # A skipped scene is still part of what was READ, and the denominator
        # a partial run is reported against ("wrote 1 of 2 scenes") is that
        # whole read -- not the subset this decided to write to.
        self.assertEqual(got["worklist"], ["sc-1", "sc-2"])

    def test_the_worklist_is_read_fresh_and_asks_for_every_scene(self):
        t = _ReconcileTransport(worklist=[])

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        self.assertEqual(len(t.reads()), 1)
        self.assertEqual(t.reads()[0]["s"],
                         {"tags": {"value": ["t-7"], "modifier": "INCLUDES"}})
        self.assertEqual(t.reads()[0]["f"]["per_page"], -1)
        self.assertEqual(got["prior"]["untagged"], [])
        # Nothing at all is written for an empty worklist -- not a bulk update
        # naming no scenes.
        self.assertEqual(t.updates(), [])

    def test_the_worklist_is_written_in_chunks_that_partition_it_in_order(self):
        # HARM: one request naming four thousand scenes may be one the server
        # refuses whole. The sizes are literals rather than derived from
        # `Stash._BULK_SCENE_CHUNK`, so a change to that constant shows up here
        # as a failing test rather than as a fixture quietly following it.
        t = _ReconcileTransport(worklist=_many_rows())

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        sent = [u["ids"] for u in t.updates()]
        self.assertEqual([len(ids) for ids in sent], [200, 200, 50])
        # Every scene in exactly one chunk, in the worklist's own order.
        self.assertEqual([i for ids in sent for i in ids], got["worklist"])
        # And every chunk carries both fields with both modes -- not just the
        # first one.
        for update in t.updates():
            self.assertEqual(update["performer_ids"],
                             {"ids": ["p-1"], "mode": "ADD"})
            self.assertEqual(update["tag_ids"],
                             {"ids": ["t-7"], "mode": "REMOVE"})
        self.assertEqual(got["prior"]["untagged"], got["worklist"])

    def test_scenes_that_already_had_the_performer_are_excluded_across_chunks(self):
        # HARM: the `attached` half is what an undo detaches from. A chunk
        # whose bookkeeping was done from the chunk's ids rather than from what
        # the read said would put every scene in it into `attached`, and the
        # undo would strip the performer from the ones that came with it.
        already = {0, 199, 200, 449}
        t = _ReconcileTransport(worklist=_many_rows(already=already))

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        expected = ["sc-%03d" % n for n in range(_MANY) if n not in already]
        self.assertEqual(got["prior"]["attached"], expected)
        self.assertEqual(got["prior"]["untagged"],
                         ["sc-%03d" % n for n in range(_MANY)])

    def test_a_refused_batch_stops_the_run_and_keeps_what_landed(self):
        # HARM: raising here would discard the record of the scenes this call
        # really did change, which is the one thing that makes a partial run
        # recoverable. Continuing would hammer a server that has just said no.
        t = _ReconcileTransport(worklist=_many_rows(), fail_on="sc-250")

        got = _stash(t).reconcile_tag_to_performer("t-7", "p-1")

        first, second = ["sc-%03d" % n for n in range(200)], \
            ["sc-%03d" % n for n in range(200, 400)]
        self.assertEqual([u["ids"] for u in t.updates()], [first, second])
        # The refused batch is in NEITHER half: whether the server wrote any of
        # it before refusing is not knowable from here, and an undo that
        # claimed it would detach a performer from scenes that never got one.
        self.assertEqual(got["prior"]["untagged"], first)
        self.assertEqual(got["prior"]["attached"], first)
        self.assertEqual(len(got["failures"]), 1)
        self.assertEqual(got["failures"][0]["scenes"], second)
        self.assertIn("the server said no", got["failures"][0]["error"])
        self.assertEqual(got["worklist"], ["sc-%03d" % n for n in range(_MANY)])

    def test_a_bulk_write_may_only_carry_add_or_remove(self):
        # HARM: `SET` replaces the whole list. One performer id sent with it
        # would leave every scene in the batch with that performer and no
        # other, and one tag id would leave them with that tag and no other --
        # hundreds of records per request, and nothing reads them back.
        # Reached directly because no caller passes it: the point is that the
        # method refuses it, not that today's callers happen not to send it.
        t = _ReconcileTransport()
        for mode in ("SET", "set", "add", None, ""):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    _stash(t)._bulk_scene_write(
                        ["sc-1"], {"tag_ids": {"ids": ["t-7"], "mode": mode}})
                self.assertIn("SET", str(caught.exception))
        self.assertEqual(t.calls, [])

    def test_every_field_is_checked_not_just_the_first(self):
        # HARM: the reconciliation sends two fields in one input. A check that
        # stopped at the first would let a `SET` on the second through, and the
        # second is the one that empties a scene's tag list.
        t = _ReconcileTransport()

        with self.assertRaises(ValueError) as caught:
            _stash(t)._bulk_scene_write(
                ["sc-1"],
                {"performer_ids": {"ids": ["p-1"], "mode": "ADD"},
                 "tag_ids": {"ids": ["t-7"], "mode": "SET"}})

        self.assertIn("tag_ids", str(caught.exception))
        self.assertEqual(t.calls, [])

    def test_a_bulk_field_with_no_mode_at_all_is_refused(self):
        # A missing mode raises rather than defaulting: the default that would
        # be convenient here is one of the three values the check exists to
        # constrain.
        t = _ReconcileTransport()

        with self.assertRaises(ValueError):
            _stash(t)._bulk_scene_write(["sc-1"], {"tag_ids": {"ids": ["t-7"]}})

        self.assertEqual(t.calls, [])


class TakingItBack(unittest.TestCase):
    def test_both_halves_are_restored_over_exactly_the_recorded_ids(self):
        # HARM: the whole input, not a count. A revert that put the tag back
        # and left the performer attached, one that detached the performer from
        # a scene that already had them, or one that sent `SET` for either
        # leaves the library in a third state that is neither before nor after
        # -- and reports success.
        prior = {"tag_id": "t-7", "performer_id": "p-1",
                 "attached": ["sc-2"], "untagged": ["sc-1", "sc-2"]}
        t = _ReconcileTransport()

        got = _stash(t).revert_reconcile("t-7", prior)

        self.assertEqual(t.updates(), [
            # The tag first, over everything it came off, so no scene is left
            # holding neither the tag nor the performer in between.
            {"ids": ["sc-1", "sc-2"],
             "tag_ids": {"ids": ["t-7"], "mode": "ADD"}},
            # The performer only from the scenes that did not already carry it.
            # sc-1 did, and keeps it.
            {"ids": ["sc-2"],
             "performer_ids": {"ids": ["p-1"], "mode": "REMOVE"}},
        ])
        self.assertEqual(got, {"detached": ["sc-2"],
                               "retagged": ["sc-1", "sc-2"]})

    def test_nothing_is_detached_when_every_scene_already_had_the_performer(self):
        # HARM: this is the whole reason the snapshot has two halves. An undo
        # that detached from everything it re-tagged would take the performer
        # off scenes somebody else attached them to, before this ever ran.
        prior = {"tag_id": "t-7", "performer_id": "p-1",
                 "attached": [], "untagged": ["sc-1", "sc-2"]}
        t = _ReconcileTransport()

        got = _stash(t).revert_reconcile("t-7", prior)

        self.assertEqual(t.updates(), [
            {"ids": ["sc-1", "sc-2"],
             "tag_ids": {"ids": ["t-7"], "mode": "ADD"}}])
        self.assertEqual(got, {"detached": [], "retagged": ["sc-1", "sc-2"]})

    def test_nothing_is_retagged_when_the_snapshot_untagged_nothing(self):
        prior = {"tag_id": "t-7", "performer_id": "p-1",
                 "attached": ["sc-1"], "untagged": []}
        t = _ReconcileTransport()

        got = _stash(t).revert_reconcile("t-7", prior)

        self.assertEqual(t.updates(), [
            {"ids": ["sc-1"],
             "performer_ids": {"ids": ["p-1"], "mode": "REMOVE"}}])
        self.assertEqual(got, {"detached": ["sc-1"], "retagged": []})

    def test_the_undo_never_asks_which_scenes_carry_the_tag_now(self):
        # HARM: the tag has moved on -- the reconciliation took it off these
        # scenes, so what carries it now is either a scene the run failed to
        # reach or one somebody tagged since. The transport's worklist is a
        # different scene entirely, so a revert that re-read the tag would name
        # it here.
        prior = {"tag_id": "t-7", "performer_id": "p-1",
                 "attached": ["sc-1"], "untagged": ["sc-1"]}
        t = _ReconcileTransport(worklist=[scene_row("sc-9", tags=["t-7"])])

        _stash(t).revert_reconcile("t-7", prior)

        self.assertEqual(t.reads(), [])
        self.assertEqual([u["ids"] for u in t.updates()],
                         [["sc-1"], ["sc-1"]])

    def test_each_half_is_chunked_the_same_way_the_write_was(self):
        ids = ["sc-%03d" % n for n in range(_MANY)]
        prior = {"tag_id": "t-7", "performer_id": "p-1",
                 "attached": list(ids), "untagged": list(ids)}
        t = _ReconcileTransport()

        _stash(t).revert_reconcile("t-7", prior)

        shape = [("tag_ids" if "tag_ids" in u else "performer_ids",
                  len(u["ids"])) for u in t.updates()]
        self.assertEqual(shape, [("tag_ids", 200), ("tag_ids", 200),
                                 ("tag_ids", 50), ("performer_ids", 200),
                                 ("performer_ids", 200), ("performer_ids", 50)])
        self.assertEqual([i for u in t.updates()
                          if "tag_ids" in u for i in u["ids"]], ids)
        self.assertEqual([i for u in t.updates()
                          if "performer_ids" in u for i in u["ids"]], ids)

    def test_a_snapshot_naming_another_tag_is_refused(self):
        # HARM: applying it would put a DIFFERENT tag onto these scenes.
        prior = {"tag_id": "t-99", "performer_id": "p-1",
                 "attached": [], "untagged": ["sc-1"]}
        t = _ReconcileTransport()

        with self.assertRaises(ValueError) as caught:
            _stash(t).revert_reconcile("t-7", prior)
        self.assertIn("t-99", str(caught.exception))
        self.assertEqual(t.calls, [])

    def test_a_missing_or_incomplete_snapshot_is_refused(self):
        # A revert that no-ops is indistinguishable from one that worked.
        whole = {"tag_id": "t-7", "performer_id": "p-1",
                 "attached": [], "untagged": []}
        cases = [None, {}]
        cases.extend({k: v for k, v in whole.items() if k != missing}
                     for missing in whole)
        for prior in cases:
            with self.subTest(prior=prior):
                t = _ReconcileTransport()
                with self.assertRaises(ValueError):
                    _stash(t).revert_reconcile("t-7", prior)
                self.assertEqual(t.calls, [])

    def test_an_empty_snapshot_of_a_run_that_changed_nothing_is_refused(self):
        # `attached` and `untagged` both empty is a run that wrote nothing, and
        # `Actions` never records a snapshot for one -- but reaching here with
        # it must not answer as though something was restored.
        t = _ReconcileTransport()
        got = _stash(t).revert_reconcile(
            "t-7", {"tag_id": "t-7", "performer_id": "p-1",
                    "attached": [], "untagged": []})

        self.assertEqual(got, {"detached": [], "retagged": []})
        self.assertEqual(t.calls, [])


# -- the performer read ---------------------------------------------------- #

class _PerformerAliasTransport:
    def __init__(self, pages=None, count=None, max_pages=6):
        self.calls = []
        self.pages = [[]] if pages is None else pages
        self.count = count
        self.max_pages = max_pages
        self.requests = 0

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        if "findPerformers(" not in q:
            raise AssertionError("unrecognized query: %s" % q)
        self.requests += 1
        if self.requests > self.max_pages:
            raise AssertionError(
                "the client is still asking for performer pages after %d "
                "requests -- it never stopped" % self.max_pages)
        page = (variables.get("f") or {}).get("page", 1)
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        total = sum(len(p) for p in self.pages)
        return {"data": {"findPerformers": {
            "count": total if self.count is None else self.count,
            "performers": rows}}}


PAGE = 500


def _performer_rows(n, prefix):
    return [performer("%s-%d" % (prefix, i), "%s %d" % (prefix, i),
                      aliases=["%s alt %d" % (prefix, i)])
            for i in range(n)]


class PerformersWithAliases(unittest.TestCase):
    def test_the_selection_set_carries_the_alias_field_performers_use(self):
        # HARM: `alias_list` is what the server calls a PERFORMER's other
        # spellings; `aliases` is a tag's and a studio's. Select the wrong one
        # and the server rejects the query -- or, against a double that never
        # parses it, the aliases silently arrive empty and the tags this ticket
        # exists to find stop being found, with nothing failing.
        t = _PerformerAliasTransport(pages=[_performer_rows(1, "alpha")])

        _stash(t).performers_with_aliases()

        query = t.calls[0][0]
        self.assertIn("findPerformers(filter:$f)", query)
        self.assertIn("id name alias_list", query)

    def test_it_pages_past_the_first_page(self):
        # HARM: the index is built from this whole list. Stopping at the first
        # page means every performer whose name sorts past it is invisible to
        # the match, and the tags naming them are silently never proposed.
        pages = [_performer_rows(PAGE, "alpha"), _performer_rows(3, "omega")]
        t = _PerformerAliasTransport(pages=pages)

        got = _stash(t).performers_with_aliases()

        self.assertEqual(got, pages[0] + pages[1])
        self.assertEqual([v["f"]["page"] for _, v in t.calls], [1, 2])

    def test_a_short_page_ends_the_read_even_when_the_count_over_reports(self):
        t = _PerformerAliasTransport(pages=[_performer_rows(4, "alpha")],
                                    count=PAGE * 3)

        got = _stash(t).performers_with_aliases()

        self.assertEqual(len(got), 4)
        self.assertEqual(len(t.calls), 1)

    def test_a_full_page_that_completes_the_count_ends_the_read(self):
        t = _PerformerAliasTransport(pages=[_performer_rows(PAGE, "alpha")])

        got = _stash(t).performers_with_aliases()

        self.assertEqual(len(got), PAGE)
        self.assertEqual(len(t.calls), 1)


# -- approving one, and taking it back ------------------------------------- #

class _RecordingStash:
    """The two writes an approve and an undo make, and nothing else."""

    def __init__(self, result=None, raises=None, revert=None):
        self.result = result
        self.raises = raises
        self.revert = revert
        self.reconciled = []
        self.reverted = []

    def reconcile_tag_to_performer(self, tag_id, performer_id):
        self.reconciled.append((tag_id, performer_id))
        if self.raises is not None:
            raise self.raises
        return self.result

    def revert_reconcile(self, tag_id, prior):
        self.reverted.append((tag_id, prior))
        return self.revert or {"detached": [], "retagged": []}

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the reconciliation path called %r on the media server" % name)
        return refuse


def _landed(untagged, attached=None, worklist=None, failures=()):
    return {"prior": {"tag_id": "7", "performer_id": "1",
                      "attached": list(attached if attached is not None
                                       else untagged),
                      "untagged": list(untagged)},
            "skipped": [], "failures": list(failures),
            "worklist": list(worklist if worklist is not None else untagged)}


class Approving(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _record(self, matches=None, performer_=None, scenes=("sc-1", "sc-2")):
        index = index_performers([performer(1, ASHGROVE)]
                                 if matches is None else matches)
        tag_row = tag(7, ASHGROVE)
        return self.store.record(producer="tag-merge", **proposal(
            tag_row, match_tag(tag_row, index), list(scenes), folder=FOLDER))

    def test_an_approve_writes_and_records_the_snapshot(self):
        fp = self._record()
        stash = _RecordingStash(result=_landed(["sc-1", "sc-2"]))
        actions = Actions(self.store, stash)

        self.assertEqual(actions.approve(fp), "reconciled")

        self.assertEqual(stash.reconciled, [("7", "1")])
        item = self.store.items(state="applied")[0]
        self.assertEqual(item["prior_state"],
                         {"tag_id": "7", "performer_id": "1",
                          "attached": ["sc-1", "sc-2"],
                          "untagged": ["sc-1", "sc-2"]})

    def test_an_ambiguous_proposal_is_refused_and_nothing_is_recorded(self):
        # HARM: reaching here means a request that did not come from the page
        # (the row offers no Approve). Picking a performer would attach every
        # scene to somebody nothing chose.
        fp = self._record(matches=[performer(1, ASHGROVE),
                                   performer(2, QUILL, aliases=[ASHGROVE])])
        stash = _RecordingStash(result=_landed(["sc-1"]))
        actions = Actions(self.store, stash)

        with self.assertRaises(ValueError) as caught:
            actions.approve(fp)

        self.assertIn(AMBIGUOUS, str(caught.exception))
        self.assertEqual(stash.reconciled, [])
        self.assertEqual(self.store.items()[0]["state"], "new")

    def test_no_media_server_is_recorded_as_a_failure_and_nothing_is_written(self):
        fp = self._record()
        actions = Actions(self.store, None)

        with self.assertRaises(ApplyFailed):
            actions.approve(fp)

        item = self.store.items()[0]
        self.assertEqual(item["state"], "failed")
        self.assertIsNone(item["prior_state"])

    def test_a_write_that_raises_is_recorded_as_failed_with_no_snapshot(self):
        fp = self._record()
        stash = _RecordingStash(raises=StashError("the server said no"))
        actions = Actions(self.store, stash)

        with self.assertRaises(ApplyFailed):
            actions.approve(fp)

        item = self.store.items()[0]
        self.assertEqual(item["state"], "failed")
        self.assertIsNone(item["prior_state"])
        self.assertIn("the server said no", item["error"])

    def test_a_partial_run_is_failed_and_still_keeps_its_undo(self):
        # HARM: this is a write to many scenes, a batch at a time. A run that
        # stopped partway really did change the batches it got through, and the
        # snapshot is the only record of which -- dropping it because the call
        # failed leaves those scenes changed with nothing able to put them
        # back.
        fp = self._record(scenes=("sc-1", "sc-2", "sc-3"))
        stash = _RecordingStash(result=_landed(
            ["sc-1"], worklist=["sc-1", "sc-2", "sc-3"],
            failures=[{"scenes": ["sc-2", "sc-3"],
                       "error": "StashError: no"}]))
        actions = Actions(self.store, stash)

        with self.assertRaises(ApplyFailed):
            actions.approve(fp)

        item = self.store.items()[0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["prior_state"]["untagged"], ["sc-1"])
        self.assertIn("wrote 1 of 3 scenes", item["error"])
        # The batch it stopped on, by size and by where it starts -- and said
        # to be unrecorded, because it is in neither half of the snapshot.
        self.assertIn("batch of 2", item["error"])
        self.assertIn("sc-2", item["error"])
        self.assertIn("partly written", item["error"])

    def test_a_run_that_wrote_nothing_at_all_keeps_no_snapshot(self):
        fp = self._record()
        stash = _RecordingStash(result=_landed(
            [], worklist=["sc-1"],
            failures=[{"scenes": ["sc-1"], "error": "StashError: no"}]))
        actions = Actions(self.store, stash)

        with self.assertRaises(ApplyFailed):
            actions.approve(fp)

        item = self.store.items()[0]
        self.assertEqual(item["state"], "failed")
        self.assertIsNone(item["prior_state"])

    def test_a_second_approve_over_an_existing_snapshot_is_refused(self):
        # HARM: it would record a snapshot covering only the second batch, and
        # the first batch's scenes would become unrecorded -- changed, with
        # nothing able to name them.
        fp = self._record(scenes=("sc-1", "sc-2"))
        stash = _RecordingStash(result=_landed(
            ["sc-1"], worklist=["sc-1", "sc-2"],
            failures=[{"scenes": ["sc-2"], "error": "StashError: no"}]))
        actions = Actions(self.store, stash)
        with self.assertRaises(ApplyFailed):
            actions.approve(fp)

        with self.assertRaises(ValueError) as caught:
            actions.approve(fp)

        self.assertIn("Undo it first", str(caught.exception))
        self.assertEqual(len(stash.reconciled), 1)


class Undoing(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _applied(self, prior):
        index = index_performers([performer(1, ASHGROVE)])
        tag_row = tag(7, ASHGROVE)
        fp = self.store.record(producer="tag-merge", **proposal(
            tag_row, match_tag(tag_row, index), ["sc-1"], folder=FOLDER))
        self.store.mark_applied(fp, prior_state=prior)
        return fp

    def test_an_undo_hands_the_client_the_tag_and_the_whole_snapshot(self):
        prior = {"tag_id": "7", "performer_id": "1",
                 "attached": ["sc-1"], "untagged": ["sc-1"]}
        fp = self._applied(prior)
        stash = _RecordingStash()
        actions = Actions(self.store, stash)

        self.assertEqual(actions.undo(fp), "reverted")

        self.assertEqual(stash.reverted, [("7", prior)])
        self.assertEqual(self.store.items()[0]["state"], "reverted")

    def test_an_applied_row_with_no_snapshot_refuses_before_writing(self):
        fp = self._applied(None)
        stash = _RecordingStash()

        with self.assertRaises(ValueError):
            Actions(self.store, stash).undo(fp)

        self.assertEqual(stash.reverted, [])

    def test_a_revert_that_raises_leaves_the_row_applied_with_its_snapshot(self):
        # So the button can be pressed again -- `revert_reconcile` is
        # idempotent for exactly this.
        prior = {"tag_id": "7", "performer_id": "1",
                 "attached": ["sc-1"], "untagged": ["sc-1"]}
        fp = self._applied(prior)

        class Boom(_RecordingStash):
            def revert_reconcile(self, tag_id, prior):
                raise StashError("the server said no")

        with self.assertRaises(StashError):
            Actions(self.store, Boom()).undo(fp)

        item = self.store.items()[0]
        self.assertEqual(item["state"], "applied")
        self.assertEqual(item["prior_state"], prior)


# -- what the section shows ------------------------------------------------ #

# A sentinel for "this test did not say", so a test CAN say `None` -- which is
# what an ambiguous payload's `performer` actually is. `performer_=None` meaning
# "give me the default" is how a two-performer fixture silently became a
# one-performer one and passed.
_UNSET = object()


def _item(state="new", prior_state=None, error=None, payload=None,
          performer_=_UNSET, matches=None, ambiguous=None,
          scenes=("sc-1", "sc-2", "sc-3")):
    return {
        "fingerprint": "fp-1",
        "state": state,
        "subject_type": SUBJECT_TYPE,
        "subject_id": "7",
        "payload": payload if payload is not None else {
            "tag": {"name": ASHGROVE},
            "performer": ({"id": "1", "name": QUILL}
                          if performer_ is _UNSET else performer_),
            "alias": ASHGROVE,
            "matches": (matches if matches is not None
                        else [{"performer": {"id": "1", "name": QUILL},
                               "alias": ASHGROVE}]),
            "ambiguous": ambiguous,
            "scenes": list(scenes),
            "counts_cover": COUNTS_COVER,
        },
        "prior_state": prior_state,
        "error": error,
    }


class TheRow(unittest.TestCase):
    def test_the_whole_row_for_an_alias_match(self):
        got = to_reconcile_row(_item(), base_url="http://server.test")

        self.assertEqual(got, ReconcileRow(
            fingerprint="fp-1", state="new", subject_type=SUBJECT_TYPE,
            subject_id="7", tag_name=ASHGROVE,
            tag_url="http://server.test/tags/7",
            performer=QUILL, performer_url="http://server.test/performers/1",
            alias=ASHGROVE,
            matches=({"id": "1", "name": QUILL, "alias": ASHGROVE,
                      "url": "http://server.test/performers/1"},),
            ambiguous=None, total_scenes=3, counts_cover=COUNTS_COVER,
            appliable=True, actionable=True, undoable=False,
            undismissable=False, unmutable=False, error=None))

    def test_the_blast_radius_is_the_size_of_the_recorded_scene_set(self):
        # HARM: the number a reviewer reads and the scenes it describes must be
        # one fact. A count stored beside the list could disagree with it, and
        # a small number beside a large write is the reading that gets an
        # unsafe approve.
        got = to_reconcile_row(_item(scenes=["a", "b", "c", "d", "e"]))

        self.assertEqual(got.total_scenes, 5)

    def test_a_two_performer_finding_offers_no_approve(self):
        got = to_reconcile_row(_item(
            performer_=None, ambiguous=AMBIGUOUS,
            matches=[{"performer": {"id": "1", "name": ASHGROVE},
                      "alias": None},
                     {"performer": {"id": "2", "name": QUILL},
                      "alias": ASHGROVE}]))

        self.assertFalse(got.appliable)
        self.assertTrue(got.actionable)
        self.assertIsNone(got.performer)
        self.assertEqual([m["name"] for m in got.matches], [ASHGROVE, QUILL])
        self.assertEqual([m["alias"] for m in got.matches], [None, ASHGROVE])

    def test_an_applied_row_offers_the_undo_and_not_the_approve(self):
        got = to_reconcile_row(_item(state="applied", prior_state={"a": 1}))

        self.assertTrue(got.undoable)
        self.assertFalse(got.appliable)
        self.assertFalse(got.actionable)

    def test_a_failed_row_that_partly_landed_offers_the_undo(self):
        # HARM: without this the scenes a partial run changed have no way back
        # from the page at all.
        got = to_reconcile_row(_item(state="failed", prior_state={"a": 1},
                                     error="wrote 1 of 3 scenes"))

        self.assertTrue(got.undoable)
        self.assertFalse(got.appliable)
        self.assertTrue(got.actionable)

    def test_a_failed_row_that_wrote_nothing_offers_the_approve_again(self):
        got = to_reconcile_row(_item(state="failed", error="the server said no"))

        self.assertFalse(got.undoable)
        self.assertTrue(got.appliable)

    def test_a_dismissed_row_offers_its_own_reversal_and_no_write(self):
        got = to_reconcile_row(_item(state="dismissed"))

        self.assertTrue(got.undismissable)
        self.assertFalse(got.unmutable)
        self.assertFalse(got.appliable)
        self.assertFalse(got.actionable)

    def test_a_muted_row_offers_its_own_reversal(self):
        got = to_reconcile_row(_item(state="muted"))

        self.assertTrue(got.unmutable)
        self.assertFalse(got.undismissable)

    def test_no_configured_server_leaves_every_link_absent(self):
        got = to_reconcile_row(_item())

        self.assertIsNone(got.tag_url)
        self.assertIsNone(got.performer_url)
        self.assertEqual([m["url"] for m in got.matches], [None])

    def test_a_payload_missing_its_scenes_is_malformed_and_raises(self):
        # HARM: read with a default it would report a blast radius of nothing
        # for a write that might touch thousands of scenes.
        payload = dict(_item()["payload"])
        del payload["scenes"]

        with self.assertRaises(KeyError):
            to_reconcile_row(_item(payload=payload))


class TheSection(unittest.TestCase):
    """What the page actually draws, rendered through the real template."""

    def _render(self, rows):
        from cronicled.web.render import render
        return render("inbox.html", rows=[], counts={}, reconciles=rows)

    @staticmethod
    def _as_rendered(text):
        """`text` as the page will really carry it.

        Escaped with the SAME library the renderer escapes with, rather than
        with a copy of its rules written here: an assertion carrying its own
        idea of how an apostrophe is encoded passes or fails on that idea and
        not on the page.
        """
        from markupsafe import escape
        return str(escape(text))

    def test_the_blast_radius_and_the_match_are_both_on_the_page(self):
        html = self._render([to_reconcile_row(_item())])

        self.assertIn(">3<", html)
        self.assertIn(QUILL, html)
        self.assertIn(ASHGROVE, html)
        self.assertIn("matched through their alias", html)

    def test_the_page_says_a_shared_name_is_not_proof(self):
        html = self._render([to_reconcile_row(_item())])

        self.assertIn("evidence, not proof", html)
        self.assertIn("deleting it is a separate decision", html)

    def test_a_two_performer_finding_draws_no_approve_control(self):
        # HARM: the button is the last thing between a name collision and a
        # write to every scene carrying the tag.
        html = self._render([to_reconcile_row(_item(
            performer_=None, ambiguous=AMBIGUOUS,
            matches=[{"performer": {"id": "1", "name": ASHGROVE},
                      "alias": None},
                     {"performer": {"id": "2", "name": QUILL},
                      "alias": ASHGROVE}]))])

        self.assertNotIn('action="/approve"', html)
        self.assertIn(self._as_rendered(AMBIGUOUS), html)
        self.assertIn(ASHGROVE, html)
        self.assertIn(QUILL, html)

    def test_an_applied_row_draws_an_undo_and_no_approve(self):
        html = self._render([to_reconcile_row(
            _item(state="applied", prior_state={"a": 1}))])

        self.assertIn('action="/undo"', html)
        self.assertNotIn('action="/approve"', html)

    def test_an_empty_section_says_so_rather_than_drawing_nothing(self):
        html = self._render([])

        self.assertIn("Tags that are a performer (0)", html)
        self.assertIn("No tag matches a performer&#39;s name.", html)


if __name__ == "__main__":
    unittest.main()
