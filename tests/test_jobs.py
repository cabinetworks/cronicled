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
    fail after yielding some real work."""

    def __init__(self, name="test-producer", cost="local", count=3,
                 gate=None, boom=False):
        self.name, self.cost, self._count = name, cost, count
        self._gate, self._boom = gate, boom

    def produce(self, ctx):
        for i in range(self._count):
            if self._gate is not None:
                self._gate.wait(5)
            ctx.log("processing item %d" % i)
            yield {"folder": "f", "subject_type": "scene", "subject_id": str(i),
                   "summary": "proposal %d" % i, "payload": {"n": i}}
        if self._boom:
            raise RuntimeError("producer failed after yielding")


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
