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

from cronicled.adapters.registry import get_adapter, load_adapters
from cronicled.config import load_server
from cronicled.jobs import JobRunner
from cronicled.scan import DEFAULT_THRESHOLD, ScanProducer
from cronicled.search import catalog_search
from cronicled.stash import Stash
from cronicled.store import Store


def build_producer(stash, adapter, store, *, limit, folder="library",
                   name_filter=None, threshold=DEFAULT_THRESHOLD,
                   aliases=None, workers=4):
    """The `ScanProducer` that scans `adapter`'s store through `stash`,
    recording through `store`.

    `limit` is REQUIRED and has no permissive default — it does not even
    accept the keyword unless the caller supplies it, and `None` is refused
    rather than treated as "unlimited". `scan.select` treats `limit=None` as
    "take every survivor of narrowing", and every one it takes spends a
    lookup against a rate-limited third party (the configured scraper). A
    first run against a whole library must not be reachable by a caller who
    simply forgot the flag; this is the same rule `scan.select` states for
    its own `limit=0` (a distinct, honoured instruction, not a missing
    limit) applied one level up, at the one call site nothing else guards.
    Pass `limit=0` deliberately to run the batch accounting — the log line
    naming how many files exist, how many are already muted or proposed —
    with no lookups spent at all.

    `adapter.censorship` reaches both halves of the scan through different
    paths: `catalog_search` expands the QUERY with it before any lookup
    happens, and it is threaded through to `ScanProducer` as well so
    `scan.examine` can decensor each candidate's TITLE for scoring — see
    `scan.examine`'s docstring for why those are two distinct uses and why
    conflating them would let a decensored title reach a proposal.

    `adapter.owner_of` is threaded through the same way, so `scan.examine`'s
    call into `cronicled.artist.resolve` can check a candidate name against
    the catalogue instead of assuming the first one — but ONLY when
    `adapter.catalog_resolvable` says a name search can identify this
    store's creators at all. An adapter configured `owner_source: "none"`
    returns "" from `owner_of` for every result, no matter the candidate;
    passed through unconditionally, every ambiguous file against such a
    store would find zero support for anything and come back unresolved —
    a regression from today's folder-wins default, not the fix this wiring
    exists for. `catalog_resolvable` already existed to answer exactly this
    question; nothing read it before this.

    `stash.scrape_scene_url` is threaded through as `enrich`, unconditionally
    — unlike `owner_of`, this needs no adapter-level gate: it scrapes the
    winning candidate's own URL directly, against whichever scraper the
    media server itself matches to that URL, rather than asking a
    per-adapter name search to identify anything. See `scan.examine`'s
    `enrich` paragraph for what happens when a candidate has no URL to give
    it, or when the scrape itself fails.
    """
    if limit is None:
        raise ValueError(
            "limit is required to build a scan: scan.select treats "
            "limit=None as \"take every survivor of narrowing\", and every "
            "file it selects spends a lookup against a rate-limited "
            "scraper. Pass an explicit limit (0 runs the selection "
            "accounting with no lookups spent, if that is what is wanted).")
    search = catalog_search(stash, adapter)
    owner_of = adapter.owner_of if adapter.catalog_resolvable else None
    return ScanProducer(
        stash, search, store=store, folder=folder, limit=limit,
        name_filter=name_filter, threshold=threshold, aliases=aliases,
        workers=workers, censorship=adapter.censorship, owner_of=owner_of,
        enrich=stash.scrape_scene_url)


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
    later inside `get_adapter` or `catalog_search`.
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
    parser.add_argument("--adapter", default=None,
                        help="adapter name; defaults to the configured "
                             "default adapter (or the only one, if there "
                             "is exactly one)")
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
        adapter = get_adapter(args.adapter, adapters)
    except (ValueError, RuntimeError, KeyError) as exc:
        print("cannot start a scan: %s" % exc, file=sys.stderr)
        return 1

    stash = Stash(server["url"], server["api_key"])
    store = Store(args.db)
    try:
        runner = JobRunner(store)
        producer = build_producer(
            stash, adapter, store, limit=args.limit, folder=args.folder,
            name_filter=args.name_filter, threshold=args.threshold,
            workers=args.workers)
        runner.register(producer)

        job = runner.start(producer.name)
        print("scan %s started against adapter %r" % (job.id, adapter.name))
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
