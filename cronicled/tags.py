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
* `SiteAdapter.aliases` (`config/adapters.example.json`) is the SAME
  folder-to-creator map, declared per store because `adapters.json` is the
  only configuration a store has;
  `cronicled.runscan.configured_aliases` pools every configured adapter's
  into the one `Aliases` a scan resolves against. It is therefore about
  attributing a file to a person too, and says nothing about a tag. Tags are
  a library-wide vocabulary that exists whether or not any store is
  configured, so extending a map keyed on a creator's folder would mean
  giving an existing key a second, unrelated meaning.

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

from cronicled import performer_tags, tag_descriptions, tag_hygiene
from cronicled.stashbox import StashBox, base_url
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

    `description` is carried for the same reason and read the same way. A
    merge deletes the losing spellings, so a description that lives on one of
    them is destroyed by the merge unless something notices it first (see
    `cronicled.tag_descriptions.merge_description`); a missing field read back
    as "no description" is exactly the reading under which it disappears
    without anybody being told.
    """
    return {"id": str(tag["id"]), "name": tag["name"],
            "scene_count": tag["scene_count"],
            "description": tag["description"]}


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


def _describe_summary(counts, dropped_keys):
    """The description half of the pass, in one line.

    `dropped_keys` -- alias keys two of one source's own tags claimed with
    different descriptions -- is reported HERE rather than only where it is
    found, and that is the whole reason it is threaded this far. `JobRunner
    ._log` keeps one field: the line `_indexes` writes when it drops a key is
    overwritten by this one before any operator sees it, so a count logged
    only there is a count nobody can ever read. Reported only when it is
    non-zero, because a source with no colliding aliases has nothing to say
    and a permanent "0 dropped" is noise that stops being read.

    REQUIRED, with no default, on the same terms as `proposal`'s `indexes`: a
    caller who forgot it would compose a line saying nothing was dropped, and
    "this pass dropped no ambiguous keys" and "this caller did not pass the
    count" must not be able to render as the same sentence.

    The `beyond_reach` figure is worded by whether every configured source was
    actually read, and the two wordings are deliberately not the same sentence
    with a number changed. "no configured source describes them" is a claim
    about the sources; when one of them failed or was read only partly, that
    claim has not been established for a single one of those tags, and the
    line says what it really knows instead -- otherwise a night of network
    trouble reports itself as a permanent backlog and gets planned around.
    """
    if counts.boxes_unread:
        reach = ("%d found nothing in the sources that answered, but %d "
                 "source(s) could not be read in full, so that is not a "
                 "verdict on them" % (counts.beyond_reach, counts.boxes_unread))
    else:
        reach = ("%d no configured source describes" % counts.beyond_reach)
    dropped = ""
    if dropped_keys:
        dropped = (", %d source alias key(s) dropped as ambiguous"
                   % dropped_keys)
    return ("%d descriptions proposed (%d tags already described, %d left to "
            "their merge, %s%s)" % (counts.outstanding, counts.described,
                                    counts.clustered, reach, dropped))


def _reconcile_summary(counts):
    """The performer half of the pass, in one line.

    Carried in the CLOSING line rather than logged where it is computed, for
    the reason `_describe_summary` is: `JobRunner._log` keeps one field, so
    every line this pass writes before its last is overwritten before an
    operator sees any of it. A count logged mid-pass is a number the code
    computes and nobody can read.

    The two suppressions are reported SEPARATELY -- `muted` and
    `already_proposed` are a person's own standing decisions, `unused` is a
    tag with no scenes to move -- because "0 proposed" reads identically for a
    library with no mis-filed tags and for one whose every finding a reviewer
    has already answered, and those call for opposite responses.

    `ambiguous` is mentioned only when it is non-zero: it is a subset of what
    was proposed, and a permanent "0 name two performers" is noise that stops
    being read.
    """
    ambiguous = ""
    if counts.ambiguous:
        ambiguous = (", %d of them name two or more performers and cannot be "
                     "approved as they stand" % counts.ambiguous)
    return ("%d tags also name a performer, %d reconciliations proposed "
            "(%d already proposed, %d muted, %d left to their merge, %d on no "
            "scenes%s)"
            % (counts.matched, counts.outstanding, counts.already_proposed,
               counts.muted, counts.clustered, counts.unused, ambiguous))


def _hygiene_summary(counts):
    """The low-count half of the pass, in one line.

    Carried in the CLOSING line rather than logged where it is computed, for
    the reason `_describe_summary` and `_reconcile_summary` are: `JobRunner
    ._log` keeps ONE field, so every line this pass writes before its last is
    overwritten before an operator sees any of it. A count logged mid-pass is a
    number the code computes and nobody can read.

    The two populations are reported SEPARATELY, because they are not the same
    finding: one changes no scene and the other changes one.

    A night that examined nothing says exactly that INSTEAD of reporting
    suppressions it never counted. `tag_hygiene`'s own docstring is why the pass
    withholds on an unread source; this is why that night cannot be mistaken for
    a library with nothing to clean up, which is the one reading that would get
    a permanently broken source planned around.
    """
    if counts.withheld:
        return ("%d tags on nought or one scene were not examined: a "
                "configured source could not be read in full, and 'no source "
                "describes this tag' is half the reason for proposing its "
                "deletion" % counts.withheld)
    return ("%d tags on nought or one scene, %d proposed for deletion (%d on "
            "no scenes, %d on one scene; %d already described, %d described by "
            "a source, %d left to their merge, %d left to their performer, %d "
            "already proposed, %d kept by hand)"
            % (counts.low, counts.outstanding, counts.no_scenes,
               counts.one_scene, counts.described, counts.sourced,
               counts.clustered, counts.reconciled, counts.already_proposed,
               counts.kept))


def proposal(cluster, folder, indexes):
    """One cluster as the proposal dict a producer yields.

    `confidence` is deliberately absent. The store documents it as a 0-to-1
    score and enforces that range; a merge is not scored, and putting 1.0
    there would state a certainty nothing computed. `cronicled.web.rows
    .to_merge_row` says what this proposal knows in words instead.

    `indexes` -- the configured sources, in order -- is REQUIRED and has no
    default. A merge is where a description gets destroyed, so what the
    survivor should end up with is part of what a person is approving, and a
    caller that forgot the argument must not be able to build a proposal that
    silently says "no source has anything" about sources it never asked. An
    empty sequence is a legitimate value and says the opposite: there were no
    sources.
    """
    description = tag_descriptions.merge_description(
        cluster.members, cluster.canonical, indexes)
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
            "description": description.as_payload(),
        },
    }


class TagMergeProducer:
    """Reads every tag the media server holds ONCE and proposes, from that one
    read, every half of what is wrong with a library's tag vocabulary: a merge
    for each cluster of spellings, a description for each blank tag a
    configured stash-box already describes, and a reconciliation for each tag
    that is really a performer filed in the wrong namespace (see
    `cronicled.performer_tags`, and `_reconcile` below), and a deletion for each
    tag that classifies nought or one scene and that nothing anywhere describes
    (see `cronicled.tag_hygiene`, and `_hygiene` below).

    ONE PASS, NOT TWO, and that is a decision rather than a convenience. The
    two findings meet on the same tag constantly -- a merge deletes spellings,
    and a description proposal attached to a spelling that is about to be
    deleted is a row pointing the opposite way from the row beside it. Reaching
    a reviewer as two unrelated rows is how a person approves both and gets a
    result neither described. So a tag inside a cluster is never offered a
    description of its own here: what the survivor should end up with is part
    of the merge proposal (`cronicled.tag_descriptions.merge_description`),
    where it is judged together with the merge that puts it at risk.

    `produce` is a generator for the reason every producer's is (see
    `cronicled.jobs`): the runner records each proposal as it is yielded, so
    a run that dies partway keeps what it already found.

    `cost` IS `box`, and it used to be `local`. Reading each configured
    stash-box's whole tag catalogue is precisely the rate-limited resource
    `COST_CLASS_LIMITS["box"]` exists to ration -- the same catalogue reads
    `cronicled.stashbox_scan.StashBoxCheckProducer` is classed for -- and a
    pass that pages a public service under the unlimited class would spend
    that budget where nothing counts it. The cost of being honest about it is
    that a library with no box configured still queues under a limit of one;
    that is a pass waiting its turn, which is recoverable, against a rate-limit
    incident, which is not.

    The performer half adds no third-party call and so does not change that
    class. `Stash.performers_with_aliases` and `Stash.tagged_scenes` are reads
    of the MEDIA SERVER -- the same server `all_tags` is already read from,
    which is this operator's own machine and is rationed by nothing. `box`
    stays the honest class because the description half still pages a public
    service.

    A pass NEVER writes: this proposes, and a person approves.
    """

    name = "tag-merge"
    cost = "box"

    def __init__(self, stash, *, store, folder="library", every=None,
                 box_client=None):
        self._stash = stash
        self._store = store
        self._folder = folder
        # How a configured box's address and key become something to ask.
        # Injected so a test can exercise the whole pass without a socket,
        # exactly as `Stash`'s own transport is; the default is the one real
        # client (`cronicled.stashbox.StashBox`), never a second hand-rolled
        # one -- this project has been bitten by two clients for one service
        # disagreeing.
        self._box_client = box_client if box_client is not None else StashBox
        # The cadence this producer DECLARES, read off the object by
        # `cronicled.schedule.resolve`, which refuses an enabled producer
        # without one rather than inventing an interval. Set unconditionally,
        # like `ScanProducer.every`, so a producer's cadence is a value that
        # was decided and never an attribute that happens to be missing.
        self.every = every

    def _indexes(self, ctx):
        """`(indexes, unread, dropped_keys)`: every configured source's tag
        catalogue in configured order, how many could not be read in full, and
        how many alias keys were dropped as ambiguous across all of them.

        A LIST, and the order is the media server's own -- which is the
        operator's configured order, and the order `find_description` treats
        as the preference. It is never keyed by box name on the way through:
        the order is the answer, and a mapping would put it at the mercy of a
        hash.

        A source that raises is skipped and the rest are asked. Its failure is
        evidence about the network, not about any tag, so it must not be able
        to turn into "no source describes this" -- which is why it is COUNTED
        as well as logged, and why the count travels with the answers rather
        than being logged and forgotten. A source read only partly counts the
        same way, for the same reason: the page that was not read is exactly
        where the missing description would be.

        `dropped_keys` travels back for the same reason and not merely for
        symmetry. `JobRunner._log` keeps only the LAST message a job wrote, so
        every line logged in this loop is overwritten before the job finishes;
        a count that stayed here would be a number the code computes, states a
        reason for, and no operator can ever read. The per-source lines are
        kept for anyone watching a run live, and the total is returned so the
        closing line can carry it.
        """
        indexes, unread, dropped_keys = [], 0, 0
        for box in self._stash.stash_box_credentials():
            name = box["name"]
            try:
                catalogue = self._box_client(
                    base_url(box["endpoint"]), box["api_key"]).all_tags()
            except Exception as exc:
                unread += 1
                ctx.log("%s could not be read (%s: %s); its tags are not in "
                        "this pass, and nothing here says a tag it might "
                        "describe has no description"
                        % (name, type(exc).__name__, exc))
                continue
            if not catalogue.complete:
                unread += 1
                ctx.log("%s was read only partly (%d tags); what it holds "
                        "beyond them is unknown, not absent"
                        % (name, len(catalogue.tags)))
            index = tag_descriptions.index_box(name, catalogue.tags)
            if index.ambiguous:
                dropped_keys += len(index.ambiguous)
                ctx.log("%s: %d key(s) two of its tags claim with different "
                        "descriptions, dropped rather than guessed at"
                        % (name, len(index.ambiguous)))
            indexes.append(index)
        return indexes, unread, dropped_keys

    def produce(self, ctx):
        tags = self._stash.all_tags()
        indexes, boxes_unread, dropped_keys = self._indexes(ctx)
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
            yield proposal(cluster, self._folder, indexes)

        described, counted = self._describe(tags, clusters, indexes,
                                            boxes_unread)
        yield from described

        reconciled, reconciled_counts = self._reconcile(tags, clusters)
        yield from reconciled

        # The performer half's LIVE rows, which the low-count half must not
        # duplicate. Two sources, both necessary: the reconciliations THIS run
        # yielded, and the ones an earlier run already put on the page (which
        # `_reconcile` skipped as `already_proposed`, so they are absent from
        # the list above while still being a row about that tag).
        #
        # A MUTED reconciliation is deliberately not in here. "Stop telling me
        # this tag is a performer" is a decision about that question and says
        # nothing about whether the tag is worth keeping, which is the
        # distinction every `SUBJECT_TYPE` in this package exists to draw.
        covered = {p["subject_id"] for p in reconciled}
        covered |= performer_tags.narrowings(self._store, self._folder)[1]
        unused, unused_counts = self._hygiene(tags, clusters, indexes,
                                              boxes_unread, covered)
        yield from unused
        ctx.log("finished: %s; %s; %s; %s"
                % (selection, _describe_summary(counted, dropped_keys),
                   _reconcile_summary(reconciled_counts),
                   _hygiene_summary(unused_counts)))

    def _hygiene(self, tags, clusters, indexes, boxes_unread, covered):
        """`(proposals, tag_hygiene.Counts)` for the low-count half of the
        pass: the tags on nought or one scene that nothing describes.

        THE SAME `all_tags` READ, the same clusters and the same source indexes
        the three halves above work from, which is why this lives inside this
        producer rather than in a pass of its own. It issues no request of any
        kind -- `tag_hygiene.evidence_against` looks up indexes already in
        hand -- so the `box` cost class this pass is in is unchanged by it.

        Built as a list rather than yielded straight out, so the closing log
        line can carry the whole tally: `JobRunner._log` keeps only the LAST
        message a job wrote.

        The order of the checks is the order of the argument, cheapest and most
        conclusive first, and two of them are load-bearing rather than
        cosmetic:

        * `boxes_unread` comes FIRST, before anything is examined, because on
          such a night the answer for every low-count tag is the same and it is
          "this pass cannot say". Counting the suppressions per-reason on a
          night when the evidence was never gathered would report a set of
          conclusions nothing reached.
        * the description checks come before the store, so a tag something
          already defines is never even looked up as a candidate.

        A tag in ANY cluster is skipped, not only one in a cluster this run
        proposed, exactly as `_describe` and `_reconcile` skip one -- and here
        for the sharpest reason of the three: the merge and the deletion are
        opposite answers to one question about the same name, and a person who
        approved both would delete a spelling the merge was about to move items
        onto.
        """
        clustered = {m["id"] for cluster in clusters for m in cluster.members}
        kept, proposed = tag_hygiene.narrowings(self._store, self._folder)

        proposals = []
        low = withheld = described = sourced = in_cluster = 0
        reconciled = kept_count = already = 0
        by_group = {group: 0 for group in tag_hygiene.GROUPS}
        for tag in tags:
            group = tag_hygiene.group_of(tag)
            if group is None:
                continue
            low += 1
            if boxes_unread:
                withheld += 1
                continue
            by_library, by_source = tag_hygiene.evidence_against(tag, indexes)
            if by_library:
                described += 1
                continue
            if by_source:
                sourced += 1
                continue
            tag_id = str(tag["id"])
            if tag_id in clustered:
                in_cluster += 1
                continue
            if tag_id in covered:
                reconciled += 1
                continue
            if tag_id in kept:
                kept_count += 1
                continue
            if tag_id in proposed:
                already += 1
                continue
            by_group[group] += 1
            proposals.append(tag_hygiene.proposal(tag, folder=self._folder))
        return proposals, tag_hygiene.Counts(
            low=low, withheld=withheld, described=described, sourced=sourced,
            clustered=in_cluster, reconciled=reconciled, kept=kept_count,
            already_proposed=already, outstanding=len(proposals),
            no_scenes=by_group[tag_hygiene.NO_SCENES],
            one_scene=by_group[tag_hygiene.ONE_SCENE])

    def _reconcile(self, tags, clusters):
        """`(proposals, performer_tags.Counts)` for the performer half of the
        pass: tags whose name is a performer's name or one of their aliases.

        THE SAME `all_tags` READ the merge and description halves work from,
        which is why this lives inside this producer rather than in a pass of
        its own. Two passes over one catalogue put two unrelated rows about
        one tag in front of a reviewer, on two cadences, with nothing relating
        them.

        Built as a list rather than yielded straight out, so the closing log
        line can carry the whole tally -- `JobRunner._log` keeps only the last
        message a job wrote.

        A tag in ANY cluster is skipped, exactly as `_describe` skips one, and
        for a sharper reason than that method has: a merge DELETES the losing
        spellings, so a reconciliation approved after the merge would name a
        tag id the server no longer has, and one approved before it would move
        the associations out from under the merge a reviewer is also looking
        at. Muting the merge does not settle it either; it only means nobody
        wants to be asked again.

        A tag on NO scenes is skipped. There is no association to move, and a
        row reporting a blast radius of nothing reads exactly like a
        reconciliation that is safe because it is small. This is also what
        stops an applied reconciliation coming back the next night: the apply
        takes the tag off every scene it was on.

        The scenes are read LAST, once per surviving tag, because that read is
        one request per tag and every check above it costs nothing. A tag a
        reviewer muted is never asked about.

        A description proposal for a tag matched here is NOT suppressed, and
        that is deliberate rather than an omission. `_describe` suppresses a
        clustered tag because a merge deletes the spelling the description
        would land on; a reconciliation deletes nothing, so the tag is still
        there afterwards and a description for it is still a description of
        that tag. The two rows do not point in opposite directions the way a
        merge and a description do.
        """
        index = performer_tags.index_performers(
            self._stash.performers_with_aliases())
        clustered = {m["id"] for cluster in clusters for m in cluster.members}
        muted, proposed = performer_tags.narrowings(self._store, self._folder)

        proposals = []
        matched = in_cluster = muted_count = already = unused = ambiguous = 0
        for tag in tags:
            matches = performer_tags.match_tag(tag, index)
            if not matches:
                continue
            matched += 1
            tag_id = str(tag["id"])
            if tag_id in clustered:
                in_cluster += 1
                continue
            if tag_id in muted:
                muted_count += 1
                continue
            if tag_id in proposed:
                already += 1
                continue
            scenes = [str(scene["id"])
                      for scene in self._stash.tagged_scenes(tag_id, None)[1]]
            if not scenes:
                unused += 1
                continue
            if len(matches) > 1:
                ambiguous += 1
            proposals.append(performer_tags.proposal(
                tag, matches, scenes, folder=self._folder))
        return proposals, performer_tags.Counts(
            matched=matched, clustered=in_cluster, muted=muted_count,
            already_proposed=already, unused=unused,
            outstanding=len(proposals), ambiguous=ambiguous)

    def _describe(self, tags, clusters, indexes, boxes_unread):
        """`(proposals, Counts)` for the description half of the pass.

        Built as a list rather than yielded straight out, so the closing log
        line can report the whole tally in one message: `JobRunner._log` keeps
        only the LAST thing a job said, and a count that is written before the
        proposals it counts would be overwritten by every line after it.

        A tag in ANY cluster is skipped, not only one in a cluster this run
        proposed. A tag with a duplicate spelling has an unsettled identity --
        which of two names it is has not been decided -- and describing it
        before that is settled attaches a definition to whichever half of the
        pair happens to survive. Muting the merge does not settle it either;
        it only means nobody wants to be asked again.
        """
        clustered = {m["id"] for cluster in clusters for m in cluster.members}
        proposals = []
        described = in_cluster = beyond_reach = 0
        for tag in tags:
            if tag_descriptions.has_description(tag):
                described += 1
                continue
            if str(tag["id"]) in clustered:
                in_cluster += 1
                continue
            found = tag_descriptions.find_description(tag["name"], indexes)
            if found is None:
                # NOT a proposal, and nothing derived from the tag's name, its
                # scenes or a similar tag's text. See
                # `cronicled.tag_descriptions`' own docstring: an invented
                # description cannot be told from a written one afterwards.
                beyond_reach += 1
                continue
            proposals.append(tag_descriptions.proposal(
                tag, found, folder=self._folder))
        return proposals, tag_descriptions.Counts(
            total=len(tags), described=described, clustered=in_cluster,
            outstanding=len(proposals), beyond_reach=beyond_reach,
            boxes_unread=boxes_unread)
