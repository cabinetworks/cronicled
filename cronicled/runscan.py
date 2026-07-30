"""Wires a real `Stash`, a configured `SiteAdapter` and a `Store` into a
runnable library scan, and gives that a command line.

Nothing before this module could actually run `cronicled.scan.ScanProducer`
against a real store: its `search` argument had no production implementation
until `cronicled.search.catalog_search` existed, and nothing constructed one
and registered it with `cronicled.jobs.JobRunner`. This module is that
construction, and `python -m cronicled.runscan` is the command that runs it
once, on request.

Two scans are built here, and the difference between them is the point of
`build_scheduled_producer` existing at all. `build_producer` builds the one a
person asks for, with the limit they chose. `build_scheduled_producer` builds
the one `cronicled/__main__.py` registers at start-up for
`cronicled.schedule.Scheduler` to run unattended: its own name, its own
declared appointment, and no file limit.

This module still constructs no `Scheduler` itself — running
`python -m cronicled.runscan` is a person asking for one scan, not a process
that decides scans are due on its own.
"""
import argparse
import os
import sys
from datetime import time

from cronicled.adapters.registry import load_adapters
from cronicled.artist import Aliases
from cronicled.config import load_marker_tag, load_server
from cronicled.jobs import JobRunner
from cronicled.scan import (DEFAULT_THRESHOLD, ScanProducer, Source,
                            identify_by_fingerprint)
from cronicled.search import catalog_search
from cronicled.stash import Stash
from cronicled.store import Store
from cronicled.text import spaceless


# The name the scan a schedule runs is registered under — deliberately NOT
# `ScanProducer.name`, which is what `web.actions.Actions.scan` reregisters on
# every click. Sharing one name would make a manual scan of 25 files replace
# the scheduled producer, so the next unattended run would quietly scan 25
# files instead of the whole set. See `build_scheduled_producer`.
SCHEDULED_SCAN_NAME = "scene-scan"

# A cadence of one day, in seconds — an INTERVAL measured from the last
# recorded run. No longer what the scheduled scan declares (see
# `SCHEDULED_SCAN_AT`), because an interval drifts: restart the service at 2pm
# and every subsequent daily scan happens at 2pm, with nothing anywhere saying
# why. Kept because the interval form is still supported and is still the right
# answer for anything that should run every few minutes — and because it is
# what an operator writes to put a producer back on one
# (`{"every": 86400}` in the schedule config).
DAILY = 86400

# The hour the unattended scan keeps, as a wall-clock time read in the zone
# `cronicled.config.load_zone` names. Overnight because a scan that can now
# reach thousands of files should not begin because somebody restarted the
# service over lunch.
#
# `cronicled.__main__` holds the other two unattended appointments, and all
# three are deliberately DIFFERENT times — see `build_scheduler` there for what
# firing together would cost and why this one is first.
SCHEDULED_SCAN_AT = time(3, 0)


class _EveryFile:
    """The one value `build_producer` accepts as "no file limit".

    `limit=None` stays refused, because a caller who forgot the argument and
    a caller who wants the whole library must not be able to write the same
    thing. This is a sentinel nobody produces by accident: it has to be
    imported and named at the call site, where it reads as the deliberate
    instruction it is rather than as an omission.
    """

    def __repr__(self):
        return "EVERY_FILE"


EVERY_FILE = _EveryFile()


def configured_aliases(adapters):
    """The one alias map a scan resolves against: every configured adapter's
    own map, pooled and checked once.

    An operator's alias entry says "the folder filed as X is really the
    creator Y" (see `cronicled.artist.Aliases`). It is declared per adapter
    because `adapters.json` is the only configuration a store has — but
    unlike `censorship`, which stays bound to the `Source` of the store that
    censors the word, an alias cannot be applied per store: the resolver runs
    once per FILE, off that file's own folder, before any store has been
    searched. There is one answer per file to give, so there is one map.

    Two adapters declaring the same folder are therefore checked against
    each other before either is pooled — but what decides it is whether they
    NAME THE SAME CREATOR, not whether they both spoke. Two adapters that
    agree agree; refusing that took the service down at start-up, before
    there was a page to read the refusal on, over a configuration shape that
    is the ordinary one: an adapter block gets written by copying another
    and changing the store-specific fields, so a shared alias map comes
    along with the copy. An entry two adapters agree on is pooled once and
    the run starts.

    Two adapters that name DIFFERENT creators for one folder is the real
    ambiguity, and it is still refused — at start-up, deliberately. Pooling
    by `dict.update` would hand the answer to whichever adapter sorted last,
    the iteration-order attribution this project has already removed from
    three other places, and a wrong attribution is written into proposals
    where nothing later re-checks it. The refusal names both adapters AND
    both names: the keys are identical by construction — they are what
    collided — so a message built from them prints one folder name twice and
    leaves the operator to diff two files by eye. Adapters are visited in
    name order, so the same pair of lines produces the same message whatever
    order the config file happens to list them in.

    "The same creator" means the name as WRITTEN, compared exactly. The
    normalised forms are deliberately not compared: agreement is what lets a
    single value be pooled, and if two spellings of one name counted as
    agreement then the spelling pooled — which is the name every proposal
    from this run carries — would be whichever adapter sorted first. That is
    the same iteration-order attribution one level down, where nothing looks
    at it. Exact equality is the only reading under which the pooled map
    cannot depend on adapter order. The cost is a refusal an operator clears
    by making two lines identical, against a message showing both.

    The KEYS, in contrast, are compared normalised, because that is what a
    lookup matches on: "V Crane" and "vcrane" are one entry, not two, so two
    adapters spelling one folder differently and naming one creator agree.
    Which of the two spellings is pooled is unobservable — `Aliases` keys its
    index on the normalised form, so both build the identical map.

    Returns an `Aliases`, built here and passed whole to the run, which is
    where the remaining malformed-map refusals (a duplicated key, a key that
    normalises to nothing, a value that is not a name) come from. A
    `DeclarativeAdapter` has already applied those to its own map as it
    loaded; this catches the cross-adapter cases that check cannot see, and
    stands as the backstop for a hand-written adapter that never went
    through it. One adapter declaring the same folder twice stays that
    refusal's business, agreeing values or not — the pairwise check below
    skips an adapter against itself, so both spellings reach the pooled map
    and `Aliases` refuses them there, describing the mistake that was
    actually made rather than telling an operator to declare in one adapter
    what they already did.

    One residual, stated rather than claimed away: an adapter that declares
    one folder twice AND shares that folder with another adapter every one of
    them names the same creator for has its internal duplicate absorbed by
    the agreement above, and is not refused. Every line agrees, so nothing
    about the attribution is decided by order; what is lost is the tidiness
    complaint, not a guard.
    """
    pooled = {}
    declared_by = {}
    for adapter_name, adapter in sorted(adapters.items()):
        for key, full in (adapter.aliases or {}).items():
            slug = spaceless(key)
            first = declared_by.get(slug)
            if first is not None and first[0] != adapter_name:
                first_adapter, first_full = first
                if full != first_full:
                    raise ValueError(
                        "adapters %r and %r disagree about the alias for "
                        "%r: %r names %r, %r names %r. An alias names a "
                        "creator, not a store, and one map is built for the "
                        "whole scan, so there is one answer to give for that "
                        "folder — correct whichever line is wrong, or leave "
                        "the entry in exactly one adapter"
                        % (first_adapter, adapter_name, slug,
                           first_adapter, first_full, adapter_name, full))
                # The two agree: already pooled, under the equal name the
                # earlier adapter wrote.
                continue
            declared_by[slug] = (adapter_name, full)
            pooled[key] = full
    return Aliases(pooled)


def build_producer(stash, adapters, store, *, limit, folder="library",
                   name_filter=None, threshold=DEFAULT_THRESHOLD,
                   workers=4, marker=None, producer_name=None, every=None,
                   at=None, zone=None):
    """The `ScanProducer` that scans EVERY one of `adapters` through `stash`,
    recording through `store`.

    `adapters` is the whole configured mapping (name -> `SiteAdapter`) —
    ordinarily `configured_adapters()`'s own return value — not one adapter
    singled out. A scan searches every configured store for every file; see
    `cronicled.scan.examine_sources` for why deciding once over everything
    that comes back, rather than stopping at the first store that answers,
    is the whole point of this ticket. Sorted by name before becoming
    `Source`s below so the ORDER `ScanProducer` sees never depends on
    whatever order a dict, or a JSON file's own key order, happened to
    produce — nothing about which store wins should be able to depend on
    that.

    `limit` is REQUIRED and has no permissive default — it does not even
    accept the keyword unless the caller supplies it, and `None` is refused
    rather than treated as "unlimited"; `EVERY_FILE` is the one way to ask
    for no limit, and it has to be named to be got. `scan.select` treats
    `limit=None` as
    "take every survivor of narrowing", and every one it takes spends a
    lookup against a rate-limited third party (the configured scraper) FOR
    EVERY CONFIGURED STORE. A first run against a whole library must not be
    reachable by a caller who simply forgot the flag; this is the same rule
    `scan.select` states for its own `limit=0` (a distinct, honoured
    instruction, not a missing limit) applied one level up, at the one call
    site nothing else guards. Pass `limit=0` deliberately to run the batch
    accounting — the log line naming how many files exist, how many are
    already muted or proposed — with no lookups spent at all.

    Each adapter's own `censorship` and `owner_of` stay bound to that
    adapter's `Source`, never pooled into one map or one reader for the
    whole run — see `Source`'s own docstring for what each does. `owner_of`
    is threaded through only when `adapter.catalog_resolvable` says a name
    search can identify that store's creators at all: an adapter configured
    `owner_source: "none"` returns "" from `owner_of` for every result, no
    matter the candidate, and passed through unconditionally would find
    zero support for anything on an ambiguous file — a regression from the
    folder-wins default, not the fix this wiring exists for.
    `catalog_resolvable` doubles as the discriminator `_choose_winner` uses
    when more than one store matches the same file, which is the whole
    reason it travels with the `Source` rather than being consulted only
    here and thrown away.

    `adapter.search_query` travels with the `Source` too, as `title_query`:
    the per-title fallback `examine_sources` spends on a file the per-creator
    pass could not resolve. It is threaded through UNCONDITIONALLY, with no
    equivalent of `owner_of`'s `catalog_resolvable` gate — phrasing a query
    is something every adapter can do (the base class supplies one, and a
    store whose spec sets `search_omits_seed` overrides it), and a store that
    cannot attribute a result to a creator can still be asked for a title.
    Omitting it would leave the fallback dead in production while every test
    that injects its own `Source` went on passing.

    `stash.scrape_scene_url` is threaded through as `enrich`, unconditionally
    and once for the whole run — unlike `owner_of`, this needs no
    adapter-level gate and no per-store copy: it scrapes the winning
    candidate's own URL directly, against whichever scraper the media
    server itself matches to that URL, rather than asking any one store's
    name search to identify anything. See `scan.examine`'s `enrich`
    paragraph for what happens when a candidate has no URL to give it, or
    when the scrape itself fails.

    `identify` is wired the same way and for the same reasons: one
    collaborator for the whole run, no adapter-level gate and no per-store
    copy, because a stash-box identifies a file by the file's own
    fingerprints and has nothing to do with which stores are configured.
    The boxes are read at SCAN time, not here — `stash.stash_boxes()` is
    called inside the closure, so a box added to the server between building
    a producer and running it is asked, and a producer built once and run
    twice does not hold a stale list. An install with no box configured
    answers `[]`, the closure asks nobody, and every file takes the text
    path exactly as it does today.

    The operator's ALIASES are read off those same adapters and pooled into
    one map for the run — see `configured_aliases` for why one and not one
    per store. There is deliberately no `aliases` parameter to pass them in
    by: every caller here already holds the adapters, and an argument a
    caller can omit is exactly how this arrived. Configuring an alias had no
    effect at all, on any scan any of the three entry points started, because
    the one that mattered — the page's Scan button, through
    `cronicled.web.actions.Actions.scan` — passed only `limit`, and a test
    that called this function with an explicit map went on passing. Derived
    here, the scheduled scan, the page's scan and the command line get the
    same map without any of them saying so, and no fourth call site can
    forget it.

    `marker` is the name of the tag that says a scene was organized
    PROVISIONALLY — see `cronicled.scan.ScanProducer` for what it pools and
    `cronicled.config.load_marker_tag` for where an operator writes it.
    Passed in rather than read here, and read with an `env` at each entry
    point rather than off the ambient environment, because `--config-dir`
    exists: a loader called here with no `env` would read a different
    directory than the one the process was started with, find no file, and
    return `None` — configuration that silently does nothing, which is the
    failure this argument is being wired to end rather than repeat. `None`
    (the default) is an operator who has named no marker, and pools the
    unorganized set exactly as before.

    `producer_name` and the timing arguments are what make a producer
    schedulable and are deliberately separate from each other. `producer_name`
    overrides `ScanProducer.name` so a scan started on a cadence is its own
    registration rather than sharing the one a manual scan replaces on every
    click (it is spelled differently from the `name` bound inside the
    `sources` comprehension below, which is an ADAPTER's name).

    `every` is a cadence in seconds and `at`/`zone` are a stated time of day
    and the zone to read it in; `cronicled.schedule.resolve` reads whichever
    was set off the producer. All three are `None` by default — what a manual
    scan keeps — so the producer declares no schedule at all and `resolve`
    refuses to schedule it rather than inventing one. Passing BOTH an `every`
    and an `at` is refused there too, as a contradiction: see
    `build_scheduled_producer` for the pair the unattended scan actually
    declares.
    """
    if limit is EVERY_FILE:
        # The deliberate unbounded caller, spelled out at its own call site.
        # `select` reads `None` as "take every survivor of narrowing", which
        # is exactly what was asked for here — and is exactly what must not
        # be reachable by forgetting an argument, which is why the refusal
        # below is untouched.
        limit = None
    elif limit is None:
        raise ValueError(
            "limit is required to build a scan: scan.select treats "
            "limit=None as \"take every survivor of narrowing\", and every "
            "file it selects spends a lookup against a rate-limited "
            "scraper. Pass an explicit limit (0 runs the selection "
            "accounting with no lookups spent, if that is what is wanted).")
    aliases = configured_aliases(adapters)
    sources = [
        Source(name=name, search=catalog_search(stash, adapter),
              owner_of=(adapter.owner_of if adapter.catalog_resolvable
                        else None),
              catalog_resolvable=adapter.catalog_resolvable,
              censorship=adapter.censorship,
              title_query=adapter.search_query)
        for name, adapter in sorted(adapters.items())
    ]

    def identify(scene_ids):
        return identify_by_fingerprint(
            scene_ids, boxes=stash.stash_boxes(),
            lookup=stash.scrape_scenes_by_fingerprint)

    return ScanProducer(
        stash, sources, store=store, folder=folder, limit=limit,
        name_filter=name_filter, threshold=threshold, aliases=aliases,
        workers=workers, enrich=stash.scrape_scene_url, identify=identify,
        marker=marker, name=producer_name, every=every, at=at, zone=zone)


def build_scheduled_producer(stash, adapters, store, *, zone,
                             at=SCHEDULED_SCAN_AT, **kwargs):
    """The scan a `cronicled.schedule.Scheduler` runs unattended.

    Three things separate it from the scan a person presses a button for, and
    each of them is a way this wiring would otherwise fail quietly:

    - **Its own name**, `SCHEDULED_SCAN_NAME`. `web.actions.Actions.scan`
      builds a fresh producer per click and `reregister`s it, because a
      scan's `limit` can only be fixed at construction. Under a shared name
      that replaces this one, so a manual scan of 25 files would silently
      become what the next unattended run scans, with nothing anywhere saying
      so. Two producers in the same `scraping` cost class SERIALISE rather
      than collide, which is the behaviour actually wanted: an unattended
      scan and a manual one must not scrape the media server at once, and
      `jobs.COST_CLASS_LIMITS` already enforces that.

    - **No file limit.** The whole unorganized set, every run, spelled
      `EVERY_FILE` at this call site so nobody later reads it as a forgotten
      argument. The cost is bounded by what a scan actually spends: lookups
      collapse per CREATOR rather than per file, the per-title fallback is
      one search per (file, store), and a fingerprint pass is one batched
      call per box. It also shrinks every night, because each run's proposals
      take their files out of the next run's selection.

    - **A declared appointment**, so `resolve` has something to schedule it
      on. `SCHEDULED_SCAN_AT` — 03:00 — rather than a 24-hour interval, which
      drifts to whichever hour the process last restarted at. `at` is
      overridable here for the same reason the schedule accepts overrides at
      all, but it has a value rather than a `None` default: a producer with no
      schedule is what `resolve` refuses, and defaulting to the refusal would
      make forgetting the argument look like a decision.

    `zone` is REQUIRED and has no default, which is the one thing here that is
    not overridable. There is no zone this could fall back to: the host's is a
    property of the deployment rather than of the schedule (a container's is
    UTC and its operator's is not), and defaulting to UTC here would mean two
    places deciding the zone — this one and
    `cronicled.config.load_zone`, which the page reads — with nothing to say
    they disagreed. A page saying 3am while the scan ran at a different 3am is
    worse than either being wrong alone. So the caller passes the one setting
    in, and forgetting it is a `TypeError` at start-up rather than an
    appointment kept in an hour nobody chose.
    """
    return build_producer(stash, adapters, store, limit=EVERY_FILE,
                          producer_name=SCHEDULED_SCAN_NAME, at=at, zone=zone,
                          **kwargs)


def configured_adapters(env=None):
    """The operator's configured adapters, or a refusal that says what to do
    about it.

    `adapters.registry.load_adapters` returns an EMPTY mapping for a fresh
    install, deliberately — see its module docstring: absence of an adapter
    is a legitimate state for the app to start in, so the loader itself must
    not raise. But a scan is not the app starting; it is a caller who is
    about to run one, and a scan needs at least one adapter to search
    against. Raising HERE, at the one call site that actually needs an
    adapter to exist, keeps `load_adapters` honest about its own contract
    while still refusing loudly rather than failing obscurely three calls
    later inside `catalog_search`.

    The WHOLE mapping is returned, every configured adapter — there is no
    "the one adapter" any more. `build_producer` searches every one of them
    for every file (see its own docstring); a caller that wants a single
    named adapter for some other purpose indexes this mapping directly.
    """
    adapters = load_adapters(env=env)
    if not adapters:
        raise RuntimeError(
            "no adapters are configured: a scan needs at least one to "
            "search against. Create adapters.json inside your config "
            "directory (see config/adapters.example.json for the shape), "
            "then try again.")
    return adapters


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m cronicled.runscan",
        description="Run one library scan against a configured media "
                    "server and site adapter, and record what it finds.")
    parser.add_argument("--db", default=os.environ.get(
        "CRONICLED_DB", "cronicled.sqlite3"))
    parser.add_argument("--folder", default="library",
                        help="the store's proposal namespace this run "
                             "writes into (default: %(default)r)")
    parser.add_argument("--name-filter", default=None,
                        help="only scan files whose path contains this "
                             "substring (case-insensitive)")
    parser.add_argument("--limit", type=int, required=True,
                        help="the most files this run may look up against "
                             "the scraper; required, with no default")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="score a candidate must clear to auto-propose "
                             "(default: %(default)s)")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    try:
        server = load_server()
        adapters = configured_adapters()
        # Read HERE, beside the other two, and never inside `build_producer`:
        # it is configuration, it is read once at start-up, and a failure to
        # understand it belongs in the same "cannot start a scan" message the
        # other loaders' failures land in.
        marker = load_marker_tag()
    except (ValueError, RuntimeError, KeyError) as exc:
        print("cannot start a scan: %s" % exc, file=sys.stderr)
        return 1

    stash = Stash(server["url"], server["api_key"])
    store = Store(args.db)
    try:
        runner = JobRunner(store)
        producer = build_producer(
            stash, adapters, store, limit=args.limit, folder=args.folder,
            name_filter=args.name_filter, threshold=args.threshold,
            workers=args.workers, marker=marker)
        runner.register(producer)

        job = runner.start(producer.name, trigger="manual")
        print("scan %s started against stores %s"
             % (job.id, ", ".join(sorted(adapters))))
        runner.wait(job.id)
        finished = runner.job(job.id)
        print("scan %s finished: %s" % (finished.id, finished.message))
        if finished.state == "failed":
            print("scan %s FAILED: %s" % (finished.id, finished.error),
                  file=sys.stderr)
            return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
