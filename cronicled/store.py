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

CREATE TABLE IF NOT EXISTS producer_run (
    producer TEXT PRIMARY KEY,
    at       TEXT NOT NULL);

-- A standing, transient record of "examined and refused" -- see the block
-- above `record_refusal` for why this is its own table, keyed by subject,
-- rather than a row in `item`.
CREATE TABLE IF NOT EXISTS refusal (
    subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
    path TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
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
# That covers *additive* change only. Altering or dropping something that
# already holds data is a different problem and this offers nothing for it —
# there is no version column here, and the first change of that kind has to
# add one. The store's spec deferred migrations because there was no user data
# to preserve; there is now, so the deferral survives only for as long as
# every change stays additive.


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
                self._conn.executescript(SCHEMA)
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
        `reason` and `at` — the same `mute` table `muted_subjects()` reads,
        but keeping the reason and timestamp that method deliberately leaves
        out (it exists for the batch membership test `select()` needs, not
        for display — see its own docstring). This is for showing a person
        what is currently hidden from them, and a bare `(type, id)` pair
        gives them nothing to judge.

        Ordered by `at` then `subject_id`, the same tie-break `items()` uses
        for its own listing.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_type, subject_id, reason, at FROM mute "
                "ORDER BY at, subject_id"
            ).fetchall()
        return [
            {"subject_type": subject_type, "subject_id": subject_id,
             "reason": reason, "at": at}
            for subject_type, subject_id, reason, at in rows
        ]

    _HIDDEN_STATES = ("dismissed", "muted")

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

    def record_refusal(self, subject_type, subject_id, path, reason, now=None):
        """Upsert the standing refusal for `subject_type`/`subject_id`.

        `path` and `reason` are overwritten on every call, same as `at`: only
        the most recent examination's verdict is worth keeping, not a history
        of every night a file stayed unresolved. `path` is carried so the
        Refused section can name the file without a second lookup against the
        media server at render time — see `cronicled.web.rows.to_refusal_row`.
        """
        subject_id = str(subject_id)
        when = now if now is not None else _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO refusal (subject_type, subject_id, path, "
                "reason, at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(subject_type, subject_id) DO UPDATE SET "
                "path = excluded.path, reason = excluded.reason, "
                "at = excluded.at",
                (subject_type, subject_id, path, reason, when),
            )
            self._conn.commit()

    def refusals(self):
        """Every standing refusal, as dicts with `subject_type`,
        `subject_id`, `path`, `reason` and `at`.

        Ordered by `at` then `subject_id`, the same tie-break `items()` and
        `mutes()` use for their own listings.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_type, subject_id, path, reason, at "
                "FROM refusal ORDER BY at, subject_id"
            ).fetchall()
        return [
            {"subject_type": subject_type, "subject_id": subject_id,
             "path": path, "reason": reason, "at": at}
            for subject_type, subject_id, path, reason, at in rows
        ]

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
