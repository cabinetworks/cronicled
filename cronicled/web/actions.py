"""What a person can do from the inbox: four things to one proposal, and one
thing that starts a scan of the whole library.

Separated from request handling so the write paths can be tested without a
socket, and so the handler cannot reach the store, the media server, or the
job runner directly and grow another kind of write nobody reviewed.
"""
import uuid

from cronicled.runscan import build_producer
from cronicled.web.rows import carries_cover


class UnknownProposal(KeyError):
    """No proposal with that fingerprint. Raised rather than ignored: a
    no-op here is indistinguishable from a success, and the person is
    watching a page that will redraw either way."""


class ApplyFailed(RuntimeError):
    """The write to the media server failed. Raised rather than returned:
    a caller that discards a return value discards it silently, and a
    failed apply must not look, to the person watching the page, like a
    successful one. The proposal is already recorded as failed in the
    store before this is raised."""


_NO_STASH = ("no media server is configured -- start cronicled with "
             "--server (and --api-key, if the server requires one) to "
             "enable Approve and Undo")

# What `undo` reports when the proposal it just reverted had carried a cover
# image. Not "reverted" alone: `Stash.revert_scene` restores exactly what
# `prior` describes, and `prior` cannot describe a scene's cover (see
# `Stash.apply_scene`'s docstring) -- so a plain "reverted" here would tell
# whoever reads it that this call undid everything the approve wrote, which
# is false for exactly this field, every time.
_COVER_NOT_RESTORED = ("reverted -- except the cover image, which cannot be "
                       "restored (Stash.apply_scene's undo snapshot has no "
                       "way to represent a scene's prior cover)")


class Actions:
    def __init__(self, store, stash, runner=None, adapter=None):
        self._store = store
        # `stash` is None when the entry point was started without a
        # configured media server -- see cronicled/__main__.py. Every method
        # here that would otherwise write to it checks that explicitly and
        # raises a message naming what is missing, rather than falling
        # through to an AttributeError on `None`.
        self._stash = stash
        # `runner`/`adapter` are None on exactly the same terms as `stash`:
        # a fresh install, or one with no site adapter configured yet
        # (`adapters.json` -- see `cronicled.adapters.registry`). Only
        # `scan` needs either, and it checks and refuses explicitly, the
        # same shape `approve`/`undo` already use for a missing `stash`.
        self._runner = runner
        self._adapter = adapter

    def _find(self, fp):
        # A single call, not one per state: `items(state=None)` already
        # returns everything except `dismissed`/`muted` -- which includes
        # `applied` -- so a second pass asking for `state="applied"`
        # explicitly could only ever re-find what the first pass already
        # covered. See `Store.items`.
        for item in self._store.items(state=None):
            if item["fingerprint"] == fp:
                return item
        raise UnknownProposal(fp)

    def approve(self, fp):
        item = self._find(fp)
        subject_id = item["subject_id"]
        if self._stash is None:
            # Recorded as failed for the same reason a real apply failure is:
            # an applied row offers an undo, and this proposal was never
            # applied at all.
            self._store.mark_failed(fp, _NO_STASH)
            raise ApplyFailed("could not apply: %s" % _NO_STASH)
        try:
            # The snapshot is produced INSIDE apply_scene, which reads the
            # scene immediately before the single write and returns nothing
            # at all if it raises first. Taking it here instead would open a
            # window between the read and the write.
            result = self._stash.apply_scene(
                subject_id, item["payload"]["candidate"])
        except Exception as exc:
            # Recorded as failed, never as applied: an applied row offers an
            # undo, and an undo of a write that never happened would restore
            # a snapshot describing nothing.
            self._store.mark_failed(fp, "%s: %s" % (type(exc).__name__, exc))
            # Raised, not returned: a caller that discards a return value
            # (as the HTTP handler did before this was a raise) must not be
            # able to answer as though the write succeeded.
            raise ApplyFailed("could not apply: %s" % exc) from exc
        self._store.mark_applied(fp, prior_state=result.get("prior"))
        return "applied"

    def undo(self, fp):
        """Revert one applied proposal to the state its stored snapshot
        describes, and report the outcome.

        `Stash.revert_scene` restores every field `prior` holds -- but
        `prior` cannot hold a scene's cover, because `apply_scene`'s
        snapshot has no representation for it (see that method's
        docstring). So when THIS proposal's own candidate carried a cover
        image, whatever `apply_scene` wrote as a cover is not, and cannot
        be, touched by this call: the return value says so explicitly
        rather than answering the bare "reverted" a caller would read as a
        complete reversal. The check is against the proposal's candidate,
        not the snapshot -- the snapshot has nothing to say about the
        cover either way, which is exactly the gap being reported.
        """
        item = self._find(fp)
        if self._stash is None:
            raise RuntimeError("cannot undo %s: %s" % (fp, _NO_STASH))
        prior = item.get("prior_state")
        if not prior:
            # Checked here so the refusal names the proposal. Left to
            # revert_scene it is still refused, but as a stack trace.
            raise ValueError(
                "cannot undo %s: no snapshot was stored for it" % fp)
        self._stash.revert_scene(item["subject_id"], prior)
        # Recorded only AFTER the revert succeeds. Marking first and then
        # raising would leave a row claiming the write was taken back while it
        # is still applied on the server -- the same ordering `approve`
        # follows, for the same reason.
        self._store.mark_reverted(fp)
        if carries_cover(item["payload"]["candidate"]):
            return _COVER_NOT_RESTORED
        return "reverted"

    def dismiss(self, fp):
        self._store.dismiss(self._find(fp)["fingerprint"],
                            reason="dismissed from the inbox")
        return "dismissed"

    def mute(self, fp):
        item = self._find(fp)
        self._store.mute(item["subject_type"], item["subject_id"],
                         reason="muted from the inbox")
        return "muted"

    def scan(self, limit):
        """Start a library scan against the configured adapter and return
        the started job.

        `limit` is passed straight through to `cronicled.runscan.build_producer`,
        which is where it is actually enforced as required -- this method
        adds no permissive default of its own, so a caller (the web handler)
        that forgot to supply one gets the same refusal the CLI's `--limit`
        gives, not a silent unlimited scan.

        Raises when there is nothing to scan against, on the same terms
        `approve`/`undo` already refuse a missing `stash`: no adapter
        configured (a fresh install with `adapters.json` never set up), or
        no media server configured. Also raises `cronicled.jobs.JobRejected`
        -- unmodified, straight from the runner -- when a scan is already
        running; the caller must not swallow that and answer as though this
        one started too.
        """
        if self._runner is None or self._adapter is None:
            raise RuntimeError(
                "no site adapter is configured -- a scan needs one to "
                "search against; set up adapters.json (see "
                "config/adapters.example.json for the shape), then try "
                "again")
        if self._stash is None:
            raise RuntimeError(_NO_STASH)
        producer = build_producer(self._stash, self._adapter, self._store,
                                  limit=limit)
        # `ScanProducer.name` is a fixed class attribute ("library-scan"),
        # and `JobRunner.register` refuses a second producer under a name
        # already registered -- reusing that name across scans would let
        # exactly one scan ever run per process. A fresh name per call sidesteps
        # that at a real, accepted cost: the runner's producer registry (unlike
        # its bounded job history) has no eviction, so one small object per
        # scan ever started through this control stays live for the life of
        # the process. See the report for this ticket.
        producer.name = "library-scan-%s" % uuid.uuid4()
        self._runner.register(producer)
        return self._runner.start(producer.name)

    def scan_status(self):
        """The most recently started scan the runner still holds, or None
        if no scan has ever run here (or scanning is not configured).

        `JobRunner.jobs()` documents its own order as oldest-start-first, so
        the last element is the most recently started job without this
        needing to re-derive "most recent" from `started_at` itself -- and
        every job this runner ever holds is one this same control started;
        `cronicled.__main__` wires no scheduler that could add another kind.
        """
        if self._runner is None:
            return None
        jobs = self._runner.jobs()
        return jobs[-1] if jobs else None
