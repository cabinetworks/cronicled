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
        """
        fp = fingerprint(folder, subject_type, subject_id, payload)
        when = now if now is not None else _utcnow()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False)
        with self._lock:
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

    def items(self):
        """Every proposal currently in the store, as dicts with `payload`
        decoded back into the Python object that was originally recorded."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT fingerprint, folder, subject_type, subject_id, "
                "summary, confidence, payload, producer, state, "
                "prior_state, error, created_at, last_seen_at, resolved_at "
                "FROM item"
            )
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result
