"""Choosing the batch a scan works on.

Every test here is about *ordering*: narrowing runs before the limit. The
scarce resource is a network lookup per selected file, so a limit spent on a
file that was never going to be proposed is a scan that appears to run and
achieves nothing.

No test touches a real filesystem. A path is a string; a scene is a dict; the
store is opened in memory.
"""
import unittest

from cronicled.scan import Counts, SUBJECT_TYPE, select
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


if __name__ == "__main__":
    unittest.main()
