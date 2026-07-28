# Site adapters

Matching against a clip store is done through a *site adapter*, configured in
`config/adapters.json`, which is not tracked by this repo. See
`config/adapters.example.json` for the shape — it configures one adapter per
mechanism below, so all three are visible there, not only the one this page
happens to show first. No store is built in.

Every adapter entry sets `owner_source`, which selects how the creator's name
is found for a given clip result. There are three mechanisms; an adapter uses
exactly one:

- **`url_segment`** — the creator's name is a path segment of the clip's URL.
  `owner_segment` gives the index of that segment, and that index counts from
  the **host**, not from the start of the path. For example, given
  `owner_segment: 2` and the URL
  `https://example.test/store/velvetcrane/copper-kettle`, the segments are
  `["example.test", "store", "velvetcrane", "copper-kettle"]`, so index 2 is
  `"velvetcrane"` — the third segment, not the second path component. This is
  the detail most likely to catch you out when writing a new adapter spec.

  ```json
  {
    "name": "examplestore",
    "owner_source": "url_segment",
    "owner_segment": 2,
    "owner_segment_example": {
      "url": "https://example.test/store/velvetcrane/copper-kettle",
      "owner": "velvetcrane"
    }
  }
  ```

  `owner_segment_example` is optional and checked once, at load time: it
  fails loudly if `owner_segment` does not resolve `"url"` to `"owner"`,
  which is exactly the off-by-one this section warns about — catching it
  when the adapter is configured rather than leaving it to be discovered
  from a library that quietly muted every file for a store it should have
  identified. An adapter with no example given behaves exactly as before.

- **`result_field`** — the creator's name is a field on the clip's search
  result, addressed by `owner_field`, a list of keys walked in order to reach
  a (possibly nested) value. HTML in the field is stripped automatically.

  ```json
  {
    "name": "examplestore",
    "owner_source": "result_field",
    "owner_field": ["studio", "name"]
  }
  ```

  For a result shaped like `{"studio": {"name": "Velvet Crane"}}`, this reads
  `"Velvet Crane"`.

- **`none`** — the store never exposes an owner directly; every candidate
  match must instead be confirmed by scraping the clip page. Set
  `catalog_resolvable: false` alongside it, since a name search cannot resolve
  a creator on a store shaped this way.

  ```json
  {
    "name": "examplestore",
    "owner_source": "none",
    "catalog_resolvable": false
  }
  ```

## `title_match_counts_as_ownership`

Every adapter entry must also set `title_match_counts_as_ownership` (a
boolean). It answers a question separate from `owner_source`: when a search
result's title or URL slug merely *names* the artist, but the store's own
attribution (via `owner_source`) does not match them, is that mention
trustworthy evidence the clip is theirs?

- `true` — a bare mention counts. This is the right answer for a store where
  a title naming a creator reliably means the clip is theirs (the "another
  store's search turning up a guest clip" case aside, which the owner-field
  check, not this one, is what tells apart).
- `false` — a bare mention proves nothing, and only the store's own
  attribution (`owner_source`) counts. This is the right answer for a store
  whose name search surfaces fan or collaboration clips sold by somebody
  else: a title containing a creator's name there is not evidence of
  ownership, and treating it as such attributes someone else's clip to them.

There is no default. A spec that omits the field fails to load — this is
deliberate: an adapter that cannot say whether its title matches are
trustworthy must not silently get the permissive reading, since that is
exactly how a wrong attribution was reaching production. A config written
before this field existed needs one line added declaring which case its
store is; guess the wrong one and the choice is between missing real
matches (`false` where `true` was true) or the original bug (`true` where
`false` was true) — reason it out per store rather than copying whichever
value is already in `config/adapters.example.json`.

This is independent of `catalog_resolvable`, which answers "can a name
search identify this store's creators at all" — a question about running a
search, not about what one result's title implies once a search has already
returned it. A store can be `catalog_resolvable: false` and still have
title matches worth trusting, or vice versa.

```json
{
  "name": "examplestore",
  "owner_source": "url_segment",
  "owner_segment": 2,
  "title_match_counts_as_ownership": true
}
```

## `search_omits_seed`

Optional, defaults to `false`. `search_query(seed, title_query)` normally
returns `seed + " " + title_query`; setting `search_omits_seed: true` makes
it return `title_query` alone, for a store where narrowing the query by the
creator seed costs recall and buys nothing. As of this writing nothing in
the production scan path calls `search_query` at all (see its docstring in
`cronicled/adapters/base.py`), so this only matters if a future caller
starts using it.

## Where adapters come from

`cronicled.adapters.registry.load_adapters` reads `adapters.json` out of the
config directory resolved by `cronicled.config.config_dir` — `$CRONICLED_CONFIG_DIR`
if it is set, otherwise a `config/` directory relative to the working directory.
See [Container](container.md) for how that is mounted in the image.

A fresh install has no config at all. `load_adapters` returns an empty mapping
rather than raising, so the app can start and say what needs configuring
instead of failing to import.
