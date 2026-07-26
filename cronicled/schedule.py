"""Which producers are due, and why the others are not.

This is arithmetic and rules over data: no thread, no sleep, no wall clock.
`now` and the last-run times are arguments, so every scheduling rule — the
exact second a cadence elapses, a machine whose clock jumped, a service that
was off for a week — is an ordinary unit test rather than something that needs
a fake clock inside a running loop. The loop that calls this on a timer is a
separate concern and deliberately holds none of the rules.

Two answers come back from `due`, not one: what to run, and a reason for each
producer that is being left alone. An operator asking "why has the nightly
scrape not run?" is asking about the second answer, and a scheduler that only
returns the first can only be debugged by re-deriving its arithmetic by hand.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# What a schedule override may say. Anything else is a typo — see `resolve`.
OVERRIDE_KEYS = ("every", "enabled")


@dataclass(frozen=True)
class Entry:
    """One producer's resolved schedule: how often, and whether at all.

    `every` is a number of seconds. It may be `None` only for a disabled
    entry — an enabled entry with no cadence is the wiring mistake `resolve`
    refuses, and `due` refuses it again rather than treating it as a producer
    that silently never runs.
    """

    producer: str
    every: Optional[float]
    enabled: bool = True


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


def resolve(producers, overrides=None):
    """Work out each producer's schedule, as `{name: Entry}`.

    A producer declares its own cadence as `every` (seconds). An override,
    keyed by producer name, may replace that cadence, disable the producer, or
    both: `{"nightly-scan": {"every": 3600}}`, `{"box-scrape": {"enabled":
    false}}`.

    Everything this refuses, it refuses here — at the point the schedule is
    wired up, rather than hours later when a tick tries to use it. The same
    reasoning as `JobRunner.register` rejecting an unknown cost class and a
    duplicate name: a wiring mistake that surfaces at startup is a stack trace
    an operator reads, while one that surfaces at 3am is a producer that
    quietly did not run.

    So each of these is a `ValueError`:

    - **A producer with no cadence and no override supplying one**, which
      would otherwise be a producer nobody scheduled. A missing `every`
      attribute and an `every` of `None` are the same mistake, not two. Note
      that an override saying only `{"enabled": true}` does NOT satisfy this:
      naming a producer is not scheduling it, and letting a mention silence
      the error would hide the mistake behind config that looks deliberate.
      A producer explicitly *disabled* is exempt — that is a scheduling
      decision, which is the opposite of an omission.
    - **An override naming a producer that does not exist**, which is a typo
      in a name; ignoring it would leave the real producer running on the
      cadence the operator believed they had changed.
    - **An override that is not a mapping, or carries a key other than
      `every`/`enabled`** — again, typos that would otherwise be dropped.
    - **A cadence that is not a positive number**, from either source.
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
        declared[name] = getattr(producer, "every", None)

    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise ValueError(
            f"schedule override names unknown producer(s) {unknown} "
            f"(known: {sorted(declared)})"
        )

    entries = {}
    for name, every in declared.items():
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

        if "every" in override:
            every = _check_every(override["every"], name, "from the override")
        elif every is not None:
            every = _check_every(every, name, "declared by the producer")

        if every is None and enabled:
            raise ValueError(
                f"producer {name!r} has no cadence: it declares no 'every' and "
                "no schedule override supplies one, so nothing would ever run "
                "it. Give it a cadence, or disable it explicitly with "
                '{"enabled": false}.'
            )

        entries[name] = Entry(producer=name, every=every, enabled=enabled)
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

    A producer is due when it has never run, or when at least `every` seconds
    have passed since it last ran. **Exactly `every` seconds is due**: the
    other choice delays every producer by a whole tick, every cycle, forever,
    and nothing about that is visible.

    Nothing here makes up missed runs. A producer whose last run was a week
    ago on a nightly cadence is due **once**, not seven times: this returns a
    set of names to run now, and the next run is counted from when that run is
    actually recorded, not from the schedule that was missed. A service that
    was off for a week must not wake up and fire seven scrapes at a media
    server, which is precisely the load the cost classes exist to prevent.

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
    at = _moment(now)
    if at is None:
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
        if entry.every is None:
            raise ValueError(
                f"schedule entry for producer {name!r} is enabled but has no "
                "cadence, so there is no interval to compare against"
            )

        recorded = runs.get(name)
        if recorded is None:
            due_names.append(name)
            continue
        last = _moment(recorded)
        if last is None or last > at:
            due_names.append(name)
            continue

        next_due = last + timedelta(seconds=entry.every)
        if at >= next_due:
            due_names.append(name)
        else:
            reasons[name] = (
                f"last ran {recorded}; next due at {next_due.isoformat()}"
            )
    return due_names, reasons
