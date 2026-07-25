"""Runs producers in the background so a long scan cannot block the interface.

A producer is any object with `name`, `cost`, and `produce(ctx)`: a generator
that yields dicts of `folder`, `subject_type`, `subject_id`, `summary`,
`payload`, and optional `confidence` — one per proposal it finds. `produce`
is a generator, not a function that returns a list, and that is the whole
design of this module: the runner records each proposal as it is yielded,
not after the producer finishes. A scan that dies partway through a
five-hundred-file library keeps the proposals it already found; collecting a
list and storing it at the end would lose all of them on the same failure,
which is the ordinary outcome of a long scrape against a flaky server, not
an edge case. `start()` enforces this rather than trusting it: a `produce`
that hands back anything but a generator is refused there, because such a
producer would silently lose the guarantee with nothing to tell its author.

`ctx` gives a producer exactly one thing: `log(message)`. It does not get the
store. Persistence, and the dismissal/mute rules that make a reviewer's past
decisions stick, belong to the runner alone — a producer that wrote to the
store directly could bypass them, and could not be tested without a runner
and a store to go with it.
"""
import inspect
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class JobRejected(Exception):
    """Raised by `start()` when the producer's cost class already has a job
    running against it. Names the job that is already running, so a caller
    told no can go find out what is holding the slot rather than just being
    left to guess."""


# Per-cost-class concurrency limits. `scraping` and `box` both drive a
# headless browser inside the media server: a second one running at the
# same time thrashes it and makes both slower, not faster, so each allows
# exactly one job at a time. `local` analysis has no such external
# bottleneck and must not queue behind a twenty-minute scrape, so it is
# unlimited (`None`).
COST_CLASS_LIMITS = {
    "scraping": 1,
    "box": 1,
    "local": None,
}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Job:
    """A snapshot of a job's state at the moment it was asked for, not a live
    handle onto the runner's bookkeeping. `JobRunner.job()` builds one while
    holding the runner's lock, so a caller can never see recorded/skipped/
    message update mid-write relative to each other.

    `recorded` and `skipped` count yields the store kept or declined, not
    distinct rows: a producer that yields the same proposal twice in one run
    reports `recorded=2` even though the store holds a single row for it.
    See `JobRunner._record` for the further wrinkle that `skipped` describes
    the store's eventual state, not necessarily the state at the instant the
    yield was recorded.

    `error` and `traceback` are the *only* record of a failure: the worker
    swallows whatever the producer raised so the thread cannot die silently,
    and nothing else logs it. So `error` always names the exception type as
    well as its message — `str(exc)` alone is empty for a bare
    `raise SomeError()`, and a mere `'folder'` for a missing key, neither of
    which a user could act on — and `traceback` keeps the frames that say
    which line of which producer gave up."""

    id: str
    producer: str
    cost: str
    state: str  # "running", "done", or "failed"
    started_at: str
    finished_at: Optional[str]
    message: str
    recorded: int
    skipped: int
    error: Optional[str]
    traceback: Optional[str]


class _JobContext:
    """What a producer receives as `ctx`: a place to log progress, nothing
    more. Built fresh per job so a producer cannot hold onto anything that
    outlives its own run."""

    def __init__(self, on_log):
        self._on_log = on_log

    def log(self, message):
        self._on_log(message)


class _JobState:
    """The runner's own mutable record for one job. Only ever touched while
    `JobRunner._lock` is held — reads (for a `Job` snapshot) and writes (as
    the worker thread makes progress) alike."""

    def __init__(self, job_id, producer, cost):
        self.id = job_id
        self.producer = producer
        self.cost = cost
        self.state = "running"
        self.started_at = _now()
        self.finished_at = None
        self.message = ""
        self.recorded = 0
        self.skipped = 0
        self.error = None
        self.traceback = None

    def snapshot(self):
        return Job(
            id=self.id,
            producer=self.producer,
            cost=self.cost,
            state=self.state,
            started_at=self.started_at,
            finished_at=self.finished_at,
            message=self.message,
            recorded=self.recorded,
            skipped=self.skipped,
            error=self.error,
            traceback=self.traceback,
        )


class JobRunner:
    """Drives registered producers on background threads and records what
    they yield through a shared `Store`.

    The runner holds the one `Store` it was given and shares it across every
    job it runs — `Store` is single-instance-per-file and enforces that
    itself, so nothing here opens a second handle on the same path.

    All of the runner's own bookkeeping (the `_JobState` for every job) is
    guarded by a single lock: the worker thread writes progress as it goes,
    while `job()`/`jobs()` may be called from another thread at any time, and
    neither side may observe the other mid-update.
    """

    def __init__(self, store):
        self._store = store
        self._producers = {}
        self._lock = threading.Lock()
        self._jobs = {}
        self._done = {}
        # cost class -> {job_id: producer_name} for every job of that class
        # currently running. Guarded by `_lock`, same as everything else:
        # the saturation check in `start()` and the reservation of a slot
        # must happen atomically, or two callers racing `start()` at once
        # could both see room and both proceed.
        self._running_by_cost = {cost: {} for cost in COST_CLASS_LIMITS}

    def register(self, producer):
        """Wire up a producer under its own name.

        Rejects an unknown cost class here, at registration, rather than
        later at `start()` — a typo in a cost class is a wiring mistake that
        should fail when the producer is set up, not hours later when a
        schedule tries to run it.

        Rejects a duplicate name on the same argument. Silently replacing a
        producer would swap out what a name runs — and its cost class with
        it, so a scrape could quietly start counting against `local` and lose
        the limit that protects the media server. Two producers claiming one
        name is a wiring mistake in exactly the same sense; if replacing one
        deliberately is ever wanted, that deserves its own method rather than
        happening by accident here.
        """
        if producer.cost not in COST_CLASS_LIMITS:
            raise ValueError(
                f"unknown cost class {producer.cost!r} for producer "
                f"{producer.name!r} (known: {sorted(COST_CLASS_LIMITS)})"
            )
        if producer.name in self._producers:
            raise ValueError(
                f"a producer named {producer.name!r} is already registered"
            )
        self._producers[producer.name] = producer

    def start(self, name):
        producer = self._producers[name]  # KeyError on an unknown producer
        job_id = str(uuid.uuid4())
        state = _JobState(job_id, producer.name, producer.cost)
        done = threading.Event()
        ctx = _JobContext(lambda message: self._log(state, message))
        stream = producer.produce(ctx)
        if not inspect.isgenerator(stream):
            # A `produce` that builds a list and returns it runs to
            # completion before the runner sees a single proposal, so a scan
            # that dies partway through records nothing at all — the exact
            # loss this module exists to prevent. Calling `produce` costs
            # nothing when it is a generator (the body does not run until
            # the worker iterates it), so the mistake can be caught here,
            # loudly, instead of silently degrading every run.
            raise TypeError(
                f"producer {producer.name!r} must make produce() a generator "
                f"that yields proposals one at a time, but it returned "
                f"{type(stream).__name__}"
            )
        limit = COST_CLASS_LIMITS[producer.cost]
        with self._lock:
            running = self._running_by_cost[producer.cost]
            if limit is not None and len(running) >= limit:
                # Refuse explicitly rather than queue: a caller who believes
                # a job started and is wrong is worse off than one told no.
                # This check and the reservation just below happen inside
                # the same lock acquisition, so two threads racing `start()`
                # can never both see room and both proceed.
                blockers = ", ".join(sorted(running.values()))
                raise JobRejected(
                    f"cost class {producer.cost!r} is already running "
                    f"{blockers}"
                )
            thread = threading.Thread(
                target=self._run, args=(producer, stream, state, done),
                daemon=True,
            )
            try:
                # All three reservations inside the guarded region, because
                # an exception can land at any bytecode boundary: one
                # landing between them would leave the cost class reserved
                # with no rollback, and `_jobs` populated without `_done` —
                # a job `job()` can see but `wait()` raises `KeyError` for.
                running[job_id] = producer.name
                self._jobs[job_id] = state
                self._done[job_id] = done
            except BaseException:
                # Nothing has been handed to a worker yet — no thread has
                # been started — so unwinding here is unconditionally safe,
                # whatever was raised.
                self._unreserve(job_id, producer.cost)
                raise
            try:
                # Started inside the same locked region that made the
                # reservation above, so reservation and start succeed or
                # fail together. This is safe: `Thread.start()` returns once
                # the new thread is running, it does not wait for the
                # worker to do anything, so a worker that immediately wants
                # `_lock` (in `_log`/`_record`) just blocks for the rest of
                # this `with` block.
                thread.start()
            except Exception:
                # The exception type is the best discriminator the parent
                # can observe. It is not a proof, and the difference matters:
                # of the exceptions the interpreter raises on its own behalf,
                # `Thread.start()` raises an `Exception` from one place only
                # — the spawn itself failing, OS resource exhaustion being
                # the realistic cause — which happens *before* any child
                # exists. In that case no worker exists, `_run`'s `finally`
                # will never run to release the slot, and unwinding here is
                # both correct and the only thing that stops every later
                # `start()` for this class being refused forever, citing a
                # job whose thread never began.
                #
                # What defeats it: an exception delivered *asynchronously*
                # whose type happens to be an `Exception` — a signal handler
                # that raises one, or an injected async exception — can land
                # in the `self._started.wait()` after the spawn and arrive
                # here with a live worker behind it. This clause would then
                # free a running job's slot. The parent cannot distinguish
                # that case: `start()` discards the ident the spawn returns,
                # and every other liveness signal is set by the child. This
                # is accepted rather than solved, because it requires the
                # embedding process to install such a handler, and because
                # the obvious alternative — asking whether the thread is
                # alive — is wrong for the *default* interrupt instead.
                self._unreserve(job_id, producer.cost)
                raise
            # A `BaseException` that is not an `Exception` — an interrupt
            # landing on this thread at just the wrong microsecond —
            # deliberately has no handler: it propagates with the
            # reservation left standing. `Thread.start()` spawns the child
            # and *then* blocks in `self._started.wait()`, and it is the
            # child that sets `_started`, so for the whole of that wait the
            # worker is already on its way while every "is it running yet"
            # test the parent could make still says no. Since we cannot
            # tell, assume the worker exists and let its own `finally` be
            # the sole authority over the slot — it takes `_lock` to do
            # that, so it releases the slot as soon as this locked region
            # ends, and cannot possibly have released it already.
            #
            # The residual, stated plainly: an interrupt landing inside
            # `Thread.start()` but *before* it spawns the child leaks the
            # reservation and wedges that cost class. Nothing here can tell
            # that case from the one above, and this is the deliberate trade
            # between them — a wedged class is visible in `jobs()` as a job
            # that never leaves `running`, while freeing a live worker's
            # slot silently breaks the one limit this whole mechanism exists
            # to enforce. "Visible" is the honest word, not "recoverable":
            # there is no cancel or release API yet, so clearing a wedged
            # class means restarting the process.
            snapshot = state.snapshot()
        return snapshot

    def _unreserve(self, job_id, cost):
        """Undo `start()`'s three-part reservation. Called only from
        `start()`, only with `_lock` already held, and only where no worker
        can exist to be robbed of its slot — never for a job whose thread
        may have started."""
        self._running_by_cost[cost].pop(job_id, None)
        self._jobs.pop(job_id, None)
        self._done.pop(job_id, None)

    def _run(self, producer, stream, state, done):
        """Drain `stream` — the generator `start()` already got from
        `producer.produce(ctx)` — wrapped so nothing it raises escapes this
        thread. An exception in a background thread does not fail anything
        visible to a caller — it prints to stderr and the thread quietly
        dies — so without this a broken producer would leave its job stuck
        in `running` forever with no record of why. The generator's body has
        not run yet: making one is not running one, so all of the producer's
        actual work still happens here, off the caller's thread.
        """
        try:
            for proposal in stream:
                self._record(state, proposal, producer.name)
        except BaseException as exc:
            # Deliberately broader than `except Exception`: a producer is
            # arbitrary third-party code running on a thread with no other
            # supervisor, and `KeyboardInterrupt`/`SystemExit`/a leaked
            # `GeneratorExit` are all `BaseException`, not `Exception`. Any
            # of them escaping here would leave the job in `running` forever
            # — indistinguishable from one still genuinely working — because
            # nothing else ever marks it failed. Do not narrow this back to
            # `Exception`; do not re-raise, either: this is a worker thread,
            # so a re-raise only reaches the thread excepthook and prints,
            # it does not propagate anywhere a caller could observe.
            #
            # Name the type, and keep the frames. This is the only record of
            # the failure that will ever exist — nothing re-raises, nothing
            # logs — and `str(exc)` alone is not one: it is `''` for a bare
            # `raise SomeError()`, and `'folder'` for a producer yielding a
            # dict missing that key, indistinguishable from an unrelated
            # `KeyError` five frames down. A failure a user cannot tell from
            # a quiet success is the failure mode this module is meant to
            # rule out.
            with self._lock:
                state.state = "failed"
                state.error = f"{type(exc).__name__}: {exc}"
                state.traceback = traceback.format_exc()
                state.finished_at = _now()
        else:
            with self._lock:
                state.state = "done"
                state.finished_at = _now()
        finally:
            # Release the cost-class slot no matter how the job ended. A
            # crashed scrape that never frees its slot would permanently
            # block every future scrape — a deadlock in slow motion, with
            # nothing in the logs to say why the tool stopped working.
            with self._lock:
                self._running_by_cost[state.cost].pop(state.id, None)
            done.set()

    def _log(self, state, message):
        with self._lock:
            state.message = message

    def _record(self, state, proposal, producer_name):
        """Store one yielded proposal and count whether it was recorded or
        skipped.

        `store.record()` returns a fingerprint whether it stored the
        proposal or declined it — a dismissed or muted subject returns the
        same fingerprint a freshly-stored one would. Telling the two apart
        needs an actual check: `store.has()` answers exactly this, with a
        single primary-key lookup rather than a scan of the whole folder —
        if it is there, the store kept it (inserted or touched); if not, the
        store declined it, whether the row exists in a hidden state or
        never existed at all (a pre-emptive mute/dismissal blocks the insert
        entirely). That presence is exactly the "did a reviewer's own
        decision suppress this" signal a user needs to tell a quiet
        producer from a broken one.

        `store.record()` and `store.has()` are two separate lock
        acquisitions on the store, not one atomic check-and-record: a
        reviewer who dismisses the item in the gap between them makes this
        yield count as `skipped` even though it was `recorded` at the
        instant `store.record()` ran. The count describes the store's
        eventual state as observed here, not a guarantee about the instant
        of the yield.
        """
        fp = self._store.record(
            folder=proposal["folder"],
            subject_type=proposal["subject_type"],
            subject_id=proposal["subject_id"],
            summary=proposal["summary"],
            payload=proposal["payload"],
            producer=producer_name,
            confidence=proposal.get("confidence"),
        )
        kept = self._store.has(fp)
        with self._lock:
            if kept:
                state.recorded += 1
            else:
                state.skipped += 1

    def job(self, job_id):
        with self._lock:
            return self._jobs[job_id].snapshot()

    def jobs(self):
        with self._lock:
            return [state.snapshot() for state in self._jobs.values()]

    def wait(self, job_id, timeout=None):
        """Block until the job finishes (or `timeout` elapses), for tests.
        Never sleeps to poll — it waits on the same `threading.Event` the
        worker thread sets when it is done."""
        with self._lock:
            done = self._done[job_id]
        return done.wait(timeout)
