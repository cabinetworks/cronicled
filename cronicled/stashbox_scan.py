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

`produce` now memoises a listing read for the length of one run:
`cronicled.stashbox.check` still pages a whole listing per CALL, but two
selected files that resolve to the same creator now share ONE such call
between them, through `_CachedListings` -- reusing
`cronicled.scan._SingleFlight`, the scan's own per-run collapse for its own
per-store searches, rather than a second hand-rolled cache that would be
free to drift from it.

This is not only a cost saving, and the cost is not the stronger reason for
it. A listing read is PAGED, so reading it twice can return two different
views of the same source if it is updated in between -- and, before this,
two files by one creator really were read separately, so they could be
judged against two different views of what is nominally "the same"
listing, with nothing anywhere saying so. The memo removes that
inconsistency along with the redundant reads: every file that shares a
creator within one run is now judged against the identical `SourceListing`
object, not merely an equal-looking one.

Built fresh inside `produce`, never held on `self`, on the same terms
`cronicled.scan.ScanProducer`'s own per-store flights are: the cache must
not outlive the run, or a later run would judge a file against a listing
the source may no longer hold. A failed read is cached too, and re-raised
to every file waiting on it, exactly as `_SingleFlight` already does for
the scan -- a transient failure is one fact about the run, never a licence
to hand back an empty listing, and therefore a confident "unlisted", to
every file that happens to share its creator.

`produce` still reports how many distinct creators the marked population
resolves to (`_resolved_creators`), and that number now means something
closer to its plain reading than it used to. Before the memo, it was
worth knowing in spite of being decoupled from what a run actually spent,
because nothing here reused an answer across files. Now, the listing reads
a run actually spends really are bounded by how many DISTINCT resolved
creators its selected files carry a known performer id for, not by how
many selected files there are. `_resolved_creators` still only counts the
population the marker itself contributed, though, so it undercounts the
true bound whenever a marked file shares a creator with an unmarked one --
that pair now costs one read rather than two, which this count has no way
to know to subtract.
"""
import posixpath

from cronicled.artist import Aliases, creator_folder, resolve
from cronicled.scan import _SingleFlight, pool_scenes, select
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


class _CachedListings:
    """A `box`-shaped facade whose `performer_listing` answers a whole
    listing read AT MOST ONCE per performer id for the life of one run,
    reusing `cronicled.scan._SingleFlight` -- the scan's own per-run
    collapse for its per-store searches -- rather than a second, hand-rolled
    cache that would be free to behave differently from it.

    An instance is built FRESH inside `StashBoxCheckProducer.produce`, never
    held on `self`: the same scoping rule `cronicled.scan.ScanProducer.produce`
    applies to its own per-store flights, for the identical reason -- a memo
    that outlives a run answers a LATER run from a listing the source may no
    longer hold, which is exactly the staleness this exists to avoid, not
    merely the redundant reads.

    Keyed on the performer id ALONE (`key=lambda call: call[0]`, `call`
    being the `(performer_id, per_page, max_pages, timeout)` tuple
    `performer_listing` below packages up), never on `_SingleFlight`'s
    default case/whitespace-folding key: that folding is a judgement about
    two spellings of one person's NAME, and would be the wrong judgement
    applied to an opaque stash-box id, which is already exact.

    `wrap=lambda listing: listing` is the other departure from
    `_SingleFlight`'s default (`list`, which assumes a search returns an
    iterable of candidate rows and hands every caller a fresh copy of it). A
    whole-listing read returns ONE `SourceListing` object, not an iterable of
    rows, and every file that shares a creator sharing that SAME object --
    never a copy of one -- is the whole point: two files by one creator must
    be judged against the identical paged read, not against two reads of a
    listing that may have changed underneath between them.

    `per_page`/`max_pages`/`timeout` still reach the underlying `box`
    exactly as `cronicled.stashbox.check` passed them -- forwarded, never
    dropped, because the real client is entitled to see the same call shape
    whether or not a memo sits in front of it. Only the CACHE KEY leaves
    them out, keying on the performer id alone: this producer has no way to
    ask for two different ones within one run, so nothing is lost by not
    keying on them, and keying on them anyway would buy nothing.
    """

    def __init__(self, box):
        # Kept alongside the flight, rather than only closed over inside it,
        # so a caller (or a test) can confirm which box this wraps without
        # reaching through `_SingleFlight`'s own internals to find out.
        self._box = box
        self._flight = _SingleFlight(
            self._read, key=lambda call: call[0],
            wrap=lambda listing: listing)

    def _read(self, call):
        performer_id, per_page, max_pages, timeout = call
        return self._box.performer_listing(
            performer_id, per_page=per_page, max_pages=max_pages,
            timeout=timeout)

    def performer_listing(self, performer_id, per_page=None, max_pages=None,
                          timeout=None):
        return self._flight((performer_id, per_page, max_pages, timeout))


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

        # One `_CachedListings` for the whole run, built HERE rather than at
        # `__init__`, on the same terms `cronicled.scan.ScanProducer.produce`
        # builds its own per-store `_SingleFlight`s: the memo must not
        # outlive this one run, or a later run would judge a file against a
        # listing the source may no longer hold. See this module's own
        # docstring and `_CachedListings` for the whole reasoning.
        cached_box = _CachedListings(self._box)

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
                    cached_box, performer_id, name, directory, resolution,
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
