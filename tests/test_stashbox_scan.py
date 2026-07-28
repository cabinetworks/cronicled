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
from cronicled.stashbox_scan import StashBoxCheckProducer
from cronicled.store import Store

WAIT = 10


def scene(scene_id, path):
    return {"id": str(scene_id), "files": [{"path": path}]}


class _FakeStash:
    def __init__(self, scenes):
        self._scenes = list(scenes)
        self.calls = []

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", limit))
        scenes = self._scenes if limit is None else self._scenes[:limit]
        return len(self._scenes), list(scenes)


class _FakeBox:
    """Scripted by performer id: `listings[performer_id]` is the
    `SourceListing` `performer_listing` answers with. A performer id asked
    for that was never scripted is a test bug, not a legitimate miss, so it
    raises rather than answering with something that would read as a real
    (if empty) source.
    """

    def __init__(self, listings):
        self._listings = dict(listings)
        self.calls = []

    def performer_listing(self, performer_id, per_page=None, max_pages=None,
                          timeout=None):
        self.calls.append(performer_id)
        return self._listings[performer_id]


def _listing(scenes=(), complete=True, performer_id="pf-1"):
    return SourceListing(performer_id, list(scenes), complete)


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

        fake.assert_called_once_with(
            box, "pf-1", "Morning Ritual.mp4", "Velvet Crane",
            Resolution(name="Velvet Crane", source="folder"),
            threshold=0.55, censorship={"x": ["y"]})


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

        job = runner.start(producer.name)
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

        job = runner.start(first.name)
        self.assertTrue(entered.wait(WAIT))
        try:
            with self.assertRaises(JobRejected):
                runner.start(second.name)
        finally:
            gate_open.set()
            runner.wait(job.id, WAIT)


if __name__ == "__main__":
    unittest.main()
