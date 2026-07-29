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

**Running unattended:** `python -m cronicled` now builds a scheduler and starts
it. A library scan is registered at start-up with a cadence of its own and no
file limit, and the scheduler starts it when it is due — so proposals arrive
without anyone pressing anything. Nothing it starts writes to the media
server: a scan reads and proposes, and every proposal still waits for a person
to approve it.

**Not yet built:** wall-clock scheduling. A cadence is an interval measured
from the last recorded run, so a daily scan drifts to a different hour across
restarts. Times of day bring a machine that was off across the appointed hour,
and daylight-saving transitions where an hour repeats or does not exist, and
neither is decided yet.

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
reversal. The unattended half is real too: the entry point registers a scan
with a declared cadence, builds a scheduler over it and starts it, and the
page reports what the loop last did — when it ticked, what was due, and why
anything due was left alone.

The distinction worth keeping in mind: the cadence is an INTERVAL, not a time
of day, so "nightly" means "a day since the last run" and drifts across
restarts. That remaining gap has its own diagram, kept separate from the ones
describing what is built, on the
[architecture](docs/index.md#what-is-not-decided-yet) page.

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

That command also registers three unattended passes, each with a cadence of
its own, and starts the loop that runs them when they are due: a library scan
with no file limit, a pass over performer descriptions that proposes cleaned
text for any carrying markup, and a tag pass that looks for one tag written
under more than one spelling and proposes a merge. To change how often any of
them runs, or to turn one off, put a `schedule.json` in the config directory —
see `config/schedule.example.json` for the shape. A cadence that
is not a positive number of seconds, a key that is not `every`/`enabled`, or
a producer name that is not registered are all refused at start-up rather
than leaving a producer running on a cadence nobody chose. Without a media
server nothing is scheduled at all and the entry point says so; without a
configured site adapter there is nothing to scan, so the scan alone is left
out — the description and tag passes read the media server's own text and
vocabulary and need no store to search.

A proposed merge is judged in its own section of the inbox, not among the
scene proposals, because it is a different weight of decision: it names every
spelling and how many scenes each carries, and it says plainly that approving
it cannot be undone. A merge moves every item off the losing spellings and
deletes them; nothing records which items came from which tag, so there is no
snapshot to restore and no Undo is offered. Where three spellings share one
form, or two carry no evidence about which was meant, the cluster is reported
without a survivor and no Merge button at all — which one wins is a person's
call, not something to settle by picking the most popular.

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
- [Contributing](CONTRIBUTING.md) — the standing rule for trusting a new test

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
