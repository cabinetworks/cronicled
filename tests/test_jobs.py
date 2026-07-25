"""The runner drives producers in the background so a long scan cannot block the
interface, and records what they yield as they yield it."""
import os
import shutil
import tempfile
import threading
import unittest

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
