# cronicled

A foundation for an always-on companion to a
[Stash](https://github.com/stashapp/stash) media library. What is here today:
string and filename normalization, date extraction, a client for the media
server's own API, a pluggable site-adapter interface for matching against a
clip store, candidate scoring, creator attribution, a durable store for
proposed changes, a background job runner, a library scan that turns unmatched
files into proposals, a scheduler that decides when each producer is due, an
inbox that shows a proposal and takes approve/dismiss/mute/undo, an entry point
that serves it, and a leak guard for the repo itself.

**Not yet built:** nothing runs this unattended. `python -m cronicled` serves
the inbox and answers a person's clicks; it constructs no scheduler and starts
no timer. The scheduler knows what is due and can run it, but nothing calls it
on its own — a scan still has to be started by something outside this package.
Until that exists, "always-on" describes the design, not what is running.

One runtime dependency: Jinja2, for autoescaped HTML. Everything else is the
Python standard library. The inbox renders text the project did not write —
filenames from disk, titles from a scraper — and autoescaping makes escaping a
property of the renderer rather than something a person must remember at every
interpolation.

## Status

The inbox is real: `python -m cronicled` serves proposals from the store,
takes approve/dismiss/mute/undo over POST, and applies or reverts a scene
through the media-server client — no write happens without a person clicking
one of those. Undo is not always complete: a proposal that carries a cover
image writes one the media server never exposes in a form its own undo
snapshot can restore, so that one field stays as the approve left it. The
inbox warns before an approve that would do this, and undo reports the same
residual afterward, rather than either one reading as a clean, full
reversal. What is still missing is the part that would make any of this
unattended: nothing constructs a scheduler, so no proposal is ever produced
without a person starting a scan themselves.

The distinction worth keeping in mind: a scheduler that is never started is a
component, not a service. What is missing is the process that would construct
one and start it on a schedule. That gap has its own diagram, kept separate
from the ones describing what is built, on the
[architecture](docs/index.md#the-service-that-does-not-exist-yet) page.

## Quickstart

The runtime version is pinned in [`.python-version`](.python-version). The
project takes one runtime dependency (Jinja2 — see above), so a checkout
installs it before running the suite:

```sh
pip install -e .
python3 -m unittest discover -s tests -t . -v
```

`tests/test_web_render.py` imports Jinja2 through `cronicled/web/render.py`,
the module autoescaping was taken as a dependency to configure.

Serve the inbox against a store:

```sh
python -m cronicled --db /path/to/cronicled.sqlite3 \
    --server http://your-stash-host:9999 --api-key "$STASH_API_KEY"
```

Every flag above (plus `--config-dir`, `--host` and `--port`) also reads an
environment variable of the matching name (`$CRONICLED_DB`,
`$CRONICLED_SERVER`, `$CRONICLED_API_KEY`, `$CRONICLED_CONFIG_DIR`,
`$CRONICLED_HOST`, `$CRONICLED_PORT`) when the flag itself is not given — the
form the container image relies on, since a `docker run` argument list and an
`-e` list are both just ways of setting the same thing. The flag wins if both
are set.

It binds to loopback only (`127.0.0.1:8571` by default) — there is no
authentication, so the binding is the only thing standing between the page's
buttons and anyone who can reach the host. The container's default command
now starts this same inbox, bound to every interface *inside* the container
instead — loopback there would be unreachable from `docker run -p` — which
moves the actual protection to how that port is published; see
[Running it, and the container](docs/container.md) for the form that keeps
it local and what the other form exposes.

## The module map

Every module under `cronicled/`, and which of them import which. An arrow
points from a module to the module it imports, so it reads "depends on".

```mermaid
flowchart TD
    subgraph pure["Pure string helpers, no I/O"]
        vocab["vocab<br/>stopwords, junk tokens, video extensions"]
        text["text<br/>normalize, tokens, strip_ext, strip_html"]
        dates["dates<br/>date extraction, date-shaped guards"]
        censorship["censorship<br/>search_variants, decensor"]
    end

    subgraph matching["Matching logic"]
        scoring["scoring<br/>score, decide"]
        artist["artist<br/>creator_folder, resolve"]
    end

    subgraph adapters["Site adapters, configured and never compiled in"]
        base["adapters.base<br/>the SiteAdapter interface"]
        declarative["adapters.declarative<br/>an adapter built from a config dict"]
        registry["adapters.registry<br/>load_adapters"]
    end

    subgraph configuration["Configuration, read from the operator's files"]
        config["config<br/>server connection, config_dir"]
    end

    stash["stash<br/>the media server's GraphQL API"]

    subgraph recording["Recording what was found"]
        store["store<br/>proposals, dismissals, mutes"]
        jobs["jobs<br/>JobRunner, cost classes"]
        schedule["schedule<br/>cadence, due-ness, the tick"]
    end

    selfcheck["selfcheck<br/>imports every module in the package;<br/>still runnable explicitly in the container"]

    text --> vocab
    dates --> text
    censorship --> text
    scoring --> text
    artist --> text
    artist --> dates
    declarative --> base
    declarative --> text
    registry --> declarative
    registry --> config
    schedule --> jobs
    schedule --> store
    stash --> text
    jobs -. "holds a Store it is given" .-> store
```

The matching path, the job lifecycle, and the planned service each have their
own diagram on the [architecture](docs/index.md) page.

## Documentation

The reference material lives on the site at <https://cabinetworks.github.io/cronicled/>, and
in [`docs/`](docs/) in this repository. This README is the overview; each fact
below is documented in exactly one of those pages, not in both.

- [Architecture](docs/index.md) — the four diagrams and the reasoning around
  them
- [Site adapters](docs/adapters.md) — the `owner_source` reference
- [Running it, and the container](docs/container.md) — the pinned runtime, the
  test invocations, the image and its mounts
- [Leak guard](docs/leak-guard.md) — what `scripts/check_leaks` scans, and the
  local `commit-msg` hook

## The published image

Every push to the default branch publishes a multi-architecture image to
`ghcr.io/cabinetworks/cronicled`, tagged with the commit SHA:

```sh
docker pull ghcr.io/cabinetworks/cronicled:<commit-sha>
```

**`latest` follows the default branch and is published.** It names the newest
build of that branch, not a reviewed release, and it moves under anyone who
pulls it — while what it starts is an unauthenticated page whose buttons write
to a library. A commit SHA or a released version makes no such promise, and
both are still published; a release tag deliberately does not move `latest`.
[The container page](docs/container.md) has the tagging scheme and the
mounts.

## License

MIT
