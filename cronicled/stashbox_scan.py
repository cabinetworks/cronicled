"""Check a batch of files against stash-box's own listing for their
resolved creator, as its own job.

`cronicled.stashbox.check` answers one question for one file: does this
performer's WHOLE stash-box listing contain no entry for it? Answering that
means paging a whole listing -- exactly the cost
`cronicled.jobs.COST_CLASS_LIMITS["box"]` exists to ration -- so this runs as
its own producer, under its own cost class, and never as part of the
site-adapter scan (`cronicled.scan.ScanProducer`, cost="scraping"). The two
must never be the same job: mixing them would spend a second rate-limited
service's budget under a job the "box" limit never counts, and would let
either one queue behind work that has nothing to do with it.

This producer never yields a proposal. It has nothing to propose -- no
candidate, no metadata to write, just a verdict about whether a source lists
a file at all -- so its whole output is what it logs: one line per file
checked, and a closing tally. `ctx.log`'s own limitation (the runner keeps
only the LAST message a job reports) means only the tally survives past the
run; the per-file lines are for whoever is watching it live, exactly as
`cronicled.scan.ScanProducer` already accepts for its own per-file lines.

`performer_ids` maps a resolved creator NAME to that performer's stash-box
id. This producer never builds that mapping itself -- a stash-box performer
is a different identity from a name resolved out of a folder or a filename,
and conflating the two is the sharp edge `cronicled.stashbox`'s own
docstring warns about. `cronicled.runstashbox.main` is what assembles it
before this producer ever runs, mostly from the media server's own
performer records (`cronicled.performer_ids.derive_performer_ids`), filled
out by an operator's own entries for whatever that cannot supply. An empty
or absent mapping means every file is skipped with a stated reason, not an
error: the same shape `cronicled.config.load_stashbox` already uses for
configuration whose absence is a legitimate state.

The creator for each file is resolved WITHOUT `owners_of` -- no site search
runs here, on purpose. `owners_of` is what lets `cronicled.artist.resolve`
check a contested candidate against a real catalogue, and that catalogue
read is `scraping`-classed cost the same as everything else a site adapter
does. Paying it from inside a `box`-classed job would be exactly the
cost-class mixing this module exists to avoid, so a file whose folder and
filename disagree resolves here on the plain folder-wins default -- the same
default `cronicled.scan.examine` itself falls back to whenever no
`owners_of` was given. Its own `competing` field still reports the
disagreement, so `cronicled.stashbox.check` still downgrades to the weaker
wording exactly as it would for any other contested file.

`marker`, when given, pools organized scenes carrying that tag alongside the
unorganized set -- exactly the population `cronicled.scan.ScanProducer`'s own
`marker` reaches, read through the same `cronicled.scan.pool_scenes` that
producer uses, rather than a second copy of that selection. `check` (see
`cronicled.stashbox`'s own docstring) never reads what a file currently
claims to be, so a scene an earlier tool organized on a guess is exactly as
good a subject for a second, source-drawn opinion as any other file -- more
so, in fact: nothing has ever checked it against an index at all.

The honest cost needs stating plainly, because it is easy to overstate in
the OTHER direction: THIS PRODUCER DOES NOT CACHE A LISTING READ ACROSS
FILES. `cronicled.stashbox.check` pages a whole listing per CALL, and
`produce` calls it once per selected file, so two files that resolve to the
same creator page that creator's listing TWICE, not once -- there is no
per-run memo here the way `cronicled.scan._SingleFlight` gives the scan for
its own per-store searches. `produce` still reports how many distinct
creators the marked population resolves to (`_resolved_creators`), because
that is the number worth knowing before ever turning a marker on here -- but
until a cache exists, the listing reads a run actually spends are bounded by
how many SELECTED files have a known performer id, not by that
distinct-creator count, whenever more than one selected file shares a
creator. Reported as a measurement to weigh, not as a claim about what this
already costs.
"""
import posixpath

from cronicled.artist import Aliases, creator_folder, resolve
from cronicled.scan import pool_scenes, select
from cronicled.scoring import DEFAULT_THRESHOLD
from cronicled.stashbox import check as stashbox_check


def _first_path(scene):
    """The path this producer judges the scene by -- the same "first file of
    a scene" rule `cronicled.scan._primary_path` states, kept as its own
    small copy rather than importing that name: `_primary_path` is a private
    helper of a module about scoring one file against a site's catalogue,
    and this module scores nothing against one -- reaching into its private
    surface to borrow four lines would couple two modules that otherwise
    share nothing.
    """
    files = scene.get("files") or []
    if not files:
        raise ValueError(
            "scene %r has no file to identify it by" % (scene.get("id"),))
    return files[0]["path"]


def _resolved_creators(scenes, aliases):
    """The distinct creator names `scenes` resolve to, on the same plain
    folder-wins default `StashBoxCheckProducer.produce`'s own loop uses.

    No catalogue is consulted -- `resolve` with no `owners_of` is a pure
    function of a name and a folder -- so this spends no lookup and costs
    nothing to compute, which is the whole point: it exists to tell an
    operator what enabling a marker would cost BEFORE any listing is read,
    including at `limit=0`, where `produce`'s own loop never runs at all.

    A scene this cannot even read a path from is skipped, the same way
    `produce`'s own loop isolates a malformed scene from the rest of a
    batch -- one unreadable entry says nothing about how many creators are
    behind everything else.
    """
    creators = set()
    for scene in scenes:
        try:
            path = _first_path(scene)
        except ValueError:
            continue
        directory = creator_folder(path)
        name = posixpath.basename(path)
        resolution = resolve(name, directory, aliases)
        if resolution.name is not None:
            creators.add(resolution.name)
    return creators


class StashBoxCheckProducer:
    """Ask stash-box, for every selected file, whether its resolved
    creator's listing rules it out -- see `cronicled.stashbox.check`.

    This is a second, independent opinion, drawn from a different index than
    whatever a site adapter's own scan found: it is checked whether or not
    that scan found, refused, or never looked at the same file, and nothing
    here reads a stored proposal or a prior scan's outcome to decide whether
    to run.
    """

    name = "stashbox-check"
    # Paging a whole stash-box listing is the resource this cost class
    # rations -- see this module's own docstring for why it must never share
    # a job with `cronicled.scan.ScanProducer`'s `"scraping"`.
    cost = "box"

    def __init__(self, stash, box, performer_ids, *, store, folder="library",
                limit=None, name_filter=None, threshold=DEFAULT_THRESHOLD,
                aliases=None, censorship=None, marker=None):
        self._stash = stash
        self._box = box
        # A plain, possibly-empty mapping -- not `Aliases`, which validates
        # and normalises operator-typed folder names. A stash-box id is
        # looked up by the EXACT resolved name `cronicled.artist.resolve`
        # already produced, not by anything a person typed freehand, so
        # there is no normalisation question here to get wrong.
        self._performer_ids = dict(performer_ids or {})
        self._store = store
        self._folder = folder
        self._limit = limit
        self._name_filter = name_filter
        self._threshold = threshold
        self._censorship = censorship or {}
        self._aliases = aliases if isinstance(aliases, Aliases) else Aliases(aliases)
        # The tag NAME, kept exactly as `cronicled.scan.ScanProducer` keeps
        # its own -- read against the server inside `produce`, not here, for
        # the same reason: ids are installation-specific, and a run built at
        # start-up and started later must ask about the tag as it stands at
        # each run. There is deliberately no separate configuration key for
        # this -- see this module's own docstring -- so a caller wires this
        # from the SAME setting `cronicled.config.load_marker_tag` reads for
        # the scan, never a second one of its own.
        self._marker = marker

    def produce(self, ctx):
        """Yield nothing, ever -- see this module's own docstring for why:
        there is no candidate here for a person to approve, only a verdict
        about whether a source lists a file at all, and the whole of that
        goes to `ctx.log`.
        """
        scenes, marked = pool_scenes(self._stash, self._marker)
        selected, counts = select(
            scenes, store=self._store, folder=self._folder,
            name_filter=self._name_filter, limit=self._limit, marked=marked)
        selection = ("selected %d of %d files for a stash-box check" %
                    (len(selected), counts.total))
        # Appended only when a marker is actually configured -- the same rule
        # `cronicled.scan.ScanProducer.produce` follows for its own line --
        # so an absent clause means "no marker was configured" rather than
        # "the marker matched nothing". The distinct-creator count is
        # computed over the WHOLE marked population, not just what the limit
        # let through, so it stays meaningful even at `limit=0`: this pages a
        # listing per resolved creator, and that count is what the read
        # actually costs -- see `_resolved_creators` and this module's own
        # docstring.
        if self._marker is not None:
            marked_scenes = [scene for scene in scenes
                             if str(scene.get("id")) in marked]
            creators = _resolved_creators(marked_scenes, self._aliases)
            selection += (
                "; %d of the %d offered only because they carry the marker "
                "tag %r, resolving to %d distinct creator(s)"
                % (counts.marked, counts.total, self._marker, len(creators)))
        ctx.log(selection)

        checked = unlisted = present = inconclusive = skipped = 0
        for done, scene in enumerate(selected, start=1):
            subject_id = str(scene.get("id"))
            try:
                path = _first_path(scene)
                directory = creator_folder(path)
                name = posixpath.basename(path)
                # No `owners_of`: see this module's docstring for why a
                # contested folder/filename pair resolves on the plain
                # folder-wins default here rather than paying for a site
                # search this job's cost class does not cover.
                resolution = resolve(name, directory, self._aliases)
                if resolution.name is None:
                    skipped += 1
                    ctx.log("%d/%d %s: no creator resolved, nothing to check "
                           "against stash-box" % (done, len(selected), subject_id))
                    continue
                performer_id = self._performer_ids.get(resolution.name)
                if performer_id is None:
                    skipped += 1
                    ctx.log("%d/%d %s: no stash-box performer id is known for "
                           "%r" % (done, len(selected), subject_id, resolution.name))
                    continue
                verdict = stashbox_check(
                    self._box, performer_id, name, directory, resolution,
                    threshold=self._threshold, censorship=self._censorship)
            except Exception as exc:
                # A malformed scene or a transport failure is evidence about
                # THIS file or THIS request, not about every other file in
                # the batch -- isolated the same way
                # `cronicled.scan.ScanProducer._examine` isolates one file's
                # exception from the rest of a run.
                skipped += 1
                ctx.log("%d/%d %s: %s: %s" % (
                    done, len(selected), subject_id, type(exc).__name__, exc))
                continue
            checked += 1
            if verdict.unlisted is True:
                unlisted += 1
            elif verdict.unlisted is False:
                present += 1
            else:
                inconclusive += 1
            ctx.log("%d/%d %s: %s" % (done, len(selected), subject_id, verdict.reason))
        ctx.log("finished: checked %d, %d unlisted, %d present, %d inconclusive, "
               "%d skipped" % (checked, unlisted, present, inconclusive, skipped))
        # Yields nothing, from nothing -- see the class docstring and
        # `cronicled.jobs`'s own requirement that `produce` always be a
        # generator, even one with nothing to hand the runner.
        yield from ()
