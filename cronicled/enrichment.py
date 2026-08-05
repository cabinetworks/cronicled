"""Filling in a performer record that has almost nothing on it.

Measured against a real library before this was designed, and the number
changed the shape of it twice:

    performers                            685
    completely bare (0 of 14 fields)      486   (71%)
    of those, on >= 1 scene               476
    of those, carrying a stash-box id       0

Per field, how many performers are missing it (details 99% down to image
71%) -- the distribution is bimodal: one population has never been touched at
all, and a smaller one is spread across a handful of filled fields. So the
unit of work here is a PERFORMER'S WHOLE SET OF BLANK FIELDS, not one field
picked out in advance the way `cronicled.descriptions` picks `details` alone.
A source that offers three of fourteen fields is still worth a proposal.

THE ID PATH REACHES NOTHING TODAY, AND IS BUILT ANYWAY
-------------------------------------------------------
Every performer carrying a stash-box id already has an image (and, this
library's own measurement says, everything else a box would have offered).
Not one of the bare 486 carries an id. So `cronicled.stashbox.StashBox
.performer_profile` -- the id lookup -- answers nothing for the population
this module exists to help, today. It is still the first source tried,
because the two populations not overlapping is a fact about THIS library at
THIS moment, not a property of the mechanism: the day anything assigns an id
to a bare performer, the id path is the strongest evidence available and
costs nothing extra to have kept ready. See `Source` and `enrich_one` below.

NAME MATCHING HAS ITS OWN RULE, AND IT IS NOT THE SCENE SCORER
----------------------------------------------------------------
`cronicled.scoring.score`/`decide` score a FILENAME against a TITLE: many
tokens, built for recall against a longer string, with a threshold measured
against thousands of real (filename, title) pairs. A person's name is one to
four tokens, commonly shares words with other names, and one name can be a
plain substring of another ("Wren" inside "Duchess Wren") -- exactly the
shape `cronicled.scoring.score`'s containment rule exists to reward, which
would turn every performer named after a common word into a false match for
every other performer whose name contains it.

So this module does not reuse that scorer, examined and rejected rather than
assumed unusable: the measured threshold and the containment bonus both
answer questions that do not transfer to a name. The rule here is instead the
one `cronicled.performer_tags` already established for the identical
problem (matching a NAME, not scoring a title): EXACT equality after
`cronicled.text.spaceless` normalisation, against either the candidate's own
name or one of the aliases it carries. A match is either exact or it is not a
match at all -- no similarity, no partial credit, no containment. See
`matches_name`.

AGREEMENT AND DISAGREEMENT ARE PER FIELD, NOT PER PERFORMER
-------------------------------------------------------------
A name search can answer with more than one performer -- two different real
people who share a name, or one person entered more than once. Any exact
match is evidence; more than one is the finding `cronicled.performer_tags`
already reports rather than resolves ("two performers answer to this tag's
name" -- neither is chosen). The same discipline applies here, field by
field: when every matching candidate that offers a given field agrees on its
value, that is corroboration and the field is proposed; when they disagree,
that ONE FIELD is refused and dropped, naming every value offered for it,
while every field the candidates DO agree on still proposes. Never resolved
by which candidate happened to come first -- see `merge_candidates`.

WHAT THIS DOES NOT COVER
--------------------------
Site adapters (the ticket's third-priority source, "for names a box does not
know") are not implemented here. `cronicled.adapters.base.SiteAdapter` has no
method for searching a performer profile by name at all -- every adapter
method that exists is about scoring a CLIP against a catalogue -- so building
one now would mean inventing a capability with no real adapter to verify it
against and no schema in this repository to measure it by. That is exactly
the kind of guess this project's own history warns against building without
measurement. It is also, separately, a COST-CLASS question: this producer
searches a stash-box, which is `cost="box"` (see `COST_CLASS` below), and
this project's own `cronicled.stashbox_scan` module documents at length why a
box-classed job and a scraping-classed job (which searching site adapters
would be) must never share one job -- mixing them would spend a
`scraping`-rationed budget under a limit that never counts it. So a
site-adapter tier belongs in a SECOND producer under `cost="scraping"`, built
once a real adapter exists that can answer "who is this performer", not
folded into this one. See the ticket report for the fuller reasoning.

SUBJECT TYPE
------------
`cronicled.descriptions` already uses `subject_type="performer"` for its own
proposals. This module deliberately does NOT reuse it -- see `SUBJECT_TYPE`
below for why two producers about the same kind of real-world subject still
need two different subject types.
"""
from dataclasses import dataclass

from cronicled.stash import Stash
from cronicled.text import spaceless

# Deliberately its OWN subject type, not `cronicled.descriptions.SUBJECT_TYPE`
# ("performer"), even though both are proposals about a performer. Two facts
# about how the store keys things make sharing it a correctness hazard, not
# a convenience:
#
# 1. `Store.mute`/`Store.record_refusal` key SOLELY on `(subject_type,
#    subject_id)` -- never on the payload, never on which producer wrote it.
#    `Store.mute`'s own docstring is explicit that this is deliberate ("reject
#    ANYTHING about a subject"), which is exactly right within one producer's
#    own proposals and exactly wrong across two unrelated ones: a reviewer
#    muting a description proposal for a performer would silently suppress
#    every future enrichment proposal for the SAME performer too, with
#    nothing on either page saying why enrichment stopped appearing for them.
# 2. `cronicled.web.rows.to_rows` dispatches the row BUILDER purely on
#    `subject_type` (`to_description_row` for `descriptions.SUBJECT_TYPE`),
#    never on what the payload actually contains. An enrichment payload has
#    none of `to_description_row`'s required keys (`faults`, `original`,
#    `cleaned`) -- sharing the type would send every enrichment proposal
#    through the wrong builder and it would KeyError on the first row drawn.
#
# The established pattern elsewhere in this project is exactly this kind of
# separation: `tag`, `tag-cluster`, `tag-performer` and `tag-unused` are FOUR
# subject types for four different standing decisions about material that
# substantially overlaps (tags), rather than one type shared across all of
# them. This follows that precedent rather than the one exception
# (`descriptions.py`, the first and so far only producer under `"performer"`)
# that happened to have nothing yet to collide with.
SUBJECT_TYPE = "performer-enrichment"

# `Stash.ENRICHMENT_FIELDS` plus the write-side name for the photo, which is
# not in that tuple because `Stash.performers_for_enrichment` reads it under
# a different name (`image_path`) -- see that method's own docstring. This is
# the field-name vocabulary every proposal, row and apply in this module
# works in: always `"image"`, never `"image_path"`.
IMAGE_FIELD = "image"
FIELDS = Stash.ENRICHMENT_FIELDS + (IMAGE_FIELD,)

# Fields whose blank value is `[]` rather than `None`. Read here once so
# `missing_fields`, `merge_candidates` and the proposal builder cannot drift
# into disagreeing about which fields are which shape -- the same list
# `Stash._ENRICHMENT_LIST_FIELDS` keeps for the apply/undo path, restated here
# rather than imported because that one is the low-level client's own
# bookkeeping and this is the policy that decides what "blank" means; see
# `tests/test_enrichment.py` for the pin that keeps the two from disagreeing.
LIST_FIELDS = ("alias_list", "urls")


def _is_blank(field, value):
    if field in LIST_FIELDS:
        return not value
    return value in (None, "")


def _lacks_image(image_path):
    """Whether `image_path` is Stash's autogenerated placeholder rather than
    a photo somebody attached.

    Stash always answers `image_path` with a URL -- there is no `None` for
    "no photo" -- and marks the placeholder by appending `?default=true` to
    it. That marker is the ONLY thing distinguishing the two populations this
    module cares about (a performer with a real photo, who must never be
    touched, from one with none, who is the whole point of this module), so
    getting the direction of this check backwards reads as "every performer
    needs an image" -- 685 proposals instead of 489 -- rather than as
    silence. See `tests/test_enrichment.py` for the pin that asserts BOTH
    directions rather than only the one a naive fix would leave standing.

    NOT VERIFIED against a live instance -- see this module's own docstring,
    and `cronicled.stashbox.PERFORMER_PROFILE`'s, for why: the marker is this
    project's best understanding of Stash's own convention for a resolved,
    computed field with no photo behind it, and it should be checked against
    a real server's response before this ships.

    A `None`/empty `image_path` is read the SAME way as a marked default --
    never as "unknown, so don't touch it" -- because `Stash
    .performers_for_enrichment` selects the field on every row it returns
    (see that method's docstring): a genuinely missing value would be a
    malformed payload, not an ordinary answer, and treating it as "has an
    image" would be the uncertainty-supplying-evidence mistake this
    project's own rules refuse elsewhere.
    """
    if not image_path:
        return True
    return "default=true" in image_path


def missing_fields(performer):
    """Which of `FIELDS` are blank on `performer` -- a dict shaped the way
    `Stash.performers_for_enrichment` returns one row, `image_path` included.

    A performer with every field filled in returns `()`, and that performer
    must never be offered a proposal -- see `EnrichmentProducer.produce`,
    which skips exactly that case, and the acceptance test that fails if a
    performer carrying a real image is ever proposed for.
    """
    missing = [f for f in FIELDS if f != IMAGE_FIELD
              and _is_blank(f, performer.get(f))]
    if _lacks_image(performer.get("image_path")):
        missing.append(IMAGE_FIELD)
    return tuple(missing)


@dataclass(frozen=True)
class Candidate:
    """One source's answer about who a performer might be, and what it knows
    about them.

    `label` is what a reviewer sees for provenance ("stash-box", or a named
    source once a second one exists) -- never a raw object, so a refusal or a
    proposal can name where a value came from without carrying a live handle
    on it.

    `fields` holds only the keys THIS candidate actually offered a value
    for -- a field this candidate is silent about is simply absent, not
    present with a blank value, so `merge_candidates` can tell "nothing
    offered" apart from "offered and blank" (the second never happens: a
    `Candidate` builder should not put a blank value in `fields` at all).
    """
    label: str
    name: str
    aliases: tuple
    fields: dict


def matches_name(performer_name, candidate):
    """Whether `candidate` may be trusted as evidence about the performer
    named `performer_name`.

    EXACT equality only, after `cronicled.text.spaceless` normalisation --
    against the candidate's own name, or against any alias it carries. Never
    containment, never a similarity score: see this module's own docstring
    for why the scene scorer's rules do not transfer to a name, and why a
    substring match ("Wren" inside "Duchess Wren") is exactly the false
    positive this refuses to manufacture.

    An empty/blank `performer_name` never matches anything -- there is no
    key to compare against, not a wildcard that matches every candidate.
    """
    key = spaceless(performer_name)
    if not key:
        return False
    if spaceless(candidate.name) == key:
        return True
    return any(spaceless(alias) == key for alias in candidate.aliases)


@dataclass(frozen=True)
class FieldConflict:
    """One field two or more matching candidates disagree about.

    `offers` names every value offered for it, paired with the label of
    whoever offered it -- "a refusal that names both", per the ticket's own
    acceptance list, generalised from two sources to however many actually
    disagreed. Never resolved here or anywhere downstream by which offer
    happens to sort first: the field is simply dropped from what gets
    proposed, and this is the record of why.
    """
    field: str
    offers: tuple


def _field_key(field, value):
    """The value `merge_candidates` compares two candidates' offers for
    `field` BY -- equality of meaning, not of representation, using
    `cronicled.text.spaceless` for the same reason `matches_name` does: two
    sources spelling one fact with different case or punctuation are not a
    disagreement about the fact.

    `alias_list`/`urls` compare as an unordered, deduplicated set of
    normalised entries -- two sources naming the same aliases in a different
    order are agreeing, not disagreeing. `height_cm` compares as itself: it
    is already a number with one honest representation.
    """
    if field in LIST_FIELDS:
        return frozenset(spaceless(v) for v in (value or ()) if spaceless(v))
    if field == "height_cm":
        return value
    return spaceless(value) if value else None


def merge_candidates(candidates, wanted_fields):
    """What a set of already-name-matched candidates jointly offer for
    `wanted_fields`, and which fields they could not agree on.

    Returns `(fields, conflicts)`. `fields` is a `{field: value}` mapping
    built ONLY from fields every offering candidate agreed on (by
    `_field_key`) -- a field no candidate offered at all is simply absent,
    the same "nothing here" `Candidate.fields` itself uses. `conflicts` is a
    tuple of `FieldConflict`, one per field where two offered values did not
    agree.

    A single candidate never conflicts with itself: one offering candidate's
    value for a field is used outright, which is also what makes the box-by-
    id path (always exactly one candidate) and a name search that resolved to
    one exact match behave identically to this function -- corroboration and
    conflict only exist once there is a second opinion to agree or disagree
    with.

    The VALUE carried into `fields` is the first offering candidate's own
    text, never a normalised or reconstructed form -- `_field_key` decides
    only whether two offers agree, the same separation
    `cronicled.scoring.decide` keeps between the text two titles are compared
    BY and the text a winning proposal actually carries.
    """
    fields = {}
    conflicts = []
    for field in wanted_fields:
        offers = [(c.label, c.fields[field]) for c in candidates
                 if field in c.fields and not _is_blank(field, c.fields[field])]
        if not offers:
            continue
        keys = {_field_key(field, value) for _, value in offers}
        if len(keys) == 1:
            fields[field] = offers[0][1]
        else:
            conflicts.append(FieldConflict(field=field, offers=tuple(offers)))
    return fields, tuple(conflicts)


# The cost class this producer runs under. Reading a stash-box (by id or by
# name) is the SAME rate-limited resource `cronicled.stashbox_scan
# .StashBoxCheckProducer` already rations under `"box"` -- see
# `cronicled.jobs.COST_CLASS_LIMITS`, which allows exactly one job of this
# class running at a time regardless of which producer holds the slot. Never
# `"scraping"`: this producer never reaches a site adapter (see this module's
# own docstring), so there is nothing here that cost class would be rationing
# in the first place.
COST_CLASS = "box"


def _box_candidate(box_row):
    return Candidate(label="stash-box", name=box_row["name"],
                     aliases=tuple(box_row["aliases"]),
                     fields=dict(box_row["fields"]))


def enrich_one(performer, box, *, wanted):
    """What to propose for one performer's `wanted` fields, trying stash-box
    by id first and then by name -- see this module's own docstring for why
    the id path is tried even though a real library measured zero performers
    reaching it, and for why a name search's candidates are filtered to exact
    matches before anything they offer is trusted.

    Returns `(fields, conflicts, source)`. `fields`/`conflicts` are
    `merge_candidates`'s own return shape; `source` says what was consulted
    ("stash-box (by id)" or "stash-box (by name)") for the producer's own log
    line, or `None` when `box` is not configured at all.

    A performer carrying a stash-box id for `box`'s own endpoint is looked up
    by id, and a search is never attempted alongside it -- once an id
    resolves the performer, a search is a WEAKER form of the same evidence,
    not a second opinion to corroborate or conflict with the first. A
    performer with no such id, or one `box.performer_profile` cannot find (an
    id gone stale), falls through to a name search instead.
    """
    if box is None:
        return {}, (), None

    stash_id = _matching_stash_id(performer, box)
    if stash_id is not None:
        profile = box.performer_profile(stash_id)
        if profile is not None:
            fields, conflicts = merge_candidates(
                [_box_candidate(profile)], wanted)
            return fields, conflicts, "stash-box (by id)"

    rows = box.search_performers(performer["name"])
    candidates = [_box_candidate(row) for row in rows]
    matching = [c for c in candidates
               if matches_name(performer["name"], c)]
    if not matching:
        return {}, (), None
    fields, conflicts = merge_candidates(matching, wanted)
    return fields, conflicts, "stash-box (by name)"


def _matching_stash_id(performer, box):
    """`performer`'s own stash-box id FOR `box`'s endpoint, or `None`.

    `performer["stash_ids"]` may carry ids from OTHER configured boxes this
    library has ever been scraped from -- an endpoint mismatch is not this
    box's performer at all, and asking it about somebody else's id would (at
    best) 404 and (at worst, if ids from two boxes ever collided) return the
    wrong profile entirely.
    """
    for entry in performer.get("stash_ids") or ():
        if entry.get("endpoint") == box.url:
            return entry.get("stash_id")
    return None


def proposal(performer, fields, source, *, folder):
    """One store proposal for a performer's enrichment -- the payload
    `cronicled.web.rows.to_enrichment_row` reads and
    `cronicled.web.actions.Actions.approve` applies.

    `fields` is whatever `merge_candidates` agreed on; every field it names
    is one this performer was confirmed blank in (see
    `EnrichmentProducer.produce`, which only ever calls this with fields
    drawn from `missing_fields`). `source` is the provenance string a
    reviewer sees beside the proposed values.
    """
    return {
        "folder": folder,
        "subject_type": SUBJECT_TYPE,
        "subject_id": str(performer["id"]),
        "summary": "%s: %d field%s from %s" % (
            performer["name"], len(fields), "" if len(fields) == 1 else "s",
            source),
        "confidence": None,
        "payload": {
            "name": performer["name"],
            "source": source,
            "fields": dict(fields),
        },
    }


class EnrichmentProducer:
    """Proposes values for every blank field of every performer stash-box
    can identify, by id or by name.

    ONE run reads the whole performer library once
    (`Stash.performers_for_enrichment`), then spends at most `limit`
    stash-box lookups -- the rate-limited resource this producer's cost class
    (`COST_CLASS`, `"box"`) rations. `limit` is REQUIRED, on the same terms
    `cronicled.runscan.build_producer` requires one for a scan: 489 bare
    performers against a rate-limited public service is not a bound this
    project widens by adding a producer, it is a new claim on the SAME bound
    (see `cronicled.jobs.COST_CLASS_LIMITS["box"]`, which already caps
    concurrency at one job regardless of which producer holds the slot).

    Never touches the media server's write path directly -- every field this
    finds is a PROPOSAL (`cronicled.store.Store.record`), never an apply.
    See `cronicled.web.actions.Actions.approve`'s own dispatch for the one
    place a value from here is ever written, and only after a person presses
    Approve.
    """

    name = "performer-enrichment"
    cost = COST_CLASS

    def __init__(self, stash, box, *, folder="library", limit,
                every=None, at=None, zone=None):
        self._stash = stash
        self._box = box
        self._folder = folder
        if limit is None:
            raise ValueError(
                "EnrichmentProducer requires an explicit limit -- 489 bare "
                "performers against a rate-limited stash-box is a bound "
                "this project measures and states, never defaults away")
        self._limit = limit
        # Same unconditional set, for the same reason, as
        # `cronicled.descriptions.DescriptionProducer`: `cronicled.schedule
        # .resolve` refuses an enabled producer declaring neither `every` nor
        # `at`/`zone` at start-up, which is the whole point of setting these
        # here rather than defaulting them away.
        self.every = every
        self.at = at
        self.zone = zone

    def produce(self, ctx):
        """Yield one proposal per performer this run confidently enriches.

        Reports, in the closing line, how many performers this run actually
        SPENT a stash-box lookup on -- the number the cost bound in this
        class's own docstring is about -- separately from how many were
        skipped for having nothing missing, so "0 proposed" cannot be misread
        as "the budget did nothing" when it was never spent at all.
        """
        if self._box is None:
            ctx.log("no stash-box is configured -- nothing to enrich against")
            return
        performers = self._stash.performers_for_enrichment()
        ctx.log("looking at %d performers for missing fields"
                % len(performers))
        proposed = spent = conflicted_fields = 0
        for performer in performers:
            if spent >= self._limit:
                break
            wanted = missing_fields(performer)
            if not wanted:
                continue
            spent += 1
            fields, conflicts, source = enrich_one(
                performer, self._box, wanted=wanted)
            for conflict in conflicts:
                conflicted_fields += 1
                ctx.log("performer %s: %s disagree on %s (%s)"
                        % (performer["id"],
                           " and ".join(label for label, _ in conflict.offers),
                           conflict.field,
                           "; ".join("%s=%r" % (label, value)
                                    for label, value in conflict.offers)))
            if not fields:
                ctx.log("performer %s: no source could confirm who this is"
                        % (performer["id"],))
                continue
            proposed += 1
            ctx.log("performer %s: %d field%s from %s"
                    % (performer["id"], len(fields),
                       "" if len(fields) == 1 else "s", source))
            yield proposal(performer, fields, source, folder=self._folder)
        ctx.log("finished: %d proposed, %d field conflicts, %d performers "
                "spent a stash-box lookup, %d performers looked at"
                % (proposed, conflicted_fields, spent, len(performers)))
