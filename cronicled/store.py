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
"""
import hashlib
import json
import sqlite3
import threading
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
"""


def fingerprint(folder, subject_type, subject_id, payload):
    """Stable identity for a proposal.

    Canonical JSON — sorted keys, fixed separators — so a payload built in a
    different key order is the same proposal rather than a second one. Every
    stored fingerprint depends on this serialisation: changing it silently
    invalidates them all, and the symptom is duplicate rows after an upgrade
    rather than an error. Nothing else may serialise a payload for hashing.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    joined = "\x1f".join([folder, subject_type, str(subject_id), canonical])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    """SQLite-backed store of proposals, safe to share across threads.

    A background job runner writes while the interface reads, both against
    the same connection, so the connection is opened with
    `check_same_thread=False` and every call is serialised through a lock
    the store owns — callers never think about concurrency themselves.
    WAL mode lets those reads and writes proceed without blocking each other
    on the filesystem any more than the lock already requires.
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        with self._lock:
            self._conn.close()

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
        """
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

    def mark_failed(self, fp, error, now=None):
        """Record that applying a proposal failed, and why."""
        when = now if now is not None else _utcnow()
        self._set_state(fp, {
            "state": "failed",
            "error": error,
            "resolved_at": when,
        })

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
        """
        when = now if now is not None else _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO dismissal (fingerprint, reason, at) "
                "VALUES (?, ?, ?)",
                (fp, reason, when),
            )
            self._conn.execute(
                "UPDATE item SET state = 'dismissed', resolved_at = ? "
                "WHERE fingerprint = ?",
                (when, fp),
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
        """
        when = now if now is not None else _utcnow()
        subject_id = str(subject_id)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO mute "
                "(subject_type, subject_id, reason, at) VALUES (?, ?, ?, ?)",
                (subject_type, subject_id, reason, when),
            )
            self._conn.execute(
                "UPDATE item SET state = 'muted', resolved_at = ? "
                "WHERE subject_type = ? AND subject_id = ?",
                (when, subject_type, subject_id),
            )
            self._conn.commit()

    _HIDDEN_STATES = ("dismissed", "muted")

    def items(self, folder=None, state=None, limit=None, offset=0):
        """Proposals in the store, as dicts with `payload` (and
        `prior_state`, when present) decoded back into the Python object
        that was originally recorded.

        Optionally filtered by `folder` and/or `state`, and paginated with
        `limit`/`offset`. With no `state` given, `dismissed` and `muted`
        rows are excluded — the inbox stays clean of a reviewer's own
        rejections. Ask for them explicitly with `items(state="dismissed")`
        or `items(state="muted")`.
        """
        query = (
            "SELECT fingerprint, folder, subject_type, subject_id, "
            "summary, confidence, payload, producer, state, "
            "prior_state, error, created_at, last_seen_at, resolved_at "
            "FROM item"
        )
        clauses = []
        params = []
        if folder is not None:
            clauses.append("folder = ?")
            params.append(folder)
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
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            item["payload"] = json.loads(item["payload"])
            if item["prior_state"] is not None:
                item["prior_state"] = json.loads(item["prior_state"])
            result.append(item)
        return result

    def counts(self, folder=None):
        """Number of proposals in each state, optionally scoped to a folder.

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
        query += " GROUP BY state"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {state: n for state, n in rows}
