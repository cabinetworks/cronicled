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
declared cadence, and no file limit.

This module still constructs no `Scheduler` itself — running
`python -m cronicled.runscan` is a person asking for one scan, not a process
that decides scans are due on its own.
"""
import argparse
import os
import sys

from cronicled.adapters.registry import load_adapters
from cronicled.config import load_server
from cronicled.jobs import JobRunner
from cronicled.scan import (DEFAULT_THRESHOLD, ScanProducer, Source,
                            identify_by_fingerprint)
from cronicled.search import catalog_search
from cronicled.stash import Stash
from cronicled.store import Store


# The name the scan a schedule runs is registered under — deliberately NOT
# `ScanProducer.name`, which is what `web.actions.Actions.scan` reregisters on
# every click. Sharing one name would make a manual scan of 25 files replace
# the scheduled producer, so the next unattended run would quietly scan 25
# files instead of the whole set. See `build_scheduled_producer`.
SCHEDULED_SCAN_NAME = "nightly-library-scan"

# How often the scheduled scan declares itself due, in seconds. An INTERVAL,
# not a time of day: `cronicled.schedule` measures from the last recorded run,
# so a daily scan drifts to a different hour across restarts. That is accepted
# — wall-clock scheduling brings a machine that was off across the appointed
# hour, and daylight-saving transitions where an hour repeats or does not
# exist, and neither is decided here.
DAILY = 86400


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


def build_producer(stash, adapters, store, *, limit, folder="library",
                   name_filter=None, threshold=DEFAULT_THRESHOLD,
                   aliases=None, workers=4, producer_name=None, every=None):
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

    `producer_name` and `every` are what make a producer schedulable and are
    deliberately separate from each other. `producer_name` overrides
    `ScanProducer.name` so a scan started on a cadence is its own
    registration rather than sharing the one a manual scan replaces on every
    click (it is spelled differently from the `name` bound inside the
    `sources` comprehension below, which is an ADAPTER's name). `every` is
    the cadence the producer DECLARES, which
    `cronicled.schedule.resolve` reads off it; left `None` — the default, and
    what a manual scan keeps — the producer declares none, and `resolve`
    refuses to schedule it rather than inventing an interval.
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
        name=producer_name, every=every)


def build_scheduled_producer(stash, adapters, store, *, every=DAILY, **kwargs):
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

    - **A declared cadence**, so `resolve` has an interval to schedule it on.
      `every` is overridable here for the same reason the schedule accepts
      overrides at all, but it has a value rather than a `None` default: a
      producer with no cadence is what `resolve` refuses, and defaulting to
      the refusal would make forgetting the argument look like a decision.
    """
    return build_producer(stash, adapters, store, limit=EVERY_FILE,
                          producer_name=SCHEDULED_SCAN_NAME, every=every,
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
            workers=args.workers)
        runner.register(producer)

        job = runner.start(producer.name)
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
