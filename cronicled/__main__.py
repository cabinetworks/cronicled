"""Start the inbox, and the schedule that runs a scan without being asked.

The one entry point this package has. Two things start a scan here, and they
are deliberately separate registrations:

- a person pressing `Scan` on the page, which posts to `/scan` and goes
  through `web.actions.Actions.scan`. That builds a producer with the limit
  they typed and `reregister`s it, per click, under `ScanProducer.name`;
- `cronicled.schedule.Scheduler`, ticking in the background, which starts
  `runscan.SCHEDULED_SCAN_NAME` — its own registration, its own declared
  appointment, and no file limit.

They share the `scraping` cost class, so the runner serialises them: an
unattended scan and a manual one never scrape the media server at once, and
whichever asks second is refused with a reason the page shows. Nothing either
of them starts writes to the media server; a scan reads and proposes, and
every proposal still waits for a person to approve it.

The ordering in `build_scheduler` below is load-bearing and is the one part
of this file that fails silently when it is wrong. Read it there.

`--server` names a media server; it has no default because there is no safe
guess for it. When neither it nor `$CRONICLED_SERVER` supplies an address, the
`server.json` in this project's own config directory is asked last — the same
file `runscan` and `runstashbox` have always read, which this entry point used
to ignore, so a complete config directory started a service that reported no
media server. Without any of the three, the inbox still starts: a person can
browse what a scan already produced, and dismiss or mute proposals, with
nothing here that needs to reach one. Approve and Undo are the two actions that
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
from datetime import time

from . import performer_tags, tag_hygiene, tags
from .adapters.registry import default_adapters_path, load_adapters
from .config import (CONFIG_DIR_ENV_VAR, ZONE_ENV_VAR, config_dir,
                     default_server_path, load_marker_tag, load_schedule,
                     load_server, load_zone)
from .descriptions import DescriptionProducer
from .jobs import JobRunner
from .runscan import build_scheduled_producer
from .schedule import Scheduler, check_zone
from .store import GONE, RUN_HISTORY_LIMIT, Store
from .stash import Stash
from .tags import TagMergeProducer
from .web import inboxes
from .web.actions import Actions
from .web.app import DEFAULT_HOST, DEFAULT_PORT, serve
from .web.rows import (to_merge_rows, to_mute_rows, to_reconcile_rows,
                        to_refusal_rows, to_rows, to_schedule_view,
                        to_summary_view, to_unused_groups)

# The subject types the scene/description row builders cannot draw. `to_rows`
# dispatches between a scene row and a performer-description row and has no
# third branch, so a tag cluster or a tag/performer reconciliation reaching it
# is a `KeyError` on `payload["path"]` that takes the whole page down rather
# than rendering oddly. Named once, here, so the five lists below narrow on one
# list and a fourth producer cannot be added to one of them and missed in the
# rest.
_OWN_SECTION_SUBJECTS = (tags.SUBJECT_TYPE, performer_tags.SUBJECT_TYPE,
                         tag_hygiene.SUBJECT_TYPE)

# Which heading on the summary page each producer's proposals are counted
# under, and what that heading is called. SIX subject types across three
# headings: a reviewer thinks in terms of the thing being changed -- a scene, a
# tag, a performer -- and not in terms of which pass proposed it, so the four
# tag-shaped producers are one number.
#
# Derived from `web.inboxes.INBOXES` rather than declared a second time here.
# The two used to be independent, exhaustive maps over the same six subject
# types -- this one, and the inbox each subject type's page belongs to -- and
# two maps over one set of values drift silently: the summary would count one
# grouping while the sidebar and routes used another, with nothing to say so.
# `INBOXES` is the survivor because its own totality is enforced (see
# `inboxes.check_total` and `tests/test_inboxes.py`); this reads it rather
# than re-declaring it, so the two cannot disagree again.
#
# EVERY subject type this package declares must appear here, and
# `tests/test_main.py::test_every_subject_type_this_package_declares_has_a_heading`
# discovers them by import rather than by list, so a seventh added later fails
# the suite instead of arriving on the page under a heading nobody designed --
# kept even though `INBOXES`'s own exhaustiveness test now covers the same
# ground independently, because the two catch a regression in different
# places (a heading and a page) and neither implies the other stays wired.
# That check is where the coverage rule lives; `waiting_counts` itself still
# falls back rather than raising, for the reason stated in its docstring.
#
# Ordered, and rendered in this order, because a list that reorders itself as
# the counts change is one a reader has to re-find their place in every time
# they look. `INBOXES` is itself ordered scenes/tags/performers (see its own
# module), so this preserves the order rather than choosing one.
WAITING_SECTIONS = tuple(inboxes.INBOXES.items())

# How much of the run log the summary reads to find each job's last run.
# `recent_runs`'s own default is twenty, which is the wrong bound for this
# question: three jobs share the log, so twenty runs of the scan -- an
# afternoon of pressing Scan -- would push the nightly tag pass off the end and
# the page would answer "did it run?" with silence. Reading the whole retained
# log is what makes the answer depend on the job rather than on how busy its
# neighbours have been.
#
# The residual, stated rather than claimed away: a job whose last run has been
# EVICTED shows as never having run. That is retention's bound, not this one,
# and it is 500 runs of everything -- months of nightly passes.
SUMMARY_RUN_HISTORY = RUN_HISTORY_LIMIT


def waiting_counts(items):
    """How many proposals are still waiting on a person, per summary heading.

    `items` is `Store.items()`: its default view already hides a reviewer's own
    dismissals and mutes, and `applied` is dropped here for the reason
    `_inbox_rows` drops it -- an applied proposal is a decision that has been
    made, and counting it as outstanding work would put a number on the page
    that never comes down.

    A subject type NO HEADING CLAIMS gets a heading of its own, named after the
    type itself, and this deliberately does NOT raise. The choice is between
    two visible failures, not between a visible one and a silent one:

    - falling back puts an undesigned heading on the page, which the operator
      can read, act on, and which costs one line in `WAITING_SECTIONS` to fix;
    - raising takes down the landing page on every render -- including the link
      to the inbox where those proposals are -- because a producer was added.
      The reader loses the whole front page and gets no way to reach the work.

    So the guard is moved earlier rather than made louder:
    `tests/test_main.py::test_every_subject_type_this_package_declares_has_a_heading`
    discovers every `SUBJECT_TYPE` the package declares, by import, and fails
    if one has no heading. A seventh type is caught before it ships. What must
    never happen is the count silently omitting it -- a summary reporting
    nothing waiting while a full inbox sits behind the link is the exact
    failure this page exists to catch, and that is what the fallback prevents.
    """
    counts = {name: 0 for name, _ in WAITING_SECTIONS}
    heading = {subject: name
               for name, subjects in WAITING_SECTIONS
               for subject in subjects}
    for item in items:
        if item["state"] == "applied":
            continue
        name = heading.get(item["subject_type"], item["subject_type"])
        counts[name] = counts.get(name, 0) + 1
    return counts

# How long shutdown waits for the loop to come out of a tick. Bounded rather
# than `None`: a tick wedged in the store or the media server would otherwise
# hold the process open forever, and a shutdown that never finishes is one
# somebody replaces with a kill signal. `close` returns whether it made it,
# and that answer is printed rather than dropped.
SCHEDULER_SHUTDOWN_TIMEOUT = 10.0

# The two unattended appointments this module decides, as wall-clock times read
# in the zone `cronicled.config.load_zone` names. The third is the scan's, and
# it belongs to the module that builds it
# (`cronicled.runscan.SCHEDULED_SCAN_AT`, 03:00).
#
# ALL THREE ARE DIFFERENT TIMES, and the stagger is the point rather than a
# detail -- see `build_scheduler` for what firing together would cost and for
# why the scan goes first.
#
# Twenty minutes apart rather than five, so that the gap is longer than the two
# passes decided here actually take -- each is one read of the library and then
# text work -- and short enough that all three still happen in the same small
# hours.
#
# The residual, stated rather than claimed away: a scan of a large library can
# run past 03:40, so the appointments being apart does not GUARANTEE the runs
# are. What the stagger delivers is that they do not begin together, which is
# the part a schedule can promise; bounding a run's duration is something this
# project deliberately does not claim anywhere (see `schedule.Scheduler`).
DESCRIPTIONS_AT = time(3, 20)
TAG_MERGE_AT = time(3, 40)


def build_scheduler(runner, store, stash, adapters, *, zone, env=None,
                    marker=None):
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

    THE THREE APPOINTMENTS ARE STAGGERED, AND WHY
    ---------------------------------------------
    Each of the three declares a stated time of day rather than a 24-hour
    interval, so the hour stops depending on when the service was last
    restarted. They are 03:00, 03:20 and 03:40 -- deliberately three different
    times, never one.

    What firing together would cost is NOT that they queue. It is worth being
    exact about this, because "the cost classes will serialise them" is the
    reassurance that sounds right and is wrong here: the three sit in three
    DIFFERENT classes -- the scan in `scraping`, the description pass in
    `local`, the tag pass in `box` -- and `jobs.COST_CLASS_LIMITS` counts each
    class on its own. So one appointment for all three would genuinely start
    all three at once, and the two limits of one would each be satisfied while
    doing nothing whatever about the overlap.

    That overlap is exactly what those limits exist to prevent, one class at a
    time: the comment on `COST_CLASS_LIMITS` says `scraping` and `box` are both
    capped at one because each drives a headless browser inside the media
    server, and a second one running alongside thrashes it and makes both
    slower rather than faster. Two of these three are those two classes. A
    single 03:00 would hand the media server precisely the concurrency the
    limits were written to deny it, through the one door they cannot watch.

    The scan goes FIRST, and that is the order rather than an arbitrary
    tie-break: what it proposes -- studios, performers, tags attached to
    scenes -- is the material the other two pass over. A person still has to
    approve any of it, so the dependency is not same-night, but where an order
    has to be chosen this is the direction that makes sense of it, and the
    reverse would have the two vocabulary passes reading a library the night's
    scan had not looked at yet.

    Note what the stagger does NOT rely on. A producer refused because its
    class is busy is skipped, not queued, and it is not recorded as having run
    -- so it stays due and the next tick starts it, a minute later rather than
    a day. Staggering makes that path rare; the schedule survives it either
    way, which is the property worth having.

    `zone` is the `tzinfo` all three appointments are read in, and it is
    REQUIRED. It comes from the one setting the PAGE also renders its
    timestamps in (`cronicled.config.load_zone`, resolved once in `main`
    below), because a page saying 3am while a pass runs at a different 3am is
    worse than either being wrong alone -- the page would be evidence for the
    schedule an operator was trying to check. No default here: a second place
    deciding the zone is the disagreement itself.

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
                                                 marker=marker, zone=zone))
    # The description pass, which wants nothing but the server. Its appointment
    # is passed HERE rather than defaulted inside the producer, for the reason
    # `build_scheduled_producer` passes the scan's: a producer with no schedule
    # is what `resolve` refuses, and defaulting to the refusal would make
    # forgetting the argument look like a decision. Twenty minutes after the
    # scan, never the same time -- see this function's docstring.
    runner.register(DescriptionProducer(stash, at=DESCRIPTIONS_AT, zone=zone))
    # The tag-merge pass, which wants nothing but the server either, and for
    # the same reason carries its appointment in from here. Last of the three.
    runner.register(TagMergeProducer(stash, store=store, at=TAG_MERGE_AT,
                                     zone=zone))
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

    # ONE zone, resolved once, for two jobs: the hour each unattended pass
    # keeps, and the hour every timestamp on the page is shown in. Read with
    # the same `env` as everything above, and validated HERE rather than
    # wherever it is first used -- an unknown name is a stack trace before the
    # service is listening, which is the difference between an operator fixing
    # a typo and a tick raising at 3am for as long as nobody looks.
    #
    # `check_zone` is `resolve`'s own rule for an override's `zone`, not a
    # second one: the page and the schedule reading one setting is only worth
    # anything if they agree about which names it may hold.
    #
    # Not resolved inside `build_scheduler`, which returns `None` on an install
    # with no media server: the page still renders there, still shows
    # timestamps, and a zone validated only on the path that builds a schedule
    # would be unchecked on exactly the install that has no schedule to check
    # it.
    zone = check_zone(load_zone(env=env), "$%s" % ZONE_ENV_VAR)

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
    # ALWAYS printed, unlike the marker line above, and that asymmetry is the
    # point: there is no such thing as "no zone configured" here -- an install
    # that names none keeps its appointments in UTC and shows its timestamps in
    # UTC, which is a decision with consequences rather than an absence. An
    # operator wondering why the overnight passes ran at 4am reads this line and
    # has their answer; a line printed only when the setting was written would
    # be silent for exactly the install that got it wrong.
    print("zone: %s (the unattended passes run overnight in it, and every "
          "time on the page is shown in it)" % zone)

    store = Store(args.db)
    # Before anything below can open a run of its own -- the scheduler
    # further down this function, or a manual scan a request triggers later
    # -- so every row this finds open belongs to a process that no longer
    # exists (this one has just started and has opened nothing yet). See
    # `Store.close_interrupted_runs` for why that makes the moment, not a
    # timestamp or a heartbeat, the whole discriminator, and why calling
    # this a second time later -- on a timer, or per run -- would risk
    # closing a pass this same process is still genuinely running.
    reopened = store.close_interrupted_runs()
    if reopened:
        print("run log: closed %d row(s) left open by an earlier process "
              "(recorded as interrupted, not failed)" % reopened)
    # THREE sources, asked in this order and no other: the flags, then their
    # environment variables (argparse has already folded that half into
    # `args.server`), then `server.json` in this project's own config
    # directory. The file goes LAST so that no existing invocation changes --
    # anyone passing either flag or either variable today keeps exactly what
    # they have, and a deployment configured that way is not quietly
    # redirected by a file that happens to be mounted beside the adapters.
    #
    # The file being asked at all is the fix. `runscan` and `runstashbox`
    # have always read it; this entry point -- the one the container runs --
    # did not, so an operator with a complete config directory was told no
    # media server was configured while the fifth file in the directory the
    # start-up line had just reported sat there unread.
    #
    # `base_url` is bound in every branch: the SAME address already resolved
    # for `stash`, reused for every row's own link to the media server. Never
    # re-derived from `stash.url`, which has `/graphql` appended for the API
    # client's own purposes (see `Stash.__init__`), and never a second piece
    # of configuration of its own -- one address, resolved once, used for both
    # jobs. Left as `args.server`, it would be `None` on exactly the installs
    # this change configures, and every link on the page would vanish.
    server_path = default_server_path(env)
    if args.server:
        stash = Stash(args.server, args.api_key)
        base_url = args.server
    elif os.path.exists(server_path):
        # Gated on the file EXISTING rather than on `load_server` succeeding,
        # because absent and unreadable are different states and only one of
        # them is a failure. `load_server` raises when nothing supplied both
        # halves -- see the rule in `cronicled.config`'s module docstring --
        # and an absent file is a legitimate fresh install that must still
        # start. So absence is decided HERE, and everything else is the
        # loader's own error, reaching the operator unswallowed. Catching it
        # and falling through to "none configured" would report a broken file
        # as a missing one, which is precisely the defect the adapter loader
        # above no longer has, one file over.
        #
        # `env=env` for the reason every other loader in this function takes
        # it: this is the seam where --config-dir either reaches the file or
        # silently does not.
        server = load_server(path=server_path, env=env)
        stash = Stash(server["url"], server["api_key"])
        base_url = server["url"]
        # WHERE the address came from, and never WHAT the key is. The api key
        # is a secret, and a start-up line naming it would put it in whatever
        # logs this process -- the same reason `Stash.stash_boxes` declines to
        # select a key it has no use for. The address is already on every row
        # of the page as a link; the key is on nothing, and must stay that way.
        print("media server: configured from %s" % server_path)
    else:
        stash = None
        base_url = None
        # Names all THREE sources now, the file included. Listing only the
        # flag and the variables is what sent an operator to check a file this
        # message did not admit to reading.
        print("WARNING: no media server configured. Browsing, dismissing and "
              "muting still work; Approve and Undo will refuse until a "
              "media server is set (--server / --api-key, or "
              "$CRONICLED_SERVER / $CRONICLED_API_KEY, or a url and api_key "
              "in %s -- see config/server.example.json for the shape)."
              % server_path)
    runner = JobRunner(store)
    actions = Actions(store, stash, runner=runner, adapters=adapters,
                      marker=marker)

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
                        and item["subject_type"] not in _OWN_SECTION_SUBJECTS],
                       base_url=base_url)

    def _scene_items(state):
        return [item for item in store.items(state=state)
                if item["subject_type"] not in _OWN_SECTION_SUBJECTS]

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

    def _reconcile_rows():
        """Every tag/performer reconciliation that still has something to show.

        The same three reads `_merge_rows` makes and for the same reason:
        `items()`'s default view hides a person's own rejections, and a
        dismissed row needs its Undismiss and a muted one its Unmute. The
        default view DOES include `applied` and `failed`, which matters more
        here than it does for a merge -- both of those states can carry an undo
        snapshot, and the Undo button is only reachable if the row is drawn.
        """
        seen = []
        for state in (None, "dismissed", "muted"):
            seen.extend(item for item in store.items(state=state)
                        if item["subject_type"]
                        == performer_tags.SUBJECT_TYPE)
        return to_reconcile_rows(seen, base_url=base_url)

    def _unused_groups():
        """Every low-count tag proposal that still has something to show, as
        one expandable group per population.

        The same three reads `_merge_rows` and `_reconcile_rows` make and for
        the same reason: `items()`'s default view hides a person's own
        rejections, and a dismissed row needs its Undismiss while a tag somebody
        kept needs the control that stops keeping it. `superseded` is absent --
        these rows offer no Refresh, so nothing puts one there.

        The GROUPING happens in `to_unused_groups`, not here: a library with a
        thousand of these must reach the page as two expandable rows, and the
        one rule that turns items into groups belongs beside the row builder
        rather than in the wiring.
        """
        seen = []
        for state in (None, "dismissed", "muted"):
            seen.extend(item for item in store.items(state=state)
                        if item["subject_type"] == tag_hygiene.SUBJECT_TYPE)
        return to_unused_groups(seen, base_url=base_url)

    scheduler = build_scheduler(runner, store, stash, adapters, env=env,
                                marker=marker, zone=zone)
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
              reconciles=_reconcile_rows,
              unused=_unused_groups,
              # `zone` here and on the schedule view below are the SAME object
              # the three appointments were declared in, threaded from the one
              # read in `main` -- never re-read here, which would be a second
              # chance for the page and the schedule to disagree.
              muted=lambda: to_mute_rows(
                  [m for m in store.mutes()
                   if m["subject_type"] not in _OWN_SECTION_SUBJECTS],
                  base_url=base_url, zone=zone),
              dismissed=lambda: to_rows(_scene_items("dismissed"),
                                        base_url=base_url),
              refused=lambda: to_refusal_rows(store.refusals(),
                                              base_url=base_url),
              superseded=lambda: to_rows(_scene_items("superseded"),
                                         base_url=base_url),
              # The subjects a scan found the media server no longer holds
              # (see `cronicled.scan.sweep_gone`). Read the way every other
              # terminal section is read -- by asking for that state
              # explicitly, because `items()`'s default view hides it -- so a
              # marked row keeps somewhere to be read rather than dropping out
              # of every list at once. Its section offers no control at all;
              # see the template for why.
              gone=lambda: to_rows(_scene_items(GONE), base_url=base_url),
              applied=lambda: to_rows(_scene_items("applied"),
                                      base_url=base_url),
              actions=actions, scan_status=actions.scan_status,
              # `None` when nothing is scheduled, which the page says out
              # loud rather than drawing as a healthy idle schedule.
              # `to_schedule_view` converts the loop's own timestamps -- and
              # the ones inside the reasons it reports -- into the configured
              # zone; the loop keeps recording them in UTC, which is what the
              # store compares against.
              schedule_status=(None if scheduler is None
                               else lambda: to_schedule_view(
                                   scheduler.status(), zone=zone)),
              # The landing page. Always wired, unlike `schedule_status`
              # above: an install with no schedule still runs scans by hand,
              # and "did the pass run, and what did it find" is the question
              # this page exists to answer whether or not anything is
              # unattended. The loop's status goes in RAW -- `to_summary_view`
              # converts it, so the run times and the schedule panel on that
              # page cannot end up in two different zones.
              summary=lambda: to_summary_view(
                  store.recent_runs(limit=SUMMARY_RUN_HISTORY),
                  waiting_counts(store.items()),
                  None if scheduler is None else scheduler.status(),
                  zone=zone),
              # The per-inbox routes (`/{inbox}`, `/{inbox}/{state}`) narrow
              # by subject type through `Store.items(subject_types=)`
              # directly -- see `web.app._serve_inbox_route` -- so they need
              # the store and the same `base_url` every other row's link is
              # built from, not a pre-built callable the way every section
              # above is wired.
              store=store, base_url=base_url,
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
