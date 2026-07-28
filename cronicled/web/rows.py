"""Store item -> what the inbox shows for it.

Kept apart from rendering and from HTTP because the interesting decision here
is editorial: which facts does a person need in front of them to judge a
proposal without opening anything else. That is worth testing on its own.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    fingerprint: str
    state: str
    filename: str
    proposed_title: str
    creator: str
    creator_source: str
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
    score: float
    score_text: str
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


def _runner_up_view(entry):
    """One losing candidate, normalised to what the screen shows.

    `scan._runners_up` builds each entry as `{"candidate": <the whole search
    result>, "score": value}` -- the title lives inside `candidate`, not at
    the top level. Deciding what the "also considered" column needs is this
    module's job, not the template's: reading `title` from the wrong place
    here would surface as a silently blank column there, since Jinja renders
    an undefined attribute as empty text rather than raising.
    """
    return {"title": entry["candidate"]["title"], "score": entry["score"]}


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


def _disagreement(creator):
    """One line naming what the resolver passed over, or None.

    The two kinds are kept apart deliberately upstream and are phrased apart
    here: `competing` is a name a reviewer could go and search for;
    `rejected_folder` is folder text that failed the guards and treating it as
    a name is the mistake the resolver exists to prevent.
    """
    competing = creator.get("competing")
    rejected = creator.get("rejected_folder")
    parts = []
    if competing:
        parts.append("the filename names %s instead" % competing)
    if rejected:
        parts.append("the folder text %r was not usable as a name" % rejected)
    return "; ".join(parts) if parts else None


def to_row(item):
    payload = item["payload"]
    # Indexed, not .get(): a proposal without a creator is malformed, and a
    # blank creator column reads as "nothing disagreed" — the reading that
    # gets a wrong row approved.
    creator = payload["creator"]
    disagreement = _disagreement(creator)
    candidate = payload["candidate"]
    score = payload["score"]
    return Row(
        fingerprint=item["fingerprint"],
        state=item["state"],
        filename=os.path.basename(payload["path"]),
        proposed_title=candidate["title"],
        creator=creator["name"],
        creator_source=creator["source"],
        contested=disagreement is not None,
        disagreement=disagreement,
        carries_cover=carries_cover(candidate),
        performers=_performer_names(candidate),
        studio=_studio_name(candidate),
        score=score,
        # Three places, matching the precision the decision was made at.
        score_text="%.3f" % score,
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


def to_rows(items):
    return [to_row(i) for i in items]


def to_refusal_row(entry):
    """One standing refusal (`Store.refusals()`'s dict shape) -> what the
    Refused section shows for it.

    `filename`, not the whole path, matching every other row's own
    editorial choice (`to_row`'s `filename` above) — the directory is the
    reviewer's own filing, not part of judging why a candidate did not
    clear the threshold.
    """
    return {
        "subject_type": entry["subject_type"],
        "subject_id": entry["subject_id"],
        "filename": os.path.basename(entry["path"]),
        "reason": entry["reason"],
        "at": entry["at"],
    }


def to_refusal_rows(entries):
    return [to_refusal_row(e) for e in entries]
