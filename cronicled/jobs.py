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
an edge case.

`ctx` gives a producer exactly one thing: `log(message)`. It does not get the
store. Persistence, and the dismissal/mute rules that make a reviewer's past
decisions stick, belong to the runner alone — a producer that wrote to the
store directly could bypass them, and could not be tested without a runner
and a store to go with it.
"""
import threading
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
    message update mid-write relative to each other."""

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
        """
        if producer.cost not in COST_CLASS_LIMITS:
            raise ValueError(
                f"unknown cost class {producer.cost!r} for producer "
                f"{producer.name!r} (known: {sorted(COST_CLASS_LIMITS)})"
            )
        self._producers[producer.name] = producer

    def start(self, name):
        producer = self._producers[name]  # KeyError on an unknown producer
        job_id = str(uuid.uuid4())
        state = _JobState(job_id, producer.name, producer.cost)
        done = threading.Event()
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
            running[job_id] = producer.name
            self._jobs[job_id] = state
            self._done[job_id] = done
            snapshot = state.snapshot()
        thread = threading.Thread(
            target=self._run, args=(producer, state, done), daemon=True
        )
        thread.start()
        return snapshot

    def _run(self, producer, state, done):
        """The whole producer run, wrapped so nothing it raises escapes this
        thread. An exception in a background thread does not fail anything
        visible to a caller — it prints to stderr and the thread quietly
        dies — so without this a broken producer would leave its job stuck
        in `running` forever with no record of why.
        """
        try:
            ctx = _JobContext(lambda message: self._log(state, message))
            for proposal in producer.produce(ctx):
                self._record(state, proposal, producer.name)
        except Exception as exc:
            with self._lock:
                state.state = "failed"
                state.error = str(exc)
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
