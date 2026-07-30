"""Tags that do almost no work, surfaced with the number that says so.

Measured against a real library: 2757 tags. Of the ones carrying no
description of their own and no description in any configured source, **256
are on zero scenes** and **793 are on exactly one**. Together that is more
than a third of the tag list, and nothing anywhere says so.

A tag on zero scenes classifies nothing: it is a name the library holds and
nothing references. A tag on one scene classifies that scene and nothing else
-- a note rather than a category, and indistinguishable from a mis-typing of a
tag that already exists. Neither is visible on any page, so they accumulate,
and each one is another near-duplicate for the merge pass to weigh and another
row a person scrolls past.

A LOW COUNT IS EVIDENCE, NEVER PROOF
------------------------------------
This module proposes and never decides, and the proposal states the count
rather than asserting the tag is useless. A tag on one scene may be exactly
right and simply rare, and a library that acquires more of that content will
use it again -- so there is NO threshold here at which anything is deleted
without a person looking at it, and no control anywhere that deletes a set.
Deleting one tag is a small, recoverable loss of a name somebody can type
again; deleting two hundred and fifty-six on one click, by a rule nobody
checked, is not recoverable at all. See `cronicled.web.rows.to_unused_groups`
for how that shapes the page.

WHICH TAGS THIS WILL NOT TALK ABOUT, AND WHY EACH IS EXCLUDED
-------------------------------------------------------------
The population is deliberately the one that was MEASURED, which is narrower
than "every tag on nought or one scene". Each narrowing withholds a deletion
proposal, which is the direction to fail in:

* **A tag that carries its own description.** Somebody wrote a definition of
  this category. That is evidence the tag is real, independent of how much of
  the library happens to use it yet, and it is a sentence the deletion would
  destroy with nothing recording what it said.
* **A tag a configured source describes** (`cronicled.tag_descriptions
  .find_description`, over the indexes the pass has ALREADY read -- this adds
  no third-party call and does not change the pass's cost class). A public
  catalogue holding a definition for this name is the same evidence from
  outside the library: the tag is a real vocabulary term that this library has
  not used much, which is a reason to keep it rather than a reason to remove
  it. Such a tag gets a DESCRIPTION proposal instead, which is the useful
  thing to say about it.
* **A tag inside a merge cluster** -- the merge row already covers it, and the
  brief's own rule is that one tag produces one row.
* **A tag the performer half has a live row about** -- see `_hygiene` in
  `cronicled.tags`. Attaching the performer and removing the tag preserves
  what the tag was recording; deleting it does not. The reversible half wins.
* **A tag a person has already kept**, and **a tag that already has a visible
  proposal in this folder** -- `narrowings`, below.

WHEN A SOURCE COULD NOT BE READ, THIS PASS PROPOSES NOTHING
-----------------------------------------------------------
"No configured source describes it" is half of the evidence above, and a
source that failed or was read only partly has not established it for a single
tag. `cronicled.tags._describe_summary` already refuses to let its
`beyond_reach` figure be READ as a verdict on such a night; a deletion
proposal cannot be worded carefully enough to survive the same gap, because
what it asks for cannot be taken back. So on a night when any configured
source could not be read in full, this half proposes nothing at all and says
so in the closing line.

The honest cost, stated rather than hidden: a library whose only configured
source is permanently broken never sees these rows. That is a visible,
recoverable failure -- the count is reported every run, and one working read
brings the rows back -- against a deletion approved on evidence that was
never gathered.
"""
from dataclasses import dataclass

from cronicled.tag_descriptions import find_description, has_description

# This module's subject: one TAG, by the media server's own id.
#
# Deliberately NOT `cronicled.tag_descriptions.SUBJECT_TYPE` ("tag") and NOT
# `cronicled.performer_tags.SUBJECT_TYPE`, even though all three are about a
# tag, and NOT `cronicled.tags.SUBJECT_TYPE` (whose subject is a cluster of
# spellings). "Stop offering me a description for this tag", "stop telling me
# this tag is a performer" and "I have looked at this tag and I am keeping it"
# are three different standing decisions about one tag, and one subject type
# would make any of the three mutes silence the other two.
SUBJECT_TYPE = "tag-unused"

# The two populations, named rather than written as bare counts at each use
# site: the producer picks a population, the row builder groups by one, and the
# page labels one, and all three have to agree on one string.
NO_SCENES = "no-scenes"
ONE_SCENE = "one-scene"

# In the order the page shows them, safest end first. A tuple, so the order is
# the answer rather than whatever a mapping's iteration happened to produce.
GROUPS = (NO_SCENES, ONE_SCENE)

# What the count on a proposal counts, said in words for the reason
# `cronicled.tags.COUNTS_COVER` and `cronicled.performer_tags.COUNTS_COVER` are:
# a bare number beside a tag name reads as "everything this tag holds", and
# `Stash.all_tags` selects `scene_count` alone. A tag is attached to markers,
# images and galleries too, and none of those are counted here -- which matters
# most in exactly this module, because a tag reported as being on no SCENES may
# still be the only thing filing a set of images.
COUNTS_COVER = "scenes"

# What each group is, for the one row that stands for it.
GROUP_LABEL = {
    NO_SCENES: "Tags on no scenes",
    ONE_SCENE: "Tags on exactly one scene",
}

# Why each group is a group, on the group's own row. TWO DISTINCT PARAGRAPHS
# rather than one with a number changed, for the reason
# `cronicled.performer_tags._summary` gives: the two send a reviewer to
# different judgements, and a catch-all that mentioned "a low scene count"
# would satisfy every assertion about there being an explanation while telling
# a person nothing about which of the two they are looking at. The first group
# is the safe end; the second is not, and says so.
GROUP_NOTE = {
    NO_SCENES: (
        "Nothing in the library references these, so deleting one changes no "
        "scene. That is the whole of the evidence, and it is not proof: a tag "
        "on no scenes may still be a category somebody meant to use, and "
        "deleting it is not reversible."),
    ONE_SCENE: (
        "Each of these classifies exactly one scene, which is a note rather "
        "than a category -- and is indistinguishable from a mis-typing of a "
        "tag that already exists. Deleting one takes it off that scene as "
        "well. A spelling that really is a duplicate of another tag is a "
        "MERGE rather than a deletion, and every such tag is left to the tag "
        "merges section instead of being offered here as well."),
}

# What a deletion carries for its whole life, before and after it is applied,
# per group. TWO SENTENCES, because the blast radius is the difference between
# the two groups and it is the only thing about them a person cannot see from
# the count: one really does change no scene, the other changes one.
#
# Why irreversible rather than reversible
# ---------------------------------------
# The alternative was an undo, the way `Stash.apply_scene` snapshots a scene.
# Refused, on three counts, any one of which is enough:
#
# 1. **The tag's id does not survive.** Re-creating the name afterwards makes a
#    NEW tag, and anything else that referenced the old id -- a saved filter,
#    another tool -- is not restored by it.
# 2. **Scenes are not all a tag holds.** The server attaches tags to markers,
#    images, galleries, performers and studios as well, and `Stash.all_tags`
#    selects `scene_count` and nothing else. A snapshot built from what this
#    code can see would restore a scene and silently lose the rest, reporting
#    success while leaving the library in a third state that is neither before
#    nor after. That is the same reason `cronicled.tags.MERGE_IS_IRREVERSIBLE`
#    gives.
# 3. **A deletion is offered one tag at a time and only after review**, which
#    is the containment this pass relies on instead. An undo would be a second
#    mechanism inviting a bigger, faster decision.
#
# `DELETE_IS_IRREVERSIBLE` is the part that is true of BOTH groups, said once
# for the one caller that has to refuse without knowing which group it is
# looking at (`web.actions.Actions.undo`). It deliberately says nothing about
# scenes, because that is exactly where the two groups differ and a sentence
# claiming either would be wrong for one of them.
DELETE_IS_IRREVERSIBLE = (
    "Deleting a tag cannot be undone. Its name, its aliases and its id do not "
    "come back, and nothing here records what they were -- there is no "
    "snapshot to restore, so no row ever offers an Undo for one.")

DELETE_WARNING = {
    NO_SCENES: (
        "Deleting this tag cannot be undone. It is on no scenes, so no scene "
        "changes -- but its name, its aliases and its id do not come back, "
        "and nothing here records what they were."),
    ONE_SCENE: (
        "Deleting this tag cannot be undone, and it is not attached to "
        "nothing: the one scene carrying it loses it. Its name, its aliases "
        "and its id do not come back, and nothing here records what they "
        "were."),
}

# Said once, on the section, so the page cannot drift from this module about
# the claim that matters most here: nothing has decided these tags are
# useless.
LOW_COUNT_IS_NOT_PROOF = (
    "A low count is evidence, not proof. A tag on one scene may be exactly "
    "right and simply rare, and a library that acquires more of that content "
    "will use it again. So nothing here deletes anything and there is no "
    "control that deletes a group: every tag is listed with its own count and "
    "its own button, and a person decides one at a time.")


def group_of(row):
    """Which population `row`'s scene count puts it in, or `None`.

    `row` is anything carrying `scene_count`: a `Stash.all_tags` row when the
    pass is deciding, a stored proposal's PAYLOAD when the page is grouping.
    One rule with two readers on purpose -- the population a count belongs to
    is a single decision, and a second copy of it in the row builder would be
    free to disagree with the one the producer made.

    `scene_count` is INDEXED, never `.get`. A default of 0 here is the one
    wrong answer this module cannot afford: it is precisely the value that puts
    a tag in the group whose whole argument is "deleting this changes no
    scene", so a malformed row missing the field would be proposed for
    deletion while the code read as though it had checked. A missing field
    raises, where the malformed row is still in hand.

    Anything that is not 0 or 1 -- including a value that is not a number at
    all -- answers `None` and is passed over. That is the safe direction: this
    module's only output is a deletion proposal, so "I cannot classify this"
    has to mean "say nothing about it".
    """
    count = row["scene_count"]
    if count == 0:
        return NO_SCENES
    if count == 1:
        return ONE_SCENE
    return None


@dataclass(frozen=True)
class Counts:
    """Why a pass's low-count tags became the deletions it proposed.

        low == withheld + described + sourced + clustered + reconciled
               + kept + already_proposed + outstanding

    and

        outstanding == no_scenes + one_scene

    both hold always, and a test asserts the two identities rather than the
    fields one at a time -- the same reason `cronicled.scan.Counts`,
    `cronicled.tags.Counts`, `cronicled.tag_descriptions.Counts` and
    `cronicled.performer_tags.Counts` all carry their own totals: a tag that
    vanished for a reason nobody named is a failure no per-field check can see.

    `low` counts tags on nought or one scene, NOT every tag the pass read. The
    rest are the overwhelming majority and they are not a backlog: nothing here
    will ever have anything to say about them, and a total that included them
    would report a few thousand items of outstanding work no number of runs can
    move.

    `withheld` is `low` itself, and every other term is zero, on a night when a
    configured source could not be read in full. See this module's docstring:
    half the evidence for a deletion is that no source describes the tag, and a
    source that was not read holds no opinion about any of them.

    `kept` is the standing "I have looked at this and I am keeping it" decision
    -- a mute on this module's own subject. It is reported SEPARATELY from
    `already_proposed` because "0 proposed" reads identically for a library
    with no low-count tags and for one whose every low-count tag a person has
    already answered, and those call for opposite responses.

    `no_scenes` and `one_scene` split `outstanding` by population, because the
    two are not the same finding: one changes no scene and the other changes
    one, and a single total would hide which kind of night this was.
    """
    low: int
    withheld: int
    described: int
    sourced: int
    clustered: int
    reconciled: int
    kept: int
    already_proposed: int
    outstanding: int
    no_scenes: int
    one_scene: int


def _summary(tag, group):
    """One line naming the tag and the count that put it here.

    TWO DISTINCT SENTENCES, not one with a number substituted, for the reason
    `cronicled.performer_tags._summary` states: "nothing references this" and
    "this classifies exactly one thing" are different findings with different
    consequences, and a catch-all mentioning the tag and its count would
    satisfy every assertion about the summary while telling a reviewer nothing
    about which they have in front of them.

    The count is printed from the row, not from `group`, so the number a person
    reads is the one the classification was made from rather than a constant
    this function chose to match it.
    """
    count = tag["scene_count"]
    if group == NO_SCENES:
        return ("%s: on %d %s -- nothing in the library references it"
                % (tag["name"], count, COUNTS_COVER))
    return ("%s: on %d scene -- it classifies that one and nothing else"
            % (tag["name"], count))


def proposal(tag, *, folder):
    """One low-count tag as the proposal dict a producer yields.

    Refuses a tag no population claims, rather than proposing a deletion for
    a tag that is doing real work. The producer already skips it, so reaching
    here with one is a wiring error and raises where it can be seen -- the
    same terms `cronicled.performer_tags.proposal` refuses a proposal with no
    match on.

    `scene_count` is carried and `group` is NOT, deliberately. The group is a
    function of the count (`group_of`), so storing both would be one fact with
    two representations -- and the stored pair could disagree, with nothing
    able to say afterwards which of them the page had grouped by. The count is
    the thing a person judges and the thing the apply re-checks against the
    server, so the count is what is written down.

    It is the count AS MEASURED WHEN THE PASS RAN, and it can be stale by the
    time somebody clicks: a proposal can be days old. That is not papered over
    here -- `Stash.delete_tag` re-reads the count one line before the write and
    refuses the deletion outright if it has moved, so a tag that has since been
    put to work is never deleted on the strength of a number from last week.

    `confidence` is `None` rather than a number. Nothing here is scored -- a
    tag is on nought scenes, or one, or more -- and a number would be one this
    module invented and then displayed in the same column, in the same type, as
    scores the scorer really produced.
    """
    group = group_of(tag)
    if group is None:
        raise ValueError(
            "cannot propose deleting tag %r: it is on %r %s, which is neither "
            "of the two populations this pass reports on"
            % (tag["name"], tag["scene_count"], COUNTS_COVER))
    return {
        "folder": folder,
        "subject_type": SUBJECT_TYPE,
        "subject_id": str(tag["id"]),
        "summary": _summary(tag, group),
        "confidence": None,
        "payload": {
            # The NAME only. The tag's id is `subject_id`, and one fact gets
            # one representation: a second copy in the payload could disagree
            # with it, and nothing would notice which of them the delete used.
            "name": tag["name"],
            "scene_count": tag["scene_count"],
            "counts_cover": COUNTS_COVER,
        },
    }


def narrowings(store, folder):
    """`(kept, proposed)`: the tag ids this pass must not propose again.

    The same two narrowings `cronicled.tags.select` applies to a cluster and
    `cronicled.performer_tags.narrowings` applies to a reconciliation, asked at
    THIS module's own subject level -- which is the whole reason `SUBJECT_TYPE`
    is its own string: a person who muted a description proposal for a tag has
    said nothing about whether the tag is worth keeping.

    `kept` comes from `store.muted_subjects()` -- the `mute` table
    `Store.record` itself consults, not the items a mute moved into the `muted`
    state, so a tag kept before any proposal existed is seen. This is the
    acceptance the ticket names: a tag reviewed and kept is not offered again
    on the next run, and Keep is the control that records it.

    `proposed` is every tag that already has a VISIBLE proposal in `folder`,
    which includes an `applied` one -- so a tag whose deletion was approved is
    not offered again, independently of whether the server still holds it.

    The residual, named rather than hidden: a proposal a person DISMISSED is no
    longer visible, so a later run offers it again. That is the same
    distinction `Store.dismiss` and `Store.mute` already draw -- a dismissal
    rejects one proposal, a mute rejects the subject -- and Keep is the durable
    answer for a tag that must stop being offered.
    """
    kept = {subject_id for subject_type, subject_id
            in store.muted_subjects() if subject_type == SUBJECT_TYPE}
    proposed = {item["subject_id"] for item in store.items(folder=folder)
                if item["subject_type"] == SUBJECT_TYPE}
    return kept, proposed


def evidence_against(tag, indexes):
    """Whether something already describes this tag as a real category.

    `(described_by_library, described_by_source)`, and either one is enough to
    withhold a deletion proposal -- see this module's docstring for why each is
    evidence the tag is real rather than merely unused.

    Two flags rather than one boolean, because the pass reports them as two
    separate counts: "somebody in this library defined this" and "a public
    catalogue defines this" are different reasons a tag is being kept, and a
    single figure would hide which of the two a night's suppressions were.

    `indexes` are the sources the pass has ALREADY read for its description
    half, passed in rather than re-fetched. Nothing here issues a request, so
    this adds no third-party call and the pass's cost class is unchanged.
    """
    if has_description(tag):
        return True, False
    return False, find_description(tag["name"], indexes) is not None
