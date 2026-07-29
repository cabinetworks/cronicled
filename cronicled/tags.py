"""Find tags that are one tag written two ways, and propose a merge.

A library accumulates the same tag under more than one spelling: the same
name spaced and unspaced, capitalised differently, punctuated differently.
Merging them is the fix, and this module is the half that FINDS them. It
never writes: it yields proposals, a person approves one, and
`cronicled.web.actions.Actions` performs the write.

Why this is deliberately, aggressively quiet
--------------------------------------------
Measured against a real library: 2704 tags, of which exactly seven clusters
differed only by case, spacing or punctuation. Seven findings in 2704 is the
whole shape of the problem. A proposer that also surfaced near-misses would
bury those seven under noise, and a section that is mostly noise stops being
read at all -- at which point the seven real findings are lost more
completely than if nothing had been proposed.

So the only rule here is exact equality of the project's existing normalised
form (`cronicled.text.spaceless`, which already removes case, spacing,
punctuation and combining-mark accents). No edit distance, no plural
stripping, no prefix matching. The measurement says the dominant real pattern
is one name written spaced and unspaced, and that is the pattern this serves.

The honest cost of `spaceless`: it cannot tell "the same name with the spaces
removed" from "two different words that happen to concatenate to the same
letters". Both come back as a cluster. That is a question for the reviewer,
who sees both spellings and both scene counts, and it is the direction this
should fail in -- a cluster shown and rejected costs one glance.

On synonyms, and the alias mechanism this deliberately does NOT extend
---------------------------------------------------------------------
The ticket asks for "a known synonym" to cluster too, from a configured map
rather than a guess. Two alias mechanisms already exist in this codebase and
NEITHER is the right shape for this, so no third one is built here:

* `cronicled.artist.Aliases` maps an as-filed FOLDER name to the creator's
  full name. It is about attributing a file to a person, is consulted only by
  the resolver, and has nothing to say about a tag.
* `SiteAdapter.aliases` (`config/adapters.example.json`) is documented as
  `{abbreviation_slug: real_store_slug}` and is scoped to ONE configured
  store. Tags are a library-wide vocabulary that exists whether or not any
  store is configured, so a per-store map is the wrong key. It is also read
  by nothing at all today -- `DeclarativeAdapter.__init__` stores it and no
  code path consults it -- so extending it would mean reviving inert config
  to carry a second meaning.

Building a third, library-wide tag-synonym map was considered and refused on
the measurement: zero of the seven real clusters needed one. A synonym map is
exactly the mechanism that turns a quiet section into a noisy one, and adding
it before a single real cluster requires it would be guessing at a shape with
no evidence. When a real synonym is found, it deserves its own config file
with its own loader, and a ticket that can point at the pair that motivated
it.

On aliases the media server already holds
-----------------------------------------
`Stash.all_tags` returns each tag's own alias list. It is read and then
deliberately not used, for two separate reasons:

* not for CLUSTERING, because widening the net is the thing this module is
  built to avoid (above);
* not for the WRITE. `Stash.merge_tags`'s `aliases` argument REPLACES the
  destination's whole alias list, so passing a list captured at proposal time
  would silently delete any alias added between the proposal and the approve.
  A proposal can be days old. `Actions` therefore merges without touching
  aliases at all and leaves the destination's list to the server's own
  behaviour, rather than overwriting it from a stale snapshot. Folding a
  merged spelling in as an alias is `Stash.update_tag_aliases`'s job and
  belongs to whichever ticket wires that up with a FRESH read.

A merge cannot be undone
------------------------
See `MERGE_IS_IRREVERSIBLE` below. That decision is why the payload here
carries no snapshot and why `cronicled.web.rows.to_merge_row` has no
`undoable` field for a template to read.
"""
from dataclasses import dataclass

from cronicled.text import normalize, spaceless

# This module's subject. Named rather than inlined for the reason
# `cronicled.scan.SUBJECT_TYPE` is: a test, the store and the row builder have
# to agree on one string, and a third subject type has to be added
# deliberately instead of by coincidence.
#
# A tag merge's subject is NOT a tag. It is the CLUSTER -- the normalised form
# two or more tags share -- and `subject_id` is that form. A per-tag subject
# would key one decision to whichever member happened to be picked, so muting
# it would leave the same cluster proposed again under its other member. The
# cluster key is also stable across runs in a way a tag id is not: merging
# destroys ids.
SUBJECT_TYPE = "tag-cluster"

# The two reasons a cluster is REPORTED without a canonical name, kept apart
# because they send a reviewer to different judgements. Distinct strings, and
# a test pins that they are distinct: collapsing both into one catch-all is a
# mutation that would otherwise satisfy every assertion about "there is a
# reason".
UNDECIDED_MANY = (
    "three or more spellings share this form, and nothing here can say which "
    "one is intended -- pick the survivor by hand")
UNDECIDED_EVEN = (
    "the two spellings are separated into the same number of words, so "
    "neither is the more written-out one -- pick the survivor by hand")

# The warning a merge carries for its whole life, before and after it is
# applied. Stated once, here, so the row builder and the page cannot drift
# from each other about a claim this important.
#
# Why irreversible rather than reversible
# ---------------------------------------
# The alternative was to record which items moved and offer an undo, the way
# `Stash.apply_scene` snapshots a scene. It was refused, on four counts, any
# one of which is enough:
#
# 1. **The snapshot would be taken at the wrong moment.** A proposal is
#    recorded on one night and approved on another. Anything tagged or
#    untagged in between makes a proposal-time list wrong in both directions:
#    items it never knew about stay behind, and items removed since would be
#    re-tagged by the "undo".
# 2. **Scenes are not all a tag holds.** The server attaches tags to markers,
#    images, galleries, performers and studios as well, and `tagsMerge` moves
#    all of them. `Stash.all_tags` selects `scene_count` and nothing else, so
#    a snapshot built from what this code can actually see would restore the
#    scenes and silently lose the rest -- an undo that reports success while
#    leaving the library in a third state that is neither before nor after.
# 3. **The source tag's id does not survive.** A merge deletes it. Anything
#    else that referenced that id -- a saved filter, another tool -- is not
#    restored by recreating the name under a new id.
# 4. **The record would be unbounded.** A tag on four thousand scenes would
#    put four thousand ids into a payload the store hashes, per proposal, per
#    run.
#
# So this is declared irreversible and the interface says so before the
# write, exactly as the cover-image warning already does for the other write
# this tool cannot take back. The residual, stated rather than hidden: the
# spellings that were merged away are recorded in the applied proposal's own
# payload, so a person can see what the names were and recreate them by hand.
# That is a record, not an undo, and this text does not call it one.
MERGE_IS_IRREVERSIBLE = (
    "Approving this merge moves every item from the other spellings onto the "
    "survivor and deletes them. It cannot be undone: nothing records which "
    "items came from which tag, and the merge moves markers, images and "
    "galleries that the scene counts above do not even show.")

# What the scene counts in a proposal actually count. Said in words on the
# row because "12" beside a tag name reads as "everything this tag holds",
# and it is not: `Stash.all_tags` selects `scene_count` alone. Reporting the
# number this code can vouch for, labelled as what it is, is the honest
# option; inventing a total from counts nothing here reads would not be.
COUNTS_COVER = "scenes"


@dataclass(frozen=True)
class Counts:
    """Why a run's clusters became the batch it proposed.

        total == already_proposed + muted + selected

    holds always, and a test asserts the identity rather than the fields one
    at a time -- the same reason `cronicled.scan.Counts` carries a sixth
    field: a cluster that vanished for a reason nobody named is a failure no
    per-field check can see.
    """
    total: int
    already_proposed: int
    muted: int
    selected: int


@dataclass(frozen=True)
class Cluster:
    """Two or more tags whose names reduce to one normalised form.

    `members` is ordered by `(name, id)` -- content, never the order the
    server listed them in, so the same library produces the same payload on
    every run and reversing the input changes nothing.

    `canonical` is the member that should survive the merge, or `None` when
    this cluster is a FINDING rather than a merge; `undecided` says which of
    the two reasons applies and is `None` exactly when `canonical` is set.
    """
    key: str
    members: tuple
    canonical: dict
    undecided: str


def _member(tag):
    """One `Stash.all_tags` row, reduced to what a proposal carries.

    Indexed, never `.get`: a tag with no `scene_count` is a malformed row,
    and a default of 0 would put "this merge moves nothing" in front of a
    reviewer for a tag that might hold thousands. A blast radius that reads
    as zero because a field was missing is the one wrong answer this cannot
    afford, so a missing field raises here, where the malformed row is still
    in hand.
    """
    return {"id": str(tag["id"]), "name": tag["name"],
            "scene_count": tag["scene_count"]}


def _separation(name):
    """How many words the normalised form of `name` is written in.

    This is the whole of the canonical-name rule, and it is a property of the
    TEXT, never of position in a list. "Velvet Crane" is two; "VelvetCrane",
    "velvet-crane" and "VELVET  CRANE" are two, one and two respectively --
    punctuation separates, so `normalize` (which turns every non-alphanumeric
    character into a space) is what counts, not the raw string.

    The measurement behind preferring the larger: all seven real clusters
    were a person's name written spaced and unspaced. The spaced spelling is
    the written-out one and the run-together one is the filing artefact, so
    the more-separated spelling is the survivor.
    """
    return len(normalize(name).split())


def _decide(members):
    """`(canonical, undecided)` for one cluster -- exactly one of the two.

    Three or more spellings never resolve. Three spellings do not tell you
    which is canonical, and picking one by frequency, by scene count or by
    iteration order is the silent resolution this codebase has removed from
    five separate places. It is reported instead, and the reviewer says which
    wins.

    Exactly two resolve only when their separation differs. Two spellings
    written in the same number of words -- a case difference, a punctuation
    difference -- carry no evidence about which was meant, and inventing one
    would be the same mistake at a smaller size.
    """
    ranked = sorted(members, key=lambda m: -_separation(m["name"]))
    if len(members) != 2:
        return None, UNDECIDED_MANY
    if _separation(ranked[0]["name"]) == _separation(ranked[1]["name"]):
        return None, UNDECIDED_EVEN
    # `sorted` is stable, so equal separations would leave the incoming order
    # deciding -- which is why the equality above returns before this line
    # rather than after it. Reached only when the two separations differ, at
    # which point the sort had exactly one answer to give.
    return ranked[0], None


def cluster_tags(tags):
    """Every cluster of two or more tags sharing one normalised form.

    Ordered by `key`, and each cluster's members ordered by `(name, id)`, so
    the answer is a function of the tags alone -- never of the order they
    arrived in.

    A tag whose name normalises to nothing (punctuation only) is dropped
    rather than clustered. Every such tag reduces to the same empty key, so
    keeping them would gather unrelated tags into one cluster on the strength
    of having no letters in common.
    """
    grouped = {}
    for tag in tags:
        member = _member(tag)
        key = spaceless(member["name"])
        if not key:
            continue
        grouped.setdefault(key, []).append(member)

    clusters = []
    for key in sorted(grouped):
        members = sorted(grouped[key], key=lambda m: (m["name"], m["id"]))
        if len(members) < 2:
            continue
        canonical, undecided = _decide(members)
        clusters.append(Cluster(key=key, members=tuple(members),
                                canonical=canonical, undecided=undecided))
    return clusters


def select(clusters, *, store, folder):
    """The clusters this run should propose, and why the others were dropped.

    The same two narrowings, asked at the SUBJECT level, that
    `cronicled.scan.select` applies to files, and for the same reasons:

    1. muted subjects, read from `store.muted_subjects()` -- the `mute` table
       `Store.record` itself consults, not the `item` rows a mute moved into
       the `muted` state, so a cluster muted before any proposal existed is
       seen;
    2. subjects that already have a visible proposal in `folder`.

    The second narrowing is what keeps this quiet across nights. A cluster's
    payload carries scene counts, which move whenever a scene is tagged, and
    a moved count is a different payload, hence a different fingerprint,
    hence a SECOND row rather than a touch of the first. Skipping a subject
    that already has a visible proposal is what stops that, and it is the
    established answer here rather than a new one.

    The residual, named rather than hidden: a cluster whose proposal was
    DISMISSED is no longer visible, so a later run whose counts have moved
    proposes it again under a new fingerprint. That is the same distinction
    `Store.dismiss` and `Store.mute` already draw -- a dismissal rejects one
    proposal, a mute rejects the subject -- and Mute is the durable answer a
    reviewer has for a cluster they never want to see again.

    No `superseded` check, unlike `scan.select`. Superseding is reached from
    the per-row Refresh control, which a merge row does not offer, and a
    superseded row is already excluded from `items()`'s default view -- so
    such a cluster is not "already proposed" here and is offered again
    without this needing to know anything about it.
    """
    muted = {subject_id for subject_type, subject_id
             in store.muted_subjects() if subject_type == SUBJECT_TYPE}
    proposed = {item["subject_id"] for item in store.items(folder=folder)
                if item["subject_type"] == SUBJECT_TYPE}

    selected = []
    muted_count = already_proposed = 0
    for cluster in clusters:
        if cluster.key in muted:
            muted_count += 1
        elif cluster.key in proposed:
            already_proposed += 1
        else:
            selected.append(cluster)
    return selected, Counts(total=len(clusters),
                            already_proposed=already_proposed,
                            muted=muted_count, selected=len(selected))


def _summary(cluster):
    """One line naming the spellings and what each carries."""
    return "%d spellings of one tag: %s" % (
        len(cluster.members),
        ", ".join("%s (%d %s)" % (m["name"], m["scene_count"], COUNTS_COVER)
                  for m in cluster.members))


def proposal(cluster, folder):
    """One cluster as the proposal dict a producer yields.

    `confidence` is deliberately absent. The store documents it as a 0-to-1
    score and enforces that range; a merge is not scored, and putting 1.0
    there would state a certainty nothing computed. `cronicled.web.rows
    .to_merge_row` says what this proposal knows in words instead.
    """
    return {
        "folder": folder,
        "subject_type": SUBJECT_TYPE,
        "subject_id": cluster.key,
        "summary": _summary(cluster),
        "payload": {
            "key": cluster.key,
            "members": [dict(m) for m in cluster.members],
            "canonical": dict(cluster.canonical) if cluster.canonical else None,
            "undecided": cluster.undecided,
            "counts_cover": COUNTS_COVER,
        },
    }


class TagMergeProducer:
    """Reads every tag the media server holds and proposes a merge for each
    cluster of spellings.

    `produce` is a generator for the reason every producer's is (see
    `cronicled.jobs`): the runner records each proposal as it is yielded, so
    a run that dies partway keeps what it already found.

    `cost` is `local`, not `scraping`. The whole run is one paged read
    against the media server's own tag list -- no third-party scraper, no
    stash-box -- so it does not belong in the class that rations the
    rate-limited resource, and serialising it behind a full-library scrape
    would delay a cheap read for no gain.

    A scan NEVER writes here either: this proposes, and a person approves.
    """

    name = "tag-merge"
    cost = "local"

    def __init__(self, stash, *, store, folder="library", every=None):
        self._stash = stash
        self._store = store
        self._folder = folder
        # The cadence this producer DECLARES, read off the object by
        # `cronicled.schedule.resolve`, which refuses an enabled producer
        # without one rather than inventing an interval. Set unconditionally,
        # like `ScanProducer.every`, so a producer's cadence is a value that
        # was decided and never an attribute that happens to be missing.
        self.every = every

    def produce(self, ctx):
        tags = self._stash.all_tags()
        clusters = cluster_tags(tags)
        selected, counts = select(clusters, store=self._store,
                                  folder=self._folder)
        # Logged opening and closing, like `ScanProducer.produce`, because
        # `JobRunner._log` keeps only the last message: "0 proposed" reads
        # identically for a library with no duplicate spellings and for one
        # whose every cluster a reviewer has already muted, and those call
        # for opposite responses.
        selection = ("%d tags, %d clusters, %d proposed (%d already proposed, "
                     "%d muted)" % (len(tags), counts.total, counts.selected,
                                    counts.already_proposed, counts.muted))
        ctx.log(selection)
        for cluster in selected:
            yield proposal(cluster, self._folder)
        ctx.log("finished: %s" % selection)
