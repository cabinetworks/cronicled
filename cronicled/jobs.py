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

A runner is built to outlive the work it is handed, which costs it two things
that a process living for minutes never notices. It forgets finished jobs past
a cap, so an always-on process does not accumulate one record per job for as
long as it is up, and admits how many it has forgotten rather than returning a
truncated history that reads like a complete one. And `close()` stops it
accepting work and waits for what is in flight, so a restart can tell whether
it is safe to go. Neither is cancellation: nothing here interrupts a producer,
and nothing asks one to stop early.
"""
import collections
import inspect
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# How many *finished* jobs a runner remembers by default. Running jobs are
# never counted against it — see `JobRunner._retire`.
#
# The number is a compromise between two things a caller wants and cannot
# have at once. A person debugging wants the history to reach back past
# whatever they were doing an hour ago; a process that stays up for weeks
# wants a ceiling that does not depend on how long it has been up. Two
# hundred sub-kilobyte records is a few hundred kilobytes at worst — noise
# next to the interpreter itself — and at the rate a schedule realistically
# starts producers (a handful an hour, not a handful a second) it covers
# days rather than minutes. It is a constructor argument because that rate
# is the operator's, not this module's: someone running a producer every
# minute should raise it, and nothing here can guess that for them.
DEFAULT_HISTORY = 200


class JobForgotten(KeyError):
    """Raised by `job()`/`wait()` for an id the runner no longer holds, when
    it has evicted at least one finished job.

    Deliberately distinct from the plain `KeyError` an id that never existed
    raises, because the two send a caller to different places. "I never had
    that id" is the caller's own bug — a typo, a stale link, an id from a
    previous process. "That job ran, finished, and has since been forgotten"
    is not a bug at all: the work happened, and only the record of how it
    went is gone. Returning one answer for both would leave a caller polling
    a job id unable to tell whether to fix its id or to accept that it asked
    too late.

    It cannot be per-id certain, and says so in its message rather than
    pretending otherwise: keeping the ids of evicted jobs in order to
    recognise them later would rebuild, one id at a time, the unbounded
    growth the eviction exists to stop. So the runner keeps a count instead
    of a set, and this exception means "not held, and N finished jobs have
    been forgotten, so it may have been one of them". When nothing has been
    evicted yet, that ambiguity does not exist and the plain `KeyError`
    still stands for "this never ran here".

    Subclasses `KeyError` so callers written against the older behaviour —
    where an unknown id was always a `KeyError` — keep working unchanged.
    """


class JobHistory(list):
    """What `jobs()` returns: the job snapshots it still holds, plus
    `evicted`, the number of finished jobs it has dropped.

    A plain list would be an introspection API that quietly claims to be
    complete. A caller reading four jobs out of a runner that has run four
    thousand would conclude nothing else ever ran, and there would be
    nothing in the value to suggest otherwise.

    It is a `list` subclass rather than a second method for two reasons.
    Existing callers keep an ordinary list — indexing, iteration, and
    equality against a plain list all behave exactly as before. And the
    count comes out of the same lock acquisition as the snapshots, so it
    describes *this* list: an `evicted()` method called separately could be
    answered after another job had been evicted, quietly reporting a total
    that matches neither call.

    Note that `evicted` takes no part in equality, because list equality is
    element-wise: two histories with the same jobs compare equal whatever
    each has forgotten. A test that cares about the count has to read it.
    """

    def __init__(self, jobs=(), evicted=0):
        super().__init__(jobs)
        self.evicted = evicted


class RunnerClosed(Exception):
    """Raised by `start()` on a runner that has been closed.

    Deliberately *not* a `JobRejected`, though both refuse a start. A caller
    catching `JobRejected` has been told "not now" about a cost class that
    frees up on its own, and the reasonable response is to try again later.
    A closed runner never frees up, so that same handler would retry until
    the process died. A separate exception means the handler does not catch
    it, and a scheduler that has not thought about shutdown fails where it
    needs to rather than spinning quietly.
    """


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
    which line of which producer gave up.

    `duration` is the job's own elapsed seconds, `None` while `state` is
    still `"running"`. It exists because `started_at` and `finished_at` are
    ISO timestamps truncated to whole seconds (see `_now`), so subtracting
    them loses every job that finishes inside the same second it started —
    which is the ordinary case, not the exception, for most producers. This
    field is measured separately, from a monotonic clock at the moment the
    job actually started and the moment it actually finished, so it carries
    sub-second precision and is immune to the wall clock being adjusted
    mid-job (an NTP step, a DST change) in a way `finished_at - started_at`
    would not be."""

    id: str
    producer: str
    cost: str
    state: str  # "running", "done", or "failed"
    started_at: str
    finished_at: Optional[str]
    duration: Optional[float]
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
        # A monotonic reading taken alongside `started_at`, for `duration`
        # alone — never exposed itself, and never used to derive `started_at`
        # or `finished_at`. See `Job.duration` for why a second clock is
        # worth the trouble: the wall-clock timestamps above are truncated to
        # whole seconds and can also jump if the system clock is adjusted
        # mid-job, and a monotonic clock has neither problem.
        self._started_monotonic = time.monotonic()
        self.finished_at = None
        self.duration = None
        self.message = ""
        self.recorded = 0
        self.skipped = 0
        self.error = None
        self.traceback = None

    def finish(self):
        """Stamp `finished_at` and `duration` together, from the same
        monotonic reading, so the two always describe the same instant.
        Called exactly once, from `_run`'s success and failure branches
        alike, with `_lock` already held."""
        self.finished_at = _now()
        self.duration = time.monotonic() - self._started_monotonic

    def snapshot(self):
        return Job(
            id=self.id,
            producer=self.producer,
            cost=self.cost,
            state=self.state,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration=self.duration,
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

    def __init__(self, store, history=DEFAULT_HISTORY):
        """`history` caps how many *finished* jobs the runner remembers.

        Refused below 1, and refused for a `bool`. A cap of zero would drop
        every job at the instant it finished, turning the ordinary
        `wait(id)` then `job(id)` into a race against the runner's own
        bookkeeping — there is no reading of it that anyone wants, so it is
        not quietly accepted. `True` is 1 to every integer test in Python,
        so `history=True` — a plausible way to write "yes, keep history" —
        would silently keep exactly one job and throw the rest away.

        There is deliberately no way to ask for an unbounded history. That
        is the behaviour this cap exists to remove, and an opt-out would
        just be the same leak with a flag in front of it.
        """
        if isinstance(history, bool) or not isinstance(history, int):
            raise ValueError(
                f"history must be an int, not {type(history).__name__} "
                f"({history!r}); it caps how many finished jobs are kept"
            )
        if history < 1:
            raise ValueError(
                f"history must be at least 1, not {history}: a runner that "
                f"forgets a job the moment it finishes cannot answer job() "
                f"for a job that just ended"
            )
        self._store = store
        self._history = history
        self._producers = {}
        self._lock = threading.Lock()
        self._jobs = {}
        self._done = {}
        # Job ids in the order they reached a terminal state, which is not
        # the order they started in: a job started first can finish last.
        # Eviction takes from the left, so the oldest *finished* job goes
        # first — a caller asking about history is nearly always asking
        # what happened recently. Only ids that have finished are ever put
        # here, which is the whole of why a running job cannot be evicted.
        self._finished = collections.deque()
        self._evicted = 0
        # Set by `close()`, read by `start()`, and — like the cost-class
        # bookkeeping — only ever touched under `_lock`, because the refusal
        # it drives and the reservation `start()` makes have to be one
        # decision. See `close()`.
        self._closed = False
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

    def reregister(self, producer):
        """Deliberately replace whatever is registered under `producer.name`,
        including its cost class if the new producer's differs. This is the
        "own method" `register()`'s docstring reserves for a caller that
        genuinely wants to replace a registration, rather than doing it by
        accident.

        The caller this exists for: a producer built fresh per run with a
        parameter `produce(ctx)` itself takes no argument for (a scan's
        `limit`, say), started under one fixed name rather than a name
        invented per call so the registry does not grow by one small object
        every time a person clicks a button. See
        `cronicled.web.actions.Actions.scan`.

        Safe with a job already running under the previous object at the
        same name: `start()` reads the producer out of this registry once,
        at the moment it is called, and hands that exact object's own
        `produce(ctx)` generator to the worker thread directly. Nothing in
        `_run` looks a producer back up by name mid-job, so replacing the
        registry entry here cannot reach into a job already in flight. The
        cost-class limit `start()` enforces is unaffected either way, since
        it counts currently-running jobs by `producer.cost` at the moment
        `start()` is called, not by the identity of whichever object is
        sitting in the registry.

        Still refuses an unknown cost class, on the same terms as
        `register()` — a typo here is exactly as much a wiring mistake as
        it is there.
        """
        if producer.cost not in COST_CLASS_LIMITS:
            raise ValueError(
                f"unknown cost class {producer.cost!r} for producer "
                f"{producer.name!r} (known: {sorted(COST_CLASS_LIMITS)})"
            )
        self._producers[producer.name] = producer

    def producers(self):
        """Every registered producer, in the order they were registered.

        The objects themselves, not their names: a schedule reads a
        producer's own declared cadence off it, and registration is the only
        place those objects exist. A new list each call, so a caller holding
        onto the answer cannot rewire the runner by mutating it.

        Registration is wiring-time and single-threaded, in the same sense
        `start()`'s own read of this mapping is; the runner's lock guards the
        per-job bookkeeping that a worker thread writes, which this is not.
        """
        return list(self._producers.values())

    def start(self, name, *, trigger):
        """Start `name` on a background thread and record the run.

        `trigger` says how this start came about -- "scheduled" or "manual",
        the values `Store.start_run` accepts. Required rather than defaulted:
        the same producer runs both ways, and a default would silently label
        whichever call site forgot to say. A reader asking "did last night's
        pass run" is asking about the scheduled one, not about a button
        somebody pressed at noon, and only the caller knows which this is.

        The run row is opened here and closed by `_run`'s `finally`, so a
        producer that raises still closes its row -- see `_run`. The one thing
        that must not happen is a row left open by a start that never handed
        work to a worker: retention deliberately never evicts an unfinished
        row (see `Store.finish_run`), and a refused start is ordinary rather
        than exceptional -- pressing Scan while a scan runs raises
        `JobRejected` -- so a leak here would grow the table for the life of
        the deployment. Every path below that refuses without a worker behind
        it therefore closes the row before re-raising.
        """
        producer = self._producers[name]  # KeyError on an unknown producer
        # Before anything else that could refuse, so an unknown producer and
        # an unknown trigger both leave nothing behind to close.
        run_id = self._store.start_run(producer.name, trigger=trigger)
        # Flipped at the point the code below stops unwinding its own
        # reservation, and for exactly the same reason: from there on a worker
        # may exist, and its `finally` is the sole authority over both the
        # cost-class slot and this row. Closing the row here as well could
        # overwrite a live worker's own verdict with this thread's. A list
        # because the flag is written from inside the nested handlers below.
        handed_over = []
        try:
            return self._begin(producer, run_id, handed_over)
        except BaseException as exc:
            if not handed_over:
                self._store.finish_run(
                    run_id, outcome="failed",
                    error=f"did not start -- {type(exc).__name__}: {exc}")
            raise

    def _begin(self, producer, run_id, handed_over):
        """The body of `start()`. Appends to `handed_over` at the moment a
        worker may exist; see `start()`."""
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
            if self._closed:
                # Inside the same lock acquisition that makes the
                # reservation below, and for the same reason the saturation
                # check is: a check that released the lock before reserving
                # could be passed by a `start()` that then reserves a slot
                # after `close()` has already taken its list of jobs to wait
                # for. The drain would report everything finished with that
                # job running behind it, which is the one thing `close()`
                # exists to rule out.
                #
                # Ahead of the saturation check, because a closed runner is
                # a permanent no and a saturated one is not: told the class
                # is busy, a caller retries, and every retry gets the same
                # misleading answer.
                raise RunnerClosed(
                    f"the runner is closed and is not accepting work; "
                    f"{producer.name!r} was not started"
                )
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
                target=self._run, args=(producer, stream, state, done, run_id),
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
            # From here on the run row belongs to the worker, on exactly the
            # terms the slot does: `Thread.start()` spawns the child and then
            # waits on an event the child sets, so for the whole of that wait
            # a worker may already exist while every test the parent could
            # make says it does not. `start()` therefore stops closing the row
            # here, and the `except Exception` below takes it back — that
            # clause is reached only when the spawn itself failed, before any
            # child existed.
            handed_over.append(True)
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
                handed_over.clear()
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

    def _run(self, producer, stream, state, done, run_id):
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
                state.finish()
        else:
            with self._lock:
                state.state = "done"
                state.finish()
        finally:
            # Release the cost-class slot no matter how the job ended. A
            # crashed scrape that never frees its slot would permanently
            # block every future scrape — a deadlock in slow motion, with
            # nothing in the logs to say why the tool stopped working.
            with self._lock:
                self._running_by_cost[state.cost].pop(state.id, None)
                self._retire(state.id)
                # Read under the lock, written to the store outside it. The
                # branches above are the only writers of `error`, and both
                # took this lock to do it; the store takes a lock of its own
                # and a commit is not something to hold this one across.
                #
                # A failed run is closed exactly as a completed one is, and
                # the `finally` is why: "did last night's scan run?" is what
                # the log exists to answer, and a log that records only the
                # runs that worked answers the opposite question.
                outcome = "failed" if state.error else "completed"
                counts = {"recorded": state.recorded, "skipped": state.skipped}
                error = state.error
            try:
                # Before `done.set()`, not after: a caller that waits for the
                # job and then reads the log must not be shown its own run
                # still open. `wait()` returning is the only signal there is.
                self._store.finish_run(run_id, outcome=outcome, counts=counts,
                                       error=error)
            finally:
                # Even if the store refused. A `done` that is never set wedges
                # every `wait()` for this job forever, with no timeout of its
                # own to end it; an unclosed row is a visible wrong answer on
                # one page. The store's exception still propagates to the
                # thread excepthook rather than being swallowed.
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

    def _retire(self, job_id):
        """Record that a job has finished, and drop the oldest finished jobs
        while there are more than `history` of them.

        Called from `_run`'s `finally`, with `_lock` held, and only ever for
        a job that has just reached a terminal state. Nothing else appends
        to `_finished`, so a running job is not a candidate for eviction —
        not because a check excludes it, but because it is not in the
        structure eviction reads. Running jobs are already bounded by the
        per-cost-class limits; it is the finished ones that accumulate for
        the life of the process.
        """
        self._finished.append(job_id)
        while len(self._finished) > self._history:
            stale = self._finished.popleft()
            self._jobs.pop(stale, None)
            self._done.pop(stale, None)
            self._evicted += 1

    def _missing(self, job_id):
        """The exception for an id the runner does not hold. Requires
        `_lock`, because it reads the eviction count.

        Returns rather than raises so the caller raises it, keeping the
        traceback pointed at the method the caller actually called.
        """
        if self._evicted:
            return JobForgotten(
                f"job {job_id} is not held: it either never ran here or was "
                f"one of the {self._evicted} finished jobs forgotten to keep "
                f"history at {self._history}"
            )
        return KeyError(job_id)

    def job(self, job_id):
        """The snapshot for one job.

        Raises `KeyError` for an id that never ran here, and `JobForgotten`
        (itself a `KeyError`) once eviction has begun and the id may have
        been dropped — see `JobForgotten` for why the runner cannot be
        certain which, and why buying that certainty would cost the bound
        this eviction exists to hold.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise self._missing(job_id)
            return state.snapshot()

    def jobs(self):
        """Every job the runner still holds, oldest start first, as a
        `JobHistory` — a list that also carries how many finished jobs have
        been evicted, so a truncated history cannot be mistaken for the
        whole of what has ever run."""
        with self._lock:
            return JobHistory(
                (state.snapshot() for state in self._jobs.values()),
                evicted=self._evicted,
            )

    def wait(self, job_id, timeout=None):
        """Block until the job finishes (or `timeout` elapses), for tests.
        Never sleeps to poll — it waits on the same `threading.Event` the
        worker thread sets when it is done.

        Raises for an id the runner does not hold, on the same terms as
        `job()`. An evicted job's `Event` goes with the rest of its record,
        and returning `False` for one — the answer that means "still
        running" — would be a lie about a job that finished long ago.
        """
        with self._lock:
            done = self._done.get(job_id)
            if done is None:
                raise self._missing(job_id)
        return done.wait(timeout)

    def close(self, timeout=None):
        """Stop accepting work and wait for what is already running.

        Returns `True` if every in-flight job finished, `False` if the
        timeout expired with work still running. This is **not**
        cancellation: nothing interrupts a producer, and nothing is asked
        to stop early. It is the wait that was never specified — a service
        restarting on deploy wants to know when it is safe to go.

        **A bool, not an exception, for the timeout.** The caller that most
        needs this calls it from a `finally` or a signal handler, and an
        exception raised there replaces whatever was already unwinding —
        the deploy would lose the original failure and gain a shutdown
        one. The two answers are genuinely different and do lead to
        different actions, which is why it is not `None`: `True` means the
        process can be killed with nothing in flight; `False` means work is
        still running, and the caller decides between waiting longer and
        accepting the consequence below. `jobs()` still answers afterwards,
        so "which ones" is one call away.

        **What happens to a job still running when the timeout expires.**
        Nothing, here: it is a daemon thread and it keeps going, so the job
        finishes normally if the process lives long enough — a second
        `close()` will wait for it. But daemon is exactly what it sounds
        like at process exit: the interpreter does not wait, and the worker
        is killed wherever it happens to be. Because the runner records
        each proposal as it is yielded, that costs at most the proposal in
        flight; what is lost is the rest of the scan and the job's own
        record of how it ended, which live only in memory. A `False` return
        is therefore a real decision to make, not a warning to log.

        **Idempotent, and re-entrant across calls.** Called twice — the
        signal handler and the `finally` both firing is the normal case,
        not the exceptional one — the second call is not an error and does
        not replay the first answer: it waits again on whatever is still
        running and reports what it finds. So a `False` followed later by a
        `True` is the ordinary shape of "give it a bit longer".

        The store is left open. The runner was handed one it did not
        create and does not own; a caller still has to read the proposals
        these jobs recorded, and closing it here would break that. Closing
        it is the owner's to do, after this returns.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            # The flag and the list of jobs to wait for are taken in one
            # acquisition. Any `start()` that reserved a slot before this
            # point is in `_jobs` and therefore waited for; any that arrives
            # after is refused. There is no third case, which is what makes
            # the answer below mean anything.
            self._closed = True
            pending = [self._done[job_id]
                       for job_id, state in self._jobs.items()
                       if state.state == "running"]
        drained = True
        for done in pending:
            # A single deadline across all of them, not `timeout` each: the
            # caller asked to wait this long in total, and a per-job timeout
            # would multiply it by however many jobs happen to be running.
            remaining = (None if deadline is None
                         else max(0.0, deadline - time.monotonic()))
            # Every event is still checked once the deadline has passed —
            # `wait(0)` returns immediately with the event's real state — so
            # the answer reflects all of the jobs rather than stopping at
            # the first one that was not finished.
            if not done.wait(remaining):
                drained = False
        return drained
