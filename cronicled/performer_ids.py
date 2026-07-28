"""Turn the media server's OWN performer records into a {creator name:
stash-box performer id} mapping, rather than guessing one from name
similarity -- see `cronicled.stashbox`'s own docstring for why a wrong guess
here is worse than no answer at all: a wrong id produces a listing read in
full for somebody else, and the verdict then reads as though it settled
something it never asked about.

A performer entity on the media server carries its own `stash_ids`: the
same `{endpoint, stash_id}` shape `cronicled.stash.Stash.scene_existing`
already reads for a scene, `cronicled.stash.Stash.performers_with_stash_ids`
reads for a performer instead. Where a performer this library already holds
has been linked to a stash-box instance -- by a scrape, an apply, or an
operator's own edit -- that link is DATA the library already recorded about
a real match, not a string-similarity guess between a resolved name and a
performer name that happens to look alike. Restricting the derivation to one
configured `endpoint` matters for the same reason `cronicled.stashbox
.StashBox`'s own `url` picks one instance: a performer can carry ids from
more than one stash-box, and conflating them would file a listing read
under the wrong service's id.

Two things stop this from ever guessing:

* **a name two different performers share at the endpoint is refused, not
  resolved by whichever one the server happened to list first.** See
  `derive_performer_ids`.
* **an operator's own entry always wins over a derived one for the same
  name.** See `merge_performer_ids` -- the same trust already extended to a
  `stored_id` in `cronicled.stash.Stash.find_or_create`, which never
  re-validates a caller-supplied id against a fresh search either.

This module never resolves a creator name out of a folder or a filename --
that is `cronicled.artist.resolve`'s job, done once per file, well before a
performer id is ever looked up. It only turns what the media server already
knows about its own performers into the mapping
`cronicled.stashbox_scan.StashBoxCheckProducer` needs.
"""
from collections import defaultdict


class DerivedPerformerIds:
    """`ids`: {name: stash-box performer id}, one entry for every name this
    server links to EXACTLY one performer id at the configured endpoint.

    `ambiguous`: {name: (id, id, ...)}, sorted, for every name two or more
    performers share there. Kept rather than dropped so a caller can report
    it -- see `merge_performer_ids` -- rather than lose it silently. A name
    is never in both dicts at once.
    """

    def __init__(self, ids, ambiguous):
        self.ids = dict(ids)
        self.ambiguous = dict(ambiguous)

    def __repr__(self):
        return "DerivedPerformerIds(ids=%d, ambiguous=%d)" % (
            len(self.ids), len(self.ambiguous))


def _stash_id_at(performer, endpoint):
    """The stash-box id `performer` carries for `endpoint`, or None.

    A performer record naming the SAME endpoint twice with two different
    ids is a malformed record this function does not try to adjudicate --
    the first one found wins, on the same terms `StashID` is documented as
    one link per external system. Nothing in this module treats that as
    the kind of ambiguity `derive_performer_ids` reports: that ambiguity is
    between two performer RECORDS competing for one NAME, which is the
    question this module exists to answer; a single record naming one
    endpoint twice is a different, much rarer defect this ticket does not
    attempt to detect.
    """
    for entry in performer.get("stash_ids") or ():
        if entry.get("endpoint") == endpoint:
            return entry.get("stash_id")
    return None


def derive_performer_ids(stash, endpoint):
    """Read every performer `stash` holds and build a `DerivedPerformerIds`
    for `endpoint` (the stash-box instance's own normalised URL -- see
    `cronicled.stashbox.StashBox.url`).

    Grouping is by the performer's exact `name` -- the same exact-string key
    `cronicled.stashbox_scan.StashBoxCheckProducer` already looks a resolved
    creator up by, so a name `cronicled.artist.resolve` produced is looked
    up here on the same terms it would be looked up in a hand-maintained
    mapping. Two performers sharing a name is real on a library of any
    size -- a stage name is not unique -- so a name mapping to more than one
    DISTINCT id at this endpoint is never resolved by iteration order; it is
    reported in `ambiguous` and left out of `ids` entirely. Two performer
    records that happen to carry the SAME id for the same name (e.g. a
    duplicate performer entry) are not a conflict -- there is only one
    answer between them, so that name still resolves.
    """
    by_name = defaultdict(set)
    for performer in stash.performers_with_stash_ids():
        pid = _stash_id_at(performer, endpoint)
        if pid is None:
            continue
        by_name[performer["name"]].add(pid)

    ids, ambiguous = {}, {}
    for name, pids in by_name.items():
        if len(pids) == 1:
            ids[name] = next(iter(pids))
        else:
            ambiguous[name] = tuple(sorted(pids))
    return DerivedPerformerIds(ids, ambiguous)


def merge_performer_ids(manual, derived):
    """`manual` (an operator-typed {name: id} mapping) filled out with
    `derived`'s (a `DerivedPerformerIds`) entries for every name `manual`
    does not already name.

    An operator's own entry always wins for a name it names at all -- the
    same trust `cronicled.stash.Stash.find_or_create` already extends to a
    caller-supplied `stored_id`, which is never re-validated against a fresh
    search either. That is what lets a name TWO performers share at the
    endpoint (`derived.ambiguous`) still be used: an operator who knows
    which of the two is the file's actual creator may say so directly in
    the mapping, and that is a deliberate decision recorded by a person, not
    the iteration-order guess this module refuses to make on its own.

    Returns `(ids, unresolved)`: `ids` is the merged {name: id} mapping;
    `unresolved` is `derived.ambiguous` minus every name `manual` already
    settled -- every name derivation could not tell apart and the operator
    did not either, so a caller can report it rather than dropping it
    silently.
    """
    ids = dict(derived.ids)
    ids.update(manual)
    unresolved = {name: pids for name, pids in derived.ambiguous.items()
                  if name not in manual}
    return ids, unresolved
