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

The default command starts the inbox — the same one `python -m cronicled`
serves outside a container (see the [architecture](index.md) page). **Run it
with the port published to loopback only:**

```sh
docker run --rm \
  -p 127.0.0.1:8571:8571 \
  -v /path/to/config:/config \
  -v /path/to/state:/var/lib/cronicled \
  -e CRONICLED_SERVER=http://your-stash-host:9999 \
  -e CRONICLED_API_KEY="$STASH_API_KEY" \
  cronicled
```

This is THE documented way to run it. `-e CRONICLED_SERVER`/`-e
CRONICLED_API_KEY` are optional in two different ways. A mounted `/config`
holding a `server.json` with a `url` and an `api_key` configures the media
server on its own — that file is asked last, after the flags and after these
two variables, so passing either here still wins and adding the file changes
nothing for a deployment that already does. With none of the three, the inbox
still starts and a person can browse, dismiss and mute what a scan already
produced, but Approve and Undo refuse until a media server is configured (see
`cronicled/__main__.py`). A `server.json` that is present and incomplete is a
third state: it stops the start with a message naming what is missing, rather
than being reported as an absent one.

`-e CRONICLED_ZONE=Europe/Lisbon` is worth adding in a container, and this is
the deployment it exists for. It names the zone the overnight passes run in AND
the zone the page shows every timestamp in — one setting, so the two cannot
disagree. Unset it is UTC, which inside a container is also the host zone, so
the appointments and the page would read hours from the ones the person
configuring them has in mind. The zone in use is printed on every start, and a
name this system does not know stops the container rather than being guessed
at. Nothing in the database moves: stored timestamps are UTC either way.

Everything above is also settable as a trailing flag instead of an `-e`
environment variable — useful for a companion tool that already builds an
argument list and would rather not also assemble one of environment pairs.
The flag and the environment variable of the same value are equivalent; the
flag wins if both are given:

```sh
docker run --rm \
  -p 127.0.0.1:8571:8571 \
  -v /path/to/config:/config \
  -v /path/to/state:/var/lib/cronicled \
  cronicled \
  --server http://your-stash-host:9999 --api-key "$STASH_API_KEY" \
  --config-dir /config --db /var/lib/cronicled/cronicled.sqlite3
```

`--config-dir` and `--db` are shown here pointed at the same paths the image's
own `ENV` defaults already resolve to (see "What is mounted, and what is not"
below) — passing them explicitly only matters when they need to differ from
those defaults, such as a second instance sharing one `/config` mount but
keeping its own database.

The inbox has no authentication of its own, so the `-p` form above is the
only thing standing between its buttons and anyone who can reach this host.
Inside the container it binds `0.0.0.0`, not the host-side default of
`127.0.0.1` — a container's own loopback answers nothing that `docker run -p`
forwards to it, so the bind host cannot be the protection here the way it is
outside a container. `-p 127.0.0.1:8571:8571` keeps that reachable only from
this machine, exactly as `127.0.0.1:8571` does when running `python -m
cronicled` directly. Writing `-p 8571:8571` (or `-P`) instead publishes this
same unauthenticated page — and the write access its buttons have to the
media library — to every interface this host has, which usually means every
machine on the same network. `serve()` prints a loud warning every time it
binds off its host-side default for exactly this reason; inside a container
that is every start, not just a mistake, and the warning says so.

**This still does not scan anything, and nothing populates the store on its
own.** The inbox only shows proposals a scan run elsewhere already wrote —
starting the container is not yet a way to get new proposals, only to review
ones that already exist.

The self-check that used to be this image's only default command is still
reachable — it imports every module in the package and exercises a handful of
pure functions end to end, proving the pinned interpreter actually runs this
project's code. Naming it as a trailing argument no longer works now that the
image has an `ENTRYPOINT` (`python -m cronicled`): trailing arguments APPEND
to that entry point instead of replacing it, so `docker run cronicled python
-m cronicled.selfcheck` would try to run `python -m cronicled` with those four
words as nonsense arguments, not the self-check. `--entrypoint` overrides the
entry point itself, which is what still reaches it:

```sh
docker run --rm --entrypoint python cronicled -m cronicled.selfcheck
```

```
cronicled selfcheck ready (36 modules imported)
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
  relative to the working directory instead. The service reads `server.json`
  out of it only when nothing else named a media server — see the run command
  above for the order.

    This is deliberately a separate mechanism from `$STASH_URL`/`$STASH_API_KEY`:
    those two name the *media server* being managed, not this project, and an
    operator may already have them set for their own reasons — a decision, not
    an inconsistency.

- `/var/lib/cronicled` — the database. The container sets
  `$CRONICLED_DB=/var/lib/cronicled/cronicled.sqlite3`, so `--db`'s own
  default (a relative path, which would otherwise land in the image's
  writable layer and vanish with the container) resolves inside this volume
  instead.

A read-only mount for the library itself will be documented here once the
metadata-enrichment path that needs it exists.

## The published image

Every push to the default branch publishes a multi-architecture image
(`linux/amd64` and `linux/arm64`) to `ghcr.io/cabinetworks/cronicled`, tagged
with the commit SHA. A release tag `vX.Y.Z` additionally publishes the version
declared in `pyproject.toml`, and fails the build if the two disagree rather
than labelling an image with a version it was not built from.

**`latest` follows the default branch.** It was withheld while this image only
imported a package, printed a line and exited, because nobody pulls `latest`
expecting that. The image serves the inbox now, so `latest` names a runnable
thing and is published.

Read it for what it is rather than what the name suggests: **the newest build
of the default branch, not a reviewed release.** It moves under anyone who
pulls it, and what it starts is a page with no authentication whose buttons
write to a media library. Pin instead for anything that should not change
without being asked — both of these are still published, and nothing was
removed to add `latest`:

```sh
docker pull ghcr.io/cabinetworks/cronicled:latest        # moves
docker pull ghcr.io/cabinetworks/cronicled:<commit-sha>  # does not
docker pull ghcr.io/cabinetworks/cronicled:<version>     # does not
```

A release tag publishes its version and its commit SHA and deliberately does
**not** move `latest`. Releases are not necessarily cut in order, and tagging
an older still-supported version would otherwise drag `latest` backwards onto
it, silently downgrading anyone who pulls it. That, rather than the tag's
ordinary imprecision, is the one way it could genuinely mislead, and a test
pins it.

Publishing runs only after the leak guard has passed, and never from a pull
request. Everything that reaches the image is tracked source the guard has
already scanned: the Dockerfile copies `cronicled/` and nothing else, and
`.dockerignore` excludes the rest. A new `COPY` line has to be checked against
`.dockerignore` before it ships.
