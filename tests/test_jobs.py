"""The runner drives producers in the background so a long scan cannot block the
interface, and records what they yield as they yield it."""
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from cronicled.jobs import JobRunner, JobRejected
from cronicled.store import Store


class _Producer:
    """A producer under test. `gate` lets a test hold it mid-run; `boom` makes it
    fail after yielding some real work; `raises` overrides what it raises after
    that first yield (default `RuntimeError`, an ordinary `Exception`)."""

    def __init__(self, name="test-producer", cost="local", count=3,
                 gate=None, boom=False, raises=None):
        self.name, self.cost, self._count = name, cost, count
        self._gate, self._boom = gate, boom
        self._raises = raises or RuntimeError

    def produce(self, ctx):
        for i in range(self._count):
            if self._gate is not None:
                self._gate.wait(5)
            ctx.log("processing item %d" % i)
            yield {"folder": "f", "subject_type": "scene", "subject_id": str(i),
                   "summary": "proposal %d" % i, "payload": {"n": i}}
        if self._boom:
            raise self._raises("producer failed after yielding")


class _StartedInterrupted:
    """Stands in for a `Thread`'s private `_started` event so a test can hold
    open the window `Thread.start()` spends waiting on it.

    CPython's `Thread.start()` spawns the child and then blocks in
    `self._started.wait()`; the child sets `_started` from inside
    `_bootstrap_inner`, just before calling `run()`. This replacement raises
    out of that `wait()` (standing in for an interrupt arriving there) and
    parks the child at its `set()` until the test lets it go — so `_started`
    is genuinely unset, and `is_alive()` therefore genuinely False, for as
    long as the assertions need, with a real worker really spawned behind
    it."""

    def __init__(self):
        self._release = threading.Event()

    def is_set(self):
        return False

    def set(self):
        # the child, in _bootstrap_inner, immediately before run()
        self._release.wait(5)

    def wait(self, timeout=None):
        raise KeyboardInterrupt("interrupt delivered inside Thread.start()")

    def let_the_worker_run(self):
        self._release.set()


class _RunnerCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._dir, "s.db"))
        self.runner = JobRunner(self.store)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class RunningAJob(_RunnerCase):
    def test_start_returns_before_the_producer_finishes(self):
        # the whole point: a long scan must not block the caller
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate))
        job = self.runner.start("test-producer")
        self.assertEqual(job.state, "running")
        gate.set()
        self.runner.wait(job.id, timeout=5)

    def test_what_the_producer_yields_is_recorded(self):
        self.runner.register(_Producer(count=3))
        job = self.runner.start("test-producer")
        self.runner.wait(job.id, timeout=5)
        self.assertEqual(len(self.store.items()), 3)
        self.assertEqual(self.runner.job(job.id).recorded, 3)

    def test_progress_is_the_producers_own_log_line(self):
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate, count=2))
        job = self.runner.start("test-producer")
        gate.set()
        self.runner.wait(job.id, timeout=5)
        self.assertIn("processing item", self.runner.job(job.id).message)

    def test_a_finished_job_reports_done_with_a_finish_time(self):
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer")
        self.runner.wait(job.id, timeout=5)
        done = self.runner.job(job.id)
        self.assertEqual(done.state, "done")
        self.assertIsNotNone(done.finished_at)

    def test_starting_an_unregistered_producer_raises(self):
        with self.assertRaises(KeyError):
            self.runner.start("nosuchproducer")


class Duration(_RunnerCase):
    """Ticket #33: `started_at`/`finished_at` are whole-second timestamps, so
    a job that finishes inside the second it started (the ordinary case, not
    an edge case) has `started_at == finished_at` and no duration can be
    read off them. `duration` is measured separately, from a monotonic
    clock, so it survives exactly that case."""

    def test_a_running_job_has_no_duration_yet(self):
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate, count=1))
        job = self.runner.start("test-producer")
        self.assertIsNone(self.runner.job(job.id).duration)
        gate.set()
        self.runner.wait(job.id, timeout=5)

    def test_a_finished_job_reports_a_sub_second_duration(self):
        # A real run of this producer completes in well under a second, so
        # `started_at == finished_at` for it -- the exact case `duration`
        # exists to cover. Driven with a real clock (not mocked) so the
        # assertion is about the actual failure mode, not a value we handed
        # the code back to itself.
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer")
        self.runner.wait(job.id, timeout=5)
        done = self.runner.job(job.id)
        self.assertEqual(done.started_at, done.finished_at,
                         "the fixture is only interesting if this holds")
        self.assertIsNotNone(done.duration)
        self.assertGreaterEqual(done.duration, 0.0)
        self.assertLess(done.duration, 1.0)

    def test_duration_is_measured_from_a_monotonic_clock_not_the_timestamps(self):
        # Pins the mechanism, not just the outcome: two monotonic readings
        # 0.25s apart must produce a duration of 0.25, regardless of what
        # the (whole-second) wall-clock timestamps say.
        with mock.patch("cronicled.jobs.time.monotonic",
                        side_effect=[100.0, 100.25]):
            self.runner.register(_Producer(count=1))
            job = self.runner.start("test-producer")
            self.runner.wait(job.id, timeout=5)
        self.assertAlmostEqual(self.runner.job(job.id).duration, 0.25, places=6)

    def test_a_failed_job_still_reports_a_duration(self):
        self.runner.register(_Producer(count=1, boom=True))
        job = self.runner.start("test-producer")
        self.runner.wait(job.id, timeout=5)
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIsNotNone(failed.duration)
        self.assertGreaterEqual(failed.duration, 0.0)


class Failure(_RunnerCase):
    def test_a_failing_producer_marks_the_job_failed_with_the_error(self):
        self.runner.register(_Producer(count=2, boom=True))
        job = self.runner.start("test-producer")
        self.runner.wait(job.id, timeout=5)
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("producer failed", failed.error)

    def test_work_done_before_a_failure_is_kept(self):
        # partial progress is the normal outcome of interrupted work, not a
        # corruption to roll back
        self.runner.register(_Producer(count=2, boom=True))
        job = self.runner.start("test-producer")
        self.runner.wait(job.id, timeout=5)
        self.assertEqual(len(self.store.items()), 2)

    def test_a_failure_does_not_stop_later_jobs(self):
        self.runner.register(_Producer(name="bad", count=1, boom=True))
        self.runner.register(_Producer(name="good", count=1))
        bad = self.runner.start("bad")
        self.runner.wait(bad.id, timeout=5)
        good = self.runner.start("good")
        self.runner.wait(good.id, timeout=5)
        self.assertEqual(self.runner.job(good.id).state, "done")


class BaseExceptions(_RunnerCase):
    def test_a_keyboard_interrupt_still_fails_the_job_and_keeps_the_yield(self):
        # KeyboardInterrupt (and SystemExit, GeneratorExit) are BaseException,
        # not Exception: a bare `except Exception` lets one strand the job in
        # "running" forever, indistinguishable from a job still working.
        self.runner.register(
            _Producer(count=1, boom=True, raises=KeyboardInterrupt)
        )
        job = self.runner.start("test-producer")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("producer failed", failed.error)
        self.assertEqual(len(self.store.items()), 1)


class _BareFailure:
    """A producer whose failure carries no message at all: `str(exc)` is `''`,
    so a job recording only that has no record of the failure whatsoever."""

    name, cost = "bare", "local"

    def produce(self, ctx):
        raise RuntimeError()
        yield {}  # never reached; makes produce a generator function


class _ReturnsAList:
    """A producer that collects its proposals and returns them, losing the
    incremental recording the runner is built around."""

    name, cost = "listy", "local"

    def produce(self, ctx):
        return [{"folder": "f", "subject_type": "scene", "subject_id": "0",
                 "summary": "proposal", "payload": {}}]


class Diagnostics(_RunnerCase):
    def test_a_failure_with_no_message_still_names_its_type(self):
        # the worker swallows the exception and nothing logs it, so an error
        # of "" would leave a failed job that looks like nothing happened
        self.runner.register(_BareFailure())
        job = self.runner.start("bare")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("RuntimeError", failed.error)

    def test_a_failure_keeps_the_frames_that_say_where_it_happened(self):
        self.runner.register(_BareFailure())
        job = self.runner.start("bare")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        detail = self.runner.job(job.id).traceback
        self.assertIn("RuntimeError", detail)
        self.assertIn("produce", detail)

    def test_a_job_that_succeeds_carries_no_error_or_traceback(self):
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        done = self.runner.job(job.id)
        self.assertIsNone(done.error)
        self.assertIsNone(done.traceback)


class ProducerContract(_RunnerCase):
    def test_registering_a_name_twice_is_refused(self):
        # silently replacing would swap what the name runs, and its cost
        # class with it -- a wiring mistake, so it fails at wiring time
        self.runner.register(_Producer(name="scan", cost="scraping"))
        with self.assertRaises(ValueError):
            self.runner.register(_Producer(name="scan", cost="local"))
        job = self.runner.start("scan")
        self.assertEqual(job.cost, "scraping")
        self.assertTrue(self.runner.wait(job.id, timeout=5))

    def test_a_produce_that_returns_a_list_is_refused(self):
        # a list means the whole scan runs before anything is recorded, so a
        # scrape that dies partway through keeps nothing -- exactly what the
        # runner's yield-by-yield recording exists to prevent
        self.runner.register(_ReturnsAList())
        with self.assertRaises(TypeError):
            self.runner.start("listy")
        self.assertEqual(self.runner.jobs(), [])
        self.assertEqual(len(self.store.items()), 0)


class WhatIsRegistered(_RunnerCase):
    """A schedule reads a producer's own declared cadence off the object, so
    it needs the objects and not just their names."""

    def test_nothing_is_registered_to_begin_with(self):
        self.assertEqual(self.runner.producers(), [])

    def test_the_producers_come_back_in_the_order_they_were_registered(self):
        first = _Producer(name="alpha")
        second = _Producer(name="zulu")
        self.runner.register(first)
        self.runner.register(second)
        self.assertEqual(self.runner.producers(), [first, second])

    def test_the_answer_is_a_copy_so_a_caller_cannot_rewire_the_runner(self):
        producer = _Producer(name="alpha")
        self.runner.register(producer)
        self.runner.producers().clear()
        self.assertEqual(self.runner.producers(), [producer])


class CostClasses(_RunnerCase):
    def test_a_second_scraping_job_is_refused_while_one_runs(self):
        # two scrapes at once thrash the media server's headless browser and get
        # slower, not faster
        gate = threading.Event()
        self.runner.register(_Producer(name="scan-a", cost="scraping", gate=gate))
        self.runner.register(_Producer(name="scan-b", cost="scraping"))
        first = self.runner.start("scan-a")
        with self.assertRaises(JobRejected) as ctx:
            self.runner.start("scan-b")
        self.assertIn("scan-a", str(ctx.exception))
        gate.set()
        self.runner.wait(first.id, timeout=5)

    def test_a_local_job_runs_alongside_a_scraping_job(self):
        gate = threading.Event()
        self.runner.register(_Producer(name="scan", cost="scraping", gate=gate))
        self.runner.register(_Producer(name="tags", cost="local", count=1))
        scan = self.runner.start("scan")
        tags = self.runner.start("tags")          # must not be refused
        self.runner.wait(tags.id, timeout=5)
        self.assertEqual(self.runner.job(tags.id).state, "done")
        gate.set()
        self.runner.wait(scan.id, timeout=5)

    def test_many_local_jobs_run_together(self):
        for i in range(4):
            self.runner.register(_Producer(name="local-%d" % i, cost="local", count=1))
        started = [self.runner.start("local-%d" % i) for i in range(4)]
        for j in started:
            self.runner.wait(j.id, timeout=5)
        self.assertTrue(all(self.runner.job(j.id).state == "done" for j in started))

    def test_the_class_frees_up_when_the_job_finishes(self):
        self.runner.register(_Producer(name="scan", cost="scraping", count=1))
        first = self.runner.start("scan")
        self.runner.wait(first.id, timeout=5)
        second = self.runner.start("scan")        # must not be refused
        self.runner.wait(second.id, timeout=5)
        self.assertEqual(self.runner.job(second.id).state, "done")

    def test_the_class_frees_up_even_when_the_job_fails(self):
        # a crashed scrape that permanently blocks all future scrapes would be a
        # deadlock in slow motion
        self.runner.register(_Producer(name="scan", cost="scraping", count=1, boom=True))
        first = self.runner.start("scan")
        self.runner.wait(first.id, timeout=5)
        second = self.runner.start("scan")        # must not be refused
        self.runner.wait(second.id, timeout=5)
        # "scan" always booms, so its second run fails too, same as its
        # first — the property under test is that starting it was allowed
        # at all (the slot was freed) and that it reached a terminal state
        # rather than hanging, not that it magically succeeded.
        self.assertEqual(self.runner.job(second.id).state, "failed")

    def test_an_unknown_cost_class_is_refused_at_registration(self):
        with self.assertRaises(ValueError):
            self.runner.register(_Producer(cost="freeform"))

    def test_a_failed_thread_start_does_not_wedge_the_cost_class(self):
        # If Thread.start() itself raises (OS resource exhaustion is the
        # realistic cause), the thread never ran, so _run's `finally` never
        # executes to release the reservation. A rollback-free start() would
        # refuse every later start() for this class forever, citing a job
        # whose thread never began.
        self.runner.register(_Producer(name="scan", cost="scraping", count=1))
        with mock.patch(
            "threading.Thread.start", side_effect=OSError("thread start failed")
        ):
            with self.assertRaises(OSError):
                self.runner.start("scan")

        # the class must not be wedged: a following start() must be allowed
        # through and must run to completion, not be refused as saturated.
        second = self.runner.start("scan")
        self.assertTrue(self.runner.wait(second.id, timeout=5))
        self.assertEqual(self.runner.job(second.id).state, "done")

    def test_an_interrupt_inside_thread_start_leaves_the_slot_to_the_worker(self):
        # The window this covers is the one Thread.start() spends in
        # `self._started.wait()`: the child has been spawned, but it is the
        # child that sets `_started`, so until it does, `is_alive()` is
        # False for a worker that is already on its way. An interrupt
        # landing there escapes start() with a live worker behind it, and
        # rolling the reservation back would free the cost class for that
        # live worker -- letting a second job of the class run alongside
        # it, the exact concurrency COST_CLASS_LIMITS exists to prevent.
        #
        # Patching Thread.start to call the real start and *then* raise
        # cannot reach this: by then `_started` is set and the window is
        # over. So the test stands in for the thread's private `_started`
        # event instead, which puts it inside the real thing -- the real
        # start() really does spawn the child and really does raise out of
        # `_started.wait()`, with `_started` genuinely unset, which is what
        # makes `is_alive()` say False. The stand-in also parks the child
        # just before run(), so the window stays open for the assertions
        # rather than closing on a race.
        self.runner.register(_Producer(name="scan", cost="scraping", count=1))
        self.runner.register(_Producer(name="scan-2", cost="scraping", count=1))

        real_start = threading.Thread.start
        started = _StartedInterrupted()

        def interrupted_start(thread):
            thread._started = started
            real_start(thread)

        with mock.patch("threading.Thread.start", interrupted_start):
            with self.assertRaises(KeyboardInterrupt):
                self.runner.start("scan")

        try:
            # the worker exists, so the slot is its own `finally`'s to
            # release: the class must still be held, and the job must still
            # be visible rather than erased while its thread runs on.
            with self.assertRaises(JobRejected):
                self.runner.start("scan-2")
            first = [j for j in self.runner.jobs() if j.producer == "scan"][0]
            self.assertEqual(self.runner.job(first.id).state, "running")
        finally:
            started.let_the_worker_run()

        self.assertTrue(self.runner.wait(first.id, timeout=5))
        self.assertEqual(self.runner.job(first.id).state, "done")
        self.assertEqual(len(self.store.items()), 1)

    def test_a_race_of_starts_never_lets_two_scraping_jobs_through(self):
        # a check-then-start that is not atomic would pass the single-
        # threaded refusal test above and fail exactly when a scheduler and
        # a user click at the same moment: this drives many threads at
        # start() simultaneously and confirms every single one is refused,
        # never more than one job of the class ever running.
        gate = threading.Event()
        self.runner.register(_Producer(name="scan-a", cost="scraping", gate=gate))
        n = 20
        for i in range(n):
            self.runner.register(_Producer(name="scan-%d" % i, cost="scraping"))
        first = self.runner.start("scan-a")

        barrier = threading.Barrier(n)
        results = [None] * n

        def attempt(i):
            barrier.wait()
            try:
                job = self.runner.start("scan-%d" % i)
                results[i] = ("started", job)
            except JobRejected as exc:
                results[i] = ("rejected", str(exc))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        outcomes = [r[0] for r in results]
        self.assertEqual(outcomes.count("started"), 0)
        self.assertEqual(outcomes.count("rejected"), n)
        for kind, detail in results:
            self.assertEqual(kind, "rejected")
            self.assertIn("scan-a", detail)

        gate.set()
        self.runner.wait(first.id, timeout=5)
