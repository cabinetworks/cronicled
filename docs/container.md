# Running it, and the container

## The pinned runtime

The runtime version is pinned in `.python-version`, which the container build
and CI both read. That file is the only place it is declared.

## Running the tests

The project has zero runtime dependencies — standard library only — so a local
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

The same holds for the documentation site. `mkdocs-material` lives in a
`docs` dependency group, which nothing else installs:

```sh
uv run --group docs mkdocs build
```

Neither the test suite nor the image can see that group. Building the docs is
not a prerequisite for running anything.

## The image

Build the image, passing the declared version explicitly (this is exactly
what CI does):

```sh
docker build --build-arg PYTHON_VERSION="$(cat .python-version)" -t cronicled .
```

There is no service yet for the container to run (see the
[architecture](index.md) page), so its default command is a self-check: it
imports every module in the package and exercises a handful of pure functions
end to end, proving the pinned interpreter actually runs this project's code.
Running the image today builds and self-checks the runtime — it is not yet a
way to run the tool:

```sh
docker run --rm \
  -v /path/to/config:/config \
  -v /path/to/state:/var/lib/cronicled \
  cronicled
```

```
cronicled selfcheck ready (16 modules imported)
```

That count is the number of modules `pkgutil` finds under the package, so it
moves whenever a module is added or removed. It is quoted literally here rather
than elided, and a test asserts this page's number against what the self-check
actually prints — see `tests/test_docs.py`. A transcript that can go stale
silently is worse than no transcript; one that fails the build when it drifts
is worth keeping exact.

## What is mounted, and what is not

The image bakes in nothing specific to any one installation; everything that
varies between installs is mounted, not copied in:

- `/config` — server and adapter configuration (see
  [Site adapters](adapters.md)). The container sets
  `$CRONICLED_CONFIG_DIR=/config`, which both `server.json` and `adapters.json`
  are read from by default (see `cronicled/config.py`'s `config_dir`); a local
  checkout with no such directory set falls back to a `config/` directory
  relative to the working directory instead.

    This is deliberately a separate mechanism from `$STASH_URL`/`$STASH_API_KEY`:
    those two name the *media server* being managed, not this project, and an
    operator may already have them set for their own reasons — a decision, not
    an inconsistency.

- `/var/lib/cronicled` — the database

A read-only mount for the library itself will be documented here once the
metadata-enrichment path that needs it exists.
