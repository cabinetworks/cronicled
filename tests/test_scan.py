"""Choosing the batch a scan works on, working one file of it, and running a
whole batch of them.

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

`SingleFlightTest` and `ScanProducerTest` are about composition and about the two properties that only
exist once several files are worked at once: identical queries collapse to one
lookup, and a proposal is yielded the moment it is ready rather than at the end
of the batch. Both protect the same thing — the lookups a run spends, and the
work a run keeps when it dies partway.

No test touches a real filesystem and no test opens a socket. A path is a
string; a scene is a dict; the store is opened in memory; the search callable
is a fake that answers from a script and records what it was asked; the media
client is a fake that refuses every call but the one read a scan is allowed to
make. Nothing sleeps: threads are ordered with `threading.Event`, and every
wait carries a timeout that exists only so a broken implementation fails
instead of hanging.
"""
import inspect
import threading
import unittest
from unittest import mock

import cronicled.artist
from cronicled.artist import Aliases
from cronicled.jobs import COST_CLASS_LIMITS, JobRunner
from cronicled.scan import (
    Counts, MAX_RUNNERS_UP, MUTE_NO_CANDIDATES, MUTE_UNRESOLVED_CREATOR,
    Outcome, ScanProducer, Source, SUBJECT_TYPE, _SingleFlight, examine,
    examine_sources, select,
)
from cronicled.store import Store
from tests.fixtures.cast import CENSORSHIP

FOLDER = "library"

# Every wait in this file is bounded by this. It is a deadlock guard, never a
# synchronisation device: in a passing run every wait returns the instant the
# other thread sets its event, and nothing here ever waits for time to pass.
WAIT = 10


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

    def test_the_subject_kind_matches_what_earlier_runs_already_wrote(self):
        """The mute and the proposal here are written with the LITERAL string,
        the way a database filled by an earlier run holds them.

        Every other assertion in this file spells the kind `SUBJECT_TYPE` on
        both sides — the value handed to the store and the value it is checked
        against — so changing the constant moves both and nothing objects.
        Confirmed by mutation: renaming it survives the whole suite.

        It is not an internal label. `select` reads muted subjects and
        existing proposals back OUT of a store that outlives the run that
        wrote them, keyed on this string. Change it and every mute a reviewer
        has ever set stops suppressing its file, every proposal already in the
        inbox stops counting as already proposed, and the next scan re-offers
        work that was decided months ago — spending the lookup budget this
        whole module exists to ration, with nothing anywhere reporting it.
        """
        self.assertEqual(SUBJECT_TYPE, "scene")
        self.store.record(folder=FOLDER, subject_type="scene", subject_id="1",
                          summary="a proposal", payload={"title": "something"},
                          producer="an-earlier-run")
        self.store.mute("scene", "2")

        _, counts = select([scene(1, "/library/one.mp4"),
                            scene(2, "/library/two.mp4"),
                            scene(3, "/library/three.mp4")],
                           store=self.store, folder=FOLDER)

        self.assertEqual(counts, Counts(
            total=3, already_proposed=1, muted=1, filtered_out=0, selected=1,
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

    def test_a_dismissed_proposal_does_not_suppress_its_file(self):
        """Dismissing rejects ONE proposal; muting rejects the subject. A
        dismissed file is scanned again, because a better proposal for it is
        allowed to arrive tomorrow and cannot if the file is never looked at.

        Today that holds only as a side effect of `items()` hiding dismissed
        rows by default. Anyone "fixing" the inbox so a dismissed file stops
        reappearing — by asking for `state="dismissed"` here as well — would
        silently convert a one-proposal rejection into a permanent
        subject-level block, which is the one thing muting is for and the one
        thing dismissal is not.
        """
        dismissed = self.propose(1)
        self.store.dismiss(dismissed)
        self.store.mute(SUBJECT_TYPE, "2")

        selected, counts = select(
            [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")],
            store=self.store, folder=FOLDER)

        # The muted file beside it, so the test cannot pass by treating every
        # earlier decision as harmless.
        self.assertEqual([s["id"] for s in selected], ["1"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=1, filtered_out=0, selected=1,
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

    def test_a_superseded_proposal_frees_its_file_for_re_examination(self):
        """The whole reason `Store.supersede` exists (ticket 86): an old,
        thin proposal must not block its file forever once a person has
        explicitly retired it."""
        self.propose(1)
        fps = {item["subject_id"]: item["fingerprint"]
              for item in self.store.items()}
        self.store.supersede(fps["1"])
        scenes = [scene(1, "/library/one.mp4"), scene(2, "/library/two.mp4")]

        selected, counts = select(scenes, store=self.store, folder=FOLDER)

        self.assertEqual([s["id"] for s in selected], ["1", "2"])
        self.assertEqual(counts, Counts(
            total=2, already_proposed=0, muted=0, filtered_out=0, selected=2,
            deferred=0))

    def test_superseding_an_applied_proposal_also_frees_its_file(self):
        """An `applied` row keeps its own `state` untouched by `supersede`
        (see its docstring), so `items()` alone still reports this subject
        as proposed -- this is the case that actually proves `select` reads
        the `supersede` table, not just `item.state`."""
        fp = self.propose(1)
        self.store.mark_applied(fp, prior_state={"title": "x"})
        self.store.supersede(fp)

        selected, counts = select([scene(1, "/library/one.mp4")],
                                  store=self.store, folder=FOLDER)

        self.assertEqual([s["id"] for s in selected], ["1"])
        self.assertEqual(counts, Counts(
            total=1, already_proposed=0, muted=0, filtered_out=0, selected=1,
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


def scraped(title, url, **over):
    """A fuller `ScrapedScene`-shaped record, as `Stash.scrape_scene_url`
    would answer it for the SAME object a thin `candidate()` above stands
    for — carrying a performer, a studio and a date `candidate()` never
    does, which is the whole difference enrichment exists to make visible.
    """
    row = {"title": title, "url": url, "urls": [url], "code": None,
           "details": None, "director": None, "date": "2025-11-02",
           "image": None, "studio": {"stored_id": None, "name": "Amber Vale"},
           "tags": [], "performers": [{"stored_id": None, "name": "Wren Ashcombe"}]}
    row.update(over)
    return row


class FakeEnrich:
    """The injected enrichment lookup `examine(..., enrich=...)` calls with
    the winning candidate's own URL: answers from a script keyed by URL, or
    raises, and remembers every URL it was asked to scrape.

    Remembering them is what lets a test tell "enrichment happened once, for
    the winner alone" from "every candidate was scraped" or "nothing was
    scraped at all" — the three shapes this whole feature's cost argument
    turns on.
    """

    def __init__(self, script=None, raises=None):
        self._script = dict(script or {})
        self._raises = raises
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if self._raises is not None:
            raise self._raises
        return self._script.get(url)


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

    def test_runners_up_on_the_same_score_are_ordered_by_title(self):
        """Exact ties are a NORMAL outcome, not a rarity, so the order of
        equal-scoring losers cannot be left to the order the catalogue
        happened to return them in.

        The three losers here score identically and are offered in reverse
        alphabetical order, so a sort on score alone — which is stable, and
        therefore keeps the input order — leaves the payload dependent on the
        catalogue's ordering. The store hashes that payload, so the same file
        re-proposes as a fresh row on any night the catalogue shuffles: an
        inbox that refills with work already reviewed, which is the harm
        `_runners_up` says it exists to prevent.
        """
        tied = [candidate("Lantern Copper", "lantern-copper"),
                candidate("Harbour Errand", "harbour-errand"),
                candidate("Copper Saffron", "copper-saffron")]
        outcome, _ = self.run_examine(
            "/library/Velvet Crane/Morning Ritual.mp4",
            results=[self.MORNING] + tied)

        self.assertEqual(outcome.proposal["payload"]["runners_up"], [
            {"candidate": tied[2], "score": 0.092},
            {"candidate": tied[1], "score": 0.092},
            {"candidate": tied[0], "score": 0.092},
        ])

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

    def test_each_mute_reason_says_which_of_the_two_it_is(self):
        """Which constant carries which sentence, pinned as a property of the
        sentence rather than as the constant compared to itself.

        The tests above spell the expected reason `MUTE_NO_CANDIDATES` and
        `MUTE_UNRESOLVED_CREATOR`, which is the same name the code returns:
        exchange the two strings at their definitions and both sides move
        together, both tests still pass, and `assertNotEqual` above still
        holds because they are still two different strings. Confirmed by
        mutation — the swap survives the whole suite.

        What the swap costs is the one thing these two reasons exist to
        separate. The stored reason is what a reviewer reads months later,
        and only ONE of the two is fixed by adding an alias. Swapped, every
        file the catalogue had nothing for tells them to go write an alias
        that will never fire, and every file whose layout named nobody tells
        them the catalogue is empty for a creator that was never identified.

        Each sentence is pinned by what it names AND by what it does not: a
        single catch-all mentioning the catalogue, the folder and the
        filename at once would satisfy the positive halves alone.
        """
        self.assertIn("candidates", MUTE_NO_CANDIDATES)
        self.assertIn("catalogue", MUTE_NO_CANDIDATES)
        self.assertNotIn("folder", MUTE_NO_CANDIDATES)
        self.assertNotIn("filename", MUTE_NO_CANDIDATES)

        self.assertIn("folder", MUTE_UNRESOLVED_CREATOR)
        self.assertIn("filename", MUTE_UNRESOLVED_CREATOR)
        self.assertNotIn("candidates", MUTE_UNRESOLVED_CREATOR)
        self.assertNotIn("catalogue", MUTE_UNRESOLVED_CREATOR)

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

    def test_a_score_resting_on_one_generic_word_yields_no_proposal_and_no_mute(self):
        """The third of `decide`'s refusals to reach here, and the same
        argument as the other two. A one-word name in a creator's folder is an
        ordinary way to file a library, and 0.880 is a near miss: one rename,
        or one nudge of the generic-word threshold, from resolving. Muting it
        hides a nearly-identified file forever, and nothing revisits a mute.
        """
        outcome, search = self.run_examine(
            "/library/Velvet Crane/Ritual.mp4",
            results=[self.MORNING, self.EVENING])

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(outcome, Outcome(
            proposal=None, mute_reason=None, error=None,
            reason="best score 0.880 rests on a single generic word "
                   "(meaningful_count=1); needs 0.90 or above"))

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


class ExamineCensorshipTest(unittest.TestCase):
    """`censorship` decensors a candidate's title for SCORING only.

    The filename below spells its subject the canonical way ("Kestrel"); the
    candidate is the store's own record of it, spelled the way the fixture
    store censors that word ("K3strel"). Read raw, that one substituted
    letter costs enough recall/similarity to fall well under the default
    threshold. Decensored for scoring, the two agree exactly and the file is
    contained, which is why this fixture (not an arbitrary one) is what
    exercises the wiring: the difference between "refused" and "chosen with
    score 1.000" IS the censorship map actually being consulted.
    """

    CENSORED = candidate("K3strel Nightfall", "kestrel-nightfall")
    PATH = "/library/Velvet Crane/Kestrel Nightfall.mp4"

    def run_examine(self, censorship=None):
        search = FakeSearch([self.CENSORED])
        return examine(scene(1, self.PATH), search=search, folder=FOLDER,
                       threshold=0.7, censorship=censorship)

    def test_without_the_map_the_censored_spelling_is_refused(self):
        # HARM: this is the baseline the two tests below exist to contrast
        # with. If this one stops refusing, the fixture no longer proves
        # anything about the map, and the "with censorship" test could pass
        # for a reason that has nothing to do with decensoring.
        outcome = self.run_examine(censorship=None)
        self.assertIsNone(outcome.proposal)
        self.assertIn("nothing above the threshold", outcome.reason)

    def test_with_the_map_the_decensored_title_is_what_gets_scored(self):
        # HARM: dropping the decensor call from the scoring path (or from
        # the query path in `cronicled.search`, which this does not exercise)
        # leaves a real store's censorship map inert, exactly as it was
        # before this was wired in — a store configured with censored-word
        # substitutions never benefits from any of them.
        outcome = self.run_examine(censorship=CENSORSHIP)
        self.assertIsNotNone(outcome.proposal)
        self.assertEqual(outcome.proposal["payload"]["score"], 1.0)

    def test_the_proposal_carries_the_original_spelling_never_the_decensored_one(self):
        # HARM: if a decensored string ever replaced the candidate that
        # reaches the payload, an apply would write a title the store never
        # used — the store called this "K3strel Nightfall", and proposing
        # "Kestrel Nightfall" instead invents a title on the strength of a
        # scoring aid, not of anything the store actually said.
        outcome = self.run_examine(censorship=CENSORSHIP)
        self.assertEqual(outcome.proposal["payload"]["candidate"], self.CENSORED)
        self.assertEqual(
            outcome.proposal["payload"]["candidate"]["title"], "K3strel Nightfall")
        self.assertEqual(
            outcome.proposal["summary"],
            'Kestrel Nightfall.mp4 -> "K3strel Nightfall" by Velvet Crane '
            '(score 1.000)')

    def test_an_absent_map_behaves_exactly_like_an_empty_one(self):
        """`censorship=None` (every existing caller of `examine`) must not be
        a different code path from `censorship={}` — both are "nothing to
        decensor", and a caller that never heard of this feature must see
        unchanged behaviour."""
        self.assertEqual(self.run_examine(censorship=None),
                         self.run_examine(censorship={}))


class ExamineOwnerOfTest(unittest.TestCase):
    """`examine(..., owner_of=...)` -- the fix for issue #66, exercised as
    `examine` actually wires it: a real `search` callable answering per
    query, and a plain reader of one field on a result. "Amberlight" stands
    in for the measured store, "Wren Ashcombe" for the measured creator;
    both invented, matching no real store or performer.

    `ScriptedSearch` (defined below, for `ScanProducerTest`) answers each
    query separately, which this needs and `FakeSearch` (answers every query
    identically) cannot: a search-backed resolve issues one query to check
    the losing candidate and another for the winner's own catalogue, and the
    two must not be confused.
    """

    PATH = "/library/Amberlight/Wren Ashcombe - Morning Ritual.mp4"

    @staticmethod
    def owner_of(result):
        return (result or {}).get("owner", "")

    def test_a_candidate_the_catalogue_confirms_reaches_the_proposal(self):
        # HARM this fixes: without a search, the folder ("Amberlight", the
        # store) would win by the old default -- exactly the wrong
        # attribution the live scan produced.
        owned = dict(candidate("Morning Ritual", "morning-ritual"),
                     owner="Wren Ashcombe")
        search = ScriptedSearch({"Amberlight": [], "Wren Ashcombe": [owned]})

        outcome = examine(scene(1, self.PATH), search=search, folder=FOLDER,
                          threshold=0.5, owner_of=self.owner_of)

        self.assertEqual(outcome.proposal["payload"]["creator"], {
            "name": "Wren Ashcombe", "source": "filename",
            "competing": "Amberlight", "rejected_folder": None})
        # both the losing candidate's check and the winner's own catalogue
        # search happened -- "Wren Ashcombe" asked twice is the SAME query,
        # once to verify and once to score; raw `examine` has no
        # single-flight cache of its own (`ScanProducer` adds one).
        self.assertEqual(search.queries,
                         ["Amberlight", "Wren Ashcombe", "Wren Ashcombe"])

    def test_a_candidate_the_catalogue_does_not_support_is_not_proposed(self):
        # The store's own name has nothing behind it; only the creator's
        # does. Swap which query answers and the winner swaps with it --
        # proving this is the search deciding, not a fixed preference.
        owned = dict(candidate("Morning Ritual", "morning-ritual"),
                     owner="Amberlight")
        search = ScriptedSearch({"Amberlight": [owned], "Wren Ashcombe": []})

        outcome = examine(scene(1, self.PATH), search=search, folder=FOLDER,
                          threshold=0.5, owner_of=self.owner_of)

        self.assertEqual(outcome.proposal["payload"]["creator"]["name"],
                         "Amberlight")
        self.assertEqual(outcome.proposal["payload"]["creator"]["source"],
                         "folder")

    def test_owner_of_omitted_keeps_the_old_folder_wins_default(self):
        # No `owner_of` at all: `examine` must not spend a single extra
        # lookup checking a candidate it was never asked to check.
        search = FakeSearch([candidate("Morning Ritual", "morning-ritual")])

        outcome = examine(scene(1, self.PATH), search=search, folder=FOLDER,
                          threshold=0.5)

        self.assertEqual(outcome.proposal["payload"]["creator"], {
            "name": "Amberlight", "source": "folder",
            "competing": "Wren Ashcombe", "rejected_folder": None})
        self.assertEqual(search.queries, ["Amberlight"])


class ExamineEnrichmentTest(unittest.TestCase):
    """`examine(..., enrich=...)` -- scraping the winning candidate's own
    URL once `decide` has picked it, so a proposal carries more than a
    title and a link. "Amber Vale"/"Wren Ashcombe" (via `scraped()`) stand
    in for a measured store and creator; both invented.
    """

    MORNING_URL = "https://example.invalid/clip/morning-ritual"
    EVENING_URL = "https://example.invalid/clip/evening-errand"
    MORNING = candidate("Morning Ritual", "morning-ritual")
    EVENING = candidate("Evening Errand", "evening-errand")
    PATH = "/library/Velvet Crane/Morning Ritual.mp4"

    def run_examine(self, enrich, results=None, path=None, threshold=0.5):
        results = [self.MORNING, self.EVENING] if results is None else results
        search = FakeSearch(results)
        return examine(scene(1, path or self.PATH), search=search,
                       folder=FOLDER, threshold=threshold, enrich=enrich)

    def test_the_winner_is_replaced_by_the_fuller_scrape(self):
        fuller = scraped("Morning Ritual", self.MORNING_URL)
        enrich = FakeEnrich({self.MORNING_URL: fuller})

        outcome = self.run_examine(enrich)

        self.assertEqual(outcome.proposal["payload"]["candidate"], fuller)

    def test_only_the_winner_is_enriched_not_a_losing_candidate(self):
        # HARM: enriching every candidate scored, rather than only the one
        # that won, spends a lookup per candidate instead of one per
        # proposal -- exactly the budget this whole module exists to
        # ration (see MAX_RUNNERS_UP and _SingleFlight for the same
        # argument made elsewhere in this file).
        enrich = FakeEnrich({
            self.MORNING_URL: scraped("Morning Ritual", self.MORNING_URL),
            self.EVENING_URL: scraped("Evening Errand", self.EVENING_URL)})

        self.run_examine(enrich)

        self.assertEqual(enrich.urls, [self.MORNING_URL])

    def test_a_raising_enrichment_degrades_to_the_thin_candidate(self):
        # HARM: refusing the proposal here throws away a title and a URL
        # that are worth writing on their own -- exactly what a proposal
        # has always carried without enrichment at all. A scrape that
        # raises is a fact about the network, the same reasoning `examine`
        # already applies to a failed `search` above.
        enrich = FakeEnrich(raises=RuntimeError("connection reset"))

        outcome = self.run_examine(enrich)

        self.assertIsNotNone(outcome.proposal)
        self.assertEqual(outcome.proposal["payload"]["candidate"], self.MORNING)

    def test_a_null_scrape_result_also_keeps_the_thin_candidate(self):
        # A `None` reply -- "nothing new for this URL", `Stash
        # .scrape_scene_url`'s own contract for a miss -- is an ordinary
        # answer, not a failure, and is handled identically to a raise: the
        # thin candidate stands.
        enrich = FakeEnrich({self.MORNING_URL: None})

        outcome = self.run_examine(enrich)

        self.assertEqual(outcome.proposal["payload"]["candidate"], self.MORNING)

    def test_enrichment_never_reaches_a_file_that_refuses(self):
        # A tie or a near miss never picks a winner at all, so there is
        # nothing for `enrich` to be called with.
        dawn = candidate("Morning Ritual Dawn", "morning-ritual-dawn")
        dusk = candidate("Morning Ritual Dusk", "morning-ritual-dusk")
        enrich = FakeEnrich()

        outcome = self.run_examine(enrich, results=[dawn, dusk])

        self.assertIsNone(outcome.proposal)
        self.assertEqual(enrich.urls, [])

    def test_enrich_omitted_issues_no_lookup_and_keeps_the_thin_candidate(self):
        outcome = examine(scene(1, self.PATH),
                          search=FakeSearch([self.MORNING, self.EVENING]),
                          folder=FOLDER, threshold=0.5)

        self.assertEqual(outcome.proposal["payload"]["candidate"], self.MORNING)

    def test_the_summary_names_the_enriched_titles_candidate(self):
        # The summary is read off `winner`, so a scrape that corrects or
        # cleans up a title (the common reason a full page and a search
        # index disagree) shows up in the one line a reviewer reads.
        fuller = scraped("Morning Ritual (Director's Cut)", self.MORNING_URL)
        enrich = FakeEnrich({self.MORNING_URL: fuller})

        outcome = self.run_examine(enrich)

        self.assertIn('"Morning Ritual (Director\'s Cut)"',
                      outcome.proposal["summary"])


class EnrichmentURLChoice(unittest.TestCase):
    """Which of a winning candidate's two URL fields `examine` hands to
    `enrich` -- see `cronicled.scan._enrichment_url`'s own docstring for why
    the choice mirrors `Stash.apply_scene`'s own `urls`/`url` precedence
    rather than inventing a second rule for the same object.
    """

    EVENING = candidate("Evening Errand", "evening-errand")
    PATH = "/library/Velvet Crane/Morning Ritual.mp4"
    PLURAL_URL = "https://example.invalid/clip/plural"
    SINGULAR_URL = "https://example.invalid/clip/singular"

    def run_examine(self, morning, enrich):
        search = FakeSearch([morning, self.EVENING])
        return examine(scene(1, self.PATH), search=search, folder=FOLDER,
                       threshold=0.5, enrich=enrich)

    def test_the_plural_urls_field_is_preferred_over_the_singular(self):
        morning = dict(candidate("Morning Ritual", "morning-ritual"),
                      urls=[self.PLURAL_URL], url=self.SINGULAR_URL)
        enrich = FakeEnrich({self.PLURAL_URL:
                             scraped("Morning Ritual", self.PLURAL_URL)})

        self.run_examine(morning, enrich)

        self.assertEqual(enrich.urls, [self.PLURAL_URL])

    def test_the_singular_url_is_used_when_the_plural_list_is_empty(self):
        morning = dict(candidate("Morning Ritual", "morning-ritual"),
                      urls=[], url=self.SINGULAR_URL)
        enrich = FakeEnrich({self.SINGULAR_URL:
                             scraped("Morning Ritual", self.SINGULAR_URL)})

        self.run_examine(morning, enrich)

        self.assertEqual(enrich.urls, [self.SINGULAR_URL])

    def test_a_candidate_with_neither_url_field_skips_enrichment_entirely(self):
        morning = {"title": "Morning Ritual"}
        enrich = FakeEnrich()

        outcome = self.run_examine(morning, enrich)

        self.assertEqual(enrich.urls, [])
        self.assertEqual(outcome.proposal["payload"]["candidate"], morning)


class ExamineSourcesTest(unittest.TestCase):
    """`examine_sources` -- searching EVERY configured store before deciding
    anything, rather than stopping at the first that answers. The two
    invented stores below are named `alpha`/`beta` throughout, deliberately
    generic: the property under test is which STORE answered and whether
    that mattered, not which real site either name might otherwise suggest.
    """

    PATH = "/library/Velvet Crane/Morning Ritual.mp4"
    MORNING = candidate("Morning Ritual", "morning-ritual")
    ALPHA_MORNING = candidate("Morning Ritual", "alpha-morning-ritual")
    BETA_MORNING = candidate("Morning Ritual", "beta-morning-ritual")

    def source(self, name, search, catalog_resolvable=True, owner_of=None,
              censorship=None):
        return Source(name=name, search=search, owner_of=owner_of,
                      catalog_resolvable=catalog_resolvable,
                      censorship=censorship or {})

    def examine(self, sources, path=None, threshold=0.5, aliases=None,
               enrich=None):
        return examine_sources(
            scene(1, path or self.PATH), sources=sources, folder=FOLDER,
            threshold=threshold, aliases=aliases, enrich=enrich)

    # -- every store is searched, never just the first that matches ------- #

    def test_every_store_is_searched_even_after_an_earlier_one_wins(self):
        """The mutation this whole ticket exists to kill: stopping at the
        first store that matches would leave `beta` never asked at all.
        Assert the SECOND store's own query fired, not just that a proposal
        came back -- a proposal alone cannot tell "beta was never asked"
        from "beta was asked and had nothing"."""
        alpha_search = FakeSearch([self.MORNING])
        beta_search = FakeSearch([])
        sources = [self.source("alpha", alpha_search),
                  self.source("beta", beta_search)]

        outcome = self.examine(sources)

        self.assertIsNotNone(outcome.proposal)
        self.assertEqual(alpha_search.queries, ["Velvet Crane"])
        self.assertEqual(beta_search.queries, ["Velvet Crane"],
                         "beta was never searched -- the scan stopped at "
                         "the first store that matched")

    def test_a_losing_stores_search_does_not_stop_a_later_ones(self):
        """The other direction: the FIRST store has nothing, and the scan
        must still go on to ask the second rather than concluding early."""
        alpha_search = FakeSearch([])
        beta_search = FakeSearch([self.MORNING])
        sources = [self.source("alpha", alpha_search),
                  self.source("beta", beta_search)]

        outcome = self.examine(sources)

        self.assertIsNotNone(outcome.proposal)
        self.assertEqual(alpha_search.queries, ["Velvet Crane"])
        self.assertEqual(beta_search.queries, ["Velvet Crane"])

    # -- two winners is a finding, decided without regard to order --------- #

    def test_two_non_resolvable_winners_refuse_regardless_of_order(self):
        """Neither store can confirm ownership, so neither may be preferred
        over the other on the strength of its own score alone -- refused,
        and refused the SAME WAY whichever order `sources` lists them in."""
        forward = [
            self.source("alpha", FakeSearch([self.ALPHA_MORNING]),
                       catalog_resolvable=False),
            self.source("beta", FakeSearch([self.BETA_MORNING]),
                       catalog_resolvable=False),
        ]
        backward = list(reversed(forward))

        first = self.examine(forward)
        second = self.examine(backward)

        self.assertIsNone(first.proposal)
        self.assertIsNone(second.proposal)
        self.assertEqual(first.reason, second.reason)
        self.assertIn("ambiguous across stores", first.reason)

    def test_two_resolvable_winners_are_a_real_conflict_and_refuse(self):
        """Two stores this project trusts to confirm ownership, each
        independently clearing the threshold for the SAME file -- the
        "occasionally a real conflict" case named in the ticket. Refused
        rather than picked by position, the same discipline
        `scoring.decide` already applies within one store's own candidates."""
        forward = [
            self.source("alpha", FakeSearch([self.ALPHA_MORNING]),
                       catalog_resolvable=True),
            self.source("beta", FakeSearch([self.BETA_MORNING]),
                       catalog_resolvable=True),
        ]
        backward = list(reversed(forward))

        first = self.examine(forward)
        second = self.examine(backward)

        self.assertIsNone(first.proposal)
        self.assertIsNone(second.proposal)
        self.assertEqual(first.reason, second.reason)
        self.assertIn("ambiguous across stores", first.reason)
        self.assertIn("alpha", first.reason)
        self.assertIn("beta", first.reason)

    def test_two_resolvable_stores_agreeing_is_the_common_case_not_a_refusal(self):
        """Refusing every time two trustworthy stores both match would make
        the ticket's OWN common case -- "most often the same work published
        in both places" -- as disruptive as the rare real conflict. A CLEAR
        score leader (well outside `scoring.AMBIGUITY_MARGIN`) is proposed,
        with the other store's own candidate recorded as `competing_store`
        -- reported, exactly as a folder and a filename disagreeing already
        is, rather than silently dropped OR refused outright."""
        stronger = candidate("Morning Ritual", "morning-ritual")   # scores 1.000
        weaker = candidate("Morning Errand", "morning-errand")     # scores 0.564
        sources = [
            self.source("alpha", FakeSearch([stronger]), catalog_resolvable=True),
            self.source("beta", FakeSearch([weaker]), catalog_resolvable=True),
        ]

        outcome = self.examine(sources, threshold=0.5)

        self.assertIsNotNone(outcome.proposal)
        payload = outcome.proposal["payload"]
        self.assertEqual(payload["candidate"], stronger)
        self.assertEqual(payload["store"], "alpha")
        self.assertEqual(payload["competing_store"],
                         [{"store": "beta", "candidate": weaker, "score": 0.564}])

    def test_a_resolvable_winner_is_chosen_over_a_non_resolvable_one(self):
        """A store that cannot confirm ownership must not out-rank, or tie
        with, one that can -- the rule `Source.catalog_resolvable` exists
        to enforce. Both winners score identically (score is not what
        decides this), and the choice does not depend on which one
        `sources` lists first."""
        forward = [
            self.source("resolvable", FakeSearch([self.ALPHA_MORNING]),
                       catalog_resolvable=True),
            self.source("unresolvable", FakeSearch([self.BETA_MORNING]),
                       catalog_resolvable=False),
        ]
        backward = list(reversed(forward))

        for sources in (forward, backward):
            outcome = self.examine(sources)
            self.assertIsNotNone(outcome.proposal)
            payload = outcome.proposal["payload"]
            self.assertEqual(payload["candidate"], self.ALPHA_MORNING)
            self.assertEqual(payload["store"], "resolvable")
            self.assertEqual(payload["competing_store"], [
                {"store": "unresolvable", "candidate": self.BETA_MORNING,
                 "score": 1.0},
            ])
            self.assertIn("also matched by unresolvable",
                         outcome.proposal["summary"])

    def test_a_single_winner_carries_no_competing_store_key_at_all(self):
        """The ordinary, single-store-answers case must not grow a
        `competing_store` key at all -- not an empty list, ABSENT -- so a
        payload from an unremarkable file stays exactly the shape it always
        was, one key added (`store`) and nothing else."""
        outcome = self.examine([self.source("alpha", FakeSearch([self.MORNING]))])

        self.assertNotIn("competing_store", outcome.proposal["payload"])
        self.assertEqual(outcome.proposal["payload"]["store"], "alpha")

    # -- muting requires EVERY store to be confirmed empty ----------------- #

    def test_muted_only_when_every_store_answers_with_nothing(self):
        outcome = self.examine([
            self.source("alpha", FakeSearch([])),
            self.source("beta", FakeSearch([])),
        ])

        self.assertEqual(outcome.mute_reason, MUTE_NO_CANDIDATES)

    def test_not_muted_when_only_some_stores_are_empty(self):
        """`alpha` found nothing; `beta` found candidates that did not
        clear the threshold -- a human should look, because a real catalogue
        DID have something to weigh, so this is a refusal, not a mute."""
        dawn = candidate("Morning Ritual Dawn", "morning-ritual-dawn")
        dusk = candidate("Morning Ritual Dusk", "morning-ritual-dusk")
        outcome = self.examine([
            self.source("alpha", FakeSearch([])),
            self.source("beta", FakeSearch([dawn, dusk])),
        ])

        self.assertIsNone(outcome.mute_reason)
        self.assertIsNone(outcome.proposal)
        self.assertIn("beta:", outcome.reason)

    def test_the_closest_refusal_does_not_depend_on_store_order(self):
        """Two stores, tied on the identical losing candidate -- the reason
        must name the same store either way, broken by NAME rather than by
        position in `sources`."""
        weak = candidate("Harbour Lights", "harbour-lights")
        forward = [self.source("beta", FakeSearch([weak])),
                  self.source("alpha", FakeSearch([weak]))]
        backward = list(reversed(forward))

        first = self.examine(forward, threshold=0.9)
        second = self.examine(backward, threshold=0.9)

        self.assertEqual(first.reason, second.reason)
        self.assertTrue(first.reason.startswith("alpha:"), first.reason)

    # -- a store's own search error is isolated to that store -------------- #

    def test_a_stores_search_error_does_not_block_another_stores_winner(self):
        outcome = self.examine([
            self.source("alpha", FakeSearch([], raises=RuntimeError("down"))),
            self.source("beta", FakeSearch([self.MORNING])),
        ])

        self.assertIsNotNone(outcome.proposal)
        self.assertEqual(outcome.proposal["payload"]["store"], "beta")
        # Visible, not silent: the failing store still shows up in the
        # reason a reviewer or a log line reads.
        self.assertIn("RuntimeError", outcome.reason)
        self.assertIn("alpha", outcome.reason)

    def test_every_store_erroring_is_reported_as_an_error_not_a_mute(self):
        outcome = self.examine([
            self.source("alpha", FakeSearch([], raises=RuntimeError("down"))),
            self.source("beta", FakeSearch([], raises=TimeoutError())),
        ])

        self.assertIsNone(outcome.mute_reason)
        self.assertIsNotNone(outcome.error)
        self.assertIn("alpha", outcome.error)
        self.assertIn("beta", outcome.error)

    def test_one_store_erroring_with_the_rest_confirmed_empty_is_an_error(self):
        """An error is evidence about the network, not a confirmed-empty
        catalogue -- it must not be treated as though every store had
        genuinely offered nothing, which is the only case that earns a
        mute."""
        outcome = self.examine([
            self.source("alpha", FakeSearch([], raises=RuntimeError("down"))),
            self.source("beta", FakeSearch([])),
        ])

        self.assertIsNone(outcome.mute_reason)
        self.assertIsNotNone(outcome.error)

    # -- ownership evidence pools across every confirmable store ------------ #

    def test_ownership_evidence_pools_across_catalog_resolvable_stores(self):
        """The candidate search that actually confirms "Wren Ashcombe"
        lives on a DIFFERENT store than the one asked about the losing
        folder name -- resolving this at all proves the evidence was
        pooled, not read off one store alone."""
        owned = dict(candidate("Morning Ritual", "morning-ritual"),
                     owner="Wren Ashcombe")

        def owner_of(result):
            return (result or {}).get("owner", "")

        sources = [
            Source(name="alpha", search=ScriptedSearch({"Amberlight": []}),
                  owner_of=owner_of, catalog_resolvable=True),
            Source(name="beta",
                  search=ScriptedSearch({"Wren Ashcombe": [owned]}),
                  owner_of=owner_of, catalog_resolvable=True),
        ]

        outcome = examine_sources(
            scene(1, "/library/Amberlight/Wren Ashcombe - Morning Ritual.mp4"),
            sources=sources, folder=FOLDER, threshold=0.5)

        self.assertEqual(outcome.proposal["payload"]["creator"]["name"],
                         "Wren Ashcombe")

    def test_a_non_resolvable_stores_search_contributes_no_ownership_evidence(self):
        """A store with `owner_of=None` (`catalog_resolvable=False`) is
        simply never asked -- it must not manufacture support for a
        candidate neither queryable store actually backs."""
        def owner_of(result):
            return (result or {}).get("owner", "")

        sources = [
            Source(name="alpha",
                  search=ScriptedSearch({"Amberlight": [], "Wren Ashcombe": []}),
                  owner_of=owner_of, catalog_resolvable=True),
            Source(name="beta", search=FakeSearch([]), owner_of=None,
                  catalog_resolvable=False),
        ]

        outcome = examine_sources(
            scene(1, "/library/Amberlight/Wren Ashcombe - Morning Ritual.mp4"),
            sources=sources, folder=FOLDER, threshold=0.5)

        self.assertEqual(outcome.mute_reason, MUTE_UNRESOLVED_CREATOR)

    # -- malformed input ----------------------------------------------------- #

    def test_no_sources_raises(self):
        with self.assertRaises(ValueError):
            examine_sources(scene(1, self.PATH), sources=[], folder=FOLDER)


# -- running a whole batch ------------------------------------------------- #

class FakeStash:
    """The media client, which a scan may READ and must never write to.

    Every attribute other than the one read a scan is allowed to make comes
    back as a call that records itself and then fails the test. That makes the
    read-only property hold across every test in the file rather than in the
    one test named for it — a write added anywhere in the scan path is caught
    by whichever test reaches it first.

    `calls` records the whole call, arguments included, so a test can assert
    the entire conversation with the media server as one shape. A test that
    checked only "no writes" could not notice a read that was added, and the
    argument this scan passes is itself load-bearing: fetching with the scan's
    `limit` would apply the limit at the SOURCE, before any narrowing.
    """

    def __init__(self, scenes):
        self._scenes = list(scenes)
        self.calls = []

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", (limit,), {}))
        scenes = self._scenes if limit is None else self._scenes[:limit]
        return len(self._scenes), list(scenes)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError(
                "the scan called %r on the media server; a scan reads and "
                "looks things up, it never writes" % (name,))
        return refuse


class ScriptedSearch:
    """The injected lookup: answers each query from a script, and records the
    queries several threads asked it.

    `gates` holds a query open until an event is set, which is how a test
    orders two files without sleeping. A gate that never opens raises rather
    than hanging, and records the timeout in `timeouts` — a test that waits on
    an ordering must be able to tell "the fast file really did come out first"
    from "the slow file gave up and was skipped", because those two look
    identical in the yielded proposals.
    """

    def __init__(self, script=None, gates=None, opens=None):
        self._script = dict(script or {})
        self._gates = dict(gates or {})
        self._opens = dict(opens or {})
        self._lock = threading.Lock()
        self.queries = []
        self.timeouts = []

    def __call__(self, query):
        with self._lock:
            self.queries.append(query)
        opened = self._opens.get(query)
        if opened is not None:
            opened.set()
        gate = self._gates.get(query)
        if gate is not None and not gate.wait(WAIT):
            with self._lock:
                self.timeouts.append(query)
            raise AssertionError("the gate for %r never opened" % (query,))
        answer = self._script.get(query, [])
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)


class FakeCtx:
    """What the runner gives a producer: somewhere to log progress.

    `message` is what production RETAINS. `JobRunner._log` assigns
    `state.message = message` — one field, no history — so the last line a
    producer logs is the whole of what the job record says once the run is
    over. Every assertion about what a user reads off a finished job belongs
    against `message`, and against nothing else: asserting an earlier line
    proves a property the real collaborator does not keep, which is how the
    scan came to report a fully-suppressed batch and an empty library in
    byte-identical terms while this file was green.

    `messages` is the stream as it was logged, in order. It is kept because
    the per-file lines are real behaviour worth pinning — every file gets its
    own line, naming the file it is about, as it completes — but it is a
    recording of a stream, NOT what survives the run, and no conclusion about
    a job's record may be drawn from it. The property that matters is pinned
    against the real `JobRunner` instead; see
    `test_the_closing_line_tells_a_suppressed_batch_from_an_empty_one`.
    """

    def __init__(self):
        self.message = ""
        self.messages = []
        self._lock = threading.Lock()

    def log(self, message):
        with self._lock:
            self.message = message
            self.messages.append(message)


class MuteSpy:
    """A real store with its `mute` calls recorded on the way through.

    Delegating rather than faking, so a test can assert both that the producer
    called `mute` with the reason Task 2 chose AND that the real store now
    refuses proposals for that subject. A fake store could pass the first
    check while the mute never landed.
    """

    def __init__(self, store):
        self._store = store
        self.mutes = []

    def mute(self, subject_type, subject_id, reason=None):
        self.mutes.append((subject_type, subject_id, reason))
        return self._store.mute(subject_type, subject_id, reason=reason)

    def __getattr__(self, name):
        return getattr(self._store, name)


class SingleFlightTest(unittest.TestCase):
    """The collapse itself, in isolation.

    The batch tests below pin that N files naming one creator fire one query.
    They cannot tell a single-flight cache from a plain memo that happens not
    to race, because a memo only issues a duplicate while the first answer is
    still in flight. That window is what these tests hold open on purpose.
    """

    def test_a_second_caller_arriving_mid_flight_does_not_issue_its_own(self):
        """The first call is held open, and the second is made while it is
        still running. A memo that only stores the answer once it arrives has
        nothing to find at that moment and issues a second lookup — which is
        precisely the case parallelism creates and the cache exists for.

        The residual, stated rather than papered over: `calling` is set by the
        second thread immediately BEFORE its call, not from inside the cache,
        so nothing here proves it had entered before the release. Winning that
        race requires the first thread to wake, return and publish in the time
        the second takes to make one call, which is not the ordering to bet
        on, but it is not a proof either.
        """
        entered = threading.Event()
        release = threading.Event()
        calling = threading.Event()
        queries = []

        def search(query):
            queries.append(query)
            entered.set()
            self.assertTrue(release.wait(WAIT))
            return [{"title": query}]

        flight = _SingleFlight(search)
        answers = {}

        def first():
            answers["first"] = flight("Velvet Crane")

        def second():
            calling.set()
            answers["second"] = flight("Velvet Crane")

        one = threading.Thread(target=first)
        one.start()
        self.assertTrue(entered.wait(WAIT))
        two = threading.Thread(target=second)
        two.start()
        self.assertTrue(calling.wait(WAIT))
        release.set()
        one.join(WAIT)
        two.join(WAIT)

        self.assertEqual(queries, ["Velvet Crane"])
        self.assertEqual(answers["first"], [{"title": "Velvet Crane"}])
        self.assertEqual(answers["second"], [{"title": "Velvet Crane"}])

    def test_a_query_that_failed_is_not_retried_within_the_run(self):
        """A dead query is dead for every file that would phrase it. Retrying
        it once per file spends the whole budget discovering the same outage."""
        queries = []

        def search(query):
            queries.append(query)
            raise RuntimeError("connection reset")

        flight = _SingleFlight(search)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                flight("Velvet Crane")

        self.assertEqual(queries, ["Velvet Crane"])

    def test_queries_differing_only_in_case_or_spacing_are_one_query(self):
        queries = []

        def search(query):
            queries.append(query)
            return []

        flight = _SingleFlight(search)
        flight("Velvet Crane")
        flight("velvet crane")
        flight("Velvet  Crane")

        self.assertEqual(queries, ["Velvet Crane"])

    def test_two_creators_sharing_a_first_name_are_not_collapsed(self):
        """The other half of the boundary, and the harm the key's own
        docstring names: collapsing two creators who are not the same person
        means every file of the second is attributed from the FIRST one's
        catalogue — a confident, wrong attribution nobody is shown.

        They share a first name on purpose. Two names with no token in common
        catch only a key that collapses everything to one entry; they cannot
        catch a key that reads the first word and calls it identity, which is
        the plausible mistake.
        """
        queries = []

        def search(query):
            queries.append(query)
            return [{"title": query}]

        flight = _SingleFlight(search)
        first = flight("Velvet Crane")
        second = flight("Velvet Marsh")

        self.assertEqual(queries, ["Velvet Crane", "Velvet Marsh"])
        self.assertEqual(first, [{"title": "Velvet Crane"}])
        self.assertEqual(second, [{"title": "Velvet Marsh"}])

    def test_a_name_with_the_word_boundary_removed_is_a_different_query(self):
        """Folding RUNS of whitespace is spelling; deleting the boundary
        between two words is a guess about identity, and this cache is not the
        place to make one.

        The permissive side above and this one together fix the key at exactly
        what it claims to be: a key that stripped punctuation and spacing
        outright would satisfy every other test in this class while quietly
        answering one creator's files from another's catalogue.
        """
        queries = []

        def search(query):
            queries.append(query)
            return [{"title": query}]

        flight = _SingleFlight(search)
        flight("Velvet Crane")
        flight("Velvetcrane")

        self.assertEqual(queries, ["Velvet Crane", "Velvetcrane"])

    def test_a_waiter_is_told_what_actually_ended_the_flight(self):
        """The broad `except BaseException` is not what stops waiters hanging
        — the `finally` beside it sets the event whatever happens. What it
        buys is that a waiter is handed the failure that really ended the
        flight.

        Narrowed to `Exception`, an interrupt would leave the entry with
        neither a result nor an error, and every waiting file would report
        its own turn as having failed on an empty result: one outage
        described as N unrelated bugs, none of them naming the cause.
        """
        entered = threading.Event()
        release = threading.Event()
        calling = threading.Event()

        def search(query):
            entered.set()
            self.assertTrue(release.wait(WAIT))
            raise KeyboardInterrupt("interrupted")

        flight = _SingleFlight(search)
        raised = {}

        def call(which):
            try:
                flight("Velvet Crane")
            except BaseException as exc:      # noqa: BLE001 - recorded, not handled
                raised[which] = exc

        one = threading.Thread(target=call, args=("first",))
        one.start()
        self.assertTrue(entered.wait(WAIT))

        def second():
            calling.set()
            call("second")

        two = threading.Thread(target=second)
        two.start()
        self.assertTrue(calling.wait(WAIT))
        release.set()
        one.join(WAIT)
        two.join(WAIT)

        self.assertEqual(sorted(raised), ["first", "second"])
        self.assertIs(raised["first"], raised["second"])
        self.assertIsInstance(raised["second"], KeyboardInterrupt)


class ScanProducerTest(unittest.TestCase):

    MORNING = candidate("Morning Ritual", "morning-ritual")
    EVENING = candidate("Evening Errand", "evening-errand")
    LEDGER = candidate("Winter Ledger", "winter-ledger")

    # One creator with a catalogue, one with a single title. Both invented.
    SCRIPT = {"Velvet Crane": [MORNING, EVENING],
              "Ivy Kingsley": [LEDGER]}

    MORNING_PATH = "/library/Velvet Crane/Morning Ritual.mp4"
    EVENING_PATH = "/library/Velvet Crane/Evening Errand.mp4"
    LEDGER_PATH = "/library/Ivy Kingsley/Winter Ledger.mp4"
    UNNAMED_PATH = "/lib/2020-05-04/clip one.mp4"

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ctx = FakeCtx()
        self.stash = None

    def build(self, scenes, search, store=None, censorship=None, owner_of=None,
             catalog_resolvable=True, source_name="store", sources=None,
             **kwargs):
        """Build a `ScanProducer` against ONE store, named `source_name`,
        wrapping `search` (and this store's own `censorship`/`owner_of`) in
        a single-element `sources` list — the shape every pre-existing test
        in this class exercises, now expressed through the same `sources`
        list a real multi-store scan uses. Pass `sources` explicitly (a list
        of `Source`) instead of `search` to exercise more than one store."""
        self.stash = FakeStash(scenes)
        kwargs.setdefault("folder", FOLDER)
        if sources is None:
            sources = [Source(name=source_name, search=search,
                              owner_of=owner_of,
                              catalog_resolvable=catalog_resolvable,
                              censorship=censorship or {})]
        return ScanProducer(self.stash, sources,
                            store=self.store if store is None else store,
                            **kwargs)

    def scan(self, scenes, search, **kwargs):
        """Run a whole batch and return the proposals it yielded."""
        return list(self.build(scenes, search, **kwargs).produce(self.ctx))

    def run_under_the_runner(self, scenes, search, **kwargs):
        """Run a whole batch through the real `JobRunner` and return the job.

        A fresh runner per call: a runner refuses a second producer under a
        name it already holds, and the scraping cost class allows one job at a
        time, so two scans in one test are two runners.
        """
        producer = self.build(scenes, search, **kwargs)
        runner = JobRunner(self.store)
        runner.register(producer)
        job = runner.start(producer.name)
        self.assertTrue(runner.wait(job.id, WAIT))
        return runner.job(job.id)

    def ids(self, proposals):
        return sorted(p["subject_id"] for p in proposals)

    # -- the producer protocol -------------------------------------------- #

    def test_it_satisfies_the_producer_protocol(self):
        """`name`, a known cost class, and a `produce` that is a generator.
        The runner refuses a `produce` that returns a list, because such a
        producer computes everything before the runner sees a single proposal
        — losing exactly the partial progress this design exists to keep."""
        producer = self.build([scene(1, self.MORNING_PATH)],
                              ScriptedSearch(self.SCRIPT))

        self.assertEqual(producer.name, "library-scan")
        self.assertEqual(producer.cost, "scraping")
        self.assertIn(producer.cost, COST_CLASS_LIMITS)

        stream = producer.produce(self.ctx)
        self.assertTrue(inspect.isgenerator(stream))
        # Making a generator is not running one: nothing has been read from
        # the media server yet, so `start()` can do this on the caller's
        # thread and still leave all of the work on the worker's.
        self.assertEqual(self.stash.calls, [])
        stream.close()

    def test_the_runner_accepts_it(self):
        """Registration is where a cost class typo is caught, so a producer
        that cannot be registered is not a producer."""
        JobRunner(self.store).register(
            self.build([], ScriptedSearch(self.SCRIPT)))

    # -- what a proposal carries ------------------------------------------- #

    def test_a_decided_file_is_yielded_as_a_complete_proposal(self):
        """Asserted as one whole shape: the runner passes these fields
        straight to `record()`, which hashes the payload, so a field added
        here changes every fingerprint and a field missing here is a
        `KeyError` on a background thread.

        `"store"` names which configured source the winning candidate came
        from -- present even with only one store configured, the same as
        every scan now searches through `examine_sources` regardless of how
        many sources it is given."""
        proposals = self.scan([scene(7, self.LEDGER_PATH)],
                              ScriptedSearch(self.SCRIPT))

        self.assertEqual(proposals, [{
            "folder": FOLDER,
            "subject_type": SUBJECT_TYPE,
            "subject_id": "7",
            "summary": 'Winter Ledger.mp4 -> "Winter Ledger" by Ivy Kingsley '
                       '(score 1.000)',
            "confidence": 1.0,
            "payload": {
                "path": self.LEDGER_PATH,
                "creator": {"name": "Ivy Kingsley", "source": "folder",
                            "competing": None, "rejected_folder": None},
                "candidate": self.LEDGER,
                "score": 1.0,
                "runners_up": [],
                "store": "store",
            },
        }])

    def test_the_default_folder_reaches_the_proposal(self):
        producer = ScanProducer(
            FakeStash([scene(1, self.LEDGER_PATH)]),
            [Source(name="store", search=ScriptedSearch(self.SCRIPT))],
            store=self.store)
        proposals = list(producer.produce(self.ctx))

        self.assertEqual([p["folder"] for p in proposals], ["library"])

    def test_the_folder_reaches_both_the_proposal_and_the_store(self):
        """`folder` is the store's proposal namespace. A scan told to work one
        namespace must propose into it AND ask its "already proposed?"
        question about it — a hard-coded folder would file the work in one
        place while suppressing it from another."""
        for subject_id, folder in (("1", "review"), ("2", "inbox")):
            self.store.record(folder=folder, subject_type=SUBJECT_TYPE,
                              subject_id=subject_id, summary="a proposal",
                              payload={"title": "something"},
                              producer="earlier")
        search = ScriptedSearch(self.SCRIPT)
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH),
             scene(3, self.EVENING_PATH)],
            search, folder="review")

        # 1 is already proposed in the folder being scanned and is dropped;
        # 2's proposal lives in another folder and does not suppress it here.
        self.assertEqual(self.ids(proposals), ["2", "3"])
        self.assertEqual([p["folder"] for p in proposals], ["review", "review"])

    # -- the two properties that only exist in a batch --------------------- #

    def test_proposals_are_yielded_as_they_complete_not_in_input_order(self):
        """A slow first file must not hold back a fast second one. Yielding
        early is what makes progress survive an interruption, so an
        implementation that gathers the batch and yields at the end loses the
        whole point of the generator protocol.

        `timeouts` is asserted because without it this test passes for the
        wrong reason: an in-order implementation waits on the gated file,
        which gives up, errors, and yields nothing — leaving the fast file's
        proposal first anyway.
        """
        release = threading.Event()
        search = ScriptedSearch(self.SCRIPT,
                                gates={"Velvet Crane": release})
        producer = self.build(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)], search)
        stream = producer.produce(self.ctx)

        first = next(stream)
        self.assertEqual(first["subject_id"], "2")
        release.set()
        rest = list(stream)

        self.assertEqual([p["subject_id"] for p in rest], ["1"])
        self.assertEqual(search.timeouts, [])

    def test_identical_queries_collapse_to_one_lookup(self):
        """Three files by one creator fire one search, not three. Without
        this, parallelism multiplies consumption of exactly the resource the
        whole selection step exists to conserve."""
        search = ScriptedSearch(
            {"Velvet Crane": [self.MORNING, self.EVENING, self.LEDGER]})
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.EVENING_PATH),
             scene(3, "/library/Velvet Crane/Winter Ledger.mp4")],
            search, workers=3)

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(self.ids(proposals), ["1", "2", "3"])

    def test_a_failing_query_is_not_retried_by_every_file(self):
        """The catalogue being down is one fact, not one fact per file. Every
        file still gets its own error, and none of them is muted."""
        search = ScriptedSearch({"Velvet Crane": RuntimeError("connection reset")})
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.EVENING_PATH),
             scene(3, "/library/Velvet Crane/Winter Ledger.mp4")],
            search, workers=3)

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual(proposals, [])
        self.assertEqual(self.store.muted_subjects(), set())
        self.assertEqual(
            len([m for m in self.ctx.messages
                 if "RuntimeError: connection reset" in m]), 3)

    def test_work_already_yielded_survives_a_later_failure(self):
        """Driven partway, then made to fail: the proposal already handed over
        is intact and complete. The legacy tool computed everything and wrote
        at the end, so a crash on file 49 of 50 discarded the 48 already
        done."""
        release = threading.Event()
        search = ScriptedSearch(
            {"Velvet Crane": KeyboardInterrupt("interrupted"),
             "Ivy Kingsley": [self.LEDGER]},
            gates={"Velvet Crane": release})
        producer = self.build(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)], search)
        stream = producer.produce(self.ctx)

        yielded = next(stream)
        release.set()
        with self.assertRaises(KeyboardInterrupt):
            next(stream)

        self.assertEqual(yielded["subject_id"], "2")
        self.assertEqual(yielded["payload"]["candidate"], self.LEDGER)
        self.assertEqual(search.timeouts, [])
        # A run that died did not finish. The closing line is the only record
        # anyone reads afterwards, and it must not claim a clean end.
        self.assertEqual([m for m in self.ctx.messages
                          if m.startswith("finished:")], [])

    def test_the_runner_keeps_what_a_failed_scan_had_already_found(self):
        """The same property through the real runner, which is what actually
        persists a yield. The failure is held back until the first proposal
        has been recorded, so a store that only wrote at the end would have
        nothing here."""
        release = threading.Event()
        search = ScriptedSearch(
            {"Velvet Crane": KeyboardInterrupt("interrupted"),
             "Ivy Kingsley": [self.LEDGER]},
            gates={"Velvet Crane": release})
        producer = self.build(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)], search)

        recorded = self.store.record

        def record(**kwargs):
            fp = recorded(**kwargs)
            release.set()
            return fp

        self.store.record = record
        runner = JobRunner(self.store)
        runner.register(producer)
        job = runner.start(producer.name)
        self.assertTrue(runner.wait(job.id, WAIT))

        finished = runner.job(job.id)
        self.assertEqual(finished.state, "failed")
        self.assertEqual(finished.recorded, 1)
        self.assertIn("KeyboardInterrupt", finished.error)
        self.assertEqual([row["subject_id"]
                          for row in self.store.items(folder=FOLDER)], ["2"])
        self.assertEqual(search.timeouts, [])

    # -- the scan is read-only against the media server -------------------- #

    def test_the_scan_never_writes_to_the_media_server(self):
        """A scan reads and it looks things up. It never sets `organized`,
        never touches tags or performers, and that is what makes it safe to
        run repeatedly — a property easy to lose by accident later, so the
        whole conversation with the client is asserted rather than trusted."""
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)],
            ScriptedSearch(self.SCRIPT))

        # The conversation first, so that when this test fails it is this
        # assertion that speaks: a stray write shows up here by name, while a
        # missing proposal only says something went wrong somewhere.
        self.assertEqual(self.stash.calls,
                         [("unorganized_scenes", (None,), {})])
        self.assertEqual(self.ids(proposals), ["1", "2"])

    def test_the_limit_is_not_spent_at_the_source(self):
        """Fetching with the scan's own limit would limit BEFORE narrowing:
        the batch would be the first `limit` files overall, and the muted and
        already-proposed ones among them would eat the budget."""
        self.store.mute(SUBJECT_TYPE, "1")
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)],
            ScriptedSearch(self.SCRIPT), limit=1)

        self.assertEqual(self.stash.calls,
                         [("unorganized_scenes", (None,), {})])
        self.assertEqual(self.ids(proposals), ["2"])

    # -- narrowing before limiting, through the producer ------------------- #

    def test_a_filter_plus_a_limit_spends_the_limit_on_matching_files(self):
        """The rule the module exists for, seen from the outside: the limit
        slices the FILTERED set. The unmatched file must not buy a lookup."""
        search = ScriptedSearch(self.SCRIPT)
        proposals = self.scan(
            [scene(1, self.LEDGER_PATH), scene(2, self.MORNING_PATH),
             scene(3, self.EVENING_PATH)],
            search, name_filter="velvet", limit=1)

        self.assertEqual(self.ids(proposals), ["2"])
        self.assertEqual(search.queries, ["Velvet Crane"])

    def test_a_muted_file_buys_no_lookup(self):
        """The budget is lookups, and a subject the store would refuse a
        proposal for must not spend one."""
        self.store.mute(SUBJECT_TYPE, "1", reason="not this one")
        search = ScriptedSearch(self.SCRIPT)
        proposals = self.scan([scene(1, self.MORNING_PATH)], search)

        self.assertEqual(proposals, [])
        self.assertEqual(search.queries, [])

    def test_an_already_proposed_file_buys_no_lookup(self):
        self.store.record(folder=FOLDER, subject_type=SUBJECT_TYPE,
                          subject_id="1", summary="a proposal",
                          payload={"title": "something"}, producer="earlier")
        search = ScriptedSearch(self.SCRIPT)
        proposals = self.scan([scene(1, self.MORNING_PATH)], search)

        self.assertEqual(proposals, [])
        self.assertEqual(search.queries, [])

    # -- what happens to the files that do not become proposals ------------ #

    def test_an_unidentifiable_file_is_muted_through_the_store_with_a_reason(self):
        """The skiplist is a mute. The two reasons stay distinct, because one
        says the catalogue had nothing for a creator we did identify and the
        other says the library's own layout named nobody — and only the second
        is fixed by an alias."""
        spy = MuteSpy(self.store)
        proposals = self.scan(
            [scene(1, "/library/Velvet Crane/Harbour Lights.mp4"),
             scene(2, self.UNNAMED_PATH)],
            ScriptedSearch({"Velvet Crane": []}), store=spy)

        self.assertEqual(proposals, [])
        self.assertEqual(sorted(spy.mutes), sorted([
            (SUBJECT_TYPE, "1", MUTE_NO_CANDIDATES),
            (SUBJECT_TYPE, "2", MUTE_UNRESOLVED_CREATOR),
        ]))
        self.assertEqual(self.store.muted_subjects(),
                         {(SUBJECT_TYPE, "1"), (SUBJECT_TYPE, "2")})

    def test_the_mute_names_the_file_that_was_unidentifiable(self):
        """The mute is bound to the scene that produced it, not to the place
        in the completion order that scene happened to finish in.

        Completion order is not input order — that is this module's headline
        property — and the mute is its only write to the store, so the two
        have to be bound. Here the unidentifiable file is held open so it
        completes SECOND, behind a file that was proposed successfully. Keyed
        off the completion index instead of off the future, the mute lands on
        the GOOD file: suppressed permanently, while the hopeless one goes on
        buying a lookup every night and every per-file line names the wrong
        file.

        The existing out-of-order test reads `subject_id` off the proposal
        dict, which `examine` fills in from the scene it was handed, so it
        cannot see this.
        """
        release = threading.Event()
        search = ScriptedSearch(
            {"Velvet Crane": [], "Ivy Kingsley": [self.LEDGER]},
            gates={"Velvet Crane": release})
        spy = MuteSpy(self.store)
        producer = self.build(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)],
            search, store=spy)
        stream = producer.produce(self.ctx)

        # Scene 1 is still inside its gated lookup, so this is scene 2.
        first = next(stream)
        self.assertEqual(first["subject_id"], "2")
        release.set()
        self.assertEqual(list(stream), [])

        self.assertEqual(spy.mutes, [(SUBJECT_TYPE, "1", MUTE_NO_CANDIDATES)])
        self.assertEqual(self.store.muted_subjects(), {(SUBJECT_TYPE, "1")})
        # The same binding as the log sees it: the file that finished first
        # is named first, and each line names the file it is about.
        self.assertEqual(self.ctx.messages[1:3], [
            "1/2 scene 2: chosen with score 1.000",
            "2/2 scene 1: " + MUTE_NO_CANDIDATES,
        ])
        # Without this the test passes for the wrong reason: a gate that gave
        # up would make scene 1 an error rather than a mute, and an empty
        # mute list would look like the same failure this test is aimed at.
        self.assertEqual(search.timeouts, [])

    def test_a_candidate_with_no_title_costs_that_file_and_no_more(self):
        """One malformed catalogue row must not end the nightly batch.

        `examine` documents itself as raising `KeyError` for a candidate with
        no title, and that is pinned on `examine` directly — but whether it
        costs one file or every remaining file is decided by the breadth of
        the worker's isolation, which only a run through the producer can
        see. Narrowed to the exception a malformed *scene* raises, this
        `KeyError` escapes the worker and aborts the scan with the rest of
        the batch unworked.
        """
        search = ScriptedSearch(
            {"Velvet Crane": [{"url": "https://example.invalid/untitled"}],
             "Ivy Kingsley": [self.LEDGER]})
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)], search)

        self.assertEqual(self.ids(proposals), ["2"])
        # Reported as an error, not a mute: a malformed row is evidence about
        # the catalogue, not a verdict that the file is unidentifiable.
        self.assertEqual(self.store.muted_subjects(), set())
        self.assertTrue(any("KeyError: 'title'" in m for m in self.ctx.messages),
                        self.ctx.messages)
        self.assertEqual(
            self.ctx.message,
            "finished: 1 proposed, 0 muted, 0 refused, 1 errors, 2 lookups; selected 2 "
            "of 2 files (0 already proposed, 0 already muted, 0 outside the "
            "filter, 0 deferred)")

    def test_an_error_never_mutes_and_does_not_end_the_scan(self):
        """A lookup that raised is evidence about the network, not about the
        file. Muting on it would hide a file permanently because a socket
        blipped once, and no later run would ever revisit it."""
        search = ScriptedSearch({"Velvet Crane": TimeoutError("timed out"),
                                 "Ivy Kingsley": [self.LEDGER]})
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)], search)

        self.assertEqual(self.ids(proposals), ["2"])
        self.assertEqual(self.store.muted_subjects(), set())
        self.assertTrue(any("TimeoutError: timed out" in m
                            for m in self.ctx.messages), self.ctx.messages)

    def test_an_ambiguous_file_is_neither_proposed_nor_muted(self):
        """A tie means a human should look. Muting it would silently hide a
        file that is one glance from being resolved."""
        dawn = candidate("Morning Ritual Dawn", "morning-ritual-dawn")
        dusk = candidate("Morning Ritual Dusk", "morning-ritual-dusk")
        proposals = self.scan([scene(1, self.MORNING_PATH)],
                              ScriptedSearch({"Velvet Crane": [dawn, dusk]}))

        self.assertEqual(proposals, [])
        self.assertEqual(self.store.muted_subjects(), set())
        self.assertTrue(any("ambiguous" in m for m in self.ctx.messages),
                        self.ctx.messages)

    def test_a_refusal_is_recorded_with_its_reason_and_file(self):
        """Not muted and not thrown away either: a refusal is the one
        outcome a person can most often fix (rename a file, add an alias,
        move the threshold), and it is unactionable if it is invisible."""
        dawn = candidate("Morning Ritual Dawn", "morning-ritual-dawn")
        dusk = candidate("Morning Ritual Dusk", "morning-ritual-dusk")
        proposals = self.scan([scene(1, self.MORNING_PATH)],
                              ScriptedSearch({"Velvet Crane": [dawn, dusk]}))

        self.assertEqual(proposals, [])
        refusals = self.store.refusals()
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["subject_type"], SUBJECT_TYPE)
        self.assertEqual(refusals[0]["subject_id"], "1")
        self.assertEqual(refusals[0]["path"], self.MORNING_PATH)
        self.assertIn("ambiguous", refusals[0]["reason"])

    def test_a_later_proposal_for_the_same_file_clears_its_refusal(self):
        """A refusal is transient -- it stops being true the moment scoring
        clears the threshold -- so a subsequent scan that DOES propose the
        file must not leave the stale refusal sitting in the Refused
        section beside the proposal that resolved it.

        Run through the real `JobRunner` (`run_under_the_runner`), not the
        bare `self.scan()` helper: clearing a stale refusal happens inside
        `Store.record()`, and only the runner ever calls that -- `produce()`
        on its own only YIELDS a proposal, it never records one (see
        `ScanProducer`'s own docstring for why).
        """
        path = "/library/Velvet Crane/Harbour Lights.mp4"
        self.run_under_the_runner([scene(1, path)], ScriptedSearch(self.SCRIPT))
        self.assertEqual(len(self.store.refusals()), 1)

        self.run_under_the_runner([scene(1, path)], ScriptedSearch(self.SCRIPT),
                                  threshold=0.1)

        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(self.store.refusals(), [])

    def test_a_malformed_scene_costs_that_file_and_no_more(self):
        """`examine` raises on a scene with no file rather than muting it. The
        producer turns that into one file's error: the batch continues, and
        the malformed record is not quietly marked never-show-me-again."""
        search = ScriptedSearch(self.SCRIPT)
        proposals = self.scan([scene(1), scene(2, self.LEDGER_PATH)], search)

        self.assertEqual(self.ids(proposals), ["2"])
        self.assertEqual(self.store.muted_subjects(), set())
        self.assertTrue(any("ValueError" in m for m in self.ctx.messages),
                        self.ctx.messages)

    def test_a_malformed_alias_map_is_refused_before_a_producer_exists(self):
        """A duplicated alias line is a wiring mistake, wrong for every file.
        Reported once per file it looks like N transient failures; raised once
        it names the mistake where it was made — and costs no lookups.

        Raised at CONSTRUCTION, which is stricter than raising on the first
        line of the run and is the point of building the index there. The
        caller who wrote the map is still on the stack, no job has been
        started, and nothing has to read a traceback off a background thread
        to find out that a configuration line needs an edit.
        """
        search = ScriptedSearch(self.SCRIPT)

        with self.assertRaises(ValueError):
            self.build([scene(1, "/library/VC/Morning Ritual.mp4")], search,
                       aliases={"VC": "Velvet Crane", "v c": "Ivy Kingsley"})

        self.assertEqual(search.queries, [])
        self.assertEqual(self.stash.calls, [])

    def test_the_alias_index_is_built_once_for_the_whole_batch(self):
        """Not per file, which is what `resolve` does when it is handed a
        plain mapping.

        The map cannot change during a run, so re-normalising its keys for
        every file is work with no possible result: 642 us per file against a
        500-entry map on this machine, and 12.7 seconds across a 50,000-file
        scan against a 200-entry one. A timing assertion would be flaky and
        would not say what went wrong, so what is counted is the number of
        times the index is built — exactly once, for a batch of four files
        that all reach the resolver.
        """
        built = []
        real = cronicled.artist._alias_index

        def counting(mapping):
            built.append(mapping)
            return real(mapping)

        with mock.patch("cronicled.artist._alias_index", counting):
            proposals = self.scan(
                [scene(i, "/library/VC/Morning Ritual.mp4") for i in range(4)],
                ScriptedSearch(self.SCRIPT), aliases={"VC": "Velvet Crane"})

        self.assertEqual(len(proposals), 4, "every file reached the resolver")
        self.assertEqual(len(built), 1, "the index was built %d times for 4 "
                                        "files" % (len(built),))

    # -- what the caller's knobs reach ------------------------------------- #

    def test_the_threshold_reaches_the_decision(self):
        """The same file, refused at one threshold and proposed at another, so
        a hard-coded threshold cannot pass both halves."""
        path = "/library/Velvet Crane/Harbour Lights.mp4"

        refused = self.scan([scene(1, path)], ScriptedSearch(self.SCRIPT))
        proposed = self.scan([scene(1, path)], ScriptedSearch(self.SCRIPT),
                             threshold=0.1)

        self.assertEqual(refused, [])
        self.assertEqual([p["payload"]["candidate"] for p in proposed],
                         [self.EVENING])

    def test_the_aliases_reach_the_resolver(self):
        search = ScriptedSearch(self.SCRIPT)
        proposals = self.scan([scene(1, "/library/VC/Morning Ritual.mp4")],
                              search, aliases={"VC": "Velvet Crane"})

        self.assertEqual(search.queries, ["Velvet Crane"])
        self.assertEqual([p["payload"]["creator"]["source"] for p in proposals],
                         ["alias"])

    def test_the_censorship_map_reaches_every_files_scoring(self):
        """The constructor argument, not a per-`produce()` one — pinned at
        the batch level so a mutation that wires it into `__init__` but
        never forwards it to `_examine`/`examine` (or forwards it to only the
        first file) shows up here rather than only in `ExamineCensorshipTest`,
        which calls `examine` directly and cannot see `ScanProducer`'s own
        plumbing."""
        censored = candidate("K3strel Nightfall", "kestrel-nightfall")
        path = "/library/Velvet Crane/Kestrel Nightfall.mp4"
        search = ScriptedSearch({"Velvet Crane": [censored]})

        without = self.scan([scene(1, path)], search)
        with_map = self.scan([scene(1, path)], ScriptedSearch({"Velvet Crane": [censored]}),
                             censorship=CENSORSHIP)

        self.assertEqual(without, [])
        self.assertEqual(len(with_map), 1)
        self.assertEqual(with_map[0]["payload"]["score"], 1.0)
        # never the applied title, even threaded through the whole producer
        self.assertEqual(with_map[0]["payload"]["candidate"], censored)

    def test_the_enrich_callable_reaches_every_files_examine_call(self):
        """The same plumbing property `test_the_censorship_map_reaches_
        every_files_scoring` pins for `censorship`, run here for `enrich`:
        a mutation that wires it into `__init__` but never forwards it to
        `_examine`/`examine` would otherwise only show up in
        `ExamineEnrichmentTest`, which calls `examine` directly and cannot
        see `ScanProducer`'s own plumbing."""
        url = "https://example.invalid/clip/winter-ledger"
        fuller = scraped("Winter Ledger", url)
        enrich = FakeEnrich({url: fuller})

        proposals = self.scan([scene(7, self.LEDGER_PATH)],
                              ScriptedSearch(self.SCRIPT), enrich=enrich)

        self.assertEqual(proposals[0]["payload"]["candidate"], fuller)
        self.assertEqual(enrich.urls, [url])

    def test_one_worker_is_accepted(self):
        """The permissive side of the guard, pinned: a scan narrowed to a
        single worker is a legitimate instruction (a fragile catalogue, a
        rate limit), not a wiring mistake."""
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)],
            ScriptedSearch(self.SCRIPT), workers=1)

        self.assertEqual(self.ids(proposals), ["1", "2"])

    def test_a_pool_of_no_workers_is_refused(self):
        """It would do nothing at all, forever. Refused where the mistake was
        made rather than on a background thread hours later."""
        with self.assertRaises(ValueError):
            ScanProducer(FakeStash([]),
                        [Source(name="store", search=ScriptedSearch())],
                        store=self.store, workers=0)

    def test_no_sources_configured_is_refused_at_construction(self):
        """A scan with nothing to search against would mute or refuse every
        file for a reason that has nothing to do with any of them -- refused
        here, at construction, rather than discovered file by file."""
        with self.assertRaises(ValueError):
            ScanProducer(FakeStash([]), [], store=self.store)

    # -- multiple configured stores ----------------------------------------- #

    def test_every_configured_store_is_searched_for_every_file(self):
        """The production-level version of the same property
        `ExamineSourcesTest` pins directly: a real batch, through the real
        pool, still asks every configured store rather than stopping once
        one of them answers."""
        alpha = ScriptedSearch({"Velvet Crane": [], "Ivy Kingsley": []})
        beta = ScriptedSearch(self.SCRIPT)
        sources = [Source(name="alpha", search=alpha),
                  Source(name="beta", search=beta)]
        proposals = self.scan(
            [scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)],
            None, sources=sources)

        self.assertEqual(sorted(alpha.queries), ["Ivy Kingsley", "Velvet Crane"])
        self.assertEqual(sorted(beta.queries), ["Ivy Kingsley", "Velvet Crane"])
        self.assertEqual(self.ids(proposals), ["1", "2"])

    def test_a_winner_names_which_store_it_came_from(self):
        alpha = ScriptedSearch({"Velvet Crane": []})
        beta = ScriptedSearch(self.SCRIPT)
        sources = [Source(name="alpha", search=alpha),
                  Source(name="beta", search=beta)]
        proposals = self.scan([scene(1, self.MORNING_PATH)], None,
                              sources=sources)

        self.assertEqual(proposals[0]["payload"]["store"], "beta")

    def test_lookups_are_counted_per_store_not_shared_across_stores(self):
        """The multiplier the ticket asks to be measured: two files sharing
        one creator, searched against two stores, cost exactly two lookups
        (one per STORE, collapsed per creator within each) -- never one
        (which would mean the stores were wrongly sharing a cache) and
        never four (which would mean nothing collapsed the repeat file).

        `beta` is scripted empty for every query, deliberately: this test's
        subject is the LOOKUP COUNT, not the cross-store winner rule, so
        `alpha` is left to propose alone rather than tying with `beta` on
        an identical candidate (see `ExamineSourcesTest` for that rule's own
        tests) -- an empty search still counts as one real lookup, which is
        exactly what this test needs to measure.
        """
        alpha = ScriptedSearch({"Velvet Crane": [self.MORNING, self.EVENING]})
        beta = ScriptedSearch({})
        sources = [Source(name="alpha", search=alpha),
                  Source(name="beta", search=beta)]

        self.scan([scene(1, self.MORNING_PATH), scene(2, self.EVENING_PATH)],
                  None, sources=sources)

        self.assertEqual(alpha.queries, ["Velvet Crane"])
        self.assertEqual(beta.queries, ["Velvet Crane"])
        self.assertEqual(
            self.ctx.message,
            "finished: 2 proposed, 0 muted, 0 refused, 0 errors, 2 lookups; "
            "selected 2 of 2 files (0 already proposed, 0 already muted, "
            "0 outside the filter, 0 deferred)")

    def test_two_different_creators_against_two_stores_cost_four_lookups(self):
        """The other half of the same measurement: TWO distinct creators,
        each searched against BOTH stores, is four lookups -- the
        per-creator collapse must not also collapse across creators, and
        the per-store separation must not also merge the two stores."""
        alpha = ScriptedSearch(self.SCRIPT)
        beta = ScriptedSearch(self.SCRIPT)
        sources = [Source(name="alpha", search=alpha),
                  Source(name="beta", search=beta)]

        self.scan([scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH)],
                  None, sources=sources)

        self.assertEqual(sorted(alpha.queries), ["Ivy Kingsley", "Velvet Crane"])
        self.assertEqual(sorted(beta.queries), ["Ivy Kingsley", "Velvet Crane"])
        self.assertIn("4 lookups", self.ctx.message)

    # -- what the log says -------------------------------------------------- #

    def test_the_log_reports_the_batch_and_then_each_file_as_it_completes(self):
        """`skipped` on the job says "your earlier decision suppressed this";
        the scan's own log is what says how the batch was chosen, and it has
        to be honest that muted files were dropped rather than absent."""
        self.store.mute(SUBJECT_TYPE, "4")
        search = ScriptedSearch(self.SCRIPT)
        self.scan([scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH),
                   scene(3, self.EVENING_PATH), scene(4, self.MORNING_PATH),
                   scene(5, self.UNNAMED_PATH)],
                  search, name_filter="library", limit=2)

        self.assertEqual(
            self.ctx.messages[0],
            "selected 2 of 5 files (0 already proposed, 1 already muted, "
            "1 outside the filter, 1 deferred)")
        self.assertEqual([m.split(" ", 1)[0] for m in self.ctx.messages[1:3]],
                         ["1/2", "2/2"])
        # The last message, because the last message is the only one the
        # runner keeps — so the breakdown has to be in it, not only in the
        # opening line above.
        self.assertEqual(
            self.ctx.message,
            "finished: 2 proposed, 0 muted, 0 refused, 0 errors, 2 lookups; "
            "selected 2 of 5 files (0 already proposed, 1 already muted, "
            "1 outside the filter, 1 deferred)")

    def test_each_completed_file_is_logged_with_what_it_concluded(self):
        self.scan([scene(9, self.LEDGER_PATH)], ScriptedSearch(self.SCRIPT))

        self.assertEqual(self.ctx.messages, [
            "selected 1 of 1 files (0 already proposed, 0 already muted, "
            "0 outside the filter, 0 deferred)",
            "1/1 scene 9: chosen with score 1.000",
            "finished: 1 proposed, 0 muted, 0 refused, 0 errors, 1 lookups; "
            "selected 1 of 1 files (0 already proposed, 0 already muted, "
            "0 outside the filter, 0 deferred)",
        ])

    def test_the_closing_line_tells_a_suppressed_batch_from_an_empty_one(self):
        """Driven through the REAL runner, which keeps exactly one message.

        The spec's named risk: `skipped` on the job distinguishes "suppressed
        by your earlier decision" from "found nothing", and the scan must feed
        that honestly rather than reporting muted files as absent. It cannot
        feed `skipped` at all — a muted file is dropped in `select` and never
        yielded, so it never reaches `_record` — which leaves the job's
        message as the only place the difference can appear. `JobRunner._log`
        assigns `state.message`, one field with no history, so the CLOSING
        line is the whole of what a user reads off a finished job: an opening
        line that was honest is gone by then.

        Asserted through the runner and not through `FakeCtx` on purpose.
        This is the one property the double cannot stand in for, because
        keeping a history is exactly what the real collaborator does not do.
        """
        for subject_id in ("1", "2", "3"):
            self.store.mute(SUBJECT_TYPE, subject_id)
        suppressed = self.run_under_the_runner(
            [scene(1, self.MORNING_PATH), scene(2, self.EVENING_PATH),
             scene(3, self.LEDGER_PATH)], ScriptedSearch(self.SCRIPT))
        empty = self.run_under_the_runner([], ScriptedSearch(self.SCRIPT))

        self.assertEqual(
            suppressed.message,
            "finished: 0 proposed, 0 muted, 0 refused, 0 errors, 0 lookups; "
            "selected 0 of 3 files (0 already proposed, 3 already muted, "
            "0 outside the filter, 0 deferred)")
        self.assertEqual(
            empty.message,
            "finished: 0 proposed, 0 muted, 0 refused, 0 errors, 0 lookups; "
            "selected 0 of 0 files (0 already proposed, 0 already muted, "
            "0 outside the filter, 0 deferred)")
        # The residual, pinned rather than papered over: the two runs really
        # are identical in the counted fields, so the message is carrying the
        # whole of the distinction and a summary that dropped the breakdown
        # would leave nothing behind it.
        self.assertEqual(
            (suppressed.recorded, suppressed.skipped, suppressed.state),
            (empty.recorded, empty.skipped, empty.state))

    def test_the_closing_line_counts_every_kind_of_outcome(self):
        """Four outcomes, four counts. A summary that reported only proposals
        cannot tell a scan that found nothing from one whose catalogue was
        down."""
        search = ScriptedSearch({"Velvet Crane": TimeoutError("timed out"),
                                 "Ivy Kingsley": [],
                                 "Ada Whitlock": [self.MORNING, self.EVENING]})
        self.scan([scene(1, self.MORNING_PATH), scene(2, self.LEDGER_PATH),
                   scene(3, "/library/Ada Whitlock/Harbour Lights.mp4"),
                   scene(4, "/library/Ada Whitlock/Morning Ritual.mp4"),
                   scene(5, "/library/Ada Whitlock/Southern Crossing.mp4")],
                  search)

        # Four different counts, on purpose: with any two of them equal, two
        # counters swapped by an edit would report the same line.
        self.assertEqual(
            self.ctx.message,
            "finished: 1 proposed, 1 muted, 2 refused, 1 errors, 3 lookups; "
            "selected 5 of 5 files (0 already proposed, 0 already muted, "
            "0 outside the filter, 0 deferred)")


if __name__ == "__main__":
    unittest.main()
