# cronicled

An always-on companion for a [Stash](https://github.com/stashapp/stash) media
library. It watches the library on a schedule and files what it finds into an
inbox of proposed changes — metadata matches, tag merges, performer fixes — that
you confirm or dismiss. Nothing is written to your library without your say-so.

Zero runtime dependencies: Python standard library only.

## Status

Early. The foundation layer is in place; the service itself is being built.

## Running it

The runtime version is pinned in [`.python-version`](.python-version), which the
container build and CI both read. That file is the only place it is declared.

The service has zero runtime dependencies — standard library only — so a local
checkout runs with nothing installed beyond a matching Python:

```sh
python3 -m unittest discover -s tests -t . -v
```

Development and CI drive the suite through [uv](https://docs.astral.sh/uv/)
instead, which reads the pinned version out of `.python-version` automatically
and supplies that exact interpreter regardless of what else is on `PATH`:

```sh
uv run python -m unittest discover -s tests -t . -v
```

uv is a development and CI convenience only — it is not a project dependency.
`pyproject.toml` declares no runtime dependencies, and the container image
ships without uv installed in it: the command above still works for anyone
without uv, on any interpreter meeting the `requires-python` constraint,
because the project must never require a tool to run its own tests.

### Container

Build the image, passing the declared version explicitly (this is exactly
what CI does):

```sh
docker build --build-arg PYTHON_VERSION="$(cat .python-version)" -t cronicled .
```

Run it:

```sh
docker run --rm \
  -v /path/to/config:/config \
  -v /path/to/state:/var/lib/cronicled \
  cronicled
```

The image bakes in nothing specific to any one installation; everything that
varies between installs is mounted, not copied in:

- `/config` — server and adapter configuration (see "Site adapters" below)
- `/var/lib/cronicled` — the database

A read-only mount for the library itself will be documented here once the
metadata-enrichment path that needs it exists.

## Site adapters

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

## Leak guard

`scripts/check_leaks` fails the build if a forbidden string (configured out
of band — see the script header — never committed to this repo) shows up in
tracked file contents or filenames, in untracked-but-not-ignored files, in
tracked file contents anywhere in history, or in a commit message anywhere in
history. CI runs it (`./scripts/check_leaks`) on every push and pull request.

To also block a bad commit message locally, *before* it is ever written,
enable the accompanying `commit-msg` hook. This repo's `core.hooksPath` is
owned by a different, unrelated mechanism, so the hook is not installed via
that — copy or symlink it into `.git/hooks/commit-msg` instead:

```sh
cp scripts/hooks/commit-msg .git/hooks/commit-msg
chmod +x .git/hooks/commit-msg
```

or, to track upstream changes to the hook automatically:

```sh
ln -sf ../../scripts/hooks/commit-msg .git/hooks/commit-msg
```

## License

MIT
