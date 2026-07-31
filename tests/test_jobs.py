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
        job = self.runner.start("test-producer", trigger="manual")
        self.assertEqual(job.state, "running")
        gate.set()
        self.runner.wait(job.id, timeout=5)

    def test_what_the_producer_yields_is_recorded(self):
        self.runner.register(_Producer(count=3))
        job = self.runner.start("test-producer", trigger="manual")
        self.runner.wait(job.id, timeout=5)
        self.assertEqual(len(self.store.items()), 3)
        self.assertEqual(self.runner.job(job.id).recorded, 3)

    def test_progress_is_the_producers_own_log_line(self):
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate, count=2))
        job = self.runner.start("test-producer", trigger="manual")
        gate.set()
        self.runner.wait(job.id, timeout=5)
        self.assertIn("processing item", self.runner.job(job.id).message)

    def test_a_finished_job_reports_done_with_a_finish_time(self):
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer", trigger="manual")
        self.runner.wait(job.id, timeout=5)
        done = self.runner.job(job.id)
        self.assertEqual(done.state, "done")
        self.assertIsNotNone(done.finished_at)

    def test_starting_an_unregistered_producer_raises(self):
        with self.assertRaises(KeyError):
            self.runner.start("nosuchproducer", trigger="manual")


class Duration(_RunnerCase):
    """Ticket #33: `started_at`/`finished_at` are whole-second timestamps, so
    a job that finishes inside the second it started (the ordinary case, not
    an edge case) has `started_at == finished_at` and no duration can be
    read off them. `duration` is measured separately, from a monotonic
    clock, so it survives exactly that case."""

    def test_a_running_job_has_no_duration_yet(self):
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate, count=1))
        job = self.runner.start("test-producer", trigger="manual")
        self.assertIsNone(self.runner.job(job.id).duration)
        gate.set()
        self.runner.wait(job.id, timeout=5)

    def test_a_finished_job_reports_a_sub_second_duration(self):
        # The ordinary case, not an edge case: a job that finishes inside the
        # second it started, so `started_at == finished_at` -- exactly what
        # `duration` exists to cover. A real run only produces that
        # combination when the machine is fast enough to clear both `_now()`
        # calls inside one wall-clock second, which is not true under load
        # (this flaked once in a stress run, off by one second). Both clocks
        # are injected instead, so the scenario holds by construction and the
        # recorded duration is an exact value, not a threshold a slow run
        # could trip.
        with mock.patch("cronicled.jobs._now",
                         return_value="2026-07-27T00:00:00+00:00"), \
             mock.patch("cronicled.jobs.time.monotonic",
                        side_effect=[200.0, 200.4]):
            self.runner.register(_Producer(count=1))
            job = self.runner.start("test-producer", trigger="manual")
            self.runner.wait(job.id, timeout=5)
        done = self.runner.job(job.id)
        # Confirms the runner stores what `_now()` handed it rather than a
        # second, independently-timed call to the real wall clock -- a
        # regression there would break this even with `_now()` pinned.
        self.assertEqual(done.started_at, done.finished_at,
                         "both timestamps come from the same pinned clock")
        self.assertAlmostEqual(done.duration, 0.4, places=6)

    def test_duration_is_measured_from_a_monotonic_clock_not_the_timestamps(self):
        # Pins the mechanism, not just the outcome: two monotonic readings
        # 0.25s apart must produce a duration of 0.25, regardless of what
        # the (whole-second) wall-clock timestamps say.
        with mock.patch("cronicled.jobs.time.monotonic",
                        side_effect=[100.0, 100.25]):
            self.runner.register(_Producer(count=1))
            job = self.runner.start("test-producer", trigger="manual")
            self.runner.wait(job.id, timeout=5)
        self.assertAlmostEqual(self.runner.job(job.id).duration, 0.25, places=6)

    def test_a_failed_job_still_reports_a_duration(self):
        self.runner.register(_Producer(count=1, boom=True))
        job = self.runner.start("test-producer", trigger="manual")
        self.runner.wait(job.id, timeout=5)
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIsNotNone(failed.duration)
        self.assertGreaterEqual(failed.duration, 0.0)


class Failure(_RunnerCase):
    def test_a_failing_producer_marks_the_job_failed_with_the_error(self):
        self.runner.register(_Producer(count=2, boom=True))
        job = self.runner.start("test-producer", trigger="manual")
        self.runner.wait(job.id, timeout=5)
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("producer failed", failed.error)

    def test_work_done_before_a_failure_is_kept(self):
        # partial progress is the normal outcome of interrupted work, not a
        # corruption to roll back
        self.runner.register(_Producer(count=2, boom=True))
        job = self.runner.start("test-producer", trigger="manual")
        self.runner.wait(job.id, timeout=5)
        self.assertEqual(len(self.store.items()), 2)

    def test_a_failure_does_not_stop_later_jobs(self):
        self.runner.register(_Producer(name="bad", count=1, boom=True))
        self.runner.register(_Producer(name="good", count=1))
        bad = self.runner.start("bad", trigger="manual")
        self.runner.wait(bad.id, timeout=5)
        good = self.runner.start("good", trigger="manual")
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
        job = self.runner.start("test-producer", trigger="manual")
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
        job = self.runner.start("bare", trigger="manual")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        failed = self.runner.job(job.id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("RuntimeError", failed.error)

    def test_a_failure_keeps_the_frames_that_say_where_it_happened(self):
        self.runner.register(_BareFailure())
        job = self.runner.start("bare", trigger="manual")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        detail = self.runner.job(job.id).traceback
        self.assertIn("RuntimeError", detail)
        self.assertIn("produce", detail)

    def test_a_job_that_succeeds_carries_no_error_or_traceback(self):
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer", trigger="manual")
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
        job = self.runner.start("scan", trigger="manual")
        self.assertEqual(job.cost, "scraping")
        self.assertTrue(self.runner.wait(job.id, timeout=5))

    def test_a_produce_that_returns_a_list_is_refused(self):
        # a list means the whole scan runs before anything is recorded, so a
        # scrape that dies partway through keeps nothing -- exactly what the
        # runner's yield-by-yield recording exists to prevent
        self.runner.register(_ReturnsAList())
        with self.assertRaises(TypeError):
            self.runner.start("listy", trigger="manual")
        self.assertEqual(self.runner.jobs(), [])
        self.assertEqual(len(self.store.items()), 0)


class Reregistering(_RunnerCase):
    """`register()`'s own docstring reserves a separate method for a caller
    that genuinely wants to replace a registration -- this is it. The
    caller it exists for is a producer rebuilt fresh per run with its own
    parameter baked in at construction (a scan's `limit`), started under one
    fixed name rather than a name invented per call that would otherwise
    accumulate in the registry forever."""

    def test_replaces_the_producer_registered_under_the_same_name(self):
        first = _Producer(name="scan", cost="local", count=1)
        second = _Producer(name="scan", cost="local", count=1)
        self.runner.register(first)
        self.runner.reregister(second)
        self.assertEqual(self.runner.producers(), [second])

    def test_a_name_never_registered_before_works_the_same_as_register(self):
        producer = _Producer(name="scan", cost="local")
        self.runner.reregister(producer)   # must not raise
        self.assertEqual(self.runner.producers(), [producer])

    def test_the_cost_class_can_change_on_a_deliberate_replace(self):
        # Unlike `register`, which refuses a duplicate name specifically so
        # a cost class cannot swap by accident -- this is the caller opting
        # in on purpose, so a genuine change of cost class is not refused.
        self.runner.register(_Producer(name="scan", cost="local"))
        self.runner.reregister(_Producer(name="scan", cost="scraping"))
        self.assertEqual(self.runner.producers()[0].cost, "scraping")

    def test_an_unknown_cost_class_is_still_refused(self):
        self.runner.register(_Producer(name="scan", cost="local"))
        with self.assertRaises(ValueError):
            self.runner.reregister(_Producer(name="scan", cost="nonsense"))

    def test_a_job_already_running_under_the_old_producer_is_unaffected(self):
        # The registry entry is swapped, but a job already in flight was
        # handed its own producer and generator directly by `start()` --
        # nothing here can reach into it.
        gate = threading.Event()
        old = _Producer(name="scan", cost="local", count=1, gate=gate)
        self.runner.register(old)
        job = self.runner.start("scan", trigger="manual")
        self.runner.reregister(_Producer(name="scan", cost="local", count=1))
        gate.set()
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        finished = self.runner.job(job.id)
        self.assertEqual(finished.state, "done")
        self.assertEqual(finished.recorded, 1)

    def test_starting_after_a_replace_runs_the_new_producer(self):
        self.runner.register(_Producer(name="scan", cost="local", count=1))
        self.runner.reregister(_Producer(name="scan", cost="local", count=5))
        job = self.runner.start("scan", trigger="manual")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        self.assertEqual(self.runner.job(job.id).recorded, 5)


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
        first = self.runner.start("scan-a", trigger="manual")
        with self.assertRaises(JobRejected) as ctx:
            self.runner.start("scan-b", trigger="manual")
        self.assertIn("scan-a", str(ctx.exception))
        gate.set()
        self.runner.wait(first.id, timeout=5)

    def test_a_local_job_runs_alongside_a_scraping_job(self):
        gate = threading.Event()
        self.runner.register(_Producer(name="scan", cost="scraping", gate=gate))
        self.runner.register(_Producer(name="tags", cost="local", count=1))
        scan = self.runner.start("scan", trigger="manual")
        tags = self.runner.start("tags", trigger="manual")          # must not be refused
        self.runner.wait(tags.id, timeout=5)
        self.assertEqual(self.runner.job(tags.id).state, "done")
        gate.set()
        self.runner.wait(scan.id, timeout=5)

    def test_many_local_jobs_run_together(self):
        for i in range(4):
            self.runner.register(_Producer(name="local-%d" % i, cost="local", count=1))
        started = [self.runner.start("local-%d" % i, trigger="manual") for i in range(4)]
        for j in started:
            self.runner.wait(j.id, timeout=5)
        self.assertTrue(all(self.runner.job(j.id).state == "done" for j in started))

    def test_the_class_frees_up_when_the_job_finishes(self):
        self.runner.register(_Producer(name="scan", cost="scraping", count=1))
        first = self.runner.start("scan", trigger="manual")
        self.runner.wait(first.id, timeout=5)
        second = self.runner.start("scan", trigger="manual")        # must not be refused
        self.runner.wait(second.id, timeout=5)
        self.assertEqual(self.runner.job(second.id).state, "done")

    def test_the_class_frees_up_even_when_the_job_fails(self):
        # a crashed scrape that permanently blocks all future scrapes would be a
        # deadlock in slow motion
        self.runner.register(_Producer(name="scan", cost="scraping", count=1, boom=True))
        first = self.runner.start("scan", trigger="manual")
        self.runner.wait(first.id, timeout=5)
        second = self.runner.start("scan", trigger="manual")        # must not be refused
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
                self.runner.start("scan", trigger="manual")

        # the class must not be wedged: a following start() must be allowed
        # through and must run to completion, not be refused as saturated.
        second = self.runner.start("scan", trigger="manual")
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
                self.runner.start("scan", trigger="manual")

        try:
            # the worker exists, so the slot is its own `finally`'s to
            # release: the class must still be held, and the job must still
            # be visible rather than erased while its thread runs on.
            with self.assertRaises(JobRejected):
                self.runner.start("scan-2", trigger="manual")
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
        first = self.runner.start("scan-a", trigger="manual")

        barrier = threading.Barrier(n)
        results = [None] * n

        def attempt(i):
            barrier.wait()
            try:
                job = self.runner.start("scan-%d" % i, trigger="manual")
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


class _StoreWatchingTheRunner(Store):
    """A real store that notes, at the moment a run is closed, whether the
    runner has already released `wait()` for that job.

    A subclass of the real thing rather than a double: the rule under test is
    an ordering between something the runner does and something the store
    does, and a stand-in that only looked like a store could not observe it.
    """

    def __init__(self, path):
        super().__init__(path)
        self.runner = None
        self.job_id = None
        self.wait_had_returned = None

    def finish_run(self, run_id, **kwargs):
        self.wait_had_returned = self.runner.wait(self.job_id, timeout=0)
        return super().finish_run(run_id, **kwargs)


class TheRunIsRecorded(_RunnerCase):
    """Every start opens a row saying how the run began, and every end closes
    it saying how it went.

    `producer_run` -- what the scheduler reads -- answers "how long ago", and
    keeps one row per producer for the purpose. This answers "did last
    night's pass run, and what did it find", which is made of the runs that
    upsert threw away.
    """

    def _the_only_run(self):
        rows = self.store.recent_runs()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_scheduled_start_records_a_scheduled_run(self):
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer", trigger="scheduled")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        row = self._the_only_run()
        self.assertEqual((row["job"], row["trigger"], row["outcome"]),
                         ("test-producer", "scheduled", "completed"))

    def test_a_manual_start_is_recorded_as_manual(self):
        """The other value, pinned separately. A trigger that always reported
        one of the two would satisfy a suite that only ever asserted that one,
        and "did last night's pass run" is a question about the scheduled
        run, not about a button somebody pressed at noon."""
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer", trigger="manual")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        self.assertEqual(self._the_only_run()["trigger"], "manual")

    def test_the_trigger_is_required_rather_than_defaulted(self):
        """The same producer runs both ways, so there is no honest default:
        one would quietly label whichever call site forgot to say, and label
        it with the value a reader trusts most."""
        self.runner.register(_Producer(count=1))
        with self.assertRaises(TypeError):
            self.runner.start("test-producer")
        self.assertEqual(self.store.recent_runs(), [])

    def test_an_unrecognised_trigger_is_refused_before_anything_runs(self):
        self.runner.register(_Producer(count=1))
        with self.assertRaises(ValueError):
            self.runner.start("test-producer", trigger="cron")
        self.assertEqual(self.store.recent_runs(), [])
        self.assertEqual(self.store.items(), [])

    def test_an_unknown_producer_leaves_no_row_behind(self):
        with self.assertRaises(KeyError):
            self.runner.start("nosuchproducer", trigger="manual")
        self.assertEqual(self.store.recent_runs(), [])

    def test_a_running_job_is_in_the_log_before_it_finishes(self):
        """What the log is for during a long pass: a scan that takes an hour
        has to be visible for that hour, not appear once it is over. An
        operator watching a pass that shows nothing cannot tell it from one
        that never started."""
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate, count=1))
        job = self.runner.start("test-producer", trigger="scheduled")
        row = self._the_only_run()
        self.assertEqual(
            (row["job"], row["trigger"], row["finished"], row["outcome"],
             row["counts"], row["error"]),
            ("test-producer", "scheduled", None, None, {}, None))
        gate.set()
        self.assertTrue(self.runner.wait(job.id, timeout=5))

    def test_a_producer_that_raises_is_recorded_as_failed_with_its_error(self):
        """Closed from a `finally`, so the failure is recorded exactly as a
        success is. A log of the runs that worked answers the opposite
        question to the one it exists for: it makes a pass that has failed
        every night for a week look like one that was never scheduled."""
        self.runner.register(_Producer(count=1, boom=True))
        job = self.runner.start("test-producer", trigger="scheduled")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        row = self._the_only_run()
        self.assertEqual(row["outcome"], "failed")
        self.assertIsNotNone(row["finished"])
        self.assertIn("producer failed after yielding", row["error"])

    def test_a_completed_run_carries_no_error(self):
        """The permissive side of the same branch. An outcome that read
        "failed" only when asked about a failure would leave a run that
        worked carrying somebody else's error text."""
        self.runner.register(_Producer(count=1))
        job = self.runner.start("test-producer", trigger="manual")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        self.assertIsNone(self._the_only_run()["error"])

    def test_what_the_run_recorded_and_what_it_skipped_are_both_kept(self):
        """Asymmetric on purpose and neither count is one: equal counts
        cannot tell the two apart if they were swapped, and a fixture of one
        cannot tell a count that accumulates from one that assigns.

        Asserted as a whole dict, because a field-by-field check cannot see a
        counter that should not be there -- and this dict is handed to a page
        that renders whatever it is given.
        """
        self.store.mute("scene", "0")
        self.store.mute("scene", "1")
        self.runner.register(_Producer(count=5))
        job = self.runner.start("test-producer", trigger="manual")
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        self.assertEqual(self._the_only_run()["counts"],
                         {"recorded": 3, "skipped": 2})

    def test_two_runs_of_one_producer_are_two_rows(self):
        """The property the log exists for, and the one a single run cannot
        show: `record_run` keeps the latest and this keeps both."""
        self.runner.register(_Producer(count=1))
        first = self.runner.start("test-producer", trigger="scheduled")
        self.assertTrue(self.runner.wait(first.id, timeout=5))
        second = self.runner.start("test-producer", trigger="manual")
        self.assertTrue(self.runner.wait(second.id, timeout=5))
        self.assertEqual([r["trigger"] for r in self.store.recent_runs()],
                         ["manual", "scheduled"])

    def test_a_start_refused_as_saturated_closes_the_row_it_opened(self):
        """A refusal is ordinary, not exceptional -- asking for a scan while
        one is running raises `JobRejected` -- and retention deliberately
        never evicts an unfinished row. A row left open by a refused start
        would therefore sit in the log for the life of the deployment, one
        per click, and be reported as a run still going.
        """
        gate = threading.Event()
        self.runner.register(_Producer(name="scan-a", cost="scraping",
                                       gate=gate, count=1))
        self.runner.register(_Producer(name="scan-b", cost="scraping"))
        first = self.runner.start("scan-a", trigger="manual")
        with self.assertRaises(JobRejected):
            self.runner.start("scan-b", trigger="manual")
        gate.set()
        self.assertTrue(self.runner.wait(first.id, timeout=5))

        rows = self.store.recent_runs()
        self.assertEqual([r["finished"] for r in rows if r["finished"] is None],
                         [])
        refused = [r for r in rows if r["job"] == "scan-b"]
        self.assertEqual(len(refused), 1, rows)
        self.assertEqual(refused[0]["outcome"], "failed")
        self.assertIn("JobRejected", refused[0]["error"])

    def test_a_spawn_that_fails_closes_the_row_it_opened(self):
        """The other no-worker path. `Thread.start()` raising an `Exception`
        is the spawn itself failing, before any child exists -- which is why
        `start` takes the reservation back there, and why it takes the row
        back too."""
        self.runner.register(_Producer(name="scan", cost="scraping", count=1))
        with mock.patch("threading.Thread.start",
                        side_effect=OSError("thread start failed")):
            with self.assertRaises(OSError):
                self.runner.start("scan", trigger="manual")
        row = self._the_only_run()
        self.assertEqual(row["outcome"], "failed")
        self.assertIsNotNone(row["finished"])
        self.assertIn("OSError", row["error"])

    def test_an_interrupt_inside_thread_start_leaves_the_row_to_the_worker(self):
        """The mirror of the slot rule beside it. In that window the child
        has been spawned but has not yet set the event `Thread.start()` is
        waiting on, so a worker is already on its way while every test this
        thread could make says otherwise. Closing the row here would let this
        thread overwrite a live worker's own verdict with a failure."""
        self.runner.register(_Producer(name="scan", cost="scraping", count=1))
        real_start = threading.Thread.start
        started = _StartedInterrupted()

        def interrupted_start(thread):
            thread._started = started
            real_start(thread)

        with mock.patch("threading.Thread.start", interrupted_start):
            with self.assertRaises(KeyboardInterrupt):
                self.runner.start("scan", trigger="manual")
        try:
            # Still open: this thread left it alone. The worker is parked
            # inside `_started.set()` and has not reached its `finally` yet.
            self.assertIsNone(self._the_only_run()["outcome"])
        finally:
            started.let_the_worker_run()

        for job in self.runner.jobs():
            self.runner.wait(job.id, timeout=5)
        # And the worker closed it, exactly once and as its own run.
        self.assertEqual(self._the_only_run()["outcome"], "completed")


class TheRunIsClosedBeforeTheWaitReturns(unittest.TestCase):
    """`wait()` returning is the only signal a caller has that a job is over.
    A close that happened after it would let a page read the log immediately
    afterwards and show the run it just waited for as still going."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = _StoreWatchingTheRunner(os.path.join(self._dir, "s.db"))
        self.runner = JobRunner(self.store)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_the_row_is_closed_before_wait_is_released(self):
        gate = threading.Event()
        self.runner.register(_Producer(gate=gate, count=1))
        job = self.runner.start("test-producer", trigger="manual")
        # Held until the id is recorded, so the worker cannot reach the store
        # before this thread knows which job to ask about.
        self.store.runner, self.store.job_id = self.runner, job.id
        gate.set()
        self.assertTrue(self.runner.wait(job.id, timeout=5))
        # `assertFalse` would pass on the `None` that means the hook never
        # ran at all, which is the one answer that proves nothing.
        self.assertEqual(self.store.wait_had_returned, False)
