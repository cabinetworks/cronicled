"""Clustering near-duplicate tags, and the merge proposal that comes of it.

Every name here is invented. The clusters this was written against were real
people's names and none of them appear in this file or anywhere else in this
repository.
"""
import inspect
import unittest

from cronicled.jobs import COST_CLASS_LIMITS, JobRunner
from cronicled.stash import StashError
from cronicled.stashbox import TagCatalogue
from cronicled.store import Store
from cronicled.tag_descriptions import SUBJECT_TYPE as TAG_SUBJECT
from cronicled.tags import (COUNTS_COVER, SUBJECT_TYPE, UNDECIDED_EVEN,
                            UNDECIDED_MANY, TagMergeProducer, Counts,
                            cluster_tags, proposal, select)

FOLDER = "library"
WAIT = 10


def tag(id, name, scene_count=0, aliases=(), description=None):
    """One row shaped exactly as `Stash.all_tags` returns it -- `id`, `name`,
    `aliases`, `description` and `scene_count`, and nothing else. Written from
    that method's own selection set rather than from what this module happens
    to read, so a field the client stops selecting is a failure here.

    `description` defaults to `None`, which is what the server really returns
    for a tag nobody has described -- never `""`. The apply path compares the
    server's value against the one a proposal recorded, so a fixture that
    normalised the two would be testing a comparison production never makes.
    """
    return {"id": str(id), "name": name, "aliases": list(aliases),
            "description": description, "scene_count": scene_count}


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
    """The four reads a tag pass makes, and nothing else.

    Any other attribute refuses: this pass proposes and never writes, and a
    write introduced here is meant to show up as a failure rather than as a
    silently tolerated call.

    `stash_box_credentials` answers a LIST, like the real one, and never a
    mapping. The pass treats its order as the operator's configured
    preference, so a double that handed back something whose order was
    incidental would let an order test pass against a property production
    does not have.

    `performers_with_aliases` and `tagged_scenes` are the performer half's two
    reads. Both default to "this library has none", which is what the merge and
    description tests below want: no tag can match a performer, so that half
    proposes nothing and their assertions are about the halves they name.
    `tagged_scenes` answers the `(count, scenes)` PAIR the real one does, and
    the counts it reports are the pages' own -- see `scenes_for`.
    """

    def __init__(self, tags, boxes=(), performers=(), scenes=None):
        self._tags = list(tags)
        self._boxes = list(boxes)
        self._performers = list(performers)
        self._scenes = dict(scenes or {})
        self.calls = []

    def all_tags(self):
        self.calls.append("all_tags")
        return list(self._tags)

    def stash_box_credentials(self):
        self.calls.append("stash_box_credentials")
        return list(self._boxes)

    def performers_with_aliases(self):
        self.calls.append("performers_with_aliases")
        return list(self._performers)

    def tagged_scenes(self, tag_id, limit):
        self.calls.append(("tagged_scenes", tag_id, limit))
        rows = self._scenes.get(tag_id, [])
        return len(rows), list(rows)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the tag pass called %r on the media server; it reads the tag "
                "list and the configured sources and proposes, it never "
                "writes" % (name,))
        return refuse


class FakeBoxClient:
    """Stands in for `cronicled.stashbox.StashBox` -- the constructor and the
    ONE call the tag pass makes on it.

    Keyed by the base url the pass built, so a test can see that the
    `/graphql` a media server stores against a box was trimmed off before the
    client appended its own. Every answer is a real `TagCatalogue`, never a
    plain list: the pass reads `complete` off it, and a double that carried
    only the tags would let a test pass against a shape production never
    hands back.
    """

    def __init__(self, catalogues):
        self._catalogues = dict(catalogues)
        self.asked = []

    def __call__(self, url, api_key):
        self.asked.append((url, api_key))
        return _FakeBox(self._catalogues[url])


class _FakeBox:
    def __init__(self, answer):
        self._answer = answer

    def all_tags(self):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def box_credential(name, endpoint=None, api_key=None):
    """One entry as `Stash.stash_box_credentials` returns it. `.invalid` is
    the reserved TLD that can never resolve, so nothing here could reach
    anything even if the fake client were removed by accident."""
    return {"name": name,
            "endpoint": endpoint or "https://%s.invalid/graphql" % name,
            "api_key": api_key or ("key-" + name)}


def box_tag(name, description, aliases=()):
    """One row as `StashBox.all_tags` returns it, with every field the query
    selects."""
    return {"id": "b-" + name, "name": name, "description": description,
            "aliases": list(aliases)}


def catalogue(tags, complete=True):
    return TagCatalogue(tags, complete=complete)


# Two visibly different sentences, so no assertion below can pass by
# comparing a value against itself. Both invented.
LANTERN = "Scenes lit only by a hand-carried lamp."
FERRY = "Filmed aboard a working passenger boat."


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
        #
        # `description` is PRESENT on both rows so that the only field missing
        # is the one this test is named for. Left out as well, either indexed
        # read satisfies the assertion and neither is pinned -- the
        # over-determined fixture that let the description read drift to a
        # default with the suite green.
        with self.assertRaises(KeyError):
            cluster_tags([{"id": "1", "name": "Velvet Crane", "aliases": [],
                           "description": None},
                          {"id": "2", "name": "VelvetCrane", "aliases": [],
                           "description": None}])

    def test_a_row_with_no_description_raises_rather_than_reading_as_blank(self):
        # The sibling, and the same reasoning one step further on. A merge
        # DELETES the losing spellings, so a description living on one of them
        # exists only until the merge runs. Read with a default, a malformed
        # row says "this spelling describes nothing", which is precisely the
        # reading under which the text is destroyed and nothing records that
        # there was any. Every other field on the row is present, so this can
        # only be the description raising.
        with self.assertRaises(KeyError):
            cluster_tags([{"id": "1", "name": "Velvet Crane", "aliases": [],
                           "scene_count": 2},
                          {"id": "2", "name": "VelvetCrane", "aliases": [],
                           "scene_count": 1}])


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

        self.assertEqual(proposal(cluster, FOLDER, []), {
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "velvetcrane",
            "summary": "2 spellings of one tag: Velvet Crane (12 scenes), "
                       "VelvetCrane (4 scenes)",
            "payload": {
                "key": "velvetcrane",
                "members": [
                    {"id": "1", "name": "Velvet Crane", "scene_count": 12,
                     "description": None},
                    {"id": "9", "name": "VelvetCrane", "scene_count": 4,
                     "description": None},
                ],
                "canonical": {"id": "1", "name": "Velvet Crane",
                              "scene_count": 12, "description": None},
                "undecided": None,
                "counts_cover": COUNTS_COVER,
                "description": {"text": None, "from_tag": None,
                                "from_box": None, "conflicting": []},
            },
        })

    def test_the_whole_proposal_for_an_undecided_cluster(self):
        cluster = cluster_tags([tag(1, "IvyMayKingsley", scene_count=1),
                                tag(2, "Ivy MayKingsley", scene_count=2),
                                tag(3, "Ivy May Kingsley", scene_count=3)])[0]

        self.assertEqual(proposal(cluster, FOLDER, []), {
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "ivymaykingsley",
            "summary": "3 spellings of one tag: Ivy May Kingsley (3 scenes), "
                       "Ivy MayKingsley (2 scenes), IvyMayKingsley (1 scenes)",
            "payload": {
                "key": "ivymaykingsley",
                "members": [
                    {"id": "3", "name": "Ivy May Kingsley", "scene_count": 3,
                     "description": None},
                    {"id": "2", "name": "Ivy MayKingsley", "scene_count": 2,
                     "description": None},
                    {"id": "1", "name": "IvyMayKingsley", "scene_count": 1,
                     "description": None},
                ],
                "canonical": None,
                "undecided": UNDECIDED_MANY,
                "counts_cover": COUNTS_COVER,
                "description": {"text": None, "from_tag": None,
                                "from_box": None, "conflicting": []},
            },
        })

    def test_the_counts_are_the_ones_the_server_reported(self):
        # Not derived, not defaulted: the number a reviewer weighs the merge
        # by is the one that came back with the tag.
        cluster = cluster_tags([tag(1, "Velvet Crane", scene_count=1187),
                                tag(9, "VelvetCrane", scene_count=2)])[0]
        payload = proposal(cluster, FOLDER, [])["payload"]

        self.assertEqual([m["scene_count"] for m in payload["members"]],
                         [1187, 2])

    def test_it_carries_no_confidence(self):
        # The store documents `confidence` as a 0-to-1 score and enforces the
        # range. A merge is not scored, and a 1.0 here would state a
        # certainty nothing computed.
        cluster = cluster_tags([tag(1, "Velvet Crane"), tag(9, "VelvetCrane")])[0]

        self.assertNotIn("confidence", proposal(cluster, FOLDER, []))

    def test_the_sources_are_a_required_argument_with_no_default(self):
        # A merge is where a description gets destroyed, so what the survivor
        # should end up with is part of what a person is approving. Given a
        # default, a caller who simply forgot the argument builds a proposal
        # whose payload says "no source has anything to carry" about sources it
        # never asked -- indistinguishable, on the page and in the store, from
        # a pass that asked every one of them and found nothing. An empty
        # sequence is a legitimate value saying the opposite: there were none.
        cluster = cluster_tags([tag(1, "Velvet Crane"), tag(9, "VelvetCrane")])[0]

        with self.assertRaises(TypeError):
            proposal(cluster, FOLDER)


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

    def build(self, tags, boxes=(), catalogues=None, **kwargs):
        self.stash = FakeStash(tags, boxes)
        self.boxes = FakeBoxClient(catalogues or {})
        kwargs.setdefault("folder", FOLDER)
        return TagMergeProducer(self.stash, store=self.store,
                                box_client=self.boxes, **kwargs)

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

    def test_it_is_in_the_cost_class_that_rations_stash_box_reads(self):
        # It used to be `local`, when the pass read nothing but the media
        # server's own tag list. It now reads each configured stash-box's
        # WHOLE tag catalogue, which is the rate-limited resource the `box`
        # class exists to ration -- the same reads
        # `StashBoxCheckProducer` is classed for. Under `local` it would page
        # a public service unlimited and in parallel with the box check,
        # spending that budget where nothing counts it.
        self.assertEqual(self.build([]).cost, "box")
        self.assertEqual(COST_CLASS_LIMITS["box"], 1)

    def test_it_yields_one_proposal_per_cluster(self):
        proposals = self.run_pass([tag(1, "Velvet Crane", scene_count=12),
                                   tag(2, "VelvetCrane", scene_count=4),
                                   tag(3, "Copper Kettle", scene_count=7)])

        self.assertEqual([p["subject_id"] for p in proposals], ["velvetcrane"])
        self.assertEqual(proposals[0]["folder"], FOLDER)

    def test_a_library_with_no_duplicate_spellings_yields_nothing(self):
        # Counts of 5 and 3 rather than the helper's default 0: this is a claim
        # about the MERGE half, and a tag on no scenes is a finding of the
        # low-count half (see `cronicled.tag_hygiene`), which would make this
        # assertion about that half instead. Ordinary tags in ordinary use.
        self.assertEqual(self.run_pass([tag(1, "Velvet Crane", scene_count=5),
                                        tag(2, "Copper Kettle",
                                            scene_count=3)]), [])

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


class DescriptionsInTheSamePass(unittest.TestCase):
    """The other half of the one read: a description for every blank tag a
    configured source already describes, and none for any other."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def build(self, tags, boxes=(), catalogues=None, **kwargs):
        self.stash = FakeStash(tags, boxes)
        self.boxes = FakeBoxClient(catalogues or {})
        kwargs.setdefault("folder", FOLDER)
        return TagMergeProducer(self.stash, store=self.store,
                                box_client=self.boxes, **kwargs)

    def run_pass(self, tags, **kwargs):
        return list(self.build(tags, **kwargs).produce(self.ctx))

    def one_box(self, tags, box_tags, **kwargs):
        """One configured source holding `box_tags`, asked about `tags`."""
        return self.run_pass(
            tags, boxes=[box_credential("first")],
            catalogues={"https://first.invalid": catalogue(box_tags)},
            **kwargs)

    def test_the_whole_proposal_for_a_tag_a_source_describes(self):
        # The WHOLE dict. A `source_box` that silently defaulted is
        # indistinguishable from one that was set, and it is the only thing
        # that tells a reviewer anybody wrote this sentence at all.
        proposals = self.one_box([tag(7, "Lantern Work")],
                                 [box_tag("Lantern Work", LANTERN)])

        self.assertEqual(proposals, [{
            "folder": FOLDER,
            "subject_type": TAG_SUBJECT,
            "subject_id": "7",
            "summary": "Lantern Work: first has a description for this tag",
            "confidence": None,
            "payload": {"name": "Lantern Work", "field": "description",
                        "original": None, "description": LANTERN,
                        "source_box": "first"},
        }])

    def test_a_tag_no_source_describes_gets_no_proposal_at_all(self):
        # THE rule. Not a sentence composed from the name, not one summarised
        # from the scenes carrying it, not a similar tag's text -- a
        # generated description reads exactly like a written one and cannot
        # be told from it afterwards. The tag stays visibly blank.
        # `scene_count=4`, not the helper's default 0: a tag on no scenes is a
        # finding of the low-count half (`cronicled.tag_hygiene`), so the
        # default would leave this passing for a reason that has nothing to do
        # with descriptions -- and failing if that half ever changed.
        proposals = self.one_box([tag(7, "Copper Kettle", scene_count=4)],
                                 [box_tag("Lantern Work", LANTERN)])

        self.assertEqual(proposals, [])

    def test_a_tag_matched_through_the_sources_alias_is_proposed(self):
        # Most of the real coverage arrives this way. A suite using only name
        # fixtures would leave a names-only pass looking correct.
        proposals = self.one_box(
            [tag(7, "Lamplight")],
            [box_tag("Lantern Work", LANTERN, aliases=["Lamplight"])])

        self.assertEqual([p["subject_id"] for p in proposals], ["7"])
        self.assertEqual(proposals[0]["payload"]["description"], LANTERN)

    def test_a_tag_that_already_has_a_description_is_left_alone(self):
        # Somebody wrote it. A source's sentence must not displace it.
        proposals = self.one_box([tag(7, "Lantern Work", description=FERRY)],
                                 [box_tag("Lantern Work", LANTERN)])

        self.assertEqual(proposals, [])

    def test_a_tag_with_a_duplicate_spelling_is_left_to_its_merge(self):
        # A merge proposal and a description proposal for one tag reaching a
        # reviewer as two unrelated rows is how somebody approves both and
        # gets a result neither described: the merge deletes the tag the
        # description was written onto.
        proposals = self.one_box(
            [tag(7, "Lantern Work"), tag(8, "LanternWork")],
            [box_tag("Lantern Work", LANTERN)])

        self.assertEqual([p["subject_type"] for p in proposals],
                         [SUBJECT_TYPE])

    def test_the_merge_carries_what_the_source_holds_for_the_survivor(self):
        # And it arrives on the merge row rather than as a second row.
        proposals = self.one_box(
            [tag(7, "Lantern Work", scene_count=9),
             tag(8, "LanternWork", scene_count=1)],
            [box_tag("Lantern Work", LANTERN)])

        self.assertEqual(proposals[0]["payload"]["description"],
                         {"text": LANTERN, "from_tag": None,
                          "from_box": "first", "conflicting": []})

    def test_a_tag_in_a_cluster_this_run_did_not_propose_is_still_left_alone(self):
        # ANY cluster, not only one this run proposed. Here the merge is MUTED,
        # so no merge row exists and the description proposal would be the only
        # row for either tag -- which is exactly when it looks harmless. It is
        # not: the two spellings still have one unsettled identity, and a
        # description approved now attaches a definition to whichever of them
        # survives a merge somebody settles later. A mute says nobody wants to
        # be asked again; it does not say which spelling won.
        #
        # Testing this through a SELECTED cluster cannot fail: `selected` and
        # `clusters` hold the same thing when nothing has been muted or
        # proposed, so the two readings are indistinguishable there.
        self.store.mute(SUBJECT_TYPE, "lanternwork")

        proposals = self.one_box(
            [tag(7, "Lantern Work"), tag(8, "LanternWork")],
            [box_tag("Lantern Work", LANTERN)])

        self.assertEqual(proposals, [])

    def test_the_closing_line_reports_keys_dropped_as_ambiguous(self):
        # A source claiming one key with two different descriptions has them
        # dropped rather than resolved, and the count is the signal that a
        # source's aliases have stopped being usable as keys. Logged only where
        # it is found it reaches nobody: `JobRunner._log` keeps ONE field, so
        # every line written during the read is overwritten before the job
        # finishes -- which is why the total travels to the closing line.
        #
        # ONE collision in the first source and TWO in the second, so the
        # expected 3 is a number no simpler rule reaches. A total that added
        # one per source would say 2, and one that assigned rather than
        # accumulated would say 2 as well -- the second source's count,
        # overwriting the first. A fixture with a single collision in a single
        # source cannot separate any of the three.
        self.run_pass(
            [tag(7, "Pewter Hinge")],
            boxes=[box_credential("first"), box_credential("second")],
            catalogues={
                "https://first.invalid": catalogue([
                    box_tag("Lantern Work", LANTERN, aliases=["Brass Ferry"]),
                    box_tag("Brass Ferry", FERRY)]),
                "https://second.invalid": catalogue([
                    box_tag("Copper Kettle", LANTERN,
                            aliases=["Slate Harbour"]),
                    box_tag("Slate Harbour", FERRY),
                    box_tag("Amber Quill", LANTERN, aliases=["Ivory Latch"]),
                    box_tag("Ivory Latch", FERRY)])})

        self.assertIn("3 source alias key(s) dropped as ambiguous",
                      self.ctx.message)

    def test_a_source_with_no_colliding_aliases_reports_no_dropped_keys(self):
        # The other half. A permanent "0 dropped" on every run is noise, and
        # noise in the one line a finished job keeps is what stops the line
        # being read at all.
        self.one_box([tag(7, "Copper Kettle")],
                     [box_tag("Lantern Work", LANTERN)])

        self.assertNotIn("dropped as ambiguous", self.ctx.message)

    def test_every_tag_is_accounted_for_by_one_of_the_four_reasons(self):
        # The identity `total == described + clustered + outstanding +
        # beyond_reach`, asserted against the counts the PASS built from a real
        # library rather than against a hand-made `Counts`. A dataclass
        # satisfying the identity for numbers a test chose says nothing about
        # whether a tag can fall out of the pass for a reason nobody named --
        # and a tag that vanished silently is the failure no per-field check
        # can see. All four reasons are represented here, so the sum is not
        # reachable by accident.
        tags = [tag(7, "Lantern Work"),                    # outstanding
                tag(8, "Copper Kettle"),                   # beyond reach
                tag(9, "Brass Ferry", description=FERRY),  # already described
                tag(1, "Velvet Crane", scene_count=2),     # clustered
                tag(2, "VelvetCrane", scene_count=1)]      # clustered
        producer = self.build(
            tags, boxes=[box_credential("first")],
            catalogues={"https://first.invalid":
                        catalogue([box_tag("Lantern Work", LANTERN)])})
        indexes, unread, _ = producer._indexes(self.ctx)

        _, counts = producer._describe(tags, cluster_tags(tags), indexes,
                                       unread)

        self.assertEqual(counts.total, len(tags))
        self.assertEqual(counts.total,
                         counts.described + counts.clustered
                         + counts.outstanding + counts.beyond_reach)
        self.assertEqual((counts.described, counts.clustered,
                          counts.outstanding, counts.beyond_reach),
                         (1, 2, 1, 1))


class WhichSourceThePassAsks(unittest.TestCase):
    """Configured order, first hit wins, read off the media server's own
    list."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def run_with(self, credentials):
        # ASYMMETRIC on purpose: the two sources describe the shared tag
        # differently, and only the second holds "Slate Harbour". Reversing
        # them changes both the text and the named source, and changes which
        # tags are covered at all -- a symmetric fixture could not detect an
        # order mutation.
        catalogues = {
            "https://first.invalid": catalogue(
                [box_tag("Lantern Work", LANTERN)]),
            "https://second.invalid": catalogue(
                [box_tag("Lantern Work", FERRY),
                 box_tag("Slate Harbour", "Shot on a stone quay.")]),
        }
        stash = FakeStash([tag(7, "Lantern Work"), tag(8, "Slate Harbour")],
                          credentials)
        producer = TagMergeProducer(stash, store=self.store, folder=FOLDER,
                                    box_client=FakeBoxClient(catalogues))
        return {p["subject_id"]: p["payload"] for p in producer.produce(self.ctx)}

    def test_the_first_configured_source_wins(self):
        payloads = self.run_with([box_credential("first"),
                                  box_credential("second")])

        self.assertEqual(payloads["7"]["description"], LANTERN)
        self.assertEqual(payloads["7"]["source_box"], "first")

    def test_reversing_the_configured_order_reverses_the_answer(self):
        # The other half. Without it, "first wins" and "last wins" both
        # satisfy the assertion above.
        payloads = self.run_with([box_credential("second"),
                                  box_credential("first")])

        self.assertEqual(payloads["7"]["description"], FERRY)
        self.assertEqual(payloads["7"]["source_box"], "second")

    def test_a_later_source_answers_what_the_first_has_nothing_for(self):
        payloads = self.run_with([box_credential("first"),
                                  box_credential("second")])

        self.assertEqual(payloads["8"]["source_box"], "second")

    def test_each_source_is_asked_at_its_own_address_with_its_own_key(self):
        # The media server stores a box's address as the GraphQL endpoint
        # itself; the client appends `/graphql` of its own. Handed one
        # straight to the other, every configured source is asked for
        # `/graphql/graphql` and silently contributes nothing.
        stash = FakeStash([], [box_credential("first"),
                               box_credential("second")])
        boxes = FakeBoxClient({
            "https://first.invalid": catalogue([]),
            "https://second.invalid": catalogue([]),
        })
        list(TagMergeProducer(stash, store=self.store, folder=FOLDER,
                              box_client=boxes).produce(self.ctx))

        self.assertEqual(boxes.asked, [("https://first.invalid", "key-first"),
                                       ("https://second.invalid", "key-second")])


class WhenASourceCannotBeRead(unittest.TestCase):
    """A source failing is evidence about the network, never about a tag."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def run_with(self, first_answer, tags=None):
        catalogues = {
            "https://first.invalid": first_answer,
            "https://second.invalid": catalogue(
                [box_tag("Slate Harbour", "Shot on a stone quay.")]),
        }
        if tags is None:
            tags = [tag(7, "Lantern Work"), tag(8, "Slate Harbour"),
                    tag(1, "Velvet Crane", scene_count=2),
                    tag(2, "VelvetCrane", scene_count=1)]
        stash = FakeStash(tags, [box_credential("first"),
                                 box_credential("second")])
        producer = TagMergeProducer(stash, store=self.store, folder=FOLDER,
                                    box_client=FakeBoxClient(catalogues))
        return list(producer.produce(self.ctx))

    def test_the_other_sources_answers_survive_it(self):
        proposals = self.run_with(StashError("host wedged", transient=True))

        self.assertIn("8", [p["subject_id"] for p in proposals
                            if p["subject_type"] == TAG_SUBJECT])

    def test_the_rest_of_the_pass_survives_it(self):
        # The merges come from the media server's own read and have nothing
        # to do with any source; losing them to a box being down would be a
        # network blip taking out work that never needed the network.
        proposals = self.run_with(StashError("host wedged", transient=True))

        self.assertEqual([p["subject_id"] for p in proposals
                          if p["subject_type"] == SUBJECT_TYPE],
                         ["velvetcrane"])

    def test_it_is_never_reported_as_no_source_describing_the_tag(self):
        # The harm: a night of network trouble reporting itself as a
        # permanent backlog, and getting planned around.
        self.run_with(StashError("host wedged", transient=True))

        self.assertIn("could not be read in full", self.ctx.message)
        self.assertNotIn("no configured source describes", self.ctx.message)

    def test_every_source_answering_says_so_in_the_other_words(self):
        # The two wordings are deliberately not one sentence with a number
        # changed: "no configured source describes them" is a claim about the
        # sources, and it has been established here and not above.
        self.run_with(catalogue([box_tag("Lantern Work", LANTERN)]))

        self.assertIn("no configured source describes", self.ctx.message)
        self.assertNotIn("could not be read in full", self.ctx.message)

    def test_a_partly_read_source_counts_the_same_as_one_that_failed(self):
        # The page that was not read is exactly where the missing description
        # would be.
        self.run_with(catalogue([box_tag("Lantern Work", LANTERN)],
                                complete=False))

        self.assertIn("could not be read in full", self.ctx.message)

    def test_a_description_found_in_a_partly_read_source_is_still_found(self):
        # A page that was never fetched cannot un-find a tag already in hand.
        proposals = self.run_with(
            catalogue([box_tag("Lantern Work", LANTERN)], complete=False))

        self.assertIn("7", [p["subject_id"] for p in proposals
                            if p["subject_type"] == TAG_SUBJECT])


class TheBacklogFigure(unittest.TestCase):
    """The count this reports as outstanding, and what it leaves out."""

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()

    def run_pass(self, tags, box_tags):
        stash = FakeStash(tags, [box_credential("first")])
        boxes = FakeBoxClient({"https://first.invalid": catalogue(box_tags)})
        return list(TagMergeProducer(stash, store=self.store, folder=FOLDER,
                                     box_client=boxes).produce(self.ctx))

    def test_it_counts_only_the_tags_a_source_can_actually_describe(self):
        # Three of the four are undescribed; one of those three is all this
        # can ever help with. A backlog figure that said 3 would describe
        # work no amount of running this will clear, and a number that cannot
        # go down stops being read.
        self.run_pass([tag(7, "Lantern Work"),
                       tag(8, "Copper Kettle"),
                       tag(9, "Slate Harbour"),
                       tag(10, "Brass Ferry", description=FERRY)],
                      [box_tag("Lantern Work", LANTERN)])

        self.assertIn("1 descriptions proposed", self.ctx.message)
        self.assertIn("2 no configured source describes", self.ctx.message)
        self.assertIn("1 tags already described", self.ctx.message)

    def test_the_closing_line_carries_both_halves_of_the_pass(self):
        # A finished job keeps ONE message, so the merges and the
        # descriptions have to be in the same sentence or one of them is lost
        # the moment the other is logged.
        self.run_pass([tag(1, "Velvet Crane", scene_count=2),
                       tag(2, "VelvetCrane", scene_count=1),
                       tag(7, "Lantern Work")],
                      [box_tag("Lantern Work", LANTERN)])

        self.assertIn("1 clusters", self.ctx.message)
        self.assertIn("1 descriptions proposed", self.ctx.message)
        self.assertIn("2 left to their merge", self.ctx.message)


if __name__ == "__main__":
    unittest.main()
