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
from dataclasses import dataclass, field

from cronicled.artist import Aliases, creator_folder, resolve
from cronicled.censorship import decensor
from cronicled.scoring import (AMBIGUITY_MARGIN, DEFAULT_THRESHOLD, decide,
                               meaningful_tokens, score, title_view)

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

    `marked` stands apart from all six and is deliberately NOT a seventh term
    of that identity. It is not a reason a file was dropped; it is how many of
    `total` this run would not have been OFFERED at all without a configured
    marker tag (see `ScanProducer._pool`) — the organized files the marker put
    back in reach. Every one of them is still counted under exactly one of the
    five buckets above, so adding it to the identity would double-count it.

    It exists because the marker multiplies the pool — measured against one
    real library, 298 files without it and roughly 2688 with — and every file
    in that increase may spend a per-title fallback query per configured
    store. A total that grew nine-fold with nothing saying why is a cost read
    off a slow night instead of off the run that chose to spend it. `0` is
    both "no marker configured" and "the marker matched nothing new", which
    are the same thing to this count: neither added a file.
    """
    total: int
    already_proposed: int
    muted: int
    filtered_out: int
    selected: int
    deferred: int
    # Defaulted, unlike its six siblings, so that every caller and every test
    # written before the marker existed goes on describing a run with no
    # marker configured — which is exactly what those runs are. `select`
    # itself always passes it.
    marked: int = 0


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
def select(scenes, *, store, folder, name_filter=None, limit=None, marked=()):
    """The files a scan should work, and why the others were dropped.

    Returns `(selected, counts)`, `selected` being the surviving scene dicts
    in the order they were offered.

    `marked` is the subject ids the configured marker tag ADDED to `scenes` —
    files this run would not have been offered without it (see
    `ScanProducer._pool`, which is the only thing that can know which those
    were). It changes no decision here: a marked file is filtered, muted,
    already-proposed, selected or deferred on exactly the terms every other
    file is, and nothing below reads it except the count. It is carried this
    far only so `Counts.marked` can say how much of `total` the marker is
    responsible for; empty — the default, and what a run with no marker
    configured passes — makes that count 0 and leaves every other number
    identical to what this function has always returned.

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
    marked = set(marked)
    pattern = (name_filter or "").casefold()
    muted = {subject_id for subject_type, subject_id in store.muted_subjects()
             if subject_type == SUBJECT_TYPE}
    superseded = store.superseded_fingerprints()
    proposed = _subjects(item for item in store.items(folder=folder)
                         if item["fingerprint"] not in superseded)

    narrowed = []
    filtered_out = muted_count = already_proposed = marked_count = 0
    for scene in scenes:
        subject_id = str(scene["id"])
        paths = _paths(scene)
        # Counted BEFORE the narrowings and outside their chain, deliberately:
        # a marked file that is also muted is one file with a reason and one
        # file the marker offered, and it has to appear in both readings. An
        # `elif` here would make the marker look like a fourth way to drop a
        # file and would take that file out of whichever count came later.
        if subject_id in marked:
            marked_count += 1
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
        marked=marked_count,
    )


# --- Working one file ------------------------------------------------------

# How many losing candidates a proposal records. Three is enough for a
# reviewer to see what else was in the running, and a cap is what stops a
# prolific creator's whole catalogue being copied into every one of their
# files' payloads — which the store then hashes, per file, per run.
MAX_RUNNERS_UP = 3

# The ONE reason a scan mutes a file, and the reasons it refuses one instead.
#
# Mute is otherwise the operator's own action — "stop showing me this" — and
# it is permanent: nothing revisits a mute and no later scan re-examines the
# file. A scan may only borrow it for a verdict that stays true however the
# operator changes their library.
#
# An unresolved creator was such a verdict until it was measured, and it is
# not one. It is the most FIXABLE outcome a scan produces — one alias, or one
# folder rename — and it used to mute, justified as rationing lookups. Over
# every file that rule had muted in a real library, two thirds cost no lookup
# at all (resolution returns before any store is searched), the whole set was
# invisible, and no scan revisited any of it: an operator could add the alias
# that fixed a file, run a scan, and watch nothing happen anywhere. So it
# refuses now — recorded, visible, and reconsidered by every later scan. See
# `RETIRED_MUTE_UNRESOLVED_CREATOR` for what that leaves in existing
# databases.
#
# `MUTE_NO_CANDIDATES` stays a mute deliberately, and is out of scope here: a
# file whose creator WAS identified really does spend the lookups its mute
# saves, so its budget argument is the one this one's was not.
MUTE_NO_CANDIDATES = (
    "no candidates: the catalogue offered nothing for this creator")

# The two refusals the unresolved-creator path produces, kept apart because
# only one of them hands the operator something to act on. A folder whose text
# a name guard turned down is exactly what an alias would be written against
# (`Resolution.rejected_folder` records it for that reason), and a single
# catch-all reason would say "unresolved" for both while losing the actionable
# half. A folder that yielded no text for a guard to judge has nothing to
# quote, and must say so as itself rather than by quoting an empty string.
#
# Neither claims the folder was empty: a folder can be a plausible name and
# still leave the creator unresolved (the evidence check declining it), in
# which case it was not rejected by a guard and there is nothing to quote.
REFUSED_UNRESOLVED_CREATOR = (
    "creator unresolved: neither the folder nor the filename yielded a "
    "creator this run could accept")
REFUSED_REJECTED_FOLDER = (
    "creator unresolved: the folder text %r was not accepted as a name, and "
    "the filename yielded none either")

# What the retired rule wrote into the `mute` table before this. HISTORY, not
# a message this code produces: it exists so `release_auto_mutes` can
# recognise the rows that rule left in databases that already exist. Nothing
# writes it again.
#
# It must never be folded into the refusal wording above even if the two ever
# read alike. The release matches this text EXACTLY, and a shared constant
# would make rewording a refusal silently stop releasing anything — while the
# rows it should have released stay hidden, which is the failure the whole
# change exists to end.
RETIRED_MUTE_UNRESOLVED_CREATOR = (
    "creator unresolved: neither the folder nor the filename names one")


@dataclass(frozen=True)
class Outcome:
    """What examining one file concluded.

    Exactly one of `proposal`, `mute_reason` and `error` is ever set, and the
    three-way split is the entire point of this type: there are three
    different kinds of "no proposal" and conflating any two of them is a bug
    a user feels.

    * `mute_reason` — the file is genuinely unidentifiable: the creator was
      identified and the catalogue offered nothing for them. Muting stops it
      consuming a lookup on every future run, which is the budget the whole
      module rations.
    * neither set — refused: a tie, nothing over the threshold, or no creator
      resolved at all. A human should look at this. Muting it would silently
      hide a file that is one glance, one alias, or one threshold change from
      being resolved.
    * `error` — the lookup raised. That is evidence about the NETWORK, not
      about the file. Muting on error would hide a file permanently because
      a socket blipped once, and no later run would ever revisit it.

    `reason` is always set: one line saying which of the four happened, safe
    to log unconditionally. It duplicates `mute_reason`/`error` when those
    are set, and is the ONLY record in the refusal case — where nothing is
    proposed, nothing is muted, and without it the caller has nothing to tell
    a reviewer and no way to distinguish a tie from a near miss.

    `fallback_queries` counts the per-title searches THIS file spent — one
    per (file, store), and zero for a file the per-creator pass resolved (see
    `examine_sources`). It is carried on the outcome rather than tallied by a
    shared counter because every file is examined on its own worker thread;
    the producer sums what comes back and logs the total, so the cost of the
    fallback is a number a reader can see rather than one they infer from the
    file limit.

    `stores` is what each searched store returned for this file, one entry
    per store, built by `_store_reports` — empty on an outcome no store
    search stands behind (a creator that never resolved, a single-store
    `examine`, a file a fingerprint identified). It is set on a REFUSAL,
    which is the outcome that used to keep nothing but one prose sentence
    about one store; see `_store_reports` for the shape and for why the
    other stores' numbers are the diagnosis rather than decoration.
    """
    proposal: dict = None
    mute_reason: str = None
    error: str = None
    reason: str = ""
    fallback_queries: int = 0
    stores: tuple = ()


def _unresolved_creator(resolution):
    """The `Outcome` for a file no creator could be resolved for: a REFUSAL,
    never a mute.

    Refusing costs a row in the Refused section that every later scan
    reconsiders. Muting cost the file itself — invisible, never revisited,
    and regenerated on the next run, so releasing a batch of them by hand was
    never a fix either. The asymmetry is the whole reason this is a separate
    function rather than a bare `Outcome(...)` at each of its two call sites:
    both paths reach the same conclusion about the same file, and a second
    copy of this branch would be free to disagree about which one mutes.

    The reason carries `Resolution.rejected_folder` when the resolver has one
    — the folder text a name guard threw out. That text is what an operator
    writes an alias against, and a mute row showed a bare id instead of it.

    Uncertainty withholds evidence rather than supplying it: with no rejected
    folder, the reason says a creator was not resolved and stops there. It
    does NOT claim the folder was empty, because a folder that IS a plausible
    name can still leave the creator unresolved when the evidence check
    declines it, and that folder was never rejected by any guard.
    """
    if resolution.rejected_folder:
        return Outcome(reason=REFUSED_REJECTED_FOLDER
                       % (resolution.rejected_folder,))
    return Outcome(reason=REFUSED_UNRESOLVED_CREATOR)


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


def candidate_url(candidate):
    """The address `candidate` stands at — its own page on the store that
    offered it — or `None` when it carries none.

    THIS PROJECT'S ONE ANSWER to "where did this candidate come from", and
    public for that reason: `examine` and `examine_sources` hand it to
    `enrich`, `_store_report` records it against a refused file's near
    miss, and `cronicled.web.rows` renders it as the link on a proposed
    title and on every runner-up beneath it. Four readers, one rule. A
    second derivation anywhere would be free to disagree with the one an
    apply uses, and a link that points somewhere an apply would not is
    worse than no link: it is evidence the page did not have.

    NEVER derived from anything but the candidate itself. There is no
    address to be assembled out of a store's name, a box's endpoint or an
    id — see `catalogue_link` for what a fingerprint identification can
    honestly say instead. Uncertainty here may withhold evidence and never
    supply it, so a candidate carrying nothing answers `None` and renders
    as plain text rather than as an anchor pointing nowhere.

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
    winning candidate's own URL (`candidate_url`) and expected to return
    either a fuller `ScrapedScene`-shaped dict describing the SAME object
    (ordinarily `Stash.scrape_scene_url`) or `None` when it has nothing new
    to add. Called at most ONCE per call to `examine`, and only after
    `decide` has already picked a winner — a losing candidate is never
    enriched, and a file that mutes or refuses never reaches this at all, so
    the cost is one extra lookup per PROPOSAL, never per candidate scored
    and never per file examined. When it returns something, that fuller
    object REPLACES `winner` for every purpose below: the payload's
    `candidate`, and the title `summary` reports. When it returns `None`, or
    when there is no URL to give it (`candidate_url` returned `None`),
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
        return _unresolved_creator(resolution)

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
        url = candidate_url(winner)
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

    `title_query` phrases THIS store's per-title fallback query — ordinarily
    `SiteAdapter.search_query`, taking the resolved creator's name and the
    filename read as a title and returning the string to search for. It
    travels with the source rather than being one rule for the run because
    the phrasing is the store's own: an adapter configured
    `search_omits_seed` narrows by title alone, and that is a fact about one
    store's search, not about the scan.

    `None` — the default — means this store contributes no fallback: it is
    asked once, by creator, and a file its page did not answer is refused as
    it is today. That default withholds an expensive query rather than a
    guard, and it fails in the recoverable direction (a refusal a person can
    see, never an automatic write); the cost of leaving it unset is recall,
    and `runscan.build_producer` sets it for every configured adapter.
    """
    name: str
    search: object
    owner_of: object = None
    catalog_resolvable: bool = True
    censorship: dict = None
    title_query: object = None


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


def _judge(source, candidates, *, name, directory, artist, threshold):
    """One store's verdict on one file's candidates: score every candidate
    against the filename and let `scoring.decide` pick or refuse.

    Shared by both passes — the per-creator search and the per-title fallback
    — so a candidate is weighed identically however it was found. A second
    copy of these three lines would be free to disagree about the artist
    subtraction, the store's own censorship map, or the threshold, and a
    fallback candidate judged by different arithmetic than the pass it is
    meant to rescue is exactly the drift this ticket exists to avoid.
    """
    matches = [score(name, directory,
                     decensor(c["title"], source.censorship or {}),
                     artist=artist)
               for c in candidates]
    return _StoreDecision(source, candidates, matches,
                          decide(matches, threshold))


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


def _agreed_title(store_decision):
    """The form of one store's winning title in which two stores' answers
    are the SAME answer.

    `decensor` is `cronicled.text.normalize` — which it calls — plus THIS
    store's own declared substitutions, which is exactly the text `_judge`
    handed the scorer. Agreement is therefore judged on the string that was
    scored rather than on a second reading of the same title: a store that
    spells a word around its own censor and one that does not are two
    spellings of one title, and the only map that can say so is the store's
    own. Comparing the raw titles would make the two disagree here while
    the scorer saw them agree.

    Nothing looser than string equality of that form — no token subsetting,
    no similarity. `normalize` is this project's existing answer to "are
    these the same string", and a second answer written here would be free
    to drift from it.
    """
    candidate = store_decision.candidates[store_decision.decision.index]
    return decensor(candidate["title"], store_decision.source.censorship or {})


def _choose_winner(winners):
    """Which store's own eligible candidate a proposal is built from, which
    OTHER winning stores named the SAME title, and which named a different
    one.

    Returns `(chosen, competing, agreeing)`, or `(None, None, None)` when
    the winners are a genuine ambiguity and nothing may be proposed.

    `winners` is every `_StoreDecision` whose own `decide()` cleared the
    threshold for this file — never fewer than one; the empty case is
    handled by `examine_sources` before this is called.

    A single winner needs no choice at all: returned with both lists empty.
    More than one is the finding this whole module exists to get right —
    see `examine_sources`'s docstring for why it is a finding and not a
    tie, and note the shape it must NOT take: refusing outright every time
    two stores agree would make the common case (the ticket's own "most
    often the same work published in both places") as disruptive as the
    rare one, which is not what a folder and a filename disagreeing already
    does — that case still proposes, with the loser recorded, and this
    follows the same shape.

    Resolved in three steps:

    1. NEVER let a store that cannot confirm ownership out-rank, or tie
       with, one that can. If at least one winner is catalogue-resolvable,
       only catalogue-resolvable winners are candidates for `chosen` at
       all — a non-resolvable winner can still be reported as `competing`,
       just never picked, and never counts towards agreement either. Only
       when EVERY winner is non-resolvable are they compared to each other
       on the same footing.
    2. Among whichever set step 1 leaves, rank by SCORE — content, never
       position — and take everything the margin cannot separate from the
       top: every winner within `scoring.AMBIGUITY_MARGIN` of it, the top
       included. A winner further down than that has simply lost on score,
       exactly as it did before.
    3. Ask what that tied set actually SAYS, by `_agreed_title`. One
       distinct title across all of it is agreement, not ambiguity: the
       stores are not offering a choice, they are corroborating each other,
       which is the strongest text evidence this tool produces. It is
       proposed, and the other tied stores are returned in `agreeing` so
       the corroboration is recorded rather than thrown away. More than one
       distinct title is a real choice between equally-trustworthy
       candidates, and this refuses (`(None, None, None)`) rather than let
       a fraction of a rounding difference, or worse, list order, decide it
       — the same discipline `scoring.decide` applies to two candidates
       scored within one store.

    PARTIAL agreement is not agreement. Three tied winners of which two
    name one title and the third another is still a choice somebody has to
    make, so it refuses: the test is that the tied set is UNANIMOUS, never
    that some pair of it happens to match.

    Which of an agreeing set is carried is a preference among equals, not a
    verdict — the stores agree about the title, which is the thing being
    decided; what differs is only whose URL and metadata the proposal
    carries. It falls out of the ranking above as the alphabetically first
    store NAME, and cannot be anything else: agreement is equality of the
    very text `_judge` scored, so agreeing winners hold identical scores by
    construction and the `-value` half of the sort key cannot separate
    them. (That is a property of step 3 being exact string equality. A
    looser agreement rule would let scores differ inside an agreeing set,
    and would have to say for itself which one is carried.)

    Never resolved by where a store happens to sit in `sources`: nothing
    above reads position, only `catalog_resolvable`, each winner's own
    score, its own title and its own name, so re-ordering a config file
    cannot change which store's candidate a proposal carries, or whether
    one is picked at all.
    """
    if len(winners) == 1:
        return winners[0], [], []
    resolvable = [w for w in winners if w.source.catalog_resolvable]
    eligible = resolvable if resolvable else winners

    ranked = sorted(eligible, key=lambda sd: (-sd.decision.match.value,
                                              sd.source.name))
    top = ranked[0]
    # Rounded to the same three places `scoring.score` rounds a value to,
    # for the identical reason `scoring.decide` does: an unrounded float
    # subtraction would decide this by representation rather than by intent.
    # `top` itself is always in here — a gap of zero — so a lone winner of
    # step 1 leaves a tied set of one, which is no tie at all.
    tied = [sd for sd in ranked
            if round(top.decision.match.value - sd.decision.match.value, 3)
            <= AMBIGUITY_MARGIN]
    agreeing = []
    if len(tied) > 1:
        if len({_agreed_title(sd) for sd in tied}) != 1:
            return None, None, None
        agreeing = [sd for sd in tied if sd is not top]
    carried = [top] + agreeing
    competing = [w for w in winners if all(w is not c for c in carried)]
    return top, competing, agreeing


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


def _store_report(source, store_decision, error):
    """What ONE searched store returned for one file, as plain data.

    Every number here was already computed by `_judge` and thrown away on the
    way to `Store.record_refusal`, which kept a single sentence naming a
    single store. A reader of that sentence cannot tell the other stores were
    searched at all, and cannot read the score back out of it without parsing
    English. Both are recorded as values instead.

    THE THREE STATES A STORE CAN BE IN ARE KEPT APART, because collapsing any
    two of them loses the only signal separating a misconfigured store from an
    unhelpful one:

    * returned rows, none good enough — `rows` is how many came back, `score`
      the best of them, `title`/`url` the candidate that earned it.
    * returned nothing — `rows` is 0 and there is no score, because nothing
      was scored. Not 0.0: a value would be a number this function invented
      and then recorded in the same field, in the same type, as scores the
      scorer really produced.
    * raised — `error` names what, and `rows` is None rather than 0. An error
      is evidence about the network, not a confirmed-empty catalogue, and a 0
      here would assert the second. That is the same distinction
      `examine_sources` already refuses to blur when deciding whether a file
      may be muted.

    A store can be in the first and the third at once: its per-creator search
    answered and its per-title fallback then raised. Everything known about it
    is kept — the rows and the score AND the error — rather than one being
    dropped to fit a single state. Which of the two a reader is shown first is
    the display's decision, not this one's; see
    `cronicled.web.rows._refused_store_view`.

    `url` is `candidate_url`'s answer, not a second reading of the
    candidate: it is already this project's one rule for "which address does
    this candidate stand at", matching the precedence an apply uses, so a link
    offered here cannot point somewhere an apply would disagree with. It is
    None for a candidate carrying no address at all, which is a candidate that
    renders as text rather than as an anchor pointing nowhere.

    Which candidate is the near miss is decided by SCORE, ties broken by
    TITLE — the identical key `_runners_up` sorts a payload's losers by, and
    for the identical reason: the catalogue's own result order must not decide
    what a person is shown.
    """
    report = {"store": source.name, "rows": None, "score": None,
              "title": None, "url": None, "error": error}
    if store_decision is None:
        if error is None:
            report["rows"] = 0
        return report
    ranked = sorted(zip(store_decision.matches, store_decision.candidates),
                    key=lambda pair: (-pair[0].value, pair[1]["title"]))
    match, candidate = ranked[0]
    report["rows"] = len(store_decision.candidates)
    report["score"] = match.value
    report["title"] = candidate["title"]
    report["url"] = candidate_url(candidate)
    return report


def _store_reports(sources, decisions, raised):
    """One `_store_report` per source, closest miss first.

    `decisions` maps a source's POSITION to its `_StoreDecision` and `raised`
    maps a position to what that store's search raised — both exactly as
    `examine_sources` keeps them, so nothing is recomputed and no second
    scoring path can disagree with `_judge` about the artist subtraction, a
    store's censorship map, or the threshold.

    Ordered by `(-score, name)`: the same key `_closest_refusal` ranks by, so
    the store the refusal's own sentence talks about is the one a reader meets
    first, and so re-ordering a config file cannot re-order what is recorded.
    Position in `sources` is read only to pair a store with its own verdict
    and never survives into the result. A store with no score of its own —
    empty, or raised — sorts as 0.0 and lands at the end, among its peers by
    name.
    """
    reports = [_store_report(source, decisions.get(index), raised.get(index))
               for index, source in enumerate(sources)]
    reports.sort(key=lambda r: (-(r["score"] or 0.0), r["store"]))
    return tuple(reports)


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
    `candidate_url` with it rather than duplicating their logic.

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
    eligible the higher SCORE wins — only stores the margin cannot separate
    (`scoring.AMBIGUITY_MARGIN`, the same margin `scoring.decide` uses for
    two candidates within one store) have anything left to settle, and what
    settles it is what they SAY. Tied stores that all name one title are
    agreeing, not disagreeing, and the proposal is made with every one of
    them recorded in the payload's `agreeing_stores`; tied stores naming
    different titles are refused. Refusing on agreement would destroy the
    strongest text evidence this tool produces — the margin rule exists to
    stop configuration order picking silently between real alternatives,
    not to refuse when there is nothing to pick between. See
    `_choose_winner`'s own docstring for the full rule. When a winner is
    chosen despite other stores matching a DIFFERENT candidate, those
    stores' candidates are recorded in the payload's `competing_store` (see
    below) rather than silently dropped — the cross-store counterpart to
    `cronicled.artist.Resolution.competing`, reported the same way a
    reviewer already sees a folder and a filename naming different
    creators.

    ONE LOOKUP PER STORE PER CREATOR, not per file: `sources[i].search` is
    expected to already be single-flighted across a batch — see
    `ScanProducer.produce`, which builds one `_SingleFlight` PER STORE
    (never one shared across stores: the same creator name against two
    different stores is two different lookups, and collapsing them would
    silently answer one store's file from another's catalogue).

    A PER-TITLE QUERY IS THE FALLBACK, AND ONLY EVER THE FALLBACK. A store's
    search answers with a PAGE, not a catalogue, so a creator with more clips
    than fit one page can have the wanted clip missing from everything that
    came back — and no scoring recovers a candidate that was never returned.
    So when the pass above leaves not one store with a winner, and only then,
    each store is asked once more for the file itself: `source.title_query`
    (the store's own `SiteAdapter.search_query`) phrases the resolved
    creator's name and `scoring.title_view(name)` — the SAME view the scorer
    weighs, never a second derivation — into one query, and the answers are
    judged by the same `_judge` the first pass used. A store that raised
    above is not asked again, and a store that raises here loses only its own
    fallback: the others still answer and the file still refuses if none of
    them can rescue it.

    THE COST, STATED, because the per-creator shape was chosen for it: the
    fallback adds at most ONE query per (file, store) — times whatever
    `censorship.search_variants` expands it to, bounded at 6 — and adds it
    only to files that were going to be refused or muted. A run's file limit
    therefore still means files (see `ScanProducer`), and the worst case for
    a batch is `files x stores` fallback queries, reached only when every
    file in it fails. `Outcome.fallback_queries` carries the real number back
    so the producer can log what a run actually spent.

    Muting is held to the same bar. A file every store answered nothing for
    is still asked by title before `MUTE_NO_CANDIDATES` is recorded, because
    a mute is the claim that the file is unidentifiable and stops it ever
    being looked at again — the strongest verdict here makes, and not one to
    reach on the cheaper question alone. The single exception is a file with
    no meaningful token of its own: `scoring._is_eligible` bars it at every
    score against every candidate, so no answer could change its outcome and
    the query is withheld as provably useless, not as a guess about it.

    The threshold is untouched by any of this. `scoring.DEFAULT_THRESHOLD`
    was measured against a per-CREATOR candidate population, and a per-title
    query returns a different one — fewer rows, closer to the filename.
    Whether 0.70 still holds for it is a measurement nobody has taken, so
    nothing here claims it does.

    Returns an `Outcome`, on the same three-way contract `examine`
    documents (`mute_reason` / refused-but-not-muted / `error`), with three
    additions when a proposal is returned: the payload's `"store"` names
    which source the winning candidate came from; `"agreeing_stores"` —
    present only when another store named the SAME title too close to call
    between — lists those stores by name; and `"competing_store"` — present
    only when another store matched a DIFFERENT candidate — lists every
    such store's own candidate and score, highest first.

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
        return _unresolved_creator(resolution)

    # Keyed by the source's POSITION, not its name: two sources sharing a
    # name is a caller's wiring mistake `Source` names but cannot catch, and
    # keying by name would let one store's fallback quietly replace another's
    # verdict. Position pairs a store with its own two passes and does
    # nothing else: `_choose_winner` and `_closest_refusal` still rank by
    # content, so re-ordering a config file still changes nothing.
    decisions = {}
    store_errors = []
    # Keyed by position like `decisions`, and holding the failure's own text
    # rather than only the fact of it: `_store_reports` records what each
    # store did, and "raised" with no `TypeError: ...` behind it sends a
    # reader back to a log they may no longer have. The `store_errors` line
    # is the same text with the store's name in front, built from this one so
    # the two cannot describe the same failure differently.
    raised = {}
    for index, source in enumerate(sources):
        try:
            candidates = list(source.search(resolution.name))
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
            store_errors.append("%s: %s" % (source.name, detail))
            raised[index] = detail
            continue
        if not candidates:
            continue
        decisions[index] = _judge(source, candidates, name=name,
                                  directory=directory, artist=resolution.name,
                                  threshold=threshold)

    def with_store_errors(reason):
        if not store_errors:
            return reason
        return "%s (store errors: %s)" % (reason, "; ".join(store_errors))

    def winners_of(decided):
        return [sd for sd in decided if sd.decision.match is not None]

    per_store = [decisions[i] for i in sorted(decisions)]
    winners = winners_of(per_store)

    # --- the per-title fallback -----------------------------------------
    #
    # Reached only here: every store has been asked once, by creator, and not
    # one of them produced a candidate this file could be proposed from. A
    # file that DID resolve never gets this far, and that ordering is the
    # whole cost argument — see this function's docstring.
    fallback_queries = 0
    if not winners:
        # ...with one exception, and it is a proof rather than a heuristic:
        # a file carrying no meaningful token is barred by
        # `scoring._is_eligible` at ANY score, for EVERY candidate, because
        # the count is derived from the filename, the folder and the artist
        # and never from a candidate title. No answer a store could give
        # would change this file's outcome, so the query cannot buy
        # anything. That is the one case where withholding a query costs no
        # recall at all — every other file gets its turn.
        if meaningful_tokens(name, directory, artist=resolution.name):
            as_title = title_view(name)
            for index, source in enumerate(sources):
                # A store that just raised is not asked a second question.
                # Its failure is already recorded against this file, and a
                # second query is another round trip to a store known to be
                # failing right now — the same reasoning `_SingleFlight`
                # applies to a cached failure, which cannot help here because
                # a different query is a different cache entry.
                if index in raised or source.title_query is None:
                    continue
                query = source.title_query(resolution.name, as_title)
                fallback_queries += 1
                try:
                    candidates = list(source.search(query))
                except Exception as exc:
                    # Recorded against the store as well as in the run's own
                    # error line. Its per-creator pass may have answered, and
                    # a store whose narrower follow-up then failed has not
                    # been shown to hold nothing — the same reason a single
                    # store's error bars a mute.
                    detail = "%s: %s" % (type(exc).__name__, exc)
                    store_errors.append("%s: %s" % (source.name, detail))
                    raised[index] = detail
                    continue
                if not candidates:
                    continue
                # REPLACES that store's per-creator verdict rather than
                # pooling with it. The two passes are two answers to two
                # different questions, and nothing deduplicates ACROSS them
                # — `search._dedup_key` folds duplicates within one call
                # only — so a clip present in both answers would reach
                # `decide` twice and refuse as "ambiguous: X vs X", the
                # artefact that dedup exists to prevent. The per-creator
                # candidates cannot win anyway (nothing here was eligible);
                # what is given up is their reason text, and the targeted
                # pass's reason describes the better-aimed question.
                decisions[index] = _judge(source, candidates, name=name,
                                          directory=directory,
                                          artist=resolution.name,
                                          threshold=threshold)
            per_store = [decisions[i] for i in sorted(decisions)]
            winners = winners_of(per_store)

    if not winners:
        if not per_store:
            if store_errors:
                combined = "; ".join(store_errors)
                return Outcome(error=combined, reason=combined,
                               fallback_queries=fallback_queries)
            return Outcome(mute_reason=MUTE_NO_CANDIDATES,
                           reason=MUTE_NO_CANDIDATES,
                           fallback_queries=fallback_queries)
        best = _closest_refusal(per_store)
        reason = "%s: %s" % (best.source.name, best.decision.reason)
        return Outcome(reason=with_store_errors(reason),
                       fallback_queries=fallback_queries,
                       stores=_store_reports(sources, decisions, raised))

    chosen, competing, agreeing = _choose_winner(winners)
    if chosen is None:
        names = ", ".join(sorted(sd.source.name for sd in winners))
        reason = ("ambiguous across stores: %s each matched a candidate "
                  "above the threshold, too close to call between them"
                  % names)
        return Outcome(reason=with_store_errors(reason),
                       fallback_queries=fallback_queries,
                       stores=_store_reports(sources, decisions, raised))

    winner = chosen.candidates[chosen.decision.index]
    if enrich is not None:
        url = candidate_url(winner)
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
    if agreeing:
        # EVERY store that named this title, not only the one carried. The
        # corroboration is the finding — the difference between one store's
        # word and two — and the carried store's name alone cannot express
        # it. Named `agreeing_stores` after `Identified.agreeing`, the same
        # field the fingerprint pass records the same fact in.
        agreed = [sd.source.name for sd in agreeing]
        payload["agreeing_stores"] = agreed
        summary += " [also named by %s]" % ", ".join(agreed)
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
        fallback_queries=fallback_queries,
    )


# --- Identifying a file before searching for it -----------------------------
#
# Everything above this line identifies a file by SEARCHING for it: a creator
# is resolved off the path, a store's catalogue is searched by that name, and
# the results are scored against the filename. What follows does not search at
# all. A stash-box is asked which scene a file's own fingerprints belong to,
# and either it has seen that exact file or it has not.
#
# The two are not alternatives and this one does not replace the other:
# measured against a real library, fingerprints identified 13 of 23 files the
# text path had refused, and answered nothing at all for the other 10. Absence
# of a fingerprint hit is not evidence about a file — most files, most boxes —
# so the text path stays exactly as it is for everything a box does not
# recognise.


# What a proposal's payload records in place of a score when a box identified
# the file. Named rather than inlined so a reader of the payload, a row
# builder and a test agree on one string, and so that a second way of
# identifying a file has to add to this deliberately.
IDENTIFIED_BY_FINGERPRINT = "fingerprint"


@dataclass(frozen=True)
class Identified:
    """One file a stash-box recognised by its own fingerprints.

    `box` is the box that is the source of `candidate` — the whole
    `ScrapedScene` it returned, the same shape a text scrape produces, so it
    can be carried into a payload and applied by the same code.

    `endpoint` is that same box's URL, and it is REQUIRED — no default, on
    the same terms every other field here that a write depends on. `box` is
    a name a person reads; a `stash_ids` link is `{endpoint, stash_id}`, so
    the endpoint is the only half of the box's identity the link can be made
    from. It was in hand at lookup time and dropped, and the whole of the
    link was lost with it.

    `remote_site_id` is that box's own id for the scene, kept because it is
    the only thing that makes two boxes' answers COMPARABLE — see
    `_resolve_claims` — and because it is the other half of the link. It may
    be `None`: a box that recognised the file but named no id has still
    identified it, and nothing downstream needs the id to record or apply the
    match. What such an identification must NOT produce is a link to
    nothing; see `catalogue_link`.

    `agreeing` names the other boxes that returned the SAME
    `remote_site_id`. Agreement is not a disagreement to report, but it is
    worth recording: it is the difference between one box's word and three.
    """
    box: str
    candidate: dict
    endpoint: str
    remote_site_id: str = None
    agreeing: tuple = ()


@dataclass(frozen=True)
class Conflict:
    """Boxes that recognised one file as DIFFERENT scenes.

    Never resolved here, and deliberately not resolvable: two boxes that
    both computed a hash off the same bytes and reached different scenes are
    not a tie to break by whichever was configured first — they are evidence
    that at least one box's record is wrong about this file, which is the
    single most useful thing a reviewer could be told about it. This project
    has removed silent iteration-order resolution from candidate scoring,
    artist resolution, alias-key collisions and cross-store winners; this is
    not the place to add a fifth.

    `claims` holds every one of them, `(box_name, remote_site_id, candidate)`
    in the order the boxes were tried, so the refusal can name them all.
    """
    claims: tuple


@dataclass(frozen=True)
class FingerprintPass:
    """What asking every configured box about a whole batch concluded.

    `identified` maps a scene's subject id to its `Identified` or `Conflict`.
    A scene NO box recognised is simply absent — the ordinary case, and the
    one that falls through to the text path unchanged.

    `errors` holds one line per box that could not be asked or could not be
    trusted. A box erroring is a fact about that box, never about the batch:
    the other boxes' answers are kept, and every file the remaining boxes did
    not recognise still reaches the text path.
    """
    identified: dict = field(default_factory=dict)
    errors: tuple = ()


def _resolve_claims(claims):
    """What a file's collected `(box_name, endpoint, match)` claims amount to.

    `None` when nothing recognised it, an `Identified` when the claims all
    name one scene, a `Conflict` when they do not.

    Agreement requires every claim to carry a NON-NULL `remote_site_id` and
    for all of them to be equal. The null half of that is the guard, not
    bookkeeping: comparing ids with a missing one treated as a value would
    make two boxes that each declined to name a scene look like two boxes
    naming the SAME scene, and a default that happens to skip a guard is how
    this project has been bitten before. Uncertainty withholds evidence; it
    never supplies it.

    A single claim needs no comparison and is taken as it stands, id or no
    id — there is nothing for it to disagree with.

    When several claims DO agree, the first is the one carried forward. That
    is the boxes' configured order, which is the operator's own stated
    preference and the order the caller was told to try them in — not an
    accident of dict iteration, and not a tie being broken: the claims agree
    about which scene this is, so what is being chosen is only whose copy of
    the same scene's metadata to record, and the others are recorded beside
    it in `agreeing` rather than dropped.
    """
    if not claims:
        return None
    first_box, first_endpoint, first_match = claims[0]
    ids = [match.get("remote_site_id") for _, _, match in claims]
    if len(claims) == 1:
        return Identified(box=first_box, candidate=first_match,
                          endpoint=first_endpoint, remote_site_id=ids[0])
    if None in ids or len(set(ids)) != 1:
        return Conflict(claims=tuple(
            (box, match.get("remote_site_id"), match)
            for box, _, match in claims))
    agreeing = tuple(dict.fromkeys(box for box, _, _ in claims[1:]
                                   if box != first_box))
    return Identified(box=first_box, candidate=first_match,
                      endpoint=first_endpoint, remote_site_id=ids[0],
                      agreeing=agreeing)


def identify_by_fingerprint(scene_ids, *, boxes, lookup):
    """Ask every one of `boxes`, in the order given, which of `scene_ids` it
    recognises by fingerprint, and return a `FingerprintPass`.

    `boxes` is a list of `{"name", "endpoint"}` — `Stash.stash_boxes`'s own
    return shape, in the server's configured order. `lookup` is
    `(endpoint, scene_ids) -> [[match, ...], ...]`, ordinarily
    `Stash.scrape_scenes_by_fingerprint`: ONE call per box for the WHOLE
    batch, not one per file. A real installation had three boxes configured
    and only one returned any match, so every box is asked and none is
    assumed to answer.

    EVERY box is asked, and one that raises does not cost the others. Its
    failure is recorded in `errors` and the loop goes on, for the same
    reason `examine_sources` isolates a store's search failure to that
    store: an outage at one box is one fact about the run, and letting it
    end the pass would take the batch's OTHER identifications with it, and
    the text fallback for every file in the batch besides.

    The reply is checked to be exactly as long as the request before any of
    it is used. `lookup` is injected and this function cannot assume it is
    the client method that already makes the same check — and the failure
    being guarded is silent: entry `i` is the answer for `scene_ids[i]` and
    nothing else ties the two together, so a short or long reply zipped
    against the ids attributes one file's box metadata to a different file.
    That is a wrong automatic write nobody notices; refusing the box's whole
    answer costs a run of fingerprint identification that the text path then
    covers.
    """
    scene_ids = [str(scene_id) for scene_id in scene_ids]
    claims = {scene_id: [] for scene_id in scene_ids}
    errors = []
    for box in boxes:
        name = box["name"]
        endpoint = box["endpoint"]
        try:
            per_scene = lookup(endpoint, scene_ids)
        except Exception as exc:
            errors.append("%s: %s: %s" % (name, type(exc).__name__, exc))
            continue
        if len(per_scene) != len(scene_ids):
            errors.append(
                "%s: answered %d match lists for %d scenes, so nothing it "
                "returned can be matched to the file it belongs to"
                % (name, len(per_scene), len(scene_ids)))
            continue
        for scene_id, matches in zip(scene_ids, per_scene):
            for match in matches:
                # The endpoint is kept beside the name, not instead of it:
                # the name is what a person reads on the row, and the
                # endpoint is the half of the box's identity a
                # `stash_ids` link is made of. Keeping only the name is
                # how the link came to be dropped.
                claims[scene_id].append((name, endpoint, match))

    identified = {}
    for scene_id in scene_ids:
        resolved = _resolve_claims(claims[scene_id])
        if resolved is not None:
            identified[scene_id] = resolved
    return FingerprintPass(identified=identified, errors=tuple(errors))


def _conflict_detail(claims):
    """Every disagreeing box named, with the scene it named — the whole of
    what a reviewer needs to go and look at both."""
    return "; ".join(
        "%s says %s (%r)" % (box, remote_site_id, (match or {}).get("title"))
        for box, remote_site_id, match in claims)


def fingerprint_outcome(scene, identification, *, folder):
    """The `Outcome` for a file a box recognised — or for one they disagreed
    about.

    A hit is a PROPOSAL, and it is recorded so that nothing downstream can
    mistake it for a scored one. It carries no `score`, no `confidence` and
    no `runners_up`: it did not score 1.0, it was identified, and nothing
    computed a number for it. Writing one in would put a value in the
    payload that no scorer produced, which the row view, the threshold
    control and the runners-up display would every one of them read as the
    scorer's own output. What it carries instead is `identified_by`, the box
    that identified it, and that box's own id for the scene.

    A `Conflict` is a REFUSAL — neither a mute nor an error, the third of
    `Outcome`'s three kinds of "no proposal": a human should look, and both
    boxes' answers are in the reason so they can. It deliberately does NOT
    fall through to the text path. Falling through would resolve, by
    scoring a filename, a question two sources that hashed the actual bytes
    could not agree on — the weaker mechanism silently settling what the
    stronger one flagged. A refusal is visible in the inbox and recoverable;
    a scored proposal built over a known conflict is neither.
    """
    path = _primary_path(scene)
    name = posixpath.basename(path)
    subject_id = str(scene["id"])
    if isinstance(identification, Conflict):
        return Outcome(reason=(
            "fingerprint conflict: %s — the boxes recognised this file as "
            "different scenes, so none of them is taken"
            % _conflict_detail(identification.claims)))

    candidate = identification.candidate
    payload = {
        "path": path,
        # The whole match as the box returned it, for the same reason a
        # scored proposal carries the whole candidate: this module cannot
        # know which field an applier will need, and a projection drops them
        # silently.
        "candidate": candidate,
        "identified_by": IDENTIFIED_BY_FINGERPRINT,
        "box": identification.box,
        # Beside the name, never instead of it. The name is what the row
        # shows a person; the endpoint is what `catalogue_link` needs to
        # make the `{endpoint, stash_id}` pair an apply writes. A payload
        # carrying only the name can be read but cannot be linked.
        "endpoint": identification.endpoint,
        "remote_site_id": identification.remote_site_id,
    }
    summary = '%s -> "%s" identified by fingerprint (%s)' % (
        name, candidate.get("title"), identification.box)
    if identification.agreeing:
        payload["agreeing_boxes"] = list(identification.agreeing)
        summary += " [also identified by %s]" % ", ".join(
            identification.agreeing)
    return Outcome(
        proposal={
            "folder": folder,
            "subject_type": SUBJECT_TYPE,
            "subject_id": subject_id,
            "summary": summary,
            # No `confidence` key at all, deliberately. `Store.record` takes
            # it as optional and stores NULL for a proposal that has none,
            # which is the honest record here: nothing measured this file
            # against anything, so there is no number to store and none is
            # invented.
            "payload": payload,
        },
        reason=summary,
    )


def catalogue_link(payload):
    """The `{endpoint, stash_id}` pair a proposal's payload stands for, or
    `None` when it names no link to make.

    This is the one case where the link between a file and a catalogue can
    be written with certainty rather than inferred: a box hashed the actual
    bytes and answered with its own id for them, so the pair is (the box we
    asked, the id it returned) — not something read back out of a scrape and
    hoped to be about the same scene.

    BOTH halves are required and both are read with `.get`, because three
    different payloads legitimately reach here:

    - a scored, text-matched proposal, which has neither key. A site scraper
      is not a catalogue endpoint and returns no such id, so there is
      nothing to link and `None` is the whole answer.
    - a fingerprint-identified proposal whose box named no id. That box has
      still identified the file, and the proposal is still worth applying —
      but a link to nothing is not a weaker link, it is a wrong one, and
      uncertainty here may withhold evidence and never supply it.
    - a fingerprint-identified proposal recorded BEFORE the endpoint was
      kept, which names an id and no endpoint at all. There is no endpoint
      to guess from the box's name (nothing maps one to the other after the
      fact), and half a link is not a link.
    """
    endpoint = payload.get("endpoint")
    stash_id = payload.get("remote_site_id")
    if not endpoint or not stash_id:
        return None
    return {"endpoint": endpoint, "stash_id": stash_id}


# --- Running a batch -------------------------------------------------------


def release_auto_mutes(store):
    """Release every mute the retired unresolved-creator rule left behind,
    returning the subjects released as `(subject_type, subject_id)` pairs.

    A one-time repair that runs every time, because it must: those rows sit
    in operators' live databases, hiding files the scan will now refuse and
    reconsider instead, and nothing else would ever look at them again.

    WHAT IT TOUCHES, precisely, because a manual mute lost here is not
    recoverable. A row qualifies only when its `reason` is EXACTLY
    `RETIRED_MUTE_UNRESOLVED_CREATOR` — not a prefix, not a substring, not
    "any reason a scan might have written". Nothing else writes that
    sentence: the only other reason a scan ever stored is
    `MUTE_NO_CANDIDATES` (different text, deliberately still a mute, and out
    of scope), and the only mute an operator can make carries the inbox's own
    wording, `Store.mute`'s `reason=None` default being the only other shape
    in the table. A mute is therefore released only on positive evidence that
    the retired rule wrote it, never on the absence of evidence that a person
    did.

    Through `Store.unmute`, one subject at a time, rather than a DELETE over
    the table: a subject absent from the `mute` table is not the same as a
    subject a scan can select, and only the second is what an operator
    wanted. `unmute` also returns the `item` rows the mute hid to `new`,
    which a DELETE would leave sitting in `state = 'muted'` — hidden from the
    inbox by a mute that no longer exists.

    Safe to run twice: the second pass finds nothing to match, because the
    first removed the rows it matched. Safe to run against a database the
    rule never touched, which is every fresh install: it reads the mute table
    and writes nothing.
    """
    released = []
    for entry in store.mutes():
        if entry["reason"] != RETIRED_MUTE_UNRESOLVED_CREATOR:
            continue
        store.unmute(entry["subject_type"], entry["subject_id"])
        released.append((entry["subject_type"], entry["subject_id"]))
    return released


@dataclass(frozen=True)
class GoneSweep:
    """What one pass of `sweep_gone` did.

    `marked` is how many subjects it recorded gone for the FIRST time —
    subjects it re-confirmed are not counted, or every night's run would
    report the same population again as though it had just found it.

    `problem` names why nothing could be decided, and is `None` only when the
    sweep really did read the whole library. That split is the reason this is
    a type rather than an int: `marked=0, problem=None` means "every stored
    subject is still there", and `marked=0` with a problem means "nothing was
    asked". Those two call for opposite responses and read identically as a
    bare zero.
    """
    marked: int
    problem: str = None


# What a sweep says when it refuses to mark anything, spelled once. Each is a
# statement about the READ, never about any file — see `sweep_gone`.
GONE_READ_FAILED = "could not read the library's scene ids"
GONE_READ_PARTIAL = "the library's scene ids came back incomplete"
GONE_READ_EMPTY = "the library reported no scenes at all"


def sweep_gone(stash, store, subject_type=SUBJECT_TYPE):
    """Mark every stored subject the media server no longer holds, or refuse.

    Returns a `GoneSweep`. Writes nothing to the media server and nothing to
    the store unless it has POSITIVE evidence of a complete read.

    Why this is not "not in the scan's pool"
    ----------------------------------------
    The pool is the unorganized scenes plus, with a marker configured, the
    organized ones carrying that tag (see `ScanProducer._pool`). An organized
    scene without the marker is absent from the pool and perfectly present in
    the library — and a scene this tool applied a proposal to is organized by
    that very act. A check built on pool membership would mark thousands of
    present files gone. So this reads the COMPLETE id set
    (`Stash.scene_ids`), which does not filter on `organized` at all.

    The three ways it refuses, and why each is the safe direction
    ------------------------------------------------------------
    Every stored mute, dismissal and refusal exists so that a scan cannot
    overrule a person. A sweep that mistook a bad read for a mass deletion
    would hide all of them at once, from a single wrong answer, with nothing
    to say it had happened. So:

    * A RAISING read marks nothing. An error is evidence about the network,
      not about any file — the same rule the rest of this module already
      applies to a store that will not answer.
    * A PARTIAL read marks nothing, and the check is positive: the number of
      DISTINCT ids returned must equal the `count` the server itself reported.
      Not "the response parsed", not "no error was raised" — a short list with
      no error is indistinguishable from a complete one, and every id missing
      from it is a present file this would mark gone. Distinct rather than
      merely counted, because a repeated id inflates the list length to
      `count` while leaving the SET short, which is the shape that actually
      decides anything here.
    * An EMPTY library marks nothing, even though `count == 0` agrees with
      itself perfectly. A server pointed at a fresh or rebuilt database
      answers this way, and it is the one answer that would hide a person's
      entire history in a single sweep. A library that genuinely holds no
      scenes loses only some tidying by being refused, which is the cheaper
      mistake by a wide margin.

    What it CANNOT catch, said plainly: a library halfway through a rebuild
    reports a consistent count for the scenes it has so far, so this will mark
    the rest gone. Nothing available here distinguishes that from real
    deletions. What limits the damage is that marking is not erasing — every
    row, reason and snapshot survives (see `Store.mark_gone`) — which is why
    that decision is the one this depends on.
    """
    try:
        count, ids = stash.scene_ids()
    except Exception as exc:
        # Caught broadly on purpose. The classification of a media-server
        # failure belongs to the client (see `cronicled.stash.StashError`),
        # and NO classification of it changes the answer here: transient or
        # permanent, a failed read is not evidence that a file was deleted.
        # Narrowing this would let an unexpected error type reach the caller
        # as a dead scan instead of as a sweep that declined.
        return GoneSweep(marked=0, problem="%s: %s: %s"
                         % (GONE_READ_FAILED, type(exc).__name__, exc))
    present = set(ids)
    if len(present) != count:
        return GoneSweep(marked=0, problem="%s (%d distinct of %d reported)"
                         % (GONE_READ_PARTIAL, len(present), count))
    if not present:
        return GoneSweep(marked=0, problem=GONE_READ_EMPTY)
    marked = 0
    # Sorted so a run's writes happen in a stated order rather than in
    # whatever order a set iterates in. Nothing here depends on the order --
    # each subject is an independent row -- but a log or a database read back
    # after the fact is easier to compare against another run's.
    for subject_id in sorted(store.subject_ids(subject_type) - present):
        # Accumulated from what the store reports, one subject at a time,
        # rather than counted from the set above: `mark_gone` is the only
        # thing that can say whether a subject was newly recorded or was
        # already gone from an earlier run, and a count of the set would
        # report the whole standing population every night.
        if store.mark_gone(subject_type, subject_id):
            marked += 1
    return GoneSweep(marked=marked)


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


def pool_scenes(stash, marker):
    """Everything a scan-shaped pass is offered, and which of it a marker
    tag added -- the unorganized set, plus, with a marker configured, the
    organized scenes that carry it.

    Returns `(scenes, marked)`, `marked` being the subject ids of the ones
    the marker contributed (see `select`'s own `marked` parameter for what
    that is for). With no marker configured this is the unorganized set and
    nothing else, exactly as every caller here always read.

    This is `ScanProducer`'s own selection, factored out so a SECOND caller
    that needs "the same population the scan reaches" -- see
    `cronicled.stashbox_scan.StashBoxCheckProducer` -- asks this rather than
    writing its own copy. A selection implemented twice is a selection that
    drifts: the scan and anything else meant to agree with it would each
    change their own copy on their own schedule, and the two would quietly
    stop meaning the same "marked".

    With one configured, the scenes carrying that tag are added --
    `Stash.tagged_scenes` does not constrain `organized`, which is the whole
    point: a file an earlier tool identified by guesswork is ordinarily
    marked organized, so the unorganized read is precisely the read that
    cannot see it. What is NOT done is pooling every organized file: the
    marker is the evidence that this particular file was organized
    provisionally, and without it a nightly pass would carry a whole library
    (6275 files in the one measured) instead of the population somebody
    actually wants looked at again.

    APPENDED to the unorganized set, never placed in front of it -- a caller
    that also takes a `limit` (see `select`) takes the first survivors, so a
    marked population many times the size of the unorganized one, put
    first, would spend the whole budget on files that were not waiting.

    A scene the unorganized read already returned is not added twice, and
    does not count as marked -- it cost this run nothing new and was always
    in the pool.

    A CONFIGURED MARKER THE SERVER HAS NO TAG FOR RAISES, and that is the one
    thing this refuses to do quietly. Tag ids are installation-specific, so
    the name is resolved here, per call, against the server as it stands.
    `Stash.tag_id_by_name` answers `None` for a name no tag has -- a typo, a
    tag renamed, a config copied from another install -- and taking that as
    "no marked files" would produce an empty addition indistinguishable from
    a marker that is working and matching nothing. That is the exact state
    the configuration exists to change, so it must be impossible to sit in
    unnoticed.

    Nothing here writes: both calls are reads, and the marker itself is left
    on every scene that carries it.
    """
    _, scenes = stash.unorganized_scenes(None)
    if marker is None:
        return scenes, frozenset()
    tag_id = stash.tag_id_by_name(marker)
    if tag_id is None:
        raise ValueError(
            "the configured marker tag %r does not exist on the media "
            "server, so this run would silently pool nothing extra and "
            "read as though no file were marked. Correct the name to the "
            "tag your library actually uses, or remove the setting."
            % (marker,))
    _, tagged = stash.tagged_scenes(tag_id, None)
    already = {str(scene["id"]) for scene in scenes}
    added = [scene for scene in tagged if str(scene["id"]) not in already]
    return (scenes + added,
            frozenset(str(scene["id"]) for scene in added))


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

    `marker`, when given, is the NAME of a tag a scene carries to say its
    metadata was inferred rather than checked — a date, a filename, sometimes
    a creator, guessed by some earlier tool and never confirmed against a
    catalogue. Such a file is usually marked organized as well, and a scan
    pools the unorganized set, so the largest population of
    provisionally-identified files in a library is the one nothing ever looks
    at again. Naming the tag here adds the scenes carrying it to the pool
    whatever their `organized` flag says; see `_pool` for exactly how, and
    for what happens when the server holds no such tag. `None` — the default,
    and what an operator who has named no marker keeps — pools precisely what
    this class always pooled.

    THE SCAN DOES NOT REMOVE THE MARKER, and nothing here should be made to.
    Shedding it belongs to whatever APPLIES a proposal — the point at which
    the metadata stops being inferred — and is out of scope for a class that
    never writes to the media server at all (see the paragraph above about
    that). Stripping it would also be no fix on its own: what hides these
    files is the `organized` flag the pool is READ by
    (`Stash.unorganized_scenes`), and `select` never looks at a tag at all —
    so removing the marker from 2390 organized scenes changes nothing at all
    about which of them a scan pools,
    while destroying the one signal that says which organized files were
    organized provisionally rather than deliberately.

    `identify`, when given, is a one-argument callable taking the whole
    batch's subject ids and returning a `FingerprintPass` — ordinarily
    `identify_by_fingerprint` bound to a `Stash`'s own boxes and lookup (see
    `cronicled.runscan.build_producer`). It runs ONCE, for the whole batch,
    BEFORE a single store is searched, and that order is the point rather
    than an optimisation: a file a box recognises by its own hashes is
    already identified, and searching a store for it afterwards would spend
    a rate-limited lookup re-deriving an answer that is already in hand and
    would offer a scored guess beside an identification. So a file the pass
    identifies never reaches `examine_sources` at all — no store search, no
    scoring, no threshold — and every file it does not identify reaches it
    exactly as it does today. `None` (the default) skips the pass entirely
    and this class behaves precisely as it always has.
    """

    # The default a manually-started scan runs under. `__init__`'s `name`
    # overrides it per instance, which is how a scan started on a cadence
    # keeps its own registration instead of sharing this one — see the
    # comment beside that assignment.
    name = "library-scan"
    # Every selected file drives at least one lookup per configured store
    # against a scraper, which is the resource `COST_CLASS_LIMITS` rations to
    # one job at a time — see `examine_sources` for why the multiplier is
    # bounded per CREATOR rather than per file, and for the one query per
    # (file, store) the per-title fallback adds on top for a file that pass
    # could not resolve.
    cost = "scraping"

    def __init__(self, stash, sources, *, store, folder="library", limit=None,
                 name_filter=None, threshold=DEFAULT_THRESHOLD, aliases=None,
                 workers=4, enrich=None, identify=None, marker=None,
                 name=None, every=None, at=None, zone=None):
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
        self._identify = identify
        # The tag NAME, kept as configured. It is resolved to this server's
        # own tag id inside `produce`, not here: ids are installation-specific
        # (see `Stash.tag_id_by_name`), and a producer built at start-up and
        # run every night must ask about the tag as it stands at each run
        # rather than hold an id read once — the same reason `build_producer`
        # reads `stash.stash_boxes()` inside the `identify` closure instead of
        # at construction.
        self._marker = marker
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
        # A name of this instance's own, shadowing the class attribute above.
        # `JobRunner` keys its registry by name and `JobRunner.reregister`
        # REPLACES whatever is registered under one, so two producers that
        # share a name are one registration: a manual scan built with a limit
        # of 25 would silently become whatever a scheduled, unbounded scan
        # runs next. A scan that something else starts on a cadence therefore
        # takes a name of its own rather than the shared class default; see
        # `cronicled.runscan.build_scheduled_producer`.
        if name is not None:
            self.name = name
        # The cadence this producer DECLARES, in seconds, read off the object
        # by `cronicled.schedule.resolve`. `None` — the default, and what a
        # manually-started scan keeps — means it declares none, which
        # `resolve` refuses for an enabled producer rather than inventing an
        # interval for it. Set unconditionally so a producer's cadence is a
        # value that was decided, never an attribute that happens to be
        # missing.
        self.every = every
        # The other way of saying when, and the one an unattended scan uses:
        # `at` is a stated time of day and `zone` the zone it is read in, both
        # read off this object by `cronicled.schedule.resolve` exactly as
        # `every` is. Set unconditionally and for the same reason. Never both
        # at once — `resolve` refuses a producer declaring a cadence AND a
        # stated time, as a contradiction rather than a precedence rule.
        self.at = at
        self.zone = zone

    def produce(self, ctx):
        """Yield one proposal per file the scan could decide.

        Yielded in COMPLETION order, not input order: order carries no
        meaning downstream (the store is keyed by fingerprint), and a slow
        file must not hold back the proposals behind it, because a proposal
        still queued when the run dies is a proposal lost.

        Each file that is not proposed is accounted for in one of three
        ways, and the difference between them is the reason `examine`
        exists as its own step:

        * unidentifiable — the creator was identified and the catalogue
          offered nothing for them — is MUTED, so it stops consuming a lookup
          on every future run;
        * a refusal (a tie, nothing over the threshold, or no creator
          resolved) is recorded through the store: a human should look, and
          muting would hide a file that is one glance, or one alias, from
          being resolved. The retired mutes of the last kind are released on
          the way in — see `release_auto_mutes`;
        * an error is logged and nothing else: that is evidence about the
          network, not about the file.

        When `identify` was given, every selected file is offered to the
        configured stash-boxes FIRST, in one batch, and the files a box
        recognised are proposed from what it returned without any store
        being searched for them at all. Only what is left goes to the pool
        below. See this class's own `identify` paragraph for why that order
        is the point.
        """
        # No alias validation here: `__init__` built the index, which is where
        # the map is now checked. Doing it again on the first line of a run
        # would be checking a value that cannot have changed since, and would
        # put the failure back on a background thread.
        #
        # The release comes FIRST, before the selection reads the mute table,
        # so a file it frees is examined by THIS run rather than waiting for
        # one nobody scheduled.
        #
        # Here rather than at start-up because this is the point where it
        # matters and the one place every scan goes through, however it was
        # started — the web UI's runner and the command-line scan both. A
        # release wired into one entry point is a release an operator who
        # only ever uses the other never gets.
        #
        # Logged only when it actually released something, the same rule the
        # fingerprint note below follows: an absent line means the retired
        # rule left nothing here, not that nothing was looked at.
        released = release_auto_mutes(self._store)
        if released:
            ctx.log("released %d file(s) muted by the retired "
                    "unresolved-creator rule; they are examined again from "
                    "this run on" % len(released))
        # Here, and before the pool is read, because this is the moment the
        # library is being read anyway and because the answer must not depend
        # on the pool: `sweep_gone` asks the server for its COMPLETE id set,
        # which is a different question from the one `_pool` asks, and putting
        # it after a `_pool` that RAISES (a marker naming no tag does) would
        # skip the sweep on exactly the runs an operator is already fixing
        # something. Not on the page-render path: a per-row call to the media
        # server while somebody is reading a list is the wrong place for it.
        sweep = sweep_gone(self._stash, self._store)
        scenes, marked = self._pool()
        selected, counts = select(
            scenes, store=self._store, folder=self._folder,
            name_filter=self._name_filter, limit=self._limit, marked=marked)
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
        # Appended only when a marker is actually configured, the same rule
        # the fingerprint note at the end of this method follows: a run
        # without one logs exactly the line it always did, so an absent
        # clause means "no marker was configured" rather than "the marker
        # matched nothing" — which are different states and call for
        # different next steps. With one configured the clause is always
        # present, 0 included: a marker that adds nothing is the fact worth
        # reading, not a line worth hiding.
        if self._marker is not None:
            selection += ("; %d of the %d offered only because they carry "
                          "the marker tag %r"
                          % (counts.marked, counts.total, self._marker))
        ctx.log(selection)

        # The fingerprint pass runs HERE — before a single `_SingleFlight` is
        # built, before the pool exists, before any store is asked anything.
        # A file a box recognises is identified, and a store search for it
        # afterwards would spend a rate-limited lookup producing a scored
        # guess about a file that is no longer in question.
        identified, box_errors = self._identify_batch(selected)
        by_fingerprint = [(scene, identified[str(scene["id"])])
                          for scene in selected
                          if str(scene["id"]) in identified]
        # Everything a box did not recognise, in the order it was selected —
        # the text path's input, unchanged in every respect but its length.
        remaining = [scene for scene in selected
                     if str(scene["id"]) not in identified]

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
                  censorship=source.censorship,
                  title_query=source.title_query)
            for source, flight in zip(self._sources, flights)
        ]
        tally = {"proposed": 0, "muted": 0, "refused": 0, "errors": 0}
        done = 0
        # Summed from what each file's own examination reports, never counted
        # here: the fallback is decided per (file, store) on a worker thread,
        # and a counter kept at this level could only ever count files. The
        # fingerprint loop below adds nothing to it — a file a box identified
        # is never searched for at all, by creator or by title.
        fallbacks = 0
        # Files a box identified are recorded first, in selection order —
        # they are already decided, so there is nothing to wait for and no
        # reason to hold them behind the pool's first completion.
        for scene, identification in by_fingerprint:
            outcome = fingerprint_outcome(scene, identification,
                                          folder=self._folder)
            tally[self._record(scene, outcome)] += 1
            done += 1
            ctx.log("%d/%d scene %s: %s" % (
                done, len(selected), str(scene["id"]), outcome.reason))
            if outcome.proposal is not None:
                yield outcome.proposal

        pool = ThreadPoolExecutor(max_workers=self._workers,
                                  thread_name_prefix=self.name)
        try:
            futures = {pool.submit(self._examine, scene, sources): scene
                       for scene in remaining}
            for future in as_completed(futures):
                scene = futures[future]
                outcome = future.result()
                tally[self._record(scene, outcome)] += 1
                fallbacks += outcome.fallback_queries
                done += 1
                ctx.log("%d/%d scene %s: %s" % (
                    done, len(selected), str(scene["id"]), outcome.reason))
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
        # The fingerprint note is appended only when the pass actually did
        # something — identified a file, or had a box fail. A run with no box
        # configured (every install today) logs exactly the line it always
        # did, and an absent note therefore means "nothing was identified by
        # fingerprint and no box erred" rather than "this run did not look".
        # Box errors belong HERE rather than on a per-file reason: a box being
        # unreachable is one fact about the run, not one fact per file, and
        # this closing line is the one message `JobRunner` keeps.
        note = ""
        if by_fingerprint or box_errors:
            note = "; %d identified by fingerprint" % len(by_fingerprint)
            if box_errors:
                note += " (box errors: %s)" % "; ".join(box_errors)
        # The gone sweep's own note, appended to the CLOSING line and to no
        # other, because the runner keeps exactly one message: `JobRunner._log`
        # assigns `state.message`, so a count logged when the sweep ran would
        # be overwritten by the first file's progress line and reach nobody.
        # A count that drops with nobody acting reads as a bug, so the run that
        # caused the drop has to say so where the drop is still readable.
        #
        # Always present, unlike `note` above, and that is the difference
        # between the two: a fingerprint pass that did nothing did not look,
        # whereas this sweep looks on every single run, so an absent clause
        # here could only ever mean this code did not run. `0 marked` with no
        # problem named is the honest report of a library nothing has been
        # deleted from, and it is not the same statement as a sweep that could
        # not ask -- which names its reason instead of a count.
        if sweep.problem is None:
            note += "; %d marked gone" % sweep.marked
        else:
            note += "; nothing marked gone (%s)" % sweep.problem
        # Reported beside `lookups` rather than folded into it: these are the
        # queries the per-title FALLBACK issued, one per (file, store) for
        # files the cheap pass could not resolve, and a person weighing the
        # file limit needs to see that second, conditional multiplier rather
        # than infer it from a single total. Counted as queries issued — a
        # file that spent one against each of two stores counts two.
        ctx.log("finished: %d proposed, %d muted, %d refused, %d errors, "
                "%d lookups, %d per-title fallback queries; %s%s"
                % (tally["proposed"], tally["muted"], tally["refused"],
                   tally["errors"], lookups, fallbacks, selection, note))

    def _pool(self):
        """Everything this run is offered, and which of it the marker tag
        added -- see the module-level `pool_scenes`, which does the actual
        work and carries the full docstring. Kept as a thin instance method
        so callers inside this class do not have to thread `self._stash` and
        `self._marker` through by hand.

        Fetched WHOLE, deliberately: `limit` belongs to `select`, which
        applies it after the narrowings. Passing it to either read would limit
        at the SOURCE, so a batch of 50 would be the first 50 files overall
        and the muted and already-proposed ones among them would eat the
        budget. The accepted cost is one query for each set rather than a page
        of it.
        """
        return pool_scenes(self._stash, self._marker)

    def _identify_batch(self, selected):
        """Ask the configured boxes about the whole batch, and never let that
        question cost the batch.

        Returns `(identified, box_errors)` — empty and `()` when no
        `identify` was given, which is every install with no box configured.

        A raising `identify` is caught here and reported as one error rather
        than ending the run. That is the same reasoning
        `identify_by_fingerprint` applies to a single box that raises, one
        level up: reading the server's box configuration can fail on its own,
        and a scan that died there would lose the text path for every file in
        the batch over an addition that is only ever supposed to save some of
        them a lookup.
        """
        if self._identify is None or not selected:
            return {}, ()
        try:
            result = self._identify([str(scene["id"]) for scene in selected])
        except Exception as exc:
            return {}, ("fingerprint lookup failed: %s: %s"
                        % (type(exc).__name__, exc),)
        return dict(result.identified), tuple(result.errors)

    def _record(self, scene, outcome):
        """Record one file's outcome through the store, and name the bucket
        it fell in.

        One place, shared by the fingerprint pass and the text path, because
        the three kinds of "no proposal" mean the same things and call for
        the same writes whichever path produced them — and a second copy of
        this branch would be free to disagree about which one mutes.
        """
        subject_id = str(scene["id"])
        if outcome.mute_reason is not None:
            # Through the store, with the reason, so a later reader learns
            # what the verdict rested on rather than only that there was
            # one. One reason reaches here now (`MUTE_NO_CANDIDATES`) and
            # the parameter stays: a mute with no reason is a row a person
            # cannot judge, and the constant is what stops a second one
            # being added by copy-paste instead of on purpose.
            self._store.mute(SUBJECT_TYPE, subject_id,
                             reason=outcome.mute_reason)
            return "muted"
        if outcome.error is not None:
            return "errors"
        if outcome.proposal is None:
            # Through the store too, so a refusal — the one outcome a
            # person can most often fix (rename a file, add an alias,
            # move the threshold) — is visible somewhere other than
            # this job's own log. Keyed by subject, not recorded as a
            # proposal; see the block above `Store.record_refusal`
            # for why reusing `item` here would re-propose the same
            # unresolved file as a fresh row every night.
            self._store.record_refusal(SUBJECT_TYPE, subject_id,
                                       _primary_path(scene), outcome.reason,
                                       stores=outcome.stores)
            return "refused"
        return "proposed"

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
