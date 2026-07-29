"""Start the inbox, and the schedule that runs a scan without being asked.

The one entry point this package has. Two things start a scan here, and they
are deliberately separate registrations:

- a person pressing `Scan` on the page, which posts to `/scan` and goes
  through `web.actions.Actions.scan`. That builds a producer with the limit
  they typed and `reregister`s it, per click, under `ScanProducer.name`;
- `cronicled.schedule.Scheduler`, ticking in the background, which starts
  `runscan.SCHEDULED_SCAN_NAME` — its own registration, its own declared
  cadence, and no file limit.

They share the `scraping` cost class, so the runner serialises them: an
unattended scan and a manual one never scrape the media server at once, and
whichever asks second is refused with a reason the page shows. Nothing either
of them starts writes to the media server; a scan reads and proposes, and
every proposal still waits for a person to approve it.

The ordering in `build_scheduler` below is load-bearing and is the one part
of this file that fails silently when it is wrong. Read it there.

`--server` names a media server; it has no default because there is no safe
guess for it. Without one, the inbox still starts: a person can browse what a
scan already produced, and dismiss or mute proposals, with nothing here that
needs to reach a media server. Approve and Undo are the two actions that
write to one. Scan never writes to a media server -- it only reads the
library and looks candidates up -- but it does need both a media server
and a configured site adapter to search against, so it refuses with its own
clear message when either is missing (see `web.actions.Actions`) rather
than the whole tool being unusable to someone who only wants to look at
what a scan already produced before wiring either one up.

The `JobRunner` built below outlives any one request: it is constructed once,
here, alongside `Store` and `Stash`, and lives for as long as this process
does — a scan a request started keeps running after that request's own
connection has closed. Like `Store` and `Stash`, it is not explicitly closed
on shutdown; that is unchanged from how this entry point already treated the
other two.
"""

import argparse
import os

from . import tags
from .adapters.registry import default_adapters_path, load_adapters
from .config import (CONFIG_DIR_ENV_VAR, config_dir, load_marker_tag,
                     load_schedule)
from .descriptions import DescriptionProducer
from .jobs import JobRunner
from .runscan import DAILY, build_scheduled_producer
from .schedule import Scheduler
from .store import Store
from .stash import Stash
from .tags import TagMergeProducer
from .web.actions import Actions
from .web.app import DEFAULT_HOST, DEFAULT_PORT, serve
from .web.rows import to_merge_rows, to_mute_rows, to_refusal_rows, to_rows

# How long shutdown waits for the loop to come out of a tick. Bounded rather
# than `None`: a tick wedged in the store or the media server would otherwise
# hold the process open forever, and a shutdown that never finishes is one
# somebody replaces with a kill signal. `close` returns whether it made it,
# and that answer is printed rather than dropped.
SCHEDULER_SHUTDOWN_TIMEOUT = 10.0

# How often the unattended tag-merge pass declares itself due, in seconds.
# An INTERVAL measured from its last recorded run, exactly like the scan's
# own cadence (see `cronicled.runscan.DAILY`) and with the same accepted
# drift. Daily rather than more often because the thing it looks for -- two
# spellings of one tag -- appears when somebody adds a tag by hand, which is
# not an hourly event; and because a person has to approve every merge
# anyway, so finding one sooner buys nothing.
TAG_MERGE_EVERY = 86400


def build_scheduler(runner, store, stash, adapters, env=None, marker=None):
    """Register the unattended producers, then resolve a schedule over them.

    **The order of the two statements below is the whole of this function.**
    `Scheduler.__init__` resolves the schedule ONCE, from the producers the
    runner holds at that instant. Built first, it resolves an empty registry:
    it schedules nothing, raises nothing, ticks on time forever and starts
    nothing — a nightly scan that never runs, with no exception, no log line
    and no symptom but an inbox that stays empty. Register first, and the
    same wiring mistake cannot happen; register LATER still and
    `Scheduler.start` refuses out loud, which is the last moment anything can
    say so.

    THREE producers are registered here, and what each one needs is different.
    The scan needs a media server to read the library from AND at least one
    configured adapter to search against, exactly as `Actions.scan` needs both
    before a person's click can do anything. The description pass needs only
    the media server: the fault it looks for is in the text the server already
    holds, so there is nothing for it to search against and no adapter for it
    to want. The tag-merge pass needs only the media server for the same kind
    of reason: tags are the server's own vocabulary and exist whether or not
    any store is configured.

    So an install with a server and no `adapters.json` schedules the
    description and tag-merge passes and says, separately, that the scan is
    the thing not scheduled -- rather than the three being one all-or-nothing
    decision, which would have left two producers with a perfectly good reason
    to run silently unregistered.

    Returns `None`, having said why, only when there is nothing to schedule at
    all, which is an install with no media server: every producer here reads
    something from one. Printed rather than silently skipped in either case,
    because "no scan is scheduled" and "a scan is scheduled and has not run
    yet" look identical from the outside otherwise.

    `marker` is the provisionally-organized marker tag's name, read from the
    config directory this process was started with and passed straight to the
    scheduled scan -- see `cronicled.scan.ScanProducer`. It reaches the scan a
    person presses Scan for by a separate route, through `Actions`, and both
    routes carry the same value from the same read: a marker that only one of
    the two honoured would make an unattended run and a manual one look at
    different halves of the library, with nothing saying which was which.

    The producer a person's `Scan` button builds is deliberately NOT in this
    schedule. It is registered later, under a different name, with the limit
    they typed (see `web.actions.Actions.scan`); it exists to be started by
    hand, and a schedule that also ran it would run somebody's 25-file
    request on a cadence.
    """
    if stash is None or not adapters:
        missing = []
        if stash is None:
            missing.append("no media server (--server)")
        if not adapters:
            missing.append("no configured site adapter (adapters.json)")
        print("no scan is scheduled: %s. The inbox still works, and Scan "
              "still refuses with the same reason." % "; and ".join(missing))
        if stash is None:
            # Nothing else here can run either: every producer below reads
            # the library from the media server.
            return None
    else:
        # First. See this function's docstring for what building the scheduler
        # ahead of this line costs, and why nothing would report it.
        runner.register(build_scheduled_producer(stash, adapters, store,
                                                 marker=marker))
    # The description pass, which wants nothing but the server. Its cadence is
    # passed HERE rather than defaulted inside the producer, for the reason
    # `build_scheduled_producer` passes the scan's: a producer with no cadence
    # is what `resolve` refuses, and defaulting to the refusal would make
    # forgetting the argument look like a decision. Daily, the same interval
    # the scan runs on -- there is no reason for a whole-library text pass
    # that issues one query to run more rarely than the scan beside it.
    runner.register(DescriptionProducer(stash, every=DAILY))
    # The tag-merge pass, which wants nothing but the server either, and for
    # the same reason carries its cadence in from here.
    runner.register(TagMergeProducer(stash, store=store,
                                     every=TAG_MERGE_EVERY))
    # Last, so `resolve` can see the producers above and read each one's
    # declared cadence off it. Overrides come from the operator's own config
    # and are validated by `resolve`, at this line, where a typo is a start-up
    # stack trace rather than a producer that quietly never runs.
    return Scheduler(runner, store, overrides=load_schedule(env=env))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cronicled")
    parser.add_argument("--db", default=os.environ.get(
        "CRONICLED_DB", "cronicled.sqlite3"))
    parser.add_argument("--server", default=os.environ.get("CRONICLED_SERVER"))
    parser.add_argument("--api-key", default=os.environ.get("CRONICLED_API_KEY"))
    # $CRONICLED_HOST/$CRONICLED_PORT give these two the same environment
    # default `--db` already has. DEFAULT_HOST stays 127.0.0.1 here -- the
    # container image is what overrides it, via ENV, not this default.
    parser.add_argument("--host", default=os.environ.get(
        "CRONICLED_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(
        os.environ.get("CRONICLED_PORT", DEFAULT_PORT)))
    parser.add_argument("--config-dir", default=os.environ.get(CONFIG_DIR_ENV_VAR))
    args = parser.parse_args(argv)

    # cronicled.config's loaders (config_dir/load_server/load_adapters) all
    # take an injectable `env` rather than reading os.environ directly, so a
    # CLI override can take effect without mutating the real environment --
    # mutating it here would leak into anything else running in this same
    # process, and outlive this one call. A copy is built, overridden only
    # when --config-dir/$CRONICLED_CONFIG_DIR resolved to something, and
    # passed explicitly wherever this project's own config directory is
    # resolved; os.environ itself is never written to.
    env = dict(os.environ)
    if args.config_dir is not None:
        env[CONFIG_DIR_ENV_VAR] = args.config_dir

    # FIRST, before the start-up line below and before anything is opened,
    # because a config that cannot be understood is a start-up failure and
    # every line printed before it would be describing an install that does
    # not exist.
    #
    # `load_adapters` draws the one distinction that matters here, and it
    # draws it deliberately (see `cronicled.adapters.registry`'s module
    # docstring, and the rule in `cronicled.config`'s):
    #
    #     absent adapters.json  -> an empty mapping, a legitimate fresh
    #                              install, nothing said
    #     present but unloadable -> raises, naming what is wrong
    #
    # This calls it and swallows NOTHING. A caller that turned the second
    # answer into the first -- as this one used to, catching ValueError,
    # RuntimeError and KeyError alike and substituting `{}` -- reports a
    # syntax error, a retired key and a missing required field all as "no
    # adapters are configured", which sends the operator to check that a
    # file they already have exists. Observed three times, from three
    # different causes.
    #
    # `configured_adapters` is deliberately NOT what is called here: it adds
    # "and a scan needs at least one" on top of the loader, which is a
    # refusal that belongs to a caller about to run a scan, not to the app
    # starting. An install with no adapters still starts, still browses,
    # still dismisses and mutes, and `Actions.scan` gives its own clear
    # refusal once somebody presses Scan.
    #
    # `env=env` and not the ambient environment: this is the seam where
    # --config-dir either reaches the adapters or silently does not.
    # Omitting it leaves the flag half-working -- the directory printed
    # below would be the one asked for, while the adapters came from
    # somewhere else -- and nothing would raise to say so.
    #
    # The WHOLE mapping, every configured adapter -- there is no single
    # "the" adapter any more (see `cronicled.runscan.build_producer`): a
    # scan searches every one of them.
    try:
        adapters = load_adapters(env=env)
    except Exception as exc:
        # Nothing is swallowed: every path out of this block either binds
        # `adapters` or raises. The wrapper exists only to name the FILE,
        # which none of the loader's own messages carry -- a JSON syntax
        # error names a line and a column and no file at all -- and it
        # carries the original message through verbatim, chained to the
        # original traceback rather than replacing it.
        raise RuntimeError(
            "the adapter config at %s could not be loaded: %s. An ABSENT "
            "adapters.json is a legitimate fresh install and starts "
            "silently with no adapters; this one is present and cannot be "
            "read, which is not the same state and must not be reported as "
            "it." % (default_adapters_path(env), exc)) from exc

    # Read with the SAME `env`, at the same point and for the same reason as
    # the adapters above: this is the seam where --config-dir either reaches
    # the marker or silently does not, and a marker that silently does not
    # arrive is indistinguishable from an install that never configured one.
    # Absent -- no scan.json, or one that names no tag -- is `None` and a
    # legitimate state; a file that names something unusable raises here,
    # naming itself, which is the same start-up failure a malformed
    # adapters.json is.
    marker = load_marker_tag(env=env)

    # AFTER the load, and reporting what the load produced. Printed before
    # it, this line announced a config directory in good health on every one
    # of the malformed configs above -- the log read as though everything was
    # found while nothing worked.
    print("config directory: %s (adapters: %s)"
          % (config_dir(env=env), ", ".join(sorted(adapters)) or "none"))
    # Printed only when one is configured, so an absent line means "no marker
    # tag is configured" rather than "one is configured and matched nothing".
    # A scan pools organized scenes carrying this tag as well as the
    # unorganized set, which is a materially larger night's work than the
    # default -- worth one line at start-up rather than a surprise in a job
    # log.
    if marker is not None:
        print("marker tag: %r (organized scenes carrying it are scanned too)"
              % (marker,))
    # The other path this process writes to and the operator cannot see from
    # the outside. Two files, both named, so "which database is this reading"
    # is answered by the start-up line rather than by guessing at the flag.
    print("database: %s" % args.db)

    store = Store(args.db)
    if args.server:
        stash = Stash(args.server, args.api_key)
    else:
        stash = None
        print("WARNING: no --server configured. Browsing, dismissing and "
              "muting still work; Approve and Undo will refuse until a "
              "media server is set (--server / --api-key, or "
              "$CRONICLED_SERVER / $CRONICLED_API_KEY).")
    runner = JobRunner(store)
    actions = Actions(store, stash, runner=runner, adapters=adapters,
                      marker=marker)
    # The same address already resolved for `stash` above (or `None`,
    # unconfigured) -- reused here for every row's own link to the media
    # server (ticket 97). Never re-derived from `stash.url`, which has
    # `/graphql` appended for the API client's own purposes (see
    # `Stash.__init__`), and never a second flag of its own: one address,
    # resolved once, used for both jobs.
    base_url = args.server

    def _inbox_rows():
        # Applied proposals get their own section below (ticket 98) -- the
        # inbox itself only ever shows what still needs a decision, and an
        # applied row does not. `items()`'s own default already hides
        # `dismissed`/`muted`/`superseded`; this excludes the one further
        # state the inbox list has to earn its own way out of, on top of
        # that -- see `Actions._find`'s docstring for why that default
        # itself is left untouched: `undo` still needs `items(state=None)`
        # to include an applied row, and this filtering happens here, not
        # by narrowing what `items()` returns.
        # Tag-merge proposals are excluded here and rendered by
        # `_merge_rows` below instead. Not tidiness: `to_row` INDEXES
        # `payload["path"]` and `payload["candidate"]`, which a merge payload
        # has neither of, so a single tag cluster in the store would take the
        # whole page down with a KeyError rather than render oddly.
        return to_rows([item for item in store.items()
                        if item["state"] != "applied"
                        and item["subject_type"] != tags.SUBJECT_TYPE],
                       base_url=base_url)

    def _scene_items(state):
        return [item for item in store.items(state=state)
                if item["subject_type"] != tags.SUBJECT_TYPE]

    def _merge_rows():
        """Every tag-merge proposal that still has something to show, in
        every state that carries a control.

        Three reads rather than one because `items()`'s default view hides a
        person's own rejections: a dismissed cluster needs its Undismiss and
        a muted one its Unmute, and both are only reachable if the row is on
        the page. `superseded` is deliberately absent -- a merge row offers
        no Refresh, so nothing puts one there.

        Muted clusters come from `items(state="muted")`, not from
        `store.mutes()`: that is what keeps `to_mute_row` -- which builds
        its row through `to_rows`, and so knows exactly the two subject
        kinds `to_rows` does -- from ever being handed a merge payload,
        which answers to neither. It also shows the spellings and their
        counts rather than a bare subject id. A cluster muted with no row
        of its own at all would be
        invisible to this, which is unreachable today: Mute is offered only
        on a row that already exists.
        """
        seen = []
        for state in (None, "dismissed", "muted"):
            seen.extend(item for item in store.items(state=state)
                        if item["subject_type"] == tags.SUBJECT_TYPE)
        return to_merge_rows(seen)

    scheduler = build_scheduler(runner, store, stash, adapters, env=env,
                                marker=marker)
    if scheduler is not None:
        scheduler.start()

    try:
        # Every section but Merges filters tag clusters out for the reason
        # `_inbox_rows` states: `to_rows` dispatches between a scene and a
        # performer-description row and has no third branch, and `to_mute_row`
        # goes through `to_rows` for exactly that reason. A merge payload
        # answers to neither, so one cluster in the wrong list is a KeyError
        # that takes the whole page down.
        serve(rows=_inbox_rows,
              merges=_merge_rows,
              muted=lambda: to_mute_rows(
                  [m for m in store.mutes()
                   if m["subject_type"] != tags.SUBJECT_TYPE],
                  base_url=base_url),
              dismissed=lambda: to_rows(_scene_items("dismissed"),
                                        base_url=base_url),
              refused=lambda: to_refusal_rows(store.refusals(),
                                              base_url=base_url),
              superseded=lambda: to_rows(_scene_items("superseded"),
                                         base_url=base_url),
              applied=lambda: to_rows(_scene_items("applied"),
                                      base_url=base_url),
              actions=actions, scan_status=actions.scan_status,
              # `None` when nothing is scheduled, which the page says out
              # loud rather than drawing as a healthy idle schedule.
              schedule_status=(None if scheduler is None
                               else scheduler.status),
              host=args.host, port=args.port)
    finally:
        # In a `finally`, so a `serve` that raises still stops the loop
        # rather than leaving a daemon thread scraping the media server on
        # the way out of a failed start-up.
        if scheduler is not None and not scheduler.close(
                SCHEDULER_SHUTDOWN_TIMEOUT):
            print("WARNING: the schedule's loop was still in a tick after "
                  "%gs; a job it started keeps running until this process "
                  "exits." % SCHEDULER_SHUTDOWN_TIMEOUT)


if __name__ == "__main__":
    main()
