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
    score: float
    score_text: str
    runners_up: tuple
    undoable: bool


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
    score = payload["score"]
    return Row(
        fingerprint=item["fingerprint"],
        state=item["state"],
        filename=os.path.basename(payload["path"]),
        proposed_title=payload["candidate"]["title"],
        creator=creator["name"],
        creator_source=creator["source"],
        contested=disagreement is not None,
        disagreement=disagreement,
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
    )


def to_rows(items):
    return [to_row(i) for i in items]
