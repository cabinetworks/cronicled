"""Store item -> what the inbox shows for it.

Kept apart from rendering and from HTTP because the interesting decision here
is editorial: which facts does a person need in front of them to judge a
proposal without opening anything else. That is worth testing on its own.

WHICH WAY TIMESTAMPS ARE CONVERTED, AND WHICH WAY THEY ARE NOT
--------------------------------------------------------------
Everything the store holds is UTC and stays UTC. `local` below converts on the
way OUT, here in the view layer, and nothing this module produces is ever read
back into a write.

That direction is the whole of the rule, and it is asymmetric on purpose. A
rendering that shows the wrong hour is wrong on a screen a person is looking at,
and one edit fixes it. A LOCAL time written into the database is a different
kind of loss: during the hour a clock repeats, two rows an hour apart carry the
same text, so nothing afterwards can tell which came first -- not the scheduler
comparing a run against an appointment, not `ORDER BY created_at`, not a person
reading the table. No edit recovers that, because the information is gone rather
than mislabelled.
"""

import os
import re
from dataclasses import dataclass, replace

from cronicled import tag_hygiene
from cronicled.descriptions import SUBJECT_TYPE as DESCRIPTION_SUBJECT
from cronicled.scan import candidate_url
from cronicled.schedule import as_utc
from cronicled.tag_descriptions import SUBJECT_TYPE as TAG_DESCRIPTION_SUBJECT
from cronicled.tags import MERGE_IS_IRREVERSIBLE
from cronicled.text import slug_match, spaceless

# An ISO-8601 instant carrying an offset, as it appears INSIDE a sentence the
# scheduler wrote ("last ran 2026-07-28T21:17:08+00:00; next due at ...").
# Only with an offset: a naive stamp names no instant, and rewriting one would
# invent the very fact it is missing.
#
# Every part is fixed-width, so a longer number cannot be half-swallowed into a
# different instant -- there is no `\b` on the left because a digit boundary is
# not a word boundary, and none is needed.
_INSTANT_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})")


def local(value, zone):
    """A stored UTC timestamp as an ISO-8601 string in `zone`.

    `value` is what the store recorded: an ISO-8601 string, or a `datetime`.
    The instant is unchanged -- this relabels it, it does not move it -- so a
    row muted at 21:17 UTC reads 23:17 in a zone two hours ahead, and the two
    orderings cannot disagree because there is only one instant.

    ANYTHING THIS CANNOT READ AS AN INSTANT COMES BACK EXACTLY AS IT WENT IN,
    `None` included. A stamp with no offset is the case that matters: it names
    no instant, so converting it would mean assuming one and then shifting it,
    putting an hour on the page that was never recorded anywhere. Showing the
    stored text verbatim is the honest failure -- it looks unlike the rows
    around it, which is exactly the signal that something upstream wrote a stamp
    it should not have. `cronicled.schedule.as_utc` draws that line, and it is
    the same line the scheduler compares timestamps by; a second reader here
    would be free to disagree with it about a row, silently, in either
    direction.

    `zone` is required and has no default. A default would hand a caller who
    forgot it UTC -- the exact output of doing nothing at all -- so the mistake
    would look like the feature working.
    """
    moment = as_utc(value)
    if moment is None:
        return value
    return moment.astimezone(zone).isoformat()


def local_times(text, zone):
    """Every ISO-8601 instant inside `text`, converted to `zone`.

    For the sentences the scheduler hands the page -- "last ran ...; next due at
    ..." -- which are prose with timestamps in them rather than timestamps. They
    are rewritten HERE, in the view, rather than by teaching
    `cronicled.schedule` to format for a page: those reasons come out of rules
    that are pure arithmetic over UTC arguments, which is what makes every
    scheduling decision an ordinary unit test, and a zone reaching in there
    would be reaching into the comparisons as well as the wording.

    A page where one line reads in the operator's hour and the next reads in UTC
    is worse than one that reads entirely in UTC, because the reader cannot tell
    which is which and so can trust neither. That is why the reasons are not
    simply left alone.

    Text with no instant in it comes back unchanged, which is most of it -- a
    "disabled by override", or a `KeyError` from a producer that would not
    start.
    """
    if not text:
        return text
    return _INSTANT_IN_TEXT.sub(lambda m: local(m.group(0), zone), text)

# What a row says it IS, so the page can pick a shape for it without
# inspecting its fields. A scene proposal and a description proposal are not
# the same row with some fields blank: one is judged by a filename, a
# candidate title and a score, the other by two versions of a paragraph. The
# template branches on this rather than on `row.original is defined`, because
# an undefined attribute renders as empty text in Jinja rather than raising --
# so a mis-typed field name there is a silently blank block, which on a page
# whose buttons write to a library is the failure mode to design out.
KIND_SCENE = "scene"
KIND_DESCRIPTION = "performer-description"
KIND_TAG_DESCRIPTION = "tag-description"
KIND_UNUSED_TAG = "unused-tag"

# What one store did when it was searched for a refused file, named so the
# Refused section can pick a line for it without inspecting which of its
# fields happen to be set. Same reasoning as KIND_SCENE above: a template
# branching on `s.error` renders an empty line for a state nobody thought
# about, and a page whose whole job is explaining a refusal must not have one.
#
# THREE, never two. A store that returned rows none of which were good enough,
# a store that returned nothing, and a store that raised are three different
# findings: the first says the catalogue has entries and none resemble this
# file, the second says the catalogue has no entry under this creator at all,
# and the third says nothing whatever about the catalogue -- it is evidence
# about the network. Collapsing any pair sends a person to look in the wrong
# place.
STORE_ANSWERED = "answered"
STORE_EMPTY = "empty"
STORE_FAILED = "failed"


@dataclass(frozen=True)
class Row:
    fingerprint: str
    state: str
    filename: str
    # The media server's own page for this row's scene, or `None` when no
    # server is configured -- see `scene_url`'s own docstring for why the
    # address is threaded in here rather than typed a second time.
    scene_url: str | None
    proposed_title: str
    # Where the proposed title came FROM: the candidate's own page on the
    # store that offered it, or `None` when the candidate carries no
    # address at all. `scene_url` above and this are the two ends of the
    # decision and must never be confused -- one is the file as the media
    # server holds it today, the other is the record a person is being
    # asked to overwrite it with -- which is why they are two fields and
    # not one, and why nothing derives either from the other.
    #
    # `cronicled.scan.candidate_url` is the one rule that answers this,
    # shared with the enrichment scrape and with a refusal's near miss. A
    # second derivation here could point somewhere an apply would not, and
    # a link that disagrees with what Approve will write is worse than no
    # link -- see that function's own docstring.
    #
    # `None` is an ordinary answer, not a failure, and the template renders
    # it as plain text: a name search commonly returns a title and nothing
    # else, and no address means no anchor rather than a guessed one.
    candidate_url: str | None
    # `None` on a proposal a stash-box identified by fingerprint: no creator
    # was resolved for it, because nothing needed one — the file was never
    # searched for by name. `identifying_box` below is what such a row says
    # instead, and the template picks between them.
    creator: str | None
    creator_source: str | None
    # Every OTHER configured store that named this same candidate, from
    # `payload["agreeing_stores"]` — empty on the ordinary proposal only one
    # store answered. Corroboration is not a warning, so it belongs beside
    # the attribution and never in `disagreement`, which is the field that
    # means "do not approve this": a reviewer who sees an agreement badge in
    # the place warnings live learns to read past the warnings.
    #
    # A fingerprint-identified row carries an empty tuple even when its own
    # payload has `agreeing_boxes`: boxes and stores are two different
    # mechanisms agreeing about two different things, and this field is the
    # one for stores.
    agreeing_stores: tuple
    contested: bool
    disagreement: str | None
    carries_cover: bool
    # What approving this proposal will actually write onto the scene —
    # the whole reason a proposal now scrapes the winning candidate's own
    # URL rather than carrying only a title and a link (see
    # `cronicled.scan.examine`'s `enrich` argument). `performers` is a tuple
    # of names, in the order the candidate carries them; `studio` is a
    # single name or `None`. Both are empty/`None` on a candidate that was
    # never enriched, or whose enrichment failed — see `_performer_names`
    # and `_studio_name` below for why that is read off the candidate
    # itself rather than tracked as a separate "did enrichment happen" flag:
    # a thin candidate's own fields already say "nothing here", and a
    # second, parallel signal saying the same thing could drift from it.
    performers: tuple
    studio: str | None
    # `None` on a fingerprint-identified proposal, and that is the whole
    # point of the field being nullable. Such a proposal did not score 1.0 —
    # nothing scored it at all — so any number here would be one this page
    # invented and then displayed in the same column, in the same type, as
    # numbers the scorer really produced. `score_text` says so in words
    # instead; see `to_row`.
    score: float | None
    score_text: str
    # The stash-box that recognised this file by its own fingerprints, or
    # `None` for an ordinary scored proposal. Both are never set at once:
    # the two are different ways of arriving at a candidate and a row shows
    # whichever one this proposal used.
    identifying_box: str | None
    runners_up: tuple
    undoable: bool
    # Why an apply failed, for a row in the `failed` state. Without it the page
    # shows a row that quietly stopped offering any control and says nothing
    # about why -- a person cannot tell a transient server error from a
    # proposal that will never apply, and cannot act on either.
    error: str | None
    # Whether this row still has a decision left in it. A failed apply wrote
    # NOTHING, so the proposal is as live as it was before the attempt: it can
    # be tried again, dismissed, or muted. Only an applied row is closed, and
    # that one has Undo instead. Derived here rather than spelled as a list of
    # states in the template, because the template is where a new state would
    # silently fall through every branch and take the controls away again --
    # which is exactly how a failed row became a dead end.
    actionable: bool
    # Last, with a default, so this stays the row every existing caller
    # already builds. See KIND_SCENE.
    kind: str = KIND_SCENE


@dataclass(frozen=True)
class DescriptionRow:
    """What the inbox shows for a proposed description rewrite.

    BOTH texts, always. That is the entire review: the reviewer is being
    asked whether the second is the first with its markup taken out, and a
    row showing only the cleaned version gives them nothing to judge it
    against -- they would be approving a write to a field whose previous
    contents they cannot see, which is precisely the "plausible-looking
    proposal that silently removed text" this producer exists to avoid
    making in the first place.

    `faults` names what was found (`markup`, `entity`, or both) rather than
    leaving a reviewer to spot the difference themselves between two
    paragraphs that may differ by four characters.

    No score. Nothing scored this -- a description either carries markup or
    it does not -- and any number would be one this page invented and then
    showed in the same column, in the same type, as numbers the scorer really
    produced. The same reasoning `Row.score` documents for a
    fingerprint-identified proposal, taken one step further: there is no
    column at all here.
    """

    kind: str
    fingerprint: str
    state: str
    subject_id: str
    name: str
    # The media server's own page for this performer, or `None` when no
    # server is configured -- see `performer_url`.
    performer_url: str | None
    faults: tuple
    original: str
    cleaned: str
    undoable: bool
    actionable: bool
    error: str | None


@dataclass(frozen=True)
class TagDescriptionRow:
    """What the inbox shows for a description a stash-box holds for a blank
    tag.

    Deliberately NOT a `DescriptionRow`. That row is a diff: two versions of
    one paragraph, and the reviewer's question is whether the second is the
    first with its markup removed. This one is a provenance question -- the
    field is empty, some other index wrote a sentence about a tag with this
    name, and the only thing that makes it judgeable is knowing WHICH index.
    Forcing the two through one shape would put the source in a field named
    for something else, or drop it.

    `source_box` is therefore carried on every row in every state, and is the
    field this row exists to show. A description with no source is a sentence
    with no author, and the only test left to a reviewer is whether it reads
    plausibly -- which is exactly the test a fabricated description passes.

    `original` is the field as it stands, and it is shown even though it is
    empty by construction. A reviewer needs to see that they are filling a
    blank rather than replacing something: the apply refuses if the field has
    been written in the meantime, and a row that never showed the current
    value would make that refusal look like a bug instead of the guard it is.

    No score, no `faults`. Nothing here is scored and nothing here is a
    defect in the text -- the tag simply has no description.
    """

    kind: str
    fingerprint: str
    state: str
    subject_id: str
    name: str
    # The media server's own page for this tag, or `None` when no server is
    # configured -- see `tag_url`.
    tag_url: str | None
    # Which configured source holds this description. Never `None`: a
    # proposal is only ever made because a named source had one.
    source_box: str
    original: str
    description: str
    undoable: bool
    actionable: bool
    error: str | None


def scene_url(base_url, subject_id):
    """The media server's own page for `subject_id`, or `None` when
    `base_url` is falsy.

    `base_url` is the same address `cronicled.__main__` already resolved
    for the `Stash` client it builds (`--server` / `$CRONICLED_SERVER`) --
    passed through from there, never re-derived from `Stash.url` (which has
    `/graphql` appended for the API client's own purposes, see `Stash
    .__init__`) and never read from a second, separate piece of
    configuration. One address, read once, used for both jobs.

    The one place this project writes the path shape for a scene page, so
    nothing else -- a row builder, the template -- reconstructs it a second
    way that could drift from this one.

    Ticket 97 is explicit that this tool "now starts read-only with no
    server configured": that is the ordinary case this returns `None` for,
    not a malformed one, so no exception here -- a caller (`to_row`,
    `to_refusal_row`, `to_mute_row`) folds `None` into a row exactly like
    any other optional field, and the template degrades to plain text.
    """
    if not base_url:
        return None
    return "%s/scenes/%s" % (base_url.rstrip("/"), subject_id)


def performer_url(base_url, subject_id):
    """The media server's own page for a performer, or `None` when
    `base_url` is falsy.

    The sibling of `scene_url` and deliberately a second function rather than
    one taking a path segment: the two are the only two subject kinds this
    page knows, and a shared helper parameterised by `"scenes"`/`"performers"`
    would let a caller pass a segment that names neither and get a link to
    nowhere. Same `base_url`, resolved once in `cronicled.__main__`, used for
    both.
    """
    if not base_url:
        return None
    return "%s/performers/%s" % (base_url.rstrip("/"), subject_id)


def tag_url(base_url, subject_id):
    """The media server's own page for a tag, or `None` when `base_url` is
    falsy.

    The third of the deliberately-separate `scene_url`/`performer_url` pair,
    written out rather than folded into a shared helper taking a path
    segment, for the reason `performer_url` states: a helper parameterised by
    the segment lets a caller pass one that names no subject kind at all and
    get back a link to nowhere, which renders as an ordinary working link.
    """
    if not base_url:
        return None
    return "%s/tags/%s" % (base_url.rstrip("/"), subject_id)


def _runner_up_view(entry):
    """One losing candidate, normalised to what the screen shows.

    `scan._runners_up` builds each entry as `{"candidate": <the whole search
    result>, "score": value}` -- the title lives inside `candidate`, not at
    the top level. Deciding what the "also considered" column needs is this
    module's job, not the template's: reading `title` from the wrong place
    here would surface as a silently blank column there, since Jinja renders
    an undefined attribute as empty text rather than raising.

    `url` is the loser's OWN address, read by the same
    `cronicled.scan.candidate_url` rule the winner's is -- not the winner's,
    and not omitted. A losing candidate is exactly the one an operator opens
    before overriding a decision, so a runner-up that could be looked at and
    is not linked costs the review the evidence it exists for; and a
    runner-up wearing the WINNER's address would send that operator to the
    page they were trying to check against.
    """
    return {"title": entry["candidate"]["title"], "score": entry["score"],
            "url": candidate_url(entry["candidate"])}


def carries_cover(candidate):
    """Would applying `candidate` write a cover image `Stash.revert_scene`
    cannot restore?

    Indexed as `candidate["image"]`, not `.get("image")`. `image` is a
    field `Stash.scrape_scenes_by_query`'s own query selects on every
    candidate it returns (see its docstring), so a real candidate always
    answers this key -- `None` or `""` meaning "the scraper found no
    cover", a plain, ordinary no. A candidate with the key absent entirely
    is not that: it is a payload from somewhere that never asked the
    question, and `.get("image")` would read it back exactly like "no
    cover" -- the one value that skips the warning this function exists to
    raise. That is a malformed payload, and it must raise, not answer
    `False` as though nothing were being hidden.

    Shared between `to_row` (the pre-approve warning) and
    `cronicled.web.actions.Actions.undo` (the post-revert one) so the two
    only ever disagree if the payload itself changes between an approve
    and its undo, never because one of them reimplemented the check.
    """
    return bool(candidate["image"])


def _performer_names(candidate):
    """The performer names a proposal's `candidate` carries, in the order
    the scraper returned them.

    Indexed as `candidate["performers"]`, not `.get`, for the same reason
    `carries_cover` indexes `candidate["image"]`: `Stash
    .scrape_scenes_by_query` and `Stash.scrape_scene_url` both select
    `performers` on every candidate either can return (see their shared
    `_SCRAPED_SCENE_SELECTION`), so a real candidate always answers this
    key — `[]` meaning "no performers found (or none carried over from
    before enrichment)", a plain, ordinary answer. A candidate with the key
    absent entirely is a payload from somewhere that never asked the
    question, and `.get("performers")` would read it back exactly like "no
    performers" — indistinguishable from the ordinary case this function
    exists to report honestly.
    """
    return tuple(p["name"] for p in (candidate["performers"] or ()))


def _studio_name(candidate):
    """The studio name a proposal's `candidate` carries, or `None` — the
    same indexing discipline as `_performer_names` and `carries_cover`, and
    for the same reason."""
    studio = candidate["studio"]
    return studio["name"] if studio else None


def _disagreement(creator, filename_stem, competing_store=None):
    """One line naming what the resolver (or a multi-store search) passed
    over, or None when nothing passed over actually disagrees.

    Three kinds, kept apart deliberately upstream and phrased apart here:
    `competing` is a name a reviewer could go and search for; `rejected_folder`
    is folder text that failed the guards, and treating it as a name is the
    mistake the resolver exists to prevent; `competing_store` (see below) is
    another configured store that ALSO matched this file above the
    threshold, with a candidate the one actually proposed did not carry.

    The resolver (and the multi-store search) go on recording all three,
    unchanged -- deciding what is worth interrupting a person about belongs
    here, not there. Two of the first kind are not disagreements at all, and
    measured against a real library they produced six warnings of which one
    meant anything. A warning that fires on almost every row stops being
    read, and then the one that matters goes past unread with the rest of
    them.

    **A folder that is the filename repeated.** One file per folder is an
    ordinary layout, and such a folder was never going to name anyone: it
    failed the guards for being a title, not because the filing convention is
    wrong. It carries no evidence independent of the filename beside it.

    Suppressed on exact equality of the normalised forms, NOT on containment,
    and that asymmetry is deliberate. Silencing a warning is the expensive
    direction here -- a folder genuinely naming a different creator is the
    mis-filing this field exists to catch, and a looser rule would hide it.
    Equality demands the strongest evidence that the folder is a copy.

    **A competing name that is the same attribution spelled longer.** A loser
    containing the winner, or contained by it, is one person written two ways
    rather than two candidates. `slug_match` is this project's existing answer
    to "are these the same name", so this asks it rather than deciding again.

    **Another store matching the same file.** `competing_store` is
    `payload.get("competing_store")` -- present only when
    `cronicled.scan.examine_sources` chose a winner despite another
    configured store ALSO clearing the threshold for this file (see
    `_choose_winner`). Reported the same way a folder/filename disagreement
    already is: not withheld, and not treated as a reason to refuse the
    proposal on its own -- a reviewer sees it and decides, exactly as they
    already do for a folder naming a different creator than the filename.
    """
    competing = creator.get("competing")
    rejected = creator.get("rejected_folder")
    parts = []
    if competing and not slug_match(competing, creator["name"]):
        parts.append("the filename names %s instead" % competing)
    if rejected and spaceless(rejected) != spaceless(filename_stem):
        parts.append("the folder text %r was not usable as a name" % rejected)
    if competing_store:
        stores = ", ".join(entry["store"] for entry in competing_store)
        parts.append(
            "another store (%s) also matched this file with a different "
            "candidate" % stores)
    return "; ".join(parts) if parts else None


# What `to_row` shows in the score column for a proposal nothing scored. Not
# a number and not blank: a blank column reads as a value that failed to
# render, and any number at all reads as the scorer's own output. This is the
# one place the difference is visible to a person, so it says so in words.
IDENTIFIED_SCORE_TEXT = "id'd"


def to_row(item, base_url=None):
    payload = item["payload"]
    filename = os.path.basename(payload["path"])
    candidate = payload["candidate"]
    # The discriminator between the two kinds of proposal, read off the
    # payload the producer wrote (`scan.IDENTIFIED_BY_FINGERPRINT`). Absent
    # means a scored proposal — which is what every payload written before
    # fingerprint identification existed is, so absence is the right default
    # here rather than a guess: it sends such a payload down the branch that
    # INDEXES `creator` and `score` and raises if either is missing, never
    # down the branch that would quietly show a row with neither.
    identified_by = payload.get("identified_by")
    if identified_by is None:
        # Indexed, not .get(): a scored proposal without a creator is
        # malformed, and a blank creator column reads as "nothing disagreed"
        # — the reading that gets a wrong row approved.
        creator = payload["creator"]
        creator_name = creator["name"]
        creator_source = creator["source"]
        score = payload["score"]
        score_text = "%.3f" % score
        identifying_box = None
        # `.get`, not indexed: every proposal made before cross-store
        # agreement existed, and every one only a single store answered,
        # legitimately has no such key. An empty tuple is the honest reading
        # of its absence — nobody corroborated this — where a missing key
        # would be a malformed payload, and neither reads as a warning.
        agreeing_stores = tuple(payload.get("agreeing_stores") or ())
        disagreement = _disagreement(
            creator, os.path.splitext(filename)[0],
            competing_store=payload.get("competing_store"))
    else:
        # A box identified this file; no creator was ever resolved for it and
        # nothing scored it. Indexed, not .get(): a payload that says it was
        # identified and cannot say by which box is malformed, and "identified
        # by nobody" is precisely the row a person would approve without
        # noticing.
        creator_name = creator_source = score = None
        score_text = IDENTIFIED_SCORE_TEXT
        identifying_box = payload["box"]
        agreeing_stores = ()
        # Nothing to contest: two boxes that disagreed never produced a
        # proposal at all (see `scan.fingerprint_outcome`), and boxes that
        # AGREED are agreement, which is not a warning.
        disagreement = None
    return Row(
        fingerprint=item["fingerprint"],
        state=item["state"],
        filename=filename,
        # Indexed, not .get(): every real `item` row carries a subject_id --
        # it is a NOT NULL column (see cronicled/store.py's schema) -- so an
        # item without one is a wiring error, not "no server configured".
        # That case is `base_url` being falsy, which `scene_url` already
        # handles on its own terms.
        scene_url=scene_url(base_url, item["subject_id"]),
        proposed_title=candidate["title"],
        # One rule, four readers -- see `Row.candidate_url`. Read off the
        # candidate itself for BOTH kinds of proposal: a fingerprint
        # identification's match comes back through the same
        # `_SCRAPED_SCENE_SELECTION` a text scrape does and carries its own
        # `urls`/`url` exactly the same way, so nothing here branches on
        # `identified_by`, and nothing assembles an address out of the box's
        # endpoint (an API address, not a page a person can open) and its id.
        candidate_url=candidate_url(candidate),
        creator=creator_name,
        creator_source=creator_source,
        agreeing_stores=agreeing_stores,
        contested=disagreement is not None,
        disagreement=disagreement,
        carries_cover=carries_cover(candidate),
        performers=_performer_names(candidate),
        studio=_studio_name(candidate),
        score=score,
        # Three places, matching the precision the decision was made at —
        # for a scored proposal. For an identified one there is no decision
        # and no precision; see IDENTIFIED_SCORE_TEXT.
        score_text=score_text,
        identifying_box=identifying_box,
        runners_up=tuple(_runner_up_view(r)
                         for r in (payload.get("runners_up") or ())),
        # An applied row with no snapshot cannot be reverted — revert_scene
        # raises on an empty one. Offering the button anyway would promise an
        # undo the code cannot perform.
        undoable=(item["state"] == "applied"
                  and bool(item.get("prior_state"))),
        error=item.get("error"),
        # Everything that is not applied still has a decision left in it.
        # Stated as "not closed" rather than as a list of open states, so a
        # state added later inherits its controls instead of silently losing
        # them -- `failed` lost them exactly that way, and the row became
        # unreachable: no retry, no dismissal, no mute, and no reason given.
        actionable=item["state"] != "applied",
    )


def to_description_row(item, base_url=None):
    """One stored description proposal -> what the inbox shows for it.

    Every payload field is INDEXED, not `.get`: `cronicled.descriptions
    .proposal` writes all five on every proposal it makes, so a payload
    missing one is malformed rather than a description with (say) no
    original text. Reading a missing `original` back as an empty string
    would draw a review panel whose "before" is blank -- which reads as "this
    field was empty and is being filled in", the one interpretation that
    would get a destructive rewrite approved without a second look.
    """
    payload = item["payload"]
    return DescriptionRow(
        kind=KIND_DESCRIPTION,
        fingerprint=item["fingerprint"],
        state=item["state"],
        subject_id=item["subject_id"],
        name=payload["name"],
        performer_url=performer_url(base_url, item["subject_id"]),
        faults=tuple(payload["faults"]),
        original=payload["original"],
        cleaned=payload["cleaned"],
        # An applied row with no snapshot cannot be reverted --
        # `revert_performer_description` raises on an empty one. The same
        # rule `to_row` applies, for the same reason: offering the button
        # would promise an undo the code cannot perform.
        undoable=(item["state"] == "applied"
                  and bool(item.get("prior_state"))),
        actionable=item["state"] != "applied",
        error=item.get("error"),
    )


def to_tag_description_row(item, base_url=None):
    """One stored tag-description proposal -> what the inbox shows for it.

    Every payload field is INDEXED: `cronicled.tag_descriptions.proposal`
    writes all five on every proposal it makes, so a payload missing one is
    malformed rather than a description with (say) no source. `source_box`
    read with a default would be the expensive one -- a row whose provenance
    silently rendered as blank looks exactly like a row whose provenance is
    blank because nobody wrote the sentence, which is the one thing this
    proposal has to be able to say for itself.

    `original` is normalised to `""` for DISPLAY only, because the field it
    describes is empty by construction and `None` renders as the word "None".
    The payload's own value is left exactly as the server gave it, and that is
    what the apply compares against -- see
    `cronicled.web.actions.Actions.approve`.
    """
    payload = item["payload"]
    return TagDescriptionRow(
        kind=KIND_TAG_DESCRIPTION,
        fingerprint=item["fingerprint"],
        state=item["state"],
        subject_id=item["subject_id"],
        name=payload["name"],
        tag_url=tag_url(base_url, item["subject_id"]),
        source_box=payload["source_box"],
        original=payload["original"] or "",
        description=payload["description"],
        # An applied row with no snapshot cannot be reverted --
        # `revert_tag_description` raises on an empty one. The same rule
        # `to_row` and `to_description_row` apply, for the same reason.
        undoable=(item["state"] == "applied"
                  and bool(item.get("prior_state"))),
        actionable=item["state"] != "applied",
        error=item.get("error"),
    )


def to_rows(items, base_url=None):
    """Every stored proposal as the row its own kind of subject needs.

    Dispatched on `subject_type`, the field the store itself keys a mute and
    a refusal by -- never on what a payload happens to contain. A description
    proposal put through `to_row` raises on the very first line
    (`payload["path"]`), which is at least loud; the reverse mistake, a
    payload-shape guess that silently picked the wrong builder, would draw a
    row with the wrong controls on it.
    """
    built = []
    for item in items:
        if item["subject_type"] == DESCRIPTION_SUBJECT:
            built.append(to_description_row(item, base_url=base_url))
        elif item["subject_type"] == TAG_DESCRIPTION_SUBJECT:
            built.append(to_tag_description_row(item, base_url=base_url))
        else:
            built.append(to_row(item, base_url=base_url))
    return built


@dataclass(frozen=True)
class MergeRow:
    """One tag-merge proposal -> what the Merges section shows for it.

    A merge row is deliberately NOT a `Row`. A scene proposal's fields --
    filename, proposed title, creator, score, cover warning, runners-up --
    describe one file's metadata; a merge describes a decision about the
    library's whole vocabulary, and forcing it through `Row` would have cost
    it exactly the two things that make it judgeable: the per-spelling item
    counts, and the warning that the write cannot be taken back.

    **There is no `undoable` field, and that is the design.** `Row` has one
    because a scene apply snapshots what it replaced. A merge cannot be
    reversed (see `cronicled.tags.MERGE_IS_IRREVERSIBLE` for the four
    separate reasons), so there is no state in which this row may offer an
    Undo -- and the cheapest way to guarantee that is for the template to
    have nothing to read.

    `warning` is carried on every row in every state, not only before the
    approve. After the merge the sources are gone, and a page that stopped
    saying so would leave a person looking for the Undo that is not there.
    """
    fingerprint: str
    state: str
    subject_type: str
    subject_id: str
    # The normalised form every spelling in this cluster reduces to.
    key: str
    # Every spelling, as `{"id", "name", "scene_count"}`, in the order the
    # payload carries them (`cronicled.tags.cluster_tags` sorts by name).
    members: tuple
    # The spelling that survives, or `None` when this cluster is a FINDING
    # rather than a merge -- three or more spellings, or two that carry no
    # evidence about which was meant. `undecided` says which, and is `None`
    # exactly when `canonical` is set.
    canonical: str | None
    # The spellings the merge would delete. Empty when `canonical` is `None`:
    # nothing is losing, because nothing has been decided.
    losing: tuple
    undecided: str | None
    # The description the survivor would end up with, and where it comes
    # from -- `inherit_from_tag` when a losing spelling wrote it (which the
    # merge would otherwise delete), `inherit_from_box` when a configured
    # source holds one for a cluster none of whose spellings is described.
    # All three are `None` when there is nothing to carry.
    inherit: str | None
    inherit_from_tag: str | None
    inherit_from_box: str | None
    # Every differing description in the cluster, as `{"name", "description"}`,
    # when two spellings describe the tag two ways. Nothing is chosen in that
    # case and `inherit` is `None`: the merge keeps the survivor's own text
    # and the other is deleted, so the row has to say so out loud rather than
    # let a person approve a write that silently drops one of two sentences
    # somebody wrote.
    conflicting: tuple
    # The blast radius, the number that decides whether this merge is safe.
    # `counts_cover` says what it counts, because it is not everything a tag
    # holds -- see `cronicled.tags.COUNTS_COVER`.
    total_scenes: int
    counts_cover: str
    warning: str
    # Whether Approve is offered. An undecided cluster never offers it: there
    # is no canonical name to merge into, and offering the button would ask a
    # person to authorise a write nothing has specified.
    appliable: bool
    # Whether Dismiss/Mute are offered -- "not closed", stated as the absence
    # of a closed state rather than as a list of open ones, so a state added
    # later inherits its controls instead of silently losing them.
    actionable: bool
    undismissable: bool
    unmutable: bool
    error: str | None


# States in which a merge proposal has no decision left in it, each for its
# own reason: `applied` is done and cannot be undone, `dismissed` and `muted`
# are a person's own standing rejections (each with its own reversal control
# instead), and `superseded` has been retired. Everything else -- `new`,
# `seen`, `failed` -- still has a decision left in it.
_CLOSED_MERGE_STATES = ("applied", "dismissed", "muted", "superseded")


def to_merge_row(item):
    """One tag-merge `item` (the store's dict shape) -> its `MergeRow`.

    Everything is INDEXED, never `.get`: every field read here is written by
    `cronicled.tags.proposal` on every proposal it makes, so an absent one is
    a malformed payload rather than an ordinary "nothing to say". The
    expensive direction is specific -- a missing `scene_count` read back as 0
    would tell a reviewer this merge moves nothing, which is precisely the
    reading that gets a large, irreversible write approved without a second
    thought.
    """
    payload = item["payload"]
    members = tuple(dict(m) for m in payload["members"])
    canonical = payload["canonical"]
    canonical_name = canonical["name"] if canonical else None
    losing = (tuple(m["name"] for m in members
                    if m["id"] != canonical["id"]) if canonical else ())
    state = item["state"]
    open_state = state not in _CLOSED_MERGE_STATES
    # The ONE field here read with a default, and the exception is deliberate.
    # Merge proposals recorded before this pass knew anything about
    # descriptions carry no `description` block at all, and they are still in
    # the store and still on the page. An empty block is the truthful reading
    # of one: that proposal asked no source anything and found no description
    # at risk, so the row offers no carry-over and reports no conflict --
    # exactly the merge it already was. Every other field is indexed, because
    # for every other field a missing value is a malformed payload.
    description = payload.get("description") or {}
    return MergeRow(
        fingerprint=item["fingerprint"],
        state=state,
        subject_type=item["subject_type"],
        subject_id=item["subject_id"],
        key=payload["key"],
        members=members,
        canonical=canonical_name,
        losing=losing,
        undecided=payload["undecided"],
        inherit=description.get("text"),
        inherit_from_tag=description.get("from_tag"),
        inherit_from_box=description.get("from_box"),
        conflicting=tuple(dict(c) for c in description.get("conflicting") or ()),
        total_scenes=sum(m["scene_count"] for m in members),
        counts_cover=payload["counts_cover"],
        warning=MERGE_IS_IRREVERSIBLE,
        appliable=open_state and canonical is not None,
        actionable=open_state,
        undismissable=state == "dismissed",
        unmutable=state == "muted",
        error=item.get("error"),
    )


def to_merge_rows(items):
    return [to_merge_row(i) for i in items]


@dataclass(frozen=True)
class ReconcileRow:
    """One tag-is-really-a-performer proposal -> what its section shows.

    Deliberately NOT a `Row`, a `TagDescriptionRow` or a `MergeRow`, on the
    same terms `MergeRow` is not a `Row`: the question here is neither a
    filename against a candidate title nor two versions of a paragraph. It is
    "is this tag that person, and how many scenes am I about to write to",
    and the two fields that make it judgeable -- what the match was, and the
    blast radius -- have no home on any of the three.

    `performer` is the name of the one performer this tag resolves to, or
    `None` when two or more answer to it. `matches` lists EVERY one of them,
    always, including the single-match case: the row shows what it matched, and
    a field that only appeared in the ambiguous case would mean the ordinary
    row had to say it a second way.

    `alias` is the alias surface the match came through, or `None` on a name
    match. Shown because "this tag is that performer's name" and "this tag is
    one of the aliases recorded for them" are different strengths of evidence.

    `undoable` is NOT the `state == "applied"` rule the other rows use, and the
    difference is the whole reason this row exists in a `failed` state at all.
    A reconciliation writes to many scenes one at a time; a run that failed
    partway through really did change the scenes it got to, and the snapshot
    recording them is the only way back. So the Undo is offered whenever a
    snapshot exists -- and `appliable` is withheld while one does, because a
    second attempt would record a snapshot covering only ITS scenes and the
    first batch would become unrecorded. Undo first, then approve again.
    """
    fingerprint: str
    state: str
    subject_type: str
    subject_id: str
    tag_name: str
    # The media server's own page for the tag, or `None` with no server
    # configured -- see `tag_url`.
    tag_url: str | None
    performer: str | None
    performer_url: str | None
    alias: str | None
    # Every performer answering to this tag's name, as
    # `{"id", "name", "alias", "url"}`, in the payload's own order
    # (`performer_tags.index_performers` sorts by name and id).
    matches: tuple
    ambiguous: str | None
    # The blast radius, derived from the recorded scene set rather than stored
    # beside it, so the number shown and the set it describes cannot disagree.
    total_scenes: int
    counts_cover: str
    appliable: bool
    actionable: bool
    undoable: bool
    undismissable: bool
    unmutable: bool
    error: str | None


# States in which a reconciliation has no decision left to offer. The same
# four `_CLOSED_MERGE_STATES` names and for the same reasons -- `applied` is
# done (and has Undo instead), `dismissed` and `muted` are standing rejections
# with their own reversal controls, `superseded` has been retired. `failed` is
# NOT here: nothing was written, or only part was, and either way the row still
# has a decision in it.
_CLOSED_RECONCILE_STATES = ("applied", "dismissed", "muted", "superseded")


def _match_view(entry, base_url):
    """One performer answering to a tag's name, as the section shows it.

    Every field INDEXED: `performer_tags.proposal` writes all of them on every
    proposal it makes. `alias` being `None` is an ordinary answer (the
    performer's own name matched); the KEY being absent is a payload from
    somewhere that never asked, and reading the two the same way would report
    an alias match as a name match -- the stronger of the two claims, made on
    the weaker of the two matches.
    """
    performer = entry["performer"]
    return {"id": performer["id"], "name": performer["name"],
            "alias": entry["alias"],
            "url": performer_url(base_url, performer["id"])}


def to_reconcile_row(item, base_url=None):
    """One stored reconciliation `item` -> its `ReconcileRow`.

    Everything is INDEXED, never `.get`: `cronicled.performer_tags.proposal`
    writes every field read here on every proposal it makes. The expensive
    direction is specific and it is `scenes` -- a missing list read back as
    empty would put a blast radius of nothing in front of a reviewer for a
    write that might touch thousands of scenes, which is precisely the reading
    that gets a large write approved without a second thought.

    `prior_state` is the one field read with `.get`, because the store itself
    leaves it null until something is applied.
    """
    payload = item["payload"]
    performer = payload["performer"]
    state = item["state"]
    open_state = state not in _CLOSED_RECONCILE_STATES
    snapshot = bool(item.get("prior_state"))
    return ReconcileRow(
        fingerprint=item["fingerprint"],
        state=state,
        subject_type=item["subject_type"],
        subject_id=item["subject_id"],
        tag_name=payload["tag"]["name"],
        # From `subject_id`, which is also what the page labels the row with --
        # one id, so the link and the label cannot point at different tags.
        tag_url=tag_url(base_url, item["subject_id"]),
        performer=performer["name"] if performer else None,
        performer_url=(performer_url(base_url, performer["id"])
                       if performer else None),
        alias=payload["alias"],
        matches=tuple(_match_view(m, base_url) for m in payload["matches"]),
        ambiguous=payload["ambiguous"],
        total_scenes=len(payload["scenes"]),
        counts_cover=payload["counts_cover"],
        appliable=open_state and performer is not None and not snapshot,
        actionable=open_state,
        undoable=snapshot and state in ("applied", "failed"),
        undismissable=state == "dismissed",
        unmutable=state == "muted",
        error=item.get("error"),
    )


def to_reconcile_rows(items, base_url=None):
    return [to_reconcile_row(i, base_url=base_url) for i in items]


@dataclass(frozen=True)
class UnusedTagRow:
    """One "this tag does almost no work" proposal -> what its section shows.

    Deliberately not any of the four rows above, on the same terms `MergeRow` is
    not a `Row`: the question is neither a filename against a candidate title,
    nor two versions of a paragraph, nor "is this tag that person". It is "this
    tag classifies this many scenes -- is it worth keeping", and the two fields
    that make it judgeable are the count and what deleting it would cost.

    **There is no `undoable` field, and that is the design.** A deletion cannot
    be reversed (see `cronicled.tag_hygiene.DELETE_IS_IRREVERSIBLE`), so there
    is no state in which this row may offer an Undo, and the cheapest way to
    guarantee that is for the template to have nothing to read. `MergeRow` omits
    it for the same reason.

    `warning` is carried on every row in every state, not only before the click.
    Afterwards the tag is gone, and a page that stopped saying so would leave a
    person looking for the Undo that is not there. It is the warning for THIS
    row's group, because whether a scene changed is the one thing the two groups
    differ on and the count alone does not say it out loud.
    """
    kind: str
    fingerprint: str
    state: str
    subject_type: str
    subject_id: str
    name: str
    # The media server's own page for this tag, or `None` with no server
    # configured -- see `tag_url`. The one link a person has for checking a
    # deletion before approving it, which is why it is here even though the
    # count is meant to be sufficient: the count is what this pass can see, and
    # the page is where the markers, images and galleries it cannot see are.
    tag_url: str | None
    # The count AS MEASURED WHEN THE PASS RAN. Shown rather than turned into a
    # verdict: the row states how much work the tag does and a person decides.
    # `Stash.delete_tag` re-reads it before the write, so a stale one refuses
    # the deletion rather than acting on it.
    scene_count: int
    counts_cover: str
    # Which population this row is in, derived from the count by the one rule
    # the producer classified with (`tag_hygiene.group_of`) -- never a second
    # field stored beside the count that could disagree with it.
    group: str
    warning: str
    appliable: bool
    actionable: bool
    undismissable: bool
    unmutable: bool
    error: str | None


@dataclass(frozen=True)
class UnusedTagGroup:
    """One population, as the single expandable row that stands for it.

    THIS TYPE IS THE POINT OF THE TICKET. A library measured for this had 256
    tags on no scenes and 793 on exactly one, and both of the obvious shapes are
    wrong:

    * **1049 top-level rows** buries every other kind of proposal on the page
      under one category, and a review queue that has stopped being readable has
      stopped being read.
    * **one "delete all unused tags" button** is a single irreversible write over
      a set assembled by a rule nobody checked, and it cannot be examined before
      it is pressed.

    So the GROUP is what the page lists and the group is expandable; the
    DECISIONS stay per-tag inside it. `rows` carries every row, each with its own
    count and its own controls, and this type carries no fingerprint and no
    control of its own -- there is nothing for a template to post, which is how
    "no bulk delete" is guaranteed rather than merely intended.

    `count` is `len(rows)`, derived here rather than carried alongside, so the
    number a person reads and the set it describes cannot drift apart. The same
    rule `ReconcileRow.total_scenes` follows.
    """
    group: str
    label: str
    note: str
    count: int
    rows: tuple


# States in which a deletion proposal has no decision left to offer. The same
# four names as `_CLOSED_MERGE_STATES` and `_CLOSED_RECONCILE_STATES`, for the
# same reasons -- `applied` is done and cannot be undone, `dismissed` and `muted`
# are standing rejections with their own reversal controls, `superseded` has been
# retired. `failed` is NOT here: nothing was deleted, so the row is as live as it
# was before the attempt.
_CLOSED_UNUSED_STATES = ("applied", "dismissed", "muted", "superseded")


def to_unused_row(item, base_url=None):
    """One stored deletion proposal -> its `UnusedTagRow`.

    Every payload field is INDEXED: `cronicled.tag_hygiene.proposal` writes all
    three on every proposal it makes. `scene_count` is the expensive one --
    read with a default it would report 0 for a malformed payload, which is
    exactly the number that reads as "deleting this changes nothing", for a tag
    that might be on thousands of scenes.

    A payload whose count belongs to NEITHER population raises rather than
    rendering. The producer never makes one, so it is a malformed payload; and
    the alternative -- a row with no group -- would either vanish from the page
    or land in a group whose note makes a claim about it that nothing checked.
    """
    payload = item["payload"]
    group = tag_hygiene.group_of(payload)
    if group is None:
        raise ValueError(
            "proposal %s says tag %r is on %r scenes, which is neither of the "
            "two populations this section shows"
            % (item["fingerprint"], payload["name"], payload["scene_count"]))
    state = item["state"]
    open_state = state not in _CLOSED_UNUSED_STATES
    return UnusedTagRow(
        kind=KIND_UNUSED_TAG,
        fingerprint=item["fingerprint"],
        state=state,
        subject_type=item["subject_type"],
        subject_id=item["subject_id"],
        name=payload["name"],
        # From `subject_id`, which is also what the write goes against -- one
        # id, so the link a person checks and the tag that gets deleted cannot
        # be two different tags.
        tag_url=tag_url(base_url, item["subject_id"]),
        scene_count=payload["scene_count"],
        counts_cover=payload["counts_cover"],
        group=group,
        warning=tag_hygiene.DELETE_WARNING[group],
        appliable=open_state,
        actionable=open_state,
        undismissable=state == "dismissed",
        unmutable=state == "muted",
        error=item.get("error"),
    )


def to_unused_groups(items, base_url=None):
    """Every stored deletion proposal, as one expandable group per population.

    In `tag_hygiene.GROUPS` order -- safest population first -- and a population
    with nothing in it produces NO group at all rather than an empty one: a row
    saying "0 tags on no scenes" is a line that is always on the page and so
    stops being read, and the section already says out loud when it is empty
    altogether.

    Rows inside a group are ordered by `(name, subject_id)` -- CONTENT, never
    the order the store happened to return them in. Two tags can share a name
    (that is what the merge half is for), so the id breaks the tie, and the
    result is a list that reads the same on every visit instead of shifting as
    proposals are recorded.
    """
    built = {group: [] for group in tag_hygiene.GROUPS}
    for item in items:
        row = to_unused_row(item, base_url=base_url)
        built[row.group].append(row)
    groups = []
    for group in tag_hygiene.GROUPS:
        rows = sorted(built[group], key=lambda r: (r.name, r.subject_id))
        if not rows:
            continue
        groups.append(UnusedTagGroup(
            group=group,
            label=tag_hygiene.GROUP_LABEL[group],
            note=tag_hygiene.GROUP_NOTE[group],
            count=len(rows),
            rows=tuple(rows)))
    return tuple(groups)


def _refused_store_view(entry):
    """One searched store's line in the Refused section.

    `entry` is one of `Store.refusals()`'s `stores`, built by
    `cronicled.scan._store_report` -- read with `[]` rather than `.get`, for
    the reason `to_row` states for a payload: an entry that exists but
    cannot answer is malformed, and a default here would render a store's
    line as a confident blank instead of failing where it can be seen.

    `outcome` is the whole editorial decision, and it is made HERE rather
    than in the template (see `_runner_up_view` for the same split): reading
    it wrong there is a silently empty line, since Jinja renders an undefined
    attribute as empty text rather than raising.

    A store that both answered and then raised leads with the failure. Its
    rows and score are still carried -- nothing is dropped -- but the failure
    is what changes their meaning: "40 returned, best 0.236" reads as "this
    store has nothing like your file", and a store whose narrower follow-up
    query never completed has not shown that.

    `url` is passed through exactly as recorded, `None` included. Deriving an
    address for a candidate that carries none would be inventing one, and the
    template's `identifier` macro already renders a missing url as plain text
    -- the same degradation a row with no configured media server gets for
    its filename.
    """
    if entry["error"] is not None:
        outcome = STORE_FAILED
    elif entry["rows"] == 0:
        outcome = STORE_EMPTY
    else:
        outcome = STORE_ANSWERED
    return {
        "store": entry["store"],
        "outcome": outcome,
        "rows": entry["rows"],
        "score": entry["score"],
        "title": entry["title"],
        "url": entry["url"],
        "error": entry["error"],
    }


def to_refusal_row(entry, base_url=None):
    """One standing refusal (`Store.refusals()`'s dict shape) -> what the
    Refused section shows for it.

    `stores` is one line per store the scan searched, in the order the scan
    recorded them (closest miss first -- see `scan._store_reports`). Nothing
    here re-orders them: a second ordering rule in the row builder would be
    free to disagree with the one that had the scores in front of it.

    `filename`, not the whole path, matching every other row's own
    editorial choice (`to_row`'s `filename` above) — the directory is the
    reviewer's own filing, not part of judging why a candidate did not
    clear the threshold.

    Refused rows are, per ticket 97, one of the two lists where a person
    has the least to go on already -- no title, no creator, nothing but a
    filename and a reason -- so the scene link matters here as much as it
    does for `to_mute_row`.
    """
    return {
        "subject_type": entry["subject_type"],
        "subject_id": entry["subject_id"],
        "filename": os.path.basename(entry["path"]),
        "reason": entry["reason"],
        "at": entry["at"],
        "scene_url": scene_url(base_url, entry["subject_id"]),
        "stores": tuple(_refused_store_view(s) for s in entry["stores"]),
    }


def to_refusal_rows(entries, base_url=None):
    return [to_refusal_row(e, base_url=base_url) for e in entries]


def to_mute_row(entry, base_url=None, *, zone):
    """One standing mute (`Store.mutes()`'s dict shape) -> what the Muted
    section shows for it.

    `at` is converted to `zone` for display and the store's own row is not
    touched -- see this module's docstring for why that direction is the only
    one this project takes. `zone` is keyword-only and required for the reason
    `local`'s is: defaulted, a caller who forgot it would get UTC, which is
    indistinguishable from the conversion not existing.

    `row` is the proposal behind the mute, built by the SAME builders the
    Dismissed section's rows are built by (`to_rows`, so a muted performer
    gets a `DescriptionRow` and a muted scene a `Row`) -- not a second,
    thinner projection of the same item. Both sections show something a
    person hid and may want back, and they were not comparable: a dismissed
    row named the file, the proposed title, the attribution and the score,
    while a muted one showed a subject id and a sentence. Sharing the
    builder is what keeps them from drifting apart again, and it is why
    `Store.mutes()` hands over the whole item rather than its payload.

    `row` is `None` -- and ONLY then -- when the store recovered no item at
    all: a subject muted ahead of any scan ever finding it, the genuine
    exception ticket 97 names. That case is real and stays honest; the
    template marks it visibly as the exception rather than drawing the rich
    shape with every field blank, which is the one reading that would make
    "nothing was ever proposed here" look like "the page failed to render
    it". Deciding that is the template's job, not this function's -- what
    this returns is `None`, never a filled-in stand-in.

    `subject_url` is what that exception branch links, and it is the only
    thing this still derives itself: with no item there is no row to carry
    an address, but the mute's own `(subject_type, subject_id)` is enough
    for the media server's page. WHICH page depends on the kind -- a mute
    can be placed on ANY subject a producer proposes about, so one click on
    a description proposal's Mute button puts a performer in this list, and
    a performer's page is not a scene's.
    """
    performer = entry["subject_type"] == DESCRIPTION_SUBJECT
    item = entry["item"]
    return {
        "subject_type": entry["subject_type"],
        "subject_id": entry["subject_id"],
        "reason": entry["reason"],
        "at": local(entry["at"], zone),
        "row": None if item is None else to_rows([item], base_url=base_url)[0],
        "subject_url": (performer_url(base_url, entry["subject_id"])
                        if performer
                        else scene_url(base_url, entry["subject_id"])),
    }


def to_mute_rows(entries, base_url=None, *, zone):
    return [to_mute_row(e, base_url=base_url, zone=zone) for e in entries]


def to_schedule_view(status, *, zone):
    """A `cronicled.schedule.LoopStatus` with every timestamp on it shown in
    `zone`. `None` in, `None` out -- the answer for an install where nothing is
    scheduled, which the page says out loud.

    The same type back, built with `dataclasses.replace`, rather than a dict or
    a thinner projection. Two reasons, and the second is the one that decided
    it: the template already reads a dozen fields off this object and a
    projection would have to list every one of them, so the next field added to
    `LoopStatus` would render as empty text on the page -- Jinja renders an
    undefined attribute as nothing rather than raising, which is the failure
    mode this whole layer is arranged to avoid.

    FIVE PLACES CARRY A TIME, not one, and they are converted together because
    a page mixing zones is unreadable in a way a page in one wrong zone is not:

    - `last_tick_at`, the answer to "when did it last look";
    - `last_error_at`, beside a failure a person is placing against something
      else they did;
    - `last_result.at`, the moment the tick decided against;
    - the reasons in `last_result.skipped` and `last_result.failed_to_start`,
      which are sentences with instants inside them ("last ran ...; next due at
      ...") -- see `local_times`;
    - `last_error` itself, which is an exception's own message and can name an
      instant: `schedule._previous_occurrence` and `due` both put one in theirs.

    `last_traceback` is deliberately left alone. It is the only field here the
    template does not render, and frames are evidence to be read as they were
    recorded rather than a sentence for a person -- if it is ever put on the
    page, it comes through `local_times` like the rest.

    The counts, the names and the job ids are passed through untouched. What is
    NOT touched anywhere is the loop's own bookkeeping: this builds a copy for
    the page, and `LoopStatus` is frozen, so the values the scheduler goes on
    comparing against the store stay in UTC however often the page is drawn.
    """
    if status is None:
        return None
    result = status.last_result
    if result is not None:
        result = replace(
            result,
            at=local(result.at, zone),
            skipped={name: local_times(reason, zone)
                     for name, reason in result.skipped.items()},
            failed_to_start={name: local_times(reason, zone)
                             for name, reason in
                             result.failed_to_start.items()},
        )
    return replace(status,
                   last_tick_at=local(status.last_tick_at, zone),
                   last_error_at=local(status.last_error_at, zone),
                   last_error=local_times(status.last_error, zone),
                   last_result=result)


def local_minute(value, zone):
    """A stored UTC instant as `YYYY-MM-DD HH:MM` in `zone`.

    AN INSTANT KEEPS ITS DATE. This shortens the seconds and the offset off a
    recorded moment and nothing else, because "when did the nightly pass run"
    is a question about a day as much as about an hour: a bare `03:00` beside a
    run cannot tell last night's from the one three weeks ago, and a reader
    checking whether a pass ran is asking exactly that. The offset goes because
    the page states its zone once (see `to_summary_view`) rather than repeating
    it on every row; the DATE is not the same kind of repetition.

    A STATED APPOINTMENT is the other case and is not this one. "03:00 every
    night" names no date because it has none, and shortening it loses nothing.
    That is `to_schedule_view`'s side of the line, and the two must not be
    confused in either direction -- an appointment carrying a date invents one,
    and an instant that loses its date destroys one.

    Anything this cannot read as an instant comes back exactly as it went in,
    `None` included, for the reason `local` gives at length: a stamp with no
    offset names no instant, and converting it would put an hour on the page
    that was never recorded. `None` is a real value here -- a run that started
    and has not finished has no `finished` -- and it renders as the template's
    own "still running", not as an invented time.
    """
    moment = as_utc(value)
    if moment is None:
        return value
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def to_summary_view(runs, waiting, schedule, *, zone):
    """The landing page: each job's last run, what is waiting, and the loop.

    `runs` is `Store.recent_runs()` -- NEWEST FIRST -- so the first row naming
    a job is that job's most recent run and every later one is history. That
    "newest first" is a guarantee of the store's own `ORDER BY started DESC,
    rowid DESC` and NOT of this loop: `_utcnow` records to the second, so two
    runs of one job started in the same second carry an identical `started`,
    and ordering on that column alone leaves the tie to SQLite -- which returns
    the OLDER one first. Take the tiebreak away and this page silently reports
    the earlier of the two as though it were the latest, which is the one
    reading that makes a stale page look like a healthy one.

    A run still in progress is a first occurrence like any other and takes the
    slot. That is deliberate: a job running right now IS its latest run, and a
    page that skipped it to show the last completed one would answer "has
    tonight's pass started?" with last night's.

    `waiting` is carried through untouched, keys and all. What the sections are
    called and how they are counted is the caller's decision -- this does not
    interpret the mapping, so a producer added there appears here without an
    edit rather than being silently dropped by a builder that knew three names.

    `schedule` is a raw `LoopStatus` and is converted HERE, through
    `to_schedule_view`, rather than being handed over ready-made. One
    convention for the whole signature -- everything in is what the store and
    the loop recorded, everything out is in `zone` -- because the alternative
    is a parameter that is already converted sitting beside two that are not,
    and a caller who mixed them up would render the schedule in UTC beside run
    times in the operator's hour with nothing on the page saying which is
    which.

    `zone` is named ONCE, in the returned view, because no timestamp above
    carries an offset any more. A page that shortens its times and then does
    not say what they are shortened to is worse than one that left them in UTC.
    """
    jobs = []
    seen = set()
    for run in runs:
        # Indexed, never `.get`: a row missing `job` is a row nothing can
        # attribute, and defaulting it would group every such run under one
        # invented heading that reads on the page as a job that exists.
        job = run["job"]
        if job in seen:
            continue
        seen.add(job)
        jobs.append({
            "id": run["id"],
            "job": job,
            "trigger": run["trigger"],
            "started": local_minute(run["started"], zone),
            "finished": local_minute(run["finished"], zone),
            "outcome": run["outcome"],
            # BOTH, always, and the template picks. A failed run's counts are
            # whatever it had got to before it stopped, so rendering them as
            # its result reads as "the pass ran and found this much" -- see
            # summary.html, which shows the error instead and says why there.
            "counts": run["counts"],
            "error": run["error"],
        })
    return {
        "jobs": jobs,
        "waiting": waiting,
        "schedule": to_schedule_view(schedule, zone=zone),
        "zone": str(zone),
    }
