"""Choosing which files a library scan works on.

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
from dataclasses import dataclass

# Selection deals in one kind of subject. It is named rather than inlined so
# a test and the store agree on the same string, and so a future second kind
# has to change this deliberately instead of by coincidence.
SUBJECT_TYPE = "scene"


@dataclass(frozen=True)
class Counts:
    """Why a scan's input became the batch it became.

    `total` is everything offered; `selected` is what will actually be worked;
    the middle three are the reasons a file was dropped, each file counted
    under exactly one of them (see `select` for the precedence).

    When the limit does not bind, the four sum to `total` — every file
    accounted for. When it does bind, they sum to LESS than `total`, and the
    shortfall is files deferred to a later run rather than dropped: nothing
    decided against them, they simply did not fit in this budget.
    """
    total: int
    already_proposed: int
    muted: int
    filtered_out: int
    selected: int


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


def select(scenes, *, store, folder, name_filter=None, limit=None):
    """The files a scan should work, and why the others were dropped.

    Returns `(selected, counts)`, `selected` being the surviving scene dicts
    in the order they were offered.

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
    honoured) instruction, not a missing limit.

    Scope note, mirroring what the store actually enforces: a mute is keyed by
    subject alone and blocks a proposal in any folder, so the muted check is
    not scoped to `folder`. An existing proposal is a row *in* a folder, so
    that check is.

    Both store questions are asked at the SUBJECT level, via `store.items()`,
    not at the fingerprint level via `store.has()`. That is forced, not
    preferred: a fingerprint covers a proposal's payload, and at selection
    time — before any scoring or lookup — there is no payload to hash. The
    question selection actually needs to ask is "has this file already been
    decided about", which is about the subject.

    One known hole, in the muted check specifically. The store's `mute` table
    has no public read, so muted subjects are read from the `item` rows the
    mute moved into the `muted` state. A subject muted PRE-EMPTIVELY — before
    any proposal for it existed, which the store explicitly allows — has no
    such row and is therefore invisible here: this scan will spend a lookup on
    it, and `record()` will then refuse the result. That is the wasteful case
    this module exists to avoid, and closing it needs a read accessor on the
    store (e.g. `muted_subjects()`), which is a change to `Store` rather than
    to this module. The failure is at least in the safe direction: a wasted
    lookup, not a proposal that escapes a mute.

    A dismissed proposal does NOT suppress its file here. Dismissal rejects
    one proposal, not the subject — a better proposal for the same file is
    allowed to arrive tomorrow, and it cannot if the file is never looked at.
    Muting is the mechanism for "stop offering me this file".
    """
    if limit is not None and limit < 0:
        # `scenes[:-1]` would quietly drop the LAST file rather than select
        # nothing, so a negative limit must not reach a slice.
        raise ValueError(f"limit must not be negative, got {limit!r}")

    scenes = list(scenes)
    pattern = (name_filter or "").casefold()
    muted = _subjects(store.items(state="muted"))
    proposed = _subjects(store.items(folder=folder))

    narrowed = []
    filtered_out = muted_count = already_proposed = 0
    for scene in scenes:
        subject_id = str(scene["id"])
        paths = _paths(scene)
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
    )
