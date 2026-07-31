"""Text-substitution mapping: canonical words and their platform-substituted forms.

A word-substitution map typically has the shape ``{canonical: [substituted_form, ...]}``
and supports two core operations:

- `search_variants(query)` — expands a query containing canonical words into
  variants that exist in the substitution map, enabling searches to hit indexed
  alternate spellings. Original query is returned first; results are de-duplicated
  and bounded to 6 entries to prevent query explosion.
- `decensor(title)` — rewrites substituted forms back to their canonical words so
  titles can be compared consistently regardless of platform censoring. Ambiguous
  substitutions — those claimed by multiple canonicals — are left alone, since
  rewriting them would be a guess.

`search_variants` also makes ONE substitution unconditionally, never read from
a per-store map: a stand-alone `&` also tries the spelled-out `and`, and a
stand-alone `and` also tries `&`. This is deliberately not folded into the
outbound query itself (see `cronicled.adapters.base.SiteAdapter.search_query`)
because a store may index either spelling and nothing here has measured
which — sending one committed form outbound would be exactly the guess this
avoids, so both are offered as variants instead, within the same bound as
every other substitution below.
"""
import re

from cronicled.text import normalize


def _replace_phrase(text, phrase, repl):
    """Whole-token phrase replace on space-separated normalized text: a short
    word is never matched inside a longer one, and multi-word phrases work."""
    return (" " + text + " ").replace(" " + phrase + " ", " " + repl + " ").strip()


# Stand-alone tokens only: a `&` or `and` glued to a neighbouring character
# (`Rock&Roll`, `sand`) is not a substitutable word, the same discipline
# `_replace_phrase`'s space-padding applies to a censored phrase. `and` is
# matched case-insensitively, because the query this runs over is not always
# the case-folded form `SiteAdapter.search_query` now returns — a creator
# name reaches here with whatever case it was resolved in.
_AMPERSAND_RE = re.compile(r"(?<!\S)&(?!\S)")
_AND_WORD_RE = re.compile(r"(?<!\S)and(?!\S)", re.IGNORECASE)


def _ampersand_variants(query):
    """`query` with `&` swapped for `and`, and/or with `and` swapped for `&`,
    for whichever of the two `query` actually carries — see this module's
    docstring for why both directions are tried rather than one chosen.
    Neither swap fires when its token is absent, and both may fire on a
    query that happens to carry both. Returns a list, `&`-to-`and` first."""
    variants = []
    if _AMPERSAND_RE.search(query):
        variants.append(_AMPERSAND_RE.sub("and", query))
    if _AND_WORD_RE.search(query):
        variants.append(_AND_WORD_RE.sub("&", query))
    return variants


def _forward_map(subs):
    """{normalized_canonical: [normalized_variant, ...]}."""
    out = {}
    for canon, forms in subs.items():
        c = normalize(canon)
        variants = [normalize(f) for f in forms if normalize(f)]
        if c and variants:
            out[c] = variants
    return out


def _reverse_map(subs):
    """{normalized_variant: canonical}, dropping any variant claimed by more than
    one canonical — an ambiguous spelling cannot be reversed without guessing."""
    claims = {}
    for canon, forms in subs.items():
        c = normalize(canon)
        for f in forms:
            v = normalize(f)
            if c and v:
                claims.setdefault(v, set()).add(c)
    return {v: next(iter(cs)) for v, cs in claims.items() if len(cs) == 1}


def search_variants(query, subs):
    """`query` plus:

    - the ampersand swap (see `_ampersand_variants`), unconditionally, never
      read from `subs`;
    - censored-form spellings of any canonical word `subs` names.

    So a search built from an uncensored filename also hits the platform's
    censored index, and a query spelled with `&` or `and` also hits a store
    indexing the other. De-duplicated, original first, the ampersand swap(s)
    next, censorship expansions last; bounded to 6 entries TOTAL to avoid a
    query explosion — the ampersand swap counts against the same cap as
    everything else, it does not sit outside it."""
    out = [query]

    def _add(candidate_query):
        if candidate_query and candidate_query not in out:
            out.append(candidate_query)
            return len(out) >= 6
        return False

    for v in _ampersand_variants(query):
        if _add(v):
            return out
    if not subs:
        return out
    nq = normalize(query)
    for canon, variants in _forward_map(subs).items():
        if (" " + canon + " ") in (" " + nq + " "):
            for v in variants:
                q = _replace_phrase(nq, canon, v)
                if _add(q):
                    return out
    return out


def decensor(title, subs):
    """Rewrite censored store-title phrases back to their canonical word so a
    censored store title string-matches an uncensored local filename. Returns
    normalized text; longer phrases first; ambiguous spellings are left alone.
    A no-op (just normalize) when `subs` is empty."""
    words = normalize(title)
    if not subs:
        return words
    rev = _reverse_map(subs)
    for variant in sorted(rev, key=lambda p: -len(p)):
        words = _replace_phrase(words, variant, rev[variant])
    return words
