"""Choosing which files a library scan works on, and working one of them.

Two halves, deliberately separate. `select` picks the batch: no scoring, no
lookups, no threading happens there. `examine` works one file: one lookup,
one decision, and no threading either. Threading composes them and lives
elsewhere, because a function that both decides and schedules is untestable
in either respect.

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
from dataclasses import dataclass

from cronicled.artist import creator_folder, resolve
from cronicled.scoring import decide, score

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


def examine(scene, *, search, folder, threshold=0.5, aliases=None):
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
