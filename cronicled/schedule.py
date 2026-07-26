"""Which producers are due, why the others are not, and starting the ones that
are.

This is arithmetic and rules over data: no thread, no sleep, no wall clock.
`now` and the last-run times are arguments, so every scheduling rule — the
exact second a cadence elapses, a machine whose clock jumped, a service that
was off for a week — is an ordinary unit test rather than something that needs
a fake clock inside a running loop. `Scheduler.tick` keeps that property: it
is called directly, once, and returns what it did. The loop that calls it on a
timer is a separate concern and deliberately holds none of the rules.

Two answers come back from `due`, not one: what to run, and a reason for each
producer that is being left alone. An operator asking "why has the nightly
scrape not run?" is asking about the second answer, and a scheduler that only
returns the first can only be debugged by re-deriving its arithmetic by hand.
`TickResult` carries the same shape one layer up.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .jobs import JobRejected

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


def _utcnow():
    """The default clock: the current time, aware and in UTC.

    Aware because `due` refuses a naive `now` outright — every timestamp the
    store holds is UTC, and assuming a naive one is too would shift every
    producer's due-ness by the machine's offset without saying so.
    """
    return datetime.now(timezone.utc)


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
    runner has registered. A producer with no cadence and no override is a
    `ValueError` there — at start-up, where an operator reads it as a stack
    trace, rather than at 3am as a producer that quietly never ran. The
    consequence to know about: a producer registered *after* the scheduler
    was built is not scheduled, because the schedule was already decided.
    """

    def __init__(self, runner, store, *, overrides=None, clock=None):
        self._runner = runner
        self._store = store
        self._clock = _utcnow if clock is None else clock
        self._entries = resolve(runner.producers(), overrides)

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
        """
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
                job = self._runner.start(name)
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
