"""Choosing the batch a scan works on, and working one file of it.

`SelectTest` is about *ordering*: narrowing runs before the limit. The scarce
resource is a network lookup per selected file, so a limit spent on a file
that was never going to be proposed is a scan that appears to run and
achieves nothing.

`ExamineTest` is about the three different kinds of "no proposal", which is
the whole reason that function exists as its own step. A file that is
genuinely unidentifiable is muted so it stops consuming a budget every run;
a file whose scoring was ambiguous is NOT, because a tie means a human should
look; a file whose lookup raised is NOT either, because that is evidence
about the network rather than about the file. Conflating any two of them is a
bug a user feels — either a file hidden forever because a socket blipped
once, or a scan that re-decides the same hopeless file every night.

No test touches a real filesystem and no test opens a socket. A path is a
string; a scene is a dict; the store is opened in memory; the search callable
is a fake that answers from a script and records what it was asked.
"""
import unittest

from cronicled.scan import (
    Counts, MAX_RUNNERS_UP, MUTE_NO_CANDIDATES, MUTE_UNRESOLVED_CREATOR,
    Outcome, SUBJECT_TYPE, examine, select,
)
from cronicled.store import Store

FOLDER = "library"


def scene(scene_id, *paths):
    """A scene as the media server returns it, trimmed to what selection reads.

    The extra keys are kept so the fixtures look like the real payload rather
    than like the two fields this module happens to use.
    """
    return {
        "id": str(scene_id),
        "title": None,
        "date": None,
        "files": [{"basename": p.rsplit("/", 1)[-1], "path": p} for p in paths],
        "studio": None,
        "performers": [],
        "tags": [],
    }


class SelectTest(unittest.TestCase):

    def setUp(self):
        # ":memory:" keeps the store off the filesystem entirely. It still
        # takes a slot in the store's open-path registry, so it must be
        # closed or the next test's Store() raises.
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def propose(self, scene_id, folder=FOLDER):
        """Put a visible proposal in the store for this subject."""
        return self.store.record(
            folder=folder, subject_type=SUBJECT_TYPE, subject_id=str(scene_id),
            summary="a proposal", payload={"title": "something"},
            producer="test-producer",
        )

    # -- the rule this module exists for --------------------------------- #

    def test_the_filter_narrows_before_the_limit_applies(self):
        """A filter plus a limit yields `limit` files MATCHING the filter.

        The two matching files sit last in the input on purpose: taking the
        first `limit` files and filtering afterwards selects nothing at all,
        which is the failure this test exists to catch.
        """
        scenes = [
            scene(1, "/library/beta/one.mp4"),
            scene(2, "/library/beta/two.mp4"),
            scene(3, "/library/beta/three.mp4"),
            scene(4, "/library/alpha/four.mp4"),
            scene(5, "/library/alpha/five.mp4"),
        ]

        selected, counts = select(
            scenes, store=self.store, folder=FOLDER,
            name_filter="alpha", limit=2,
        )

        self.assertEqual([s["id"] for s in selected], ["4", "5"])
        self.assertEqual(counts, Counts(
            total=5, already_proposed=0, muted=0, filtered_out=3, selected=2,
            deferred=0))

    def test_an_already_proposed_file_is_dropped_before_the_limit(self):
        """A second run's budget goes to fresh files, not to re-deciding the
        file the first run already proposed."""
        self.propose(1)
        scenes = [scene(1, "/library/one.mp4"),
                  scene(2, "/library/two.mp4"),
                  scene(3, "/library/three.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  limit=2)

        self.assertEqual([s["id"] for s in selected], ["2", "3"])
        self.assertEqual(counts, Counts(
            total=3, already_proposed=1, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_a_muted_subject_is_dropped_before_the_limit(self):
        """Same budget argument as an already-proposed file: the reviewer has
        already said no, so a lookup for it buys nothing."""
        self.propose(1)
        self.store.mute(SUBJECT_TYPE, "1")
        scenes = [scene(1, "/library/one.mp4"),
                  scene(2, "/library/two.mp4"),
                  scene(3, "/library/three.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  limit=2)

        self.assertEqual([s["id"] for s in selected], ["2", "3"])
        self.assertEqual(counts, Counts(
            total=3, already_proposed=0, muted=1, filtered_out=0, selected=2,
            deferred=0))

    def test_a_pre_emptively_muted_subject_is_dropped(self):
        """A subject muted BEFORE any proposal existed has no `item` row, so
        the mute is invisible in `items(state="muted")` — selection has to ask
        the `mute` table the store itself consults.

        Without that, this file survives selection, buys a network lookup, and
        `record()` then refuses the proposal it produced: the wasted budget
        this module exists to prevent, in its quietest form.
        """
        self.store.mute(SUBJECT_TYPE, "1")   # no proposal was ever recorded
        scenes = [scene(1, "/library/one.mp4"),
                  scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER)

        self.assertEqual([s["id"] for s in selected], ["2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=1, filtered_out=0, selected=1,
            deferred=0))

    def test_a_muted_subject_is_not_also_counted_as_already_proposed(self):
        """Muting a proposed subject moves its row out of the visible view, so
        the same file must not be counted under two reasons at once."""
        self.propose(1)
        self.store.mute(SUBJECT_TYPE, "1")

        _, counts = select([scene(1, "/library/one.mp4")],
                           store=self.store, folder=FOLDER)

        self.assertEqual(counts, Counts(
            total=1, already_proposed=0, muted=1, filtered_out=0, selected=0,
            deferred=0))

    # -- the counts account for every file ------------------------------- #

    def assertAccountsForEveryFile(self, counts):
        """`total` is the sum of the five outcomes.

        One assertion that catches a file dropped for a reason nobody named —
        which is the failure a per-field assertion cannot see, because it can
        only check the reasons it already knows to look for.
        """
        self.assertEqual(
            counts.total,
            counts.already_proposed + counts.muted + counts.filtered_out
            + counts.selected + counts.deferred)

    def test_the_counts_explain_every_file_when_no_limit_binds(self):
        self.propose(2)
        self.propose(3)
        self.store.mute(SUBJECT_TYPE, "3")
        scenes = [
            scene(1, "/library/alpha/one.mp4"),      # selected
            scene(2, "/library/alpha/two.mp4"),      # already proposed
            scene(3, "/library/alpha/three.mp4"),    # muted
            scene(4, "/library/beta/four.mp4"),      # filtered out
            scene(5, "/library/alpha/five.mp4"),     # selected
        ]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  name_filter="alpha")

        self.assertEqual([s["id"] for s in selected], ["1", "5"])
        self.assertEqual(counts, Counts(
            total=5, already_proposed=1, muted=1, filtered_out=1, selected=2,
            deferred=0))
        self.assertAccountsForEveryFile(counts)

    def test_the_counts_explain_every_file_when_the_limit_binds_too(self):
        """The files a binding limit cut are `deferred` — nothing was decided
        against them, they simply did not fit this budget. Without that field
        they belong to no outcome and the identity silently stops holding
        exactly when a scan is busiest."""
        scenes = [scene(i, "/library/%d.mp4" % i) for i in range(1, 6)]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  limit=2)

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=5, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=3))
        self.assertAccountsForEveryFile(counts)

    def test_a_file_dropped_by_a_reason_is_not_also_deferred(self):
        """`deferred` counts only files that survived every narrowing step and
        were then cut by the limit. Counting the dropped ones too — deriving
        it from `total` rather than from the narrowed set — would double-count
        them and break the identity it exists to hold."""
        self.propose(1)
        self.store.mute(SUBJECT_TYPE, "2")
        scenes = [
            scene(1, "/library/alpha/one.mp4"),      # already proposed
            scene(2, "/library/alpha/two.mp4"),      # muted
            scene(3, "/library/beta/three.mp4"),     # filtered out
            scene(4, "/library/alpha/four.mp4"),     # selected
            scene(5, "/library/alpha/five.mp4"),     # deferred
        ]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  name_filter="alpha", limit=1)

        self.assertEqual([s["id"] for s in selected], ["4"])
        self.assertEqual(counts, Counts(
            total=5, already_proposed=1, muted=1, filtered_out=1, selected=1,
            deferred=1))
        self.assertAccountsForEveryFile(counts)

    def test_a_file_outside_the_filter_counts_only_as_filtered_out(self):
        """Precedence is filter, then muted, then already proposed: a file the
        user's filter excludes is not in this scan's scope at all, so
        reporting it as muted would describe a decision that never applied."""
        self.propose(1)
        self.store.mute(SUBJECT_TYPE, "1")
        self.propose(2)

        selected, counts = select(
            [scene(1, "/library/beta/one.mp4"), scene(2, "/library/beta/two.mp4"),
             scene(3, "/library/alpha/three.mp4")],
            store=self.store, folder=FOLDER, name_filter="alpha")

        self.assertEqual([s["id"] for s in selected], ["3"])
        self.assertEqual(counts, Counts(
            total=3, already_proposed=0, muted=0, filtered_out=2, selected=1,
            deferred=0))

    # -- the filter ------------------------------------------------------- #

    def test_an_empty_filter_matches_everything(self):
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  name_filter="")

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_no_filter_matches_everything(self):
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  name_filter=None)

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_the_filter_matches_an_ancestor_directory_case_insensitively(self):
        """Matching the whole path, not just the name, is what lets a pattern
        naming a directory scope a scan to that subtree."""
        scenes = [scene(1, "/library/Cohort-Alpha/2020/one.mp4"),
                  scene(2, "/library/cohort-beta/2020/alpha.mp4"),
                  scene(3, "/library/cohort-beta/2020/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  name_filter="COHORT-alpha")

        self.assertEqual([s["id"] for s in selected], ["1"])
        self.assertEqual(counts, Counts(
            total=3, already_proposed=0, muted=0, filtered_out=2, selected=1,
            deferred=0))

    def test_any_of_a_scenes_files_may_match_the_filter(self):
        scenes = [scene(1, "/library/beta/one.mp4", "/library/alpha/one.mkv")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  name_filter="alpha")

        self.assertEqual([s["id"] for s in selected], ["1"])
        self.assertEqual(counts, Counts(
            total=1, already_proposed=0, muted=0, filtered_out=0, selected=1,
            deferred=0))

    def test_a_scene_with_no_files_cannot_match_a_filter_but_survives_without_one(self):
        no_files = scene(1)

        filtered, filtered_counts = select([no_files], store=self.store,
                                           folder=FOLDER, name_filter="alpha")
        kept, kept_counts = select([no_files], store=self.store, folder=FOLDER)

        self.assertEqual(filtered, [])
        self.assertEqual(filtered_counts, Counts(
            total=1, already_proposed=0, muted=0, filtered_out=1, selected=0,
            deferred=0))
        self.assertEqual([s["id"] for s in kept], ["1"])
        self.assertEqual(kept_counts, Counts(
            total=1, already_proposed=0, muted=0, filtered_out=0, selected=1,
            deferred=0))

    # -- the limit -------------------------------------------------------- #

    def test_a_limit_larger_than_the_set_returns_the_whole_set(self):
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  limit=10)

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_a_limit_equal_to_the_set_returns_the_whole_set(self):
        """The accepting side of the boundary. A limit that drifts one file
        too strict is as wrong as one too loose, and quieter."""
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  limit=2)

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_a_limit_of_one_takes_exactly_the_first_file(self):
        """The restrictive side of the same boundary, and the order in which
        the batch is taken."""
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER,
                                  limit=1)

        self.assertEqual([s["id"] for s in selected], ["1"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=1,
            deferred=1))

    def test_a_limit_of_zero_selects_nothing_and_differs_from_no_limit(self):
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        zero, zero_counts = select(scenes, store=self.store, folder=FOLDER,
                                   limit=0)
        unlimited, unlimited_counts = select(scenes, store=self.store,
                                             folder=FOLDER, limit=None)

        self.assertEqual(zero, [])
        # `limit=0` defers both files rather than deciding against either:
        # the instruction was "no budget this run", not "these are unwanted".
        self.assertEqual(zero_counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=0,
            deferred=2))
        self.assertEqual([s["id"] for s in unlimited], ["1", "2"])
        self.assertEqual(unlimited_counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_a_negative_limit_is_refused(self):
        """`scenes[:-1]` silently drops the LAST file instead of selecting
        none, so a negative limit must raise rather than be handed to a
        slice."""
        with self.assertRaises(ValueError):
            select([scene(1, "/library/one.mp4")], store=self.store,
                   folder=FOLDER, limit=-1)

    # -- scope and malformed input ---------------------------------------- #

    def test_a_proposal_in_another_folder_does_not_suppress_this_scan(self):
        self.propose(1, folder="another-folder")

        selected, counts = select([scene(1, "/library/one.mp4")],
                                  store=self.store, folder=FOLDER)

        self.assertEqual([s["id"] for s in selected], ["1"])
        self.assertEqual(counts, Counts(
            total=1, already_proposed=0, muted=0, filtered_out=0, selected=1,
            deferred=0))

    def test_a_mute_recorded_under_another_folder_still_suppresses(self):
        """A mute is keyed by subject, not by folder — the store blocks it
        globally, so selection must not spend a lookup on it either."""
        self.propose(1, folder="another-folder")
        self.store.mute(SUBJECT_TYPE, "1")

        selected, counts = select([scene(1, "/library/one.mp4")],
                                  store=self.store, folder=FOLDER)

        self.assertEqual(selected, [])
        self.assertEqual(counts, Counts(
            total=1, already_proposed=0, muted=1, filtered_out=0, selected=0,
            deferred=0))

    def test_a_proposal_about_a_different_kind_of_subject_is_ignored(self):
        """Subject ids are only unique within a subject type, so a proposal
        (or a mute) about performer "1" must not suppress scene "1" — that
        would silently starve a scan of files nothing was ever decided about.
        """
        self.store.record(
            folder=FOLDER, subject_type="performer", subject_id="1",
            summary="a proposal", payload={"name": "someone"},
            producer="test-producer")
        self.store.record(
            folder=FOLDER, subject_type="performer", subject_id="2",
            summary="a proposal", payload={"name": "someone else"},
            producer="test-producer")
        self.store.mute("performer", "2")

        selected, counts = select(
            [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")],
            store=self.store, folder=FOLDER)

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_a_scene_without_an_id_raises(self):
        with self.assertRaises(KeyError):
            select([{"files": []}], store=self.store, folder=FOLDER)

    def test_a_scene_without_a_files_key_raises(self):
        """A missing field is a malformed scene, not an empty one: defaulting
        it would let a broken payload flow into the batch looking healthy."""
        with self.assertRaises(KeyError):
            select([{"id": "1"}], store=self.store, folder=FOLDER)


def candidate(title, slug):
    """A catalogue result as the injected search hands one back.

    Kept as a dict with more than the one field scoring reads, because
    `examine` records the candidate WHOLE — it does not know which of a
    store's fields an applier will need, so it must not project.
    """
    return {"title": title, "url": "https://example.invalid/clip/" + slug}


class FakeSearch:
    """The injected lookup: answers from a script, and remembers the queries.

    Remembering them is not decoration. The queries are the scarce resource
    the whole design rations, so "was this looked up at all" is a property
    worth asserting — a file whose creator never resolved must not buy one.
    """

    def __init__(self, results=(), raises=None):
        self._results = list(results)
        self._raises = raises
        self.queries = []

    def __call__(self, query):
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises
        return list(self._results)


class ExamineTest(unittest.TestCase):

    MORNING = candidate("Morning Ritual", "morning-ritual")
    EVENING = candidate("Evening Errand", "evening-errand")

    def run_examine(self, path, results=(), raises=None, threshold=0.5,
                    aliases=None, scene_id=1):
        search = FakeSearch(results, raises)
        outcome = examine(scene(scene_id, path), search=search, folder=FOLDER,
                          threshold=threshold, aliases=aliases)
        return outcome, search

    # -- a file that decides -------------------------------------------- #

    def test_a_decided_file_yields_a_proposal_with_its_score_and_runners_up(self):
        """The whole proposal is asserted as one shape rather than field by
        field: a field-by-field check cannot notice a field that was ADDED,
        and every key here reaches a store that hashes the payload."""
        outcome, search = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4",
            results=[self.MORNING, self.EVENING])

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(outcome, Outcome(
            proposal={
                "folder": FOLDER,
                "subject_type": SUBJECT_TYPE,
                "subject_id": "1",
                "summary": 'Morning Ritual.mp4 -> "Morning Ritual" '
                           'by Velvet Crane (score 1.000)',
                "confidence": 1.0,
                "payload": {
                    "path": "/library/Velvet Crane/Morning Ritual.mp4",
                    "creator": {"name": "Velvet Crane", "source": "folder",
                                "competing": None, "rejected_folder": None},
                    "candidate": self.MORNING,
                    "score": 1.0,
                    "runners_up": [{"candidate": self.EVENING, "score": 0.15}],
                },
            },
            mute_reason=None,
            error=None,
            reason="chosen with score 1.000",
        ))

    def test_the_summary_is_one_line_naming_the_file_and_the_conclusion(self):
        """A reviewer reads this line and nothing else before deciding, so it
        has to say which file, which candidate, and how confident — a summary
        that names only the candidate cannot be judged without opening the
        payload."""
        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Harbour Lights.mp4",
            results=[self.MORNING, self.EVENING], threshold=0.1)

        self.assertEqual(
            outcome.proposal["summary"],
            'Harbour Lights.mp4 -> "Evening Errand" by Velvet Crane '
            '(score 0.138)')

    def test_the_runners_up_are_the_highest_scoring_losers_in_order(self):
        """Ordered by score and capped, so the payload stays reviewable and
        stops depending on the order the catalogue happened to return."""
        extras = [candidate("Harbour Lights", "harbour-lights"),
                  candidate("Winter Ledger", "winter-ledger"),
                  candidate("Morning Errand", "morning-errand")]
        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4",
            results=[self.MORNING, self.EVENING] + extras)

        self.assertEqual(outcome.proposal["payload"]["runners_up"], [
            {"candidate": extras[2], "score": 0.564},
            {"candidate": self.EVENING, "score": 0.15},
            {"candidate": extras[0], "score": 0.107},
        ])
        self.assertEqual(MAX_RUNNERS_UP, 3)

    # -- the resolver's disagreement, which currently goes nowhere -------- #

    def test_a_folder_and_a_filename_naming_different_creators_reach_the_payload(self):
        """The folder wins, and the name it beat is recorded. A library where
        this shows up often is one whose filing convention is not what the
        operator assumed, and that is the most useful signal the resolver
        produces — dropping it makes the mis-filing invisible."""
        outcome, search = self.run_examine(
            "/library/Velvet Crane/Ivy Kingsley - Morning Ritual.mp4",
            results=[self.MORNING, self.EVENING])

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(outcome.proposal["payload"]["creator"], {
            "name": "Velvet Crane", "source": "folder",
            "competing": "Ivy Kingsley", "rejected_folder": None,
        })

    def test_a_folder_the_guards_threw_out_reaches_the_payload_too(self):
        """A filename-sourced attribution with an empty `rejected_folder`
        reads as "the folder had nothing to say". Here the folder said
        something and the guards refused it, which is a different fact and the
        one that explains the attribution."""
        outcome, _ = self.run_examine(
            "/lib/2020-05-04/Ivy Kingsley - Morning Ritual.mp4",
            results=[self.MORNING, self.EVENING])

        self.assertEqual(outcome.proposal["payload"]["creator"], {
            "name": "Ivy Kingsley", "source": "filename",
            "competing": None, "rejected_folder": "2020-05-04",
        })

    # -- the first kind of "no proposal": the file is unidentifiable ------ #

    def test_no_candidates_mutes_and_says_so(self):
        outcome, search = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4", results=[])

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(outcome, Outcome(
            proposal=None, mute_reason=MUTE_NO_CANDIDATES, error=None,
            reason=MUTE_NO_CANDIDATES))

    def test_an_unresolved_creator_mutes_and_buys_no_lookup(self):
        """Nothing names a creator, so there is no query to phrase — and the
        budget this whole module rations must not be spent finding that out."""
        outcome, search = self.run_examine("/lib/2020-05-04/clip one.mp4",
                                           results=[self.MORNING])

        self.assertEqual(search.queries, [])
        self.assertEqual(outcome, Outcome(
            proposal=None, mute_reason=MUTE_UNRESOLVED_CREATOR, error=None,
            reason=MUTE_UNRESOLVED_CREATOR))

    def test_the_two_mute_reasons_are_different_text(self):
        """They mean different things to whoever reads them later — one says
        the catalogue has nothing, the other says the library's own layout
        names nobody — and only one of them is fixed by an alias. A single
        catch-all reason would satisfy both tests above and lose that."""
        self.assertNotEqual(MUTE_NO_CANDIDATES, MUTE_UNRESOLVED_CREATOR)

    # -- the second kind: a human should look ----------------------------- #

    def test_an_ambiguous_decision_yields_no_proposal_and_no_mute(self):
        """Two candidates too close to call means the file is one glance from
        being resolved. Muting it would silently hide it forever."""
        dawn = candidate("Morning Ritual Dawn", "morning-ritual-dawn")
        dusk = candidate("Morning Ritual Dusk", "morning-ritual-dusk")

        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4", results=[dawn, dusk])

        self.assertEqual(outcome, Outcome(
            proposal=None, mute_reason=None, error=None,
            reason="ambiguous: 0.955 vs 0.955 are too close to call"))

    def test_nothing_above_the_threshold_yields_no_proposal_and_no_mute(self):
        """The catalogue had candidates and none was good enough. The lever
        that would fix that is the threshold, which is a decision a human
        makes — so this is a refusal, not a verdict that the file can never
        be identified."""
        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Harbour Lights.mp4",
            results=[self.MORNING, self.EVENING])

        self.assertEqual(outcome, Outcome(
            proposal=None, mute_reason=None, error=None,
            reason="nothing above the threshold (0.50); best score was 0.138"))

    # -- the third kind: the network, not the file ------------------------ #

    def test_a_search_that_raises_yields_an_error_and_no_mute(self):
        """A transient failure is not evidence about the file. Muting here
        would hide it permanently because a socket blipped once."""
        outcome, search = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4",
            raises=RuntimeError("connection reset"))

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(outcome, Outcome(
            proposal=None, mute_reason=None,
            error="RuntimeError: connection reset",
            reason="RuntimeError: connection reset"))

    def test_the_error_names_the_exception_type(self):
        """`str(exc)` alone is the empty string for a bare `raise`, which
        reports a failure a user cannot tell from a quiet success."""
        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4", raises=TimeoutError())

        self.assertEqual(outcome.error, "TimeoutError: ")

    def test_a_malformed_alias_map_is_not_swallowed_as_a_per_file_error(self):
        """A duplicated alias line is a wiring mistake in the operator's
        config, wrong for every file, and it must fail where it was made
        rather than be reported as this one file's transient trouble."""
        with self.assertRaises(ValueError):
            self.run_examine("/library/VC/Morning Ritual.mp4",
                             results=[self.MORNING],
                             aliases={"VC": "Velvet Crane", "v c": "Ivy Kingsley"})

    # -- the ordering that makes the zero-evidence rule reachable --------- #

    def test_a_file_named_only_after_its_creator_is_not_proposed(self):
        """The creator is resolved BEFORE scoring, so `score(artist=)` can
        subtract the creator's tokens from the evidence. Score first and the
        evidence still contains the creator's name: this file then scores
        0.900 by containment on that name alone and takes on the metadata of
        whichever of the creator's titles happened to be offered."""
        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Velvet Crane.mp4",
            results=[candidate("Velvet Crane Morning Ritual", "vcmr")])

        self.assertIsNone(outcome.proposal)
        self.assertIsNone(outcome.mute_reason)
        self.assertEqual(
            outcome.reason,
            "nothing to match on (meaningful_count=0): the name carries no "
            "word that is not the artist's or generic")

    # -- what the caller's knobs actually reach ---------------------------- #

    def test_the_threshold_reaches_the_decision(self):
        """The same file, refused at one threshold and proposed at another —
        so a hard-coded threshold cannot pass both halves."""
        path = "/library/Velvet Crane/Harbour Lights.mp4"
        results = [self.MORNING, self.EVENING]

        refused, _ = self.run_examine(path, results=results, threshold=0.5)
        proposed, _ = self.run_examine(path, results=results, threshold=0.1)

        self.assertIsNone(refused.proposal)
        self.assertEqual(proposed.proposal["payload"]["candidate"],
                         self.EVENING)

    def test_the_aliases_reach_the_resolver(self):
        """An as-filed folder the guards would refuse ("VC" is two characters)
        resolves once the operator has declared what it stands for, and the
        query goes out under the full name."""
        path = "/library/VC/Morning Ritual.mp4"

        without, no_search = self.run_examine(path, results=[self.MORNING])
        with_alias, search = self.run_examine(
            path, results=[self.MORNING], aliases={"VC": "Velvet Crane"})

        self.assertEqual(no_search.queries, [])
        self.assertEqual(without.mute_reason, MUTE_UNRESOLVED_CREATOR)
        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(with_alias.proposal["payload"]["creator"], {
            "name": "Velvet Crane", "source": "alias",
            "competing": None, "rejected_folder": None,
        })

    def test_the_file_is_judged_by_its_path_not_by_the_reported_basename(self):
        """A scene carries both a `path` and a `basename`, and a rename can
        leave them disagreeing. The creator's directory can only come from the
        path, so the name must too: read one from each and the attribution and
        the evidence describe two different files, silently."""
        stale = {"id": "1",
                 "files": [{"basename": "Evening Errand.mp4",
                            "path": "/library/Velvet Crane/Morning Ritual.mp4"}]}

        outcome = examine(stale, search=FakeSearch([self.MORNING, self.EVENING]),
                          folder=FOLDER, threshold=0.5, aliases=None)

        self.assertEqual(outcome.proposal["payload"]["candidate"], self.MORNING)
        self.assertEqual(
            outcome.proposal["summary"],
            'Morning Ritual.mp4 -> "Morning Ritual" by Velvet Crane '
            '(score 1.000)')

    # -- malformed input fails loudly ------------------------------------- #

    def test_a_scene_with_no_file_raises_rather_than_muting(self):
        """A scene with no file has no name and no folder, so every guard
        declines it and it would mute — a malformed record quietly becoming
        "never show me this again". Raise instead: the producer isolates one
        file's failure, and a visible error is recoverable where a mute is
        not."""
        search = FakeSearch([self.MORNING])
        with self.assertRaises(ValueError):
            examine(scene(1), search=search, folder=FOLDER, threshold=0.5,
                    aliases=None)
        self.assertEqual(search.queries, [])

    def test_a_scene_without_a_files_key_raises(self):
        with self.assertRaises(KeyError):
            examine({"id": "1"}, search=FakeSearch(), folder=FOLDER,
                    threshold=0.5, aliases=None)

    def test_a_candidate_without_a_title_raises(self):
        """A candidate with no title cannot be scored. Defaulting it to the
        empty string scores it against every file in the library instead."""
        with self.assertRaises(KeyError):
            self.run_examine("/library/Velvet Crane/Morning Ritual.mp4",
                             results=[{"url": "https://example.invalid/x"}])


if __name__ == "__main__":
    unittest.main()
