"""Tags that are a creator filed in the wrong namespace.

Measured against a real library: of the tags carrying no description and no
source in any configured stash-box, 376 also exist as a PERFORMER -- by name,
or by one of that performer's aliases -- and they carry 4031 scene-tag
associations between them. Twenty-six of them are on twenty or more scenes
each, so this is not a tail of strays.

WHY THIS IS NOT DUPLICATION
---------------------------
A tag and a performer are not two spellings of one thing. A performer record
carries a catalogue identity, aliases, a description and an image, and is what
every lookup in this project resolves against; a tag carries a name. So a
scene tagged with a creator's name instead of having the performer attached
does not appear under that performer, is linked to no catalogue entry, and is
invisible to `cronicled.artist`'s resolution -- which looks at performers.
Metadata that exists and is not doing its job.

WHAT IS PROPOSED, AND WHAT IS DELIBERATELY NOT
----------------------------------------------
The proposal is ONE step: attach the performer to every scene carrying the
tag, then take the tag off those scenes. Deleting the tag afterwards is a
SEPARATE decision and is not proposed here at all.

That split is not caution for its own sake. A tag may legitimately share a
name with a performer -- a studio and its owner, a series named after its
creator -- and deleting on a name match alone destroys a real distinction that
nothing here can see. It is also the reversibility boundary: attaching a
performer can be undone, removing a tag from a scene can be undone, deleting
the tag cannot. So the reversible half is what this offers, and the
irreversible half is left to a ticket that can argue for it.

A NAME MATCH IS EVIDENCE, NOT PROOF
-----------------------------------
Which is why the proposal names the performer it matched AND the alias it
matched through, and why it states how many scenes it would touch before it
touches them. A reviewer approving this is authorising a write to every one of
those scenes, and the two things they need are what the match was and how big
it is.

ONE TAG MATCHING TWO PERFORMERS IS A FINDING
--------------------------------------------
Both are reported and neither is chosen -- not the one with more scenes, not
the alphabetically first, not the first encountered. `AMBIGUOUS` is set, the
resolved performer is `None`, and the row offers no Approve, exactly as
`cronicled.tags` refuses a cluster with no agreed survivor. Two performers
answering to one name is the most useful thing a reviewer could be told about
such a tag, and picking silently and picking correctly look identical
afterwards.

HOW A TAG IS MATCHED
--------------------
Every performer's name and every alias the media server records for them are
normalised with `cronicled.text.spaceless` -- the SAME form
`cronicled.tags.cluster_tags` groups spellings by and `cronicled
.tag_descriptions.index_box` keys a source's catalogue by -- and the tag's own
NAME is looked up in that index once. One whole-library read of the performers
(`Stash.performers_with_aliases`), not a search per tag: a library's thousands
of tags against a server's thousands of performers would be one request per
tag for an answer one read already holds.

The tag's OWN aliases are deliberately not matched. This is the direction where
widening the net costs the most: a match here authorises a write to every scene
carrying the tag, and a tag alias somebody added to fold two content tags
together is not evidence about a creator. Uncertainty may withhold evidence; it
may never supply it.

An exact NAME match wins over an alias match for one performer, which is the
same precedence `Stash._find_first` already applies when it resolves a name
against the server. A performer whose name and one of whose aliases both
normalise to the tag's key is one performer either way, so the only thing at
stake is which surface the proposal quotes -- and the name is the stronger
thing to show a reviewer.

Two ALIASES of ONE performer normalising to the same key is agreement rather
than ambiguity: both name the same performer, and the choice is only which
spelling to quote. The server's own alias order decides it, and that is stated
here rather than left to be discovered.
"""
from dataclasses import dataclass

from cronicled.text import spaceless

# This module's subject: one TAG, by the media server's own id -- the thing a
# reviewer is deciding about, and the thing a mute should silence.
#
# Deliberately NOT `cronicled.tag_descriptions.SUBJECT_TYPE` ("tag"), even
# though the subject is a tag there too, and not `cronicled.tags.SUBJECT_TYPE`
# (whose subject is a cluster of spellings). "Stop offering me a description
# for this tag" and "stop telling me this tag is a performer" are two
# different standing decisions about one tag, and one subject type would make
# either mute silence both.
SUBJECT_TYPE = "tag-performer"

# The reason a matched tag is REPORTED without a performer to reconcile it
# with. Named once, here, so the producer, the row builder and the apply path
# cannot drift into two spellings of one refusal -- and phrased as its own
# sentence rather than as a generic "ambiguous", which is a string that
# satisfies every assertion about there being a reason.
AMBIGUOUS = ("two or more performers answer to this tag's name, and nothing "
             "here can say which of them the scenes belong to -- attach the "
             "right one by hand")

# What the scene count on a proposal counts, said in words for the reason
# `cronicled.tags.COUNTS_COVER` is: a bare number beside a tag name reads as
# "everything this tag holds", and this one is the scenes the reconciliation
# would write to and nothing else. A tag is also attached to markers, images
# and galleries, and none of those are touched or counted here.
COUNTS_COVER = "scenes"


@dataclass(frozen=True)
class Match:
    """One performer a tag's name resolves to, and how it got there.

    `alias` is the alias SURFACE the match came through, exactly as the server
    spells it, or `None` when the performer's own name matched. It is not
    decoration: "this tag is that performer's name" and "this tag is one of
    the eleven aliases somebody once typed for that performer" are different
    strengths of evidence, and a reviewer cannot tell them apart from the
    performer's name alone.
    """
    performer: dict
    alias: str


@dataclass(frozen=True)
class Counts:
    """Why a pass's matched tags became the reconciliations it proposed.

        matched == clustered + muted + already_proposed + unused + outstanding

    holds always, and a test asserts the identity rather than the fields one
    at a time -- the same reason `cronicled.scan.Counts`, `cronicled.tags
    .Counts` and `cronicled.tag_descriptions.Counts` all carry their own
    totals: a tag that vanished for a reason nobody named is a failure no
    per-field check can see.

    `matched` counts tags whose name answers to at least one performer, NOT
    every tag the pass read. The tags that match nothing are the overwhelming
    majority and they are not a backlog: nothing here will ever have anything
    to say about them, and a total that included them would report a few
    thousand items of outstanding work that no number of runs can move.

    `unused` is matched tags carrying no scenes. A tag on no scenes has
    nothing to reconcile -- there is no association to move -- so it is
    counted and skipped rather than proposed as a write to nothing. This is
    also what stops a reconciliation being re-proposed every night after it
    has been applied: the apply takes the tag off every scene it was on, so
    the next run finds it carrying none.

    `ambiguous` is the subset of `outstanding` that names two or more
    performers, and is deliberately NOT a term in the identity above: those
    are proposals, they are on the page, and they need a person. It is carried
    so the closing log line can say how many of a night's findings cannot be
    approved as they stand.
    """
    matched: int
    clustered: int
    muted: int
    already_proposed: int
    unused: int
    outstanding: int
    ambiguous: int


def _performer(row):
    """One `Stash.performers_with_aliases` row, reduced to what a proposal
    carries.

    `id` and `name` are INDEXED, never `.get`: the query selects both on every
    row it returns, so a row missing either is a malformed answer rather than
    a performer with no name. A blank name read back as "" would normalise to
    the empty key, which is the one key that would gather every unnamed
    performer into one collision and report an ambiguity that does not exist.

    `id` is coerced to `str` because it is compared against, and recorded as,
    the text the store holds -- `subject_id` is a `TEXT` column and
    `store.muted_subjects()` returns strings, so an integer arriving here
    would propose a tag whose reconciliation a reviewer had already muted.
    """
    return {"id": str(row["id"]), "name": row["name"]}


def index_performers(performers):
    """`{normalised surface -> (Match, ...)}` over every performer's name and
    every alias the server records for them.

    Each key's matches are ordered by `(performer name, performer id)` --
    CONTENT, never the order the server listed the performers in. A tag
    matching two performers is a finding that names both, and the pair must
    read the same on every run and be unchanged by reversing the input;
    ordering by arrival would make a reversed read a different payload, hence
    a different fingerprint, hence a second row about one tag.

    `alias_list` is INDEXED for the reason `_performer` indexes `name`: the
    query selects it, so a row without it is malformed. A performer with no
    aliases answers `[]`, which is ordinary.

    A surface that normalises to nothing -- punctuation only -- is dropped
    rather than keyed. Every such surface reduces to the same empty key, so
    keeping them would collect unrelated performers under one key and report
    two of them as answering to a tag on the strength of having no letters in
    common. `cluster_tags` and `index_box` drop them for the same reason.
    """
    claimed = {}
    for row in performers:
        performer = _performer(row)
        # Name FIRST, so the `already claimed` guard below keeps the name
        # match when an alias of the same performer normalises to the same
        # key. See this module's docstring on precedence.
        surfaces = [(row["name"], None)]
        surfaces.extend((alias, alias) for alias in row["alias_list"])
        for surface, alias in surfaces:
            key = spaceless(surface)
            if not key:
                continue
            by_performer = claimed.setdefault(key, {})
            if performer["id"] in by_performer:
                continue
            by_performer[performer["id"]] = Match(performer=performer,
                                                 alias=alias)
    return {key: tuple(sorted(
                by_performer.values(),
                key=lambda m: (m.performer["name"], m.performer["id"])))
            for key, by_performer in claimed.items()}


def match_tag(tag, index):
    """Every performer this tag's NAME answers to, or `()`.

    `tag["name"]` is indexed: `Stash.all_tags` selects it on every row.

    A tag whose name normalises to nothing matches nothing, and does so before
    the lookup rather than by happening to miss. `index_performers` already
    refuses to store an empty key, so the two mechanisms agree today -- but the
    empty key is the one value that could match every unnamed performer at
    once, and a lookup that relied on the index alone to be safe would become
    unsafe the moment anything built an index by another route.
    """
    key = spaceless(tag["name"])
    if not key:
        return ()
    return index.get(key, ())


def _summary(tag, matches, scenes):
    """One line naming the match and its blast radius.

    Three shapes, and they are three DISTINCT sentences rather than one with a
    number changed: a name match, an alias match and a two-performer finding
    send a reviewer to three different judgements. A catch-all phrasing that
    mentioned the performer and the count would satisfy every assertion about
    "the summary names the performer" while telling a reviewer nothing about
    which of the three they are looking at.
    """
    count = "%d %s" % (len(scenes), COUNTS_COVER)
    if len(matches) > 1:
        return ("%s: %d performers answer to this name (%s), on %s -- nothing "
                "here can say which"
                % (tag["name"], len(matches),
                   ", ".join(m.performer["name"] for m in matches), count))
    match = matches[0]
    if match.alias is None:
        return ("%s: also the performer %s, on %s"
                % (tag["name"], match.performer["name"], count))
    return ("%s: also the performer %s, through their alias %r, on %s"
            % (tag["name"], match.performer["name"], match.alias, count))


def proposal(tag, matches, scenes, *, folder):
    """One matched tag as the proposal dict a producer yields.

    `matches` and `scenes` are both REQUIRED and both refused when empty.
    A proposal with no match is a write to a performer nobody identified, and
    a proposal with no scenes is a row asking a person to authorise a write to
    nothing -- which reads on the page exactly like a reconciliation that is
    safe because it is small. The producer already skips both, so reaching
    here with either is a wiring error and raises where it can be seen.

    `scenes` is the blast radius AS MEASURED WHEN THE PASS RAN, and the payload
    says so by carrying the ids rather than a bare count: the count on the row
    is derived from this list, so the number a reviewer reads and the set it
    describes cannot drift apart. What the apply actually writes to is read
    FRESH at apply time (see `Stash.reconcile_tag_to_performer`) -- a proposal
    can be days old, and a scene tagged or untagged since is exactly what a
    proposal-time list would get wrong in both directions. The undo snapshot
    is likewise what the apply recorded, never this.

    `performer` and `ambiguous` are set exactly opposite each other, the same
    way `cronicled.tags.proposal` pairs `canonical` and `undecided`: a payload
    that named a performer AND a reason nothing could be decided would let a
    row offer Approve for a decision nothing made.

    `confidence` is `None` rather than a number. Nothing here is scored -- a
    tag's name either answers to a performer or it does not -- and a number
    would be one this module invented and then displayed in the same column,
    in the same type, as scores the scorer really produced.
    """
    if not matches:
        raise ValueError(
            "cannot propose a reconciliation for tag %r: no performer answers "
            "to it" % (tag["name"],))
    if not scenes:
        raise ValueError(
            "cannot propose a reconciliation for tag %r: it is on no scenes, "
            "so there is no association to move" % (tag["name"],))
    resolved = matches[0] if len(matches) == 1 else None
    return {
        "folder": folder,
        "subject_type": SUBJECT_TYPE,
        "subject_id": str(tag["id"]),
        "summary": _summary(tag, matches, scenes),
        "confidence": None,
        "payload": {
            # The NAME only. The tag's id is `subject_id` above, and one fact
            # gets one representation: a second copy in the payload could
            # disagree with it, and nothing would ever notice which of them the
            # write used. `web.rows` links, `web.actions` writes and
            # `Stash.revert_reconcile` cross-checks all read `subject_id`.
            "tag": {"name": tag["name"]},
            "performer": dict(resolved.performer) if resolved else None,
            "alias": resolved.alias if resolved else None,
            "matches": [{"performer": dict(m.performer), "alias": m.alias}
                        for m in matches],
            "ambiguous": None if resolved else AMBIGUOUS,
            "scenes": [str(scene) for scene in scenes],
            "counts_cover": COUNTS_COVER,
        },
    }


def narrowings(store, folder):
    """`(muted, proposed)`: the tag ids this pass must not propose again.

    The same two narrowings `cronicled.tags.select` applies to a cluster,
    asked at this module's own subject level -- and asked at THIS subject
    level for the reason `SUBJECT_TYPE` exists: a reviewer who muted a
    description proposal for a tag has said nothing about whether that tag is
    a performer.

    `muted` comes from `store.muted_subjects()` -- the `mute` table `Store
    .record` itself consults, not the items a mute moved into the `muted`
    state, so a tag muted before any proposal existed is seen.

    `proposed` is every tag that already has a VISIBLE proposal in `folder`,
    which includes an `applied` one. That is what stops a reconciliation being
    re-proposed the night after it was approved, independently of the tag
    having been left on no scenes by the apply -- two mechanisms, either
    sufficient, because a re-proposal here is a second row offering to write
    to thousands of scenes again.

    The residual, named rather than hidden: a proposal a reviewer DISMISSED is
    no longer visible, so a later run offers it again. That is the same
    distinction `Store.dismiss` and `Store.mute` already draw -- a dismissal
    rejects one proposal, a mute rejects the subject -- and Mute is the
    durable answer for a tag that must stop being offered.
    """
    muted = {subject_id for subject_type, subject_id
             in store.muted_subjects() if subject_type == SUBJECT_TYPE}
    proposed = {item["subject_id"] for item in store.items(folder=folder)
                if item["subject_type"] == SUBJECT_TYPE}
    return muted, proposed
