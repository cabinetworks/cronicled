# Running it, and the container

## The pinned runtime

The runtime version is pinned in `.python-version`, which the container build
and CI both read. That file is the only place it is declared.

## Running the tests

The project takes one runtime dependency, Jinja2, for autoescaped rendering.
Everything else is the standard library, and a local checkout installs that
one dependency before running the suite:

```sh
pip install -e .
python3 -m unittest discover -s tests -t . -v
```

Development and CI drive the suite through [uv](https://docs.astral.sh/uv/)
instead, which reads the pinned version out of `.python-version` automatically,
supplies that exact interpreter regardless of what else is on `PATH`, and
installs the one declared dependency itself:

```sh
uv run python -m unittest discover -s tests -t . -v
```

uv is a development and CI convenience only — it is not a project dependency.
`pyproject.toml` declares exactly one runtime dependency, Jinja2, and the
container image installs that dependency itself rather than shipping uv: the
commands above still work for anyone without uv, on any interpreter meeting
the `requires-python` constraint, because the project must never require a
tool other than its one declared dependency to run its own tests.

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
cronicled selfcheck ready (22 modules imported)
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

## The published image

Every push to the default branch publishes a multi-architecture image
(`linux/amd64` and `linux/arm64`) to `ghcr.io/cabinetworks/cronicled`, tagged
with the commit SHA. A release tag `vX.Y.Z` additionally publishes the version
declared in `pyproject.toml`, and fails the build if the two disagree rather
than labelling an image with a version it was not built from.

**There is no `latest` tag, and its absence is deliberate — please do not add
one.** `latest` is read as "the one you want", and nobody reaches for it
expecting a program that prints a line and exits. A commit SHA or a released
version makes no such promise. The reasoning is repeated in a comment in
`.github/workflows/ci.yml` and pinned by a test, because an omission is
otherwise indistinguishable from an oversight.

```sh
docker pull ghcr.io/cabinetworks/cronicled:<commit-sha>
```

Publishing runs only after the leak guard has passed, and never from a pull
request. Everything that reaches the image is tracked source the guard has
already scanned: the Dockerfile copies `cronicled/` and nothing else, and
`.dockerignore` excludes the rest. A new `COPY` line has to be checked against
`.dockerignore` before it ships.
