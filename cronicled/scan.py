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
from cronicled.censorship import decensor
from cronicled.scoring import AMBIGUITY_MARGIN, DEFAULT_THRESHOLD, decide, score

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


# A file whose subject already has a proposal is dropped here (see the
# `already_proposed` narrowing below) BEFORE this module ever examines it
# again -- deliberately, since re-examining it would spend a rate-limited
# lookup on a file a person has already been shown. That is also what makes a
# proposal from an older, thinner version of this tool permanent: nothing
# scans past it on its own, which is the whole reason `Store.supersede`
# exists as an explicit, per-row action instead.
#
# A tempting complement is a MARKER on the payload itself -- something
# recording the shape (schema, producer version) that produced it, so a
# scan could one day notice "this payload predates the current shape" and
# offer to refresh it automatically. Deliberately not done here. Nothing in
# this module would read such a marker without ALSO re-examining an
# already-proposed file to make use of it, and that re-examination is exactly
# the cost this narrowing exists to ration -- one lookup per file per run,
# spent on files a person has already been shown. A marker with no reader is
# not a smaller first step toward that; it is a field every future producer
# has to keep populating, and a change to what it means later would be
# exactly the kind of interpretation `store.py`'s module docstring already
# warns a payload must stay opaque to. If an automatic staleness check is
# ever built, it will need its own budget (its own cost class, its own
# cadence) independent of this one -- at which point stamping a marker
# becomes worth its cost, because something would finally read it.
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

    A SUPERSEDED proposal does not suppress its file either, and that is what
    lets a person explicitly retire a stale proposal and have its file looked
    at again — the one path a dismissal deliberately does not offer for an
    `applied` or `failed` row (see `Store.supersede`'s docstring). The check
    is against `store.superseded_fingerprints()` directly, by fingerprint,
    rather than against `item.state`: an `applied` or `failed` row's own
    `state` is left untouched by `supersede` on purpose (its `resolved_at`
    records a real write, or a real attempt, and when), so `state` alone
    could never tell this apart from an ordinary still-blocking proposal.
    The `supersede` table is the only place that distinction is recorded, so
    it is the only place this can read it from.
    """
    if limit is not None and limit < 0:
        # `scenes[:-1]` would quietly drop the LAST file rather than select
        # nothing, so a negative limit must not reach a slice.
        raise ValueError(f"limit must not be negative, got {limit!r}")

    scenes = list(scenes)
    pattern = (name_filter or "").casefold()
    muted = {subject_id for subject_type, subject_id in store.muted_subjects()
             if subject_type == SUBJECT_TYPE}
    superseded = store.superseded_fingerprints()
    proposed = _subjects(item for item in store.items(folder=folder)
                         if item["fingerprint"] not in superseded)

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


def _owners_of(search, owner_of):
    """The `owners_of` collaborator `resolve` uses to confirm a candidate
    name against the catalogue (see `cronicled.artist._resolve_by_evidence`),
    or None when this run has no way to read an owner off a result at all.

    Built from the SAME `search` `examine` already calls for the winning
    candidate's own catalogue — not a second, unwrapped lookup — so a
    candidate `resolve` checks and then goes on to win costs exactly one
    network round trip, not two: the verifying call here and the later
    `search(resolution.name)` below are the identical query, and
    `_SingleFlight` (see `ScanProducer.produce`) answers the second from the
    first's cached result. Only a LOSING candidate's check is genuinely
    extra — bounded by how many plausible names one file's folder and
    filename disagree about, which is the filename's own dash count, not
    the library.

    `owner_of` is a single-result reader — `SiteAdapter.owner_of`, or None
    when the adapter cannot attribute a result to anyone at all
    (`catalog_resolvable=False`; see `docs/adapters.md`) — passed straight
    through rather than a whole adapter, the same reason `censorship` below
    is a plain dict and not one either.
    """
    if owner_of is None:
        return None

    def owners_of(candidate):
        return [owner_of(r) for r in search(candidate)]
    return owners_of


def _enrichment_url(candidate):
    """The URL `examine` hands to `enrich` for the winning candidate, or
    `None` when there is nothing to scrape.

    Prefers `urls` (the plural, forward-looking field), falling back to the
    deprecated singular `url` only when the plural is empty — the SAME
    precedence `Stash.apply_scene` already uses when deciding what to write
    onto the scene (`match.get("urls") or ([match["url"]] if
    match.get("url") else [])`). Matching that precedence here, rather than
    inventing a second rule for the same object, is deliberate: it is what
    guarantees enrichment always scrapes the URL an apply would eventually
    treat as canonical for this candidate, never a different one a
    disagreement between the two fields could otherwise produce.

    `None` when the candidate carries neither — there is nothing to enrich
    with, and `examine` treats that exactly like an `enrich` that was never
    supplied at all, rather than as a failure.
    """
    urls = candidate.get("urls") or []
    if urls:
        return urls[0]
    return candidate.get("url") or None


def examine(scene, *, search, folder, threshold=DEFAULT_THRESHOLD, aliases=None,
           censorship=None, owner_of=None, enrich=None):
    """Work out what `scene` is, and return what that concluded.

    `search` is the injected lookup: called with the resolved creator's name
    and expected to return that creator's catalogue as a list of dicts, each
    carrying at least a `title`. Nothing in this package executes a query, so
    the expensive networked half belongs to the caller — and no test here
    opens a socket. A caller running several files at once wraps `search` to
    collapse identical queries; that is the seam, and this function stays
    single-file and single-lookup.

    `owner_of`, when given, is a one-argument callable reading the owner name
    off a single search result (`SiteAdapter.owner_of`, unbound from the rest
    of the adapter — see `_owners_of`). It is what lets `resolve` check a
    candidate name against the catalogue instead of assuming the first one
    when the folder and filename disagree — see
    `cronicled.artist._resolve_by_evidence`. Omitted (the default), the
    creator is resolved exactly as it always was: no search happens before
    scoring, and the folder-wins default applies without checking it.

    `folder` is the store's proposal namespace, not a directory on disk: the
    proposal returned is complete and can be yielded to the job runner
    unchanged. (The creator's own directory is read off the path, and is a
    different thing entirely — see `creator_folder`.)

    `censorship` is a store's word-substitution map (`{canonical:
    [substituted_form, ...]}`, the shape `SiteAdapter.censorship` carries),
    used HERE for exactly one purpose: `cronicled.censorship.decensor` rewrites
    each candidate's title back to its canonical spelling before it is
    SCORED, so a censored store title still string-matches an uncensored
    local filename. It is never applied to the candidate that reaches the
    proposal — `winner` is either the object `search` returned or, after a
    successful enrichment (see `enrich` below), the fuller object `enrich`
    returned for the SAME URL; neither is ever the decensored form — so a
    decensored title can influence which candidate wins but can never
    itself become the applied title. The store called a title what it
    called it; rewriting that and writing the rewrite back would invent a
    title the store never used. `None` (the default, and what every caller
    that has no censorship map to offer should pass) behaves as `{}`, which
    `decensor` defines as a no-op.

    `enrich`, when given, is a one-argument callable: called with the
    winning candidate's own URL (`_enrichment_url`) and expected to return
    either a fuller `ScrapedScene`-shaped dict describing the SAME object
    (ordinarily `Stash.scrape_scene_url`) or `None` when it has nothing new
    to add. Called at most ONCE per call to `examine`, and only after
    `decide` has already picked a winner — a losing candidate is never
    enriched, and a file that mutes or refuses never reaches this at all, so
    the cost is one extra lookup per PROPOSAL, never per candidate scored
    and never per file examined. When it returns something, that fuller
    object REPLACES `winner` for every purpose below: the payload's
    `candidate`, and the title `summary` reports. When it returns `None`, or
    when there is no URL to give it (`_enrichment_url` returned `None`),
    `winner` is left exactly as `search` returned it — the same thin
    candidate a proposal has always carried.

    A raising `enrich` is handled exactly like a raising `search` above: a
    fact about the NETWORK, not about the file (see `Only the FINAL search
    call` below, and `cronicled.stash.Stash.scrape_scene_url`'s own
    docstring for why a miss there is a `None`, not an exception, in the
    first place). The proposal is not withheld over it — `winner` is simply
    left thin, and the title and URL it already carries are exactly what a
    proposal has always written. A row built from a thin `winner` shows no
    performers or studio it did not get; see `cronicled.web.rows.to_row` and
    `carries_cover`, which hold the same discipline for the fields a
    candidate does carry. Omitted (the default, and what every caller that
    has no enrichment source to offer should pass), no enrichment call is
    ever made and `examine` behaves exactly as it always has.

    ORDER: the creator is resolved BEFORE anything is scored, and that is
    load-bearing rather than stylistic. `scoring.score(..., artist=)`
    subtracts the creator's tokens from the evidence, so scoring first would
    score against evidence that still contains the creator's name — and a
    file named after nobody but its creator would then match by containment
    on that name alone, taking on the metadata of whichever of the creator's
    titles happened to be offered. That is precisely the failure the
    zero-evidence rule in `scoring` exists to catch, and it can only see it
    if the artist reaches it.

    Only the FINAL `search` call below is wrapped: a raising lookup there is
    the transient failure this is built to survive. A malformed alias map, a
    scene with no file, a candidate with no title are all wiring or data
    mistakes that are wrong for every file, and they propagate rather than
    being reported as this one file's bad luck — and so, for the same
    reason, does a raising `owners_of` call inside `resolve` itself: this
    function does not distinguish that from any other exception `resolve`
    can raise. In production that distinction does not matter — `resolve`
    only ever raises here on a genuine transient failure (the alias map is
    validated once, at `ScanProducer` construction, long before any file
    reaches this function), and `ScanProducer._examine` isolates whichever
    scene hit it into its own `Outcome(error=...)` exactly as it does for
    this function's own uncaught exceptions today. A caller of `examine`
    directly, outside `ScanProducer`, sees the exception, not an `Outcome` —
    which is already true of a malformed alias map, and this is simply the
    same contract extended to a new way `resolve` can raise.
    """
    path = _primary_path(scene)
    name = posixpath.basename(path)
    # The creator's directory, derived from the same path string the name
    # comes from, rather than from the scene's own `basename` field: one
    # source of truth, so a payload whose two fields disagree cannot make the
    # attribution and the evidence describe different files.
    directory = creator_folder(path)

    resolution = resolve(name, directory, aliases,
                         owners_of=_owners_of(search, owner_of))
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

    # `c["title"]` decensored for THIS computation only: `winner` below is
    # sliced from `candidates`, the untouched list, so the proposal always
    # carries whatever `search` returned rather than this rewritten form.
    matches = [score(name, directory, decensor(c["title"], censorship or {}),
                     artist=resolution.name)
               for c in candidates]
    decision = decide(matches, threshold)
    if decision.match is None:
        # A tie, a near miss, or evidence that was nothing but the creator's
        # own name. All three are refusals a person can act on — by looking,
        # by moving the threshold, by renaming the file — so none of them
        # mutes. The reason carries what to act on.
        return Outcome(reason=decision.reason)

    winner = candidates[decision.index]
    # Enrich ONLY the winner, and only once `decide` has actually picked one
    # — a losing candidate is never scraped, and neither is anything on a
    # file that mutes or refuses above this point. A raise here is a fact
    # about the network, not about the file (the same reasoning `search`'s
    # own try/except above applies): it degrades to the thin `winner`
    # `search` already returned rather than costing the file its proposal —
    # a title and a URL are still worth writing, which is exactly what
    # happens without `enrich` at all. See `examine`'s own docstring for the
    # full reasoning and for why a missing URL is treated the same as a
    # `None` reply rather than as a failure.
    if enrich is not None:
        url = _enrichment_url(winner)
        if url:
            try:
                enriched = enrich(url)
            except Exception:
                pass
            else:
                if enriched:
                    winner = enriched
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
        # The candidate whole, as `search` returned it — or, when `enrich`
        # was given a URL and answered with something, the fuller record
        # `enrich` returned for that SAME object instead (see the block
        # above). Either way this module cannot know which of a store's
        # fields an applier will need, and a projection drops them silently.
        # The cost, stated plainly: a search (or a scrape) returning a field
        # that changes between runs (a view count, a signed thumbnail URL)
        # changes the payload, hence the fingerprint, hence re-proposes the
        # same file nightly. That failure is visible in the inbox; a payload
        # missing the field an apply needed is not.
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


# --- Searching every configured store, not one ------------------------------
#
# `examine` above works ONE store: it is the primitive, still fully valid on
# its own, and every test in `ExamineTest` continues to exercise exactly that
# — one search callable, one owner_of, one censorship map, for a caller with
# exactly one store configured and no interest in a second. What follows is
# the layer above it: a real installation configures several stores, and a
# scan must search all of them before it decides anything, never stop at the
# first that answers. See `examine_sources`'s own docstring for the whole
# design; `ScanProducer` is its only production caller.


@dataclass(frozen=True)
class Source:
    """One configured store's contribution to a scan that searches every one
    of them — see `examine_sources`.

    `name` identifies the store in a proposal's payload and in a cross-store
    finding. Two sources sharing a name is a caller's wiring mistake — a
    duplicate config entry — not something this module can catch on its
    own; `ScanProducer` is where every configured store is actually
    assembled and is where such a mistake would need to be refused.

    `search` is this store's own single-argument lookup — the same contract
    `examine`'s own `search` argument documents: called with a creator's
    RESOLVED name, returns that creator's whole catalogue from THIS store as
    a list of dicts, each carrying at least a `title`.

    `owner_of`, when given, reads the owner off one of THIS store's own
    search results (`SiteAdapter.owner_of`), and is used only to help
    resolve which creator a file belongs to — see `_combined_owners_of`. It
    is never used to filter or re-weight this store's own candidate titles,
    which are scored on their text alone, exactly as a single store's
    always were. `None` — the same value
    `cronicled.runscan.build_producer` already passes for a store whose
    `catalog_resolvable` is False — means this store contributes no
    ownership evidence at all; the caller is trusted to keep the two
    consistent, the same trust `build_producer` already places in itself
    for a single store.

    `catalog_resolvable` mirrors `SiteAdapter.catalog_resolvable`. Beyond
    gating `owner_of`, it decides one further thing: when more than one
    store's own search independently clears the threshold for the SAME file
    (see `_choose_winner`), a store that cannot confirm ownership must not
    out-rank, or tie with, one that can — stores are not interchangeable,
    and a bare title match on a store that itself says a title mention
    proves nothing must never be merged onto equal terms with a
    catalogue-resolvable store's winner.

    `censorship` is this store's own word-substitution map, applied only to
    ITS OWN candidates before they are scored — the same reasoning
    `examine`'s own `censorship` argument documents, now kept apart PER
    STORE rather than assumed to be one map for the whole run: two stores
    can censor the same word two different ways, or censor two different
    words, and neither's substitutions belong on the other's titles.
    """
    name: str
    search: object
    owner_of: object = None
    catalog_resolvable: bool = True
    censorship: dict = None


class _StoreDecision:
    """One store's own verdict on one file: the candidates it returned, the
    matches they scored, and what `scoring.decide` made of them — exactly
    what a single-store `examine` would have computed internally, kept
    apart per store so `examine_sources` can compare stores' verdicts to
    EACH OTHER, not only to a threshold."""

    __slots__ = ("source", "candidates", "matches", "decision")

    def __init__(self, source, candidates, matches, decision):
        self.source = source
        self.candidates = candidates
        self.matches = matches
        self.decision = decision


def _combined_owners_of(sources):
    """The `owners_of` collaborator `resolve` uses to confirm a candidate
    creator name, pooling evidence from every source that can offer any —
    see `Source.owner_of`.

    A source with `owner_of=None` (a store `catalog_resolvable` says cannot
    attribute a result to anyone) contributes nothing: it is simply absent
    from the list asked, exactly as a single such store already contributes
    nothing to `_owners_of` today. That is what keeps a non-attributing
    store from being merged onto equal terms with a catalogue-resolvable
    one at the CREATOR-RESOLUTION step — the same discipline `_choose_winner`
    applies at the CANDIDATE step below, for the other place two stores can
    disagree.

    Returns None — "no way to check a candidate against any catalogue" —
    when not one configured source can answer, the same as `_owners_of`
    already returns for a single store with no `owner_of` at all; `resolve`
    then falls back to its ordinary folder-wins default.
    """
    confirmable = [s for s in sources if s.owner_of is not None]
    if not confirmable:
        return None
    per_source = [_owners_of(s.search, s.owner_of) for s in confirmable]

    def owners_of(candidate):
        names = []
        for owners in per_source:
            names.extend(owners(candidate))
        return names
    return owners_of


def _choose_winner(winners):
    """Which store's own eligible candidate a proposal is built from, and
    which OTHER winning stores are recorded as a cross-store finding.

    `winners` is every `_StoreDecision` whose own `decide()` cleared the
    threshold for this file — never fewer than one; the empty case is
    handled by `examine_sources` before this is called.

    A single winner needs no choice at all: returned with an empty
    `competing` list. More than one is the finding this whole module exists
    to get right — see `examine_sources`'s docstring for why it is a
    finding and not a tie, and note the shape it must NOT take: refusing
    outright every time two stores agree would make the common case (the
    ticket's own "most often the same work published in both places") as
    disruptive as the rare one, which is not what a folder and a filename
    disagreeing already does — that case still proposes, with the loser
    recorded, and this follows the same shape.

    Resolved in two steps:

    1. NEVER let a store that cannot confirm ownership out-rank, or tie
       with, one that can. If at least one winner is catalogue-resolvable,
       only catalogue-resolvable winners are candidates for `chosen` at
       all — a non-resolvable winner can still be reported as `competing`,
       just never picked. Only when EVERY winner is non-resolvable are
       they compared to each other on the same footing.
    2. Among whichever set step 1 leaves, rank by SCORE — content, never
       position — and pick the top. If the runner-up is within
       `scoring.AMBIGUITY_MARGIN` of it, that is a genuine tie between
       equally-trustworthy evidence, and this refuses (`(None, None)`)
       rather than let a fraction of a rounding difference, or worse, list
       order, decide it — the same discipline `scoring.decide` already
       applies to two candidates scored within one store. Ties within the
       margin are broken by store NAME only for building the refusal's own
       message, never to still pick a winner.

    Never resolved by where a store happens to sit in `sources`: nothing
    above reads position, only `catalog_resolvable` and each winner's own
    score, so re-ordering a config file cannot change which store's
    candidate a proposal carries, or whether one is picked at all.
    """
    if len(winners) == 1:
        return winners[0], []
    resolvable = [w for w in winners if w.source.catalog_resolvable]
    eligible = resolvable if resolvable else winners

    ranked = sorted(eligible, key=lambda sd: (-sd.decision.match.value,
                                              sd.source.name))
    top = ranked[0]
    if len(ranked) > 1:
        runner_up_value = ranked[1].decision.match.value
        # Rounded to the same three places `scoring.score` rounds a value
        # to, for the identical reason `scoring.decide` does: an unrounded
        # float subtraction would decide this by representation rather than
        # by intent.
        if round(top.decision.match.value - runner_up_value, 3) <= AMBIGUITY_MARGIN:
            return None, None
    competing = [w for w in winners if w is not top]
    return top, competing


def _closest_refusal(per_store):
    """Among stores whose own `decide()` found nothing eligible, the one
    whose best candidate came closest — by raw score, not by where the
    store sits in `sources` — so that reordering a config file never
    changes which store's reason a refusal reports. Ties broken by the
    store's NAME, the one thing about it that cannot depend on position.
    """
    def best_value(store_decision):
        return max((m.value for m in store_decision.matches), default=0.0)
    ranked = sorted(per_store,
                    key=lambda sd: (-best_value(sd), sd.source.name))
    return ranked[0]


def examine_sources(scene, *, sources, folder, threshold=DEFAULT_THRESHOLD,
                    aliases=None, enrich=None):
    """Work out what `scene` is by searching EVERY one of `sources`, and
    decide once over everything that came back.

    This is `examine`'s multi-store sibling, not a wrapper around it:
    creator resolution has to pool evidence across every catalogue-
    resolvable store BEFORE any store is searched for candidates (see
    `_combined_owners_of`), which `examine`'s single-callable `owners_of`
    contract cannot express — so this reimplements the flow, sharing
    `resolve`, `score`, `decide`, `decensor`, `_runners_up` and
    `_enrichment_url` with it rather than duplicating their logic.

    THE RULE THIS FUNCTION EXISTS FOR: every source in `sources` is
    searched, unconditionally, before anything is decided. Nothing here
    stops at the first store that answers — doing that would make the
    outcome depend on the order `sources` happens to list its stores in,
    which is the same silent-ordering mistake this codebase has already
    removed from candidate scoring (`scoring.decide`'s ambiguity refusal),
    from artist resolution (`artist._resolve_by_evidence`'s two-supported-
    candidates refusal), and from alias-key collisions
    (`artist._alias_index`). A store answering nothing for this creator is
    an ordinary, expected outcome — most stores will, most of the time —
    and is never treated as a reason to skip the rest.

    Each store's own candidates are scored and decided ENTIRELY on their
    own — `scoring.decide` runs once per store, over that store's own
    candidate list, never over a list pooled across stores. Pooling
    candidates from different stores into one `decide()` call would let a
    non-attributing store's bare title match compete on equal arithmetic
    terms with a catalogue-resolvable store's confirmed one, which is
    exactly the laundering `Source.catalog_resolvable` exists to prevent;
    keeping each store's decision separate is what makes that prevention
    possible, at the cost below.

    TWO OR MORE STORES CLEARING THE THRESHOLD IS A FINDING, NOT A TIE. Most
    often the same work published in both places; occasionally a real
    conflict about who made it. `_choose_winner` resolves it BY CONTENT,
    never by position: a store that cannot confirm ownership never
    out-ranks or ties with one that can, and among whichever stores remain
    eligible the higher SCORE wins — only a genuine tie (within
    `scoring.AMBIGUITY_MARGIN`, the same margin `scoring.decide` uses for
    two candidates within one store) is refused rather than picked, so the
    common case — several stores agreeing, or one clearly ahead of the rest
    — still produces a proposal instead of demanding a person look at every
    ordinary republish. See `_choose_winner`'s own docstring for the full
    rule. When a winner IS chosen despite other stores also matching, the
    OTHER stores' winning candidates are recorded in the payload's
    `competing_store` (see below) rather than silently dropped — the
    cross-store counterpart to `cronicled.artist.Resolution.competing`,
    reported the same way a reviewer already sees a folder and a filename
    naming different creators.

    ONE LOOKUP PER STORE PER CREATOR, not per file: `sources[i].search` is
    expected to already be single-flighted across a batch — see
    `ScanProducer.produce`, which builds one `_SingleFlight` PER STORE
    (never one shared across stores: the same creator name against two
    different stores is two different lookups, and collapsing them would
    silently answer one store's file from another's catalogue).

    Returns an `Outcome`, on the same three-way contract `examine`
    documents (`mute_reason` / refused-but-not-muted / `error`), with two
    additions when a proposal is returned: the payload's `"store"` names
    which source the winning candidate came from, and `"competing_store"`
    — present only when another store also matched — lists every other
    winning store's own candidate and score, highest first.

    A store's search raising is isolated to THAT store for this file: it
    is recorded and the remaining stores are still searched and still
    allowed to produce a proposal. Only when EVERY store either raised or
    returned nothing does the distinction matter for the outcome itself —
    if at least one store raised, the file cannot be muted (an error is
    evidence about the network, not a confirmed-empty catalogue, and mute
    is reserved for the latter); with no errors at all and every store
    genuinely empty, the file is muted exactly as a single empty store
    already mutes it today. Any store error is also appended to whatever
    `reason` a file ends up with — visible in the per-file log line even
    when the file still proposes off a different, healthy store.
    """
    if not sources:
        raise ValueError(
            "examine_sources needs at least one configured source to "
            "search against")
    path = _primary_path(scene)
    name = posixpath.basename(path)
    directory = creator_folder(path)

    resolution = resolve(name, directory, aliases,
                         owners_of=_combined_owners_of(sources))
    if resolution.name is None:
        return Outcome(mute_reason=MUTE_UNRESOLVED_CREATOR,
                       reason=MUTE_UNRESOLVED_CREATOR)

    per_store = []
    store_errors = []
    for source in sources:
        try:
            candidates = list(source.search(resolution.name))
        except Exception as exc:
            store_errors.append(
                "%s: %s: %s" % (source.name, type(exc).__name__, exc))
            continue
        if not candidates:
            continue
        matches = [score(name, directory,
                         decensor(c["title"], source.censorship or {}),
                         artist=resolution.name)
                  for c in candidates]
        decision = decide(matches, threshold)
        per_store.append(_StoreDecision(source, candidates, matches, decision))

    def with_store_errors(reason):
        if not store_errors:
            return reason
        return "%s (store errors: %s)" % (reason, "; ".join(store_errors))

    winners = [sd for sd in per_store if sd.decision.match is not None]

    if not winners:
        if not per_store:
            if store_errors:
                combined = "; ".join(store_errors)
                return Outcome(error=combined, reason=combined)
            return Outcome(mute_reason=MUTE_NO_CANDIDATES,
                           reason=MUTE_NO_CANDIDATES)
        best = _closest_refusal(per_store)
        reason = "%s: %s" % (best.source.name, best.decision.reason)
        return Outcome(reason=with_store_errors(reason))

    chosen, competing = _choose_winner(winners)
    if chosen is None:
        names = ", ".join(sorted(sd.source.name for sd in winners))
        reason = ("ambiguous across stores: %s each matched a candidate "
                  "above the threshold, too close to call between them"
                  % names)
        return Outcome(reason=with_store_errors(reason))

    winner = chosen.candidates[chosen.decision.index]
    if enrich is not None:
        url = _enrichment_url(winner)
        if url:
            try:
                enriched = enrich(url)
            except Exception:
                pass
            else:
                if enriched:
                    winner = enriched

    payload = {
        "path": path,
        "creator": {
            "name": resolution.name,
            "source": resolution.source,
            "competing": resolution.competing,
            "rejected_folder": resolution.rejected_folder,
        },
        "candidate": winner,
        "score": chosen.decision.match.value,
        "runners_up": _runners_up(chosen.candidates, chosen.matches,
                                  chosen.decision.index),
        "store": chosen.source.name,
    }
    summary = '%s -> "%s" by %s (score %.3f)' % (
        name, winner["title"], resolution.name, chosen.decision.match.value)
    if competing:
        ordered = sorted(
            competing, key=lambda sd: (-sd.decision.match.value, sd.source.name))
        payload["competing_store"] = [
            {"store": sd.source.name,
             "candidate": sd.candidates[sd.decision.index],
             "score": sd.decision.match.value}
            for sd in ordered]
        summary += " [also matched by %s]" % ", ".join(
            sd.source.name for sd in ordered)

    return Outcome(
        proposal={
            "folder": folder,
            "subject_type": SUBJECT_TYPE,
            "subject_id": str(scene["id"]),
            "summary": summary,
            "confidence": chosen.decision.match.value,
            "payload": payload,
        },
        reason=with_store_errors(chosen.decision.reason),
    )


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

    `calls` counts how many of those entries were actually created — a real
    lookup issued against `search` — as opposed to a caller that arrived
    while one was already in flight or found one already answered. That is
    precisely "lookups actually spent" against this store for the run: the
    number a person choosing a file limit now needs, since a scan can search
    several stores and the limit itself still counts files, not lookups (see
    `ScanProducer`'s own docstring). Incremented under the same lock that
    decides `mine`, so two workers racing to create the same entry can never
    both count it.
    """

    def __init__(self, search):
        self._search = search
        self._lock = threading.Lock()
        self._flights = {}
        self.calls = 0

    def __call__(self, query):
        key = _query_key(query)
        with self._lock:
            flight = self._flights.get(key)
            mine = flight is None
            if mine:
                flight = self._flights[key] = _Flight()
                self.calls += 1
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

    `sources` is a list of `Source` — every configured store, injected.
    Nothing in this package executes a query — the adapter layer phrases one
    and reads the results — so the expensive, networked half is the
    caller's, and no test here opens a socket. Every one of them is
    searched for every file this scan examines; see `examine_sources`, the
    per-file worker this delegates to, for why that is the whole point and
    not merely a detail — stopping at the first store that answers would
    make a proposal's contents depend on the order `sources` lists its
    stores in. At least one is required: a scan with nothing configured to
    search against is a wiring mistake to refuse here, at construction, not
    three calls later inside `examine_sources`.

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

    Each source's own `censorship` map and `owner_of` reach `examine_sources`
    unchanged — see `Source`'s own docstring for what each does and why they
    stay per-store rather than becoming one map/reader for the whole run.

    `enrich` is passed straight through, store-agnostic on purpose: it
    scrapes the WINNING candidate's own URL directly, against whichever
    scraper the media server itself matches to that URL, never asking any
    one store's own search to identify anything further — so it stays a
    single, run-wide collaborator rather than one per source. See
    `examine`'s `enrich` paragraph for the full contract and for why a raise
    from it degrades a proposal to its thin candidate rather than costing
    the file its turn. `None` (the default) keeps a proposal's candidate
    exactly as `search` returned it, with no second lookup issued at all —
    today's behaviour, unchanged.
    """

    name = "library-scan"
    # Every selected file drives at least one lookup per configured store
    # against a scraper, which is the resource `COST_CLASS_LIMITS` rations to
    # one job at a time — see `examine_sources` for why the multiplier is
    # bounded per CREATOR rather than per file.
    cost = "scraping"

    def __init__(self, stash, sources, *, store, folder="library", limit=None,
                 name_filter=None, threshold=DEFAULT_THRESHOLD, aliases=None,
                 workers=4, enrich=None):
        if workers < 1:
            # A pool of nothing would do nothing at all, forever. Refuse it
            # where the mistake was made rather than on a background thread
            # hours later. `select` owns the matching rule for `limit`.
            raise ValueError(f"workers must be at least 1, got {workers!r}")
        if not sources:
            # A scan with no store configured to search against would mute
            # or refuse every single file it ever looked at, for a reason
            # that has nothing to do with any of them — a wiring mistake,
            # refused where it was made rather than discovered file by file
            # on a background thread.
            raise ValueError(
                "ScanProducer needs at least one configured source to "
                "search against, got %r" % (sources,))
        self._stash = stash
        self._sources = list(sources)
        self._store = store
        self._folder = folder
        self._limit = limit
        self._name_filter = name_filter
        self._threshold = threshold
        self._enrich = enrich
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

        # One `_SingleFlight` PER SOURCE, never one shared across sources: the
        # same creator name against two different stores is two different
        # lookups, and collapsing them into one cache entry would silently
        # answer one store's file from another's catalogue. Built fresh here,
        # inside `produce`, for the same reason the single-store version
        # always was — the cache must not outlive the run, and a second call
        # to `produce` (there is none in production, but nothing stops a
        # test) must not go on answering from a previous run's entries.
        flights = [_SingleFlight(source.search) for source in self._sources]
        sources = [
            Source(name=source.name, search=flight, owner_of=source.owner_of,
                  catalog_resolvable=source.catalog_resolvable,
                  censorship=source.censorship)
            for source, flight in zip(self._sources, flights)
        ]
        proposed = muted = refused = errors = 0
        pool = ThreadPoolExecutor(max_workers=self._workers,
                                  thread_name_prefix=self.name)
        try:
            futures = {pool.submit(self._examine, scene, sources): scene
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
                    # Through the store too, so a refusal — the one outcome a
                    # person can most often fix (rename a file, add an alias,
                    # move the threshold) — is visible somewhere other than
                    # this job's own log. Keyed by subject, not recorded as a
                    # proposal; see the block above `Store.record_refusal`
                    # for why reusing `item` here would re-propose the same
                    # unresolved file as a fresh row every night.
                    self._store.record_refusal(
                        SUBJECT_TYPE, subject_id,
                        _primary_path(futures[future]), outcome.reason)
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
        #
        # `lookups` is the sum of every source's own `_SingleFlight.calls` —
        # how many REAL queries this run actually issued, after single-flight
        # collapse, across every configured store. `select`'s file limit
        # still means files, not lookups (see this module's own docstring for
        # why that stays true even now) — but that limit is now a multiplier
        # of up to `len(sources)` lookups per file, so a person choosing it
        # deserves to see what it actually cost this run, not just how many
        # files it was spent on.
        lookups = sum(flight.calls for flight in flights)
        ctx.log("finished: %d proposed, %d muted, %d refused, %d errors, "
                "%d lookups; %s"
                % (proposed, muted, refused, errors, lookups, selection))

    def _examine(self, scene, sources):
        """One file's turn, with its exceptions kept to itself.

        The isolation is around `examine_sources` as a whole, not only
        around a lookup: a malformed scene raises there too, and one bad
        record must not end a batch whose other files are fine. It is
        reported as an error rather than a mute, because a mute is a verdict
        that the file is unidentifiable and a malformed record is not
        evidence of that.
        """
        try:
            return examine_sources(scene, sources=sources, folder=self._folder,
                                   threshold=self._threshold,
                                   aliases=self._aliases, enrich=self._enrich)
        except Exception as exc:
            # Name the type as well as the message, for the same reason
            # `examine` does: `str(exc)` alone is '' for a bare raise.
            error = "%s: %s" % (type(exc).__name__, exc)
            return Outcome(error=error, reason=error)
