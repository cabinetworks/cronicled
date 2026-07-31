"""`cronicled.stashbox_scan.StashBoxCheckProducer` is the wiring that turns
`cronicled.stashbox.check` from a function nothing calls into a runnable job.

No test here opens a socket: the media server is a small fake exposing only
`unorganized_scenes`, and stash-box is a fake exposing only
`performer_listing`, scripted by performer id. Nothing here re-proves
`cronicled.stashbox.check` or `cronicled.stashbox.listing_verdict` -- both are
pinned by mutation in `tests/test_stashbox.py` already. What is worth pinning
here is the ASSEMBLY: which files get checked at all, which creator name is
looked up, what happens when a mapping or a resolution is missing, that
nothing here is ever recorded as a proposal, and that the job this producer
runs under really does carry the `"box"` cost class.
"""
import unittest
from unittest import mock

from cronicled.artist import Resolution
from cronicled.jobs import COST_CLASS_LIMITS, JobRejected, JobRunner
from cronicled.scoring import DEFAULT_THRESHOLD
from cronicled.stashbox import SourceListing
from cronicled.stashbox_scan import StashBoxCheckProducer, _CachedListings
from cronicled.store import Store

WAIT = 10


def scene(scene_id, path, tags=()):
    return {"id": str(scene_id), "files": [{"path": path}], "tags": list(tags)}


class _FakeStash:
    """`organized` and `tag_ids` let a test build the population
    `cronicled.scan.pool_scenes` reaches: `unorganized_scenes` filters out
    anything in `organized`, exactly as the real media server does, and
    `tagged_scenes`/`tag_id_by_name` answer the marker reads `pool_scenes`
    makes when a marker is configured. A double that ignored `organized`
    could not tell a check that reaches only marked files from one that
    pools the whole library.
    """

    def __init__(self, scenes, organized=(), tag_ids=None):
        self._scenes = list(scenes)
        self._organized = {str(scene_id) for scene_id in organized}
        self._tag_ids = dict(tag_ids or {})
        self.calls = []

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", limit))
        scenes = [s for s in self._scenes if s["id"] not in self._organized]
        scenes = scenes if limit is None else scenes[:limit]
        return len(scenes), list(scenes)

    def tagged_scenes(self, tag_id, limit):
        self.calls.append(("tagged_scenes", tag_id, limit))
        scenes = [s for s in self._scenes
                 if any(tag["id"] == tag_id for tag in s.get("tags", ()))]
        scenes = scenes if limit is None else scenes[:limit]
        return len(scenes), list(scenes)

    def tag_id_by_name(self, name):
        self.calls.append(("tag_id_by_name", name))
        return self._tag_ids.get(name)


class _FakeBox:
    """Scripted by performer id: `listings[performer_id]` is the
    `SourceListing` `performer_listing` answers with. A performer id asked
    for that was never scripted is a test bug, not a legitimate miss, so it
    raises rather than answering with something that would read as a real
    (if empty) source.

    A scripted value may also be a `list`/`tuple` of listings -- a
    SEQUENCE, one answer per real call, holding the last once the sequence
    runs out. That is what lets a test tell a memoised second caller apart
    from a genuinely re-read one: a second REAL call to `performer_listing`
    for the same id gets a VISIBLY different listing, so a memo that quietly
    fails to collapse two files' reads is caught by a wrong verdict on the
    second file, not only by an extra entry in `calls`.
    """

    def __init__(self, listings):
        self._listings = dict(listings)
        self._served = {}
        self.calls = []

    def performer_listing(self, performer_id, per_page=None, max_pages=None,
                          timeout=None):
        self.calls.append(performer_id)
        scripted = self._listings[performer_id]
        if isinstance(scripted, (list, tuple)):
            index = self._served.get(performer_id, 0)
            self._served[performer_id] = index + 1
            return scripted[min(index, len(scripted) - 1)]
        return scripted


def _listing(scenes=(), complete=True, performer_id="pf-1", total=None,
             pages_read=1):
    scene_list = list(scenes)
    if total is None:
        total = len(scene_list)
    return SourceListing(performer_id, scene_list, complete,
                         total=total, pages_read=pages_read)


class _Ctx:
    def __init__(self):
        self.lines = []

    def log(self, message):
        self.lines.append(message)


class Selection(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_a_muted_subject_is_never_checked(self):
        stash = _FakeStash([scene(1, "/library/Velvet Crane/Morning Ritual.mp4")])
        self.store.mute("scene", "1", reason="muted from the inbox")
        box = _FakeBox({})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, [])
        self.assertTrue(any("selected 0 of 1" in line for line in ctx.lines))


class MarkerSelection(unittest.TestCase):
    """The population this check reaches when a marker tag is configured --
    the same one `cronicled.scan.ScanProducer`'s own `marker` reaches, read
    through the shared `cronicled.scan.pool_scenes` rather than a second
    copy of that selection (see `tests/test_scan.py`'s own marker section
    for the scan's half of this).

    Every assertion below is against the WHOLE set of performer ids actually
    queried (`box.calls`), never a count or a single expected id alone: a
    change that pooled every organized scene, not just the marked ones,
    would still put the marked scene's id in a count-only or
    contains-only assertion and pass regardless.
    """

    MARKER = "inferred-metadata"
    MARKER_TAG = {"id": "t-7", "name": MARKER}
    MARKER_TAG_IDS = {MARKER: "t-7"}
    OTHER_TAG = {"id": "t-3", "name": "shortlist"}

    ALPHA_PATH = "/library/Alpha Vale/Morning.mp4"
    BETA_PATH = "/library/Beta Wren/Evening.mp4"
    GAMMA_PATH = "/library/Gamma Kite/Night.mp4"

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _listing_for(self, *performer_ids):
        return _FakeBox({pid: _listing(scenes=[], complete=True)
                         for pid in performer_ids})

    def test_an_organized_scene_carrying_the_marker_is_checked(self):
        # The third scene is organized and carries a DIFFERENT tag, so
        # passing this test cannot be done by pooling everything organized.
        stash = _FakeStash(
            [scene(1, self.ALPHA_PATH),
             scene(2, self.BETA_PATH, tags=[self.MARKER_TAG]),
             scene(3, self.GAMMA_PATH, tags=[self.OTHER_TAG])],
            organized=("2", "3"), tag_ids=self.MARKER_TAG_IDS)
        box = self._listing_for("pf-1", "pf-2")
        producer = StashBoxCheckProducer(
            stash, box,
            {"Alpha Vale": "pf-1", "Beta Wren": "pf-2", "Gamma Kite": "pf-3"},
            store=self.store, marker=self.MARKER)

        list(producer.produce(_Ctx()))

        self.assertEqual(box.calls, ["pf-1", "pf-2"])

    def test_an_organized_scene_without_the_marker_is_not_checked(self):
        # The guard, on its own fixture: a marker is configured and
        # resolves, and the organized file that does not carry it is still
        # never offered. Without this, pooling all organized scenes passes
        # the test above too.
        stash = _FakeStash(
            [scene(1, self.ALPHA_PATH), scene(2, self.BETA_PATH)],
            organized=("2",), tag_ids=self.MARKER_TAG_IDS)
        box = self._listing_for("pf-1")
        producer = StashBoxCheckProducer(
            stash, box, {"Alpha Vale": "pf-1", "Beta Wren": "pf-2"},
            store=self.store, marker=self.MARKER)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, ["pf-1"])
        self.assertEqual(
            ctx.lines[0],
            "selected 1 of 1 files for a stash-box check; 0 of the 1 "
            "offered only because they carry the marker tag "
            "'inferred-metadata', resolving to 0 distinct creator(s)")

    def test_with_no_marker_configured_the_marked_scene_stays_invisible(self):
        # Absent configuration is a legitimate state and behaves exactly as
        # it did before this ticket: the marked organized file is invisible
        # and the opening line carries no marker clause at all.
        stash = _FakeStash(
            [scene(1, self.ALPHA_PATH),
             scene(2, self.BETA_PATH, tags=[self.MARKER_TAG])],
            organized=("2",), tag_ids=self.MARKER_TAG_IDS)
        box = self._listing_for("pf-1")
        producer = StashBoxCheckProducer(
            stash, box, {"Alpha Vale": "pf-1", "Beta Wren": "pf-2"},
            store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, ["pf-1"])
        self.assertEqual(ctx.lines[0],
                         "selected 1 of 1 files for a stash-box check")

    def test_the_marked_population_is_reported_with_its_distinct_creators(self):
        # Two of the three marked files share a creator, so the distinct
        # count must be 2, not 3 -- a file count would mislead about what
        # this actually costs (see the module docstring on why the count
        # of listing reads is not the same thing).
        stash = _FakeStash(
            [scene(1, self.ALPHA_PATH),
             scene(2, self.BETA_PATH, tags=[self.MARKER_TAG]),
             scene(3, "/library/Beta Wren/Noon.mp4", tags=[self.MARKER_TAG]),
             scene(4, self.GAMMA_PATH, tags=[self.MARKER_TAG])],
            organized=("2", "3", "4"), tag_ids=self.MARKER_TAG_IDS)
        box = self._listing_for("pf-1", "pf-2", "pf-3")
        producer = StashBoxCheckProducer(
            stash, box,
            {"Alpha Vale": "pf-1", "Beta Wren": "pf-2", "Gamma Kite": "pf-3"},
            store=self.store, marker=self.MARKER)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(
            ctx.lines[0],
            "selected 4 of 4 files for a stash-box check; 3 of the 4 "
            "offered only because they carry the marker tag "
            "'inferred-metadata', resolving to 2 distinct creator(s)")

    def test_an_unresolvable_marked_scene_is_not_counted_as_a_creator(self):
        # A date-shaped folder resolves to no creator at all (see
        # `CreatorResolution.test_an_unresolved_creator_is_skipped_and_logged`
        # below for the same fixture shape) -- it must not inflate the
        # distinct-creator count the way counting `None` as a "creator"
        # would.
        stash = _FakeStash(
            [scene(1, self.ALPHA_PATH),
             scene(2, self.BETA_PATH, tags=[self.MARKER_TAG]),
             scene(3, "/2023 September 11/clip.mp4", tags=[self.MARKER_TAG])],
            organized=("2", "3"), tag_ids=self.MARKER_TAG_IDS)
        box = self._listing_for("pf-1", "pf-2")
        producer = StashBoxCheckProducer(
            stash, box, {"Alpha Vale": "pf-1", "Beta Wren": "pf-2"},
            store=self.store, marker=self.MARKER)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(
            ctx.lines[0],
            "selected 3 of 3 files for a stash-box check; 2 of the 3 "
            "offered only because they carry the marker tag "
            "'inferred-metadata', resolving to 1 distinct creator(s)")

    def test_a_marker_naming_no_tag_on_this_server_refuses(self):
        # `pool_scenes` (shared with the scan) raises for a marker naming no
        # tag rather than silently pooling nothing extra -- this producer
        # must not swallow that.
        stash = _FakeStash([scene(1, self.ALPHA_PATH)])
        producer = StashBoxCheckProducer(
            stash, _FakeBox({}), {"Alpha Vale": "pf-1"}, store=self.store,
            marker="no-such-tag")

        with self.assertRaises(ValueError):
            list(producer.produce(_Ctx()))


class CreatorResolution(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_an_unresolved_creator_is_skipped_and_logged(self):
        # A date-shaped folder is not a creator, and the filename has no
        # dash or `feat` marker -- nothing here names anyone.
        stash = _FakeStash([scene(1, "/2023 September 11/clip.mp4")])
        box = _FakeBox({})
        producer = StashBoxCheckProducer(stash, box, {}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, [])
        self.assertTrue(any("no creator resolved" in line for line in ctx.lines))
        self.assertTrue(any("finished: checked 0" in line and "1 skipped" in line
                           for line in ctx.lines))

    def test_a_resolved_creator_with_no_known_performer_id_is_skipped(self):
        stash = _FakeStash([scene(1, "/library/Velvet Crane/Morning Ritual.mp4")])
        box = _FakeBox({})
        producer = StashBoxCheckProducer(stash, box, {}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, [])
        self.assertTrue(any("no stash-box performer id" in line
                           for line in ctx.lines))
        self.assertTrue(any("'Velvet Crane'" in line for line in ctx.lines))

    def test_a_contested_folder_and_filename_still_checks_the_folders_pick(self):
        # No `owners_of` is ever available here (see the module docstring),
        # so this resolves on the plain folder-wins default: "Velvet Crane"
        # wins, "Ivy Thorn" is reported as `competing`, and the eventual
        # verdict must be downgraded rather than treated as settled.
        path = "/library/Velvet Crane/Ivy Thorn - Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "A Totally Different Scene"}],
            complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, ["pf-1"])
        self.assertTrue(any("different creators" in line for line in ctx.lines))
        self.assertTrue(any("1 inconclusive" in line for line in ctx.lines))


class CheckCallShape(unittest.TestCase):
    """`cronicled.stashbox.check` is the function that actually reads a
    listing and scores it -- pinned by mutation in `tests/test_stashbox.py`
    already. What is worth pinning here, independently of any score a fake
    listing happens to produce, is that THIS producer calls it with the
    file's own name and folder in the order `check` documents, and with the
    resolution it just computed -- an argument swap here would still often
    score similarly (see `scoring.score`'s asymmetric treatment of name vs.
    folder is mild for many real inputs) and so is not safely caught by a
    behavioural fixture alone.

    The `box` argument itself is no longer the object a caller handed the
    producer: `produce` now wraps it in `_CachedListings` (see
    `MemoisedListing` below for the memo's own behaviour), so what reaches
    `check` is that wrapper. This still confirms it wraps the SAME box the
    producer was given, rather than a disconnected substitute.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_the_files_own_name_and_folder_and_resolution_reach_check(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store,
            threshold=0.55, censorship={"x": ["y"]})

        with mock.patch("cronicled.stashbox_scan.stashbox_check") as fake:
            fake.return_value = mock.Mock(unlisted=True, reason="r")
            list(producer.produce(_Ctx()))

        fake.assert_called_once()
        called_box, *rest = fake.call_args.args
        self.assertIsInstance(called_box, _CachedListings)
        self.assertIs(called_box._box, box)
        self.assertEqual(
            tuple(rest),
            ("pf-1", "Morning Ritual.mp4", "Velvet Crane",
             Resolution(name="Velvet Crane", source="folder")))
        self.assertEqual(fake.call_args.kwargs,
                         {"threshold": 0.55, "censorship": {"x": ["y"]}})


class CachedListingsTest(unittest.TestCase):
    """`_CachedListings` in isolation, against a bare `mock.Mock` box rather
    than through the whole producer -- what `MemoisedListing` below pins
    end-to-end, this pins at the seam itself.

    `per_page`/`max_pages`/`timeout` are given three DIFFERENT values here on
    purpose: stash-box's own `PER_PAGE` and `MAX_PAGES` defaults happen to
    both be 100 (see `cronicled.stashbox`), so a call shape built from those
    two real defaults cannot tell a transposed pair apart, and neither can a
    `mock.ANY`-based assertion elsewhere. Distinct values here are what make
    a transposition a wrong call, not an accidentally-equivalent one.
    """

    def test_the_three_paging_arguments_reach_the_real_box_unchanged(self):
        box = mock.Mock()
        box.performer_listing.return_value = _listing(scenes=[], complete=True)
        cached = _CachedListings(box)

        cached.performer_listing("pf-1", per_page=7, max_pages=13, timeout=9)

        box.performer_listing.assert_called_once_with(
            "pf-1", per_page=7, max_pages=13, timeout=9)

    def test_a_second_call_for_the_same_id_answers_from_the_first(self):
        box = mock.Mock()
        listing = _listing(scenes=[], complete=True)
        box.performer_listing.return_value = listing
        cached = _CachedListings(box)

        first = cached.performer_listing("pf-1")
        second = cached.performer_listing("pf-1")

        box.performer_listing.assert_called_once()
        self.assertIs(first, second)
        self.assertIs(first, listing)

    def test_a_different_id_still_reads_the_real_box(self):
        box = mock.Mock()
        box.performer_listing.side_effect = [
            _listing(scenes=[], complete=True, performer_id="pf-1"),
            _listing(scenes=[], complete=True, performer_id="pf-2"),
        ]
        cached = _CachedListings(box)

        cached.performer_listing("pf-1")
        cached.performer_listing("pf-2")

        self.assertEqual(box.performer_listing.call_count, 2)

    def test_a_raised_read_is_reraised_not_turned_into_an_empty_listing(self):
        box = mock.Mock()
        box.performer_listing.side_effect = RuntimeError("connection reset")
        cached = _CachedListings(box)

        with self.assertRaises(RuntimeError):
            cached.performer_listing("pf-1")
        with self.assertRaises(RuntimeError):
            cached.performer_listing("pf-1")

        box.performer_listing.assert_called_once()


class MemoisedListing(unittest.TestCase):
    """`produce` now reads a creator's whole listing AT MOST ONCE per run,
    through `_CachedListings` -- see that class's own docstring, and this
    module's, for why that is a correctness property (two files judged
    against the SAME paged read) at least as much as a cost one.

    Every test here is scripted so a memo that fails in one of its specific
    ways is caught by a WRONG VERDICT, not only by an extra entry in
    `box.calls`: a read count alone cannot tell a collapsed cache from one
    that quietly re-reads and hands the second caller a different view --
    see `_FakeBox`'s own docstring for how the fixture makes that visible.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_two_files_by_one_creator_cause_one_listing_read(self):
        path_a = "/library/Velvet Crane/Morning Ritual.mp4"
        path_b = "/library/Velvet Crane/Evening Walk.mp4"
        stash = _FakeStash([scene(1, path_a), scene(2, path_b)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "Morning Ritual"}], complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)

        list(producer.produce(_Ctx()))

        self.assertEqual(box.calls, ["pf-1"])

    def test_two_files_by_different_creators_cause_two_reads(self):
        # The two listings are visibly different -- each names only its OWN
        # file's title -- so a memo keyed too loosely (collapsing the two
        # creators onto one cache entry) shows up as a WRONG verdict on
        # whichever file was judged against the other creator's listing,
        # not only as a missing call.
        path_a = "/library/Velvet Crane/Morning Ritual.mp4"
        path_b = "/library/Ivy Thorn/Night Market.mp4"
        stash = _FakeStash([scene(1, path_a), scene(2, path_b)])
        box = _FakeBox({
            "pf-1": _listing(scenes=[{"id": "s1", "title": "Morning Ritual"}],
                             complete=True, performer_id="pf-1"),
            "pf-2": _listing(scenes=[{"id": "s2", "title": "Night Market"}],
                             complete=True, performer_id="pf-2"),
        })
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1", "Ivy Thorn": "pf-2"},
            store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(sorted(box.calls), ["pf-1", "pf-2"])
        self.assertTrue(any(
            "checked 2, 0 unlisted, 2 present, 0 inconclusive, 0 skipped" in line
            for line in ctx.lines))

    def test_two_files_by_one_creator_are_judged_against_the_same_listing(self):
        # The SECOND scripted listing is reachable only if the memo fails to
        # collapse the second file's read: it names that file's own title,
        # which the first (correctly shared) listing does not. A memo that
        # quietly re-reads for the second file would turn its verdict from
        # "unlisted" into "present" -- a wrong answer, not merely an extra
        # read -- so this checks WHICH listing both files were judged
        # against, directly, rather than only how many reads happened.
        path_a = "/library/Velvet Crane/Morning Ritual.mp4"
        path_b = "/library/Velvet Crane/Evening Walk.mp4"
        stash = _FakeStash([scene(1, path_a), scene(2, path_b)])
        first_listing = _listing(
            scenes=[{"id": "s1", "title": "Morning Ritual"}], complete=True)
        second_listing = _listing(
            scenes=[{"id": "s2", "title": "Evening Walk"}], complete=True)
        box = _FakeBox({"pf-1": [first_listing, second_listing]})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertEqual(box.calls, ["pf-1"])
        self.assertTrue(any(
            "checked 2, 1 unlisted, 1 present, 0 inconclusive, 0 skipped" in line
            for line in ctx.lines))

    def test_the_memo_does_not_survive_a_second_run(self):
        # The second run's own listing is scripted to disagree with the
        # first's, so a memo that wrongly survives past the run it was built
        # for is caught by the SECOND run reporting the FIRST run's verdict
        # again, not merely by a missing second call.
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        first_listing = _listing(
            scenes=[{"id": "s1", "title": "Morning Ritual"}], complete=True)
        second_listing = _listing(
            scenes=[{"id": "s2", "title": "A Totally Different Scene"}],
            complete=True)
        box = _FakeBox({"pf-1": [first_listing, second_listing]})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)

        list(producer.produce(_Ctx()))
        second_ctx = _Ctx()
        list(producer.produce(second_ctx))

        self.assertEqual(box.calls, ["pf-1", "pf-1"])
        self.assertTrue(any(
            "checked 1, 1 unlisted, 0 present, 0 inconclusive, 0 skipped" in line
            for line in second_ctx.lines))

    def test_a_failed_read_is_not_memoised_as_an_empty_listing(self):
        # Uncertainty may withhold evidence, never supply it: a transient
        # failure must never be read back as a confirmed-empty listing, for
        # either file that shares the creator it failed for.
        class _FailingBox:
            def __init__(self):
                self.calls = []

            def performer_listing(self, performer_id, per_page=None,
                                  max_pages=None, timeout=None):
                self.calls.append(performer_id)
                raise RuntimeError("connection reset")

        path_a = "/library/Velvet Crane/Morning Ritual.mp4"
        path_b = "/library/Velvet Crane/Evening Walk.mp4"
        stash = _FakeStash([scene(1, path_a), scene(2, path_b)])
        box = _FailingBox()
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        # ONE real attempt for the shared creator: the failure is cached and
        # re-raised to the second file rather than retried within the run.
        self.assertEqual(box.calls, ["pf-1"])
        # Neither file may read as "unlisted" (nor "present"): an error is
        # evidence about the network, not a confirmed-empty listing, so both
        # are skipped and neither tallies as a verdict.
        self.assertTrue(any(
            "checked 0, 0 unlisted, 0 present, 0 inconclusive, 2 skipped" in line
            for line in ctx.lines))
        self.assertEqual(
            sum("RuntimeError: connection reset" in line for line in ctx.lines),
            2)


class Tally(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_an_unlisted_verdict_is_tallied_and_logged(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "A Totally Different Scene"}],
            complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertTrue(any("in full" in line for line in ctx.lines))
        self.assertTrue(any(
            "checked 1, 1 unlisted, 0 present, 0 inconclusive, 0 skipped" in line
            for line in ctx.lines))

    def test_a_present_verdict_is_tallied_separately(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "Morning Ritual"}], complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertTrue(any(
            "checked 1, 0 unlisted, 1 present, 0 inconclusive, 0 skipped" in line
            for line in ctx.lines))

    def test_an_inconclusive_verdict_from_an_incomplete_read_is_tallied_separately(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "A Totally Different Scene"}],
            complete=False)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertTrue(any(
            "checked 1, 0 unlisted, 0 present, 1 inconclusive, 0 skipped" in line
            for line in ctx.lines))

    def test_a_malformed_scene_is_isolated_from_the_rest_of_the_batch(self):
        good = scene(2, "/library/Velvet Crane/Morning Ritual.mp4")
        broken = {"id": "1", "files": []}
        stash = _FakeStash([broken, good])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "Morning Ritual"}], complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        ctx = _Ctx()

        list(producer.produce(ctx))

        self.assertTrue(any("ValueError" in line for line in ctx.lines))
        self.assertTrue(any(
            "checked 1, 0 unlisted, 1 present, 0 inconclusive, 1 skipped" in line
            for line in ctx.lines))


class NeverAProposal(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_the_producer_never_yields_anything(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "A Totally Different Scene"}],
            complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)

        yielded = list(producer.produce(_Ctx()))

        self.assertEqual(yielded, [])


class JobRunnerWiring(unittest.TestCase):
    """The whole point of a dedicated producer is the cost class it runs
    under -- see the module docstring. This confirms it actually IS "box",
    not merely that the attribute happens to hold that string: a second job
    of this same class is refused while one is running, exactly the
    protection `cronicled.scan.ScanProducer`'s own `"scraping"` gets, and
    exactly the isolation from it that this module exists for.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_declares_the_box_cost_class(self):
        producer = StashBoxCheckProducer(
            _FakeStash([]), _FakeBox({}), {}, store=self.store)
        self.assertEqual(producer.cost, "box")
        self.assertIn("box", COST_CLASS_LIMITS)

    def test_runs_to_completion_recording_nothing(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        stash = _FakeStash([scene(1, path)])
        box = _FakeBox({"pf-1": _listing(
            scenes=[{"id": "s1", "title": "Morning Ritual"}], complete=True)})
        producer = StashBoxCheckProducer(
            stash, box, {"Velvet Crane": "pf-1"}, store=self.store)
        runner = JobRunner(self.store)
        runner.register(producer)

        job = runner.start(producer.name, trigger="manual")
        self.assertTrue(runner.wait(job.id, WAIT))
        finished = runner.job(job.id)

        self.assertEqual(finished.state, "done")
        self.assertEqual(finished.cost, "box")
        self.assertEqual(finished.recorded, 0)
        self.assertEqual(self.store.items(folder="library"), [])

    def test_a_second_box_job_is_rejected_while_one_is_running(self):
        gate_open = __import__("threading").Event()
        entered = __import__("threading").Event()

        class _SlowStash(_FakeStash):
            def unorganized_scenes(self, limit):
                entered.set()
                gate_open.wait(WAIT)
                return super().unorganized_scenes(limit)

        first = StashBoxCheckProducer(
            _SlowStash([]), _FakeBox({}), {}, store=self.store)
        first.name = "stashbox-check-first"
        second = StashBoxCheckProducer(
            _FakeStash([]), _FakeBox({}), {}, store=self.store)
        second.name = "stashbox-check-second"
        runner = JobRunner(self.store)
        runner.register(first)
        runner.register(second)

        job = runner.start(first.name, trigger="manual")
        self.assertTrue(entered.wait(WAIT))
        try:
            with self.assertRaises(JobRejected):
                runner.start(second.name, trigger="manual")
        finally:
            gate_open.set()
            runner.wait(job.id, WAIT)


if __name__ == "__main__":
    unittest.main()
