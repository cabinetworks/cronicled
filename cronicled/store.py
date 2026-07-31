"""Durable store for proposed changes.

Everything the service finds today dies with the process. This store is the
keystone the job runner, scheduler, folder shell and rules engine all read
and write through, so a proposal survives a restart instead of evaporating
with it.

The store never interprets a payload — it stores JSON and hashes it. The
moment it needs to understand a payload's shape, every new producer becomes
a schema migration here. `payload` goes in as an opaque JSON value and comes
back out as the same Python object, nothing more.

A nightly producer runs against the same subjects every night. Without a
stable identity for "this proposal", its second run duplicates its first and
the inbox becomes noise instead of a queue. `fingerprint` is that identity;
see its docstring for how canonical serialisation makes it stable across
producers that happen to build a payload's keys in a different order.

The store also remembers when each producer last ran, for the scheduler that
decides what is due. Same reason as the proposals: an answer that resets on
restart makes every producer due at once. See the block above `record_run`.
"""
import hashlib
import json
import os
import sqlite3
import threading
import unicodedata
import uuid
import weakref
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS item (
    fingerprint   TEXT PRIMARY KEY,
    folder        TEXT NOT NULL,
    subject_type  TEXT NOT NULL,
    subject_id    TEXT NOT NULL,
    summary       TEXT NOT NULL,
    confidence    REAL,
    payload       TEXT NOT NULL,
    producer      TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'new',
    prior_state   TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS item_folder_state ON item(folder, state);
CREATE INDEX IF NOT EXISTS item_subject ON item(subject_type, subject_id);

CREATE TABLE IF NOT EXISTS dismissal (
    fingerprint TEXT PRIMARY KEY, reason TEXT, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS mute (
    subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
    reason TEXT, at TEXT NOT NULL,
    PRIMARY KEY (subject_type, subject_id));

-- A durable record that a proposal has been superseded -- see the block
-- above `Store.supersede` for why this is its own table, keyed by
-- fingerprint, rather than a row in `dismissal`.
CREATE TABLE IF NOT EXISTS supersede (
    fingerprint TEXT PRIMARY KEY, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS producer_run (
    producer TEXT PRIMARY KEY,
    at       TEXT NOT NULL);

-- One row per run, never upserted. `producer_run` above answers "how long ago
-- did this last run" for the scheduler and is deliberately still an upsert;
-- this answers "what has happened", which needs a history. Two tables because
-- the two questions have different lifetimes: the scheduler's answer must
-- never be evicted, and this one is bounded (see `RUN_HISTORY_LIMIT`).
--
-- `counts` is a JSON object rather than a column per counter: what a job
-- reports will change as it grows, and adding a counter must not be a schema
-- change. The fixed facts -- job, trigger, start, finish, outcome -- stay
-- columns, because they are what every row has and what a reader filters and
-- orders by.
--
-- Ordered by `started` and then by `rowid`, never by `started` alone.
-- Timestamps here have one-second resolution (`_utcnow`), so two runs of one
-- job started inside the same second carry an IDENTICAL `started` and that
-- column cannot say which came second. `rowid` is insertion order, which is
-- exactly the missing fact; without it both the newest-first read and the
-- retention eviction pick among ties arbitrarily, and SQLite's arbitrary
-- happens to be oldest-first -- the wrong end for both.
CREATE TABLE IF NOT EXISTS run (
    id       TEXT PRIMARY KEY,
    job      TEXT NOT NULL,
    trigger  TEXT NOT NULL,
    started  TEXT NOT NULL,
    finished TEXT,
    outcome  TEXT,
    counts   TEXT NOT NULL DEFAULT '{}',
    error    TEXT);

-- On `started` alone: SQLite will not index `rowid` (it is the table's own
-- key, not a column), so the tiebreak above is a sort the reader pays for
-- rather than an index it reads. At `RUN_HISTORY_LIMIT` rows that is nothing,
-- and the alternative is an ordering that is wrong whenever two runs share a
-- second.
CREATE INDEX IF NOT EXISTS run_started ON run (started DESC);

-- How many `run` rows retention has dropped. ONE row, incremented rather than
-- recomputed: the rows it counts are gone, so nothing can recount them. One
-- row per eviction would answer the same question while replacing one
-- unbounded table with another, so the increment is an upsert on a fixed key
-- (see `finish_run`) and no row is seeded here -- a database that has never
-- evicted anything has no row, and `runs_evicted` reads that as the zero it
-- is.
CREATE TABLE IF NOT EXISTS run_evicted (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    n  INTEGER NOT NULL);

-- A standing, transient record of "examined and refused" -- see the block
-- above `record_refusal` for why this is its own table, keyed by subject,
-- rather than a row in `item`.
--
-- `stores` is the JSON list of what every searched store returned for this
-- subject -- see `record_refusal` for its shape and for why a score has to
-- be a value here rather than a number spelled inside `reason`. Listed in
-- `_ADDED_COLUMNS` too, which is what gives a database written before this
-- column the column rather than a missing one.
CREATE TABLE IF NOT EXISTS refusal (
    subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
    path TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
    stores TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (subject_type, subject_id));

-- A durable record that a subject is no longer on the media server -- see the
-- block above `Store.mark_gone` for why the `item` state cannot carry this on
-- its own, and why nothing here is ever deleted.
CREATE TABLE IF NOT EXISTS gone (
    subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (subject_type, subject_id));
"""

# On adding to this schema, given databases already exist
# ------------------------------------------------------
# The whole script is re-applied on every open and every statement in it is
# `IF NOT EXISTS`, so a database written before a table existed gains it and
# keeps everything already in it. `producer_run` was added that way and it was
# checked rather than assumed: a database built by the previous code, then
# opened by this one, came back with its item, dismissal and mute rows
# identical, its dismissals and mutes still blocking, and
# `PRAGMA integrity_check` reporting ok. `SchemaAdditionOnAnExistingDatabase`
# in the tests pins it.
#
# A new COLUMN on a table that already exists is the one additive change
# `IF NOT EXISTS` does NOT carry: the `CREATE TABLE` is skipped whole, so the
# column named in it never appears on a database that already has the table.
# `_ADDED_COLUMNS` below is the list of those, applied by
# `_add_missing_columns` on every open.
#
# Both of those are still *additive* only. Altering or dropping something that
# already holds data is a different problem, and this schema offers nothing
# for it on its own — see `SCHEMA_VERSION` below for the marker that at least
# lets it be detected.

# Columns added to a table after that table had already shipped, as
# `(table, column, definition)`.
#
# Every definition here MUST carry a non-NULL default. It is what SQLite
# requires to add a `NOT NULL` column to a table that already holds rows, and
# it is also what makes the addition genuinely additive in both directions:
# rows written before the column read back as the default rather than as NULL,
# and code written before the column keeps inserting without naming it.
#
# The addition is decided by asking the table what columns it HAS
# (`PRAGMA table_info`), never by branching on `SCHEMA_VERSION`. A version
# stamp records what a build believed; `table_info` records what the file
# actually is, and only the second can be wrong in a way that is visible here.
_ADDED_COLUMNS = (
    ("refusal", "stores", "TEXT NOT NULL DEFAULT '[]'"),
)


def _add_missing_columns(conn):
    """Give `conn`'s tables every column in `_ADDED_COLUMNS` they are missing.

    Idempotent, and cheap enough to run on every open: a database that
    already has them does one `PRAGMA table_info` per entry and no writes.
    """
    for table, column, definition in _ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(
            "PRAGMA table_info(%s)" % (table,))}
        if column not in present:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                         % (table, column, definition))

# The shape's version, stamped into the database file itself via SQLite's
# built-in `PRAGMA user_version` (an integer the engine reserves for
# application use and never touches itself).
#
# This is a MARKER, not a migration mechanism — nothing here knows how to
# carry a version 1 database forward to a version 2 shape. What it buys is
# narrower and still worth having: code opening a database can now tell
# whether the shape it is looking at is the one it understands, instead of
# guessing from `sqlite_master` or assuming.
#
# Every database in existence today — including ones written by code before
# this constant existed — has an unset `user_version`, which SQLite reports
# as 0. That is indistinguishable from "brand new, empty file", and
# deliberately treated the same way: `Store.__init__` stamps a 0 straight to
# `SCHEMA_VERSION` on open, because right now every such database, new or
# old, has this exact shape — the same fact `SchemaAdditionOnAnExistingDatabase`
# checks for `producer_run`. That is what makes stamping safe to do now,
# before any non-additive change exists: it costs nothing today and gives the
# first such change something to branch on, rather than starting that change
# from the same PRAGMA user_version = 0 every database has always had.
#
# A `user_version` that is neither 0 nor `SCHEMA_VERSION` means this code does
# not recognise the shape it opened — a newer version from code run after a
# migration this build has never heard of, or an older one left mid-upgrade.
# `Store.__init__` refuses to open in that case rather than run the current
# schema and queries against a shape it cannot vouch for: a dismissal or mute
# is a person's standing decision, re-applying rules built for the wrong
# shape risks reading or writing it wrong in a way nothing would ever surface
# — a refusal that stops the process is recoverable (fix the file, or the
# code, and try again); a silent misread of a mute is not.
#
# NOT bumped by an additive change, and `refusal.stores` is the first one to
# test that. Bumping would make every build that predates the column refuse to
# open a database that has it — a real cost — and buy nothing in return: an
# added column with a default is invisible to code that does not name it
# (every statement here lists its columns), rows written before it read back
# as the default, and `_add_missing_columns` needs no version to know whether
# to act because `PRAGMA table_info` already tells it. What this marker is for
# is the change that CANNOT be carried that way — altering or dropping
# something already holding data — which is the case the comment above
# describes and which still does not exist.
SCHEMA_VERSION = 1

# The state an `item` row carries once its subject is no longer on the media
# server. Named here rather than spelled in each of the places that read it
# (`Store._HIDDEN_STATES`, the entry point's own section, `web.actions.undo`)
# so a fourth reader cannot join by copying the string.
#
# A row in this state is HIDDEN from the working lists and kept whole:
# `payload`, `prior_state` and `resolved_at` are all left exactly as they
# were, so what a proposal said, what it wrote and when survive the subject
# it was about disappearing. See `Store.mark_gone`.
GONE = "gone"

# How many `run` rows to keep. Bounded because a nightly pass writes a row a
# night forever and nothing else would ever drop one; NAMED, and paired with
# `Store.runs_evicted`, because silent truncation reads as "this is
# everything" when it is not -- a reader who cannot see that the list was cut
# concludes the missing runs never happened, which is the opposite of what
# this log exists to tell them.
RUN_HISTORY_LIMIT = 500

# How a run began. Stored rather than inferred, because the same job runs both
# ways and a reader asking "did last night's pass run" means the scheduled one,
# not the button somebody pressed at noon. A closed set, checked on the way in:
# see `Store.start_run`.
RUN_TRIGGERS = ("scheduled", "manual")


class SchemaVersionError(RuntimeError):
    """Raised by `Store.__init__` when a database's `PRAGMA user_version`
    is neither 0 (unstamped — treated as this same, current shape) nor
    `SCHEMA_VERSION` (already stamped and matching).

    Either reading means this build does not know the shape it opened: a
    version ahead of what this code understands (opened by something newer,
    or a migration this build predates), or a version behind it with no
    migration here to carry it forward (there is none yet — see
    `SCHEMA_VERSION`'s comment). Refusing to open is the only honest answer
    to either: this store's whole point is that a reviewer's dismissals and
    mutes durably outrank a producer's repetition, and silently running
    today's rules against a shape they were not written for is exactly the
    kind of misread that would never announce itself.
    """


def _nfc(value):
    """Recursively normalise every string in a JSON-compatible value to NFC.

    A filesystem commonly hands back a decomposed form (e.g. "e" + combining
    acute) while a title from an API is composed (a single "é" codepoint).
    Those are the same human-identical string, and without this they'd hash
    to two different fingerprints — silently duplicating a proposal on the
    exact grounds `fingerprint` exists to prevent. Dict keys are normalised
    too, for the same reason. Non-string scalars (int, float, bool, None)
    pass through unchanged: normalising *values* doesn't mean coercing
    *types* — see the note on `1` vs `1.0` below.
    """
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    return value


def fingerprint(folder, subject_type, subject_id, payload):
    """Stable identity for a proposal.

    Canonical JSON — sorted keys, fixed separators — so a payload built in a
    different key order is the same proposal rather than a second one. Every
    stored fingerprint depends on this serialisation: changing it silently
    invalidates them all, and the symptom is duplicate rows after an upgrade
    rather than an error. Nothing else may serialise a payload for hashing.

    Every string involved — `folder`, `subject_type`, `subject_id`, and any
    string anywhere inside `payload` — is normalised to Unicode NFC first
    (see `_nfc`), so a composed and a decomposed spelling of the same title
    are one proposal, not two.

    Numeric type is deliberately NOT coerced: `1` and `1.0` remain distinct
    fingerprints. Collapsing them would mean guessing they represent the
    same logical value, which is exactly the kind of interpretation this
    store refuses to do with an opaque payload.
    """
    normalized_payload = _nfc(payload)
    canonical = json.dumps(normalized_payload, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    joined = "\x1f".join([
        unicodedata.normalize("NFC", folder),
        unicodedata.normalize("NFC", subject_type),
        unicodedata.normalize("NFC", str(subject_id)),
        canonical,
    ])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_open_paths = set()
_open_paths_lock = threading.Lock()


def _release_path(path):
    """Remove `path` from the open-path registry. Idempotent by nature
    (`set.discard` is a no-op if the path isn't there), which is what lets
    `close()` and a `weakref.finalize` callback share this without either
    caring whether the other already ran."""
    with _open_paths_lock:
        _open_paths.discard(path)


class Store:
    """SQLite-backed store of proposals, safe to share across threads.

    A background job runner writes while the interface reads, both against
    the same connection, so the connection is opened with
    `check_same_thread=False` and every call is serialised through a lock
    the store owns — callers never think about concurrency themselves.
    WAL mode lets those reads and writes proceed without blocking each other
    on the filesystem any more than the lock already requires.

    Exactly one `Store` may be open on a given database file at a time. The
    lock above is per-instance, not per-file: a second `Store` opened on the
    same path would get its own lock, so two handles could interleave — one
    handle's `dismiss` landing between another handle's dismissal-check and
    its `INSERT` in `record`, resurrecting a just-dismissed proposal with
    `state='new'`. Nothing today opens a second handle, but nothing should
    have to remember not to either, so `__init__` checks a process-wide
    registry of open paths and raises `RuntimeError` if the path is already
    open; `close()` (and the context manager) releases it so the same path
    can be reopened afterwards.

    The registry keys on `os.path.realpath`, not just `os.path.abspath`: a
    symlink pointing at an already-open database file is still the same
    file, and the guard exists precisely to stop two handles on one file,
    not two spellings of a path that happen to differ.

    A `Store` that is dropped without `close()` (an exception on some path
    that skips a `with` block, say) still must not lock its path forever —
    there would be no route back short of restarting the process. A
    `weakref.finalize` registered in `__init__` releases the path when the
    instance is garbage collected, sharing the same idempotent release
    function `close()` uses, so whichever of the two runs first "wins" and
    the other is a no-op rather than a double-release or an error.
    """

    def __init__(self, path):
        canonical_path = os.path.realpath(path)
        with _open_paths_lock:
            if canonical_path in _open_paths:
                raise RuntimeError(
                    f"Store is already open on {canonical_path!r} — only one "
                    "Store instance may hold a given database file at a time "
                    "(see the class docstring for why)."
                )
            _open_paths.add(canonical_path)
        try:
            self._path = canonical_path
            self._lock = threading.Lock()
            self._conn = sqlite3.connect(path, check_same_thread=False)
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                # Read the stamp BEFORE touching the file. A shape this build
                # does not recognise must be left exactly as it was found:
                # re-running the schema script against it is harmless (every
                # statement is `IF NOT EXISTS`), but `_add_missing_columns`
                # writes, and altering a table inside a shape nothing here can
                # vouch for is the one thing the refusal below exists to stop.
                found = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if found != 0 and found != SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"{canonical_path!r} is stamped schema version "
                        f"{found}, but this code understands version "
                        f"{SCHEMA_VERSION}. Refusing to open rather than "
                        f"run this version's rules against a shape it was "
                        f"not written for."
                    )
                self._conn.executescript(SCHEMA)
                # Tables the script created just now already carry every
                # column; tables that survived from an earlier open do not,
                # because `CREATE TABLE IF NOT EXISTS` skipped them whole.
                _add_missing_columns(self._conn)
                self._conn.commit()
                if found == 0:
                    # Unstamped — either brand new, or written before this
                    # marker existed. Both are this exact shape today (see
                    # `SCHEMA_VERSION`'s comment), so stamping is safe and
                    # costs nothing; it is what gives the first genuinely
                    # non-additive change something to compare against
                    # instead of the same unstamped 0 every database has
                    # always had.
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self._conn.commit()
            self._finalizer = weakref.finalize(self, _release_path, canonical_path)
        except Exception:
            _release_path(canonical_path)
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        with self._lock:
            self._conn.close()
        # Calling the finalizer explicitly runs its callback now (releasing
        # the path) and marks it "dead", so if this instance is later
        # garbage collected the same callback does NOT fire again.
        self._finalizer()

    def record(self, folder, subject_type, subject_id, summary, payload,
               producer, confidence=None, now=None):
        """Insert a proposal, or touch an existing one with the same
        fingerprint rather than duplicate it. Returns the fingerprint.

        This is the property the whole design rests on: a nightly producer
        that finds the same thing again must UPDATE a row, not create a
        second one, or the inbox turns into noise on its second night.

        Before inserting, this checks whether a reviewer has already said
        "no" to it — either this exact proposal (`dismissal`, by
        fingerprint) or anything about this subject (`mute`, by
        subject_type/subject_id). If either matches, `record` returns the
        fingerprint without storing anything: a reviewer's decision outranks
        a producer's repetition, and a dismissed or muted proposal must not
        resurrect itself just because the producer offered it again.

        On an existing fingerprint, the conflict clause updates
        `last_seen_at` and nothing else — not state, not summary, not
        confidence. Freshening those columns would let a producer's rerun
        quietly overwrite a reviewer's decision (`seen`, `dismissed`,
        `muted`) or their context; resist that urge here.

        `confidence`, unlike `payload`, is not opaque — the store has a
        documented contract for it (0 to 1 inclusive, or `None`) and
        enforces it here, raising `ValueError` on anything outside that
        range rather than storing a nonsensical score.
        """
        if confidence is not None and not (0 <= confidence <= 1):
            raise ValueError(
                f"confidence must be between 0 and 1 (or None), got {confidence!r}"
            )
        fp = fingerprint(folder, subject_type, subject_id, payload)
        when = now if now is not None else _utcnow()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False)
        with self._lock:
            dismissed = self._conn.execute(
                "SELECT 1 FROM dismissal WHERE fingerprint = ?", (fp,)
            ).fetchone()
            muted = self._conn.execute(
                "SELECT 1 FROM mute WHERE subject_type = ? AND subject_id = ?",
                (subject_type, str(subject_id)),
            ).fetchone()
            if dismissed or muted:
                return fp
            # A refusal is transient: it is true only until scoring, the
            # catalogue, or the file itself changes enough to clear the
            # threshold. The moment THIS subject produces a real proposal --
            # right here -- whatever refusal was standing for it is stale, so
            # it is cleared in the same transaction as the insert rather than
            # left to confuse a reviewer reading the Refused section next to
            # a proposal that already resolved it. See `record_refusal`'s
            # docstring for why a refusal is keyed by subject at all.
            self._conn.execute(
                "DELETE FROM refusal WHERE subject_type = ? AND subject_id = ?",
                (subject_type, str(subject_id)),
            )
            self._conn.execute(
                """
                INSERT INTO item (fingerprint, folder, subject_type,
                    subject_id, summary, confidence, payload, producer,
                    created_at, last_seen_at)
                VALUES (:fp, :folder, :subject_type, :subject_id, :summary,
                    :confidence, :payload, :producer, :when, :when)
                ON CONFLICT(fingerprint) DO UPDATE SET last_seen_at = :when
                """,
                {
                    "fp": fp,
                    "folder": folder,
                    "subject_type": subject_type,
                    "subject_id": str(subject_id),
                    "summary": summary,
                    "confidence": confidence,
                    "payload": encoded,
                    "producer": producer,
                    "when": when,
                },
            )
            self._conn.commit()
        return fp

    def _set_state(self, fp, fields):
        """Update `item` columns for a known fingerprint, or raise `KeyError`.

        Shared by the three `mark_*` transitions: silently doing nothing on
        an unknown fingerprint would hide a real bug in the caller, so this
        checks the row exists before (and inside the same lock as) writing.
        """
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        params = dict(fields)
        params["fp"] = fp
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE item SET {assignments} WHERE fingerprint = :fp",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(fp)
            self._conn.commit()

    def mark_seen(self, fp, now=None):
        """Record that a human has looked at this proposal."""
        self._set_state(fp, {"state": "seen"})

    def mark_applied(self, fp, prior_state=None, now=None):
        """Record that a proposal was applied, with an undo snapshot and a
        resolution time."""
        when = now if now is not None else _utcnow()
        encoded = (json.dumps(prior_state, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False)
                   if prior_state is not None else None)
        self._set_state(fp, {
            "state": "applied",
            "prior_state": encoded,
            "resolved_at": when,
        })

    def mark_reverted(self, fp, now=None):
        """Record that an applied proposal was undone.

        Without this an undo left NO trace: the revert happened on the media
        server, the row stayed `applied`, and the page went on offering an
        Undo button. A person clicked it, it worked, the page redrew
        identically, and the only reasonable conclusion was that it had not
        worked. A working action that looks broken is worse than one that
        fails loudly.

        Every other decision a reviewer makes here is durable -- a dismissal
        and a mute both survive re-recording, specifically so a later scan
        cannot overrule a person. An undo is a decision of the same kind and
        was the only one leaving no record. It also makes "how often is an
        approve taken back?" answerable, which is the most direct evidence
        available about whether the threshold is right.

        `prior_state` is deliberately KEPT rather than cleared. It is the
        snapshot that was restored, and the only record of what the scene
        looked like before the apply; discarding it at the moment it was used
        would throw away the audit trail for the write it just reversed.
        Keeping it cannot re-trigger anything, because offering Undo requires
        the `applied` state -- see `web.rows.to_row`.

        A reverted row is NOT closed. The proposal is exactly as live as it
        was before it was applied, so it can be approved again (undoing by
        mistake is ordinary), dismissed, or muted. What it is not is
        `applied`, and that is what stops the Undo button reappearing.
        """
        self._set_state(fp, {
            "state": "reverted",
            "resolved_at": now if now is not None else _utcnow(),
        })

    def mark_failed(self, fp, error, now=None):
        """Record that applying a proposal failed, and why."""
        when = now if now is not None else _utcnow()
        self._set_state(fp, {
            "state": "failed",
            "error": error,
            "resolved_at": when,
        })

    _TERMINAL_STATES = ("applied", "failed")

    def dismiss(self, fp, reason=None, now=None):
        """Reject THIS proposal by fingerprint. A better proposal for the
        same subject may still arrive tomorrow and is not affected.

        `dismissed` is a state, not a deletion: the row (summary, payload,
        confidence and all) stays in `item` so a reviewer can see what they
        rejected and why, it's just excluded from the default `items()`/
        `counts()` view. The `dismissal` table is the durable record that
        makes the rejection stick — it is never pruned, so re-recording the
        same fingerprint can never resurrect it, with or without a matching
        `item` row.

        If the row is already in a terminal state (`applied` or `failed`),
        this does NOT move it to `dismissed` — `applied`/`failed` plus their
        `resolved_at` record that a real change already happened (or was
        attempted) and when; overwriting that with `dismissed` would erase
        the fact and time of that change while the file itself stays
        modified. The dismissal is still recorded in the `dismissal` table
        (so it still blocks a future re-record of the same fingerprint) —
        only the row's own `state`/`resolved_at` are left alone.

        This protection is deliberately narrow: only `applied` and `failed`
        are terminal *resolutions* of "was the change actually made". A
        `muted` row has no such fact to protect — nothing happened to the
        file — so `dismiss` is free to move a `muted` row to `dismissed`
        (and, symmetrically, `mute` is free to move a `dismissed` row to
        `muted`). That is intended, not an oversight.

        Unlike `mark_seen`/`mark_applied`/`mark_failed`, calling this on a
        fingerprint with no matching `item` row is not an error: dismissal
        (like mute) is allowed pre-emptively, before the row exists, mirroring
        `mute`'s ability to block a subject that has never had a proposal.
        The three `mark_*` transitions describe something that happened to
        an already-recorded proposal, so an unknown fingerprint there is a
        caller bug; `dismiss`/`mute` describe a standing rejection that may
        outlive — or precede — any particular row, so there's nothing to
        require existing first.
        """
        when = now if now is not None else _utcnow()
        placeholders = ", ".join("?" for _ in self._TERMINAL_STATES)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO dismissal (fingerprint, reason, at) "
                "VALUES (?, ?, ?)",
                (fp, reason, when),
            )
            self._conn.execute(
                f"""
                UPDATE item SET
                    state = CASE WHEN state IN ({placeholders})
                                 THEN state ELSE 'dismissed' END,
                    resolved_at = CASE WHEN state IN ({placeholders})
                                       THEN resolved_at ELSE ? END
                WHERE fingerprint = ?
                """,
                (*self._TERMINAL_STATES, *self._TERMINAL_STATES, when, fp),
            )
            self._conn.commit()

    def undismiss(self, fp):
        """Reverse `dismiss` for exactly this fingerprint — a person changing
        their own mind, never a producer overruling them: the only caller is
        `cronicled.web.actions.Actions.undismiss`, reached from a person's own
        click, and `record()`'s re-run path is never the one that clears this.

        Two things happen together, in the same transaction:

        1. The `dismissal` row is deleted — that table is the durable block
           `record()` checks by fingerprint, so this is what makes the
           fingerprint eligible to be recorded again.
        2. If (and only if) the `item` row for `fp` is currently sitting in
           `state = 'dismissed'`, it is moved back to `'new'`. Without this,
           the row would stay excluded from `items()`'s default view even
           after its block was lifted — the whole point of "reversible" is
           that the proposal comes back into the inbox, not merely that a
           future re-record is no longer refused.

        The `state = 'dismissed'` condition on the UPDATE is load-bearing,
        not decorative: `dismiss` is free to move a `muted` row to
        `dismissed` (see its own docstring), so a fingerprint dismissed and
        THEN separately muted is sitting in `state = 'muted'` by the time
        this runs. Reversing the dismissal must not silently clear that
        separate, still-active mute — the `mute` table is untouched here,
        and the condition stops the UPDATE from moving a muted row anywhere.
        `unmute` below preserves the same separation in the other direction:
        it never touches the `dismissal` table or an item's state at all.

        `new` rather than whatever state preceded the dismissal, because
        nothing here — or in `dismiss` itself — records what that was; there
        is nothing truer to restore it to, the same gap `mute`/`unmute` have.

        Calling this on a fingerprint that was never dismissed, or whose row
        has since moved to `applied`/`failed`, is not an error: like
        `dismiss` itself, a standing rejection may not correspond to any row
        at all, or the row it named may have moved on since — see
        `dismiss`'s own docstring for why an unmatched fingerprint there is
        not a caller mistake either.
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM dismissal WHERE fingerprint = ?", (fp,))
            self._conn.execute(
                "UPDATE item SET state = 'new' "
                "WHERE fingerprint = ? AND state = 'dismissed'",
                (fp,),
            )
            self._conn.commit()

    def mute(self, subject_type, subject_id, reason=None, now=None):
        """Reject ANYTHING about a subject — for a subject that will never
        be identifiable. Outlives any single proposal.

        Like `dismiss`, `muted` is a state on any existing `item` row(s) for
        the subject, not a deletion. The `mute` table is what actually does
        the blocking in `record()`, keyed by `(subject_type, subject_id)`
        rather than by fingerprint — so it works even for a subject with no
        `item` row yet, which is the whole point of muting ahead of a
        proposal ever arriving.

        Same terminal-state protection as `dismiss`: any row already
        `applied` or `failed` keeps its own `state` and `resolved_at` — the
        subject-level mute still gets recorded in the `mute` table and still
        blocks future proposals for it, it just doesn't overwrite the record
        of a real change that already happened to this particular row.

        Also like `dismiss`, calling this for a subject with no `item` row
        at all is not an error — see `dismiss`'s docstring for why.
        """
        when = now if now is not None else _utcnow()
        subject_id = str(subject_id)
        placeholders = ", ".join("?" for _ in self._TERMINAL_STATES)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO mute "
                "(subject_type, subject_id, reason, at) VALUES (?, ?, ?, ?)",
                (subject_type, subject_id, reason, when),
            )
            self._conn.execute(
                f"""
                UPDATE item SET
                    state = CASE WHEN state IN ({placeholders})
                                 THEN state ELSE 'muted' END,
                    resolved_at = CASE WHEN state IN ({placeholders})
                                       THEN resolved_at ELSE ? END
                WHERE subject_type = ? AND subject_id = ?
                """,
                (*self._TERMINAL_STATES, *self._TERMINAL_STATES, when,
                 subject_type, subject_id),
            )
            self._conn.commit()

    def unmute(self, subject_type, subject_id):
        """Reverse `mute`: remove the standing block on `subject_type`/
        `subject_id`, so a future `record()`/`select()` is free to consider
        it again. A person changing their own mind, never a scan overruling
        one — the only caller is `cronicled.web.actions.Actions.unmute`,
        reached from a person's own click, never from `ScanProducer`.

        Removes the standing block AND brings back the `item` row(s) `mute`
        hid, by returning any still sitting in `state = 'muted'` to `new`.

        Both halves are needed, and doing only the first was tried: the block
        lifted, `items()` still filtered the row out as `muted`, and a person
        clicking Unmute saw the page redraw completely unchanged. That is the
        same failure an undo had -- an action that works and looks broken --
        and it is worse than the fault it replaced, because a control that
        appears to do nothing gets pressed again.

        `new` rather than a remembered previous state, because none is
        stored, and because `new` is what the state means here: this
        proposal is waiting for a decision. The alternative readings are
        `seen` (claims a person looked at it, which nothing knows) and
        leaving it hidden (the bug above).

        A row that reached a TERMINAL state keeps it. `mute` deliberately
        leaves `applied` and `failed` alone -- muting a subject does not
        un-apply a write that already happened -- so there is nothing for
        this to restore, and forcing such a row to `new` would offer a fresh
        Approve for a proposal already written to the library.

        Never touches the `dismissal` table -- see `undismiss`'s docstring
        for the same separation held the other way. A mute and a dismissal
        are different rejections and reversing one must not quietly reverse
        the other.

        Deliberately triggers no lookup and no scan of its own.

        Calling this for a subject that is not currently muted is not an
        error, mirroring `mute`'s own tolerance of a subject with no `item`
        row at all: "already not blocked" is an ordinary outcome here, not a
        caller mistake to catch.
        """
        subject_id = str(subject_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM mute WHERE subject_type = ? AND subject_id = ?",
                (subject_type, subject_id),
            )
            # Only rows this mute actually hid. `state = 'muted'` is the
            # exact condition `mute` wrote and `items()` filters on, so this
            # cannot disturb a row that reached `applied` or `failed` before
            # the mute was applied.
            #
            # A row DISMISSED before it was muted goes back to `dismissed`,
            # not to `new`. `mute` overwrites the state, so the dismissal is
            # no longer legible from the row itself -- only the `dismissal`
            # table still remembers it. Restoring everything to `new` was
            # tried and resurrected a proposal the person had already
            # rejected: reversing one rejection must not quietly reverse the
            # other, which is the separation `undismiss` holds from the
            # opposite side.
            self._conn.execute(
                "UPDATE item SET state = CASE"
                "   WHEN EXISTS (SELECT 1 FROM dismissal d"
                "                WHERE d.fingerprint = item.fingerprint)"
                "   THEN 'dismissed' ELSE 'new' END "
                "WHERE subject_type = ? AND subject_id = ? AND state = 'muted'",
                (subject_type, subject_id),
            )
            self._conn.commit()

    def mutes(self):
        """Every standing mute, as dicts with `subject_type`, `subject_id`,
        `reason`, `at` and `item` — the same `mute` table `muted_subjects()`
        reads, but keeping the reason and timestamp that method deliberately
        leaves out (it exists for the batch membership test `select()` needs,
        not for display — see its own docstring). This is for showing a person
        what is currently hidden from them, and a bare `(type, id)` pair
        gives them nothing to judge.

        `item` is the WHOLE most recently seen `item` row ever recorded for
        this subject, in exactly the shape and with exactly the decoding
        `items()` returns (they share `_ITEM_COLUMNS` and `_decode_item`) —
        or `None` when no proposal was ever recorded for this subject at
        all. That second case is the one `mute`'s own docstring names: a
        subject muted ahead of any scan ever finding it. `mute` itself never
        deletes an existing `item` row (it only changes its `state`, and only
        for a non-terminal one — see `mute`'s docstring), so the row recorded
        against whichever proposal was seen last is still sitting in the
        `item` table regardless of what state it ended up in, terminal or
        not; a subject can also carry more than one `item` row (successive
        proposals under different producers or payloads before it was muted),
        so this reads the freshest by `last_seen_at` rather than picking
        arbitrarily.

        THE WHOLE ROW, not the payload alone, and that is what lets a muted
        subject be shown with everything a dismissed one is shown with:
        `cronicled.web.rows.to_mute_row` builds the identical row builders
        the Dismissed section uses, and those need the fingerprint, the
        state and the subject type as well as the payload. Handing over the
        payload and letting the row builder invent the rest would be
        inventing store state on a page whose controls write to a library.

        Deciding what to show a person for a subject with no recoverable
        item is `to_mute_row`'s job, not this method's — this only ever
        answers what the store actually knows, honestly.

        Ordered by `at` then `subject_id`, the same tie-break `items()` uses
        for its own listing.

        A subject marked GONE is left out entirely -- see `mark_gone`. A mute
        is an instruction to stop offering a file, and a file the server no
        longer holds is not on offer; the Unmute this section draws for it
        would lift a block on a subject nothing can propose, which reads as a
        control that does nothing. The `mute` row itself is untouched and the
        reason survives, exactly as it does for a subject that is still there.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_type, subject_id, reason, at FROM mute "
                "WHERE NOT EXISTS (SELECT 1 FROM gone g "
                "                  WHERE g.subject_type = mute.subject_type "
                "                    AND g.subject_id = mute.subject_id) "
                "ORDER BY at, subject_id"
            ).fetchall()
            result = []
            for subject_type, subject_id, reason, at in rows:
                cursor = self._conn.execute(
                    "SELECT %s FROM item WHERE subject_type = ? AND "
                    "subject_id = ? ORDER BY last_seen_at DESC LIMIT 1"
                    % self._ITEM_COLUMNS, (subject_type, subject_id))
                columns = [d[0] for d in cursor.description]
                item_row = cursor.fetchone()
                item = (None if item_row is None
                        else self._decode_item(columns, item_row))
                result.append({"subject_type": subject_type,
                              "subject_id": subject_id, "reason": reason,
                              "at": at, "item": item})
        return result

    # Superseding a proposal
    # ----------------------
    # A proposal made by an older version of this tool can carry only what an
    # early producer offered -- a title and a URL, none of a fuller scrape's
    # detail -- and `select()` skips a file whose subject already has a
    # visible proposal BEFORE examining it, so that thin proposal blocks its
    # file from ever being looked at again. Dismissing frees a `new` row (see
    # `dismiss`'s docstring for why hiding it is what does that), but an
    # `applied` or `failed` row keeps its state and its `resolved_at` by
    # design, and goes on blocking forever.
    #
    # Superseding is deliberately NOT a dismissal. A person superseding a
    # proposal is not saying it was wrong -- they are saying it is out of
    # date, and a `dismiss` recorded on their behalf would put a rejection in
    # the store they never made, plus block this exact fingerprint (via the
    # `dismissal` table `record()` checks) from ever being written again,
    # which is nonsensical for a payload nothing was ever proposed to be wrong
    # about. That is why this is its OWN table, keyed by fingerprint like
    # `dismissal` is, but never consulted by `record()` -- nothing here stops
    # the identical payload being proposed again, because there was never
    # anything wrong with it to begin with.
    def supersede(self, fp, now=None):
        """Retire the proposal named by `fp` and free its subject for the
        next scan to examine again.

        Unlike `dismiss`/`mute`, this describes something happening to an
        ALREADY-RECORDED proposal, never a standing rejection that might
        precede one -- there is no "pre-emptive supersede" the way there is a
        pre-emptive mute, so an unknown fingerprint is a caller mistake and
        raises `KeyError`, the same contract `mark_seen`/`mark_applied`/
        `mark_failed` already hold (see `_set_state`).

        Two things happen, together:

        1. `fp` is recorded in the `supersede` table. This is the actual
           mechanism that frees the subject -- `scan.select` reads it
           directly, alongside `store.items()`, rather than trusting `state`
           alone (see the point below for why `state` is not enough on its
           own).
        2. The `item` row's own `state`/`resolved_at` are updated to
           `superseded` -- UNLESS the row is already in one of
           `_TERMINAL_STATES` (`applied`/`failed`), in which case they are
           left exactly as they are. This is the same CASE `dismiss`/`mute`
           already use to protect a terminal resolution, applied here for the
           same reason: `applied` plus its `resolved_at` records that a real
           write already happened and when, and `failed` plus its
           `resolved_at` records that one was attempted and when -- either
           overwritten would erase that fact. It is also why point 1 cannot
           be dropped in favour of just checking `state`: an `applied` row
           whose `state` never changes still has to stop blocking
           `scan.select`, and the only place that is recorded is this table.

        The `dismissal` and `mute` tables are untouched -- superseding is
        neither. And `prior_state` -- an applied row's undo snapshot -- is
        untouched too: superseding only ever touches `state` and
        `resolved_at`, never the column an Undo depends on, so an applied
        row stays exactly as undoable after being superseded as before.
        """
        when = now if now is not None else _utcnow()
        placeholders = ", ".join("?" for _ in self._TERMINAL_STATES)
        with self._lock:
            cursor = self._conn.execute(
                f"""
                UPDATE item SET
                    state = CASE WHEN state IN ({placeholders})
                                 THEN state ELSE 'superseded' END,
                    resolved_at = CASE WHEN state IN ({placeholders})
                                       THEN resolved_at ELSE ? END
                WHERE fingerprint = ?
                """,
                (*self._TERMINAL_STATES, *self._TERMINAL_STATES, when, fp),
            )
            if cursor.rowcount == 0:
                raise KeyError(fp)
            self._conn.execute(
                "INSERT OR REPLACE INTO supersede (fingerprint, at) "
                "VALUES (?, ?)",
                (fp, when),
            )
            self._conn.commit()

    def superseded_fingerprints(self):
        """Every fingerprint ever superseded, as a set.

        The membership test `scan.select` needs to free a subject whose
        visible proposal has been superseded -- a set, rather than a
        per-fingerprint `is_superseded`, for the same reason
        `muted_subjects()` answers a set: selection asks about every
        candidate file's existing proposal at once, and one round trip per
        file is the cost this exists to avoid.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT fingerprint FROM supersede").fetchall()
        return {fp for (fp,) in rows}

    # `superseded` joins `dismissed`/`muted` here for the same reason both of
    # those are hidden: a superseded proposal has already been retired by a
    # person's own action and is no longer outstanding work. See `supersede`
    # above for why a row can carry this state at all, and why `applied`/
    # `failed` rows never do -- their subject is freed through the separate
    # `supersede` TABLE instead, precisely because their own `state` is left
    # untouched.
    #
    # `gone` joins them too, and for the sharpest version of the same reason:
    # a subject the media server no longer holds cannot be acted on at all, so
    # every control a working list offers for it would write to an id that is
    # not there. It is still in the table, and `items(state=GONE)` is how the
    # entry point draws it (see `mark_gone`).
    _HIDDEN_STATES = ("dismissed", "muted", "superseded", GONE)

    def has(self, fp):
        """Whether `fp` names a row currently in a visible state — i.e. one
        `items()`'s default view would return: not `dismissed`, not `muted`.

        A single primary-key lookup, unlike scanning `items()` for the same
        fingerprint: the caller (the job runner, telling `recorded` from
        `skipped`) only ever needs a yes/no answer for one fingerprint, not
        every row in a folder.
        """
        placeholders = ", ".join("?" for _ in self._HIDDEN_STATES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT 1 FROM item WHERE fingerprint = ? "
                f"AND state NOT IN ({placeholders})",
                (fp, *self._HIDDEN_STATES),
            ).fetchone()
        return row is not None

    def muted_subjects(self):
        """Every muted subject, as a set of `(subject_type, subject_id)`.

        This reads the `mute` table, NOT the `item` rows a mute moved into the
        `muted` state, and that distinction is the reason the method exists.
        `mute` deliberately accepts a subject that has never had a proposal
        (see its docstring), and such a subject has no row — so
        `items(state="muted")` reports it as unmuted while `record()` goes on
        refusing proposals for it. Anything asking "would the store refuse
        this?" must ask the same table `record()` asks.

        The set form, rather than a per-subject `is_muted`, because the caller
        this exists for is a scan choosing a batch: it asks about every
        candidate at once, and N round trips to answer one question per file
        is the cost the batch is trying to avoid. A caller with a single
        subject can test membership; a caller with a batch cannot cheaply
        undo N queries.

        `record()` deliberately does NOT route through this. Its check must
        happen inside the same lock as its INSERT, or a `mute` landing in
        between would be checked-then-ignored; calling a method that takes
        and releases the lock on its own would open exactly that window.

        `subject_id` comes back as the string `mute` stored it as, so a caller
        holding ids from an API can compare `str(id)` and get a match.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_type, subject_id FROM mute").fetchall()
        return {(subject_type, subject_id) for subject_type, subject_id in rows}

    # Recording a refusal
    # -------------------
    # `scan.examine` returns a refusal (a tie, or nothing over the threshold)
    # for a file that is genuinely one glance, or one threshold change, away
    # from being resolved — see `scan.Outcome`'s docstring. Before this, that
    # verdict was logged and thrown away, so a person had no way to see what
    # was refused short of reading the job log.
    #
    # A refusal is NOT recorded in `item`. `record()`'s fingerprint hashes the
    # payload, and a refusal's natural payload (scores, runners-up) can change
    # between runs even when nothing about the file changed — `scan.py`
    # already documents this exact hazard for a proposal's own payload. Reusing
    # `item` for a refusal would mean a nightly re-examination of the same
    # unresolved file recording a FRESH row every night rather than touching
    # one, which is precisely the noise `record()`'s fingerprint exists to
    # prevent for a proposal. Keyed by subject instead — the same key `mute`
    # uses — an upsert keeps exactly one row per refused subject no matter how
    # many nights it stays refused, so growth here is bounded by how many
    # DISTINCT subjects a library ever refuses, the same bound the `mute`
    # table already accepts, not by how many scans have run.
    #
    # A refusal is also transient in a way a mute is not: it stops being true
    # the moment scoring, the catalogue, or the file itself changes enough to
    # clear the threshold. `record()` clears the row for a subject the moment
    # THAT subject produces a real proposal — see the comment there — so a
    # resolved refusal does not linger next to the proposal that resolved it.

    def record_refusal(self, subject_type, subject_id, path, reason,
                       stores=(), now=None):
        """Upsert the standing refusal for `subject_type`/`subject_id`.

        `path`, `reason` and `stores` are overwritten on every call, same as
        `at`: only the most recent examination's verdict is worth keeping, not
        a history of every night a file stayed unresolved. `path` is carried
        so the Refused section can name the file without a second lookup
        against the media server at render time — see
        `cronicled.web.rows.to_refusal_row`.

        `stores` is what EVERY store searched returned for this subject, one
        entry each, built by `cronicled.scan._store_reports` and stored as
        JSON exactly the way `record()` stores a payload — opaque here, never
        interpreted. It exists because a refusal used to keep one prose
        sentence naming one store, which reads as though the others were
        never consulted, and because the score in that sentence could only be
        recovered by parsing it back out of the text. Nothing about a stored
        score may require reading English.

        The order of the entries is the CALLER's, and it is content-ordered
        rather than iteration-ordered — see `_store_reports`. Nothing here
        re-sorts them: a second ordering rule in the store would be free to
        disagree with the one that has the scores in front of it.
        """
        subject_id = str(subject_id)
        when = now if now is not None else _utcnow()
        encoded = json.dumps(list(stores), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO refusal (subject_type, subject_id, path, "
                "reason, at, stores) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(subject_type, subject_id) DO UPDATE SET "
                "path = excluded.path, reason = excluded.reason, "
                "at = excluded.at, stores = excluded.stores",
                (subject_type, subject_id, path, reason, when, encoded),
            )
            self._conn.commit()

    def refusals(self):
        """Every standing refusal, as dicts with `subject_type`,
        `subject_id`, `path`, `reason`, `at` and `stores` — the last decoded
        back into the list of dicts that was recorded, the same way `items()`
        decodes a payload.

        Ordered by `at` then `subject_id`, the same tie-break `items()` and
        `mutes()` use for their own listings.

        A subject marked GONE is left out, for the reason `mutes()` leaves one
        out: a refusal is a standing "a person should look at this file", and
        there is no longer a file to look at. The `refusal` row is untouched --
        nothing here deletes what was decided.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_type, subject_id, path, reason, at, stores "
                "FROM refusal "
                "WHERE NOT EXISTS (SELECT 1 FROM gone g "
                "                  WHERE g.subject_type = refusal.subject_type "
                "                    AND g.subject_id = refusal.subject_id) "
                "ORDER BY at, subject_id"
            ).fetchall()
        return [
            {"subject_type": subject_type, "subject_id": subject_id,
             "path": path, "reason": reason, "at": at,
             "stores": json.loads(stores)}
            for subject_type, subject_id, path, reason, at, stores in rows
        ]

    # Marking a subject the media server no longer holds
    # ---------------------------------------------------
    # A deleted scene leaves every decision ever made about it behind, and each
    # of those decisions is drawn with controls that would write to an id the
    # server does not have. Approving one fails, confusingly, because the row
    # still reads as though the file were there.
    #
    # MARKED, NEVER DELETED, and that is the whole shape of this. Every other
    # rejection here is a state rather than a deletion so the reason survives
    # (see `dismiss`), and this one has a second argument on top: a deletion is
    # not always permanent -- a library rebuilt can hold the same content again
    # -- and nothing removed from this side is recoverable from it.
    #
    # It takes BOTH a table and a state, for the reason `supersede` needs both.
    # The state is what hides an `item` row from the working lists; the table is
    # the only place a subject with no `item` row at all can be recorded, and a
    # muted or refused subject is exactly that -- `mute` and `record_refusal`
    # are both keyed by subject and both accept one that has never had a
    # proposal. A state alone would leave `mutes()` and `refusals()` with
    # nothing to filter on.
    #
    # What this does NOT do is prune the `supersede` or `dismissal` tables. Both
    # are keyed by fingerprint and neither is ever pruned by anything (see
    # `dismiss`); a superseded or dismissed subject reaches its section through
    # the `item` row's own state, which this overwrites, so it is hidden either
    # way. The rows left behind are what keeps a re-record blocked and a
    # subject unblocked exactly as they were.

    def mark_gone(self, subject_type, subject_id, now=None):
        """Record that `subject_type`/`subject_id` is no longer on the media
        server, and hide every `item` row about it.

        Returns whether this is the FIRST time the subject was recorded gone,
        so a caller can report how many subjects a sweep newly marked rather
        than how many it re-confirmed. Calling it again is not an error and
        changes nothing -- the recorded `at` stays the moment it was first
        noticed, which is the honest answer to "when did this go", not the
        moment of the most recent sweep.

        The `item` rows are moved to `GONE` UNCONDITIONALLY -- including the
        `applied` and `failed` ones the terminal-state protection in
        `dismiss`/`mute`/`supersede` deliberately leaves alone. Those exist to
        stop a later decision erasing the record of a real write; this is not a
        later decision about the write, it is the file the write was made to
        going away, and a row still offering Undo for it would offer an undo
        that cannot work. What made the protection matter is kept regardless:
        `prior_state` (the undo snapshot, and the only record of what was
        written) and `resolved_at` (when it was written) are both untouched --
        this writes `state` and nothing else. `web.actions.undo` is what turns
        the row's own honesty into a refusal a person can read.
        """
        subject_id = str(subject_id)
        when = now if now is not None else _utcnow()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO gone (subject_type, subject_id, at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(subject_type, subject_id) DO NOTHING",
                (subject_type, subject_id, when),
            )
            newly = cursor.rowcount == 1
            self._conn.execute(
                "UPDATE item SET state = ? "
                "WHERE subject_type = ? AND subject_id = ?",
                (GONE, subject_type, subject_id),
            )
            self._conn.commit()
        return newly

    def subject_ids(self, subject_type):
        """Every subject id of `subject_type` this store holds any decision
        about, as a set of strings.

        The input to "which of these does the media server still have". Read
        across every table that keys a decision by subject -- `item`, `mute`
        and `refusal` -- because each of them can hold one the others do not:
        `mute` and `record_refusal` both accept a subject with no `item` row,
        and a proposal exists for plenty of subjects nobody has muted.

        `dismissal` is keyed by FINGERPRINT and names no subject of its own, so
        it needs no term here: the only way a dismissal is reachable at all is
        through the `item` row it names (that is what `items(state="dismissed")`
        draws), and the `item` term already covers that row. The same is true of
        `supersede`.

        Subjects already marked gone are INCLUDED. This answers what the store
        holds, not what is new -- deciding that is `mark_gone`'s return value,
        which is the one place that can answer it without a race between the
        read and the write.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_id FROM item WHERE subject_type = ? "
                "UNION SELECT subject_id FROM mute WHERE subject_type = ? "
                "UNION SELECT subject_id FROM refusal WHERE subject_type = ?",
                (subject_type, subject_type, subject_type),
            ).fetchall()
        return {subject_id for (subject_id,) in rows}

    # Every column an `item` row is READ back as, in one place, because two
    # methods now return that row to the same consumers -- `items()` for the
    # inbox's own sections and `mutes()` for the muted subject behind a
    # standing mute. Two hand-maintained SELECT lists would be free to drift,
    # and the drift is silent in the expensive direction: the muted row is
    # built by the SAME row builders the dismissed row is, so a column
    # missing here draws a muted proposal with a blank field where the
    # dismissed one beside it shows a real value.
    _ITEM_COLUMNS = (
        "fingerprint, folder, subject_type, subject_id, summary, "
        "confidence, payload, producer, state, prior_state, error, "
        "created_at, last_seen_at, resolved_at"
    )

    @staticmethod
    def _decode_item(columns, row):
        """One `item` row -> the dict shape every caller of `items()` and
        `mutes()` already expects, with the two JSON columns decoded back
        into the Python objects that were recorded.

        Shared rather than written twice for the reason `_ITEM_COLUMNS`
        states: a second decoder would be free to hand one caller a JSON
        string where the other gets a dict, and a payload that reached a row
        builder as a string fails at the first index rather than anywhere a
        reader would look.
        """
        item = dict(zip(columns, row))
        item["payload"] = json.loads(item["payload"])
        if item["prior_state"] is not None:
            item["prior_state"] = json.loads(item["prior_state"])
        return item

    def items(self, folder=None, state=None, limit=None, offset=0,
              subject_types=None):
        """Proposals in the store, as dicts with `payload` (and
        `prior_state`, when present) decoded back into the Python object
        that was originally recorded.

        Optionally filtered by `folder` and/or `state`, and paginated with
        `limit`/`offset`. With no `state` given, `dismissed` and `muted`
        rows are excluded — the inbox stays clean of a reviewer's own
        rejections. Ask for them explicitly with `items(state="dismissed")`
        or `items(state="muted")`.

        `subject_types`, when given, narrows to rows whose `subject_type` is
        one of the tuple's members — this is how an inbox page (see
        `cronicled.web.inboxes`) asks for only the subject types it owns.
        Checked with `is not None`, not truthiness: an empty tuple is a
        real, distinct request ("select nothing"), not "no filter given".
        """
        query = "SELECT %s FROM item" % self._ITEM_COLUMNS
        clauses = []
        params = []
        if folder is not None:
            clauses.append("folder = ?")
            params.append(folder)
        if subject_types is not None:
            # `(subject_placeholders or "NULL")` reads like a typo but is
            # deliberate: an empty tuple joins to "", which would otherwise
            # emit `IN ()`. On the SQLite bundled with this project's pinned
            # interpreter that already matches no row rather than raising,
            # so this guard is not load-bearing here -- but `IN ()` was a
            # syntax error on SQLite before 3.31 (2020), and nothing pins
            # this project to a build newer than that. Kept for that
            # portability rather than relied on for this one's behaviour.
            subject_placeholders = ", ".join("?" for _ in subject_types)
            clauses.append(
                "subject_type IN (%s)" % (subject_placeholders or "NULL"))
            params.extend(subject_types)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        else:
            placeholders = ", ".join("?" for _ in self._HIDDEN_STATES)
            clauses.append(f"state NOT IN ({placeholders})")
            params.extend(self._HIDDEN_STATES)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, fingerprint"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(query, params)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
        return [self._decode_item(columns, row) for row in rows]

    def counts(self, folder=None, subject_types=None):
        """Number of proposals in each state, optionally scoped to a folder
        and/or to a tuple of subject types (see `items()`'s `subject_types`
        for the same `is not None` / empty-tuple reasoning).

        Excludes `dismissed` and `muted` rows, same as `items()`'s default —
        a badge counting a reviewer's own rejections as outstanding work
        would be wrong.
        """
        placeholders = ", ".join("?" for _ in self._HIDDEN_STATES)
        query = f"SELECT state, COUNT(*) FROM item WHERE state NOT IN ({placeholders})"
        params = list(self._HIDDEN_STATES)
        if folder is not None:
            query += " AND folder = ?"
            params.append(folder)
        if subject_types is not None:
            # See `items()`'s own `subject_types` clause for why the
            # `(... or "NULL")` guard is here.
            subject_placeholders = ", ".join("?" for _ in subject_types)
            query += " AND subject_type IN (%s)" % (subject_placeholders or "NULL")
            params.extend(subject_types)
        query += " GROUP BY state"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {state: n for state, n in rows}

    # When each producer last ran
    # ---------------------------
    # A scheduler decides what is due by comparing a producer's cadence against
    # when it last ran, so that answer has to outlive the process. Held in
    # memory it resets on every restart, which makes every producer due at once
    # — a nightly full-library scrape would run on every deploy, and a service
    # restarting a few times in an afternoon would scrape continuously. That is
    # the load the cost classes exist to prevent, arriving through a door they
    # do not watch.
    #
    # It lives here rather than in a state file of its own because this is
    # already the durable local state, already has a schema created on first
    # open, and already survives exactly as long as the proposals do.
    #
    # Like everything else on this class, each of the three takes `self._lock`
    # exactly once and calls nothing that takes it. `record()`'s note about
    # holding the lock across its mute check applies in reverse here: the lock
    # is a plain non-reentrant `threading.Lock`, so none of these may ever be
    # called from inside a block that already holds it.

    def record_run(self, producer, at=None):
        """Remember that `producer` has just run, replacing any previous record.

        A producer has *a* last run, not a history — the scheduler only ever
        asks "how long ago", and a growing row-per-run would be a log this
        store has no reason to keep. `producer` is the primary key and the
        write is an upsert, so the table holds one row per producer however
        many times it runs.

        `at` is stored exactly as given and is not compared with what is
        already there. Keeping whichever timestamp is larger would mean
        interpreting it, and would quietly ignore an operator correcting a run
        stamped by a skewed clock. Omitted, it is the current UTC time in the
        same format as every other timestamp here.

        This is recorded for a failed run as much as a successful one. Backing
        off after a failure is a real feature and deliberately not this; a
        producer that fell silent for a day after one transient error would be
        a worse outcome than one that retries on its normal cadence.
        """
        when = at if at is not None else _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO producer_run (producer, at) VALUES (?, ?) "
                "ON CONFLICT(producer) DO UPDATE SET at = excluded.at",
                (producer, when),
            )
            self._conn.commit()

    def last_run(self, producer):
        """When `producer` last ran, or `None` if it never has.

        `None` rather than an error: a producer that has never run is the
        ordinary state of one just added to the schedule, and the caller's
        answer to it — run it now — is a normal decision, not a failure.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT at FROM producer_run WHERE producer = ?",
                (producer,),
            ).fetchone()
        return row[0] if row is not None else None

    def runs(self):
        """Every producer's last run, as `{producer: at}`, in one query.

        A tick asks about every producer it knows before deciding what to
        start, so the read is shaped for that rather than for N calls to
        `last_run`.

        A producer that has never run is simply absent — no key, not a key
        mapped to `None`. The caller iterating this is comparing timestamps,
        and a `None` sitting among them would be a value every comparison has
        to remember to exclude.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT producer, at FROM producer_run").fetchall()
        return {producer: at for producer, at in rows}

    # What has happened, one row per run
    # ----------------------------------
    # The three above answer the scheduler's question -- "how long ago did this
    # last run" -- and are deliberately an upsert, one row per producer. This
    # answers a person's question -- "did last night's pass run, and what did
    # it find" -- which no upsert can answer, because it needs the runs the
    # last one replaced.
    #
    # So: a second table, never upserted, bounded by `RUN_HISTORY_LIMIT` and
    # reporting what the bound dropped. The scheduler's answer is NOT bounded
    # and must never be evicted; this one is, and the two lifetimes are why
    # these are not one table with a flag.
    #
    # Same lock discipline as everything else here: each takes `self._lock`
    # exactly once and calls nothing that takes it.

    def start_run(self, job, *, trigger, at=None):
        """Open a run row for `job` and return its id.

        `trigger` says how the run began -- one of `RUN_TRIGGERS`. Checked
        here rather than trusted, because an unrecognised trigger would be
        stored happily and then read back by the summary as though it meant
        something; a value nobody can interpret is worse than a refusal at the
        one point that still knows which call site produced it.

        The id is a fresh UUID per call, which is what makes this a history:
        an id derived from the job would collide on the second run and the row
        would be replaced rather than added.

        `at` is stored exactly as given, for the same reason `record_run` does
        not compare it with what is already there. Omitted, it is the current
        UTC time in the same format as every other timestamp here.
        """
        if trigger not in RUN_TRIGGERS:
            raise ValueError(
                "trigger must be one of %s, not %r -- an unrecognised trigger "
                "would be stored and then read back as though it meant "
                "something." % (", ".join(repr(t) for t in RUN_TRIGGERS),
                                trigger))
        run_id = str(uuid.uuid4())
        when = at if at is not None else _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO run (id, job, trigger, started) "
                "VALUES (?, ?, ?, ?)",
                (run_id, job, trigger, when),
            )
            self._conn.commit()
        return run_id

    def finish_run(self, run_id, *, outcome, counts=None, error=None, at=None):
        """Close the run row `run_id`, then evict beyond the retention bound.

        A FAILED run is closed here exactly as a completed one is. "Did last
        night's scan run?" is the question this log exists to answer, and a
        log of successes answers the opposite one -- it makes a job that has
        been failing every night for a week indistinguishable from one that
        was never scheduled.

        `counts` defaults to an empty object, not to SQL NULL: `recent_runs`
        hands back whatever is here, and a caller that has to test for `None`
        before every lookup will one day not.

        Eviction happens here rather than on a timer because this is the only
        moment a row is added to the finished history, so it is the only
        moment the bound can be exceeded. What it drops is counted, never
        merely dropped -- see `runs_evicted`.
        """
        when = at if at is not None else _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE run SET finished = ?, outcome = ?, counts = ?, "
                "error = ? WHERE id = ?",
                (when, outcome, json.dumps(counts or {}), error, run_id),
            )
            # `NOT IN (the newest N)` rather than a cutoff timestamp: the
            # bound is a row count, and a cutoff would keep however many rows
            # happened to share the boundary second.
            #
            # Only a FINISHED row is a candidate, and only finished rows count
            # towards the bound. An unfinished run is by definition still
            # happening, and a log that drops the run currently in progress
            # answers "what has happened" with the one thing that has not.
            # Without this, a job open long enough for `RUN_HISTORY_LIMIT`
            # others to complete has its row deleted before it finishes, and
            # the `UPDATE` above then matches nothing and closes nothing --
            # silently, because this sits in the worker's `finally` where
            # raising would replace the producer's own exception with a store
            # error. The case is removed rather than reported.
            #
            # Both halves matter. Filtering only the DELETE would leave open
            # rows consuming the bound, so a backlog of open rows larger than
            # the bound would evict every finished row to make room for runs
            # that have not produced an answer yet.
            #
            # The residual: an open row whose process died is never evicted by
            # this, because nothing here can tell it from one still working.
            # `start()` closes the rows it opens on every path where no worker
            # exists, which is what keeps the ordinary refusal from leaking
            # one; a killed process leaves one behind per interrupted job.
            dropped = self._conn.execute(
                "DELETE FROM run WHERE finished IS NOT NULL AND id NOT IN ("
                "  SELECT id FROM run WHERE finished IS NOT NULL "
                "  ORDER BY started DESC, rowid DESC LIMIT ?)",
                (RUN_HISTORY_LIMIT,),
            ).rowcount
            if dropped:
                # Accumulated, never assigned: a later call must add to the
                # total rather than restate it.
                #
                # `dropped` rather than a literal 1, even though the exclusion
                # above now holds it to 0 or 1: each call makes at most one
                # more row finished, so it can put the table at most one row
                # over the bound. That is a consequence of where this is
                # called from, not a rule this statement enforces -- anything
                # that finishes several rows between two eviction passes (a
                # bulk close, a bound lowered against an existing database, a
                # migration back-filling `finished`) drops several at once,
                # and a literal would then undercount every one of them.
                self._conn.execute(
                    "INSERT INTO run_evicted (id, n) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET n = n + excluded.n",
                    (dropped,),
                )
            self._conn.commit()

    def recent_runs(self, limit=20):
        """The most recent runs, newest first, as a list of dicts.

        Each carries `id job trigger started finished outcome counts error`.
        A run that has been started and not yet finished is here too, with
        `finished`, `outcome` and `error` all `None` -- a job that is still
        going, or one whose process died mid-run, is a thing a reader needs to
        be able to see rather than an absence they have to infer.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, job, trigger, started, finished, outcome, counts, "
                "error FROM run ORDER BY started DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": r[0], "job": r[1], "trigger": r[2], "started": r[3],
                 "finished": r[4], "outcome": r[5], "counts": json.loads(r[6]),
                 "error": r[7]} for r in rows]

    def runs_evicted(self):
        """How many run rows retention has dropped, over the store's life.

        Reported rather than hidden, for the reason `JobHistory` reports its
        own evictions: a partial list presented as complete is a list that
        lies. Zero until something has actually been dropped -- the counter
        row is written by the first eviction, so `SUM` over no rows is the
        honest zero and not a missing answer.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(n), 0) FROM run_evicted").fetchone()
        return row[0]
