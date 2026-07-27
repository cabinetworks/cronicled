"""Choosing which files a library scan works on, working one of them, and
running a whole batch.

Three parts, deliberately separate. `select` picks the batch: no scoring, no
lookups, no threading happens there. `examine` works one file: one lookup,
one decision, and no threading either. `ScanProducer` is the only part that
schedules anything — it composes the other two across a bounded pool and
yields proposals as they complete — so a function that decides is never also
a function that schedules, and each is testable in its own terms.

Selection is deliberately separate from the work: no scoring, no lookups, no
threading happens here. What survives selection is a plain list of scenes and
a record of why everything else did not, which is small enough to reason
about and to test on its own.

The one rule the whole module exists for is an ORDER: **narrowing happens
before limiting.** A filter plus a limit yields `limit` files *matching the
filter*, not the first `limit` files overall — and the same for the two
narrowings the store supplies, files already proposed and subjects the
reviewer has muted. Both are dropped before the limit applies, so a second
run's budget goes to fresh work instead of re-deciding what was already
decided.

The reason that ordering matters more than it looks: the scarce resource is a
network lookup against a scraper, and the limit exists to ration it. A limit
spent on files that were never going to be proposed is a scan that appears to
run and achieves nothing.
"""
import posixpath
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from cronicled.artist import Aliases, creator_folder, resolve
from cronicled.scoring import DEFAULT_THRESHOLD, decide, score

# Selection deals in one kind of subject. It is named rather than inlined so
# a test and the store agree on the same string, and so a future second kind
# has to change this deliberately instead of by coincidence.
SUBJECT_TYPE = "scene"


@dataclass(frozen=True)
class Counts:
    """Why a scan's input became the batch it became.

    `total` is everything offered; `selected` is what will actually be worked;
    `already_proposed`, `muted` and `filtered_out` are the reasons a file was
    dropped, each file counted under exactly one of them (see `select` for the
    precedence); `deferred` is the files that survived every one of those
    narrowings and were then cut by the limit — nothing was decided against
    them, they simply did not fit in this budget and a later run may take them.

    `deferred` exists so that

        total == already_proposed + muted + filtered_out + selected + deferred

    holds ALWAYS, not just while the limit fails to bind. That identity is one
    assertion catching a file that vanished for a reason nobody named, which is
    a failure no per-field check can see — it can only look for the reasons it
    already knows about. Without the sixth field the identity lapses exactly
    when a scan is busiest, which is when a miscount matters most.
    """
    total: int
    already_proposed: int
    muted: int
    filtered_out: int
    selected: int
    deferred: int


def _paths(scene):
    """Every path a scene's files sit at, lowered for comparison.

    Read without a default, and read for every scene rather than only when a
    filter is present: a scene missing `files` (or a file missing `path`) is
    malformed, and a validation that only runs on the filtered path would let
    a broken payload through whenever nobody passed a filter.
    """
    return [f["path"].casefold() for f in scene["files"]]


def _subjects(rows):
    """The subject ids of `rows` that are scenes, as a set for lookup."""
    return {row["subject_id"] for row in rows
            if row["subject_type"] == SUBJECT_TYPE}


def select(scenes, *, store, folder, name_filter=None, limit=None):
    """The files a scan should work, and why the others were dropped.

    Returns `(selected, counts)`, `selected` being the surviving scene dicts
    in the order they were offered.

    Narrowing runs first, in this precedence — each dropped file is counted
    under exactly one reason:

    1. `name_filter`, matched case-insensitively as a substring of the whole
       PATH of any of the scene's files, not just of its name. That is
       deliberate and load-bearing: it lets a pattern naming an ancestor
       directory scope a scan to that whole subtree, which is the main way
       this filter gets used. The accepted cost is that a broad pattern (a
       directory near the root, or a common substring) matches a great many
       files, and since narrowing runs before the limit, those matches are
       what a small limit gets spent on — a too-broad filter does not fail,
       it quietly buys a batch the user did not have in mind. An empty or
       absent filter matches everything rather than nothing. The filter comes
       first because a file outside it is not in this scan's scope at all:
       reporting it as `muted` would describe a reviewer decision that never
       applied to this run.
    2. muted subjects — the reviewer has rejected anything about them, so the
       store would refuse a proposal for them anyway.
    3. subjects that already have a visible proposal in `folder`.

    Only then does `limit` take the first `limit` survivors. `limit=None`
    takes all of them; `limit=0` takes none, which is a distinct (and
    honoured) instruction, not a missing limit. Survivors the limit did not
    reach are counted as `deferred` rather than dropped — no reason was found
    against them, so a later run may take them.

    Scope note, mirroring what the store actually enforces: a mute is keyed by
    subject alone and blocks a proposal in any folder, so the muted check is
    not scoped to `folder`. An existing proposal is a row *in* a folder, so
    that check is.

    Both store questions are asked at the SUBJECT level, not at the
    fingerprint level via `store.has()`. That is forced, not preferred: a
    fingerprint covers a proposal's payload, and at selection time — before
    any scoring or lookup — there is no payload to hash. The question
    selection actually needs to ask is "has this file already been decided
    about", which is about the subject.

    The muted check reads `store.muted_subjects()` — the `mute` table — rather
    than the `item` rows a mute moved into the `muted` state. A subject muted
    PRE-EMPTIVELY, before any proposal for it existed, has no such row, so
    reading rows would miss it: the scan would spend a lookup on a file
    `record()` was always going to refuse. Asking the same table `record()`
    asks is what makes selection's answer agree with the store's.

    A dismissed proposal does NOT suppress its file here. Dismissal rejects
    one proposal, not the subject — a better proposal for the same file is
    allowed to arrive tomorrow, and it cannot if the file is never looked at.
    Muting is the mechanism for "stop offering me this file".
    """
    if limit is not None and limit < 0:
        # `scenes[:-1]` would quietly drop the LAST file rather than select
        # nothing, so a negative limit must not reach a slice.
        raise ValueError(f"limit must not be negative, got {limit!r}")

    scenes = list(scenes)
    pattern = (name_filter or "").casefold()
    muted = {subject_id for subject_type, subject_id in store.muted_subjects()
             if subject_type == SUBJECT_TYPE}
    proposed = _subjects(store.items(folder=folder))

    narrowed = []
    filtered_out = muted_count = already_proposed = 0
    for scene in scenes:
        subject_id = str(scene["id"])
        paths = _paths(scene)
        if pattern and not any(pattern in path for path in paths):
            filtered_out += 1
        elif subject_id in muted:
            muted_count += 1
        elif subject_id in proposed:
            already_proposed += 1
        else:
            narrowed.append(scene)

    selected = narrowed if limit is None else narrowed[:limit]
    return selected, Counts(
        total=len(scenes),
        already_proposed=already_proposed,
        muted=muted_count,
        filtered_out=filtered_out,
        selected=len(selected),
        # Measured against `narrowed`, not `scenes`: a file dropped by a
        # narrowing already has a reason, and counting it here as well would
        # double-count it and break the identity `deferred` exists to hold.
        deferred=len(narrowed) - len(selected),
    )


# --- Working one file ------------------------------------------------------

# How many losing candidates a proposal records. Three is enough for a
# reviewer to see what else was in the running, and a cap is what stops a
# prolific creator's whole catalogue being copied into every one of their
# files' payloads — which the store then hashes, per file, per run.
MAX_RUNNERS_UP = 3

# The two reasons — and the ONLY two reasons — a file is muted. Both mean the
# same thing operationally ("stop offering me this file") and different
# things to whoever reads them later: one says the catalogue had nothing to
# offer for a creator we did identify, the other says nothing in the library's
# own layout named a creator at all. Only the second is fixed by an alias.
#
# They are named constants rather than literals so a caller, a test and a
# stored mute reason cannot drift apart, and so that collapsing them into one
# catch-all string has to be done deliberately instead of by a copy-paste.
MUTE_NO_CANDIDATES = (
    "no candidates: the catalogue offered nothing for this creator")
MUTE_UNRESOLVED_CREATOR = (
    "creator unresolved: neither the folder nor the filename names one")


@dataclass(frozen=True)
class Outcome:
    """What examining one file concluded.

    Exactly one of `proposal`, `mute_reason` and `error` is ever set, and the
    three-way split is the entire point of this type: there are three
    different kinds of "no proposal" and conflating any two of them is a bug
    a user feels.

    * `mute_reason` — the file is genuinely unidentifiable (no candidates, or
      no creator resolved). Muting stops it consuming a lookup on every
      future run, which is the budget the whole module rations.
    * neither set — the scoring refused: a tie, or nothing over the
      threshold. A human should look at this. Muting it would silently hide a
      file that is one glance, or one threshold change, from being resolved.
    * `error` — the lookup raised. That is evidence about the NETWORK, not
      about the file. Muting on error would hide a file permanently because
      a socket blipped once, and no later run would ever revisit it.

    `reason` is always set: one line saying which of the four happened, safe
    to log unconditionally. It duplicates `mute_reason`/`error` when those
    are set, and is the ONLY record in the refusal case — where nothing is
    proposed, nothing is muted, and without it the caller has nothing to tell
    a reviewer and no way to distinguish a tie from a near miss.
    """
    proposal: dict = None
    mute_reason: str = None
    error: str = None
    reason: str = ""


def _primary_path(scene):
    """The path `examine` judges the scene by.

    The first file, when a scene has several (a re-encode, a duplicate): they
    are the same work under different containers, so any of them attributes
    it, and picking one keeps a scene to a single lookup.

    A missing `files` key or a file missing `path` raises `KeyError`, as it
    does in `select` — a malformed scene is not an empty one. An empty
    `files` list raises `ValueError` for a sharper reason: such a scene has
    no name and no folder, every guard in the resolver would decline it, and
    it would come back as a MUTE. A malformed record must not quietly become
    "never show me this file again"; the producer isolates one file's
    exception, so raising costs that file's turn and nothing else.
    """
    files = scene["files"]
    if not files:
        raise ValueError(
            "scene %r has no file to identify it by" % (scene.get("id"),))
    return files[0]["path"]


def examine(scene, *, search, folder, threshold=DEFAULT_THRESHOLD, aliases=None):
    """Work out what `scene` is, and return what that concluded.

    `search` is the injected lookup: called with the resolved creator's name
    and expected to return that creator's catalogue as a list of dicts, each
    carrying at least a `title`. Nothing in this package executes a query, so
    the expensive networked half belongs to the caller — and no test here
    opens a socket. A caller running several files at once wraps `search` to
    collapse identical queries; that is the seam, and this function stays
    single-file and single-lookup.

    `folder` is the store's proposal namespace, not a directory on disk: the
    proposal returned is complete and can be yielded to the job runner
    unchanged. (The creator's own directory is read off the path, and is a
    different thing entirely — see `creator_folder`.)

    ORDER: the creator is resolved BEFORE anything is scored, and that is
    load-bearing rather than stylistic. `scoring.score(..., artist=)`
    subtracts the creator's tokens from the evidence, so scoring first would
    score against evidence that still contains the creator's name — and a
    file named after nobody but its creator would then match by containment
    on that name alone, taking on the metadata of whichever of the creator's
    titles happened to be offered. That is precisely the failure the
    zero-evidence rule in `scoring` exists to catch, and it can only see it
    if the artist reaches it.

    Only `search` is wrapped: a raising lookup is the transient failure this
    is built to survive. A malformed alias map, a scene with no file, a
    candidate with no title are all wiring or data mistakes that are wrong
    for every file, and they propagate rather than being reported as this
    one file's bad luck.
    """
    path = _primary_path(scene)
    name = posixpath.basename(path)
    # The creator's directory, derived from the same path string the name
    # comes from, rather than from the scene's own `basename` field: one
    # source of truth, so a payload whose two fields disagree cannot make the
    # attribution and the evidence describe different files.
    directory = creator_folder(path)

    resolution = resolve(name, directory, aliases)
    if resolution.name is None:
        return Outcome(mute_reason=MUTE_UNRESOLVED_CREATOR,
                       reason=MUTE_UNRESOLVED_CREATOR)

    try:
        candidates = list(search(resolution.name))
    except Exception as exc:
        # Name the type as well as the message: `str(exc)` alone is '' for a
        # bare `raise SomeError()`, which reports a failure indistinguishable
        # from a quiet success.
        error = "%s: %s" % (type(exc).__name__, exc)
        return Outcome(error=error, reason=error)

    # Asked of the candidate list directly rather than read off `decide`'s
    # refusal reason: "the catalogue had nothing" is the one refusal that is
    # a fact about the file rather than about a threshold, and it is the only
    # one that mutes. Inferring it from a reason string would tie a mute to
    # wording that is free to change.
    if not candidates:
        return Outcome(mute_reason=MUTE_NO_CANDIDATES,
                       reason=MUTE_NO_CANDIDATES)

    matches = [score(name, directory, c["title"], artist=resolution.name)
               for c in candidates]
    decision = decide(matches, threshold)
    if decision.match is None:
        # A tie, a near miss, or evidence that was nothing but the creator's
        # own name. All three are refusals a person can act on — by looking,
        # by moving the threshold, by renaming the file — so none of them
        # mutes. The reason carries what to act on.
        return Outcome(reason=decision.reason)

    winner = candidates[decision.index]
    payload = {
        "path": path,
        # The resolver's disagreements, which otherwise go nowhere. A folder
        # naming one creator while the filename names another is not a tie to
        # be broken quietly: it is evidence that the filing convention is not
        # what the operator assumed, and it is invisible unless a proposal
        # carries it to where somebody reads it.
        "creator": {
            "name": resolution.name,
            "source": resolution.source,
            "competing": resolution.competing,
            "rejected_folder": resolution.rejected_folder,
        },
        # The candidate whole, as the search returned it. This module cannot
        # know which of a store's fields an applier will need, and a
        # projection drops them silently. The cost, stated plainly: a search
        # returning a field that changes between runs (a view count, a signed
        # thumbnail URL) changes the payload, hence the fingerprint, hence
        # re-proposes the same file nightly. That failure is visible in the
        # inbox; a payload missing the field an apply needed is not.
        "candidate": winner,
        "score": decision.match.value,
        "runners_up": _runners_up(candidates, matches, decision.index),
    }
    return Outcome(
        proposal={
            "folder": folder,
            "subject_type": SUBJECT_TYPE,
            "subject_id": str(scene["id"]),
            # One line a reviewer can judge without opening the payload: which
            # file, which candidate, and how confident.
            "summary": '%s -> "%s" by %s (score %.3f)' % (
                name, winner["title"], resolution.name, decision.match.value),
            "confidence": decision.match.value,
            "payload": payload,
        },
        reason=decision.reason,
    )


def _runners_up(candidates, matches, winning_index):
    """The best of the losing candidates, highest first.

    Sorted by score and then by title, so the payload does not depend on the
    order the catalogue happened to return its results — two runs that get
    the same candidates in a different order are the same proposal, not two.
    Capped at MAX_RUNNERS_UP; see it for why.
    """
    losers = [(match.value, c["title"], c)
              for i, (c, match) in enumerate(zip(candidates, matches))
              if i != winning_index]
    losers.sort(key=lambda row: (-row[0], row[1]))
    return [{"candidate": c, "score": value}
            for value, _, c in losers[:MAX_RUNNERS_UP]]


# --- Running a batch -------------------------------------------------------


def _query_key(query):
    """The form in which two queries are the SAME query.

    Case and internal spacing only. Nothing cleverer: a key that stripped
    punctuation or dropped words would collapse two creators who are not the
    same person into one lookup, and the caller would never see it — every
    file of the second creator would be attributed from the first one's
    catalogue. Folding case and runs of whitespace is spelling; anything
    beyond that is a guess about identity, and this cache is not the place to
    make one.
    """
    return " ".join(query.split()).casefold()


class _Flight:
    """One query's slot in the cache: the answer, or the failure, plus the
    event every later caller waits on until one of the two exists."""

    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None


class _SingleFlight:
    """Wraps a search so identical queries issue ONE lookup per run.

    Single-flight, not a memo: a caller arriving while an identical query is
    still in flight waits on that flight rather than starting its own. The
    distinction is the whole point under a pool — a memo that only publishes
    an answer once it has arrived is empty for exactly as long as the lookup
    takes, which is precisely the window N workers resolving one creator
    arrive in. Without this, parallelism multiplies consumption of the
    network lookups the selection step exists to conserve.

    Failures are cached too, and re-raised to every later caller. A catalogue
    that is down is one fact about the run, not one fact per file, and
    retrying it once per file spends the whole budget rediscovering the same
    outage. The cost, stated: a failure that would have cleared mid-run is
    not retried until the next run. The reverse — a dead query retried by
    every worker — is the failure this exists to stop, and a run is the
    natural boundary because the cache does not outlive one.

    The cached exception object is re-raised as-is, so several threads may
    hold the same instance and its traceback accumulates frames. That is
    cosmetic; `examine` reads only the type and the message from it.

    One entry per distinct creator for the life of a run. That is bounded by
    the batch, not by the library, and each entry holds a catalogue the
    caller was going to hold anyway.
    """

    def __init__(self, search):
        self._search = search
        self._lock = threading.Lock()
        self._flights = {}

    def __call__(self, query):
        key = _query_key(query)
        with self._lock:
            flight = self._flights.get(key)
            mine = flight is None
            if mine:
                flight = self._flights[key] = _Flight()
        if mine:
            try:
                flight.result = list(self._search(query))
            except BaseException as exc:
                # Deliberately broader than `Exception`, but NOT because a
                # narrower clause would hang the waiters: the `finally` below
                # sets the event on every path out of this call, interrupts
                # included, so nobody blocks either way. What the breadth buys
                # is that a waiter is handed the failure that actually ended
                # the flight. Narrowed to `Exception`, an interrupt would
                # leave this entry with neither a result nor an error, and
                # every waiting file would report its own turn as having
                # failed on an empty result — one outage described as N
                # unrelated bugs, not one of them naming the cause.
                flight.error = exc
            finally:
                flight.done.set()
        else:
            flight.done.wait()
        if flight.error is not None:
            raise flight.error
        # A fresh list per caller: one file's candidates list must not be the
        # object another file is iterating.
        return list(flight.result)


class ScanProducer:
    """Reads a batch of the library, works out what each file is, and yields
    a proposal for every file it could decide.

    `produce` is a GENERATOR, and that is the design rather than a detail:
    the runner records each proposal as it is yielded, so a scan that dies on
    file 49 of 50 keeps the 48 it already found. The legacy tool computed
    everything and wrote at the end, and lost all of it on the same failure.

    `search` is injected. Nothing in this package executes a query — the
    adapter layer phrases one and reads the results — so the expensive,
    networked half is the caller's, and no test here opens a socket.

    `store` is injected for two reasons and no others: `select` asks it which
    subjects are muted or already proposed, and an unidentifiable file is
    muted through it. It is deliberately NOT how proposals are persisted —
    those are yielded, and the runner records them, so the dismissal and mute
    rules that make a reviewer's past decisions stick stay in one place.
    Hand this the same `Store` the runner holds; `Store` is
    single-instance-per-file and will refuse a second handle on the same
    path.

    A scan NEVER writes to the media server. It reads the batch and it looks
    things up; it does not set `organized`, does not touch tags or
    performers, and does not write anything back. That is what makes it safe
    to run repeatedly.
    """

    name = "library-scan"
    # Every selected file drives a lookup against a scraper, which is the
    # resource `COST_CLASS_LIMITS` rations to one job at a time.
    cost = "scraping"

    def __init__(self, stash, search, *, store, folder="library", limit=None,
                 name_filter=None, threshold=DEFAULT_THRESHOLD, aliases=None, workers=4):
        if workers < 1:
            # A pool of nothing would do nothing at all, forever. Refuse it
            # where the mistake was made rather than on a background thread
            # hours later. `select` owns the matching rule for `limit`.
            raise ValueError(f"workers must be at least 1, got {workers!r}")
        self._stash = stash
        self._search = search
        self._store = store
        self._folder = folder
        self._limit = limit
        self._name_filter = name_filter
        self._threshold = threshold
        # Indexed and checked HERE, for the same reason `workers` is checked
        # here: a duplicated or empty alias line is a wiring mistake that is
        # wrong for every file, and this is the last point at which the caller
        # who made it is still on the stack. Checked inside `produce` instead,
        # it raises on a background thread inside a started job, where it
        # reads as that run failing rather than as a line needing an edit.
        #
        # It is also the whole index this run will use. `resolve` rebuilds one
        # per call from a plain mapping, so passing the mapping down would
        # re-normalise every key once per FILE — measured at 12.7 seconds
        # across a 50,000-file scan against a 200-entry map, spent on keys
        # that cannot have changed.
        self._aliases = aliases if isinstance(aliases, Aliases) else Aliases(aliases)
        self._workers = workers

    def produce(self, ctx):
        """Yield one proposal per file the scan could decide.

        Yielded in COMPLETION order, not input order: order carries no
        meaning downstream (the store is keyed by fingerprint), and a slow
        file must not hold back the proposals behind it, because a proposal
        still queued when the run dies is a proposal lost.

        Each file that is not proposed is accounted for in one of three
        ways, and the difference between them is the reason `examine`
        exists as its own step:

        * unidentifiable — no candidates, or no creator resolved — is MUTED,
          so it stops consuming a lookup on every future run;
        * a refusal (a tie, or nothing over the threshold) is logged and
          nothing else: a human should look, and muting would hide a file
          that is one glance from being resolved;
        * an error is logged and nothing else: that is evidence about the
          network, not about the file.
        """
        # No alias validation here: `__init__` built the index, which is where
        # the map is now checked. Doing it again on the first line of a run
        # would be checking a value that cannot have changed since, and would
        # put the failure back on a background thread.
        #
        # Fetched WHOLE, deliberately: `limit` belongs to `select`, which
        # applies it after the narrowings. Passing it here would limit at the
        # source, so a batch of 50 would be the first 50 files overall and
        # the muted and already-proposed ones among them would eat the
        # budget. The accepted cost is one query for the unorganized set
        # rather than a page of it.
        _, scenes = self._stash.unorganized_scenes(None)
        selected, counts = select(
            scenes, store=self._store, folder=self._folder,
            name_filter=self._name_filter, limit=self._limit)
        # Built once and logged twice, opening and closing, because the
        # runner keeps ONE message: `JobRunner._log` assigns `state.message`,
        # so by the time a job ends every line but its last has been
        # overwritten. Logged only at the start, this breakdown is the record
        # of how the batch was chosen right up until the moment anybody reads
        # it. The spec asks the scan to distinguish "your earlier decisions
        # suppressed these files" from "there was nothing to do"; the job's
        # `skipped` cannot carry that (a muted file is dropped in `select`
        # and never yielded, so it never reaches `_record` to be counted), so
        # the closing line is the only place left for it.
        selection = (
            "selected %d of %d files (%d already proposed, %d already muted, "
            "%d outside the filter, %d deferred)" % (
                counts.selected, counts.total, counts.already_proposed,
                counts.muted, counts.filtered_out, counts.deferred))
        ctx.log(selection)

        search = _SingleFlight(self._search)
        proposed = muted = refused = errors = 0
        pool = ThreadPoolExecutor(max_workers=self._workers,
                                  thread_name_prefix=self.name)
        try:
            futures = {pool.submit(self._examine, scene, search): scene
                       for scene in selected}
            for done, future in enumerate(as_completed(futures), start=1):
                subject_id = str(futures[future]["id"])
                outcome = future.result()
                if outcome.mute_reason is not None:
                    # Through the store, with the reason, so a later reader
                    # learns whether the catalogue had nothing for a creator
                    # we did identify or the layout named nobody at all —
                    # only the second is fixed by an alias.
                    self._store.mute(SUBJECT_TYPE, subject_id,
                                     reason=outcome.mute_reason)
                    muted += 1
                elif outcome.error is not None:
                    errors += 1
                elif outcome.proposal is None:
                    refused += 1
                else:
                    proposed += 1
                ctx.log("%d/%d scene %s: %s" % (
                    done, len(selected), subject_id, outcome.reason))
                if outcome.proposal is not None:
                    yield outcome.proposal
        finally:
            # `cancel_futures` matters when the consumer walks away — a
            # closed generator, or a runner shutting down mid-batch. The
            # default `shutdown(wait=True)` would hold the closer until
            # every queued file had been looked up, which for a large batch
            # means a "stop" that takes as long as finishing. Files already
            # in flight cannot be cancelled and are left to finish; nothing
            # reads their results.
            pool.shutdown(wait=False, cancel_futures=True)
        # Outside the `finally` on purpose: a run that was abandoned did not
        # finish, and a log line claiming it did would be the only record.
        # The counts describe what this run DID; the breakdown describes what
        # it was given and why most of it may not have been worked. "0
        # proposed" alone reads the same for a library nobody has decided
        # anything about and for one whose every file a reviewer has already
        # muted, and those call for opposite responses.
        ctx.log("finished: %d proposed, %d muted, %d refused, %d errors; %s"
                % (proposed, muted, refused, errors, selection))

    def _examine(self, scene, search):
        """One file's turn, with its exceptions kept to itself.

        The isolation is around `examine` as a whole, not only around the
        lookup: a malformed scene raises there too, and one bad record must
        not end a batch whose other files are fine. It is reported as an
        error rather than a mute, because a mute is a verdict that the file
        is unidentifiable and a malformed record is not evidence of that.
        """
        try:
            return examine(scene, search=search, folder=self._folder,
                           threshold=self._threshold, aliases=self._aliases)
        except Exception as exc:
            # Name the type as well as the message, for the same reason
            # `examine` does: `str(exc)` alone is '' for a bare raise.
            error = "%s: %s" % (type(exc).__name__, exc)
            return Outcome(error=error, reason=error)
