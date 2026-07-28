"""Wires a real media server, a real stash-box instance and a `Store` into a
runnable stash-box check, and gives that a command line -- the counterpart
to `cronicled.runscan` for `cronicled.stashbox_scan.StashBoxCheckProducer`.

Nothing before this module could actually run that producer against a real
stash-box instance: `cronicled.stashbox.StashBox` had no caller outside its
own tests, and nothing built the `performer_ids` mapping it needs to turn a
resolved creator name into a listing to read. This module is that
construction, and `python -m cronicled.runstashbox` is the command that runs
one check, on request -- registered under its own `"box"` cost class, never
inside a `"scraping"`-classed scan (see `cronicled.stashbox_scan`'s own
docstring for why the two must never share a job).

The `performer_ids` mapping -- a resolved creator NAME to the stash-box id
whose listing should be read for them -- is the one piece of this feature
nothing in the project resolves automatically. A stash-box performer is a
different identity from a name resolved out of a folder or a filename, and
this ticket did not find a source that links the two (see this ticket's own
report). What this module offers instead is the plainest possible stopgap: a
flat JSON object, `{"resolved name": "stash-box performer id", ...}`, read
from `--performer-ids` (default: `performer_ids.json` inside the configured
`config_dir()`). An absent file is a legitimate state, not an error -- the
same shape `cronicled.adapters.registry.load_adapters` already uses for a
fresh install -- and means every file is skipped with a stated reason rather
than the run refusing to start.
"""
import argparse
import json
import os
import sys

from cronicled.config import config_dir, load_server, load_stashbox
from cronicled.jobs import JobRunner
from cronicled.scoring import DEFAULT_THRESHOLD
from cronicled.stash import Stash
from cronicled.stashbox import StashBox
from cronicled.stashbox_scan import StashBoxCheckProducer
from cronicled.store import Store


def default_performer_ids_path(env=None):
    return os.path.join(config_dir(env), "performer_ids.json")


def load_performer_ids(path=None, env=None):
    """The operator-maintained `{name: stash-box performer id}` mapping, or
    `{}` when the file does not exist.

    Absence is a legitimate state, not an error, on the same terms
    `cronicled.config.load_stashbox` already treats a missing endpoint: a
    check that finds no mapping simply skips every file with a stated
    reason (see `cronicled.stashbox_scan.StashBoxCheckProducer`) rather than
    refusing to run.
    """
    if path is None:
        path = default_performer_ids_path(env)
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


def build_producer(stash, box, performer_ids, store, *, limit, folder="library",
                   name_filter=None, threshold=DEFAULT_THRESHOLD):
    """The `StashBoxCheckProducer` that checks `stash`'s unorganized scenes
    against `box`, recording nothing -- see that class's own docstring.

    `limit` is REQUIRED, with no permissive default, for the same reason
    `cronicled.runscan.build_producer` refuses one: every file selected pays
    for a whole listing read against a rate-limited public service, and a
    first run against a whole library must not be reachable by a caller who
    simply forgot the flag. Pass `limit=0` deliberately to run the selection
    accounting alone, with no reads spent at all.
    """
    if limit is None:
        raise ValueError(
            "limit is required to run a stash-box check: scan.select "
            "treats limit=None as \"take every survivor of narrowing\", "
            "and every file it selects pages a whole listing against a "
            "rate-limited public service. Pass an explicit limit (0 runs "
            "the selection accounting with no reads spent, if that is what "
            "is wanted).")
    return StashBoxCheckProducer(
        stash, box, performer_ids, store=store, folder=folder, limit=limit,
        name_filter=name_filter, threshold=threshold)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m cronicled.runstashbox",
        description="Check a batch of library files against stash-box's "
                    "own listing for their resolved creator, and log what "
                    "that does and does not establish.")
    parser.add_argument("--db", default=os.environ.get(
        "CRONICLED_DB", "cronicled.sqlite3"))
    parser.add_argument("--folder", default="library",
                        help="the store's proposal namespace this run "
                             "selects against (default: %(default)r)")
    parser.add_argument("--name-filter", default=None,
                        help="only check files whose path contains this "
                             "substring (case-insensitive)")
    parser.add_argument("--limit", type=int, required=True,
                        help="the most files this run may check against "
                             "stash-box; required, with no default")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--performer-ids", default=None,
                        help="path to the {name: stash-box performer id} "
                             "JSON mapping (default: performer_ids.json "
                             "inside the configured config directory)")
    args = parser.parse_args(argv)

    try:
        server = load_server()
    except ValueError as exc:
        print("cannot run a stash-box check: %s" % exc, file=sys.stderr)
        return 1

    stashbox_config = load_stashbox()
    if stashbox_config is None:
        print("cannot run a stash-box check: no stash-box endpoint is "
             "configured (set $STASHBOX_URL/$STASHBOX_API_KEY, or provide "
             "them in stashbox.json inside your config directory -- see "
             "config/stashbox.example.json for the shape)", file=sys.stderr)
        return 1

    performer_ids = load_performer_ids(args.performer_ids)

    stash = Stash(server["url"], server["api_key"])
    box = StashBox(stashbox_config["url"], stashbox_config["api_key"])
    store = Store(args.db)
    try:
        runner = JobRunner(store)
        producer = build_producer(
            stash, box, performer_ids, store, limit=args.limit,
            folder=args.folder, name_filter=args.name_filter,
            threshold=args.threshold)
        runner.register(producer)

        job = runner.start(producer.name)
        print("stash-box check %s started" % job.id)
        runner.wait(job.id)
        finished = runner.job(job.id)
        print("stash-box check %s finished: %s" % (finished.id, finished.message))
        if finished.state == "failed":
            print("stash-box check %s FAILED: %s" % (finished.id, finished.error),
                  file=sys.stderr)
            return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
