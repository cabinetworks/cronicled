"""Tags that do almost no work: the two populations, and the deletion offered.

Every tag, performer and description here is invented. The library this was
measured against held real people's names and real store names, and none of
them appear in this file or anywhere else in this repository.

No test here opens a socket: the media server is reached through an injected
transport, exactly as every other client test in this suite reaches it.
"""
import re
import unittest

from cronicled import performer_tags, tag_hygiene
from cronicled.jobs import JobRunner
from cronicled.performer_tags import proposal as reconcile_proposal
from cronicled.stash import Stash, StashError
from cronicled.store import Store
from cronicled.tag_descriptions import SUBJECT_TYPE as TAG_DESCRIPTION_SUBJECT
from cronicled.tag_hygiene import (COUNTS_COVER, DELETE_IS_IRREVERSIBLE,
                                   DELETE_WARNING, GROUP_LABEL, GROUP_NOTE,
                                   GROUPS, LOW_COUNT_IS_NOT_PROOF, NO_SCENES,
                                   ONE_SCENE, SUBJECT_TYPE, Counts,
                                   evidence_against, group_of, narrowings,
                                   proposal)
from cronicled.tags import SUBJECT_TYPE as CLUSTER_SUBJECT
from cronicled.tags import TagMergeProducer
from cronicled.web.actions import Actions, ApplyFailed
from cronicled.web.app import build_handler
from cronicled.web.render import render
from cronicled.web.rows import (UnusedTagGroup, UnusedTagRow, to_unused_groups,
                                to_unused_row)
from tests.test_tags import (FakeBoxClient, FakeCtx, FakeStash, box_credential,
                             box_tag, catalogue, tag)

FOLDER = "library"
WAIT = 10

# Invented tag names. Chosen so no two normalise to the same form by accident,
# so nothing here clusters unless a test says so.
LANTERN = "Lantern Drift"
KETTLE = "Copper Kettle"
FERRY = "Harbour Ferry"
QUARRY = "Slate Quarry"
ORCHARD = "Walled Orchard"
CISTERN = "Stone Cistern"

# Two visibly different invented descriptions, so no assertion below can pass
# by comparing a value against itself.
LANTERN_TEXT = "Scenes lit only by a hand-carried lamp."
KETTLE_TEXT = "Filmed entirely in a working kitchen."


# -- which population a count belongs to ----------------------------------- #

class Classifying(unittest.TestCase):
    def test_a_tag_on_no_scenes_is_the_no_scenes_population(self):
        self.assertEqual(group_of(tag(1, LANTERN, scene_count=0)), NO_SCENES)

    def test_a_tag_on_exactly_one_scene_is_the_one_scene_population(self):
        self.assertEqual(group_of(tag(1, LANTERN, scene_count=1)), ONE_SCENE)

    def test_a_tag_on_two_scenes_is_in_neither(self):
        # The permissive side of the guard, pinned on its own. A rule that
        # drifted to "two or fewer" would propose deleting a tag doing real
        # work, and nothing above would notice: every other assertion here is
        # about a tag the rule is meant to catch.
        self.assertIsNone(group_of(tag(1, LANTERN, scene_count=2)))

    def test_a_tag_on_many_scenes_is_in_neither(self):
        self.assertIsNone(group_of(tag(1, LANTERN, scene_count=413)))

    def test_a_row_with_no_scene_count_at_all_raises(self):
        # HARM: a default of 0 is precisely the value that puts a tag in the
        # population whose whole argument is "deleting this changes no scene".
        # A malformed row read that way would be proposed for deletion while
        # the code read as though it had checked.
        with self.assertRaises(KeyError):
            group_of({"id": "1", "name": LANTERN})

    def test_a_stored_payload_is_classified_by_the_same_rule(self):
        # One rule, two readers -- the producer classifies a media-server row,
        # the page classifies a recorded payload. A second copy of the rule in
        # the row builder would be free to disagree with the one the producer
        # decided by.
        built = proposal(tag(1, LANTERN, scene_count=1), folder=FOLDER)

        self.assertEqual(group_of(built["payload"]), ONE_SCENE)

    def test_the_two_populations_are_listed_safest_first(self):
        self.assertEqual(GROUPS, (NO_SCENES, ONE_SCENE))


# -- what already describing a tag says about it --------------------------- #

class EvidenceThatATagIsReal(unittest.TestCase):
    def test_a_tag_the_library_itself_describes_is_flagged_as_described(self):
        self.assertEqual(
            evidence_against(tag(1, LANTERN, description=LANTERN_TEXT), []),
            (True, False))

    def test_a_tag_a_source_describes_is_flagged_as_sourced(self):
        index = _index("first", [box_tag(LANTERN, LANTERN_TEXT)])

        self.assertEqual(evidence_against(tag(1, LANTERN), [index]),
                         (False, True))

    def test_a_tag_nothing_describes_is_flagged_neither_way(self):
        index = _index("first", [box_tag(KETTLE, KETTLE_TEXT)])

        self.assertEqual(evidence_against(tag(1, LANTERN), [index]),
                         (False, False))

    def test_the_librarys_own_description_is_reported_ahead_of_a_sources(self):
        # Two separate reasons a tag is being kept, reported apart: "somebody
        # here defined this" and "a public catalogue defines this" call for
        # different responses, and one figure would hide which a night was.
        index = _index("first", [box_tag(LANTERN, LANTERN_TEXT)])

        self.assertEqual(
            evidence_against(tag(1, LANTERN, description=KETTLE_TEXT),
                             [index]),
            (True, False))


def _index(name, box_tags):
    from cronicled.tag_descriptions import index_box
    return index_box(name, box_tags)


# -- the proposal ---------------------------------------------------------- #

class TheProposal(unittest.TestCase):
    def test_the_whole_proposal_for_a_tag_on_no_scenes(self):
        # The WHOLE dict. A field-by-field check cannot see a field that was
        # ADDED -- and an extra `group` key here would be a second copy of a
        # fact the count already carries, free to disagree with it.
        self.assertEqual(
            proposal(tag(7, LANTERN, scene_count=0), folder=FOLDER),
            {
                "folder": FOLDER,
                "subject_type": SUBJECT_TYPE,
                "subject_id": "7",
                "summary": "%s: on 0 scenes -- nothing in the library "
                           "references it" % LANTERN,
                "confidence": None,
                "payload": {"name": LANTERN, "scene_count": 0,
                            "counts_cover": COUNTS_COVER},
            })

    def test_the_whole_proposal_for_a_tag_on_one_scene(self):
        self.assertEqual(
            proposal(tag(9, KETTLE, scene_count=1), folder=FOLDER),
            {
                "folder": FOLDER,
                "subject_type": SUBJECT_TYPE,
                "subject_id": "9",
                "summary": "%s: on 1 scene -- it classifies that one and "
                           "nothing else" % KETTLE,
                "confidence": None,
                "payload": {"name": KETTLE, "scene_count": 1,
                            "counts_cover": COUNTS_COVER},
            })

    def test_the_two_summaries_are_two_different_sentences(self):
        # A catch-all naming the tag and its count would satisfy every
        # assertion about "the summary names the tag" while telling a reviewer
        # nothing about which finding they are looking at -- and one of the two
        # changes a scene while the other does not.
        nothing = proposal(tag(1, LANTERN, scene_count=0),
                           folder=FOLDER)["summary"]
        one = proposal(tag(2, LANTERN, scene_count=1),
                       folder=FOLDER)["summary"]

        self.assertNotEqual(nothing.replace(LANTERN, ""),
                            one.replace(LANTERN, ""))
        self.assertIn("nothing in the library references it", nothing)
        self.assertIn("classifies that one and nothing else", one)

    def test_the_count_in_the_proposal_is_the_rows_own(self):
        # Two tags in one read, each with its own count: a proposal that
        # carried a constant, or the other tag's number, fails here.
        built = [proposal(t, folder=FOLDER)
                 for t in (tag(1, LANTERN, scene_count=1),
                           tag(2, KETTLE, scene_count=0))]

        self.assertEqual([(p["subject_id"], p["payload"]["scene_count"])
                          for p in built], [("1", 1), ("2", 0)])

    def test_the_summary_prints_the_rows_own_count(self):
        self.assertIn("on 1 scene", proposal(tag(1, LANTERN, scene_count=1),
                                             folder=FOLDER)["summary"])
        self.assertIn("on 0 scenes", proposal(tag(2, KETTLE, scene_count=0),
                                              folder=FOLDER)["summary"])

    def test_an_integer_id_is_recorded_as_the_text_the_store_holds(self):
        # `subject_id` is a TEXT column and `muted_subjects()` answers strings,
        # so an integer here would propose a tag whose deletion a person had
        # already refused.
        built = proposal({"id": 7, "name": LANTERN, "aliases": [],
                          "description": None, "scene_count": 0},
                         folder=FOLDER)

        self.assertEqual(built["subject_id"], "7")

    def test_a_tag_on_two_scenes_cannot_be_proposed_for_deletion(self):
        with self.assertRaises(ValueError) as caught:
            proposal(tag(1, LANTERN, scene_count=2), folder=FOLDER)

        self.assertIn(LANTERN, str(caught.exception))

    def test_both_populations_can_be_proposed(self):
        # The permissive side of the same guard: a rule that drifted to "only
        # zero" would silently drop 793 of the 1049 tags this exists for, and
        # every assertion about a refusal above would still pass.
        for count in (0, 1):
            with self.subTest(count=count):
                proposal(tag(1, LANTERN, scene_count=count), folder=FOLDER)


# -- the narrowings -------------------------------------------------------- #

class TheNarrowings(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_only_this_modules_own_subject_is_read_from_either_table(self):
        # A person who muted a description proposal for a tag has said nothing
        # about whether the tag is worth keeping, and vice versa. One subject
        # type for both would make either mute silence the other.
        self.store.mute(TAG_DESCRIPTION_SUBJECT, "5")
        self.store.mute(SUBJECT_TYPE, "6")
        self.store.record(folder=FOLDER, subject_type=TAG_DESCRIPTION_SUBJECT,
                          subject_id="7", summary="s", payload={},
                          producer="p")
        self.store.record(folder=FOLDER, subject_type=SUBJECT_TYPE,
                          subject_id="8", summary="s", payload={},
                          producer="p")

        self.assertEqual(narrowings(self.store, FOLDER), ({"6"}, {"8"}))


# -- the pass -------------------------------------------------------------- #

class ThePass(unittest.TestCase):
    """The fourth half of the one tag read."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def build(self, tags, boxes=(), catalogues=None, performers=(),
              scenes=None, **kwargs):
        self.stash = FakeStash(tags, boxes, performers=performers,
                              scenes=scenes)
        self.boxes = FakeBoxClient(catalogues or {})
        kwargs.setdefault("folder", FOLDER)
        return TagMergeProducer(self.stash, store=self.store,
                                box_client=self.boxes, **kwargs)

    def run_pass(self, tags, **kwargs):
        return list(self.build(tags, **kwargs).produce(self.ctx))

    def deletions(self, tags, **kwargs):
        return [p for p in self.run_pass(tags, **kwargs)
                if p["subject_type"] == SUBJECT_TYPE]

    def test_a_tag_on_no_scenes_is_proposed_for_deletion(self):
        got = self.deletions([tag(1, LANTERN, scene_count=0)])

        self.assertEqual([p["subject_id"] for p in got], ["1"])

    def test_a_tag_on_one_scene_is_proposed_for_deletion(self):
        got = self.deletions([tag(1, LANTERN, scene_count=1)])

        self.assertEqual([p["subject_id"] for p in got], ["1"])

    def test_a_tag_on_many_scenes_is_not_proposed(self):
        # The other direction, and the expensive one: this pass's only output
        # is a deletion, so a rule that fired on a working tag would offer to
        # remove it from every scene carrying it.
        self.assertEqual(self.deletions([tag(1, LANTERN, scene_count=2),
                                         tag(2, KETTLE, scene_count=97)]), [])

    def test_a_tag_the_library_already_describes_is_not_proposed(self):
        # Somebody wrote a definition of this category. That is evidence the
        # tag is real, independent of how much of the library uses it yet.
        got = self.deletions([tag(1, LANTERN, scene_count=0,
                                  description=LANTERN_TEXT)])

        self.assertEqual(got, [])

    def test_a_tag_a_source_describes_is_not_proposed_and_gets_its_text(self):
        # A public catalogue defining this name says the tag is a real
        # vocabulary term this library has barely used -- a reason to keep it.
        # The useful row for it is the description, and it is the row it gets.
        proposals = self.run_pass(
            [tag(1, LANTERN, scene_count=0)],
            boxes=[box_credential("first")],
            catalogues={"https://first.invalid":
                        catalogue([box_tag(LANTERN, LANTERN_TEXT)])})

        self.assertEqual([p["subject_type"] for p in proposals],
                         [TAG_DESCRIPTION_SUBJECT])

    def test_a_tag_with_a_duplicate_spelling_is_left_to_its_merge(self):
        # The merge and the deletion are opposite answers about one name. A
        # person who approved both would delete a spelling the merge was about
        # to move items onto.
        proposals = self.run_pass([tag(1, "Velvet Crane", scene_count=0),
                                   tag(2, "VelvetCrane", scene_count=1)])

        self.assertEqual([p["subject_type"] for p in proposals],
                         [CLUSTER_SUBJECT])

    def test_a_tag_the_performer_half_proposes_about_is_not_proposed(self):
        # Attaching the performer and removing the tag preserves what the tag
        # was recording; deleting it does not, and it cannot be taken back.
        # One tag, one row, and the reversible answer wins.
        proposals = self.run_pass(
            [tag(1, "Delia Ashgrove", scene_count=1)],
            performers=[{"id": "p-1", "name": "Delia Ashgrove",
                         "alias_list": []}],
            scenes={"1": [{"id": "sc-1"}]})

        self.assertEqual([p["subject_type"] for p in proposals],
                         [performer_tags.SUBJECT_TYPE])

    def test_a_tag_whose_reconciliation_is_already_on_the_page_is_not_proposed(self):
        # `_reconcile` skips it as already-proposed, so it is absent from this
        # run's own list while still being a live row about that tag.
        built = reconcile_proposal(
            tag(1, "Delia Ashgrove", scene_count=1),
            (performer_tags.Match(performer={"id": "p-1",
                                            "name": "Delia Ashgrove"},
                                  alias=None),),
            ["sc-1"], folder=FOLDER)
        self.store.record(folder=FOLDER,
                          subject_type=built["subject_type"],
                          subject_id=built["subject_id"], summary="s",
                          payload=built["payload"], producer="p")

        self.assertEqual(
            self.deletions([tag(1, "Delia Ashgrove", scene_count=1)],
                           performers=[{"id": "p-1",
                                        "name": "Delia Ashgrove",
                                        "alias_list": []}],
                           scenes={"1": [{"id": "sc-1"}]}),
            [])

    def test_a_muted_reconciliation_does_not_silence_this_finding(self):
        # "Stop telling me this tag is a performer" is a decision about that
        # question. Whether the tag earns its place is a different question,
        # and the whole reason each of these passes has its own subject type.
        self.store.mute(performer_tags.SUBJECT_TYPE, "1")

        got = self.deletions(
            [tag(1, "Delia Ashgrove", scene_count=1)],
            performers=[{"id": "p-1", "name": "Delia Ashgrove",
                         "alias_list": []}],
            scenes={"1": [{"id": "sc-1"}]})

        self.assertEqual([p["subject_id"] for p in got], ["1"])

    def test_a_tag_that_already_has_a_proposal_is_not_proposed_again(self):
        self.store.record(folder=FOLDER, subject_type=SUBJECT_TYPE,
                          subject_id="1", summary="s",
                          payload={"name": LANTERN, "scene_count": 0,
                                   "counts_cover": COUNTS_COVER},
                          producer="p")

        self.assertEqual(self.deletions([tag(1, LANTERN, scene_count=0)]), [])

    def test_a_proposal_in_another_folder_does_not_suppress_this_one(self):
        self.store.record(folder="elsewhere", subject_type=SUBJECT_TYPE,
                          subject_id="1", summary="s",
                          payload={"name": LANTERN, "scene_count": 0,
                                   "counts_cover": COUNTS_COVER},
                          producer="p")

        got = self.deletions([tag(1, LANTERN, scene_count=0)])

        self.assertEqual([p["subject_id"] for p in got], ["1"])

    def test_a_tag_reviewed_and_kept_is_not_proposed_on_the_next_run(self):
        """The acceptance this ticket turns on for repeat runs.

        Two whole runs through the real runner and the real store, with the
        Keep in between -- not a call to `narrowings` -- because what must not
        happen is the row coming BACK, and only a second run can show that.
        """
        runner = JobRunner(self.store)
        tags = [tag(1, LANTERN, scene_count=0),
                tag(2, KETTLE, scene_count=1),
                tag(3, FERRY, scene_count=1)]
        producer = self.build(tags)
        runner.register(producer)
        job = runner.start(producer.name, trigger="manual")
        self.assertTrue(runner.wait(job.id, WAIT))
        first = sorted(i["subject_id"] for i in self.store.items(folder=FOLDER)
                       if i["subject_type"] == SUBJECT_TYPE)
        self.assertEqual(first, ["1", "2", "3"])

        # A person looks at the middle one and keeps it. Both the proposal and
        # the standing decision go away with it.
        self.store.mute(SUBJECT_TYPE, "2", reason="keeping this one")

        second = self.build(tags)
        runner.reregister(second)
        job = runner.start(second.name, trigger="manual")
        self.assertTrue(runner.wait(job.id, WAIT))

        self.assertEqual(
            sorted(i["subject_id"] for i in self.store.items(folder=FOLDER)
                   if i["subject_type"] == SUBJECT_TYPE),
            ["1", "3"])

    def test_the_pass_yields_the_deletions_it_proposes(self):
        # Not merely computed: the runner records what the generator yields, so
        # a half that built its list and never yielded would count them in the
        # log line and put nothing on the page.
        got = self.deletions([tag(1, LANTERN, scene_count=0),
                              tag(2, KETTLE, scene_count=1)])

        self.assertEqual([p["subject_id"] for p in got], ["1", "2"])

    def test_the_half_asks_the_media_server_and_the_sources_nothing_extra(self):
        # It must add no third-party call: the pass is in the `box` cost class
        # because the description half pages a public service, and a fourth
        # half issuing its own lookups would spend that budget uncounted.
        self.run_pass([tag(1, LANTERN, scene_count=0),
                       tag(2, KETTLE, scene_count=1)],
                      boxes=[box_credential("first")],
                      catalogues={"https://first.invalid":
                                  catalogue([box_tag(QUARRY, KETTLE_TEXT)])})

        self.assertEqual(self.stash.calls,
                         ["all_tags", "stash_box_credentials",
                          "performers_with_aliases"])
        self.assertEqual(self.boxes.asked, [("https://first.invalid",
                                             "key-first")])


class WhenASourceCouldNotBeRead(unittest.TestCase):
    """Half the evidence for a deletion is that no source describes the tag.

    A source that failed, or was read only partly, has not established that for
    a single tag -- and a proposal to delete something cannot be worded
    carefully enough to survive the gap, because what it asks for cannot be
    taken back.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def run_pass(self, tags, catalogues):
        self.stash = FakeStash(tags, [box_credential("first")])
        producer = TagMergeProducer(
            self.stash, store=self.store, folder=FOLDER,
            box_client=FakeBoxClient(catalogues))
        return list(producer.produce(self.ctx))

    def test_a_source_that_failed_outright_withholds_every_deletion(self):
        proposals = self.run_pass(
            [tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=1)],
            {"https://first.invalid": RuntimeError("the source said no")})

        self.assertEqual([p for p in proposals
                          if p["subject_type"] == SUBJECT_TYPE], [])

    def test_a_source_read_only_partly_withholds_every_deletion(self):
        # The page that was not read is exactly where the missing description
        # would be.
        proposals = self.run_pass(
            [tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=1)],
            {"https://first.invalid": catalogue([box_tag(QUARRY, KETTLE_TEXT)],
                                                complete=False)})

        self.assertEqual([p for p in proposals
                          if p["subject_type"] == SUBJECT_TYPE], [])

    def test_a_source_that_answered_in_full_does_not_withhold(self):
        # The permissive side. Without it, "withhold on an unread source" and
        # "never propose anything" are the same code.
        proposals = self.run_pass(
            [tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=1)],
            {"https://first.invalid": catalogue([box_tag(QUARRY,
                                                         KETTLE_TEXT)])})

        self.assertEqual([p["subject_id"] for p in proposals
                          if p["subject_type"] == SUBJECT_TYPE], ["1", "2"])

    def test_the_withheld_night_says_so_and_does_not_read_as_nothing_to_do(self):
        # "0 proposed" reads identically for a library with nothing to clean up
        # and for a night when the evidence was never gathered, and those call
        # for opposite responses -- one of them being to fix the source.
        self.run_pass(
            [tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=1),
             tag(3, FERRY, scene_count=1)],
            {"https://first.invalid": RuntimeError("the source said no")})

        self.assertIn("3 tags on nought or one scene were not examined",
                      self.ctx.message)
        self.assertNotIn("0 proposed for deletion", self.ctx.message)


class TheCounts(unittest.TestCase):
    """Why a pass's low-count tags became the deletions it proposed.

    Every fixture here is ASYMMETRIC and every group holds more than one, so a
    counter that assigned (`n = 1`) instead of accumulating (`n += 1`) cannot
    agree with one that accumulated.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def test_the_two_populations_are_counted_apart_and_accumulated(self):
        # Two on no scenes, three on one: distinct and both above one, so
        # `n = 1` disagrees with `n += 1` in both fields, and a fixture that
        # swapped the two fields fails as well.
        counts = _hygiene_counts(self.store, [
            tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=0),
            tag(3, FERRY, scene_count=1), tag(4, QUARRY, scene_count=1),
            tag(5, ORCHARD, scene_count=1)])

        self.assertEqual((counts.no_scenes, counts.one_scene), (2, 3))
        self.assertEqual(counts.outstanding, 5)

    def test_outstanding_is_the_sum_of_the_two_populations(self):
        counts = _hygiene_counts(self.store, [
            tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=0),
            tag(3, FERRY, scene_count=0), tag(4, QUARRY, scene_count=1)])

        self.assertEqual(counts.outstanding,
                         counts.no_scenes + counts.one_scene)

    def test_the_identity_holds_over_every_outcome_at_once(self):
        """A tag that vanished for a reason nobody named is a failure no
        per-field check can see, which is why this asserts the identity.

        Every reason is exercised, and **every bucket holds more than one tag
        and a different number of them**. That is not tidiness: a bucket of one
        cannot tell `n += 1` from `n = 1`, and a bucket of the same size as its
        neighbour cannot tell one from the other when a term lands in the wrong
        field. Both mutations survived an earlier version of this fixture that
        had a single reconciled tag in it.
        """
        described = _spread("Described", 2, description=LANTERN_TEXT)
        sourced = _spread("Sourced", 3)
        clustered = [tag("c-0", "Velvet Crane", scene_count=0),
                     tag("c-1", "VelvetCrane", scene_count=1),
                     tag("c-2", "Copper Kettle", scene_count=1),
                     tag("c-3", "CopperKettle", scene_count=0)]
        # Every one on exactly ONE scene: the performer half skips a matched tag
        # on no scenes as having nothing to move, so a nought-scene one would
        # never be covered and would land in `outstanding` instead.
        reconciled = [tag("r-%d" % i, "Reconciled %d" % i, scene_count=1)
                      for i in range(5)]
        kept = _spread("Kept", 6)
        already = _spread("Already", 7)
        outstanding = _spread("Outstanding", 9)

        for row in kept:
            self.store.mute(SUBJECT_TYPE, row["id"])
        for row in already:
            self.store.record(folder=FOLDER, subject_type=SUBJECT_TYPE,
                              subject_id=row["id"], summary="s",
                              payload={"name": row["name"], "scene_count": 0,
                                       "counts_cover": COUNTS_COVER},
                              producer="p")

        counts = _hygiene_counts(
            self.store,
            described + sourced + clustered + reconciled + kept + already
            + outstanding + [tag("w-1", "Working Tag", scene_count=6)],
            boxes=[box_credential("first")],
            catalogues={"https://first.invalid": catalogue(
                [box_tag(row["name"], LANTERN_TEXT) for row in sourced])},
            performers=[{"id": "p-%d" % i, "name": "Reconciled %d" % i,
                         "alias_list": []} for i in range(5)],
            scenes={row["id"]: [{"id": "sc-1"}] for row in reconciled})

        self.assertEqual(
            counts.low,
            counts.withheld + counts.described + counts.sourced
            + counts.clustered + counts.reconciled + counts.kept
            + counts.already_proposed + counts.outstanding)
        # And the whole shape, so a term that moved into the wrong field is
        # not hidden by an identity that still adds up.
        self.assertEqual(counts, Counts(
            low=36, withheld=0, described=2, sourced=3, clustered=4,
            reconciled=5, kept=6, already_proposed=7, outstanding=9,
            no_scenes=5, one_scene=4))

    def test_a_withheld_night_puts_every_low_count_tag_in_withheld(self):
        counts = _hygiene_counts(
            self.store,
            [tag(1, LANTERN, scene_count=0), tag(2, KETTLE, scene_count=1),
             tag(3, FERRY, scene_count=1), tag(4, "Working Tag",
                                               scene_count=9)],
            boxes=[box_credential("first")],
            catalogues={"https://first.invalid":
                        RuntimeError("the source said no")})

        self.assertEqual(counts, Counts(
            low=3, withheld=3, described=0, sourced=0, clustered=0,
            reconciled=0, kept=0, already_proposed=0, outstanding=0,
            no_scenes=0, one_scene=0))


class TheClosingLine(unittest.TestCase):
    """A claim about a log is a claim about what a person sees.

    `JobRunner._log` keeps ONE field, so a count written before the pass's last
    message is a number the code computes and nobody can read. `FakeCtx` keeps
    one field for the same reason -- a double that accumulated a list would let
    every assertion here pass against a line no operator ever sees.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def _run(self, tags, **kwargs):
        stash = FakeStash(tags, kwargs.pop("boxes", ()), **kwargs)
        producer = TagMergeProducer(stash, store=self.store, folder=FOLDER,
                                    box_client=FakeBoxClient({}))
        list(producer.produce(self.ctx))

    def test_the_closing_line_carries_the_low_count_half(self):
        self._run([tag(1, LANTERN, scene_count=0),
                   tag(2, KETTLE, scene_count=0),
                   tag(3, FERRY, scene_count=1)])

        self.assertIn("3 tags on nought or one scene", self.ctx.message)
        self.assertIn("3 proposed for deletion", self.ctx.message)
        self.assertIn("2 on no scenes", self.ctx.message)
        self.assertIn("1 on one scene", self.ctx.message)

    def test_the_closing_line_still_carries_the_other_three_halves(self):
        # The line a person reads is one string; a fourth clause appended over
        # the top of the third would take the reconciliation tally with it.
        self._run([tag(1, "Velvet Crane", scene_count=4),
                   tag(2, "VelvetCrane", scene_count=2)])

        self.assertIn("1 clusters", self.ctx.message)
        self.assertIn("descriptions proposed", self.ctx.message)
        self.assertIn("reconciliations proposed", self.ctx.message)
        self.assertIn("tags on nought or one scene", self.ctx.message)

    def test_suppressed_findings_do_not_read_as_nothing_to_do(self):
        # "0 proposed for deletion" reads identically for a library with no
        # low-count tags and for one whose every low-count tag a person has
        # already kept, and those call for opposite responses.
        self.store.mute(SUBJECT_TYPE, "1")
        self.store.mute(SUBJECT_TYPE, "2")

        self._run([tag(1, LANTERN, scene_count=0),
                   tag(2, KETTLE, scene_count=1)])

        self.assertIn("0 proposed for deletion", self.ctx.message)
        self.assertIn("2 kept by hand", self.ctx.message)

    def test_every_suppression_reason_is_named_in_the_line_with_its_own_count(self):
        """The whole tally, not the two figures a summary is easiest to read.

        A clause reported as a constant zero -- which is what a dropped term
        looks like -- makes a night of suppressions read as a night with
        nothing to suppress, and each of these reasons calls for a different
        response: fix a source, answer a merge, or nothing at all.
        """
        # Distinct counts per reason, all above one, so a term reported from
        # the wrong field or as a constant cannot agree with the right one.
        for subject_id in ("30", "31", "32"):
            self.store.record(folder=FOLDER, subject_type=SUBJECT_TYPE,
                              subject_id=subject_id, summary="s",
                              payload={"name": "x", "scene_count": 0,
                                       "counts_cover": COUNTS_COVER},
                              producer="p")
        self.store.mute(SUBJECT_TYPE, "40")
        self.store.mute(SUBJECT_TYPE, "41")
        self.store.mute(SUBJECT_TYPE, "42")
        self.store.mute(SUBJECT_TYPE, "43")

        self._run([
            tag(10, LANTERN, scene_count=0, description=LANTERN_TEXT),
            tag(11, KETTLE, scene_count=1, description=KETTLE_TEXT),
            tag(20, "Velvet Crane", scene_count=0),
            tag(21, "VelvetCrane", scene_count=1),
            tag(22, "Copper Pan", scene_count=0),
            tag(23, "CopperPan", scene_count=1),
            tag(24, "Slate Roof", scene_count=1),
            tag(25, "SlateRoof", scene_count=0),
            tag(30, "Reed Bed", scene_count=0),
            tag(31, "Ash Coppice", scene_count=1),
            tag(32, "Salt Marsh", scene_count=0),
            tag(40, CISTERN, scene_count=0),
            tag(41, "Iron Gate", scene_count=1),
            tag(42, "Lead Roof", scene_count=0),
            tag(43, "Tin Bath", scene_count=1),
        ])

        self.assertIn("2 already described", self.ctx.message)
        self.assertIn("6 left to their merge", self.ctx.message)
        self.assertIn("3 already proposed", self.ctx.message)
        self.assertIn("4 kept by hand", self.ctx.message)


def _spread(prefix, n, **over):
    """`n` invented tags under one prefix, alternating between the two
    populations, so no bucket in the identity fixture is all of one kind."""
    return [tag("%s-%d" % (prefix, i), "%s %d" % (prefix, i),
                scene_count=i % 2, **over) for i in range(n)]


def _hygiene_counts(store, tags, boxes=(), catalogues=None, performers=(),
                    scenes=None):
    """The low-count half's `Counts` from a WHOLE run of the pass.

    Through `produce`, never by calling `_hygiene` with hand-made arguments:
    the clusters, the source indexes, the unread-source tally and the performer
    half's coverage are all decided by the pass, and a helper that supplied its
    own would let a wiring mistake in `produce` pass unnoticed here.
    """
    captured = {}
    stash = FakeStash(tags, boxes, performers=performers, scenes=scenes)
    producer = TagMergeProducer(stash, store=store, folder=FOLDER,
                                box_client=FakeBoxClient(catalogues or {}))
    real = producer._hygiene

    def spy(*args, **kwargs):
        proposals, counts = real(*args, **kwargs)
        captured["counts"] = counts
        return proposals, counts

    producer._hygiene = spy
    list(producer.produce(FakeCtx()))
    return captured["counts"]


# -- the row, and the grouping this ticket turns on ------------------------ #

def _item(subject_id="1", name=LANTERN, scene_count=0, state="new",
          fingerprint=None, error=None, prior_state=None):
    """One store item shaped exactly as `Store.items` returns it, carrying the
    payload `tag_hygiene.proposal` writes."""
    return {"fingerprint": fingerprint or ("fp-" + subject_id),
            "state": state, "subject_type": SUBJECT_TYPE,
            "subject_id": subject_id, "summary": "s", "confidence": None,
            "payload": {"name": name, "scene_count": scene_count,
                        "counts_cover": COUNTS_COVER},
            "error": error, "prior_state": prior_state}


class TheRow(unittest.TestCase):
    def test_the_whole_row_for_a_new_proposal(self):
        self.assertEqual(
            to_unused_row(_item(subject_id="7", name=LANTERN, scene_count=1),
                          base_url="http://server.invalid"),
            UnusedTagRow(
                kind="unused-tag", fingerprint="fp-7", state="new",
                subject_type=SUBJECT_TYPE, subject_id="7", name=LANTERN,
                tag_url="http://server.invalid/tags/7",
                scene_count=1, counts_cover=COUNTS_COVER, group=ONE_SCENE,
                warning=DELETE_WARNING[ONE_SCENE],
                appliable=True, actionable=True, undismissable=False,
                unmutable=False, error=None))

    def test_the_row_has_no_undo_field_at_all(self):
        # A deletion cannot be taken back, and the cheapest way to guarantee no
        # state ever offers an Undo is for the template to have nothing to read.
        self.assertNotIn("undoable", UnusedTagRow.__dataclass_fields__)
        self.assertNotIn("prior_state", UnusedTagRow.__dataclass_fields__)

    def test_the_warning_is_the_one_for_this_rows_own_population(self):
        # The two differ on whether a scene changes, which is the one thing
        # about them the count does not say out loud. A catch-all warning would
        # satisfy every assertion about "the row warns" while telling a person
        # the wrong thing about half the rows.
        self.assertEqual(to_unused_row(_item(scene_count=0)).warning,
                         DELETE_WARNING[NO_SCENES])
        self.assertEqual(to_unused_row(_item(scene_count=1)).warning,
                         DELETE_WARNING[ONE_SCENE])
        self.assertNotEqual(DELETE_WARNING[NO_SCENES],
                            DELETE_WARNING[ONE_SCENE])

    def test_the_no_scenes_warning_says_no_scene_changes(self):
        self.assertIn("on no scenes", DELETE_WARNING[NO_SCENES])
        self.assertIn("cannot be undone", DELETE_WARNING[NO_SCENES])

    def test_the_one_scene_warning_says_a_scene_does_change(self):
        self.assertIn("loses it", DELETE_WARNING[ONE_SCENE])
        self.assertIn("cannot be undone", DELETE_WARNING[ONE_SCENE])

    def test_no_configured_server_leaves_the_tag_link_none(self):
        self.assertIsNone(to_unused_row(_item()).tag_url)

    def test_a_payload_in_neither_population_raises_rather_than_rendering(self):
        # The producer never writes one. A row with no group would either
        # vanish from the page or land in a group whose note makes a claim
        # about it that nothing checked.
        with self.assertRaises(ValueError) as caught:
            to_unused_row(_item(scene_count=5))

        self.assertIn("neither", str(caught.exception))

    def test_a_payload_with_no_count_raises_rather_than_reading_as_zero(self):
        item = _item()
        del item["payload"]["scene_count"]

        with self.assertRaises(KeyError):
            to_unused_row(item)

    def test_the_four_closed_states_offer_no_control(self):
        for state in ("applied", "dismissed", "muted", "superseded"):
            with self.subTest(state=state):
                row = to_unused_row(_item(state=state))
                self.assertFalse(row.appliable)
                self.assertFalse(row.actionable)

    def test_a_failed_row_is_still_live(self):
        # Nothing was deleted, so the proposal is exactly as open as it was.
        row = to_unused_row(_item(state="failed", error="the server said no"))

        self.assertTrue(row.appliable)
        self.assertTrue(row.actionable)
        self.assertEqual(row.error, "the server said no")

    def test_a_dismissed_row_offers_its_own_reversal(self):
        self.assertTrue(to_unused_row(_item(state="dismissed")).undismissable)

    def test_a_kept_row_offers_its_own_reversal(self):
        self.assertTrue(to_unused_row(_item(state="muted")).unmutable)


class TheGrouping(unittest.TestCase):
    """The design decision this ticket turns on.

    256 tags on no scenes and 793 on exactly one must not arrive as 1049
    top-level rows, and must not arrive as one button that deletes a set
    nobody examined either.
    """

    def test_hundreds_of_tags_become_one_row_per_population(self):
        # Asymmetric on purpose -- three in one group and four in the other --
        # so a builder that counted the groups, or that put everything in one,
        # cannot agree with one that grouped correctly.
        items = ([_item(subject_id=str(i), name="Tag %d" % i, scene_count=0)
                  for i in range(1, 4)]
                 + [_item(subject_id=str(i), name="Tag %d" % i, scene_count=1)
                    for i in range(4, 8)])

        groups = to_unused_groups(items)

        self.assertEqual([(g.group, g.count) for g in groups],
                         [(NO_SCENES, 3), (ONE_SCENE, 4)])
        self.assertEqual(sum(g.count for g in groups), 7)

    def test_a_group_carries_no_control_of_its_own(self):
        # There is nothing for a template to post: no fingerprint, no subject.
        # That is how "no bulk delete" is guaranteed rather than intended.
        for field in ("fingerprint", "subject_id", "subject_type",
                      "appliable", "actionable"):
            self.assertNotIn(field, UnusedTagGroup.__dataclass_fields__)

    def test_every_tag_keeps_its_own_row_inside_its_group(self):
        items = [_item(subject_id="1", name=LANTERN, scene_count=0),
                 _item(subject_id="2", name=KETTLE, scene_count=0)]

        rows = to_unused_groups(items)[0].rows

        self.assertEqual([(r.subject_id, r.fingerprint, r.appliable)
                          for r in rows],
                         [("2", "fp-2", True), ("1", "fp-1", True)])

    def test_an_empty_population_produces_no_group_at_all(self):
        # A row saying "0 tags on no scenes" is always on the page and so stops
        # being read; the section already says out loud when it is empty.
        groups = to_unused_groups([_item(subject_id="1", scene_count=1)])

        self.assertEqual([g.group for g in groups], [ONE_SCENE])

    def test_nothing_at_all_produces_no_groups(self):
        self.assertEqual(to_unused_groups([]), ())

    def test_the_group_count_is_derived_from_the_rows_it_carries(self):
        # Not stored beside them: the number a person reads and the set it
        # describes must not be able to drift apart.
        group = to_unused_groups(
            [_item(subject_id="1", scene_count=0),
             _item(subject_id="2", scene_count=0),
             _item(subject_id="3", scene_count=0)])[0]

        self.assertEqual(group.count, len(group.rows))
        self.assertEqual(group.count, 3)

    def test_rows_are_ordered_by_name_not_by_the_order_the_store_returned(self):
        # Content-derived, so the list reads the same on every visit instead of
        # shifting as proposals are recorded. The fixture arrives in an order
        # that is neither sorted nor reversed, so neither a missing sort nor a
        # descending one passes.
        items = [_item(subject_id="1", name=QUARRY, scene_count=0),
                 _item(subject_id="2", name=LANTERN, scene_count=0),
                 _item(subject_id="3", name=FERRY, scene_count=0)]

        rows = to_unused_groups(items)[0].rows

        self.assertEqual([r.name for r in rows], [FERRY, LANTERN, QUARRY])

    def test_two_tags_sharing_a_name_are_ordered_by_id(self):
        items = [_item(subject_id="9", name=LANTERN, scene_count=0),
                 _item(subject_id="2", name=LANTERN, scene_count=0)]

        rows = to_unused_groups(items)[0].rows

        self.assertEqual([r.subject_id for r in rows], ["2", "9"])

    def test_each_group_carries_its_own_label_and_note(self):
        groups = to_unused_groups([_item(subject_id="1", scene_count=0),
                                   _item(subject_id="2", scene_count=1)])

        self.assertEqual([(g.label, g.note) for g in groups],
                         [(GROUP_LABEL[NO_SCENES], GROUP_NOTE[NO_SCENES]),
                          (GROUP_LABEL[ONE_SCENE], GROUP_NOTE[ONE_SCENE])])

    def test_the_two_notes_are_two_different_paragraphs(self):
        self.assertNotEqual(GROUP_NOTE[NO_SCENES], GROUP_NOTE[ONE_SCENE])
        self.assertIn("changes no scene", GROUP_NOTE[NO_SCENES])
        self.assertIn("exactly one scene", GROUP_NOTE[ONE_SCENE])
        self.assertIn("MERGE", GROUP_NOTE[ONE_SCENE])

    def test_the_link_configured_for_the_page_reaches_every_row(self):
        groups = to_unused_groups([_item(subject_id="4", scene_count=0)],
                                  base_url="http://server.invalid")

        self.assertEqual(groups[0].rows[0].tag_url,
                         "http://server.invalid/tags/4")


# -- the page ------------------------------------------------------------- #

_DELETE_FORM_RE = re.compile(
    r'<form method="post" action="/approve">'
    r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
    r'<button>(?P<label>[^<]*)</button></form>')
_KEEP_FORM_RE = re.compile(
    r'<form method="post" action="/mute">'
    r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
    r'<button>Keep</button></form>')
_GROUP_SUMMARY_RE = re.compile(
    r'<details class="group">\s*<summary>(?P<text>[^<]*)</summary>')


def _page(groups, **over):
    context = dict(rows=[], counts={}, unused=groups,
                   low_count_is_not_proof=LOW_COUNT_IS_NOT_PROOF)
    context.update(over)
    return render("inbox.html", **context)


class ThePage(unittest.TestCase):
    def _groups(self, no_scenes=3, one_scene=4):
        items = [_item(subject_id="n%d" % i, name="Nought %d" % i,
                       scene_count=0) for i in range(no_scenes)]
        items += [_item(subject_id="o%d" % i, name="One %d" % i,
                        scene_count=1) for i in range(one_scene)]
        return to_unused_groups(items)

    def test_seven_tags_reach_the_page_as_two_expandable_rows(self):
        html = _page(self._groups())

        self.assertEqual(
            [m.strip() for m in _GROUP_SUMMARY_RE.findall(html)],
            ["%s (3)" % GROUP_LABEL[NO_SCENES],
             "%s (4)" % GROUP_LABEL[ONE_SCENE]])

    def test_the_sections_own_number_is_the_tag_total_not_the_group_count(self):
        # "(2)" over a thousand tags is the one number on that line a person
        # would read as how much work is waiting.
        html = _page(self._groups())

        self.assertIn("Tags that do almost no work (7)", html)
        self.assertNotIn("Tags that do almost no work (2)", html)

    def test_every_tag_gets_its_own_delete_control(self):
        html = _page(self._groups())

        self.assertEqual(
            [fp for fp, label in _DELETE_FORM_RE.findall(html)],
            ["fp-n0", "fp-n1", "fp-n2", "fp-o0", "fp-o1", "fp-o2", "fp-o3"])

    def test_there_is_no_control_that_deletes_a_group(self):
        # HARM: one click over a set assembled by a rule nobody checked, and
        # not undoable. The group summary carries no form at all, so the only
        # fingerprints any button posts are the seven individual ones.
        html = _page(self._groups())

        posted = {fp for fp, label in _DELETE_FORM_RE.findall(html)}
        self.assertEqual(len(posted), 7)
        for text in _GROUP_SUMMARY_RE.findall(html):
            self.assertNotIn("<form", text)
            self.assertNotIn("<button", text)

    def test_every_tag_gets_its_own_keep_control(self):
        html = _page(self._groups(no_scenes=2, one_scene=3))

        self.assertEqual(len(_KEEP_FORM_RE.findall(html)), 5)

    def test_the_page_never_offers_an_undo_for_a_deletion(self):
        applied = to_unused_groups([_item(subject_id="1", scene_count=0,
                                          state="applied")])

        html = _page(applied)

        self.assertNotIn('action="/undo"', html)

    def test_a_row_with_no_decision_left_draws_no_delete_control(self):
        # On the PAGE, not only on the row: `UnusedTagRow.appliable` is already
        # False in these four states, and a template that ignored it would draw
        # a Delete button that posts a fingerprint the store has closed -- and
        # for `applied`, a second deletion of a tag already gone.
        for state in ("applied", "dismissed", "muted", "superseded"):
            with self.subTest(state=state):
                html = _page(to_unused_groups(
                    [_item(subject_id="1", scene_count=0, state=state)]))
                self.assertEqual(_DELETE_FORM_RE.findall(html), [])

    def test_an_open_row_does_draw_one(self):
        # The permissive side, so "never draw Delete" cannot satisfy the four
        # assertions above.
        html = _page(to_unused_groups([_item(subject_id="1", scene_count=0)]))

        self.assertEqual([label for fp, label
                          in _DELETE_FORM_RE.findall(html)], ["Delete"])

    def test_an_applied_row_still_carries_its_warning(self):
        # By then the tag is gone, and a page that stopped saying so would
        # leave a person looking for the Undo that is not there.
        applied = to_unused_groups([_item(subject_id="1", scene_count=0,
                                          state="applied")])

        self.assertIn(DELETE_WARNING[NO_SCENES], _page(applied))

    def test_the_low_count_caveat_reaches_the_page(self):
        self.assertIn(LOW_COUNT_IS_NOT_PROOF, _page(self._groups()))

    def test_the_caveat_comes_from_the_module_that_owns_it(self):
        # Named properties of the sentence rather than a second copy of it: a
        # test comparing the page against the constant it was rendered from can
        # only ever confirm the page agrees with itself.
        self.assertIn("evidence, not proof", LOW_COUNT_IS_NOT_PROOF)
        self.assertIn("one at a time", LOW_COUNT_IS_NOT_PROOF)

    def test_the_caveat_is_wired_through_the_handler_not_only_the_template(self):
        # It has no default in the template, so an entry point that stopped
        # passing it would render a blank paragraph rather than raise. Driven
        # through `build_handler`'s own GET, with no socket: `do_GET` reads
        # `self.path` and calls `self._send`, and nothing else.
        handler = build_handler(rows=lambda: [], actions=None,
                                unused=lambda: self._groups())
        instance = object.__new__(handler)
        instance.path = "/"
        sent = {}
        instance._send = lambda status, body=b"", headers=(): sent.update(
            status=status, body=body)

        instance.do_GET()

        html = sent["body"].decode()
        self.assertIn(LOW_COUNT_IS_NOT_PROOF, html)
        self.assertIn("Tags that do almost no work (7)", html)

    def test_a_page_with_none_of_these_says_so(self):
        html = _page(())

        self.assertIn("Tags that do almost no work (0)", html)
        self.assertNotIn('<details class="group">', html)

    def test_each_row_shows_its_own_count_and_what_the_count_covers(self):
        html = _page(to_unused_groups([_item(subject_id="1", scene_count=1)]))

        self.assertIn('<div class="score">1<div class="note">scenes</div>',
                      html)

    def test_a_hostile_tag_name_is_escaped(self):
        hostile = '<script>alert("x")</script>'
        html = _page(to_unused_groups(
            [_item(subject_id="1", name=hostile, scene_count=0)]))

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_failed_row_offers_a_retry_and_says_nothing_was_written(self):
        html = _page(to_unused_groups(
            [_item(subject_id="1", scene_count=0, state="failed",
                   error="the server said no")]))

        self.assertEqual([label for fp, label in
                          _DELETE_FORM_RE.findall(html)], ["Try again"])
        self.assertIn("nothing was written", html)


# -- the write ------------------------------------------------------------ #

class _DeleteTransport:
    """Fake transport for the deletion write path. Opens no socket.

    Holds a small library -- tags with their scene counts, and scenes with the
    tags they carry -- and MUTATES it in response to the writes a real server
    would honour. That is what makes "deleting a tag attached to nothing
    changed no scene" an assertion about the library rather than about the call
    returning: `sceneUpdate` here really does replace a scene's tag list, so a
    client that issued one shows up as a changed scene.

    It is not more capable than the real server, deliberately: an unknown tag
    answers `findTag: null`, exactly as the real one does, rather than
    inventing a count of zero -- which is the one answer under which deleting
    something already gone would look like a success.
    """

    def __init__(self, tags=None, scenes=None):
        self.tags = dict(tags or {})
        self.scenes = {sid: list(t) for sid, t in (scenes or {}).items()}
        self.calls = []

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        if "findTag(" in q:
            if variables["id"] not in self.tags:
                return {"data": {"findTag": None}}
            return {"data": {"findTag": {
                "id": variables["id"],
                "scene_count": self.tags[variables["id"]]}}}
        if "tagDestroy(" in q:
            self.tags.pop(variables["id"], None)
            return {"data": {"tagDestroy": True}}
        if "sceneUpdate(" in q:
            self.scenes[variables["in"]["id"]] = list(
                variables["in"]["tag_ids"])
            return {"data": {"sceneUpdate": {"id": variables["in"]["id"]}}}
        raise AssertionError("test transport does not recognize query: %s" % q)

    def snapshot(self):
        return {sid: list(t) for sid, t in self.scenes.items()}

    def operations(self):
        """Every GraphQL operation sent, in order, named by what it does."""
        names = []
        for q, _ in self.calls:
            for marker in ("findTag(", "tagDestroy(", "sceneUpdate("):
                if marker in q:
                    names.append(marker.rstrip("("))
        return names


def _stash(transport):
    return Stash("http://server.invalid", "k", transport=transport)


class TheWrite(unittest.TestCase):
    def test_deleting_a_tag_attached_to_nothing_changes_no_scene(self):
        # The whole scene table before and after, not "the call succeeded".
        # `sceneUpdate` really would change this transport's scenes, so a
        # client that wrote one fails here.
        t = _DeleteTransport(tags={"t-7": 0, "t-4": 3},
                             scenes={"sc-1": ["t-4"], "sc-2": ["t-4"]})
        before = t.snapshot()

        _stash(t).delete_tag("t-7", expected_scene_count=0)

        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.operations(), ["findTag", "tagDestroy"])
        self.assertNotIn("t-7", t.tags)

    def test_the_count_is_read_immediately_before_the_write(self):
        # Taking the comparison anywhere else opens a window between it and
        # the write; the order is the guard.
        t = _DeleteTransport(tags={"t-7": 1})

        _stash(t).delete_tag("t-7", expected_scene_count=1)

        self.assertEqual(t.operations(), ["findTag", "tagDestroy"])

    def test_a_tag_whose_count_has_moved_is_refused_and_not_deleted(self):
        # HARM: a proposal can be days old. A tag put to work since would be
        # taken off every scene carrying it, with nothing recording which.
        t = _DeleteTransport(tags={"t-7": 40})

        with self.assertRaises(StashError) as caught:
            _stash(t).delete_tag("t-7", expected_scene_count=0)

        self.assertIn("40", str(caught.exception))
        self.assertEqual(t.operations(), ["findTag"])
        self.assertIn("t-7", t.tags)

    def test_a_tag_that_grew_from_one_scene_to_two_is_refused(self):
        t = _DeleteTransport(tags={"t-7": 2})

        with self.assertRaises(StashError):
            _stash(t).delete_tag("t-7", expected_scene_count=1)

        self.assertEqual(t.operations(), ["findTag"])

    def test_a_tag_still_on_the_count_it_was_proposed_at_is_deleted(self):
        # The permissive side of the same guard, pinned separately: a check
        # that refused everything would satisfy every assertion above.
        t = _DeleteTransport(tags={"t-7": 1})

        _stash(t).delete_tag("t-7", expected_scene_count=1)

        self.assertNotIn("t-7", t.tags)

    def test_a_tag_the_server_does_not_know_is_refused_not_reported_deleted(self):
        t = _DeleteTransport(tags={})

        with self.assertRaises(StashError):
            _stash(t).delete_tag("t-7", expected_scene_count=0)

        self.assertEqual(t.operations(), ["findTag"])

    def test_the_expected_count_has_no_default_and_must_be_named(self):
        # A caller who forgot it and a caller who meant "delete regardless"
        # must not be able to write the same thing, and the second is not
        # offered at all.
        t = _DeleteTransport(tags={"t-7": 0})

        with self.assertRaises(TypeError):
            _stash(t).delete_tag("t-7")
        with self.assertRaises(TypeError):
            _stash(t).delete_tag("t-7", 0)

        self.assertEqual(t.operations(), [])

    def test_the_scene_count_read_answers_none_for_an_unknown_tag(self):
        self.assertIsNone(_stash(_DeleteTransport()).tag_scene_count("t-7"))

    def test_the_scene_count_read_answers_the_servers_own_number(self):
        self.assertEqual(
            _stash(_DeleteTransport(tags={"t-7": 12})).tag_scene_count("t-7"),
            12)


# -- approving one --------------------------------------------------------- #

class _RecordingStash:
    def __init__(self, fail=None):
        self.calls = []
        self._fail = fail

    def delete_tag(self, tag_id, *, expected_scene_count):
        self.calls.append(("delete", tag_id, expected_scene_count))
        if self._fail is not None:
            raise self._fail
        return None


class _RecordingStore:
    def __init__(self, item):
        self.item = item
        self.calls = []
        self.muted = set()

    def items(self, folder=None, state=None, limit=None, offset=0):
        if state == "dismissed":
            return []
        if state is not None:
            # Answered for the state the row is ACTUALLY in, never for
            # whichever one was asked about -- the same note as on
            # `tests.test_web_actions._FakeStore.items`. A double answering
            # every explicit `state=` with its one row would report this tag
            # as a subject the media server no longer holds.
            return [self.item] if self.item["state"] == state else []
        return [row for row in [self.item]
                if row["state"] not in Store._HIDDEN_STATES]

    def mark_applied(self, fp, prior_state=None):
        self.calls.append(("applied", fp, prior_state))

    def mark_failed(self, fp, error):
        self.calls.append(("failed", fp, error))

    def mute(self, subject_type, subject_id, reason=None):
        self.calls.append(("muted", subject_type, subject_id, reason))

    def dismiss(self, fp, reason=None):
        self.calls.append(("dismissed", fp, reason))

    def muted_subjects(self):
        return self.muted


class Approving(unittest.TestCase):
    def test_approving_deletes_exactly_that_tag_at_the_count_recorded(self):
        item = _item(subject_id="7", scene_count=1)
        store, stash = _RecordingStore(item), _RecordingStash()

        result = Actions(store, stash).approve("fp-7")

        self.assertEqual(result, "deleted")
        self.assertEqual(stash.calls, [("delete", "7", 1)])

    def test_the_count_passed_to_the_write_is_the_one_the_payload_recorded(self):
        # Two proposals with different counts, so a hard-coded 0 fails.
        for count in (0, 1):
            with self.subTest(count=count):
                item = _item(subject_id="7", scene_count=count)
                stash = _RecordingStash()
                Actions(_RecordingStore(item), stash).approve("fp-7")
                self.assertEqual(stash.calls, [("delete", "7", count)])

    def test_an_applied_deletion_is_recorded_with_no_snapshot(self):
        # This is where the irreversibility is enforced rather than described:
        # no snapshot in the store means no row can ever claim an undo.
        item = _item(subject_id="7", scene_count=0)
        store = _RecordingStore(item)

        Actions(store, _RecordingStash()).approve("fp-7")

        self.assertEqual(store.calls, [("applied", "fp-7", None)])

    def test_a_refused_write_is_recorded_as_failed_and_raises(self):
        item = _item(subject_id="7", scene_count=0)
        store = _RecordingStore(item)
        stash = _RecordingStash(fail=StashError("tag 7 is on 40 scenes"))

        with self.assertRaises(ApplyFailed):
            Actions(store, stash).approve("fp-7")

        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0][0], "failed")
        self.assertIn("40 scenes", store.calls[0][2])

    def test_no_media_server_is_recorded_as_failed_with_the_reason(self):
        item = _item(subject_id="7", scene_count=0)
        store = _RecordingStore(item)

        with self.assertRaises(ApplyFailed):
            Actions(store, None).approve("fp-7")

        self.assertEqual(store.calls[0][0], "failed")
        self.assertIn("--server", store.calls[0][2])

    def test_a_deletion_can_never_be_undone_and_says_why(self):
        item = _item(subject_id="7", scene_count=0, state="applied")
        store = _RecordingStore(item)

        with self.assertRaises(ValueError) as caught:
            Actions(store, _RecordingStash()).undo("fp-7")

        self.assertIn(DELETE_IS_IRREVERSIBLE, str(caught.exception))
        self.assertEqual(store.calls, [])

    def test_the_undo_refusal_is_the_reason_not_a_missing_snapshot(self):
        # "No snapshot was stored for it" reads as an omission somebody could
        # go and fix. None can exist.
        item = _item(subject_id="7", scene_count=0, state="applied")

        with self.assertRaises(ValueError) as caught:
            Actions(_RecordingStore(item), _RecordingStash()).undo("fp-7")

        self.assertNotIn("no snapshot was stored", str(caught.exception))

    def test_keep_records_a_mute_on_this_passs_own_subject_only(self):
        item = _item(subject_id="7", scene_count=0)
        store = _RecordingStore(item)

        Actions(store, _RecordingStash()).mute("fp-7")

        self.assertEqual(store.calls,
                         [("muted", SUBJECT_TYPE, "7",
                           "muted from the inbox")])

    def test_nothing_on_this_path_can_delete_more_than_one_tag(self):
        # The containment the whole finding rests on. `Actions` exposes one
        # approve, keyed by one fingerprint, and the row it finds names one
        # subject.
        item = _item(subject_id="7", scene_count=0)
        stash = _RecordingStash()

        Actions(_RecordingStore(item), stash).approve("fp-7")

        self.assertEqual(len(stash.calls), 1)
