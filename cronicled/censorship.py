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
"""
from cronicled.text import normalize


def _replace_phrase(text, phrase, repl):
    """Whole-token phrase replace on space-separated normalized text: a short
    word is never matched inside a longer one, and multi-word phrases work."""
    return (" " + text + " ").replace(" " + phrase + " ", " " + repl + " ").strip()


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
    """`query` plus censored-form spellings of any canonical word it contains, so a
    search built from an uncensored filename also hits the platform's censored
    index. De-duplicated, original first; bounded to avoid a query explosion."""
    out = [query]
    if not subs:
        return out
    nq = normalize(query)
    for canon, variants in _forward_map(subs).items():
        if (" " + canon + " ") in (" " + nq + " "):
            for v in variants:
                q = _replace_phrase(nq, canon, v)
                if q and q not in out:
                    out.append(q)
                    if len(out) >= 6:
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
