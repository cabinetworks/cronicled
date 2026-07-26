"""What is due, and why everything else is not.

Every rule here is arithmetic over data — no thread, no sleep, no wall clock.
`now` is a parameter, which is the whole reason the tick is separated from the
loop that drives it: a clock that jumped back a year, a service that was off for
a week, and the exact second a cadence elapses are all ordinary unit tests here
and would each need a fixture with a fake clock inside a running loop otherwise.
"""
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from cronicled.jobs import JobRunner
from cronicled.schedule import Entry, Scheduler, TickResult, due, resolve
from cronicled.store import Store

HOUR = 3600
DAY = 86400

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-07-26T12:00:00+00:00"


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat()


def ahead(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


class FakeProducer:
    """Everything `resolve` is allowed to read off a producer: a name, and a
    cadence it may or may not declare."""

    def __init__(self, name, every=None):
        self.name = name
        self.every = every


class ProducerWithNoCadenceAttributeAtAll:
    """A producer written before cadences existed. Missing the attribute and
    declaring it as `None` must be the same wiring mistake, not two."""

    def __init__(self, name):
        self.name = name


class CadenceAndOverrides(unittest.TestCase):
    def test_a_producers_own_cadence_is_used_when_nothing_overrides_it(self):
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY)]),
            {"nightly": Entry(producer="nightly", every=DAY, enabled=True)})

    def test_no_overrides_argument_is_the_same_as_an_empty_mapping(self):
        producers = [FakeProducer("nightly", every=DAY)]
        self.assertEqual(resolve(producers), resolve(producers, {}))

    def test_an_override_replaces_the_producers_own_cadence(self):
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"every": HOUR}}),
            {"nightly": Entry(producer="nightly", every=HOUR, enabled=True)})

    def test_an_override_can_supply_a_cadence_the_producer_does_not_have(self):
        self.assertEqual(
            resolve([FakeProducer("nightly")], {"nightly": {"every": HOUR}}),
            {"nightly": Entry(producer="nightly", every=HOUR, enabled=True)})

    def test_an_override_can_disable_a_producer(self):
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"enabled": False}}),
            {"nightly": Entry(producer="nightly", every=DAY, enabled=False)})

    def test_an_override_can_enable_a_producer_explicitly(self):
        # The permissive side of the same rule: `enabled: true` written out in
        # a config file must resolve, not be treated as an odd key.
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"enabled": True}}),
            {"nightly": Entry(producer="nightly", every=DAY, enabled=True)})

    def test_an_override_can_set_the_cadence_and_disable_at_once(self):
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"every": HOUR, "enabled": False}}),
            {"nightly": Entry(producer="nightly", every=HOUR, enabled=False)})

    def test_a_disabled_producer_needs_no_cadence(self):
        # Nobody scheduled it *because somebody said not to* — an explicit
        # decision, not the silent omission `resolve` refuses below.
        self.assertEqual(
            resolve([FakeProducer("nightly")], {"nightly": {"enabled": False}}),
            {"nightly": Entry(producer="nightly", every=None, enabled=False)})

    def test_an_override_leaves_every_other_producer_alone(self):
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY),
                     FakeProducer("hourly", every=HOUR)],
                    {"nightly": {"every": 60}}),
            {"nightly": Entry(producer="nightly", every=60, enabled=True),
             "hourly": Entry(producer="hourly", every=HOUR, enabled=True)})


class ResolveRefusesAWiringMistake(unittest.TestCase):
    def test_a_producer_with_no_cadence_and_no_override_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly")])

    def test_a_producer_missing_the_attribute_entirely_is_the_same_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([ProducerWithNoCadenceAttributeAtAll("nightly")])

    def test_enabling_a_producer_does_not_stand_in_for_a_cadence(self):
        # `{"enabled": true}` mentions the producer without scheduling it. If
        # merely being named silenced this error, the wiring mistake would be
        # back with a config entry that looks deliberate.
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly")], {"nightly": {"enabled": True}})

    def test_an_override_naming_a_producer_that_does_not_exist_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "typo"):
            resolve([FakeProducer("nightly", every=DAY)],
                    {"typo": {"every": HOUR}})

    def test_an_unknown_key_in_an_override_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "evrey"):
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"evrey": HOUR}})

    def test_an_override_that_is_not_a_mapping_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every=DAY)], {"nightly": HOUR})

    def test_two_producers_claiming_one_name_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every=DAY),
                     FakeProducer("nightly", every=HOUR)])

    def test_a_cadence_of_zero_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every=0)])

    def test_a_negative_cadence_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every=-HOUR)])

    def test_a_cadence_of_one_second_is_accepted(self):
        # The permissive side of the same guard: a guard that drifts too
        # strict is as wrong as one too loose, and quieter.
        self.assertEqual(
            resolve([FakeProducer("nightly", every=1)]),
            {"nightly": Entry(producer="nightly", every=1, enabled=True)})

    def test_a_boolean_cadence_is_an_error(self):
        # `True` is an `int` in Python, so `{"every": true}` in a JSON config
        # would otherwise resolve to a one-second cadence on a full scrape.
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"every": True}})

    def test_a_cadence_that_is_a_string_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every="3600")])

    def test_an_enabled_flag_that_is_not_a_boolean_is_an_error(self):
        # "false" is a true string; a config written that way must not turn
        # into a producer that runs.
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"enabled": "false"}})


class WhatIsDue(unittest.TestCase):
    def setUp(self):
        self.entries = resolve([FakeProducer("nightly", every=DAY)])

    def test_a_producer_that_has_never_run_is_due(self):
        self.assertEqual(due(self.entries, {}, NOW_ISO), (["nightly"], {}))

    def test_a_producer_that_ran_longer_ago_than_its_cadence_is_due(self):
        self.assertEqual(due(self.entries, {"nightly": ago(DAY + HOUR)}, NOW_ISO),
                         (["nightly"], {}))

    def test_a_producer_that_ran_more_recently_is_not_due_and_says_when(self):
        # Ran at 11:01 on an hourly cadence, so it is next due at 12:01 — the
        # whole reason string, not a substring of it.
        entries = resolve([FakeProducer("hourly", every=HOUR)])
        self.assertEqual(
            due(entries, {"hourly": "2026-07-26T11:01:00+00:00"}, NOW_ISO),
            ([], {"hourly": "last ran 2026-07-26T11:01:00+00:00; "
                            "next due at 2026-07-26T12:01:00+00:00"}))

    def test_a_disabled_producer_is_not_due_however_long_it_has_been(self):
        entries = resolve([FakeProducer("nightly", every=DAY)],
                          {"nightly": {"enabled": False}})
        self.assertEqual(due(entries, {"nightly": ago(365 * DAY)}, NOW_ISO),
                         ([], {"nightly": "disabled by override"}))

    def test_a_disabled_producer_that_has_never_run_is_not_due_either(self):
        entries = resolve([FakeProducer("nightly")],
                          {"nightly": {"enabled": False}})
        self.assertEqual(due(entries, {}, NOW_ISO),
                         ([], {"nightly": "disabled by override"}))

    def test_every_entry_is_either_due_or_carries_a_reason(self):
        entries = resolve(
            [FakeProducer("never-run", every=DAY),
             FakeProducer("overdue", every=DAY),
             FakeProducer("recent", every=DAY),
             FakeProducer("off", every=DAY)],
            {"off": {"enabled": False}})
        runs = {"overdue": ago(2 * DAY), "recent": "2026-07-26T11:00:00+00:00"}
        self.assertEqual(
            due(entries, runs, NOW_ISO),
            (["never-run", "overdue"],
             {"off": "disabled by override",
              "recent": "last ran 2026-07-26T11:00:00+00:00; "
                        "next due at 2026-07-27T11:00:00+00:00"}))

    def test_the_due_list_is_ordered_by_name_whatever_order_they_arrived_in(self):
        entries = resolve([FakeProducer("zulu", every=DAY),
                           FakeProducer("alpha", every=DAY),
                           FakeProducer("mike", every=DAY)])
        self.assertEqual(due(entries, {}, NOW_ISO)[0], ["alpha", "mike", "zulu"])

    def test_a_recorded_run_for_a_producer_nobody_schedules_is_ignored(self):
        # A producer removed from the wiring leaves its row behind in the
        # store. It is not due, and it is not a reason either.
        self.assertEqual(
            due(self.entries, {"nightly": ago(2 * DAY), "retired": ago(2 * DAY)},
                NOW_ISO),
            (["nightly"], {}))

    def test_now_may_be_a_datetime_as_well_as_a_string(self):
        runs = {"nightly": ago(HOUR)}
        self.assertEqual(due(self.entries, runs, NOW),
                         due(self.entries, runs, NOW_ISO))

    def test_a_now_with_no_timezone_is_refused(self):
        # Every timestamp in the store is UTC. A naive `now` is the caller's
        # local clock or the caller's bug, and guessing which would shift
        # every producer's due-ness by the machine's offset, silently.
        with self.assertRaises(ValueError):
            due(self.entries, {}, "2026-07-26T12:00:00")

    def test_an_enabled_entry_with_no_cadence_is_refused_rather_than_skipped(self):
        # `resolve` cannot produce this; a hand-built one must not quietly
        # become a producer that never runs.
        entries = {"nightly": Entry(producer="nightly", every=None, enabled=True)}
        with self.assertRaisesRegex(ValueError, "nightly"):
            due(entries, {}, NOW_ISO)


class TheDueBoundary(unittest.TestCase):
    """One second either way. Too eager runs a scrape early; too strict delays
    every producer by a whole tick, forever and silently."""

    def setUp(self):
        self.entries = resolve([FakeProducer("hourly", every=HOUR)])

    def test_exactly_one_cadence_since_the_last_run_is_due(self):
        self.assertEqual(due(self.entries, {"hourly": ago(HOUR)}, NOW_ISO),
                         (["hourly"], {}))

    def test_one_second_short_of_the_cadence_is_not_due(self):
        names, reasons = due(self.entries, {"hourly": ago(HOUR - 1)}, NOW_ISO)
        self.assertEqual(names, [])
        self.assertEqual(reasons,
                         {"hourly": "last ran 2026-07-26T11:00:01+00:00; "
                                    "next due at 2026-07-26T12:00:01+00:00"})

    def test_one_second_past_the_cadence_is_due(self):
        self.assertEqual(due(self.entries, {"hourly": ago(HOUR + 1)}, NOW_ISO),
                         (["hourly"], {}))


class AClockThatWentBackwards(unittest.TestCase):
    """The store records what it is given and does not keep the maximum, so a
    last run stamped by a fast clock arrives here untouched."""

    def setUp(self):
        self.entries = resolve([FakeProducer("hourly", every=HOUR)])

    def test_a_last_run_a_year_in_the_future_does_not_buy_a_year_of_silence(self):
        self.assertEqual(due(self.entries, {"hourly": ahead(365 * DAY)}, NOW_ISO),
                         (["hourly"], {}))

    def test_a_last_run_one_second_in_the_future_is_already_due(self):
        self.assertEqual(due(self.entries, {"hourly": ahead(1)}, NOW_ISO),
                         (["hourly"], {}))

    def test_running_it_heals_the_skew(self):
        # The run that the future stamp provoked is recorded against the
        # current clock, and the producer settles onto its cadence again.
        self.assertEqual(
            due(self.entries, {"hourly": NOW_ISO}, NOW_ISO),
            ([], {"hourly": "last ran 2026-07-26T12:00:00+00:00; "
                            "next due at 2026-07-26T13:00:00+00:00"}))

    def test_a_last_run_with_no_timezone_is_due_rather_than_wedging_the_tick(self):
        # Comparing a naive stamp with an aware `now` raises TypeError in
        # Python, which would take out the whole tick — every producer, not
        # the one with the bad row.
        #
        # The stamp is one minute old, which is what makes this test say
        # anything: read as UTC it would be nowhere near due, so a passing
        # result here is the "cannot be read, so run it" rule and not the
        # ordinary arithmetic quietly agreeing.
        self.assertEqual(due(self.entries, {"hourly": "2026-07-26T11:59:00"},
                             NOW_ISO),
                         (["hourly"], {}))

    def test_a_last_run_that_is_not_a_timestamp_at_all_is_due(self):
        self.assertEqual(due(self.entries, {"hourly": "yesterday"}, NOW_ISO),
                         (["hourly"], {}))

    def test_one_bad_row_does_not_disturb_the_other_producers(self):
        entries = resolve([FakeProducer("hourly", every=HOUR),
                           FakeProducer("daily", every=DAY)])
        self.assertEqual(
            due(entries, {"hourly": "yesterday",
                          "daily": "2026-07-26T11:00:00+00:00"}, NOW_ISO),
            (["hourly"],
             {"daily": "last ran 2026-07-26T11:00:00+00:00; "
                       "next due at 2026-07-27T11:00:00+00:00"}))


class MissedRunsAreNotMadeUp(unittest.TestCase):
    """A service that was off for a week wakes up owing one nightly scrape,
    not seven."""

    def setUp(self):
        self.entries = resolve([FakeProducer("nightly", every=DAY)])

    def test_a_week_of_missed_runs_makes_it_due_exactly_once(self):
        names, reasons = due(self.entries, {"nightly": ago(7 * DAY)}, NOW_ISO)
        self.assertEqual(names, ["nightly"])
        self.assertEqual(reasons, {})

    def test_the_next_run_is_a_cadence_from_now_not_from_the_missed_schedule(self):
        # Having run at `now`, it is next due at now + a day. A scheduler
        # catching up would count from the old stamp and keep it due.
        self.assertEqual(
            due(self.entries, {"nightly": NOW_ISO}, NOW_ISO),
            ([], {"nightly": "last ran 2026-07-26T12:00:00+00:00; "
                             "next due at 2026-07-27T12:00:00+00:00"}))


class RunnableProducer:
    """A producer the real runner can actually drive: a cadence for the
    schedule, a cost class for the runner, and a `produce` that yields one
    proposal.

    `gate` holds it open, so a test can look at a job that is genuinely still
    running rather than at a fake that says it is. `boom` makes it fail after
    yielding, which is the only way to tell "the run was recorded whatever
    happened" from "the run was recorded because it worked".
    """

    def __init__(self, name, every=DAY, cost="local", gate=None, boom=False):
        self.name = name
        self.every = every
        self.cost = cost
        self._gate = gate
        self._boom = boom

    def produce(self, ctx):
        if self._gate is not None:
            self._gate.wait(5)
        yield {"folder": "inbox", "subject_type": "scene",
               "subject_id": self.name, "summary": f"{self.name} found one",
               "payload": {"from": self.name}}
        if self._boom:
            raise RuntimeError("the producer gave up")


class ClosedRunner:
    """Stands in for a runner that has been closed.

    `close()` and its `RunnerClosed` land with the runner-lifecycle work and
    are not on this branch yet (see the task report), so this raises its own
    exception — which is the point of the rule being pinned. What the tick
    must not do is treat a permanent refusal as the transient one
    `JobRejected` describes: a closed runner never frees up, so a tick that
    filed it as "try again next tick" would spin on it for the life of the
    process. Anything that is not a `JobRejected` is therefore a failure to
    start, whatever its type, and that rule needs no import to hold.
    """

    class Closed(Exception):
        pass

    def __init__(self, producers):
        self._producers = list(producers)

    def producers(self):
        return list(self._producers)

    def jobs(self):
        return []

    def start(self, name):
        raise self.Closed(
            f"the runner is closed and is not accepting work; {name!r} was "
            "not started")


class SchedulerCase(unittest.TestCase):
    """A real `JobRunner` over a real `Store`, because the rules under test
    are about what the runner does when asked — a fake runner would let a
    scheduler that never really starts anything pass every one of them."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._dir, "s.db"))
        self.runner = JobRunner(self.store)
        self._gates = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # Release every held producer and let its job end before the store
        # goes, so a worker never finds a closed database under it. No sleep:
        # `wait` blocks on the runner's own event.
        for gate in self._gates:
            gate.set()
        for job in self.runner.jobs():
            self.runner.wait(job.id, timeout=5)
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def gate(self):
        gate = threading.Event()
        self._gates.append(gate)
        return gate

    def scheduler(self, *producers, overrides=None, clock=None):
        for producer in producers:
            self.runner.register(producer)
        return Scheduler(self.runner, self.store, overrides=overrides,
                         clock=clock)


class ATickStartsWhatIsDue(SchedulerCase):
    def test_a_due_producer_is_started_through_the_runner_and_recorded(self):
        scheduler = self.scheduler(RunnableProducer("nightly"))

        result = scheduler.tick(NOW_ISO)

        jobs = self.runner.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            result,
            TickResult(at=NOW_ISO, due=["nightly"],
                       started={"nightly": jobs[0].id}, skipped={},
                       failed_to_start={}))
        self.assertTrue(self.runner.wait(jobs[0].id, timeout=5))
        self.assertEqual(self.runner.job(jobs[0].id).state, "done")
        # It went *through* the runner rather than around it: the proposal
        # the producer yielded is in the store.
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(self.store.last_run("nightly"), NOW_ISO)

    def test_the_run_is_stamped_with_the_moment_the_tick_decided(self):
        # Not with a fresh read of the wall clock inside the store. The tick
        # decides due-ness against `now` and records against the same value,
        # so "next due" is a cadence from the decision and a test with an
        # injected clock says something.
        scheduler = self.scheduler(RunnableProducer("nightly"))
        scheduler.tick(NOW)
        self.assertEqual(self.store.last_run("nightly"), NOW_ISO)

    def test_a_producer_that_is_not_due_is_not_started_and_says_when_it_is(self):
        scheduler = self.scheduler(RunnableProducer("nightly"))
        self.store.record_run("nightly", "2026-07-26T09:00:00+00:00")

        result = scheduler.tick(NOW_ISO)

        self.assertEqual(
            result,
            TickResult(at=NOW_ISO, due=[], started={},
                       skipped={"nightly":
                                "last ran 2026-07-26T09:00:00+00:00; "
                                "next due at 2026-07-27T09:00:00+00:00"},
                       failed_to_start={}))
        self.assertEqual(self.runner.jobs(), [])
        self.assertEqual(self.store.last_run("nightly"),
                         "2026-07-26T09:00:00+00:00")

    def test_a_disabled_producer_is_not_started_and_says_that_instead(self):
        scheduler = self.scheduler(RunnableProducer("nightly"),
                                   overrides={"nightly": {"enabled": False}})

        result = scheduler.tick(NOW_ISO)

        self.assertEqual(
            result,
            TickResult(at=NOW_ISO, due=[], started={},
                       skipped={"nightly": "disabled by override"},
                       failed_to_start={}))
        self.assertEqual(self.runner.jobs(), [])
        self.assertIsNone(self.store.last_run("nightly"))

    def test_the_tick_uses_the_injected_clock_when_it_is_given_no_now(self):
        scheduler = self.scheduler(RunnableProducer("nightly"),
                                   clock=lambda: NOW)
        result = scheduler.tick()
        self.assertEqual(result.at, NOW_ISO)
        self.assertEqual(self.store.last_run("nightly"), NOW_ISO)

    def test_without_a_clock_the_tick_uses_the_current_utc_time(self):
        # Aware, and UTC: a naive default would make the comparison below
        # raise `TypeError` rather than merely be wrong, and a local-time one
        # would land outside the bracket by the machine's offset.
        scheduler = self.scheduler(RunnableProducer("nightly"))
        before = datetime.now(timezone.utc)
        result = scheduler.tick()
        after = datetime.now(timezone.utc)
        self.assertLessEqual(before, datetime.fromisoformat(result.at))
        self.assertLessEqual(datetime.fromisoformat(result.at), after)

    def test_a_producer_with_no_cadence_is_refused_when_the_scheduler_is_built(self):
        # `resolve`'s wiring-mistake error surfaces where an operator reads
        # it — at start-up, in a stack trace — rather than at 3am as a
        # producer that quietly never ran.
        with self.assertRaisesRegex(ValueError, "nightly"):
            self.scheduler(RunnableProducer("nightly", every=None))


class ATickDoesNotStartWhatIsAlreadyRunning(SchedulerCase):
    """The only bound on a run this ticket can honestly offer. There is no
    cancellation, so a slow producer cannot be stopped — but it can be kept
    from being started a second time on top of itself, which means a slow
    producer delays its own next run and nothing else's."""

    def test_a_producer_still_running_from_a_previous_tick_is_left_alone(self):
        gate = self.gate()
        scheduler = self.scheduler(RunnableProducer("nightly", gate=gate))
        first = scheduler.tick(NOW_ISO)
        job_id = first.started["nightly"]

        # Two days later it is due again, and still running.
        result = scheduler.tick(ahead(2 * DAY))

        self.assertEqual(
            result,
            TickResult(at=ahead(2 * DAY), due=["nightly"], started={},
                       skipped={"nightly": f"already running as job {job_id}"},
                       failed_to_start={}))
        self.assertEqual(len(self.runner.jobs()), 1)

    def test_a_producer_that_has_finished_is_started_again_when_it_is_due(self):
        # The permissive side of the same guard: "already running" must mean
        # running, not "has ever run", or a producer would fire exactly once
        # in the life of the process.
        scheduler = self.scheduler(RunnableProducer("nightly"))
        first = scheduler.tick(NOW_ISO)
        self.assertTrue(self.runner.wait(first.started["nightly"], timeout=5))

        result = scheduler.tick(ahead(DAY))

        self.assertEqual(list(result.started), ["nightly"])
        self.assertEqual(result.skipped, {})
        self.assertEqual(len(self.runner.jobs()), 2)

    def test_a_producer_running_twice_over_has_both_its_jobs_named(self):
        # `local` is unlimited, so a person can start the same producer twice
        # from the interface. Naming one of the two by iteration order would
        # put half the evidence in front of an operator with nothing to say
        # the other half existed.
        gate = self.gate()
        scheduler = self.scheduler(RunnableProducer("nightly", gate=gate))
        first = self.runner.start("nightly")
        second = self.runner.start("nightly")

        result = scheduler.tick(NOW_ISO)

        self.assertEqual(
            result.skipped,
            {"nightly": f"already running as job {first.id}, "
                        f"job {second.id}"})
        self.assertEqual(result.started, {})

    def test_a_saturated_cost_class_is_a_skip_and_not_an_exception(self):
        # `scraping` allows one job at a time, and the due list is sorted, so
        # 'box-scrape' takes the slot and 'full-scrape' is refused. The tick
        # must report that, not let `JobRejected` out.
        gate = self.gate()
        scheduler = self.scheduler(
            RunnableProducer("box-scrape", cost="scraping", gate=gate),
            RunnableProducer("full-scrape", cost="scraping", gate=gate))

        result = scheduler.tick(NOW_ISO)

        self.assertEqual(result.due, ["box-scrape", "full-scrape"])
        self.assertEqual(list(result.started), ["box-scrape"])
        self.assertEqual(
            result.skipped,
            {"full-scrape": "cost class saturated: cost class 'scraping' is "
                            "already running box-scrape"})
        self.assertEqual(result.failed_to_start, {})

    def test_a_saturated_class_leaves_the_refused_producer_due_next_tick(self):
        # A skip is not a run. Recording one for a producer that never
        # started would push it a whole cadence into the future on the
        # strength of having been refused.
        gate = self.gate()
        scheduler = self.scheduler(
            RunnableProducer("box-scrape", cost="scraping", gate=gate),
            RunnableProducer("full-scrape", cost="scraping", gate=gate))
        scheduler.tick(NOW_ISO)
        self.assertIsNone(self.store.last_run("full-scrape"))


class ATickReportsARefusalItCannotRetry(SchedulerCase):
    def test_a_closed_runner_is_reported_rather_than_raising_out_of_the_tick(self):
        scheduler = Scheduler(ClosedRunner([RunnableProducer("nightly")]),
                              self.store)

        result = scheduler.tick(NOW_ISO)

        self.assertEqual(
            result,
            TickResult(at=NOW_ISO, due=["nightly"], started={}, skipped={},
                       failed_to_start={
                           "nightly": "Closed: the runner is closed and is "
                                      "not accepting work; 'nightly' was not "
                                      "started"}))

    def test_a_permanent_refusal_is_not_filed_as_a_transient_one(self):
        # The distinction the whole bucket exists for: `skipped` is "try
        # again next tick", and a closed runner never frees up. Filing it
        # there is a busy loop that never recovers.
        scheduler = Scheduler(ClosedRunner([RunnableProducer("nightly")]),
                              self.store)
        result = scheduler.tick(NOW_ISO)
        self.assertEqual(result.skipped, {})
        self.assertEqual(list(result.failed_to_start), ["nightly"])

    def test_a_refused_start_does_not_count_as_a_run(self):
        scheduler = Scheduler(ClosedRunner([RunnableProducer("nightly")]),
                              self.store)
        scheduler.tick(NOW_ISO)
        self.assertIsNone(self.store.last_run("nightly"))

    def test_one_producer_that_cannot_start_does_not_stop_the_others(self):
        class HalfClosed(ClosedRunner):
            def __init__(self, producers, runner):
                super().__init__(producers)
                self._runner = runner

            def start(self, name):
                if name == "broken":
                    return super().start(name)
                return self._runner.start(name)

        working = RunnableProducer("working")
        self.runner.register(working)
        scheduler = Scheduler(
            HalfClosed([RunnableProducer("broken"), working], self.runner),
            self.store)

        result = scheduler.tick(NOW_ISO)

        self.assertEqual(list(result.started), ["working"])
        self.assertEqual(list(result.failed_to_start), ["broken"])


class TheRunIsRecordedWhetherItWorkedOrNot(SchedulerCase):
    """Failure must not delay the next attempt differently from success.
    Backing off needs the transient-versus-permanent classification that has
    never been wired to anything; done badly it means a producer that fails
    once falls silent for a day."""

    def test_a_producer_that_fails_still_has_its_run_recorded(self):
        scheduler = self.scheduler(RunnableProducer("nightly", boom=True))

        result = scheduler.tick(NOW_ISO)

        job_id = result.started["nightly"]
        self.assertTrue(self.runner.wait(job_id, timeout=5))
        self.assertEqual(self.runner.job(job_id).state, "failed")
        self.assertEqual(self.store.last_run("nightly"), NOW_ISO)

    def test_a_failure_and_a_success_leave_the_next_run_at_the_same_moment(self):
        scheduler = self.scheduler(RunnableProducer("good"),
                                   RunnableProducer("bad", boom=True))

        scheduler.tick(NOW_ISO)
        for job in self.runner.jobs():
            self.assertTrue(self.runner.wait(job.id, timeout=5))

        self.assertEqual({job.producer: job.state
                          for job in self.runner.jobs()},
                         {"good": "done", "bad": "failed"})
        self.assertEqual(self.store.runs(), {"good": NOW_ISO, "bad": NOW_ISO})
        # A second later, neither is due, and for the same reason worded the
        # same way. A back-off would push the failed one further out.
        settled = scheduler.tick(ahead(1))
        self.assertEqual(settled.started, {})
        self.assertEqual(
            settled.skipped,
            {"good": "last ran 2026-07-26T12:00:00+00:00; "
                     "next due at 2026-07-27T12:00:00+00:00",
             "bad": "last ran 2026-07-26T12:00:00+00:00; "
                    "next due at 2026-07-27T12:00:00+00:00"})


class ATickThatRanNothingSaysWhichKindOfNothing(SchedulerCase):
    """A tick that quietly does nothing is indistinguishable from a tick that
    could not do anything, and the second is the one an operator needs."""

    def test_nothing_due_and_everything_blocked_are_different_answers(self):
        gate = self.gate()
        scheduler = self.scheduler(RunnableProducer("nightly", gate=gate))
        scheduler.tick(NOW_ISO)

        blocked = scheduler.tick(ahead(2 * DAY))
        settled = scheduler.tick(ahead(1))

        # Both ran nothing. `due` is what says which kind of nothing it was:
        # work that could not be begun, or no work owing.
        self.assertEqual(blocked.started, {})
        self.assertEqual(blocked.due, ["nightly"])
        self.assertEqual(settled.started, {})
        self.assertEqual(settled.due, [])

    def test_every_scheduled_producer_lands_in_exactly_one_of_the_three(self):
        gate = self.gate()
        scheduler = self.scheduler(
            RunnableProducer("box", cost="scraping", gate=gate),
            RunnableProducer("full", cost="scraping", gate=gate),
            RunnableProducer("local-scan"),
            RunnableProducer("off"),
            overrides={"off": {"enabled": False}})
        self.store.record_run("local-scan", NOW_ISO)

        result = scheduler.tick(NOW_ISO)

        landed = (list(result.started) + list(result.skipped)
                  + list(result.failed_to_start))
        self.assertEqual(sorted(landed), ["box", "full", "local-scan", "off"])
        self.assertEqual(len(landed), len(set(landed)))


if __name__ == "__main__":
    unittest.main()
