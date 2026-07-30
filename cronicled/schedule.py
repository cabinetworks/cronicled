"""Which producers are due, why the others are not, and starting the ones that
are.

The rules are arithmetic over data: no thread, no sleep, no wall clock. `now`
and the last-run times are arguments, so every scheduling rule — the exact
second a cadence elapses, a machine whose clock jumped, a service that was off
for a week — is an ordinary unit test rather than something that needs a fake
clock inside a running loop. `Scheduler.tick` keeps that property: it is called
directly, once, and returns what it did.

Wall-clock scheduling arrives the same way. A producer may be scheduled at a
stated time of day in a named zone instead of on an interval, and the two cases
that break a naive implementation of that — a machine that was off across the
appointed hour, and the two days a year a wall clock names an hour twice or not
at all — are still just `now` and a last-run time. See `_occurrence` for the
daylight-saving rules and `due` for what a missed appointment does; neither
needs a zone's transitions to be reachable from a running loop to be tested,
because a transition is a value of `now` like any other.

The loop that calls it on a timer lives here too, at the bottom, and it holds
none of the rules — `start`, `close` and `status` know about a thread, an
event and a count, and nothing about cadences. That separation is the reason
every test above this line can be a plain function call, and the reason the
loop's own tests are only about surviving and being seen to.

Two answers come back from `due`, not one: what to run, and a reason for each
producer that is being left alone. An operator asking "why has the nightly
scrape not run?" is asking about the second answer, and a scheduler that only
returns the first can only be debugged by re-deriving its arithmetic by hand.
`TickResult` carries the same shape one layer up, and `LoopStatus` one layer
above that.
"""
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .jobs import JobRejected

# What a schedule override may say. Anything else is a typo — see `resolve`.
OVERRIDE_KEYS = ("every", "at", "zone", "enabled")

# The keys that say WHEN, as opposed to whether. An override naming any of
# them supplies the whole of that producer's timing and the producer's own
# declaration is set aside — see `resolve` for why that is one rule rather
# than three keys merging field by field.
SCHEDULE_KEYS = ("every", "at", "zone")

# How far either side of `now`'s own local date the occurrences of a stated
# time are looked for. An occurrence does not have to fall on the local date it
# is named for, in EITHER direction, so this window is measured rather than
# reasoned about:
#
# - Forward, because `_occurrence` pushes a time inside a spring-forward gap
#   past the end of that gap, and where the gap is the last hour of the local
#   day it lands on the next date. America/Nuuk does that every spring since
#   2024 (its clock goes 22:59 -> 00:00), so a producer stated at 23:59 has its
#   29 March 2025 appointment at 00:59 on the 30th, and on the 30th the latest
#   appointment that has PASSED is the 28th's — two dates back.
# - Backward, because a zone that puts its clocks back just after midnight
#   returns the local date to yesterday while the following date's appointment
#   is already in the past. America/St_Johns did that until 2011, moving its
#   clocks at 00:01: at 23:30 on 6 November 2010, midnight of the 7th has
#   happened and the next midnight is TWO dates ahead of the date on the clock.
#
# Enumerated over every zone `zoneinfo` knows, every offset change between 1970
# and 2040, every hour within two days of each, at four times of day —
# 12,081,544 cases. The latest appointment that had passed lay two dates back
# in 262 of them and one date FORWARD in 53; the earliest still to come lay one
# date back in 262 and two dates forward in 53. Never further either way, so
# both numbers below are two, and both are load-bearing.
#
# Bounded rather than an unbounded walk, and a `ValueError` rather than a silent
# `None`: a zone this cannot bracket is a fault worth a stack trace, and a
# producer that quietly never fires is the outcome worth avoiding at any price.
_DAYS_BACK = 2
_DAYS_FORWARD = 2

# How long the loop waits between ticks when nobody says otherwise. This is
# the resolution of the whole schedule, not a cadence: a producer due at
# 03:00:00 on a minute-resolution loop starts by 03:01, and the cost of the
# resolution is one `runs()` query and one `jobs()` snapshot per minute.
DEFAULT_INTERVAL = 60.0


@dataclass(frozen=True)
class Entry:
    """One producer's resolved schedule: when, and whether at all.

    Two shapes can say when, exactly one of them per producer, and both exist
    because each is right for something:

    - `every`, a number of seconds since the last recorded run. The right
      answer for something that should run every few minutes.
    - `at`, a `datetime.time` of day, read in `zone` (a `tzinfo`). The right
      answer for something nightly, because an interval measured from the last
      run drifts: a daily pass restarted at 2pm runs at 2pm from then on, and
      nothing says why.

    `every` and `at` are never both set, and an enabled entry always has one
    of them. `at` is never set without `zone`. All three are refused by
    `resolve`, at wiring time, and refused again by `due` — a hand-built entry
    must not become a producer that silently never runs, or one whose stated
    hour is read in whatever zone the host happens to be in.
    """

    producer: str
    every: Optional[float]
    enabled: bool = True
    at: Optional[time] = None
    zone: Optional[tzinfo] = None


def _check_every(value, producer, source):
    """A cadence must be a positive number of seconds, from either source.

    `bool` is excluded explicitly because `True` is an `int` in Python: an
    override reading `{"every": true}` in a JSON config would otherwise
    resolve to a one-second cadence on a full-library scrape.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"cadence for producer {producer!r} ({source}) must be a number of "
            f"seconds, got {value!r}"
        )
    if value <= 0:
        raise ValueError(
            f"cadence for producer {producer!r} ({source}) must be greater "
            f"than zero seconds, got {value!r}"
        )
    return value


def _check_at(value, producer, source):
    """A stated time of day: `"HH:MM"`, `"HH:MM:SS"`, or a `datetime.time`.

    A UTC offset on it is refused rather than honoured. `"03:00+01:00"` would
    be a second way of saying the zone, disagreeing silently with `zone`
    whenever the two differ — and worse than that, it pins an offset, which is
    the one thing a zone with daylight saving does not have. An operator who
    wrote it meant a local hour, and a local hour is `zone`'s job.
    """
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"stated time for producer {producer!r} ({source}) must be a "
                f'time of day such as "03:00", got {value!r}'
            ) from None
    else:
        raise ValueError(
            f"stated time for producer {producer!r} ({source}) must be a time "
            f'of day such as "03:00", got {value!r}'
        )
    if parsed.tzinfo is not None:
        raise ValueError(
            f"stated time for producer {producer!r} ({source}) must not carry "
            f"a UTC offset, got {value!r}: name the zone in 'zone' instead, so "
            "that daylight saving moves the offset rather than an offset "
            "outliving it"
        )
    return parsed


def _check_zone(value, where):
    """The zone a stated time is read in, as a `tzinfo`.

    A name `zoneinfo` knows (`"Europe/Lisbon"`), or a `tzinfo` already built.
    A name it does not know is refused here, at wiring time: the alternative is
    a `ZoneInfoNotFoundError` raised from inside a tick, once an interval, for
    as long as nobody looks — and a tick that raises starts nothing at all, for
    any producer.

    `where` names the place the value came from, and is the whole of the
    difference between this being called for one producer's override and being
    called for the deployment's single zone setting (see `check_zone`). It is a
    phrase rather than a producer name because the second caller has no
    producer to name: a setting that is wrong is wrong for all three of them.
    """
    if isinstance(value, tzinfo):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"zone {where} must be the name of a "
            f'time zone, such as "Europe/Lisbon", got {value!r}'
        )
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"zone {where} is not a time zone "
            f'this system knows: {value!r}. Name one from the IANA database, '
            'such as "Europe/Lisbon" or "UTC"'
        ) from None


def check_zone(value, source):
    """The ONE zone this deployment reads a stated time in — and, because it
    is the same setting, the zone the page shows every stored timestamp in.

    `source` names where the value was configured, for the refusal's message.

    Public, and a wrapper over the rule `resolve` already applies to an
    override's `zone`, because there must not be a second way to validate a
    zone name: the page and the schedule reading one setting is only worth
    anything if the two agree about which names that setting may hold. A page
    that rendered in a zone the schedule had refused — or refused a zone the
    schedule was happily keeping appointments in — would be the same
    disagreement this setting exists to remove, arriving from the other side.

    Called at start-up, once, so an unknown name is a stack trace an operator
    reads before the service is listening, rather than something discovered at
    3am by a tick that raises and starts nothing for anybody.
    """
    return _check_zone(value, f"configured for this deployment ({source})")


def _check_interval(value):
    """The loop's wait between ticks: a number of seconds, never negative.

    `bool` is excluded for the reason `_check_every` excludes it — `True` is
    an `int`, so a config typo would otherwise become a one-second loop.

    **Zero is allowed**, and means the loop starts the next tick the instant
    the last one ends. That is a hot loop and not a setting for a running
    service; it exists because it is how a test drives the loop deterministically
    without sleeping, which is worth more than refusing a value nobody would
    configure by accident. A negative is a typo and is refused.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"the loop's interval must be a number of seconds, got {value!r}"
        )
    if value < 0:
        raise ValueError(
            f"the loop's interval must not be negative, got {value!r}"
        )
    return value


def resolve(producers, overrides=None):
    """Work out each producer's schedule, as `{name: Entry}`.

    A producer declares its own cadence as `every` (seconds). An override,
    keyed by producer name, may replace that cadence, disable the producer, or
    both: `{"nightly-scan": {"every": 3600}}`, `{"box-scrape": {"enabled":
    false}}`.

    An override may instead give a **stated time of day** and the zone to read
    it in: `{"nightly-scan": {"at": "03:00", "zone": "Europe/Lisbon"}}`. Both
    forms exist because both are right for something: an interval for a pass
    that should run every few minutes, a stated time for a nightly one, which
    on an interval drifts to whichever hour the process last restarted at.

    **An override naming both `every` and `at` is refused.** It is a
    contradiction, not a preference to resolve by precedence, and it belongs
    here with the other wiring mistakes rather than at 3am.

    An override naming ANY of `every`/`at`/`zone` supplies that producer's
    whole timing, and what the producer declared is set aside — so `{"at":
    "03:00", "zone": "UTC"}` moves a producer off its declared `every` rather
    than colliding with it. The alternative, merging key by key, would mean an
    override could only ever ADD a stated time to a declared interval, which is
    the contradiction above; and it would leave `{"at": ...}` inheriting a zone
    from somewhere the operator was not looking.

    Everything this refuses, it refuses here — at the point the schedule is
    wired up, rather than hours later when a tick tries to use it. The same
    reasoning as `JobRunner.register` rejecting an unknown cost class and a
    duplicate name: a wiring mistake that surfaces at startup is a stack trace
    an operator reads, while one that surfaces at 3am is a producer that
    quietly did not run.

    So each of these is a `ValueError`:

    - **A producer with neither a cadence nor a stated time, and no override
      supplying one**, which would otherwise be a producer nobody scheduled. A
      missing `every` attribute and an `every` of `None` are the same mistake,
      not two. Note that an override saying only `{"enabled": true}` does NOT
      satisfy this: naming a producer is not scheduling it, and letting a
      mention silence the error would hide the mistake behind config that looks
      deliberate. A producer explicitly *disabled* is exempt — that is a
      scheduling decision, which is the opposite of an omission.
    - **An override naming a producer that does not exist**, which is a typo
      in a name; ignoring it would leave the real producer running on the
      cadence the operator believed they had changed.
    - **An override that is not a mapping, or carries a key other than
      `every`/`at`/`zone`/`enabled`** — again, typos that would otherwise be
      dropped.
    - **A cadence that is not a positive number**, from either source.
    - **Both a cadence and a stated time**, as above.
    - **A stated time that is not a time of day, or that carries a UTC
      offset** — see `_check_at`.
    - **A stated time with no zone.** THIS IS THE ANSWER TO "what happens when
      the zone is not configured": nothing happens, because the schedule does
      not load. There is no default and deliberately not a fallback to the
      host's zone — a container runs in UTC while its operator thinks in their
      own hour, so inheriting the host would mean an appointment kept several
      hours from the one that was asked for, correct-looking in every log, and
      moving whenever the deployment moves. Nor is UTC assumed: an operator who
      wanted UTC can write `"zone": "UTC"` in four characters, and inferring it
      would spend a real, common mistake (forgetting the zone) to save that.
      Refusing costs one startup failure with a message an operator can act on,
      which is the cheapest of the three and the only visible one.
    - **A zone with no stated time.** It schedules nothing and changes nothing,
      so an operator who wrote it believes something untrue about when their
      producer runs. That belief is the fault; the key being harmless is what
      makes it worth saying out loud.
    - **An `enabled` that is not a boolean.** The string `"false"` is true.
    - **Two producers claiming one name**, which would silently drop one
      schedule.
    """
    overrides = {} if overrides is None else dict(overrides)

    declared = {}
    for producer in producers:
        name = producer.name
        if name in declared:
            raise ValueError(
                f"two producers are both named {name!r}; a schedule cannot "
                "tell them apart"
            )
        declared[name] = {key: getattr(producer, key, None)
                          for key in SCHEDULE_KEYS}

    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise ValueError(
            f"schedule override names unknown producer(s) {unknown} "
            f"(known: {sorted(declared)})"
        )

    entries = {}
    for name, declaration in declared.items():
        override = overrides.get(name, {})
        if not isinstance(override, dict):
            raise ValueError(
                f"schedule override for producer {name!r} must be a mapping of "
                f"{list(OVERRIDE_KEYS)}, got {override!r}"
            )
        strange = sorted(set(override) - set(OVERRIDE_KEYS))
        if strange:
            raise ValueError(
                f"schedule override for producer {name!r} has unknown key(s) "
                f"{strange} (known: {list(OVERRIDE_KEYS)})"
            )

        enabled = override.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(
                f"'enabled' for producer {name!r} must be true or false, got "
                f"{enabled!r}"
            )

        # Either the override says when, or the producer does. A key PRESENT
        # in the override with a value of `None` still counts as the override
        # saying when — and is then refused by the check below, which is the
        # difference between an operator writing `{"every": null}` (a value
        # that is not a number of seconds) and a producer that declares no
        # cadence at all (an omission). The declaration side drops its `None`s
        # for exactly that reason.
        if any(key in override for key in SCHEDULE_KEYS):
            timing = {key: override[key] for key in SCHEDULE_KEYS
                      if key in override}
            source = "from the override"
        else:
            timing = {key: value for key, value in declaration.items()
                      if value is not None}
            source = "declared by the producer"

        if "every" in timing and "at" in timing:
            raise ValueError(
                f"the schedule for producer {name!r} ({source}) names both a "
                f"cadence ('every': {timing['every']!r}) and a stated time "
                f"('at': {timing['at']!r}). Those are two different schedules "
                "and there is no sensible way to keep both: an interval suits "
                "something that runs every few minutes, a stated time suits "
                "something nightly. Name one."
            )

        every = at = zone = None
        if "every" in timing:
            every = _check_every(timing["every"], name, source)
        if "at" in timing:
            at = _check_at(timing["at"], name, source)
            if "zone" not in timing:
                raise ValueError(
                    f"the schedule for producer {name!r} ({source}) states a "
                    f"time ({timing['at']!r}) but no 'zone' to read it in. "
                    "There is no default: the host's zone is whatever the "
                    "deployment happens to be in — UTC in a container — so "
                    "inheriting it would keep an appointment hours from the "
                    'one asked for and say nothing. Name a zone, such as '
                    '{"at": "03:00", "zone": "Europe/Lisbon"}, or "UTC" if '
                    "that is genuinely what was meant."
                )
            zone = _check_zone(timing["zone"],
                               f"for producer {name!r} ({source})")
        elif "zone" in timing:
            raise ValueError(
                f"the schedule for producer {name!r} ({source}) names a zone "
                f"({timing['zone']!r}) but no time of day to read in it, so it "
                "changes nothing about when that producer runs. Add 'at', or "
                "drop the zone."
            )

        if every is None and at is None and enabled:
            raise ValueError(
                f"producer {name!r} has no schedule: it declares neither an "
                "'every' nor an 'at', and no schedule override supplies one, "
                "so nothing would ever run it. Give it a cadence in seconds, "
                'or a stated time and zone ({"at": "03:00", "zone": '
                '"Europe/Lisbon"}), or disable it explicitly with '
                '{"enabled": false}.'
            )

        entries[name] = Entry(producer=name, every=every, enabled=enabled,
                              at=at, zone=zone)
    return entries


def _moment(value):
    """A timestamp as an aware UTC `datetime`, or `None` if it is not one.

    Accepts a `datetime` or an ISO-8601 string, which is the shape the store
    writes. A naive value (no offset) comes back as `None` rather than being
    assumed to be UTC: guessing would shift a comparison by the machine's
    offset without saying so, and comparing a naive value with an aware one
    raises `TypeError` in Python, which would take out a whole tick.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def as_utc(value):
    """A timestamp as an aware UTC `datetime`, or `None` when it is not one.

    Public for the page, which converts the same stored stamps for display and
    must read them by exactly the rule the schedule compares them by. A second
    parser would be a second place for the two to disagree, and the
    disagreement would be silent: both would hand back a plausible-looking
    timestamp for the same row.

    Note what this being the shared rule guarantees at the display end: a
    stamp with no offset comes back `None`, so the page shows it exactly as
    stored rather than relabelling it as an instant it may not be. Guessing
    would move a timestamp by the configured offset and say nothing.
    """
    return _moment(value)


def _utcnow():
    """The default clock: the current time, aware and in UTC.

    Aware because `due` refuses a naive `now` outright — every timestamp the
    store holds is UTC, and assuming a naive one is too would shift every
    producer's due-ness by the machine's offset without saying so.
    """
    return datetime.now(timezone.utc)


def _occurrence(day, at, zone):
    """The UTC instant that the wall-clock time `at` names on `day` in `zone`.

    The two days a year on which a wall clock is not a one-to-one map are
    decided here, and both are decided by `fold=0`. That is Python's default,
    which is exactly why it is written down rather than left to be inherited: a
    library default is not a rule, and "whichever way the date library happens
    to go" is how a nightly job comes to run twice.

    **The hour that happens twice** — a clock going back. `fold=0` is the
    FIRST of the two instants, so a job stated for 01:30 runs at 01:30 summer
    time and NOT again an hour later at 01:30 winter time. It fires once
    because `due` compares instants, and by the time the wall clock reads 01:30
    for the second time the run already recorded is not older than this
    occurrence. Choosing the first of the two rather than the second is what
    makes "it ran at the time you asked for" true of the earlier reading; the
    later one is an hour late for no reason.

    **The hour that does not exist** — a clock going forward. `fold=0` reads
    the stated time with the offset in force BEFORE the transition, which
    places the instant AFTER the gap: where 02:00 to 03:00 does not exist, a
    job stated for 02:30 runs at 03:30 local, once. After the gap, not before
    it, and that is the choice rather than an accident. Before would mean a job
    stated for 02:30 running at 01:30 — earlier than the operator asked for,
    inside an hour they may have kept for something else, and the direction a
    person notices least until it collides with whatever else runs overnight.
    After is late by the length of the gap, one night a year, and never earlier
    than the time it was told.
    """
    return datetime.combine(day, at).replace(tzinfo=zone).astimezone(
        timezone.utc)


def _occurrences(at, zone, moment):
    """Every instant `at` could name near `moment`, earliest first.

    The local dates from `_DAYS_BACK` before `moment`'s own to `_DAYS_FORWARD`
    after it. All of them, rather than a walk that stops at the first date
    whose occurrence has passed: an occurrence pushed out of a spring-forward
    gap can land on the following date, so "the first date going backwards
    whose occurrence has passed" is not the same as "the latest occurrence that
    has passed", and it is the second that a schedule needs. Measured, not
    reasoned about — see `_DAYS_BACK`.

    Kept as a list of instants because `moment` and the last-run time are
    arguments: no clock is read here, so every case a wall clock can produce is
    a value a test can pass in.
    """
    first = moment.astimezone(zone).date() - timedelta(days=_DAYS_BACK)
    return [_occurrence(first + timedelta(days=step), at, zone)
            for step in range(_DAYS_BACK + _DAYS_FORWARD + 1)]


def _previous_occurrence(at, zone, moment):
    """The latest instant `at` named in `zone` that is not after `moment`.

    The instant a stated time is measured against: a producer is due when its
    last run is older than this. "The machine was off for a week" is a `moment`
    a week later and nothing else.
    """
    passed = [found for found in _occurrences(at, zone, moment)
              if found <= moment]
    if not passed:
        raise ValueError(
            f"no occurrence of {at.isoformat()} in {zone} has passed by "
            f"{moment.isoformat()}, which should not be possible: the zone's "
            "offsets do not behave like a zone's"
        )
    return max(passed)


def _next_occurrence(at, zone, moment):
    """The earliest instant `at` names in `zone` that is after `moment`.

    Only ever for the reason string on a producer that is not due — a "next due
    at" an operator can read against the clock, which is the difference between
    a schedule that can be debugged and one that has to be re-derived by hand.
    The earliest, not the first found: a gap can leave the occurrence of an
    earlier date still ahead of `moment`, and naming the one after it would
    tell an operator to expect the run a day later than it will happen.
    """
    upcoming = [found for found in _occurrences(at, zone, moment)
                if found > moment]
    if not upcoming:
        raise ValueError(
            f"no occurrence of {at.isoformat()} in {zone} follows "
            f"{moment.isoformat()}, which should not be possible: the zone's "
            "offsets do not behave like a zone's"
        )
    return min(upcoming)


def due(entries, runs, now):
    """What to run now, as `(due_names, reasons)`.

    `entries` is `{name: Entry}` from `resolve`, `runs` is `{name: timestamp}`
    from `Store.runs()` (a producer that has never run is simply absent), and
    `now` is an aware `datetime` or an ISO-8601 string with an offset.

    Every entry lands in exactly one of the two: either its name is in
    `due_names` (sorted, so the answer does not depend on dict order), or its
    name maps to a plain-language reason it is being left alone. A run
    recorded for a producer no longer in `entries` — a leftover row for
    something that was unwired — appears in neither.

    A producer on a cadence is due when it has never run, or when at least
    `every` seconds have passed since it last ran. **Exactly `every` seconds is
    due**: the other choice delays every producer by a whole tick, every cycle,
    forever, and nothing about that is visible.

    A producer at a **stated time** is due when it has never run, or when the
    most recent occurrence of its time in its zone is later than its last
    recorded run. So it fires ONCE per occurrence, not on every tick of the
    hour it names: the run recorded at 03:00:07 is not older than that day's
    03:00, so the ticks at 03:01 and 03:02 leave it alone, and tomorrow's 03:00
    is a later instant again. `_occurrence` holds what "the occurrence of a
    time" means on the two days a wall clock does not cooperate.

    **A missed appointment is owed, not skipped.** A machine that was off
    across 03:00 runs the moment it is back, at whatever hour that is. THIS IS
    A CHOICE, and the other one is defensible: skipping means a laptop opened
    at noon is not immediately made to scan its library. It loses on the
    failure it can produce. A machine that is off, asleep or in a tunnel every
    night at 03:00 — a laptop, which is most of them — would never scan at all
    under skipping, and would report nothing wrong while never doing the one
    thing it was configured for. Owing costs a scan at an inconvenient hour,
    which is visible on the very first occurrence and fixable by moving the
    time or disabling the producer; skipping costs silence that looks like
    health, and looks like it indefinitely. Where the two disagree, this module
    already has a direction: prefer the visible, recoverable failure. If the
    other answer is ever wanted, it becomes a per-producer setting; it is not
    one now, because a setting would be a way of not choosing.

    Nothing here makes up missed runs, and owing is not queueing. A producer
    whose last run was a week ago — nightly cadence or stated time — is due
    **once**, not seven times: this returns a set of names to run now, and the
    next run is counted from when that run is actually recorded, not from the
    schedule that was missed. A service that was off for a week must not wake up
    and fire seven scrapes at a media server, which is precisely the load the
    cost classes exist to prevent.

    **A last run in the future makes a producer due immediately.** The store
    records what it is given and deliberately does not keep the maximum, so a
    timestamp written by a machine whose clock was fast — a container with a
    bad clock, a laptop resumed from suspend — arrives here untouched. Plain
    arithmetic would make such a producer not-due for as long as the skew
    lasts; for a clock a year fast, that is a year of total silence with
    nothing logged and nothing to see. Running it is the recoverable direction
    and it is self-healing: the run is recorded against the current clock, so
    the bad stamp is overwritten and the producer settles back onto its
    cadence. The cost of being wrong is one extra run; the cost of waiting is
    a producer that never runs again and never says so.

    A last run that cannot be read at all — not a timestamp, or a timestamp
    with no timezone — is treated the same way, and for the same reason: the
    tick cannot tell how long ago it was, refusing to run it silences that one
    producer indefinitely, and running it rewrites the unreadable value. It is
    deliberately *not* raised: one bad row must not take out the tick for
    every other producer.
    """
    moment = _moment(now)
    if moment is None:
        raise ValueError(
            "`now` must be a timezone-aware datetime or an ISO-8601 timestamp "
            f"with an offset, got {now!r}"
        )

    due_names = []
    reasons = {}
    for name in sorted(entries):
        entry = entries[name]
        if not entry.enabled:
            reasons[name] = "disabled by override"
            continue
        # `resolve` cannot produce any of the three below. They are refused
        # again anyway, because the alternative for each is a producer that
        # silently never runs or one that runs at the wrong hour, and a
        # hand-built entry reaches here without passing `resolve` at all.
        if entry.every is not None and entry.at is not None:
            raise ValueError(
                f"schedule entry for producer {name!r} has both a cadence and "
                "a stated time, so there is no telling which was meant"
            )
        if entry.every is None and entry.at is None:
            raise ValueError(
                f"schedule entry for producer {name!r} is enabled but has "
                "neither a cadence nor a stated time, so there is nothing to "
                "compare against"
            )
        if entry.at is not None and entry.zone is None:
            raise ValueError(
                f"schedule entry for producer {name!r} states a time but no "
                "zone to read it in; the host's zone is not a default, because "
                "it is a property of the deployment rather than of the schedule"
            )

        recorded = runs.get(name)
        if recorded is None:
            due_names.append(name)
            continue
        last = _moment(recorded)
        if last is None or last > moment:
            due_names.append(name)
            continue

        if entry.at is not None:
            scheduled = _previous_occurrence(entry.at, entry.zone, moment)
            if last < scheduled:
                due_names.append(name)
            else:
                upcoming = _next_occurrence(entry.at, entry.zone, moment)
                reasons[name] = (
                    f"last ran {recorded}; next due at {upcoming.isoformat()}"
                )
            continue

        next_due = last + timedelta(seconds=entry.every)
        if moment >= next_due:
            due_names.append(name)
        else:
            reasons[name] = (
                f"last ran {recorded}; next due at {next_due.isoformat()}"
            )
    return due_names, reasons


@dataclass(frozen=True)
class TickResult:
    """What one tick did, and why it did not do the rest.

    `at` is the moment the tick decided against, as an ISO-8601 UTC string —
    the same value stamped on every run it recorded, so a "next due at" in a
    later tick's reason can be read against it directly.

    `due` is what the schedule said was owing *before* the runner was asked
    for anything. It is the field that tells the two silent ticks apart: a
    tick with `due == []` had no work, and a tick with a non-empty `due` and
    an empty `started` had work it could not begin. Without it both read as
    "nothing happened", and the second is the one an operator needs to see.

    Every scheduled producer lands in exactly one of `started` (name → the
    id of the job now running it), `skipped` (name → why it was left alone
    this time) and `failed_to_start` (name → why asking the runner did not
    work). The split between the last two is not cosmetic: a skip is a
    "not now" that the next tick may resolve on its own — not due yet,
    disabled, still running, cost class saturated — while a failure to start
    is the runner refusing in a way that repeating the request will not fix.
    A caller that treats the second as the first spins.
    """

    at: str
    due: list
    started: dict
    skipped: dict
    failed_to_start: dict


@dataclass(frozen=True)
class LoopStatus:
    """What the background loop has been doing, for somebody who was not
    watching.

    This exists because of the one failure mode a scheduler must not have. An
    exception on a thread fails nothing visible — it prints, and the thread
    dies — so a loop that died three days ago is indistinguishable from a
    scheduler with nothing to do, and the only symptom is an inbox that
    stopped filling. Every field here is a way of telling those two apart:

    - `running` and `closed` together say *why* it is not ticking. Not running
      and closed is a clean shutdown; not running and **not** closed is a loop
      that died, and that combination is the whole point of keeping both.
    - `ticks` and `failures` count the ticks that finished and the ticks that
      raised. `consecutive_failures` is the one that says whether it is
      failing *now* rather than having failed once in the small hours.
    - `last_error`, `last_error_at` and `last_traceback` are the only record
      of what went wrong that will ever exist — nothing re-raises and nothing
      logs — so, as in the runner, the error names its type as well as its
      message and the frames are kept.
    - `failing_to_start` is a producer name to the number of consecutive ticks
      in which the runner refused to start it. A tick can only see its own
      refusal; it takes the loop to notice the same one on every tick since
      the process began.
    - `last_result` is the whole `TickResult` of the last tick that FINISHED,
      or `None` before there has been one. The counts above say whether the
      loop is alive; this says what it decided, and it is the only place the
      reasons survive — `due` returns them, a tick carries them, and without
      this they end with the tick that produced them. An operator asking "why
      has the nightly scan not run?" is asking for `last_result.skipped`, and
      a status that could only answer "the loop is fine, 400 ticks" would be
      telling them nothing they asked about. A tick that RAISED leaves this
      holding the last good one: a tick that did not finish decided nothing,
      and blanking the last real answer would lose the record at the moment
      there is most to explain.
    """

    running: bool
    closed: bool
    ticks: int
    failures: int
    consecutive_failures: int
    last_tick_at: Optional[str]
    last_error: Optional[str]
    last_error_at: Optional[str]
    last_traceback: Optional[str]
    failing_to_start: dict = field(default_factory=dict)
    last_result: Optional[TickResult] = None


class Scheduler:
    """Starts the producers that are due, and reports why the others were not.

    It schedules *producers*, and a producer only ever proposes: nothing this
    starts applies anything to a library. Every safety rule in the project —
    the mute, the ambiguity refusal, the confidence threshold, the read-only
    scan — still stands between a proposal and a change, and a person still
    reviews. This is the first thing here that acts with nobody present, and
    that is the reason to say so plainly rather than leave it inferred.

    What it can and cannot bound
    ----------------------------
    There is no cancellation, deliberately: interrupting a producer needs
    cooperative checks inside every producer. So a run that has begun cannot
    be stopped, and the honest guarantee is about *starts* — a producer still
    running from an earlier tick is not started on top of itself, and a
    saturated cost class refuses as it always did. In other words **a slow
    producer delays its own next run and nothing else's**. Not "no run
    exceeds N minutes"; a scheduler that looks like it enforces a time limit
    and does not would be worse than one that never claimed to.

    The clock
    ---------
    `clock` is called with no arguments and returns the current time; it
    defaults to the real UTC clock. `tick(now)` overrides it outright. Both
    exist so that no test ever has to sleep to advance time — a scheduling
    rule tested against a real clock either sleeps or tests nothing.

    Wiring
    ------
    The schedule is resolved once, in the constructor, from the producers the
    runner has registered. Everything `resolve` refuses is refused there — a
    producer nothing schedules, an override naming both a cadence and a stated
    time, a stated time with no zone or a zone this system does not know — at
    start-up, where an operator reads it as a stack trace, rather than at 3am as
    a producer that quietly never ran or ran in the wrong hour.

    The consequence is that **a producer registered after the scheduler was
    built is not in the schedule**, so `start()` refuses to run a loop while
    one exists rather than leaving it unscheduled and unmentioned. The loop
    deliberately does not re-resolve instead: re-resolving would move
    `resolve`'s wiring-mistake `ValueError` out of start-up and onto the loop
    thread, where it is either a dead loop or a failure recorded once a
    minute, and it would let the schedule change under a running loop with
    nothing to say that it had. Registering a producer *after* `start()` is
    the residual, and it is a wiring rule rather than a check: register
    everything, then build the scheduler, then start it.

    The loop
    --------
    `start()` ticks in the background until `close()`. Everything the loop
    knows is in `LoopStatus`, and the reason it keeps any of it is that an
    exception on a thread fails nothing visible. See `_loop`.
    """

    def __init__(self, runner, store, *, overrides=None, clock=None,
                 interval=DEFAULT_INTERVAL):
        self._runner = runner
        self._store = store
        self._clock = _utcnow if clock is None else clock
        self._entries = resolve(runner.producers(), overrides)
        self._interval = _check_interval(interval)

        # Held for the whole of a tick, so two ticks cannot run at once. See
        # `tick` for why that matters and why one loop thread is not an
        # answer to it.
        self._tick_lock = threading.Lock()
        # Guards the loop's own bookkeeping below: the loop thread writes it
        # and `status()` reads it from whatever thread asked.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._ticks = 0
        self._failures = 0
        self._consecutive_failures = 0
        self._last_tick_at = None
        self._last_error = None
        self._last_error_at = None
        self._last_traceback = None
        self._failing_to_start = {}
        self._last_result = None

    def tick(self, now=None):
        """Start everything due, once, and return a `TickResult`.

        No producer is started twice, nothing is queued, and nothing is made
        up for a missed cadence — `due` returns a set of names to run now, and
        the next run is counted from the stamp this writes.

        **The run is recorded whether the producer goes on to succeed or
        fail**, and it is recorded *after* the runner has accepted it, which
        are two separate rules pulling against the same mistake from opposite
        sides.

        Recording only on success would mean a failure delays the next attempt
        differently from a success. There is no back-off here on purpose —
        done properly it needs the transient-versus-permanent classification
        that exists and has never been wired to anything, and done badly it
        means a producer that fails once falls silent for a day. It would also
        break the healing that `due` depends on: an unreadable or future-dated
        last run makes a producer due immediately, and that is only survivable
        because the run is then re-stamped against the current clock. Make the
        stamp conditional and a bad row becomes permanent, and that producer
        starts on every tick for ever.

        Recording before the start would mark a producer as having run when
        the runner refused it — the same corruption arriving from the other
        side, and worse, because a refusal is the common case (a saturated
        cost class) rather than an exceptional one.

        Nothing a single producer does takes out the tick. A `JobRejected` is
        a skip, because the class frees itself and the next tick may well
        succeed; **any other exception from `start()` is a failure to start**,
        recorded and moved past. That rule is deliberately about the absence
        of a type rather than the presence of one: it is what makes a closed
        runner report itself instead of being retried for ever, and it holds
        for a producer whose `produce` is not a generator, or which is no
        longer registered, without this module needing to know about either.

        The catch stops at `Exception`. This runs on its caller's thread, so a
        `KeyboardInterrupt` has somewhere to go and should go there — unlike
        the runner's worker, which catches `BaseException` precisely because
        nothing is above it to notice.

        **One tick at a time.** The whole body is held under a lock, so a
        caller ticking by hand while the loop is running waits for the loop's
        tick instead of interleaving with it. That there is a single loop
        thread today is not a reason to leave it out: `tick` is public, an
        interface calling it by hand is the obvious next use, and the rule it
        protects is the one thing a tick promises. `jobs()` is a snapshot, so
        two ticks reading it at the same moment would both see a producer
        idle and both start it — a doubled scrape against the media server,
        arriving through the door the cost classes cannot watch, because both
        starts are legitimate as far as the runner can tell. The cost of the
        lock is that a hand tick waits out a loop tick, which is a bounded
        wait for a pair of store reads.
        """
        with self._tick_lock:
            return self._tick(now)

    def _tick(self, now):
        """The body of `tick`, always called with `_tick_lock` held."""
        if now is None:
            now = self._clock()
        # `due` validates `now`, so `_moment` below cannot return None.
        due_names, reasons = due(self._entries, self._store.runs(), now)
        at = _moment(now).isoformat()

        # A producer with a live job is not started again. `jobs()` is a
        # snapshot, so a job may finish between this read and the `start()`
        # below — that costs one tick's delay for that producer and nothing
        # else, which is the direction to be wrong in.
        #
        # Every live job is collected, not the first one found. `local` is
        # unlimited, so a person can start the same producer twice from the
        # interface, and picking one of the two by iteration order would put
        # half the evidence in front of an operator with nothing to say the
        # other half existed. They come out in the order they began, which is
        # the order `jobs()` reports.
        running = {}
        for job in self._runner.jobs():
            if job.state == "running":
                running.setdefault(job.producer, []).append(job.id)

        started = {}
        skipped = dict(reasons)
        failed_to_start = {}
        for name in due_names:
            live = running.get(name)
            if live:
                skipped[name] = "already running as " + ", ".join(
                    f"job {job_id}" for job_id in live)
                continue
            try:
                job = self._runner.start(name, trigger="scheduled")
            except JobRejected as exc:
                skipped[name] = f"cost class saturated: {exc}"
                continue
            except Exception as exc:
                # Name the type as well as the message, for the reason the
                # runner does: `str(exc)` alone is '' for a bare
                # `raise SomeError()` and a lone key name for a `KeyError`,
                # neither of which an operator could act on.
                failed_to_start[name] = f"{type(exc).__name__}: {exc}"
                continue
            started[name] = job.id
            self._store.record_run(name, at)

        return TickResult(at=at, due=due_names, started=started,
                          skipped=skipped, failed_to_start=failed_to_start)

    def start(self):
        """Begin ticking in the background until `close()`.

        The first tick happens straight away rather than after an interval: a
        process that has just restarted is the most likely one to be holding
        an overdue producer, and waiting a full interval before so much as
        looking would be the wrong way round.

        Refuses three things, all of them for the same reason — each would
        otherwise be a scheduler that looks like it is ticking and is not, or
        is ticking twice:

        - **starting twice**, which would run two loops against one schedule
          and double every producer's rate for as long as nobody noticed;
        - **starting after `close()`**, because the stop event stays set and
          the new loop would exit on its first wait, leaving a scheduler that
          reports itself as started and never ticks again;
        - **a producer the schedule does not cover**, which is the one the
          constructor cannot catch: registered after the scheduler was built,
          it would be a producer nobody runs and nothing mentions. This is
          the last moment it can be said out loud.

        The thread is a daemon, as the runner's workers are: an operator who
        kills the process should not have to close the scheduler first, and a
        loop that outlived the interpreter would be worse than one that stops
        abruptly — nothing it does is a write anybody is waiting on.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    "this scheduler has been closed and will not tick again; "
                    "build another one rather than restarting this one"
                )
            if self._thread is not None:
                raise RuntimeError(
                    "this scheduler is already ticking; a second loop would "
                    "run every producer at twice its cadence"
                )
            unscheduled = sorted(
                producer.name for producer in self._runner.producers()
                if producer.name not in self._entries
            )
            if unscheduled:
                raise ValueError(
                    f"producer(s) {unscheduled} are registered but not in the "
                    "schedule, so nothing would ever run them: the schedule is "
                    "resolved when the scheduler is built, so register every "
                    "producer before building it"
                )
            thread = threading.Thread(target=self._loop, daemon=True,
                                      name="cronicled-scheduler")
            self._thread = thread
            try:
                thread.start()
            except Exception:
                # No loop exists, so nothing can be running: unwinding is
                # safe, and leaving `_thread` set would wedge this scheduler
                # as "already ticking" for ever over a loop that never began.
                self._thread = None
                raise

    def close(self, timeout=None):
        """Stop ticking, and say whether the loop has actually stopped.

        Returns `True` once the loop thread has finished — including when
        there was never a loop to stop, so closing a scheduler that was never
        started is an ordinary answer rather than an error. `False` means the
        `timeout` elapsed with the loop still going, which is a tick wedged in
        the store or the runner; the return value is the only way to find that
        out, so it is a value worth looking at rather than a formality.

        Idempotent: closing twice sets the same event and joins the same
        finished thread. Once closed, a scheduler stays closed — `start()`
        refuses rather than trying to reuse a stop event that is already set.

        **It stops the ticking, not the producers.** There is no
        cancellation, so a job started by an earlier tick keeps running and
        this does not wait for it. Claiming otherwise would be the stronger
        promise this module deliberately does not make; the runner owns those
        jobs and is the thing to ask about them.
        """
        with self._lock:
            self._closed = True
            thread = self._thread
        self._stop.set()
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def status(self):
        """What the loop has been doing, as a `LoopStatus`.

        Safe to call from any thread, and from before `start()` — the answer
        for a scheduler that never started and one whose loop died differ in
        `closed`, which is exactly the distinction worth having.
        """
        with self._lock:
            thread = self._thread
            return LoopStatus(
                running=thread is not None and thread.is_alive(),
                closed=self._closed,
                ticks=self._ticks,
                failures=self._failures,
                consecutive_failures=self._consecutive_failures,
                last_tick_at=self._last_tick_at,
                last_error=self._last_error,
                last_error_at=self._last_error_at,
                last_traceback=self._last_traceback,
                # A copy: a caller holding the answer must not be able to
                # edit the loop's own counts by mutating it.
                failing_to_start=dict(self._failing_to_start),
                # Not copied, and does not need to be: `TickResult` is
                # frozen, and nothing writes to the dicts it carries after
                # the tick that built them returned.
                last_result=self._last_result,
            )

    def _loop(self):
        """Tick, wait, repeat, until `close()` — surviving whatever a tick
        does short of ending the interpreter.

        **The catch is the point of this whole method.** An exception on a
        thread does not fail anything a caller can see: it prints to stderr
        and the thread quietly dies. A scheduler whose loop died three days
        ago looks exactly like a scheduler with nothing to do, and the only
        symptom is an inbox that stopped filling. So a tick that raises is
        recorded and the loop comes round again — including for the failures
        a tick cannot recover from itself, such as a store that cannot record
        a run and therefore abandons the producers after the first.

        Recorded, not swallowed: without `LoopStatus` this would be a loop
        that survives invisibly, which is only marginally better than one
        that dies invisibly.

        A `BaseException` that is not an `Exception` — `SystemExit`, an
        injected interrupt — ends the loop, because it is not this module's
        to override, but it is recorded on the way out for the same reason.
        `status()` then reads `running=False, closed=False`, which is the
        signature of a loop that died rather than one that was stopped.

        The wait is on the stop event, never `sleep`. `close()` sets it, so a
        scheduler on an hourly interval stops in the time it takes to join a
        thread rather than in an hour — and a shutdown that takes an hour is a
        shutdown somebody replaces with a kill signal, which is how a service
        loses whatever it was doing.
        """
        while True:
            try:
                result = self.tick()
            except BaseException as exc:
                self._note_failure(exc)
                if not isinstance(exc, Exception):
                    return
            else:
                self._note_tick(result)
            if self._stop.wait(self._interval):
                return

    def _note_tick(self, result):
        """Record a tick that finished, and keep the count of producers the
        runner will not start.

        The count is of *consecutive* ticks: a producer that started, or that
        was merely skipped this time, comes off the list. A count that only
        ever went up would say "failing on every tick" about a producer that
        failed once at breakfast and has been fine since, which is the kind of
        number an operator learns to ignore.
        """
        with self._lock:
            self._ticks += 1
            self._last_tick_at = result.at
            # The whole result, not a summary of it: `status()` is the only
            # way anybody sees why a due producer was left alone, and a
            # summary would have to decide in advance which of those reasons
            # was worth keeping. See `LoopStatus.last_result`.
            self._last_result = result
            self._consecutive_failures = 0
            self._failing_to_start = {
                name: self._failing_to_start.get(name, 0) + 1
                for name in result.failed_to_start
            }

    def _note_failure(self, exc):
        """Record a tick that raised. Called from inside the `except` block,
        so `format_exc` still has the exception being handled.

        The type as well as the message, and the frames as well as the type,
        for the reason the runner keeps both: `str(exc)` is `''` for a bare
        `raise SomeError()` and a lone key name for a `KeyError`, and a
        failure an operator cannot act on is barely better than none.

        `failing_to_start` is deliberately left alone here. A tick that raised
        never reached the runner for some or all of its producers, so it is
        evidence of nothing either way, and resetting the count on it would
        let a store failing every other tick keep clearing the record of a
        producer that has not started in a week.
        """
        detail = f"{type(exc).__name__}: {exc}"
        frames = traceback.format_exc()
        at = self._stamp()
        with self._lock:
            self._failures += 1
            self._consecutive_failures += 1
            self._last_error = detail
            self._last_error_at = at
            self._last_traceback = frames

    def _stamp(self):
        """The moment to record a failure against.

        The injected clock, so a test says something exact and an operator
        reads failure times in the same frame as run times — but never at the
        cost of the record itself. A clock that raises, or that hands back a
        naive datetime, is one of the things that makes a tick fail in the
        first place, and trusting it here would lose the failure at exactly
        the moment there is most to say about it. So it falls back to the real
        UTC clock, which cannot be broken by wiring.
        """
        try:
            moment = _moment(self._clock())
        except Exception:
            moment = None
        if moment is None:
            moment = _utcnow()
        return moment.isoformat()
