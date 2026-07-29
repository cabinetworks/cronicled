"""What a person can do from the inbox: approve, dismiss, mute, undo or
refresh a proposal, reverse a dismissal or a mute, and start a scan of the
whole library.

Separated from request handling so the write paths can be tested without a
socket, and so the handler cannot reach the store, the media server, or the
job runner directly and grow another kind of write nobody reviewed.
"""
from cronicled import tags
from cronicled.descriptions import SUBJECT_TYPE as DESCRIPTION_SUBJECT
from cronicled.runscan import build_producer
from cronicled.web.rows import carries_cover


class UnknownProposal(KeyError):
    """No proposal with that fingerprint — or, for `unmute`, no standing mute
    matching the subject asked about. Raised rather than ignored: a no-op
    here is indistinguishable from a success, and the person is watching a
    page that will redraw either way."""


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
    def __init__(self, store, stash, runner=None, adapters=None):
        self._store = store
        # `stash` is None when the entry point was started without a
        # configured media server -- see cronicled/__main__.py. Every method
        # here that would otherwise write to it checks that explicitly and
        # raises a message naming what is missing, rather than falling
        # through to an AttributeError on `None`.
        self._stash = stash
        # `runner`/`adapters` are None (or empty) on exactly the same terms
        # as `stash`: a fresh install, or one with no site adapter
        # configured yet (`adapters.json` -- see
        # `cronicled.adapters.registry`). Only `scan` needs either, and it
        # checks and refuses explicitly, the same shape `approve`/`undo`
        # already use for a missing `stash`.
        #
        # `adapters` is the WHOLE configured mapping (name -> `SiteAdapter`)
        # -- every store this scan searches, never one singled out. See
        # `cronicled.runscan.build_producer` for why a scan searches all of
        # them rather than one chosen adapter.
        self._runner = runner
        self._adapters = adapters

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
        if item["subject_type"] == tags.SUBJECT_TYPE:
            return self._approve_merge(fp, item)
        subject_id = item["subject_id"]
        if self._stash is None:
            # Recorded as failed for the same reason a real apply failure is:
            # an applied row offers an undo, and this proposal was never
            # applied at all.
            self._store.mark_failed(fp, _NO_STASH)
            raise ApplyFailed("could not apply: %s" % _NO_STASH)
        try:
            # The snapshot is produced INSIDE the apply, which reads the
            # subject immediately before the single write and returns nothing
            # at all if it raises first. Taking it here instead would open a
            # window between the read and the write.
            #
            # Dispatched on the row's own `subject_type`, the same field the
            # store keys a mute by, rather than on the shape of its payload:
            # a proposal about a performer's description and one about a
            # scene are two different writes to two different endpoints, and
            # asking "does this payload have a candidate" would guess at
            # which from a field that a malformed payload could be missing
            # for a different reason entirely.
            if item["subject_type"] == DESCRIPTION_SUBJECT:
                payload = item["payload"]
                result = self._stash.apply_performer_description(
                    subject_id, payload["cleaned"],
                    expected=payload["original"])
            else:
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

    def _approve_merge(self, fp, item):
        """Perform one tag merge: move every item off the losing spellings
        onto the surviving one and delete them.

        Refuses an UNDECIDED cluster outright, and does so before touching
        the store or the media server. A cluster of three spellings, or of
        two that carry no evidence about which was meant, is a finding: there
        is no canonical name in the payload, so there is nothing to merge
        into and nothing to invent one from. The row never offers Approve for
        such a proposal (`MergeRow.appliable`), so reaching here means a
        request that did not come from the page -- refused with a reason
        rather than resolved by picking a member.

        Nothing is recorded for that refusal, unlike the `_NO_STASH` case
        below. A missing media server is a real attempt at a write that
        failed, and an applied-or-failed row is the honest record of it; an
        undecided cluster was never a write that could be attempted, and
        marking it `failed` would put a resolution on a proposal that is
        still exactly as open as it was.

        `Stash.merge_tags` is called WITHOUT `aliases`. Its `aliases`
        argument replaces the destination's whole alias list, and the only
        list available here is whatever the proposal captured when it was
        made -- days ago, potentially -- so passing it would silently delete
        any alias added since. See `cronicled.tags`'s module docstring.

        `mark_applied` is called with NO `prior_state`, and that is where
        this module enforces the irreversibility decision rather than merely
        describing it: there is no snapshot, so the store holds none, so no
        row can ever claim an undo it cannot perform.
        """
        payload = item["payload"]
        canonical = payload["canonical"]
        if canonical is None:
            raise ValueError(
                "cannot merge %s: this cluster has no agreed surviving "
                "spelling (%s). Nothing here may pick one for you."
                % (payload["key"], payload["undecided"]))
        if self._stash is None:
            self._store.mark_failed(fp, _NO_STASH)
            raise ApplyFailed("could not apply: %s" % _NO_STASH)
        sources = [m["id"] for m in payload["members"]
                   if m["id"] != canonical["id"]]
        try:
            self._stash.merge_tags(canonical["id"], sources)
        except Exception as exc:
            self._store.mark_failed(fp, "%s: %s" % (type(exc).__name__, exc))
            raise ApplyFailed("could not apply: %s" % exc) from exc
        self._store.mark_applied(fp)
        return "merged"

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
        if item["subject_type"] == tags.SUBJECT_TYPE:
            # Refused with the REASON, not with the generic "no snapshot was
            # stored" the check below would otherwise give. A merge has no
            # snapshot because none can exist (see
            # `cronicled.tags.MERGE_IS_IRREVERSIBLE`), and "no snapshot was
            # stored for it" reads as an omission somebody could go and fix.
            # The page never offers Undo on a merge row -- `MergeRow` has no
            # `undoable` field at all -- so this answers a request that did
            # not come from the page.
            raise ValueError(
                "cannot undo %s: %s" % (fp, tags.MERGE_IS_IRREVERSIBLE))
        if self._stash is None:
            raise RuntimeError("cannot undo %s: %s" % (fp, _NO_STASH))
        prior = item.get("prior_state")
        if not prior:
            # Checked here so the refusal names the proposal. Left to
            # revert_scene it is still refused, but as a stack trace.
            raise ValueError(
                "cannot undo %s: no snapshot was stored for it" % fp)
        if item["subject_type"] == DESCRIPTION_SUBJECT:
            self._stash.revert_performer_description(item["subject_id"], prior)
            # Recorded only AFTER the revert succeeds, exactly as below.
            self._store.mark_reverted(fp)
            # A plain "reverted", with no caveat, and that is a claim this
            # one can actually make: the apply wrote a single field and the
            # snapshot holds that same field, so there is nothing the revert
            # leaves behind. The cover-image caveat below is about a scene
            # apply writing something its snapshot has no way to represent,
            # and reaching for `carries_cover` here would raise on a payload
            # that has no candidate at all.
            return "reverted"
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

    def refresh(self, fp):
        """Retire the proposal named by `fp` as superseded -- not wrong, out
        of date -- and free its subject for the NEXT scan to examine again.

        This is the per-row control ticket 86 asks for: a proposal made by an
        older, thinner version of this tool can carry only a title and a URL,
        and `cronicled.scan.select` skips a file whose subject already has a
        proposal before ever looking at it again, so that thin proposal
        blocks its file forever, and an `applied` or `failed` row has no
        other path off that block at all (dismissing only ever frees a `new`
        row -- see `Store.dismiss`'s and `Store.supersede`'s own docstrings).

        Deliberately NOT `dismiss`: the person is not saying this proposal
        was wrong, so recording it as a rejection would put a decision in the
        store they never made. `Store.supersede` is its own action for
        exactly that reason -- see its docstring for what it does to an
        `applied` row's state and undo snapshot (nothing) and how
        `cronicled.scan.select` learns a subject is free again.

        Like `dismiss`/`mute`/`undo`, raises `UnknownProposal` for a
        fingerprint that is not currently visible -- a doubled click on an
        already-superseded (and so hidden) row must not look like a second
        success.
        """
        item = self._find(fp)
        self._store.supersede(item["fingerprint"])
        return "refreshed"

    def undismiss(self, fp):
        """Reverse a dismissal: the proposal named by `fp` comes back into
        the inbox, and the standing dismissal that was blocking it is lifted.

        Checked against `items(state="dismissed")`, not `_find` — `_find`
        searches only the VISIBLE set (`items(state=None)`), which by
        definition never contains a dismissed row, so it could never find
        the very thing this action exists to reverse. Raising
        `UnknownProposal` when no currently-dismissed row matches `fp` is the
        same "a doubled click must not look like a second success" reasoning
        `approve`/`dismiss`/`mute`/`undo` already apply to a stale
        fingerprint.
        """
        match = next((item for item in self._store.items(state="dismissed")
                     if item["fingerprint"] == fp), None)
        if match is None:
            raise UnknownProposal(fp)
        self._store.undismiss(fp)
        return "undismissed"

    def unmute(self, subject_type, subject_id):
        """Reverse a mute on `subject_type`/`subject_id`: the subject becomes
        eligible for the NEXT scan, and nothing else. No lookup and no scan
        happen here — `Store.unmute` only ever removes the standing block;
        whatever a future scan finds for this subject is that scan's own
        decision, made later, only when a person explicitly presses Scan
        (see `scan` below and `cronicled.scan.select`).

        Checked against `muted_subjects()` first and raised as
        `UnknownProposal` when the subject is not currently muted, the same
        "doubled click" reasoning `undismiss` applies above.
        """
        if (subject_type, str(subject_id)) not in self._store.muted_subjects():
            raise UnknownProposal((subject_type, subject_id))
        self._store.unmute(subject_type, subject_id)
        return "unmuted"

    def scan(self, limit):
        """Start a library scan against EVERY configured adapter and return
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
        if self._runner is None or not self._adapters:
            raise RuntimeError(
                "no site adapter is configured -- a scan needs at least "
                "one to search against; set up adapters.json (see "
                "config/adapters.example.json for the shape), then try "
                "again")
        if self._stash is None:
            raise RuntimeError(_NO_STASH)
        producer = build_producer(self._stash, self._adapters, self._store,
                                  limit=limit)
        # A fresh `ScanProducer` is built above for every call, because
        # `limit` has to be enforced at construction (see `build_producer`'s
        # own docstring) -- there is no later point to hand it in. `register`
        # refuses a second producer under a name already registered, so
        # reusing `ScanProducer.name` ("library-scan") as-is across scans
        # would let exactly one scan ever run per process.
        #
        # `reregister` is the deliberate escape hatch for exactly this: it
        # replaces whatever is currently registered under the name rather
        # than refusing, so every scan this control starts is filed under
        # one stable, recognisable name instead of a fresh one invented per
        # click that stayed in the registry forever (the registry, unlike
        # the bounded job history beside it, has no eviction of its own). A
        # scan already running under the previous object is untouched by the
        # swap -- see `JobRunner.reregister`'s own docstring -- and `start`
        # below still refuses with `JobRejected` in that case, on the same
        # terms as before.
        self._runner.reregister(producer)
        return self._runner.start(producer.name)

    def scan_status(self):
        """The most recently started scan the runner still holds, or None
        if no scan has ever run here (or scanning is not configured).

        `JobRunner.jobs()` documents its own order as oldest-start-first, so
        the last element is the most recently started job without this
        needing to re-derive "most recent" from `started_at` itself.

        It is NOT necessarily a scan this control started. `cronicled.__main__`
        registers a scheduled scan of its own and a background loop starts it
        on a cadence, so the most recent job may be that one -- which is the
        right answer to "what is the scan doing", and the reason this reports
        the job rather than filtering by which control began it. The job
        carries its own `producer` name, so the two are told apart by
        whatever reads this rather than by hiding one of them.
        """
        if self._runner is None:
            return None
        jobs = self._runner.jobs()
        return jobs[-1] if jobs else None
