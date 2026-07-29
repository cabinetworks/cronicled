"""Clustering near-duplicate tags, and the merge proposal that comes of it.

Every name here is invented. The clusters this was written against were real
people's names and none of them appear in this file or anywhere else in this
repository.
"""
import inspect
import unittest

from cronicled.jobs import COST_CLASS_LIMITS, JobRunner
from cronicled.store import Store
from cronicled.tags import (COUNTS_COVER, SUBJECT_TYPE, UNDECIDED_EVEN,
                            UNDECIDED_MANY, TagMergeProducer, Counts,
                            cluster_tags, proposal, select)

FOLDER = "library"
WAIT = 10


def tag(id, name, scene_count=0, aliases=()):
    """One row shaped exactly as `Stash.all_tags` returns it -- `id`, `name`,
    `aliases` and `scene_count`, and nothing else. Written from that method's
    own selection set rather than from what this module happens to read, so a
    field the client stops selecting is a failure here."""
    return {"id": str(id), "name": name, "aliases": list(aliases),
            "scene_count": scene_count}


class FakeCtx:
    """What the runner gives a producer: somewhere to log progress.

    Keeps only the LAST message, because that is all the real collaborator
    keeps: `JobRunner._log` assigns `state.message`, one field, no history.
    A double that accumulated a list would let an assertion pass against a
    property production does not have.
    """

    def __init__(self):
        self.message = None

    def log(self, message):
        self.message = message


class FakeStash:
    """The one read a tag-merge pass makes, and nothing else.

    Any other attribute refuses: this pass proposes and never writes, and a
    write introduced here is meant to show up as a failure rather than as a
    silently tolerated call.
    """

    def __init__(self, tags):
        self._tags = list(tags)
        self.calls = []

    def all_tags(self):
        self.calls.append("all_tags")
        return list(self._tags)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the tag-merge pass called %r on the media server; it reads "
                "the tag list and proposes, it never writes" % (name,))
        return refuse


class Clustering(unittest.TestCase):
    """What is one tag written twice, and what is two tags."""

    def test_two_spellings_differing_only_by_spacing_cluster(self):
        clusters = cluster_tags([tag(1, "Velvet Crane"),
                                 tag(2, "VelvetCrane")])

        self.assertEqual(len(clusters), 1)
        self.assertEqual([m["name"] for m in clusters[0].members],
                         ["Velvet Crane", "VelvetCrane"])
        self.assertEqual(clusters[0].key, "velvetcrane")

    def test_two_spellings_differing_only_by_case_cluster(self):
        clusters = cluster_tags([tag(1, "Ivy Kingsley"),
                                 tag(2, "IVY KINGSLEY")])

        self.assertEqual(len(clusters), 1)

    def test_two_spellings_differing_only_by_punctuation_cluster(self):
        clusters = cluster_tags([tag(1, "Amber Vale"), tag(2, "Amber.Vale")])

        self.assertEqual(len(clusters), 1)

    def test_near_misses_do_not_cluster(self):
        """The other half, and the one that decides whether this is usable.

        Measured against a real library: 2704 tags, seven clusters. A rule
        loosened to containment, to a plural strip, or to any edit distance
        would gather every one of these -- and burying seven real findings
        under near-misses loses them more completely than proposing nothing
        would. So a plural, a longer title containing the name, and an
        unrelated tag must all stay apart, and this is what fails if the rule
        is ever widened.
        """
        clusters = cluster_tags([
            tag(1, "Velvet Crane"),
            tag(2, "Velvet Cranes"),        # a plural
            tag(3, "Velvet Crane Reel"),    # contains the whole name
            tag(4, "Copper Kettle"),        # nothing in common
        ])

        self.assertEqual(clusters, [])

    def test_a_tag_on_its_own_is_not_a_cluster(self):
        self.assertEqual(cluster_tags([tag(1, "Velvet Crane")]), [])

    def test_names_that_normalise_to_nothing_are_not_clustered(self):
        # Every punctuation-only name reduces to the same empty key, so
        # clustering them would gather unrelated tags on the strength of
        # having no letters in common at all.
        self.assertEqual(cluster_tags([tag(1, "!!!"), tag(2, "???")]), [])

    def test_members_are_ordered_by_content_not_by_arrival(self):
        forwards = cluster_tags([tag(9, "VelvetCrane"), tag(1, "Velvet Crane")])
        backwards = cluster_tags([tag(1, "Velvet Crane"), tag(9, "VelvetCrane")])

        self.assertEqual(forwards, backwards)

    def test_a_row_with_no_scene_count_raises_rather_than_reading_as_zero(self):
        # A blast radius that reads as zero because a field was missing is
        # the one wrong answer this cannot afford: it puts "this merge moves
        # nothing" in front of somebody about to authorise an irreversible
        # write.
        with self.assertRaises(KeyError):
            cluster_tags([{"id": "1", "name": "Velvet Crane", "aliases": []},
                          {"id": "2", "name": "VelvetCrane", "aliases": []}])


class WhichSpellingSurvives(unittest.TestCase):
    """The canonical name is read off the text, never off a position."""

    def test_the_more_written_out_spelling_wins(self):
        clusters = cluster_tags([tag(1, "Velvet Crane"),
                                 tag(2, "VelvetCrane")])

        self.assertEqual(clusters[0].canonical["name"], "Velvet Crane")
        self.assertIsNone(clusters[0].undecided)

    def test_the_winner_is_not_simply_the_last_member(self):
        # `members` is ordered by name, and in THIS pair the written-out
        # spelling happens to sort first -- so a rule reading `members[-1]`
        # gives the wrong answer here and this is what says so.
        clusters = cluster_tags([tag(1, "Velvet Crane"),
                                 tag(2, "VelvetCrane")])

        self.assertEqual([m["name"] for m in clusters[0].members],
                         ["Velvet Crane", "VelvetCrane"])
        self.assertEqual(clusters[0].canonical["name"], "Velvet Crane")

    def test_the_winner_is_not_simply_the_first_member(self):
        """The other half, and the one the pair above cannot supply.

        With only two members the winner is always either the first or the
        last, so ONE fixture can never distinguish a content rule from a
        position rule -- whichever end it lands on, one of the two position
        rules answers correctly and looks tested. Here the written-out
        spelling sorts SECOND (an uppercase letter sorts before a lowercase
        one), so `members[0]` gives the wrong answer; above, `members[-1]`
        does. Together they leave neither end standing.
        """
        clusters = cluster_tags([tag(1, "VelvetCrane"),
                                 tag(2, "velvet crane")])

        self.assertEqual([m["name"] for m in clusters[0].members],
                         ["VelvetCrane", "velvet crane"])
        self.assertEqual(clusters[0].canonical["name"], "velvet crane")

    def test_reversing_the_input_does_not_change_which_one_wins(self):
        """The fixture is deliberately ASYMMETRIC.

        Two spellings that differ only by case would answer the same thing
        whichever way round they went in, so reversing them could never
        detect a rule that picked by position -- the mutation would look
        killed while nothing had tested it. These two differ in word count
        AND in id AND in scene count, so a rule reading first-wins,
        last-wins, largest-id or largest-count gives a different answer in at
        least one of the two orders.
        """
        forwards = cluster_tags([tag(1, "Velvet Crane", scene_count=3),
                                 tag(9, "VelvetCrane", scene_count=40)])
        backwards = cluster_tags([tag(9, "VelvetCrane", scene_count=40),
                                  tag(1, "Velvet Crane", scene_count=3)])

        self.assertEqual(forwards[0].canonical["name"], "Velvet Crane")
        self.assertEqual(backwards[0].canonical["name"], "Velvet Crane")
        # Whole clusters, not just the winner: a payload that differed in
        # member order between the two runs would be two fingerprints for one
        # finding, and the inbox would hold it twice.
        self.assertEqual(forwards, backwards)

    def test_a_cluster_of_three_is_reported_and_never_resolved(self):
        """Three spellings do not say which is canonical.

        The fixture gives each member a DIFFERENT word count, so there is a
        unique most-written-out spelling for a rule to latch onto. That is
        what makes this able to fail: with the size check gone, the
        two-spelling rule below it answers "Ivy May Kingsley" and this test
        goes red. A fixture whose three members tied would leave the same
        mutation reporting `None` and looking correct.
        """
        clusters = cluster_tags([tag(1, "IvyMayKingsley"),
                                 tag(2, "Ivy MayKingsley"),
                                 tag(3, "Ivy May Kingsley")])

        self.assertEqual(len(clusters), 1)
        self.assertIsNone(clusters[0].canonical)
        self.assertEqual(clusters[0].undecided, UNDECIDED_MANY)
        self.assertEqual(len(clusters[0].members), 3)

    def test_two_spellings_in_the_same_number_of_words_are_reported(self):
        # A case difference carries no evidence about which was meant.
        # Inventing one would be the three-spelling mistake at a smaller size.
        clusters = cluster_tags([tag(1, "Ivy Kingsley"),
                                 tag(2, "IVY KINGSLEY")])

        self.assertIsNone(clusters[0].canonical)
        self.assertEqual(clusters[0].undecided, UNDECIDED_EVEN)

    def test_the_two_undecided_reasons_are_different_answers(self):
        # Collapsing both into one catch-all string would satisfy every
        # assertion of the form "there is a reason" while losing the
        # distinction that sends a reviewer to two different judgements.
        self.assertNotEqual(UNDECIDED_MANY, UNDECIDED_EVEN)


class ProposalShape(unittest.TestCase):
    def test_the_whole_proposal_for_a_decided_cluster(self):
        # The WHOLE dict, not sampled fields: an extra key here is an extra
        # key in the fingerprint, which re-proposes every cluster in the
        # library once, and a field-by-field check cannot see one arrive.
        cluster = cluster_tags([tag(1, "Velvet Crane", scene_count=12),
                                tag(9, "VelvetCrane", scene_count=4)])[0]

        self.assertEqual(proposal(cluster, FOLDER), {
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "velvetcrane",
            "summary": "2 spellings of one tag: Velvet Crane (12 scenes), "
                       "VelvetCrane (4 scenes)",
            "payload": {
                "key": "velvetcrane",
                "members": [
                    {"id": "1", "name": "Velvet Crane", "scene_count": 12},
                    {"id": "9", "name": "VelvetCrane", "scene_count": 4},
                ],
                "canonical": {"id": "1", "name": "Velvet Crane",
                              "scene_count": 12},
                "undecided": None,
                "counts_cover": COUNTS_COVER,
            },
        })

    def test_the_whole_proposal_for_an_undecided_cluster(self):
        cluster = cluster_tags([tag(1, "IvyMayKingsley", scene_count=1),
                                tag(2, "Ivy MayKingsley", scene_count=2),
                                tag(3, "Ivy May Kingsley", scene_count=3)])[0]

        self.assertEqual(proposal(cluster, FOLDER), {
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "ivymaykingsley",
            "summary": "3 spellings of one tag: Ivy May Kingsley (3 scenes), "
                       "Ivy MayKingsley (2 scenes), IvyMayKingsley (1 scenes)",
            "payload": {
                "key": "ivymaykingsley",
                "members": [
                    {"id": "3", "name": "Ivy May Kingsley", "scene_count": 3},
                    {"id": "2", "name": "Ivy MayKingsley", "scene_count": 2},
                    {"id": "1", "name": "IvyMayKingsley", "scene_count": 1},
                ],
                "canonical": None,
                "undecided": UNDECIDED_MANY,
                "counts_cover": COUNTS_COVER,
            },
        })

    def test_the_counts_are_the_ones_the_server_reported(self):
        # Not derived, not defaulted: the number a reviewer weighs the merge
        # by is the one that came back with the tag.
        cluster = cluster_tags([tag(1, "Velvet Crane", scene_count=1187),
                                tag(9, "VelvetCrane", scene_count=2)])[0]
        payload = proposal(cluster, FOLDER)["payload"]

        self.assertEqual([m["scene_count"] for m in payload["members"]],
                         [1187, 2])

    def test_it_carries_no_confidence(self):
        # The store documents `confidence` as a 0-to-1 score and enforces the
        # range. A merge is not scored, and a 1.0 here would state a
        # certainty nothing computed.
        cluster = cluster_tags([tag(1, "Velvet Crane"), tag(9, "VelvetCrane")])[0]

        self.assertNotIn("confidence", proposal(cluster, FOLDER))


class Selection(unittest.TestCase):
    """Which clusters a run proposes, and why the others were dropped."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.clusters = cluster_tags([
            tag(1, "Velvet Crane"), tag(2, "VelvetCrane"),
            tag(3, "Ivy Kingsley"), tag(4, "IvyKingsley"),
        ])
        self.assertEqual(len(self.clusters), 2)

    def test_everything_is_selected_against_an_empty_store(self):
        selected, counts = select(self.clusters, store=self.store,
                                  folder=FOLDER)

        self.assertEqual(selected, self.clusters)
        self.assertEqual(counts, Counts(total=2, already_proposed=0, muted=0,
                                        selected=2))

    def test_a_muted_cluster_is_dropped(self):
        self.store.mute(SUBJECT_TYPE, "velvetcrane")

        selected, counts = select(self.clusters, store=self.store,
                                  folder=FOLDER)

        self.assertEqual([c.key for c in selected], ["ivykingsley"])
        self.assertEqual(counts, Counts(total=2, already_proposed=0, muted=1,
                                        selected=1))

    def test_a_mute_on_another_subject_type_does_not_drop_it(self):
        # `mute` is keyed by (type, id), and a scene whose id happened to
        # read like a cluster key must not silence a tag cluster.
        self.store.mute("scene", "velvetcrane")

        selected, _ = select(self.clusters, store=self.store, folder=FOLDER)

        self.assertEqual(len(selected), 2)

    def test_a_cluster_that_already_has_a_visible_proposal_is_dropped(self):
        """The narrowing that keeps this quiet night after night.

        A proposal's payload carries scene counts, and a count moves whenever
        a scene is tagged. A moved count is a different payload, so a
        different fingerprint, so a SECOND row rather than a touch of the
        first -- this is what stops that.
        """
        self.store.record(FOLDER, SUBJECT_TYPE, "velvetcrane", "s",
                          {"anything": 1}, "tag-merge")

        selected, counts = select(self.clusters, store=self.store,
                                  folder=FOLDER)

        self.assertEqual([c.key for c in selected], ["ivykingsley"])
        self.assertEqual(counts, Counts(total=2, already_proposed=1, muted=0,
                                        selected=1))

    def test_a_proposal_for_a_scene_does_not_drop_a_cluster(self):
        self.store.record(FOLDER, "scene", "velvetcrane", "s", {"a": 1}, "p")

        selected, _ = select(self.clusters, store=self.store, folder=FOLDER)

        self.assertEqual(len(selected), 2)

    def test_a_proposal_in_another_folder_does_not_drop_it(self):
        self.store.record("elsewhere", SUBJECT_TYPE, "velvetcrane", "s",
                          {"a": 1}, "tag-merge")

        selected, _ = select(self.clusters, store=self.store, folder=FOLDER)

        self.assertEqual(len(selected), 2)

    def test_the_counts_account_for_every_cluster_offered(self):
        # total == already_proposed + muted + selected, always. One assertion
        # catching a cluster that vanished for a reason nobody named, which
        # no per-field check can see.
        self.store.mute(SUBJECT_TYPE, "velvetcrane")

        _, counts = select(self.clusters, store=self.store, folder=FOLDER)

        self.assertEqual(counts.total, counts.already_proposed + counts.muted
                         + counts.selected)


class Producer(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def build(self, tags, **kwargs):
        self.stash = FakeStash(tags)
        kwargs.setdefault("folder", FOLDER)
        return TagMergeProducer(self.stash, store=self.store, **kwargs)

    def run_pass(self, tags, **kwargs):
        return list(self.build(tags, **kwargs).produce(self.ctx))

    def test_it_satisfies_the_producer_protocol(self):
        producer = self.build([], every=86400)

        self.assertEqual(producer.name, "tag-merge")
        self.assertIn(producer.cost, COST_CLASS_LIMITS)
        stream = producer.produce(self.ctx)
        self.assertTrue(inspect.isgenerator(stream))
        # Making a generator is not running one: nothing has been read from
        # the media server yet, so `start()` can do this on the caller's
        # thread and leave the work on the worker's.
        self.assertEqual(self.stash.calls, [])
        stream.close()

    def test_it_is_not_in_the_rate_limited_cost_class(self):
        # One paged read of the server's own tag list -- no third-party
        # scraper. Filed under `scraping` it would serialise behind a
        # full-library scrape for nothing.
        self.assertEqual(self.build([]).cost, "local")
        self.assertIsNone(COST_CLASS_LIMITS["local"])

    def test_it_yields_one_proposal_per_cluster(self):
        proposals = self.run_pass([tag(1, "Velvet Crane", scene_count=12),
                                   tag(2, "VelvetCrane", scene_count=4),
                                   tag(3, "Copper Kettle", scene_count=7)])

        self.assertEqual([p["subject_id"] for p in proposals], ["velvetcrane"])
        self.assertEqual(proposals[0]["folder"], FOLDER)

    def test_a_library_with_no_duplicate_spellings_yields_nothing(self):
        self.assertEqual(self.run_pass([tag(1, "Velvet Crane"),
                                        tag(2, "Copper Kettle")]), [])

    def test_the_closing_line_distinguishes_suppressed_from_nothing_to_do(self):
        """A finished job keeps ONE message, so it has to carry both facts.

        "0 proposed" alone reads identically for a library with no duplicate
        spellings and for one whose every cluster a person has already muted,
        and those call for opposite responses.
        """
        self.store.mute(SUBJECT_TYPE, "velvetcrane")

        self.run_pass([tag(1, "Velvet Crane"), tag(2, "VelvetCrane")])

        self.assertIn("1 clusters", self.ctx.message)
        self.assertIn("0 proposed", self.ctx.message)
        self.assertIn("1 muted", self.ctx.message)

    def test_the_runner_records_what_it_yields(self):
        producer = self.build([tag(1, "Velvet Crane", scene_count=12),
                               tag(2, "VelvetCrane", scene_count=4)])
        runner = JobRunner(self.store)
        runner.register(producer)

        job = runner.start(producer.name)
        self.assertTrue(runner.wait(job.id, WAIT))
        finished = runner.job(job.id)

        self.assertEqual(finished.state, "done", finished.traceback)
        self.assertEqual(finished.recorded, 1)
        stored = self.store.items(folder=FOLDER)
        self.assertEqual([i["subject_type"] for i in stored], [SUBJECT_TYPE])
        self.assertEqual([i["subject_id"] for i in stored], ["velvetcrane"])

    def test_a_second_run_whose_counts_moved_proposes_nothing_new(self):
        """The noise this would otherwise make every single night.

        The second run sees the SAME cluster with a different scene count --
        a scene was tagged in between, which is ordinary. A different count
        is a different payload, so a different fingerprint, so the store
        would record a SECOND row rather than touch the first. Nothing about
        the store prevents that; the selection narrowing does, and this is
        what fails if it goes.
        """
        runner = JobRunner(self.store)
        recorded = []
        for count in (4, 5):
            producer = self.build([tag(1, "Velvet Crane", scene_count=12),
                                   tag(2, "VelvetCrane", scene_count=count)])
            runner.reregister(producer)
            job = runner.start(producer.name)
            self.assertTrue(runner.wait(job.id, WAIT))
            recorded.append(runner.job(job.id).recorded)

        self.assertEqual(recorded, [1, 0])
        self.assertEqual(len(self.store.items(folder=FOLDER)), 1)


if __name__ == "__main__":
    unittest.main()
