"""What is due, why everything else is not, and the loop that asks.

Every scheduling rule here is arithmetic over data — no thread, no sleep, no
wall clock. `now` is a parameter, which is the whole reason the tick is
separated from the loop that drives it: a clock that jumped back a year, a
service that was off for a week, and the exact second a cadence elapses are all
ordinary unit tests here and would each need a fixture with a fake clock inside
a running loop otherwise.

The loop tests at the bottom do run a thread, because a loop is a thread. They
still never sleep. Two things make that possible: an interval of zero, so the
loop's wait between ticks returns at once instead of the test waiting one out;
and a store double that counts the ticks that have begun and hands back a
`threading.Event` set on the nth, so a test waits for the loop to get somewhere
rather than guessing how long it takes. Every wait is bounded, and every bound
is only ever reached by a test that is failing.
"""
import ast
import dataclasses
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from cronicled import schedule
from cronicled.jobs import JobRunner
from cronicled.schedule import (Entry, LoopStatus, Scheduler, TickResult,
                                as_utc, check_zone, due, resolve)
from cronicled.store import Store

HOUR = 3600
DAY = 86400

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-07-26T12:00:00+00:00"

# A zone that OBSERVES daylight saving, which is the whole point of it being
# this one: the two rules about a repeated and a skipped hour cannot fail in a
# zone that has neither, however carefully a test asserts. Its 2026
# transitions are 8 March (02:00 -> 03:00, so 02:30 does not exist) and 1
# November (02:00 -> 01:00, so 01:30 happens twice). Nothing about the zone is
# private: it is an entry in a public database.
ZONE_NAME = "America/New_York"
ZONE = ZoneInfo(ZONE_NAME)

# A second zone for the tests about whose zone is used. Half an hour off UTC
# and with no daylight saving of its own, so an instant computed in it cannot
# coincide with one computed in the zone above, or in UTC, by accident.
OTHER_ZONE_NAME = "Asia/Kolkata"
OTHER_ZONE = ZoneInfo(OTHER_ZONE_NAME)


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat()


def ahead(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def utc(*parts):
    """An aware UTC instant, written out by hand.

    Every expected instant in the stated-time tests below is one of these,
    worked out from the zone's offset on the day in question rather than by
    asking the code under test what it thinks. A test that derives its
    expectation from the thing it is testing can only confirm the code agrees
    with itself.
    """
    return datetime(*parts, tzinfo=timezone.utc)


def sweep(entries, start, minutes, runs=None):
    """Call `due` once a minute from `start`, recording the runs it starts.

    A minute apart because that is the loop's own resolution
    (`DEFAULT_INTERVAL`), and each run is recorded against the tick's own
    `now`, which is what `Scheduler._tick` records — see
    `TheRunIsRecordedWhetherItWorkedOrNot`. Nothing here sleeps or holds a
    clock: the whole sweep is arithmetic over arguments.

    It stands in for a scheduler whose every start is accepted, which is the
    case the timing rules have to be right about. A refused start records
    nothing, so the producer stays due and the next tick tries again; that is
    `_tick`'s rule and it is tested against the real runner, not here.

    Returns `{producer: [instant, ...]}`, so "fired once" and "fired on every
    tick of the hour it named" are different answers rather than the same
    `True`.
    """
    runs = {} if runs is None else dict(runs)
    fired = {}
    for step in range(minutes):
        moment = start + timedelta(minutes=step)
        for name in due(entries, runs, moment)[0]:
            fired.setdefault(name, []).append(moment)
            runs[name] = moment.isoformat()
    return fired


class FakeProducer:
    """Everything `resolve` is allowed to read off a producer: a name, and the
    timing it may or may not declare — a cadence, or a stated time and the zone
    to read it in."""

    def __init__(self, name, every=None, at=None, zone=None):
        self.name = name
        self.every = every
        self.at = at
        self.zone = zone


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


class AStatedTimeResolves(unittest.TestCase):
    """An override may say `every`, or a time of day and a zone, and never
    both."""

    def test_a_stated_time_and_zone_resolve_to_an_entry(self):
        # The whole entry, not a sampled field: an `every` left set alongside
        # the time, or a zone quietly dropped, is exactly what this has to
        # catch.
        self.assertEqual(
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:00", "zone": ZONE_NAME}}),
            {"nightly": Entry(producer="nightly", every=None, enabled=True,
                              at=time(3, 0), zone=ZONE)})

    def test_a_stated_time_replaces_a_declared_cadence(self):
        # The producer still declares a daily interval; moving it to an hour
        # must not leave both in the entry, which `due` would refuse.
        self.assertEqual(
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"at": "03:00", "zone": ZONE_NAME}}),
            {"nightly": Entry(producer="nightly", every=None, enabled=True,
                              at=time(3, 0), zone=ZONE)})

    def test_seconds_may_be_stated_as_well_as_hours_and_minutes(self):
        self.assertEqual(
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:30:15", "zone": ZONE_NAME}}),
            {"nightly": Entry(producer="nightly", every=None, enabled=True,
                              at=time(3, 30, 15), zone=ZONE)})

    def test_a_producer_may_declare_its_own_stated_time(self):
        self.assertEqual(
            resolve([FakeProducer("nightly", at="03:00", zone=ZONE_NAME)]),
            {"nightly": Entry(producer="nightly", every=None, enabled=True,
                              at=time(3, 0), zone=ZONE)})

    def test_a_time_and_a_tzinfo_are_accepted_as_well_as_their_names(self):
        # A config file can only hold strings; a producer written in Python
        # holds the objects, and both reach `resolve`.
        self.assertEqual(
            resolve([FakeProducer("nightly", at=time(3, 0), zone=ZONE)]),
            {"nightly": Entry(producer="nightly", every=None, enabled=True,
                              at=time(3, 0), zone=ZONE)})

    def test_an_interval_and_a_stated_time_live_in_one_config(self):
        # Refusing either form would be a regression for whichever was not
        # chosen: an interval is right for something that runs every few
        # minutes, a stated time for something nightly.
        self.assertEqual(
            resolve([FakeProducer("frequent", every=HOUR),
                     FakeProducer("nightly", every=DAY)],
                    {"nightly": {"at": "03:00", "zone": ZONE_NAME}}),
            {"frequent": Entry(producer="frequent", every=HOUR, enabled=True,
                               at=None, zone=None),
             "nightly": Entry(producer="nightly", every=None, enabled=True,
                              at=time(3, 0), zone=ZONE)})

    def test_a_disabled_producer_may_still_carry_a_stated_time(self):
        self.assertEqual(
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:00", "zone": ZONE_NAME,
                                 "enabled": False}}),
            {"nightly": Entry(producer="nightly", every=None, enabled=False,
                              at=time(3, 0), zone=ZONE)})


class ResolveRefusesAContradictoryTime(unittest.TestCase):
    """Everything about a stated time that can be wrong is refused at wiring
    time. A schedule that will not load is read as a stack trace by whoever is
    deploying; a schedule that loads wrong is found at 3am, or not at all."""

    def test_an_override_naming_both_an_interval_and_a_time_is_refused(self):
        with self.assertRaisesRegex(ValueError, "both"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"every": DAY, "at": "03:00",
                                 "zone": ZONE_NAME}})

    def test_the_refusal_says_which_source_the_contradiction_came_from(self):
        with self.assertRaisesRegex(ValueError, "from the override"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"every": DAY, "at": "03:00",
                                 "zone": ZONE_NAME}})

    def test_a_producer_declaring_both_is_refused_and_named_as_the_source(self):
        with self.assertRaisesRegex(ValueError, "declared by the producer"):
            resolve([FakeProducer("nightly", every=DAY, at="03:00",
                                  zone=ZONE_NAME)])

    def test_a_stated_time_with_no_zone_is_refused(self):
        # The answer to "what happens when the zone is not configured". Not the
        # host's zone, and not an assumed UTC: a startup failure naming the
        # producer.
        with self.assertRaisesRegex(ValueError, "zone"):
            resolve([FakeProducer("nightly")], {"nightly": {"at": "03:00"}})

    def test_that_refusal_holds_for_a_disabled_producer_too(self):
        # Disabling exempts a producer from needing a schedule; it does not
        # turn a broken one into a good one, and the operator who re-enables it
        # is not the one who wrote it.
        with self.assertRaisesRegex(ValueError, "zone"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:00", "enabled": False}})

    def test_a_zone_with_no_stated_time_is_refused(self):
        # It changes nothing about when the producer runs, so whoever wrote it
        # believes something untrue.
        with self.assertRaisesRegex(ValueError, "no time of day"):
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"zone": ZONE_NAME}})

    def test_a_zone_the_system_does_not_know_is_refused(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:00", "zone": "Nowhere/Atlantis"}})

    def test_a_stated_time_carrying_an_offset_is_refused(self):
        # `"03:00+01:00"` is a second way of saying the zone, and it pins an
        # offset that daylight saving moves.
        with self.assertRaisesRegex(ValueError, "offset"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:00+01:00", "zone": ZONE_NAME}})

    def test_a_stated_time_that_is_not_a_time_of_day_is_refused(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "3 o'clock", "zone": ZONE_NAME}})

    def test_a_boolean_stated_time_is_refused(self):
        # For the reason `{"every": true}` is refused: JSON `true` arrives as
        # something Python is happy to treat as a number.
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": True, "zone": ZONE_NAME}})

    def test_a_zone_that_is_not_a_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "nightly"):
            resolve([FakeProducer("nightly")],
                    {"nightly": {"at": "03:00", "zone": 60}})

    def test_an_explicit_null_cadence_is_still_a_cadence_that_is_not_a_number(self):
        # An override that NAMES a schedule key has said something about the
        # timing, even when the value is null — which is a bad value, not the
        # absence `resolve` reports as "no schedule". The two messages send an
        # operator to different lines of different files.
        with self.assertRaisesRegex(ValueError, "number of seconds"):
            resolve([FakeProducer("nightly", every=DAY)],
                    {"nightly": {"every": None}})

    def test_a_producer_with_neither_form_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, "no schedule"):
            resolve([FakeProducer("nightly")])


class AStatedTimeIsDue(unittest.TestCase):
    """03:00 in a named zone, once a night: the hour it names and no other."""

    def setUp(self):
        self.entries = resolve([FakeProducer("nightly")],
                               {"nightly": {"at": "03:00",
                                            "zone": ZONE_NAME}})
        # 03:00 on 25 July 2026 in a zone four hours behind UTC that summer.
        self.last_night = "2026-07-25T07:00:00+00:00"

    def test_a_producer_that_has_never_run_is_due(self):
        self.assertEqual(due(self.entries, {}, NOW_ISO), (["nightly"], {}))

    def test_it_is_due_at_the_instant_the_stated_hour_arrives(self):
        self.assertEqual(
            due(self.entries, {"nightly": self.last_night},
                utc(2026, 7, 26, 7, 0)),
            (["nightly"], {}))

    def test_it_is_not_due_a_minute_before_that(self):
        # The permissive side of the guard above has its own test because a
        # rule that fires an hour early is as wrong as one that never fires,
        # and this pins which minute is which.
        self.assertEqual(
            due(self.entries, {"nightly": self.last_night},
                utc(2026, 7, 26, 6, 59)),
            ([], {"nightly": "last ran 2026-07-25T07:00:00+00:00; "
                             "next due at 2026-07-26T07:00:00+00:00"}))

    def test_once_it_has_run_it_is_not_due_again_until_tomorrow(self):
        self.assertEqual(
            due(self.entries, {"nightly": "2026-07-26T07:00:00+00:00"}, NOW),
            ([], {"nightly": "last ran 2026-07-26T07:00:00+00:00; "
                             "next due at 2026-07-27T07:00:00+00:00"}))

    def test_at_the_stated_instant_the_reason_already_names_tomorrow(self):
        # The tick that has just run it asks again a moment later, and "next
        # due" must have moved on. A "next" that means "at or after now"
        # answers with the appointment just kept, which reads as a schedule
        # that is about to run something it already ran.
        self.assertEqual(
            due(self.entries, {"nightly": "2026-07-26T07:00:00+00:00"},
                utc(2026, 7, 26, 7, 0)),
            ([], {"nightly": "last ran 2026-07-26T07:00:00+00:00; "
                             "next due at 2026-07-27T07:00:00+00:00"}))

    def test_it_fires_exactly_once_over_a_whole_day_of_ticks(self):
        # 1440 ticks a minute apart, from local midnight. A rule that fired on
        # every tick of the hour it named would return sixty instants here, and
        # one that compared only the hour would return sixty more at 03:00
        # tomorrow.
        self.assertEqual(
            sweep(self.entries, utc(2026, 7, 26, 4, 0), 1440,
                  {"nightly": self.last_night}),
            {"nightly": [utc(2026, 7, 26, 7, 0)]})

    def test_an_interval_alongside_it_keeps_its_own_rate(self):
        # Adding a stated time must not disturb the form that was already
        # there, and the two are in one config here rather than in two tests.
        entries = resolve([FakeProducer("frequent", every=HOUR),
                           FakeProducer("nightly")],
                          {"nightly": {"at": "03:00", "zone": ZONE_NAME}})
        fired = sweep(entries, utc(2026, 7, 26, 4, 0), 1440,
                      {"nightly": self.last_night,
                       "frequent": "2026-07-26T03:00:00+00:00"})
        self.assertEqual(fired["nightly"], [utc(2026, 7, 26, 7, 0)])
        self.assertEqual(
            fired["frequent"],
            [utc(2026, 7, 26, 4, 0) + timedelta(hours=n) for n in range(24)])


class AMissedStatedTimeIsOwed(unittest.TestCase):
    """The decision: a machine that was off across 03:00 runs when it comes
    back, at whatever hour that is.

    Skipping is defensible and loses on the failure it produces — a laptop
    that is asleep every night at 03:00 would never scan, and would say
    nothing was wrong. These tests fail if that answer is ever swapped in
    quietly."""

    def setUp(self):
        self.entries = resolve([FakeProducer("nightly")],
                               {"nightly": {"at": "03:00",
                                            "zone": ZONE_NAME}})

    def test_a_gap_spanning_the_stated_hour_makes_it_due_at_once(self):
        # Last ran five nights ago; it is noon. Owed, not left until tonight.
        self.assertEqual(
            due(self.entries, {"nightly": "2026-07-21T07:00:00+00:00"},
                utc(2026, 7, 26, 16, 0)),
            (["nightly"], {}))

    def test_five_missed_nights_are_owed_once_and_not_five_times(self):
        # Owing is not queueing: five scrapes at a media server is the load
        # the cost classes exist to prevent.
        self.assertEqual(
            sweep(self.entries, utc(2026, 7, 26, 16, 0), 240,
                  {"nightly": "2026-07-21T07:00:00+00:00"}),
            {"nightly": [utc(2026, 7, 26, 16, 0)]})

    def test_after_the_owed_run_it_settles_back_onto_the_stated_hour(self):
        # The owed run happens at noon; the next is the following 03:00 rather
        # than a day after noon, which is the difference between a stated time
        # and the interval this replaced.
        self.assertEqual(
            sweep(self.entries, utc(2026, 7, 26, 16, 0), 1440,
                  {"nightly": "2026-07-21T07:00:00+00:00"}),
            {"nightly": [utc(2026, 7, 26, 16, 0), utc(2026, 7, 27, 7, 0)]})


class DaylightSaving(unittest.TestCase):
    """The two days a year a wall clock is not a clock.

    Both use a zone that observes daylight saving, because in a zone that does
    not there is nothing here to get wrong and a test asserting there is would
    pass whatever the code did."""

    def entries_at(self, stated):
        return resolve([FakeProducer("nightly")],
                       {"nightly": {"at": stated, "zone": ZONE_NAME}})

    def test_the_hour_that_happens_twice_fires_once(self):
        # 1 November 2026: 01:30 arrives at 05:30 UTC on summer time and again
        # at 06:30 UTC on winter time. 1500 ticks covers the whole 25-hour
        # local day.
        self.assertEqual(
            sweep(self.entries_at("01:30"), utc(2026, 11, 1, 4, 0), 1500,
                  {"nightly": "2026-10-31T05:30:00+00:00"}),
            {"nightly": [utc(2026, 11, 1, 5, 30)]})

    def test_and_it_is_the_first_of_the_two_readings(self):
        # Which of the two is not cosmetic: the second is an hour late for no
        # reason. Asserted through the local reading, so the assertion says
        # 01:30 on the offset that was in force first rather than restating
        # the UTC instant above.
        fired, = sweep(self.entries_at("01:30"), utc(2026, 11, 1, 4, 0), 1500,
                       {"nightly": "2026-10-31T05:30:00+00:00"})["nightly"]
        local = fired.astimezone(ZONE)
        self.assertEqual(local.time(), time(1, 30))
        self.assertEqual(local.utcoffset(), timedelta(hours=-4))

    def test_the_day_after_the_clocks_go_back_fires_at_the_stated_time_again(self):
        # 01:30 on winter time is 06:30 UTC. The shift belongs to the
        # transition day and to no other, and without this the rule above
        # could be "always an hour early after November".
        self.assertEqual(
            sweep(self.entries_at("01:30"), utc(2026, 11, 2, 5, 0), 1440,
                  {"nightly": "2026-11-01T05:30:00+00:00"}),
            {"nightly": [utc(2026, 11, 2, 6, 30)]})

    def test_the_hour_that_does_not_exist_fires_once(self):
        # 8 March 2026: the local clock goes 01:59 -> 03:00, so 02:30 is never
        # read. 1380 ticks covers the whole 23-hour local day.
        self.assertEqual(
            sweep(self.entries_at("02:30"), utc(2026, 3, 8, 5, 0), 1380,
                  {"nightly": "2026-03-07T07:30:00+00:00"}),
            {"nightly": [utc(2026, 3, 8, 7, 30)]})

    def test_and_it_fires_after_the_gap_rather_than_before_it(self):
        # 07:30 UTC reads as 03:30 local: the stated 02:30 pushed past the
        # gap, not pulled back to 01:30 before it. A job stated for 02:30 must
        # not run at 01:30, in an hour its operator kept for something else.
        fired, = sweep(self.entries_at("02:30"), utc(2026, 3, 8, 5, 0), 1380,
                       {"nightly": "2026-03-07T07:30:00+00:00"})["nightly"]
        local = fired.astimezone(ZONE)
        self.assertEqual(local.time(), time(3, 30))
        self.assertGreater(local.time(), time(2, 30))

    def test_the_day_after_the_clocks_go_forward_fires_at_the_stated_time(self):
        # 02:30 on summer time is 06:30 UTC, the ordinary reading.
        self.assertEqual(
            sweep(self.entries_at("02:30"), utc(2026, 3, 9, 4, 0), 1440,
                  {"nightly": "2026-03-08T07:30:00+00:00"}),
            {"nightly": [utc(2026, 3, 9, 6, 30)]})


class AnOccurrencePushedPastMidnight(unittest.TestCase):
    """A zone whose spring-forward gap is the LAST hour of the local day, so
    the rule that fires after a gap puts that day's appointment on the next
    date.

    This is the case that decides how far back the search for a passed
    occurrence has to go, and it is not hypothetical: this zone's clock has
    gone 22:59 -> 00:00 every spring since 2024. A window of one date back
    cannot answer it, and answering it wrongly is a producer that fires early,
    twice, or not at all on one night a year.

    The instants below are the zone's own, on a transition that has already
    happened: 29 March 2025 in this zone ends at 22:59 local, and the offset
    moves from two hours behind UTC to one."""

    ZONE_NAME = "America/Nuuk"

    def setUp(self):
        self.entries = resolve([FakeProducer("nightly")],
                               {"nightly": {"at": "23:59",
                                            "zone": self.ZONE_NAME}})
        # 23:59 on 28 March, the last ordinary night before the transition.
        self.last_night = "2025-03-29T01:59:00+00:00"

    def test_the_appointment_it_owes_is_the_one_before_the_gap(self):
        # Local midnight on 30 March. The occurrence named for 29 March has
        # been pushed to 00:59 on the 30th and has NOT happened yet, so what
        # this run is measured against is 28 March's — two dates back.
        self.assertEqual(
            due(self.entries, {"nightly": self.last_night},
                utc(2025, 3, 30, 1, 0)),
            ([], {"nightly": "last ran 2025-03-29T01:59:00+00:00; "
                             "next due at 2025-03-30T01:59:00+00:00"}))

    def test_it_fires_once_on_the_night_the_hour_goes_missing(self):
        # A sweep of the whole local day of 29 March and the hour after it.
        # 02:00 UTC is local midnight on the 29th; the one appointment kept in
        # that window is 00:59 local on the 30th, after the gap.
        fired = sweep(self.entries, utc(2025, 3, 29, 2, 0), 1440,
                      {"nightly": self.last_night})
        self.assertEqual(fired, {"nightly": [utc(2025, 3, 30, 1, 59)]})
        self.assertEqual(fired["nightly"][0].astimezone(
            ZoneInfo(self.ZONE_NAME)).isoformat(), "2025-03-30T00:59:00-01:00")


class AZoneThatGoesBackJustAfterMidnight(unittest.TestCase):
    """The mirror image of the class above, and the reason the search for an
    occurrence looks FORWARD of the local date as well as back.

    This zone put its clocks back at one minute past midnight. So on the night
    of 6 November 2010 the local clock reached 00:00 on the 7th, ran for a
    minute, and went back to 23:01 on the 6th — which means that at 23:30 on
    the 6th, midnight of the 7th is already in the PAST. A producer stated at
    00:00 must not keep that appointment twice, and the answer to "when next"
    is two dates ahead of the date the clock is showing.

    Measured rather than imagined: enumerating every zone `zoneinfo` knows over
    every offset change between 1970 and 2040, this shape is where the search
    window has to reach furthest, and this zone on this night is the most
    recent instance of it."""

    ZONE_NAME = "America/St_Johns"

    def setUp(self):
        self.entries = resolve([FakeProducer("nightly")],
                               {"nightly": {"at": "00:00",
                                            "zone": self.ZONE_NAME}})

    def test_the_appointment_just_kept_is_not_owed_again(self):
        # 03:00 UTC reads as 23:30 on the 6th locally, after the clocks went
        # back — and midnight of the 7th happened half an hour earlier.
        self.assertEqual(
            due(self.entries, {"nightly": "2010-11-07T02:30:00+00:00"},
                utc(2010, 11, 7, 3, 0)),
            ([], {"nightly": "last ran 2010-11-07T02:30:00+00:00; "
                             "next due at 2010-11-08T03:30:00+00:00"}))

    def test_a_midnight_that_has_passed_is_owed_even_from_a_later_date(self):
        self.assertEqual(
            due(self.entries, {"nightly": "2010-11-06T02:30:00+00:00"},
                utc(2010, 11, 7, 3, 0)),
            (["nightly"], {}))

    def test_it_fires_once_per_local_midnight_over_the_long_night(self):
        # The 25-hour local day of 6 November: two midnights fall in it, the
        # 6th's at its start and the 7th's at its end, and neither is kept
        # twice while the clock reads 23:30 for the second time.
        self.assertEqual(
            sweep(self.entries, utc(2010, 11, 6, 2, 30), 1500,
                  {"nightly": "2010-11-05T02:30:00+00:00"}),
            {"nightly": [utc(2010, 11, 6, 2, 30), utc(2010, 11, 7, 2, 30)]})


class AHalfHourThatDoesNotExist(unittest.TestCase):
    """Not every transition is a whole hour, and not every zone is behind UTC.

    This one moved HALF an hour forward at 23:30 on 4 May 2018, so 23:59 that
    evening never happened locally and the appointment for the 4th landed at
    00:29 on the 5th — the same shape as the class above, at a different
    granularity and in a zone nine hours ahead rather than one behind. A rule
    written around whole hours, or around zones west of UTC, passes everything
    above this line and fails here."""

    ZONE_NAME = "Asia/Pyongyang"

    def setUp(self):
        self.entries = resolve([FakeProducer("nightly")],
                               {"nightly": {"at": "23:59",
                                            "zone": self.ZONE_NAME}})
        # 23:59 on 3 May, when the zone was half an hour behind what it became.
        self.last_night = "2018-05-03T15:29:00+00:00"

    def test_it_is_not_due_yet_and_the_reason_reaches_back_two_dates(self):
        self.assertEqual(
            due(self.entries, {"nightly": self.last_night},
                utc(2018, 5, 4, 15, 0)),
            ([], {"nightly": "last ran 2018-05-03T15:29:00+00:00; "
                             "next due at 2018-05-04T15:29:00+00:00"}))

    def test_it_fires_once_after_the_half_hour_that_does_not_exist(self):
        fired = sweep(self.entries, utc(2018, 5, 4, 15, 0), 60,
                      {"nightly": self.last_night})
        self.assertEqual(fired, {"nightly": [utc(2018, 5, 4, 15, 29)]})
        self.assertEqual(fired["nightly"][0].astimezone(
            ZoneInfo(self.ZONE_NAME)).isoformat(), "2018-05-05T00:29:00+09:00")


class AStatedTimeIsReadInItsOwnZone(unittest.TestCase):
    """Never the host's. A container runs in UTC while the person who
    configured it thinks in their own hour, and an appointment kept four hours
    from the one that was asked for looks correct in every log."""

    def test_two_producers_at_one_stated_time_fire_in_their_own_zones(self):
        # 03:00 is 07:00 UTC in one zone and 21:30 UTC the previous day in the
        # other. Both instants are hand-computed from the offsets; a schedule
        # that read either in the host's zone would fire them together.
        entries = resolve(
            [FakeProducer("west"), FakeProducer("east")],
            {"west": {"at": "03:00", "zone": ZONE_NAME},
             "east": {"at": "03:00", "zone": OTHER_ZONE_NAME}})
        self.assertEqual(
            sweep(entries, utc(2026, 7, 26, 0, 0), 1440,
                  {"west": "2026-07-25T07:00:00+00:00",
                   "east": "2026-07-25T21:30:00+00:00"}),
            {"west": [utc(2026, 7, 26, 7, 0)],
             "east": [utc(2026, 7, 26, 21, 30)]})

    def test_the_zone_that_was_asked_for_is_the_one_in_the_entry(self):
        entries = resolve([FakeProducer("east")],
                          {"east": {"at": "03:00", "zone": OTHER_ZONE_NAME}})
        self.assertEqual(entries["east"].zone, OTHER_ZONE)

    def test_due_refuses_an_entry_with_a_time_and_no_zone(self):
        # `resolve` cannot build one, and a hand-built one must not fall back
        # to the host's zone here either — the fallback would be invisible
        # everywhere except in the hour things happened.
        entries = {"nightly": Entry(producer="nightly", every=None,
                                    enabled=True, at=time(3, 0), zone=None)}
        with self.assertRaisesRegex(ValueError, "zone"):
            due(entries, {}, NOW_ISO)

    def test_due_refuses_an_entry_holding_both_forms(self):
        entries = {"nightly": Entry(producer="nightly", every=DAY,
                                    enabled=True, at=time(3, 0), zone=ZONE)}
        with self.assertRaisesRegex(ValueError, "both"):
            due(entries, {}, NOW_ISO)


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


class StatedProducer(RunnableProducer):
    """A producer the runner can drive that declares a STATED TIME instead of
    a cadence -- the shape all three unattended passes really declare, and the
    one a status has an hour to report for."""

    def __init__(self, name, at, zone, cost="local"):
        super().__init__(name, every=None, cost=cost)
        self.at = at
        self.zone = zone


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

    def start(self, name, *, trigger):
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

    def scheduler(self, *producers, overrides=None, clock=None, store=None,
                  interval=None):
        for producer in producers:
            self.runner.register(producer)
        extra = {} if interval is None else {"interval": interval}
        return Scheduler(self.runner, self.store if store is None else store,
                         overrides=overrides, clock=clock, **extra)

    def looping(self, scheduler):
        """Start the loop, and stop it before the store underneath it goes.

        Registered before `start`, and `addCleanup` runs last-in-first-out, so
        this closes ahead of `SchedulerCase`'s own teardown — a loop still
        ticking against a closed database would spend the rest of the suite
        recording failures nobody asked for.
        """
        self.addCleanup(scheduler.close, 5)
        scheduler.start()
        return scheduler


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

    def test_a_tick_records_the_run_as_scheduled_not_as_a_manual_one(self):
        """The whole reason `trigger` is stored rather than inferred. A
        reader asking "did last night's pass run" is asking about the
        unattended run; a tick that filed its own starts as manual would
        answer with whichever button was last pressed, and the two are
        indistinguishable once written."""
        scheduler = self.scheduler(RunnableProducer("nightly"))

        scheduler.tick(NOW_ISO)

        jobs = self.runner.jobs()
        self.assertTrue(self.runner.wait(jobs[0].id, timeout=5))
        rows = self.store.recent_runs()
        self.assertEqual([(r["job"], r["trigger"], r["outcome"]) for r in rows],
                         [("nightly", "scheduled", "completed")])

    def test_the_scheduler_still_answers_how_long_ago_from_its_own_table(self):
        """The run log is beside `producer_run`, not instead of it. The
        scheduler's next decision is made from `last_run`, and a log that
        quietly became its source would put the scheduler's answer under a
        retention bound that was never meant to hold it."""
        scheduler = self.scheduler(RunnableProducer("nightly"))

        scheduler.tick(NOW_ISO)

        jobs = self.runner.jobs()
        self.assertTrue(self.runner.wait(jobs[0].id, timeout=5))
        self.assertEqual(self.store.runs(), {"nightly": NOW_ISO})

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
        first = self.runner.start("nightly", trigger="manual")
        second = self.runner.start("nightly", trigger="manual")

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

            def start(self, name, *, trigger):
                if name == "broken":
                    return super().start(name, trigger=trigger)
                return self._runner.start(name, trigger=trigger)

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


class ATickKeepsAStatedTimeAppointment(SchedulerCase):
    """The pure rule reaching the real runner and the real store. Every rule
    above this line is arithmetic over arguments; this is the one test that the
    arithmetic is what a tick asks."""

    def scheduled_at_three(self):
        return self.scheduler(
            RunnableProducer("nightly", every=None),
            overrides={"nightly": {"at": "03:00", "zone": ZONE_NAME}})

    def test_a_tick_at_the_stated_hour_starts_it_and_records_the_run(self):
        scheduler = self.scheduled_at_three()
        result = scheduler.tick(utc(2026, 7, 26, 7, 0))
        self.assertEqual(result.due, ["nightly"])
        self.assertEqual(list(result.started), ["nightly"])
        self.assertEqual(result.skipped, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(self.store.runs(),
                         {"nightly": "2026-07-26T07:00:00+00:00"})

    def test_the_next_tick_in_the_same_hour_does_not_start_it_again(self):
        scheduler = self.scheduled_at_three()
        scheduler.tick(utc(2026, 7, 26, 7, 0))
        again = scheduler.tick(utc(2026, 7, 26, 7, 1))
        self.assertEqual(again.due, [])
        self.assertEqual(again.started, {})
        self.assertEqual(again.skipped,
                         {"nightly": "last ran 2026-07-26T07:00:00+00:00; "
                                     "next due at 2026-07-27T07:00:00+00:00"})

    def test_a_tick_before_the_hour_still_owes_a_first_run(self):
        scheduler = self.scheduled_at_three()
        result = scheduler.tick(utc(2026, 7, 26, 6, 0))
        # It has never run, so it is owed its first appointment immediately —
        # the same rule as a missed one. Pinned here rather than left implied,
        # because the alternative reading of this tick is that a stated time
        # waits for its hour before it has ever run at all.
        self.assertEqual(result.due, ["nightly"])


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


class StoreGoneAway(Exception):
    """What a store that can no longer be read raises. Raised as a fresh
    instance every time: re-raising one instance in a hot loop appends to its
    traceback on every raise."""


class CannotWrite(Exception):
    """What a store that can be read but not written raises."""


class ClockStopped(Exception):
    """What a clock that cannot be read raises."""


class LoopAbandoned(BaseException):
    """Deliberately not an `Exception`: the class of thing the loop records
    and then lets end it, rather than overriding."""


def gone_away():
    return StoreGoneAway("the database file is gone")


def abandoned():
    return LoopAbandoned("the interpreter is going down")


class WatchedStore:
    """The real store, with a count of the ticks that have begun and a way to
    make one fail.

    Every tick reads `runs()` exactly once, before it decides anything, so
    counting that call counts ticks *begun* — which is exactly what the loop
    tests need to know: a loop that came round again is a loop that survived
    whatever the last tick did to it. `reached(n)` hands back an `Event` set on
    the nth call, so a test waits for the loop to get there rather than
    sleeping to give it time.

    `read_fails` makes every tick fail at that first read: a callable that
    builds the exception rather than the exception itself, so no instance is
    ever raised twice. `write_fails` makes a tick fail after the runner has
    already been asked to start something, which is the case that abandons the
    producers further down the due list.
    """

    def __init__(self, store, read_fails=None, write_fails=False):
        self._store = store
        self._read_fails = read_fails
        self._write_fails = write_fails
        self._lock = threading.Lock()
        self._waiters = []
        self.ticks = 0

    def reached(self, n):
        event = threading.Event()
        with self._lock:
            if self.ticks >= n:
                event.set()
            else:
                self._waiters.append((n, event))
        return event

    def runs(self):
        with self._lock:
            self.ticks += 1
            for wanted, event in self._waiters:
                if self.ticks >= wanted:
                    event.set()
        if self._read_fails is not None:
            raise self._read_fails()
        return self._store.runs()

    def record_run(self, producer, at=None):
        if self._write_fails:
            raise CannotWrite("the database is read-only")
        return self._store.record_run(producer, at)

    def last_run(self, producer):
        return self._store.last_run(producer)


class BrokenClock:
    """A clock that cannot be read, counting its calls so a test can wait for
    the loop to come round instead of sleeping.

    It is called twice per failed tick — once by the tick, and once again by
    the loop stamping the failure — which is the point: the second call is the
    one that would lose the failure record if the loop trusted it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters = []
        self.calls = 0

    def reached(self, n):
        event = threading.Event()
        with self._lock:
            if self.calls >= n:
                event.set()
            else:
                self._waiters.append((n, event))
        return event

    def __call__(self):
        with self._lock:
            self.calls += 1
            for wanted, event in self._waiters:
                if self.calls >= wanted:
                    event.set()
        raise ClockStopped("the clock is not readable")


class WedgedStore:
    """A store whose read does not come back until the test says so, so the
    tick that called it — and the loop inside it — is genuinely stuck."""

    def __init__(self, store):
        self._store = store
        self.entered = threading.Event()
        self.release = threading.Event()

    def runs(self):
        self.entered.set()
        self.release.wait(5)
        return self._store.runs()

    def record_run(self, producer, at=None):
        return self._store.record_run(producer, at)


class LockWatchingStore:
    """Answers, from inside a tick, whether the tick held its lock.

    `runs()` is called by `tick` itself, on the ticking thread, so a
    non-reentrant lock that is held refuses this acquire and one that is not
    held grants it. No second thread, no timing, nothing to race.
    """

    def __init__(self, store):
        self._store = store
        self.scheduler = None
        self.held_during_tick = None

    def runs(self):
        lock = self.scheduler._tick_lock
        taken = lock.acquire(blocking=False)
        if taken:
            lock.release()
        self.held_during_tick = not taken
        return self._store.runs()

    def record_run(self, producer, at=None):
        return self._store.record_run(producer, at)


class RefusesUntilAsked(ClosedRunner):
    """Refuses every start until it is opened, then starts them for real."""

    def __init__(self, producers, runner, opened):
        super().__init__(producers)
        self._runner = runner
        self._opened = opened

    def jobs(self):
        return self._runner.jobs()

    def start(self, name, *, trigger):
        if not self._opened.is_set():
            return super().start(name, trigger=trigger)
        return self._runner.start(name, trigger=trigger)


class TheIntervalBetweenTicks(SchedulerCase):
    def test_an_interval_of_zero_is_accepted(self):
        # The permissive side, and the one every loop test below relies on:
        # zero means the loop never waits, which is how a test drives it
        # without a sleep.
        self.scheduler(RunnableProducer("nightly"), interval=0)

    def test_a_negative_interval_is_refused(self):
        with self.assertRaisesRegex(ValueError, "interval"):
            self.scheduler(RunnableProducer("nightly"), interval=-1)

    def test_an_interval_that_is_not_a_number_is_refused(self):
        with self.assertRaisesRegex(ValueError, "interval"):
            self.scheduler(RunnableProducer("nightly"), interval="60")

    def test_a_boolean_interval_is_refused(self):
        # `True` is an `int` in Python, so a config typo would otherwise
        # resolve to a one-second loop.
        with self.assertRaisesRegex(ValueError, "interval"):
            self.scheduler(RunnableProducer("nightly"), interval=True)


class TheResolvedScheduleIsTheSchedulersOwn(unittest.TestCase):
    """`status()` is the whole of what anything outside this module may know
    about the schedule.

    The shortcut this exists to prevent is a page reading `Scheduler._entries`
    directly. It would work, and it would couple rendering to a structure this
    module keeps internal -- so the next change to how a schedule is resolved
    would break the page, with no test standing between the two. Stated as a
    rule over the source rather than as a habit, because a habit is not
    something a later change can fail.
    """

    def _package(self):
        return pathlib.Path(schedule.__file__).resolve().parent

    @staticmethod
    def _reaches_in(path):
        """Does this file read `<something>._entries` as an attribute?

        PARSED, not searched for as text: prose is allowed to name the rule --
        the docstrings on both sides of this boundary do -- and a guard that
        counted a mention as a violation would make the rule unwritable
        exactly where it most needs explaining.
        """
        tree = ast.parse(path.read_text(), filename=str(path))
        return any(isinstance(node, ast.Attribute) and node.attr == "_entries"
                   for node in ast.walk(tree))

    def test_nothing_outside_this_module_reaches_into_the_resolved_entries(self):
        package = self._package()
        reaching = sorted(path.relative_to(package).as_posix()
                          for path in package.rglob("*.py")
                          if path.name != "schedule.py"
                          and self._reaches_in(path))
        self.assertEqual(
            reaching, [],
            "these read the scheduler's resolved entries directly; the "
            "schedule reaches a caller through `Scheduler.status()` and "
            "nowhere else, so that how a schedule is resolved stays this "
            "module's to change")

    def test_the_rule_is_looking_at_the_files_it_thinks_it_is(self):
        # The guard above passes just as happily over an empty file list, and
        # a walk that stopped matching would report "nothing reaches in" about
        # a package it never opened. Both halves are checked: the module the
        # page really renders from is among the files scanned, and the rule
        # finds the reach-in that is SUPPOSED to be there, in the one file
        # allowed to have it.
        package = self._package()
        scanned = {path.relative_to(package).as_posix()
                   for path in package.rglob("*.py")
                   if path.name != "schedule.py"}
        self.assertIn("web/rows.py", scanned)
        self.assertIn("__main__.py", scanned)
        self.assertTrue(self._reaches_in(package / "schedule.py"))


class TheLoopTicksInTheBackground(SchedulerCase):
    def test_a_scheduler_that_has_never_started_says_so(self):
        scheduler = self.scheduler(RunnableProducer("nightly"))
        # The whole status, every field at once, against values written out
        # here -- including the schedule, which is true BEFORE the first tick
        # and is the half a count of ticks can never say anything about.
        self.assertEqual(
            scheduler.status(),
            LoopStatus(running=False, closed=False, ticks=0, failures=0,
                       consecutive_failures=0, last_tick_at=None,
                       last_error=None, last_error_at=None,
                       last_traceback=None,
                       appointments={"nightly": Entry(
                           producer="nightly", every=DAY, enabled=True,
                           at=None, zone=None)},
                       failing_to_start={}))

    def test_the_status_states_the_schedule_as_well_as_the_ticks(self):
        # THE WIDENING, tested as its own rule. Every other field on
        # `LoopStatus` is an observation about ticks; this one is the
        # declaration, and it is the only route anything outside this module
        # has to the resolved schedule -- so a `status()` that stopped
        # reporting it would leave a page with no way to say when a pass is
        # due except by reaching into `Scheduler._entries`.
        scheduler = self.scheduler(
            RunnableProducer("nightly"),
            StatedProducer("overnight", at=time(3, 20),
                           zone=ZoneInfo("Europe/Madrid")))
        self.assertEqual(scheduler.status().appointments, {
            "nightly": Entry(producer="nightly", every=DAY, enabled=True,
                             at=None, zone=None),
            "overnight": Entry(producer="overnight", every=None, enabled=True,
                               at=time(3, 20),
                               zone=ZoneInfo("Europe/Madrid")),
        })

    def test_editing_the_answer_does_not_edit_the_schedule(self):
        # A copy, for the reason `failing_to_start` is copied. A caller
        # holding the status must not be able to unschedule a producer, or
        # add one, in the loop this is still ticking.
        scheduler = self.scheduler(RunnableProducer("nightly"))
        answer = scheduler.status().appointments
        answer["nightly"] = "tampered"
        answer["invented"] = "also tampered"
        self.assertEqual(scheduler.status().appointments, {
            "nightly": Entry(producer="nightly", every=DAY, enabled=True,
                             at=None, zone=None)})

    def test_a_status_cannot_be_built_without_stating_its_appointments(self):
        # The docstring on the field argues that a defaulted empty mapping
        # would read on the page exactly like a deployment with nothing
        # scheduled, so a schedule that went missing would be invisible.
        # That argument was unpinned: giving the field a default left every
        # test passing, because no current caller omits it. This fails the
        # moment one could.
        fields = {f.name: f for f in dataclasses.fields(LoopStatus)}
        self.assertIs(
            fields["appointments"].default, dataclasses.MISSING,
            "appointments must stay required")
        self.assertIs(
            fields["appointments"].default_factory, dataclasses.MISSING,
            "appointments must stay required")
        with self.assertRaises(TypeError):
            LoopStatus(running=True, closed=False, ticks=0, failures=0,
                       consecutive_failures=0, last_tick_at=None,
                       last_error=None, last_error_at=None,
                       last_traceback=None)

    def test_starting_the_loop_ticks_for_real_until_it_is_closed(self):
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)

        self.looping(scheduler)
        self.assertTrue(watched.reached(3).wait(5))
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        self.assertFalse(status.running)
        self.assertTrue(status.closed)
        self.assertGreaterEqual(status.ticks, 3)
        self.assertEqual(status.failures, 0)
        self.assertIsNone(status.last_error)
        self.assertEqual(status.last_tick_at, NOW_ISO)
        # It ran the real tick, not a stub of one: the producer was started
        # through the runner, its run was recorded against the injected
        # clock, and the cadence then held — one job, not one per tick.
        self.assertEqual(len(self.runner.jobs()), 1)
        self.assertEqual(self.store.last_run("nightly"), NOW_ISO)

    def test_the_first_tick_does_not_wait_for_the_interval_to_pass(self):
        # A restart must not leave an overdue producer waiting a full interval
        # before anything even looks at it.
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=HOUR, clock=lambda: NOW)

        self.looping(scheduler)

        self.assertTrue(watched.reached(1).wait(5))

    def test_close_returns_promptly_rather_than_waiting_out_the_interval(self):
        # The whole point of waiting on an event rather than sleeping. An
        # hourly scheduler that took an hour to shut down would be stopped
        # with a kill signal instead, every time.
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=HOUR, clock=lambda: NOW)
        self.looping(scheduler)
        self.assertTrue(watched.reached(1).wait(5))

        self.assertTrue(scheduler.close(5))

        self.assertFalse(scheduler.status().running)

    def test_close_stops_the_ticking_and_leaves_a_running_producer_alone(self):
        # There is no cancellation, so closing bounds *starts*, not runs. A
        # close that claimed to stop the work would be the stronger promise
        # this ticket deliberately does not make.
        gate = self.gate()
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly", gate=gate),
                                   store=watched, interval=0,
                                   clock=lambda: NOW)
        self.looping(scheduler)
        self.assertTrue(watched.reached(2).wait(5))

        self.assertTrue(scheduler.close(5))

        job = self.runner.jobs()[0]
        self.assertEqual(self.runner.job(job.id).state, "running")

    def test_close_is_idempotent(self):
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)
        self.looping(scheduler)
        self.assertTrue(watched.reached(1).wait(5))

        self.assertTrue(scheduler.close(5))
        self.assertTrue(scheduler.close(5))

        self.assertTrue(scheduler.status().closed)

    def test_close_says_so_when_it_could_not_stop_the_loop(self):
        # The return value is the answer to "did it stop", not a formality: a
        # tick wedged in the store or the runner outlives the timeout, and an
        # operator who was told `True` would go on to close the database
        # under a loop that is still using it.
        wedged = WedgedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=wedged,
                                   interval=0, clock=lambda: NOW)
        self.looping(scheduler)
        self.addCleanup(wedged.release.set)
        self.assertTrue(wedged.entered.wait(5))

        # No timeout to wait out: the loop is inside the tick right now, so
        # joining for zero seconds is a complete answer.
        self.assertFalse(scheduler.close(0))

        wedged.release.set()
        self.assertTrue(scheduler.close(5))

    def test_closing_a_scheduler_that_never_started_is_not_an_error(self):
        scheduler = self.scheduler(RunnableProducer("nightly"))
        self.assertTrue(scheduler.close(5))
        self.assertTrue(scheduler.status().closed)

    def test_starting_twice_is_an_error_rather_than_two_loops(self):
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)
        self.looping(scheduler)

        with self.assertRaisesRegex(RuntimeError, "already"):
            scheduler.start()

    def test_starting_again_after_close_is_an_error(self):
        scheduler = self.scheduler(RunnableProducer("nightly"), interval=0,
                                   clock=lambda: NOW)
        self.assertTrue(scheduler.close(5))

        with self.assertRaisesRegex(RuntimeError, "closed"):
            scheduler.start()

    def test_a_producer_registered_after_the_scheduler_was_built_is_refused(self):
        # The schedule is resolved once, in the constructor, so a producer
        # registered afterwards would be one nobody ever runs and nothing ever
        # mentions. Starting the loop is the last moment that can be said out
        # loud, so it is said there.
        scheduler = self.scheduler(RunnableProducer("nightly"), interval=0)
        self.runner.register(RunnableProducer("added-later"))

        with self.assertRaisesRegex(ValueError, "added-later"):
            scheduler.start()

    def test_the_loop_starts_when_the_schedule_covers_every_producer(self):
        # The permissive side of the same check: it must not refuse a
        # schedule that is merely disabled, or one built before a producer was
        # registered *with* the scheduler.
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"),
                                   RunnableProducer("off"), store=watched,
                                   interval=0, clock=lambda: NOW,
                                   overrides={"off": {"enabled": False}})

        self.looping(scheduler)

        self.assertTrue(watched.reached(1).wait(5))


class OnlyOneTickAtATime(SchedulerCase):
    """`jobs()` is a snapshot, so two ticks running at once could both see a
    producer idle and start it twice — the one thing the tick promises not to
    do. That there is a single loop thread today is not the reason it cannot
    happen: `tick()` is public, and an interface calling it by hand while the
    loop runs is the obvious next use of it.

    Forcing a real overlap and watching it fail would mean holding one tick
    open while another tries to get in, and "tries, and does not get in" can
    only be observed by waiting for a moment that never arrives — a sleep.
    So this pins the mechanism instead, asserted from inside the tick itself
    on the tick's own thread, where there is nothing to race.
    """

    def test_a_tick_holds_a_lock_a_second_tick_would_have_to_wait_for(self):
        watching = LockWatchingStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watching)
        watching.scheduler = scheduler

        scheduler.tick(NOW_ISO)

        self.assertIs(watching.held_during_tick, True)

    def test_the_lock_is_released_when_the_tick_ends(self):
        scheduler = self.scheduler(RunnableProducer("nightly"))
        scheduler.tick(NOW_ISO)
        self.assertTrue(scheduler._tick_lock.acquire(blocking=False))
        scheduler._tick_lock.release()

    def test_the_lock_is_released_by_a_tick_that_raised(self):
        # A lock a failed tick kept would wedge every later tick — the loop
        # would block for ever on its next round, which is the silent stop
        # this whole task exists to rule out.
        watched = WatchedStore(self.store, read_fails=gone_away)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched)
        with self.assertRaises(StoreGoneAway):
            scheduler.tick(NOW_ISO)
        self.assertTrue(scheduler._tick_lock.acquire(blocking=False))
        scheduler._tick_lock.release()


class TheLoopSurvivesATickThatRaises(SchedulerCase):
    """An exception on a thread fails nothing visible — it prints, and the
    thread dies. A scheduler whose loop died three days ago looks exactly like
    a scheduler with nothing to do, and the inbox simply stops filling. Both
    halves are pinned here: the loop comes round again, and the failure is
    somewhere an operator could find it.
    """

    def test_a_tick_that_raises_every_time_does_not_kill_the_loop(self):
        watched = WatchedStore(self.store, read_fails=gone_away)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)

        self.looping(scheduler)
        # Three ticks *begun*, so it came round twice after the first failure.
        self.assertTrue(watched.reached(3).wait(5))
        self.assertTrue(scheduler.status().running)
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        self.assertEqual(status.ticks, 0)
        self.assertGreaterEqual(status.failures, 3)
        self.assertGreaterEqual(status.consecutive_failures, 3)

    def test_the_failure_is_recorded_where_an_operator_could_find_it(self):
        watched = WatchedStore(self.store, read_fails=gone_away)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)
        self.looping(scheduler)
        self.assertTrue(watched.reached(1).wait(5))
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        # The type as well as the message, for the reason the runner names it
        # too: `str(exc)` is '' for a bare `raise SomeError()`.
        self.assertEqual(status.last_error,
                         "StoreGoneAway: the database file is gone")
        self.assertEqual(status.last_error_at, NOW_ISO)
        self.assertIn("StoreGoneAway", status.last_traceback)
        # The frames, not just the name: which line gave up is the whole
        # value of keeping a traceback at all.
        self.assertIn("runs", status.last_traceback)

    def test_a_store_that_cannot_record_a_run_does_not_stop_the_loop(self):
        # The tick abandons the rest of the due list when `record_run` raises,
        # deliberately — a store that cannot write is a larger failure than a
        # missed producer. The loop is where that becomes visible instead of
        # being the end of the schedule.
        gate = self.gate()
        watched = WatchedStore(self.store, write_fails=True)
        scheduler = self.scheduler(RunnableProducer("nightly", gate=gate),
                                   store=watched, interval=0,
                                   clock=lambda: NOW)

        self.looping(scheduler)
        self.assertTrue(watched.reached(3).wait(5))
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        self.assertEqual(status.last_error,
                         "CannotWrite: the database is read-only")
        self.assertEqual(status.failures, 1)
        self.assertGreaterEqual(status.ticks, 1)
        # The ticks after it succeeded, so the run of failures is over — and
        # the count says so rather than staying up for the life of the loop.
        self.assertEqual(status.consecutive_failures, 0)

    def test_a_loop_that_died_says_so_instead_of_looking_idle(self):
        # A `BaseException` is not this module's to override, so it ends the
        # loop — but it is recorded on the way out, and `running=False` with
        # `closed=False` is the signature of a scheduler that died rather
        # than one that was stopped. Without that pair, the two look
        # identical, which is the whole failure mode this task is about.
        watched = WatchedStore(self.store, read_fails=abandoned)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)

        self.looping(scheduler)
        # Joining the loop's own thread rather than closing it: `close()`
        # would set `closed`, which is the very field under test.
        scheduler._thread.join(5)

        status = scheduler.status()
        self.assertFalse(status.running)
        self.assertFalse(status.closed)
        self.assertEqual(status.failures, 1)
        self.assertEqual(status.last_error,
                         "LoopAbandoned: the interpreter is going down")

    def test_a_clock_that_raises_does_not_lose_the_failure_it_caused(self):
        # The loop stamps a failure with the same injected clock the tick
        # uses, so a broken clock would take out the recording as well as the
        # tick — the failure would vanish exactly when there is most to say.
        clock = BrokenClock()
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=clock)

        self.looping(scheduler)
        self.assertTrue(clock.reached(4).wait(5))
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        self.assertGreaterEqual(status.failures, 2)
        self.assertEqual(status.last_error,
                         "ClockStopped: the clock is not readable")
        stamped = datetime.fromisoformat(status.last_error_at)
        self.assertIsNotNone(stamped.tzinfo)


class AProducerThatNeverStarts(SchedulerCase):
    """A tick reports a producer it could not start and moves on, which is
    right: one producer's permanent refusal must not silence the others. What
    the tick cannot see is that it happened on *every* tick — each one only
    knows about itself. The loop can see it, so the loop counts it.

    It does not act on it. Stopping would be the silent death this task
    exists to prevent, arriving by a decision instead of by accident, and it
    would remove the only thing that would resume the work if the condition
    cleared. Costing one refused `start()` per tick, the count is worth more
    than the stop.
    """

    def test_a_producer_that_cannot_start_is_counted_tick_after_tick(self):
        watched = WatchedStore(self.store)
        scheduler = Scheduler(ClosedRunner([RunnableProducer("nightly")]),
                              watched, interval=0, clock=lambda: NOW)

        self.looping(scheduler)
        self.assertTrue(watched.reached(3).wait(5))
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        self.assertEqual(set(status.failing_to_start), {"nightly"})
        self.assertGreaterEqual(status.failing_to_start["nightly"], 2)
        # The tick reported it; it did not raise. A permanent refusal is not
        # a broken tick, and conflating the two would bury both.
        self.assertEqual(status.failures, 0)
        self.assertIsNone(status.last_error)

    def test_the_answer_cannot_be_edited_by_whoever_asked_for_it(self):
        # `status()` hands out the loop's own counts otherwise, and an
        # interface rendering them could rewrite the record of what failed.
        scheduler = self.scheduler(RunnableProducer("nightly"))
        scheduler.status().failing_to_start["invented"] = 99
        self.assertEqual(scheduler.status().failing_to_start, {})

    def test_the_count_clears_once_the_producer_starts(self):
        # The permissive side: a producer that failed once and then started
        # must come off the list, or the count says "failing on every tick"
        # about something that is running fine.
        opened = threading.Event()
        producer = RunnableProducer("nightly", gate=self.gate())
        self.runner.register(producer)
        watched = WatchedStore(self.store)
        scheduler = Scheduler(
            RefusesUntilAsked([producer], self.runner, opened), watched,
            interval=0, clock=lambda: NOW)

        self.looping(scheduler)
        self.assertTrue(watched.reached(2).wait(5))
        self.assertEqual(set(scheduler.status().failing_to_start), {"nightly"})

        opened.set()
        self.assertTrue(watched.reached(watched.ticks + 2).wait(5))
        self.assertTrue(scheduler.close(5))

        self.assertEqual(scheduler.status().failing_to_start, {})


class FailsAfterTheFirstTick(WatchedStore):
    """The real store for one tick, and a store that cannot be read for every
    tick after it.

    It exists to make "the last tick that FINISHED" a different tick from
    "the last tick", which is the only way to tell a status that keeps the
    last real answer from one that keeps whatever happened most recently.
    """

    def runs(self):
        answer = super().runs()
        if self.ticks > 1:
            raise gone_away()
        return answer


class TheStatusCarriesTheLastTicksAnswers(SchedulerCase):
    """`LoopStatus` counts ticks, and a count cannot answer "why has the
    nightly scan not run?".

    The reasons exist for exactly that question — `due` returns them and a
    `TickResult` carries them — but until they reach `status()` they end with
    the tick that produced them, because nothing else keeps them and nothing
    logs them. A status reporting only what STARTED answers a different
    question from the one anybody asks of it.
    """

    def test_a_scheduler_that_has_never_ticked_has_no_last_result(self):
        scheduler = self.scheduler(RunnableProducer("nightly"))
        self.assertIsNone(scheduler.status().last_result)

    def test_the_reason_a_due_producer_was_left_alone_reaches_status(self):
        # Recorded as having just run, so every tick has a cadence to explain
        # rather than work to do — and so the reason is the cadence one
        # rather than "already running", which would depend on how quickly a
        # job the loop started happened to finish.
        self.store.record_run("nightly", NOW_ISO)
        watched = WatchedStore(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly"), store=watched,
                                   interval=0, clock=lambda: NOW)

        self.looping(scheduler)
        self.assertTrue(watched.reached(2).wait(5))
        self.assertTrue(scheduler.close(5))

        result = scheduler.status().last_result
        # Whole shapes, not sampled keys: a tick with nothing due and a tick
        # with work it could not begin both read as "nothing started", and
        # telling those apart is the whole of what this field is for.
        self.assertEqual(result.due, [])
        self.assertEqual(result.started, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(list(result.skipped), ["nightly"])
        self.assertIn("next due at", result.skipped["nightly"])
        self.assertEqual(result.at, NOW_ISO)
        # Nothing ran, which is what the reason above says: a status that
        # reported a reason while the producer had in fact started would be
        # worse than one that reported nothing.
        self.assertEqual(self.runner.jobs(), [])

    def test_the_answer_it_keeps_is_the_last_tick_that_FINISHED(self):
        # Both halves in one fixture, because they are the same rule: what
        # `status()` holds is the last tick that decided something, so the
        # started side is pinned as well as the reason side, and a tick that
        # RAISED leaves the last real answer standing rather than blanking it
        # at the moment there is most to explain.
        watched = FailsAfterTheFirstTick(self.store)
        scheduler = self.scheduler(RunnableProducer("nightly",
                                                    gate=self.gate()),
                                   store=watched, interval=0,
                                   clock=lambda: NOW)

        self.looping(scheduler)
        self.assertTrue(watched.reached(3).wait(5))
        self.assertTrue(scheduler.close(5))

        status = scheduler.status()
        self.assertGreaterEqual(status.failures, 2)
        result = status.last_result
        self.assertEqual(result.due, ["nightly"])
        self.assertEqual(list(result.started), ["nightly"])
        self.assertEqual(result.skipped, {})
        self.assertEqual(result.failed_to_start, {})
        self.assertEqual(result.at, NOW_ISO)


class TheOneZoneSetting(unittest.TestCase):
    """`check_zone` is what a deployment's single zone setting goes through --
    the setting the three unattended appointments are read in AND the setting
    every timestamp on the page is shown in.

    What these tests are FOR is that it is the same rule an override's own
    `zone` goes through, not a second one beside it. Two validators would let
    the page render in a zone the schedule had refused (or refuse one the
    schedule was happily keeping appointments in), which is the disagreement
    one setting exists to make impossible, arriving from the other side.
    """

    def test_a_known_name_becomes_the_zone_it_names(self):
        self.assertEqual(check_zone(ZONE_NAME, "from a setting"), ZONE)

    def test_a_zone_already_built_comes_back_unchanged(self):
        # How the one setting TRAVELS: resolved once at start-up and handed to
        # the producers, where `resolve` sees it again. Rebuilding it from a
        # name a second time would be a second chance to build a different one.
        #
        # A FIXED-OFFSET tzinfo, not a `ZoneInfo`. `ZoneInfo` is a cache --
        # `ZoneInfo(str(z)) is z` for any zone it built -- so `assertIs` on one
        # of those cannot tell "handed back" from "rebuilt from its own name",
        # and a mutation doing the second survived exactly that assertion. A
        # fixed offset has no name to be rebuilt from, which is what makes the
        # identity observable at all.
        built = timezone(timedelta(hours=1))
        self.assertIs(check_zone(built, "from a setting"), built)
        # And a named zone travels too, which is the case production uses.
        self.assertEqual(check_zone(ZoneInfo(OTHER_ZONE_NAME),
                                    "from a setting"), OTHER_ZONE)

    def test_a_stated_time_can_be_read_in_a_zone_that_has_no_name(self):
        # The consequence of the above, carried through `resolve`: `Entry.zone`
        # is a `tzinfo`, and a fixed-offset one is a `tzinfo`. If this ever
        # stopped working the passthrough branch above would be dead code, and
        # the assertion on it would be pinning nothing.
        offset = timezone(timedelta(hours=1))
        entries = resolve([FakeProducer("nightly", at=time(3, 0),
                                        zone=offset)])
        self.assertIs(entries["nightly"].zone, offset)
        _names, reasons = due(entries,
                              {"nightly": "2026-07-26T12:00:00+00:00"}, NOW)
        self.assertIn("next due at 2026-07-27T02:00:00+00:00",
                      reasons["nightly"])

    def test_a_name_this_system_does_not_know_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not a time zone"):
            check_zone("Nowhere/Atlantis", "from a setting")

    def test_a_setting_present_and_empty_is_refused_rather_than_defaulted(self):
        # `cronicled.config.load_zone` hands an empty setting through for
        # exactly this: an operator who set the variable meant to name a zone,
        # and quietly substituting UTC would report the mistake as success.
        with self.assertRaises(ValueError):
            check_zone("", "from a setting")

    def test_the_refusal_names_where_the_value_was_configured(self):
        # A message naming a producer would be wrong here and actively
        # misleading: the setting is wrong for all three of them, and none of
        # them chose it.
        with self.assertRaisesRegex(ValueError, r"\$CRONICLED_ZONE"):
            check_zone("Nowhere/Atlantis", "$CRONICLED_ZONE")

    def test_it_accepts_and_refuses_exactly_what_an_override_does(self):
        # THE POINT OF THE WHOLE CLASS, and asserted as an agreement rather
        # than as two lists of names: whatever the sample, the setting and the
        # override must answer alike. A second validator would show up here as
        # a name one took and the other did not.
        for name in (ZONE_NAME, OTHER_ZONE_NAME, "UTC", "Nowhere/Atlantis",
                     "", "   ", "Europe/Madrid", "not a zone at all"):
            setting_refused = override_refused = None
            try:
                check_zone(name, "from a setting")
                setting_refused = False
            except ValueError:
                setting_refused = True
            try:
                resolve([FakeProducer("nightly", at="03:00", zone=name)])
                override_refused = False
            except ValueError:
                override_refused = True
            self.assertEqual(setting_refused, override_refused, name)

    def test_a_time_stated_in_the_setting_is_read_in_that_very_zone(self):
        # Not just that the object comes back, but that an appointment made in
        # it lands where the zone says: 03:00 in a zone four hours behind UTC
        # in July is 07:00 UTC. Without this, `check_zone` could hand back any
        # zone at all and every assertion above would hold.
        zone = check_zone(ZONE_NAME, "from a setting")
        entries = resolve([FakeProducer("nightly", at=time(3, 0), zone=zone)])
        names, reasons = due(entries, {"nightly": "2026-07-26T08:00:00+00:00"},
                             NOW)
        self.assertEqual(names, [])
        self.assertEqual(reasons["nightly"],
                         "last ran 2026-07-26T08:00:00+00:00; next due at "
                         "2026-07-27T07:00:00+00:00")


class ZonesAgree(unittest.TestCase):
    """`schedule._zones_agree`: whether two zone specifications are the same
    zone, decided by what they DO rather than by the string that names them.
    """

    def test_the_same_object_agrees_with_itself(self):
        self.assertTrue(schedule._zones_agree(ZONE, ZONE))

    def test_the_same_name_read_twice_agrees(self):
        self.assertTrue(
            schedule._zones_agree(ZoneInfo(ZONE_NAME), ZoneInfo(ZONE_NAME)))

    def test_a_retired_alias_agrees_with_the_name_that_replaced_it(self):
        # Same zone, two spellings -- exactly the case that must NOT become a
        # refusal. "UTC" and "Etc/UTC" are two names for one entry in the
        # IANA database.
        self.assertTrue(
            schedule._zones_agree(ZoneInfo("UTC"), ZoneInfo("Etc/UTC")))

    def test_two_unrelated_zones_disagree(self):
        self.assertFalse(schedule._zones_agree(ZONE, OTHER_ZONE))

    def test_sharing_todays_offset_is_not_agreement(self):
        # New York and Santiago read the same offset in July -- one on
        # daylight saving, the other on standard time, from opposite
        # hemispheres -- and a rule that stopped at "today" would call them
        # the same zone. They part ways every January.
        new_york = ZoneInfo("America/New_York")
        santiago = ZoneInfo("America/Santiago")
        july = datetime(2026, 7, 26, 12, 0, 0)
        self.assertEqual(july.replace(tzinfo=new_york).utcoffset(),
                         july.replace(tzinfo=santiago).utcoffset())
        self.assertFalse(schedule._zones_agree(new_york, santiago))

    def test_a_fixed_offset_agrees_only_with_an_identical_fixed_offset(self):
        one = timezone(timedelta(hours=2))
        other = timezone(timedelta(hours=2))
        self.assertTrue(schedule._zones_agree(one, other))
        self.assertFalse(
            schedule._zones_agree(one, timezone(timedelta(hours=3))))


class AnOverrideMayNotSecondGuessTheDeploymentsZone(unittest.TestCase):
    """`resolve`'s `deployment_zone` parameter closes the hole
    `cronicled.__main__.build_scheduler`'s docstring names: an override's own
    `zone` key is a second place deciding the zone, reachable through
    ordinary configuration -- a schedule override naming a zone per entry,
    with no deployment-wide zone setting of its own, keeps its appointments
    in that zone while the page (reading the deployment's) renders every
    timestamp hours away, with nothing said.
    """

    def test_omitting_deployment_zone_leaves_every_existing_behaviour_alone(self):
        # The default. Every OTHER test in this file calls `resolve` this
        # way, and none of them is exercising this rule.
        entries = resolve(
            [FakeProducer("nightly")],
            {"nightly": {"at": "03:00", "zone": OTHER_ZONE_NAME}})
        self.assertEqual(entries["nightly"].zone, OTHER_ZONE)

    def test_an_override_naming_a_genuinely_different_zone_is_refused(self):
        with self.assertRaisesRegex(ValueError, "different zone"):
            resolve(
                [FakeProducer("nightly")],
                {"nightly": {"at": "03:00", "zone": OTHER_ZONE_NAME}},
                deployment_zone=ZONE)

    def test_the_refusal_names_the_producer_and_both_zones(self):
        with self.assertRaisesRegex(
                ValueError,
                r"nightly.*%s.*%s" % (OTHER_ZONE_NAME, ZONE_NAME)):
            resolve(
                [FakeProducer("nightly")],
                {"nightly": {"at": "03:00", "zone": OTHER_ZONE_NAME}},
                deployment_zone=ZONE)

    def test_an_override_naming_the_deployments_own_zone_still_works(self):
        # Agreement, and it must not become a start-up failure.
        entries = resolve(
            [FakeProducer("nightly")],
            {"nightly": {"at": "03:00", "zone": ZONE_NAME}},
            deployment_zone=ZONE)
        self.assertEqual(entries["nightly"].zone, ZONE)

    def test_an_override_naming_the_same_zone_by_a_different_spelling_still_works(self):
        # THE point of the whole rule: agreement is not string equality.
        entries = resolve(
            [FakeProducer("nightly")],
            {"nightly": {"at": "03:00", "zone": "Etc/UTC"}},
            deployment_zone=ZoneInfo("UTC"))
        self.assertEqual(entries["nightly"].zone, ZoneInfo("Etc/UTC"))

    def test_a_producers_own_declared_zone_is_never_the_disagreeing_side(self):
        # No override at all: the zone travels straight from
        # `deployment_zone` to the producer's own declaration, the way
        # `build_scheduler` wires the three real producers -- so a producer
        # that merely declares what it was handed can never be the side that
        # disagrees.
        entries = resolve(
            [FakeProducer("nightly", at=time(3, 0), zone=ZONE)],
            deployment_zone=ZONE)
        self.assertIs(entries["nightly"].zone, ZONE)

    def test_an_override_changing_only_the_time_must_still_agree_on_zone(self):
        # An override that names 'at' without repeating 'zone' is already
        # refused for a different reason (no zone to read it in) -- this is
        # the case where it DOES repeat 'zone', but repeats the wrong one.
        with self.assertRaisesRegex(ValueError, "different zone"):
            resolve(
                [FakeProducer("nightly", at=time(3, 0), zone=ZONE)],
                {"nightly": {"at": "04:00", "zone": OTHER_ZONE_NAME}},
                deployment_zone=ZONE)


class TheRuleTheStoreAndThePageShareForReadingATimestamp(unittest.TestCase):
    """`as_utc` is public so the page can convert the same stamps the schedule
    compares, by the same rule. A second reader would be free to disagree with
    this one about a row, silently, in either direction.
    """

    def test_an_iso_string_with_an_offset_is_that_instant_in_utc(self):
        self.assertEqual(as_utc("2026-07-26T14:00:00+02:00"),
                         datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc))

    def test_a_datetime_is_taken_as_it_stands(self):
        self.assertEqual(as_utc(NOW), NOW)

    def test_a_stamp_with_no_offset_is_none_rather_than_assumed_to_be_utc(self):
        # The line that matters at the display end too: a naive stamp names no
        # instant, so the page shows it verbatim instead of shifting it by the
        # configured offset and saying nothing.
        self.assertIsNone(as_utc("2026-07-26T12:00:00"))

    def test_something_that_is_not_a_timestamp_at_all_is_none(self):
        self.assertIsNone(as_utc("never"))
        self.assertIsNone(as_utc(None))

    def test_it_is_the_rule_the_tick_itself_records_by(self):
        # Asserted through the scheduler rather than beside it: if `as_utc`
        # stopped being what `_moment` does, this is where the page and the
        # store would start disagreeing about a row.
        self.assertEqual(as_utc(NOW).isoformat(), NOW_ISO)


if __name__ == "__main__":
    unittest.main()
