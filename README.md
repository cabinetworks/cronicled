# cronicled

A foundation for an always-on companion to a
[Stash](https://github.com/stashapp/stash) media library. What is here today:
string and filename normalization, date extraction, a client for the media
server's own API, a pluggable site-adapter interface for matching against a
clip store, candidate scoring, creator attribution, a durable store for
proposed changes, a background job runner, a library scan that turns unmatched
files into proposals, a scheduler that decides when each producer is due, and a
leak guard for the repo itself.

**Not yet built:** there is no inbox of proposed changes, nothing that applies
one, and — the part that matters most — **no entry point that starts any of
this.** The scheduler knows what is due and can run it; nothing constructs a
scheduler. The pieces above are library code, called directly (including by
tests); nothing here watches a library or writes to it on its own.

Zero runtime dependencies: Python standard library only.

## Status

Early. The foundation layer described above is what exists, and it now reaches
as far as deciding when work is due — but the always-on *service* does not
exist: nothing runs continuously, no inbox shows what was proposed, and no
write happens without a person calling the client directly.

The distinction worth keeping in mind: a scheduler that is never started is a
component, not a service. What is missing is the process that would construct
one, the interface that would show its output, and the approval gate between a
proposal and a write. Those have their own diagram, kept separate from the ones
describing what is built, on the
[architecture](docs/index.md#the-service-that-does-not-exist-yet) page.

## Quickstart

The runtime version is pinned in [`.python-version`](.python-version). There
are no runtime dependencies, so a checkout runs its own tests with nothing
installed beyond a matching Python:

```sh
python3 -m unittest discover -s tests -t . -v
```

There is no entry point to run yet. The container's default command is a
self-check that proves the pinned interpreter can import and run the package;
see [Running it, and the container](docs/container.md).

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

    selfcheck["selfcheck<br/>imports every module in the package;<br/>the container's default command"]

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

**There is no `latest` tag, and its absence is deliberate.** The image is a
pinned runtime for a library with no entry point yet — its default command
prints one line and exits — and `latest` is read as "the one you want".
[The container page](docs/container.md) has the tagging scheme and the mounts.

## License

MIT
