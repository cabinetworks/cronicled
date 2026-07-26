# Site adapters

Matching against a clip store is done through a *site adapter*, configured in
`config/adapters.json`, which is not tracked by this repo. See
`config/adapters.example.json` for the shape. No store is built in.

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
    "owner_segment": 2
  }
  ```

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

## Where adapters come from

`cronicled.adapters.registry.load_adapters` reads `adapters.json` out of the
config directory resolved by `cronicled.config.config_dir` — `$CRONICLED_CONFIG_DIR`
if it is set, otherwise a `config/` directory relative to the working directory.
See [Container](container.md) for how that is mounted in the image.

A fresh install has no config at all. `load_adapters` returns an empty mapping
rather than raising, so the app can start and say what needs configuring
instead of failing to import.
