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
    running against it. Cost classes and their limits are wired up in a
    later task; this exception exists now because producers and callers are
    written against it from the start."""


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

    def register(self, producer):
        self._producers[producer.name] = producer

    def start(self, name):
        producer = self._producers[name]  # KeyError on an unknown producer
        job_id = str(uuid.uuid4())
        state = _JobState(job_id, producer.name, producer.cost)
        done = threading.Event()
        with self._lock:
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
        needs an actual check: `store.items()`, with no explicit `state`,
        already excludes `dismissed` and `muted` rows (that is its
        documented default). So after recording, this looks the fingerprint
        up in that same default view, scoped to the proposal's folder — if
        it is there, the store kept it (inserted or touched); if not, the
        store declined it, whether the row exists in a hidden state or
        never existed at all (a pre-emptive mute/dismissal blocks the insert
        entirely). Presence in the default view is exactly the "did a
        reviewer's own decision suppress this" signal a user needs to tell a
        quiet producer from a broken one.
        """
        folder = proposal["folder"]
        fp = self._store.record(
            folder=folder,
            subject_type=proposal["subject_type"],
            subject_id=proposal["subject_id"],
            summary=proposal["summary"],
            payload=proposal["payload"],
            producer=producer_name,
            confidence=proposal.get("confidence"),
        )
        kept = any(
            item["fingerprint"] == fp for item in self._store.items(folder=folder)
        )
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
