# Architecture

This page describes what the package is made of and how the pieces fit
together. It is deliberately explicit about a distinction the diagrams below
would otherwise blur: **everything in the first three diagrams exists in the
repository today; the fourth mixes what exists with the one thing that
still does not.**

There is now an inbox of proposed changes, an entry point that serves it, and
an approval gate: `python -m cronicled` serves what the store holds, and
nothing writes to the media server except through an explicit approve or
undo. What does not exist is anything that runs unattended.

A scheduler is part of the library code: it resolves each producer's cadence,
decides what is due, runs it and records the run. What does not exist is a
process that constructs one and calls it on a timer. A scheduler nobody starts
is a component, not a service, and the fourth diagram is drawn around that one
remaining gap.

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

A few things the shape of that graph is saying:

- **The helpers know nothing about the layers above them.** `text` depends only
  on `vocab`; `vocab` depends on nothing. That is what lets the matching rules
  be tested against strings rather than against a library.
- **`store` and `jobs` import nothing from the rest of the package.** The runner
  takes a store at construction and takes producers by registration, so neither
  of them knows what a filename or a candidate title is. A producer is anything
  with `name`, `cost` and a `produce(ctx)` generator.
- **Nothing imports `stash`.** The client is a leaf: matching logic never
  reaches for the media server on its own.
- **`censorship` is called from two places, for two different reasons.**
  `cronicled.search.catalog_search` expands the QUERY with `search_variants`
  before any lookup happens, using the adapter's own substitution map, so a
  censored spelling never has to reach `scan.examine` for that half.
  `scan.examine` calls `decensor` on each candidate's TITLE, but only to
  decide which one SCORES best — the candidate a proposal carries is always
  the one `search` returned, untouched, so a decensored title can win but
  can never itself become the applied title. See `scan.examine`'s docstring.
- **No module is compiled against a particular store.** `adapters.registry`
  reads adapters out of the operator's config; see
  [Site adapters](adapters.md).
- **`config` has exactly one in-package importer**, and it is that registry.
  Its other half — the media server's URL and API key — is read by whoever
  constructs a `Stash`, deliberately, so the client can be built against a fake
  transport in a test with no config file existing at all.

## The matching path

How a filename becomes either a match or a refusal. Each box names the module
that does that step.

This is the path for a file that has to be *searched for*. A file can also be
*identified*: a scan offers the whole batch to each configured stash-box first,
and a box that recognises a file by its own fingerprints has answered the
question outright - no text search, no scoring, no threshold, so none of what
follows applies to it. That is identity rather than similarity, and it is
recorded as such: the proposal carries the box that identified it instead of a
score, because nothing scored it. Two boxes recognising one file as *different*
scenes is reported as a refusal naming both, never settled by whichever box was
configured first. A file no box recognises - most files, most boxes - takes the
path below exactly as it always did.

!!! warning "No module in this package runs this sequence"

    There is no function you can call that goes from one end of this diagram to
    the other. The arrows are the order in which each step's inputs become
    available, not a call graph of something already written. The dashed box is
    not part of this package at all: the media server runs the search, and this
    project only says how to phrase it and how to read the results back.

```mermaid
flowchart TD
    file["a filename, and the folder it sits in"]
    norm["text<br/>normalize, strip_ext, tokens: fold case and accents, drop junk"]
    date["dates<br/>lift a date prefix out of the name"]
    who["artist<br/>creator_folder walks up past Clips, Downloads, Misc;<br/>resolve attributes the file to a creator"]
    query["adapters + censorship<br/>search_query phrases it, search_variants expands it"]
    ext["the search itself, run by the media server's scraper"]
    read["adapter.owner_of / artist_from_url<br/>read a creator out of each result"]
    score["scoring.score, once per candidate<br/>a Match: value, contained, meaningful_count"]
    decide{"scoring.decide"}
    apply["a Match to apply, and the Resolution naming its creator"]
    refuse["a refusal, carrying a reason a person can act on"]

    file --> norm --> date --> who --> query --> ext --> read --> score --> decide
    decide -- "exactly one candidate clears the bar" --> apply
    decide -- "none clears it, or two are within 0.05 of each other" --> refuse

    classDef outside stroke-dasharray:6 4
    class ext outside
```

The two ends of that diagram are not symmetrical, and deliberately so.
Refusing costs somebody a review; a wrong automatic write costs a corrupted
file that nobody notices. So `decide` refuses rather than guesses:

- a score below the threshold refuses, and the reason names the candidate that
  came *closest to being eligible* rather than the one with the highest raw
  value — only the near miss tells a reader something they can act on;
- a match resting on a single generic word needs 0.9 or better before it counts
  as evidence at all;
- two eligible candidates within `AMBIGUITY_MARGIN` (0.05) of each other are a
  dilemma, not a decision, and both are refused rather than one being picked by
  list order.

`artist.resolve` follows the same principle from the other direction. When the
folder names one creator and the filename names another, the folder wins —
somebody chose to file the video there — but the filename's answer comes back
in `competing`, and folder text that no guard would accept as a name comes back
in `rejected_folder`. Neither is dropped. A library where those turn up often
is one whose filing convention is not what the operator assumed, and that is
the most useful signal available.

## Job lifecycle

`jobs.JobRunner` runs a producer on a background thread so a long scan cannot
block whatever started it. A job has exactly three states, and `Job.state` is
always one of them.

```mermaid
stateDiagram-v2
    [*] --> running: start() reserves a slot in the cost class and spawns the worker
    running --> done: the producer's generator is exhausted
    running --> failed: the producer raised
    done --> [*]
    failed --> [*]

    note left of running
        A cost class caps how many jobs of that kind
        run at once: scraping and box allow one each
        because both drive the media server's headless
        browser; local is unlimited.
        A saturated class is refused at start() with
        JobRejected, naming the job holding the slot.
        No job record is created in that case, so a
        refusal is not a state on this diagram.
    end note

    note right of running
        The slot is released in a finally, so it is
        freed on either exit. A crashed scrape that
        kept its slot would block every later scrape
        with nothing in the logs to say why.
    end note

    note right of failed
        The worker swallows whatever the producer
        raised, so the job's error and traceback are
        the only record of it that will ever exist:
        nothing re-raises and nothing else logs. The
        error names the exception type as well as its
        message, because str(exc) alone is empty for
        a bare raise.
    end note
```

Two details worth knowing before writing a producer:

- **`produce` must be a generator**, and `start()` refuses anything else with a
  `TypeError`. The runner records each proposal as it is yielded, so a scan that
  dies partway through a long library keeps what it already found; a `produce`
  that built a list and returned it would lose all of them on the same failure.
- **A producer never touches the store.** It gets a `ctx` with one method,
  `log(message)`. Persistence, and the dismissal and mute rules that make a
  reviewer's past decisions stick, belong to the runner alone — a producer
  writing to the store directly could bypass them.

The runner also forgets. It holds every running job and the most recent
finished ones — two hundred by default, a constructor argument — because a
process that stays up for weeks would otherwise keep a record of every job it
has ever run, and `jobs()` would grow with it. Running jobs are never dropped:
they are already bounded by the cost-class limits, and dropping one would erase
the only record of work still in flight. What is dropped is admitted rather
than hidden — `jobs()` carries the number of finished jobs evicted alongside
the snapshots, so a truncated history cannot be read as the whole of what ever
ran, and asking for an evicted job raises `JobForgotten` rather than the plain
`KeyError` of an id that never existed. "That ran and I no longer remember it"
and "that never happened" send a caller to different places.

And it can be shut down. `close(timeout=None)` stops the runner accepting work
and waits for the jobs already running, returning `True` if they all finished
and `False` if the timeout expired with work still in flight — a deploy asking
"is it safe to kill this process" gets two answers there and has to act
differently on each. After `close()`, `start()` raises `RunnerClosed`, and that
refusal is the point: without it a shutdown races a new job, and the wait means
nothing because a third job begins behind the two being waited for. It is not
cancellation — nothing interrupts a producer, and a job still running when the
timeout expires keeps running on its daemon thread, which the process exit will
kill wherever it has got to.

## What is not decided yet

Only one node below is **planned**. Everything else in this diagram now has
code behind it — it is drawn separately from the three above only because it
carries the one node that does not exist, and mixing a planned node into a
diagram of what is built is how a picture starts claiming more than the prose
does.

```mermaid
flowchart TD
    entry["BUILT: `python -m cronicled`<br/>registers the scheduled scan, then builds and starts the scheduler"]
    sched["BUILT: Scheduler decides what is due,<br/>runs it, and records the run"]
    wallclock{"PLANNED: a cadence written as a time of day"}
    built["BUILT: JobRunner drives the producer,<br/>Store records each proposal as it is yielded"]
    inbox["BUILT: the inbox — a person sees what was proposed"]
    gate{"BUILT: approval<br/>no write happens without one"}
    apply["BUILT: Actions.approve calls Stash.apply_scene;<br/>Actions.undo calls Stash.revert_scene"]

    entry -- "starts the loop, and closes it on shutdown" --> sched
    wallclock -. "would replace the interval with an appointed hour" .-> sched
    sched --> built
    built -- "the store now holds a proposal" --> inbox
    entry --> inbox
    inbox --> gate
    gate -- "approved" --> apply
    gate -- "dismissed" --> inbox

    classDef planned stroke-dasharray:6 4
    classDef built stroke-width:3px
    class wallclock planned
    class entry,sched,built,inbox,gate,apply built
```

One node in that diagram is dashed, and it is the only one that does not
exist: a cadence written as a time of day. Everything else — the entry point,
the scheduler it now starts, the inbox, the approval gate, and the caller that
applies or reverts a scene — is built and tested.

What replaced the dashed arrow *into* the scheduler is worth reading
carefully, because the ordering it depends on fails silently. A scheduler
resolves its schedule once, in its constructor, from whatever producers the
runner holds at that instant, so the entry point registers the scheduled scan
BEFORE it builds the scheduler. Built the other way round it resolves an empty
registry: it schedules nothing, raises nothing, and ticks on time forever
without ever starting anything.

The scan it starts is a registration of its own, with no file limit — the
whole unorganized set each run. The scan a person presses the button for keeps
the name it always had and the limit they typed, and the two never replace
each other; they share the `scraping` cost class instead, so the runner
serialises them rather than letting both scrape at once.

The remaining gap is narrower than it was, and it is about WHEN rather than
whether. A cadence is an interval measured from the last recorded run, so a
daily scan drifts to a different hour across restarts, and a machine that was
off for a week runs once when it comes back rather than seven times. Times of
day would bring the two cases that break a naive implementation — a machine
off across the appointed hour, and daylight-saving transitions where an hour
repeats or does not exist — and neither is decided here.
