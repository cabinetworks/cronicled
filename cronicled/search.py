"""Turns a `Stash` and a `SiteAdapter` into the `search` callable
`cronicled.scan.examine` / `cronicled.scan.ScanProducer` take as an injected
lookup.

Nothing in `cronicled.scan` executes a query on its own, by design — see
`scan.examine`'s docstring: the expensive, networked half belongs to the
caller. This module is that caller. `catalog_search(stash, adapter)` returns
a one-argument callable, `search(name)`, that asks `adapter.scraper_id` (via
`stash.scrape_scenes_by_query`) for every censored-spelling variant of `name`
that `cronicled.censorship.search_variants` proposes — using
`adapter.censorship`, the store's own substitution map — and returns the
union of what came back.

`scan.examine` calls `search(resolution.name)` — the creator's NAME, not a
title — because a lookup per creator, not per file, is the shape the
measured scoring threshold in `cronicled.scoring` assumed (roughly 99 lookups
against a real library instead of roughly 5,924).
`SiteAdapter.search_query(seed, title_query)` describes the OTHER shape, one
query per title; it is not used here and stays unused — see its own
docstring for why.

`cronicled.censorship.decensor` — the map's other half — is deliberately NOT
used here. It rewrites a STORE TITLE back to canonical words for SCORING,
and scoring happens in `scan.examine`, the one place that also holds the raw
candidate a proposal must carry unrewritten. Wiring it here as well would be
a second place a future reader has to check that the rewritten form never
reaches a payload, for no benefit this module could name.
"""
import json

from cronicled.censorship import search_variants


def catalog_search(stash, adapter):
    """The production `search` callable, built once per `(stash, adapter)`
    pair and handed to `scan.examine` / `scan.ScanProducer`.

    `stash` is anything with `scrape_scenes_by_query(scraper_id, query)` —
    ordinarily a `cronicled.stash.Stash` — and is never imported by this
    module, only called: that keeps this callable exercisable against a fake
    that opens no socket, the same discipline `cronicled.stash.Stash` itself
    follows for its transport.

    Every variant `search_variants` proposes for `name` (bounded to 6 — see
    its own docstring) is asked of `adapter.scraper_id` in turn, and the
    results are pooled and deduplicated. Deduplication matters, not just
    tidiness: the SAME store scene answering two spelling variants of one
    name is the expected case this exists to handle, and an undeduplicated
    pair is two identical rows reaching `scoring.decide` — which can turn a
    genuine winner into an "ambiguous: X vs X" refusal, an artefact of how
    the query happened to be phrased rather than evidence about the file.

    Deduplication is by exact equality of the whole row: a real duplicate
    comes back byte-identical, because it is the same scraped page read
    twice. A row that merely resembles another is kept as a distinct
    candidate rather than folded into it — collapsing two rows that are not
    provably the same scene would be a guess this project does not make
    elsewhere (see `_query_key` in `cronicled.scan`, which folds only case
    and whitespace for the same reason). The accepted cost of the stricter
    rule: a scraper that reorders a nested list (`tags`, `performers`)
    between two otherwise-identical answers is not deduplicated, and a
    genuine duplicate survives into the candidate list once per spelling
    that found it. That costs the ambiguity margin `scoring.decide` enforces,
    which is the same failure this function exists to avoid — but never
    wrongly merges two different scenes, which is the worse of the two ways
    to be wrong.

    Returns a fresh list on every call. `examine` indexes into what `search`
    returns to build its runners-up list, and a list this function kept a
    reference to would let a second caller's iteration disturb the first's —
    the same aliasing `cronicled.scan._SingleFlight.__call__` avoids by
    returning `list(flight.result)` rather than `flight.result` itself.

    Raises whatever `stash.scrape_scenes_by_query` raises, on the first
    variant that fails, without trying the rest. `scan.examine` treats a
    raising `search` as a transient failure to report, not as "the catalogue
    has nothing for this creator" — swallowing a mid-run failure here so a
    later variant could still answer would turn a real outage into a
    confident MUTE for whichever candidates the failing variant might have
    named, which is a worse outcome than reporting the error and letting a
    later run try again.
    """
    def search(name):
        seen = set()
        results = []
        for variant in search_variants(name, adapter.censorship):
            for row in stash.scrape_scenes_by_query(adapter.scraper_id, variant):
                key = _dedup_key(row)
                if key in seen:
                    continue
                seen.add(key)
                results.append(row)
        return results
    return search


def _dedup_key(row):
    """A hashable fingerprint of one candidate row's whole content.

    `json.dumps(..., sort_keys=True)` rather than `repr`: `sort_keys` is a
    documented guarantee that the same mapping produces the same string
    regardless of insertion order, which is exactly "the same content", not
    "the same object" — `repr` of a dict carries no such guarantee across
    Python versions or construction paths. No `default=`: a row this
    method cannot serialise is a shape `scrapeSingleScene` was not expected
    to return, and that is a wiring or schema mistake worth raising on, not
    one to paper over with a stringified fallback that might just as easily
    hide two rows silently colliding on the same fallback text.
    """
    return json.dumps(row, sort_keys=True)
