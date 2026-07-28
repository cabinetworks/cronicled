"""Wires a real `Stash`, a configured `SiteAdapter` and a `Store` into a
runnable library scan, and gives that a command line.

Nothing before this module could actually run `cronicled.scan.ScanProducer`
against a real store: its `search` argument had no production implementation
until `cronicled.search.catalog_search` existed, and nothing constructed one
and registered it with `cronicled.jobs.JobRunner`. This module is that
construction, and `python -m cronicled.runscan` is the command that runs it
once, on request.

This is deliberately NOT a UI trigger. `python -m cronicled` (see
`cronicled/__main__.py`) still constructs no scheduler and starts no scan on
its own; adding a button to the inbox that starts one is a separate task.
And this module itself constructs no `cronicled.schedule.Scheduler` either —
running this command is still a person asking for one scan, not a process
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


def build_producer(stash, adapters, store, *, limit, folder="library",
                   name_filter=None, threshold=DEFAULT_THRESHOLD,
                   aliases=None, workers=4):
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
    rather than treated as "unlimited". `scan.select` treats `limit=None` as
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
    """
    if limit is None:
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
        workers=workers, enrich=stash.scrape_scene_url, identify=identify)


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
