"""What the runner does over a long life: a bounded history, and a shutdown.

Both are invisible while a person starts jobs by hand and the process lives
for minutes. Both become real the day something starts producers unattended
in a process that stays up for weeks: history that only grows, and no way to
tell a deploy that it is safe to kill the process.

The fixtures live in `tests.test_jobs`, deliberately: these behaviours are
the same runner's, and a second copy of the producer stub would be free to
drift away from the one every other job test uses.

Nothing here sleeps to synchronise. Every wait is on a `threading.Event` or
a join, and every one of them is bounded so a broken build fails instead of
hanging.
"""
import threading
import unittest

from cronicled.jobs import (
    DEFAULT_HISTORY, JobForgotten, JobRunner,
)
from tests.test_jobs import _Producer, _RunnerCase

# Every wait in this file is bounded by this. It is a deadlock guard, never a
# synchronisation device: in a passing run every wait returns the instant the
# other thread sets its event, and nothing here ever waits for time to pass.
WAIT = 10


class BoundedHistory(_RunnerCase):
    """Finished jobs are dropped once there are more than the cap; running
    jobs never are, and the runner says how many it has dropped."""

    def _runner(self, history):
        return JobRunner(self.store, history=history)

    def _finish(self, runner, n, prefix="p"):
        """Run `n` jobs to completion, one at a time, and return their ids in
        the order they finished."""
        ids = []
        for i in range(n):
            name = "%s-%d" % (prefix, i)
            runner.register(_Producer(name=name, count=1))
            job = runner.start(name)
            self.assertTrue(runner.wait(job.id, timeout=WAIT))
            ids.append(job.id)
        return ids

    def test_finished_jobs_past_the_cap_are_dropped_oldest_first(self):
        # The newest are the ones worth keeping: a caller looking at job
        # history is almost always asking what just happened.
        runner = self._runner(history=2)
        ids = self._finish(runner, 5)
        self.assertEqual([j.id for j in runner.jobs()], ids[-2:])

    def test_exactly_the_cap_is_kept_whole(self):
        # The permissive side of the boundary. A cap that starts evicting one
        # job early is as wrong as one that never evicts, and much quieter.
        runner = self._runner(history=3)
        ids = self._finish(runner, 3)
        self.assertEqual([j.id for j in runner.jobs()], ids)
        self.assertEqual(runner.jobs().evicted, 0)

    def test_one_past_the_cap_evicts_exactly_one(self):
        runner = self._runner(history=3)
        ids = self._finish(runner, 4)
        self.assertEqual([j.id for j in runner.jobs()], ids[1:])
        self.assertEqual(runner.jobs().evicted, 1)

    def test_the_count_is_how_many_were_dropped_not_merely_that_some_were(self):
        # An introspection API that silently returns a truncated list invites
        # a caller to conclude nothing else ever ran. The count is the whole
        # value of admitting it, so it has to be the real number.
        runner = self._runner(history=2)
        self._finish(runner, 7)
        self.assertEqual(runner.jobs().evicted, 5)
        self.assertEqual(len(runner.jobs()), 2)

    def test_a_running_job_is_never_evicted(self):
        # Concurrency is already capped per cost class, so running jobs are
        # self-limiting; it is finished ones that accumulate. Evicting a
        # running job would erase the only record of work still in flight.
        gate = threading.Event()
        runner = self._runner(history=1)
        runner.register(_Producer(name="held", gate=gate, count=1))
        held = runner.start("held")

        self._finish(runner, 6)

        self.assertIn(held.id, [j.id for j in runner.jobs()])
        self.assertEqual(runner.job(held.id).state, "running")
        # the cap bounds finished jobs, so what is held is the running job
        # plus at most `history` finished ones
        self.assertEqual(len(runner.jobs()), 2)

        gate.set()
        self.assertTrue(runner.wait(held.id, timeout=WAIT))
        self.assertEqual(runner.job(held.id).state, "done")

    def test_eviction_follows_the_order_jobs_finished_in(self):
        # Start order and finish order are not the same order, and only one of
        # them is the right one to evict by. Here the first job started is the
        # last to finish: evicting by start order would drop the job that
        # finished most recently and keep the older one.
        gate = threading.Event()
        runner = self._runner(history=1)
        runner.register(_Producer(name="slow", gate=gate, count=1))
        runner.register(_Producer(name="quick", count=1))

        slow = runner.start("slow")
        quick = runner.start("quick")
        self.assertTrue(runner.wait(quick.id, timeout=WAIT))
        gate.set()
        self.assertTrue(runner.wait(slow.id, timeout=WAIT))

        self.assertEqual([j.id for j in runner.jobs()], [slow.id])
        self.assertEqual(runner.jobs().evicted, 1)

    def test_an_id_that_never_existed_is_not_reported_as_forgotten(self):
        # "I never had that" and "that ran and I no longer remember it" lead a
        # caller to different places: a wrong id is its own bug, a forgotten
        # job is a job that really ran and whose outcome is now unknowable.
        runner = self._runner(history=2)
        self._finish(runner, 1)
        with self.assertRaises(KeyError) as ctx:
            runner.job("no-such-job")
        self.assertNotIsInstance(ctx.exception, JobForgotten)

    def test_an_evicted_job_says_it_was_forgotten(self):
        runner = self._runner(history=2)
        ids = self._finish(runner, 5)
        with self.assertRaises(JobForgotten) as ctx:
            runner.job(ids[0])
        # the count is what makes the answer actionable: it says how much
        # history the caller is missing, which the id alone cannot
        self.assertIn("3", str(ctx.exception))
        # the ones still held are unaffected
        self.assertEqual(runner.job(ids[-1]).id, ids[-1])

    def test_a_forgotten_job_is_still_a_key_error(self):
        # `job()` has always raised `KeyError` for an id it does not hold, and
        # callers written against that must not start leaking a new exception
        # type past their handlers.
        runner = self._runner(history=1)
        ids = self._finish(runner, 3)
        with self.assertRaises(KeyError):
            runner.job(ids[0])

    def test_waiting_on_an_evicted_job_also_says_it_was_forgotten(self):
        # `wait()` loses the job's Event to the same eviction. Returning False
        # would say "still running" about a job that finished long ago.
        runner = self._runner(history=1)
        ids = self._finish(runner, 3)
        with self.assertRaises(JobForgotten):
            runner.wait(ids[0], timeout=WAIT)

    def test_a_cap_below_one_is_refused(self):
        # A cap of zero makes every job unobservable the instant it finishes,
        # so the ordinary `wait(id)` then `job(id)` becomes a race nobody
        # asked for. There is no useful reading of it, so it is not accepted.
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                self._runner(history=bad)

    def test_a_cap_of_one_is_accepted(self):
        # the permissive side of the same boundary
        runner = self._runner(history=1)
        ids = self._finish(runner, 2)
        self.assertEqual([j.id for j in runner.jobs()], [ids[-1]])

    def test_a_boolean_cap_is_refused(self):
        # `True` is 1 to every integer check in Python, so `history=True` --
        # a plausible way to write "yes, keep history" -- would silently keep
        # exactly one job and throw away everything else.
        with self.assertRaises(ValueError):
            self._runner(history=True)

    def test_a_runner_that_never_reaches_the_cap_keeps_everything(self):
        # nothing changes for the caller who starts a handful of jobs by hand
        runner = JobRunner(self.store)
        ids = self._finish(runner, 5)
        self.assertEqual([j.id for j in runner.jobs()], ids)
        self.assertEqual(runner.jobs().evicted, 0)
        self.assertGreater(DEFAULT_HISTORY, 5)


if __name__ == "__main__":
    unittest.main()
