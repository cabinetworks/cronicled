"""Run the performer-enrichment pass once, from the command line.

See `cronicled.enrichment` for what this proposes and why. Like
`cronicled.runstashbox`, this is a standalone entry point rather than a job
registered on the always-on scheduler in `cronicled.__main__`: it drives a
`cost="box"` producer, the SAME rate-limited resource
`cronicled.stashbox_scan.StashBoxCheckProducer` rations, and that producer is
kept off the always-on scheduler for the identical reason -- reading the
whole performer library and spending a bounded number of stash-box lookups is
an operator-initiated pass, not a nightly appointment this project has
measured a cadence for yet. Mixing this into the scheduled scan or the
scheduled description pass would also risk exactly what
`cronicled.stashbox_scan`'s own docstring warns against: a `box`-classed read
queueing behind, or racing, work under a different cost class.

Nothing here calls `cronicled.schedule.resolve` -- there is no cadence to
validate, because this never runs on one; it runs once, when invoked, and
exits. `EnrichmentProducer` still accepts `every`/`at`/`zone` for a future
caller that DOES want it scheduled (see that class's own docstring), but this
entry point leaves all three unset.
"""
import argparse
import os
import sys

from cronicled.config import load_server, load_stashbox
from cronicled.enrichment import EnrichmentProducer
from cronicled.jobs import JobRunner
from cronicled.stash import Stash
from cronicled.stashbox import StashBox
from cronicled.store import Store


def build_producer(stash, box, *, folder="library", limit):
    """The one place this entry point assembles an `EnrichmentProducer`,
    mirroring `cronicled.runstashbox.build_producer`'s own reason for
    existing as its own function: a test can exercise the assembly without
    also driving a `JobRunner`.
    """
    return EnrichmentProducer(stash, box, folder=folder, limit=limit)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m cronicled.runenrichment",
        description="Propose values for every blank field of a performer "
                    "that stash-box can identify, by id or by name.")
    parser.add_argument("--db", default=os.environ.get(
        "CRONICLED_DB", "cronicled.sqlite3"))
    parser.add_argument("--folder", default="library",
                        help="the store's proposal namespace this run "
                             "writes into (default: %(default)r)")
    parser.add_argument("--limit", type=int, required=True,
                        help="the most performers this run may spend a "
                             "stash-box lookup on; required, with no "
                             "default -- see cronicled.enrichment "
                             ".EnrichmentProducer's own docstring for why")
    args = parser.parse_args(argv)

    try:
        server = load_server()
    except ValueError as exc:
        print("cannot run enrichment: %s" % exc, file=sys.stderr)
        return 1

    stashbox_config = load_stashbox()
    box = None
    if stashbox_config is None:
        # Not a fatal condition -- the SAME "not configured" state
        # `cronicled.stashbox_scan`/`cronicled.runstashbox` already treat as
        # ordinary rather than broken. `EnrichmentProducer.produce` reads a
        # `None` box and logs that it has nothing to enrich against, rather
        # than raising, so this run finishes cleanly having proposed nothing.
        print("no stash-box endpoint is configured -- this run will "
             "propose nothing (set $STASHBOX_URL/$STASHBOX_API_KEY, or "
             "provide them in stashbox.json inside your config directory "
             "-- see config/stashbox.example.json for the shape)",
             file=sys.stderr)
    else:
        box = StashBox(stashbox_config["url"], stashbox_config["api_key"])

    stash = Stash(server["url"], server["api_key"])
    store = Store(args.db)
    try:
        runner = JobRunner(store)
        producer = build_producer(
            stash, box, limit=args.limit, folder=args.folder)
        runner.register(producer)

        job = runner.start(producer.name, trigger="manual")
        print("performer-enrichment %s started" % job.id)
        runner.wait(job.id)
        finished = runner.job(job.id)
        print("performer-enrichment %s finished: %s"
             % (finished.id, finished.message))
        if finished.state == "failed":
            print("performer-enrichment %s FAILED: %s"
                 % (finished.id, finished.error), file=sys.stderr)
            return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
