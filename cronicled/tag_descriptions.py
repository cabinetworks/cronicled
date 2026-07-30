"""Descriptions a stash-box already wrote, for tags the library leaves blank.

Measured before it was built, and the measurement is why this is small. A
library holds 2757 tags and 20 of them carry a description. "2737 need one" is
the wrong framing; broken down, only one part of it is description-shaped:

    948   a configured stash-box already describes it   <- THIS MODULE
    376   also exist as a performer                     <- a different question
    256   carry no scenes at all                        <- a different question
    793   carry exactly one scene                       <- a different question
    ~66   genuine content tags in real use              <- typed by a person
    few   workflow markers and store names              <- neither

So this fetches, and only fetches. It reads what a source already holds and
offers it for review. Everything else on that list is somebody else's ticket,
and the count this reports as outstanding is narrowed to match -- a backlog
figure that included the other four thousand would describe work no amount of
running this can ever clear, which is worse than no figure.

NOTHING HERE INVENTS A DESCRIPTION
----------------------------------
This is the whole of the module's caution and it has no exceptions. A tag no
configured source describes gets NO proposal and stays visibly blank.

A generated description reads exactly like a written one. Once it is in the
field, nothing -- not the page, not the next run, not a person looking at it
in a year -- can tell that no one ever wrote it. That is why none of the
following is done here, though each was available and each would have raised
the coverage number:

* composing a sentence out of the tag's own name, which produces a definition
  of a category by restating its label;
* summarising the scenes that carry the tag, which describes a sample rather
  than a category, and describes it differently every time the sample moves;
* falling back to a related or similarly-spelled tag's text, which is a
  sentence somebody wrote about something else.

Uncertainty may withhold evidence; it may never supply it. An empty
description is a visible gap that a person can fill. A fabricated one is an
invisible error that nobody will ever look for.

IMAGES ARE NOT IN SCOPE EITHER, for the same reason and a sharper one: no
configured source exposes one, none of the library's tags has one, and a still
lifted from one scene carrying a tag is not a definition of the category -- it
is one example, presented as the whole.

HOW A SOURCE IS ASKED
---------------------
Each configured box's whole tag catalogue is read once per pass
(`cronicled.stashbox.StashBox.all_tags`) and turned into an index here. It is
not a lookup per tag: a library's thousands of tags against a source's
thousands would be one request per tag against a rate-limited public service,
and the answers would be identical.

The index is keyed on `cronicled.text.spaceless` -- the same normalised form
`cronicled.tags` clusters spellings by -- over the source's tag NAME and every
ALIAS it lists for that tag. The alias keys are not a refinement: measured
against a real library, names alone matched far fewer tags than names plus
aliases, so an implementation that indexed names only would miss most of the
coverage this module exists for.

WHICH SOURCE WINS
-----------------
Configured order, first hit wins, and the order is the media server's own list
(`Stash.stash_box_credentials`), which is the operator's. Measured, the second
and third sources between them added thirty tags the first did not cover, so
the later ones are expected to contribute almost nothing -- but a differently
configured library could invert those numbers entirely, which is why all of
them are asked rather than just the best one.

Two sources both describing a tag is AGREEMENT, not a conflict: each is a
sentence somebody wrote about the same category, and neither is more true than
the other. Taking the first is a preference among equals and nothing here
reports it as a finding. That is a different situation from a MERGE bringing
two DIFFERENT descriptions of the same tag together inside one library, which
is a real disagreement and is reported as one -- see `merge_description`.

WITHIN one source, though, two DIFFERENT tags claiming one key -- an alias of
one colliding with the name of another -- is not agreement and not a
preference. It is the source saying two things, and whichever this picked
would be picked by iteration order. Such a key is dropped from the index
entirely: eliminating the ambiguity is better than pinning a resolution to it.
"""
from dataclasses import dataclass

from cronicled.text import spaceless

# This module's subject: one tag, by the media server's own id. Named rather
# than inlined for the reason `cronicled.scan.SUBJECT_TYPE` is -- a test, the
# store, the row builder and the apply path have to agree on one string.
#
# Deliberately NOT `cronicled.tags.SUBJECT_TYPE`, whose subject is a CLUSTER
# of spellings rather than a tag. Muting "stop proposing a description for
# this tag" and muting "stop proposing that these two spellings are one tag"
# are two different decisions about two different things, and one subject type
# would make either mute silence both.
SUBJECT_TYPE = "tag"

# The media server's own field name. The one place it is written, so the read
# query, the write mutation and the undo snapshot cannot drift apart into two
# spellings of one field.
FIELD = "description"


@dataclass(frozen=True)
class BoxIndex:
    """One source's tag catalogue, as the lookup this module actually does.

    `name` is the box's configured name -- what a proposal records as the
    source of the text, and the only thing a reviewer has to judge it by.

    `keys` maps a normalised name-or-alias to the description behind it.
    Tags the source holds with NO description contribute nothing at all: an
    entry that maps a key to an empty description is indistinguishable from a
    key nobody claimed, and would only crowd out a later source that does
    describe it.

    `ambiguous` is the keys two of this source's own tags claimed with
    DIFFERENT descriptions. They are dropped from `keys` rather than resolved,
    and kept here so the pass can say how many -- a count that quietly grows
    is a source whose aliases have stopped being usable as keys.
    """
    name: str
    keys: dict
    ambiguous: tuple


@dataclass(frozen=True)
class Found:
    """A description a source holds, and which source holds it.

    Two fields, always both set. `box` is not decoration: a description with
    no source is a sentence with no provenance, and the reviewer approving it
    has nothing to weigh except whether it reads plausibly -- which is exactly
    the judgement a fabricated description would survive.
    """
    description: str
    box: str


def has_description(tag):
    """Whether one media-server tag row already carries a description.

    Whitespace only counts as none. A field holding a space is not a
    description of anything, and treating it as one would leave the tag out of
    the backlog for ever on the strength of a stray character.

    `tag["description"]` is INDEXED: `Stash.all_tags` selects the field on
    every row it returns, so a row without it is malformed rather than a tag
    with no description (which is the field being `None`). Read with a default
    it would answer "already described" for a malformed row and drop the tag
    out of every count silently.
    """
    return bool((tag["description"] or "").strip())


def index_box(name, tags):
    """One source's tag rows as a `BoxIndex`.

    `tags` is what `StashBox.all_tags` returned, each row carrying `name`,
    `description` and `aliases` -- all three INDEXED, because the query
    selects all three, so a row missing one is a malformed answer rather than
    a tag with nothing to say.

    A row with no description is skipped whole: it has nothing to contribute
    and its keys must not shadow a source that does describe the same tag.

    A key claimed twice with the SAME description is agreement -- two spellings
    of one idea -- and is kept. A key claimed twice with DIFFERENT descriptions
    is the source disagreeing with itself, and is dropped from the index and
    never re-added, including by a later row that would otherwise re-claim it.
    """
    keys = {}
    # An ordered set: membership is asked once per surface over a catalogue of
    # thousands, and the order it was built in is what makes the reported
    # tuple the same on every run.
    ambiguous = {}
    for row in tags:
        description = (row["description"] or "").strip()
        if not description:
            continue
        for surface in [row["name"], *row["aliases"]]:
            key = spaceless(surface)
            # A name that normalises to nothing -- punctuation only -- would
            # gather every such tag under one key, on the strength of having
            # no letters in common. `cronicled.tags.cluster_tags` drops them
            # for the same reason.
            if not key:
                continue
            if key in ambiguous:
                continue
            if key not in keys:
                keys[key] = description
            elif keys[key] != description:
                del keys[key]
                ambiguous[key] = True
    return BoxIndex(name=name, keys=keys, ambiguous=tuple(ambiguous))


def find_description(name, indexes):
    """The first configured source that describes `name`, or `None`.

    `indexes` is a LIST, in configured order, and the order is the answer:
    this walks it front to back and stops at the first source that has
    anything to say. Two sources describing one tag is agreement (see this
    module's docstring), so there is nothing here to report and nothing to
    weigh -- but the order still has to be the operator's, not whatever a
    container's iteration happened to produce, which is why it travels as a
    sequence and never as a mapping keyed by box name.

    The empty-key guard below is UNOBSERVABLE through any index `index_box`
    can build, and is kept anyway. `index_box` already refuses to store a key
    that normalises to nothing, so a punctuation-only tag name looks up a key
    no index holds and gets `None` either way -- deleting the guard changes
    one dict lookup and no answer, which a mutation audit confirmed. It
    becomes load-bearing the moment anything constructs a `BoxIndex` by some
    other route, or `index_box` stops dropping empty keys; a test written to
    kill it would have to build an index production never produces.
    """
    key = spaceless(name)
    if not key:
        return None
    for index in indexes:
        description = index.keys.get(key)
        if description is not None:
            return Found(description=description, box=index.name)
    return None


@dataclass(frozen=True)
class Counts:
    """Why a pass's tags became the descriptions it proposed.

        total == described + clustered + outstanding + beyond_reach

    holds always, and a test asserts the identity rather than the fields one
    at a time: a tag that vanished for a reason nobody named is a failure no
    per-field check can see. `cronicled.scan.Counts` and
    `cronicled.tags.Counts` carry their own totals for the same reason.

    `outstanding` IS THE BACKLOG FIGURE, and it counts only tags a configured
    source can actually describe. `beyond_reach` -- undescribed tags no source
    has anything for -- is deliberately not in it. A number that added the two
    would say a few thousand tags are waiting on this pass when running it
    every night for a year would never move most of them, and a backlog that
    cannot go down stops being read.

    `boxes_unread` is how many configured sources could not be read in full,
    whether they failed outright or were read only partly. It exists to keep
    `beyond_reach` honest: a source that was never read holds no opinion about
    any tag, so with `boxes_unread` above zero, `beyond_reach` is a count of
    tags nothing *that answered* describes -- which is not the same claim, and
    the pass says so in words rather than letting the number be read as one.
    """
    total: int
    described: int
    clustered: int
    outstanding: int
    beyond_reach: int
    boxes_unread: int


def proposal(tag, found, *, folder):
    """One store proposal offering one source's description for one tag.

    `original` carries the tag's description exactly as the server returned
    it -- `None` for a tag that has none, never normalised to `""`. It is the
    value `Stash.apply_tag_description` compares the server against before
    writing, so a tag described by somebody between this pass and the click is
    refused rather than overwritten; a normalised copy would fail that
    comparison against the very state it was recorded from.

    `source_box` is the whole provenance of the text and is why this is a
    proposal rather than a guess. A reviewer looking at a sentence about a
    category can tell whether it reads well; they cannot tell whether anybody
    wrote it. Naming the source is what makes that answerable, which is why it
    is a field of the payload and not a phrase in the summary.

    `confidence` is `None` rather than a number. Nothing here is scored -- a
    source either holds a description under this tag's name or it does not --
    and a number would be one this module invented and then displayed in the
    same column, in the same type, as scores a scorer really produced.

    The `str()` around `tag["id"]` is an EQUIVALENT MUTANT under the current
    callers, and is kept anyway. A GraphQL `ID` is serialised as a JSON
    string, so every row `Stash.all_tags` hands back already carries a `str`
    here and removing the coercion changes nothing an audit can observe -- two
    independently sufficient mechanisms, the schema's and this one. Killing it
    would take a fixture holding an integer id, which is a row the real
    loader never produces, and a test built on one pins an implementation
    detail rather than a decision.

    It becomes load-bearing the moment a caller builds a tag row itself
    rather than reading it off `Stash.all_tags`, or a server serialises `ID`
    as a JSON number: `subject_id` is what the store fingerprints and what
    `store.muted_subjects()` is compared against, both of which hold text, so
    an integer arriving here would silently propose a tag a reviewer had
    already muted.
    """
    return {
        "folder": folder,
        "subject_type": SUBJECT_TYPE,
        "subject_id": str(tag["id"]),
        "summary": "%s: %s has a description for this tag" % (tag["name"],
                                                              found.box),
        "confidence": None,
        "payload": {
            "name": tag["name"],
            "field": FIELD,
            "original": tag["description"],
            "description": found.description,
            "source_box": found.box,
        },
    }


@dataclass(frozen=True)
class MergeDescription:
    """What description the survivor of a tag merge should end up with.

    A merge deletes the losing spellings, and tagsMerge keeps the
    DESTINATION's own fields. So a description living only on a spelling about
    to be deleted is destroyed by the merge, silently, with nothing recording
    what it said. That is the whole reason this type exists.

    `text` is the one description the survivor should carry, with exactly one
    of `from_tag` (a losing spelling wrote it) and `from_box` (a source holds
    it) naming where it came from. It is `None` whenever nothing needs
    carrying -- including when the survivor already carries the only
    description in the cluster, which is a merge with nothing to do here
    rather than a write of the same text back over itself.

    `conflicting` is every differing description in the cluster, in member
    order, when two or more of the spellings carry DIFFERENT text. In that
    case `text` is `None` and NOTHING is chosen: not the destination's, not
    the longest, not the first. Two people described the same category two
    ways and only a person can say which survives -- the same rule this
    project applies to every other tie, for the same reason, which is that
    picking silently and picking correctly look identical afterwards.

    Two spellings carrying the IDENTICAL description is agreement, not
    conflict: `conflicting` is empty and the survivor keeps what it already
    had. Reading agreement as conflict puts a decision in front of a person
    who has nothing to decide, which is how a review queue stops being read.
    """
    text: str = None
    from_tag: str = None
    from_box: str = None
    conflicting: tuple = ()

    def as_payload(self):
        """The dict a merge proposal carries. Written whole, every key every
        time, so a reader never has to tell "this pass had nothing to say"
        from "this key was not written"."""
        return {"text": self.text, "from_tag": self.from_tag,
                "from_box": self.from_box,
                "conflicting": [dict(c) for c in self.conflicting]}


def merge_description(members, canonical, indexes):
    """What `canonical` should end up describing, given its cluster.

    `members` are the cluster's tags as `cronicled.tags` carries them, each
    with its own `description`; `canonical` is the spelling that survives, or
    `None` for a cluster nothing has decided. `indexes` are the configured
    sources, in order.

    An undecided cluster answers with an empty `MergeDescription` and asks the
    sources nothing. No spelling is being deleted, so no description is at
    risk, and offering text for a survivor that has not been chosen would be
    proposing a write to a tag nobody has named.
    """
    if canonical is None:
        return MergeDescription()

    carried = [m for m in members if has_description(m)]
    distinct = []
    for member in carried:
        text = member["description"]
        if text not in distinct:
            distinct.append(text)

    if len(distinct) > 1:
        return MergeDescription(conflicting=tuple(
            {"name": m["name"], "description": m["description"]}
            for m in carried))

    if distinct:
        # Exactly one description in the cluster. If the survivor is what
        # carries it, the merge keeps it untouched and there is nothing to do.
        if has_description(canonical):
            return MergeDescription()
        # `carried[0]` versus `carried[-1]` is an EQUIVALENT MUTANT as
        # `cronicled.tags` clusters today, because `carried` can only hold one
        # member by the time this line runs: `tags._decide` resolves a
        # canonical for clusters of EXACTLY TWO, and a two-member cluster
        # whose members both carry text has `distinct` of one only when the
        # text agrees -- in which case the survivor is one of the two and the
        # early return above has already fired. Proved rather than argued: of
        # 48 reachable (members, canonical) pairs enumerated over spellings
        # and descriptions, every one reaches here with exactly one carrier.
        #
        # It becomes load-bearing the moment `_decide` resolves a cluster of
        # three or more, at which point two losers can agree while the
        # survivor is blank and this index chooses which spelling `from_tag`
        # credits. Member order is content-derived and stable (see
        # `cluster_tags`), so first-in-member-order is a rule rather than an
        # arrival-order accident -- but it would then be a rule nothing here
        # states, and it would need one.
        writer = carried[0]
        return MergeDescription(text=distinct[0], from_tag=writer["name"])

    # No spelling in this cluster describes the tag, so nothing is at risk of
    # being deleted and a source is free to supply one. Looked up under the
    # SURVIVOR's name: it is the spelling that will exist afterwards, and the
    # only one a description would end up attached to.
    #
    # Swapping `canonical` here for any other member is an EQUIVALENT MUTANT
    # while `cronicled.tags` clusters on `spaceless`: every member of a
    # cluster shares one normalised form BY CONSTRUCTION, and that form is the
    # key `find_description` looks up, so all of them ask the same question.
    # Proved: over every decided cluster enumerable from a set of spellings,
    # the canonical's lookup key and the first member's never differed.
    #
    # It becomes load-bearing the moment clustering keys on anything other
    # than the form this lookup uses -- a near-miss or edit-distance cluster
    # would hold members that normalise differently, and then asking under the
    # wrong spelling would attach one tag's description to another's.
    found = find_description(canonical["name"], indexes)
    if found is None:
        return MergeDescription()
    return MergeDescription(text=found.description, from_box=found.box)
