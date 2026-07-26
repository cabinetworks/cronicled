"""What is due, and why everything else is not.

Every rule here is arithmetic over data — no thread, no sleep, no wall clock.
`now` is a parameter, which is the whole reason the tick is separated from the
loop that drives it: a clock that jumped back a year, a service that was off for
a week, and the exact second a cadence elapses are all ordinary unit tests here
and would each need a fixture with a fake clock inside a running loop otherwise.
"""
import unittest
from datetime import datetime, timedelta, timezone

from cronicled.schedule import Entry, due, resolve

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


if __name__ == "__main__":
    unittest.main()
