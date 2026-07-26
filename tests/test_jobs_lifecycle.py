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
from unittest import mock

from cronicled.jobs import (
    DEFAULT_HISTORY, JobForgotten, JobRejected, JobRunner, RunnerClosed,
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


class OrderlyShutdown(_RunnerCase):
    """`close()` stops new work, waits for what is in flight, and says which
    of the two answers the caller got.

    Every "is it still waiting?" check here is a join with a short bound, not
    a sleep: a `close()` that failed to wait would have returned already, so
    the thread being alive is the observation, and the bound is only there so
    a broken build fails instead of hanging.
    """

    def _closer(self, runner, timeout, out):
        return threading.Thread(target=lambda: out.append(runner.close(timeout)))

    def test_start_after_close_refuses(self):
        # The property the whole drain rests on. Without it a shutdown races
        # a new job: you wait for two while a third begins behind you, and
        # the process is killed with work in flight anyway.
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", cost="scraping", count=1))
        self.assertTrue(runner.close(timeout=WAIT))
        with self.assertRaises(RunnerClosed):
            runner.start("scan")
        # nothing was recorded and no job record was left behind
        self.assertEqual(list(runner.jobs()), [])
        self.assertEqual(len(self.store.items()), 0)

    def test_an_unlimited_cost_class_is_refused_too(self):
        # `local` has no concurrency limit, so a refusal written into the
        # saturation branch would never fire for it -- and `local` is what
        # most producers are.
        runner = JobRunner(self.store)
        runner.register(_Producer(name="tags", cost="local", count=1))
        runner.close(timeout=WAIT)
        with self.assertRaises(RunnerClosed):
            runner.start("tags")

    def test_a_closed_runner_is_not_a_busy_one(self):
        # A caller that catches JobRejected treats it as "try again shortly",
        # because a cost class frees up. A closed runner never does, and a
        # scheduler retrying one would spin until the process died.
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", cost="scraping", count=1))
        runner.close(timeout=WAIT)
        with self.assertRaises(RunnerClosed) as ctx:
            runner.start("scan")
        self.assertNotIsInstance(ctx.exception, JobRejected)

    def test_close_waits_for_every_running_job_not_just_one(self):
        gate_a, gate_b = threading.Event(), threading.Event()
        runner = JobRunner(self.store)
        runner.register(_Producer(name="a", gate=gate_a, count=1))
        runner.register(_Producer(name="b", gate=gate_b, count=1))
        job_a = runner.start("a")
        job_b = runner.start("b")

        out = []
        closer = self._closer(runner, WAIT, out)
        closer.start()
        try:
            closer.join(0.2)
            self.assertTrue(closer.is_alive(), "close() returned with two "
                                               "jobs still running")
            gate_a.set()
            self.assertTrue(runner.wait(job_a.id, timeout=WAIT))
            closer.join(0.2)
            self.assertTrue(closer.is_alive(), "close() returned with one "
                                               "job still running")
        finally:
            gate_b.set()
        closer.join(WAIT)
        self.assertFalse(closer.is_alive())
        self.assertEqual(out, [True])
        self.assertEqual(runner.job(job_b.id).state, "done")

    def test_close_reports_everything_finished(self):
        gate = threading.Event()
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", gate=gate, count=2))
        job = runner.start("scan")
        gate.set()
        self.assertTrue(runner.close(timeout=WAIT))
        self.assertEqual(runner.job(job.id).state, "done")
        self.assertEqual(len(self.store.items()), 2)

    def test_close_reports_giving_up_rather_than_everything_finished(self):
        # A deploy script asking "is it safe to kill this process" gets two
        # different answers here and must act differently on them. A close()
        # that cannot tell them apart is useless to the caller that needs it
        # most.
        gate = threading.Event()
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", gate=gate, count=1))
        job = runner.start("scan")

        self.assertFalse(runner.close(timeout=0.1))

        # the job was not cancelled and was not killed: it is a daemon
        # thread, still running, and it finishes if given the chance
        self.assertEqual(runner.job(job.id).state, "running")
        gate.set()
        self.assertTrue(runner.wait(job.id, timeout=WAIT))
        self.assertEqual(runner.job(job.id).state, "done")

    def test_a_closed_runner_says_so_even_when_the_class_is_saturated(self):
        # Both refusals apply to this start, and only one of them is worth
        # telling the caller: "the class is busy" invites the retry that a
        # closed runner will never reward.
        gate = threading.Event()
        runner = JobRunner(self.store)
        runner.register(
            _Producer(name="scan-a", cost="scraping", gate=gate, count=1))
        runner.register(_Producer(name="scan-b", cost="scraping", count=1))
        job = runner.start("scan-a")
        self.assertFalse(runner.close(timeout=0.1))
        with self.assertRaises(RunnerClosed):
            runner.start("scan-b")
        gate.set()
        self.assertTrue(runner.wait(job.id, timeout=WAIT))

    def test_the_timeout_is_shared_across_jobs_rather_than_given_to_each(self):
        # A caller asking to wait one second means one second in total.
        # Handing that timeout to each job in turn multiplies it by however
        # many happen to be running, so a drain that reports waiting a
        # second can take ten.
        #
        # Read off the waits themselves rather than off the clock: the
        # second job is waited on with what is left of the budget, which the
        # first has already spent, so it is strictly smaller. A per-job
        # timeout makes the two identical. Only waits on the calling thread
        # are counted -- the producers' own gate waits happen on the worker
        # threads.
        gate = threading.Event()
        runner = JobRunner(self.store)
        runner.register(_Producer(name="a", gate=gate, count=1))
        runner.register(_Producer(name="b", gate=gate, count=1))
        job_a = runner.start("a")
        job_b = runner.start("b")

        caller = threading.current_thread()
        real_wait = threading.Event.wait
        seen = []

        def spy(event, timeout=None):
            if threading.current_thread() is caller:
                seen.append(timeout)
            return real_wait(event, timeout)

        try:
            with mock.patch.object(threading.Event, "wait", spy):
                self.assertFalse(runner.close(timeout=0.1))
            self.assertEqual(len(seen), 2, "both running jobs must be waited "
                                           "on, not just the first")
            self.assertLess(seen[1], seen[0])
        finally:
            gate.set()
        self.assertTrue(runner.wait(job_a.id, timeout=WAIT))
        self.assertTrue(runner.wait(job_b.id, timeout=WAIT))

    def test_a_second_close_re_waits_rather_than_repeating_its_answer(self):
        # close() is called from a signal handler and from a finally on the
        # same process more often than not, so it has to be callable twice.
        # The second call must answer for the state it finds, not replay the
        # first: here the work finishes in between, and "I gave up" would now
        # be false.
        gate = threading.Event()
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", gate=gate, count=1))
        job = runner.start("scan")
        self.assertFalse(runner.close(timeout=0.1))
        gate.set()
        self.assertTrue(runner.wait(job.id, timeout=WAIT))
        self.assertTrue(runner.close(timeout=WAIT))

    def test_closing_twice_is_not_an_error(self):
        runner = JobRunner(self.store)
        self.assertTrue(runner.close(timeout=WAIT))
        self.assertTrue(runner.close(timeout=WAIT))
        runner.register(_Producer(name="scan", count=1))
        with self.assertRaises(RunnerClosed):
            runner.start("scan")

    def test_close_with_nothing_running_says_everything_finished(self):
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", count=1))
        job = runner.start("scan")
        self.assertTrue(runner.wait(job.id, timeout=WAIT))
        self.assertTrue(runner.close(timeout=WAIT))

    def test_close_leaves_the_store_open(self):
        # The runner was handed a store it did not open. Closing someone
        # else's store on the way out would break the caller that still has
        # to read the proposals the jobs just recorded.
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", count=1))
        job = runner.start("scan")
        self.assertTrue(runner.close(timeout=WAIT))
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(runner.job(job.id).recorded, 1)

    def test_introspection_still_works_after_close(self):
        # close() stops new work; it does not blind the caller to what ran.
        runner = JobRunner(self.store)
        runner.register(_Producer(name="scan", count=1))
        job = runner.start("scan")
        self.assertTrue(runner.close(timeout=WAIT))
        self.assertEqual([j.id for j in runner.jobs()], [job.id])
        self.assertEqual(runner.job(job.id).state, "done")

    def test_a_race_of_starts_against_close_leaves_nothing_running(self):
        # The refusal and the drain have to be one atomic decision, the same
        # way the saturation check and the slot reservation are. If close()
        # set its flag and took its list of jobs to wait for in two steps, a
        # start() landing between them would be accepted and then not waited
        # for -- close() would report everything finished with a job running.
        runner = JobRunner(self.store)
        n = 12
        for i in range(n):
            runner.register(_Producer(name="p-%d" % i, count=1))
        barrier = threading.Barrier(n + 1)
        results = [None] * n

        def attempt(i):
            barrier.wait(WAIT)
            try:
                results[i] = ("started", runner.start("p-%d" % i))
            except RunnerClosed:
                results[i] = ("refused", None)

        threads = [threading.Thread(target=attempt, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        barrier.wait(WAIT)
        drained = runner.close(timeout=WAIT)
        for t in threads:
            t.join(WAIT)

        self.assertTrue(drained)
        self.assertTrue(all(r is not None for r in results))
        started = [job for kind, job in results if kind == "started"]
        for job in started:
            self.assertNotEqual(
                runner.job(job.id).state, "running",
                "close() reported everything finished while a job it "
                "accepted was still running")
        self.assertEqual(len(runner.jobs()), len(started))


if __name__ == "__main__":
    unittest.main()
