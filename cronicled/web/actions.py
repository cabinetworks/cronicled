"""What a person can do from the inbox: approve, dismiss, mute, undo or
refresh a proposal, reverse a dismissal or a mute, and start a scan of the
whole library.

Separated from request handling so the write paths can be tested without a
socket, and so the handler cannot reach the store, the media server, or the
job runner directly and grow another kind of write nobody reviewed.
"""
from cronicled import performer_tags, tag_hygiene, tags
from cronicled.descriptions import SUBJECT_TYPE as DESCRIPTION_SUBJECT
from cronicled.tag_descriptions import SUBJECT_TYPE as TAG_DESCRIPTION_SUBJECT
from cronicled.runscan import build_producer
from cronicled.scan import catalogue_link
from cronicled.store import GONE
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


def _match_to_apply(payload):
    """The scene metadata to write, which is the proposal's candidate plus
    the catalogue link the payload stands for.

    Built HERE, at apply time, out of the two fields the payload already
    carries for a person to read (`endpoint` and `remote_site_id`) rather
    than stored a third time when the proposal was made. One fact, one
    representation: a stored copy of the pair could disagree with the fields
    beside it, and nothing would ever notice which of them was right.

    A link the candidate ALREADY carries is kept and the new one appended,
    never replaced. A site scraper does not return one today (see
    `scan.catalogue_link`), but if one ever does, dropping it here would
    silently discard a link the proposal was made with. `apply_scene` is the
    single authority on what happens when two entries name one endpoint.
    """
    candidate = payload["candidate"]
    link = catalogue_link(payload)
    if link is None:
        return candidate
    return dict(candidate,
                stash_ids=list(candidate.get("stash_ids") or ()) + [link])


class Actions:
    def __init__(self, store, stash, runner=None, adapters=None,
                 marker=None):
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
        # The provisionally-organized marker tag's NAME, read from this
        # install's own config by the entry point (see
        # `cronicled.config.load_marker_tag`) and handed on to every scan this
        # control starts. Carried rather than re-read here for the same
        # reason `adapters` is: `--config-dir` means only the entry point
        # knows which directory the config was loaded from, and a loader
        # called from this far in would read a different one, find nothing,
        # and leave a configured marker doing nothing at all on the one path
        # a person actually presses. That is not hypothetical -- an alias map
        # was configured and ignored on exactly this path, because this
        # method did not pass it either.
        self._marker = marker

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
        if item["subject_type"] == performer_tags.SUBJECT_TYPE:
            return self._approve_reconcile(fp, item)
        if item["subject_type"] == tag_hygiene.SUBJECT_TYPE:
            return self._approve_delete(fp, item)
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
            elif item["subject_type"] == TAG_DESCRIPTION_SUBJECT:
                payload = item["payload"]
                # `expected` is the payload's `original` VERBATIM -- the value
                # the server gave when the pass ran, `None` and all. The
                # proposal exists because the field was empty; a tag somebody
                # has described since is refused by the client rather than
                # having a third-party sentence written over it.
                result = self._stash.apply_tag_description(
                    subject_id, payload["description"],
                    expected=payload["original"])
            else:
                result = self._stash.apply_scene(
                    subject_id, _match_to_apply(item["payload"]))
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

        A DESCRIPTION, unlike the aliases, IS passed -- and the difference is
        not inconsistency. The alias list is REPLACED by what is sent, so a
        stale list deletes aliases added since; a description is carried onto
        a survivor the proposal recorded as having NONE, and `Stash
        .merge_tags` re-reads the survivor one line before the write and
        refuses the whole merge if it has gained one. One is a write whose
        staleness cannot be checked, the other is a write whose staleness is
        exactly what is checked. Without it the merge deletes the only tag
        carrying the description and nothing records what it said.

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
        # `.get` on both, and only on these two. A merge proposal recorded
        # before this pass knew about descriptions carries no block at all,
        # and the truthful reading of one is "nothing to carry" -- the merge
        # it already was. Everything above is indexed, because for everything
        # above a missing value is a malformed payload.
        #
        # `text` is `None` for every merge with nothing to carry, including
        # one whose spellings describe the tag two DIFFERENT ways: that is
        # reported on the row and left to a person. Passing the survivor's
        # own text, or the longer, or the first, would be this project's
        # oldest mistake made in a place a reviewer could not see it.
        inherit = (payload.get("description") or {}).get("text")
        try:
            self._stash.merge_tags(canonical["id"], sources,
                                   description=inherit)
        except Exception as exc:
            self._store.mark_failed(fp, "%s: %s" % (type(exc).__name__, exc))
            raise ApplyFailed("could not apply: %s" % exc) from exc
        self._store.mark_applied(fp)
        return "merged"

    def _approve_delete(self, fp, item):
        """Delete one tag that classifies nought or one scene.

        ONE tag, the row's own subject, and there is no path here that takes a
        set. That is the containment this whole finding rests on: the population
        is assembled by a rule (`cronicled.tag_hygiene`), and a rule nobody
        checked applied to two hundred and fifty-six irreversible writes on one
        click is the shape this deliberately does not offer. The page groups the
        rows so they can be read; it never groups the buttons.

        The recorded count is passed to `Stash.delete_tag` as
        `expected_scene_count` and the client refuses the deletion if the tag's
        count has moved since the pass ran. Indexed, never `.get`: a payload
        without it is malformed, and `.get` would hand `None` to a comparison
        that then fails against every real count -- turning a missing field into
        a refusal that looks like the guard firing, which is the one way this
        check could stop meaning anything without anybody noticing.

        `mark_applied` is called with NO `prior_state`, and that is where this
        module enforces the irreversibility rather than merely describing it:
        there is no snapshot, so the store holds none, so no row can ever claim
        an undo it cannot perform. `undo` refuses this subject type by name, for
        the reason it refuses a merge.
        """
        if self._stash is None:
            self._store.mark_failed(fp, _NO_STASH)
            raise ApplyFailed("could not apply: %s" % _NO_STASH)
        try:
            self._stash.delete_tag(
                item["subject_id"],
                expected_scene_count=item["payload"]["scene_count"])
        except Exception as exc:
            self._store.mark_failed(fp, "%s: %s" % (type(exc).__name__, exc))
            raise ApplyFailed("could not apply: %s" % exc) from exc
        self._store.mark_applied(fp)
        return "deleted"

    def _approve_reconcile(self, fp, item):
        """Perform one tag/performer reconciliation: attach the performer to
        every scene carrying the tag and take the tag off those scenes.

        The TAG IS NOT DELETED. Nothing on this path can delete it -- see
        `cronicled.performer_tags` for why that is a separate decision, and
        `Stash.reconcile_tag_to_performer`, which is the only write this calls.

        Refuses an AMBIGUOUS proposal outright, before touching the store or
        the media server, exactly as `_approve_merge` refuses an undecided
        cluster: two performers answer to the tag's name, the payload names no
        one performer, and there is nothing here that may pick between them.
        Nothing is recorded for that refusal -- it was never a write that could
        be attempted, and marking it `failed` would put a resolution on a
        proposal that is still exactly as open as it was.

        Refuses a proposal that ALREADY carries a snapshot for the same reason
        the row withholds its Approve button in that case (see
        `web.rows.ReconcileRow`): a partly-applied reconciliation's snapshot is
        the only record of the scenes it changed, and a second attempt would
        overwrite it with one covering only the second batch. The undo is the
        way forward from there.

        A PARTIAL run is recorded as `failed` WITH the snapshot of what landed,
        which is the one shape the store can express for it: `mark_applied`
        writes the snapshot, `mark_failed` then writes the state and the
        reason, and `mark_failed` deliberately leaves `prior_state` alone. So
        the row says the apply failed, says why, and still offers the undo for
        the scenes that really did change. A run where NOTHING landed gets no
        snapshot at all -- there is nothing to undo, and a snapshot describing
        no scenes would offer a button that does nothing.
        """
        payload = item["payload"]
        performer = payload["performer"]
        if performer is None:
            raise ValueError(
                "cannot reconcile tag %s: %s. Nothing here may pick one for "
                "you." % (payload["tag"]["name"], payload["ambiguous"]))
        if item.get("prior_state"):
            raise ValueError(
                "cannot reconcile tag %s again: an earlier attempt already "
                "changed some scenes, and the record of which ones is the "
                "only way back. Undo it first, then approve it again."
                % (payload["tag"]["name"],))
        if self._stash is None:
            self._store.mark_failed(fp, _NO_STASH)
            raise ApplyFailed("could not apply: %s" % _NO_STASH)
        try:
            result = self._stash.reconcile_tag_to_performer(
                item["subject_id"], performer["id"])
        except Exception as exc:
            self._store.mark_failed(fp, "%s: %s" % (type(exc).__name__, exc))
            raise ApplyFailed("could not apply: %s" % exc) from exc
        prior = result["prior"]
        if result["failures"]:
            failure = result["failures"][0]
            reason = ("wrote %d of %d scenes and then stopped at scene %s (%s); "
                      "the scenes already written carry the performer and no "
                      "longer carry the tag, and Undo restores exactly those"
                      % (len(prior["untagged"]), len(result["worklist"]),
                         failure["scene"], failure["error"]))
            if prior["untagged"] or prior["attached"]:
                self._store.mark_applied(fp, prior_state=prior)
            self._store.mark_failed(fp, reason)
            raise ApplyFailed("could not apply: %s" % reason)
        self._store.mark_applied(fp, prior_state=prior)
        return "reconciled"

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

        A proposal whose SUBJECT has been marked gone is refused here, with the
        subject named, before anything else is considered. The write really did
        happen and the snapshot really does describe the scene as it was --
        there is simply nothing left to restore it onto, and `revert_scene`
        reached with that snapshot would fail against an id the server does not
        have. Checked ahead of `_find`, which searches only the visible set and
        so cannot see a gone row at all: left to it, the one outcome this exists
        to explain would arrive as `UnknownProposal`, which says a fingerprint
        is unknown when the truth is that the file is.
        """
        missing = next((item for item in self._store.items(state=GONE)
                        if item["fingerprint"] == fp), None)
        if missing is not None:
            # Named by subject and, when the payload carries one, by path --
            # the id is what the server knows the scene as and the path is what
            # a person recognises it by. `.get`, because only a scene proposal
            # has a path at all; a description payload has none and must not
            # turn this refusal into a KeyError.
            where = missing["payload"].get("path") if isinstance(
                missing["payload"], dict) else None
            raise ValueError(
                "cannot undo %s: %s %s%s is no longer on the media server, so "
                "there is nothing left to restore its snapshot onto"
                % (fp, missing["subject_type"], missing["subject_id"],
                   "" if where is None else " (%s)" % (where,)))
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
        if item["subject_type"] == tag_hygiene.SUBJECT_TYPE:
            # Refused with the REASON, for the reason a merge is: a deleted tag
            # has no snapshot because none can exist (see
            # `tag_hygiene.DELETE_WARNING`), and the generic "no snapshot was
            # stored for it" below reads as an omission somebody could go and
            # fix. `UnusedTagRow` carries no `undoable` field at all, so the
            # page never offers this and reaching here is a request that did not
            # come from it.
            raise ValueError(
                "cannot undo %s: %s" % (fp, tag_hygiene.DELETE_IS_IRREVERSIBLE))
        if self._stash is None:
            raise RuntimeError("cannot undo %s: %s" % (fp, _NO_STASH))
        prior = item.get("prior_state")
        if not prior:
            # Checked here so the refusal names the proposal. Left to
            # revert_scene it is still refused, but as a stack trace.
            raise ValueError(
                "cannot undo %s: no snapshot was stored for it" % fp)
        if item["subject_type"] == performer_tags.SUBJECT_TYPE:
            # The tag id is the row's own SUBJECT -- the same value the
            # approve wrote against -- and the client checks it against the
            # snapshot's own (see `Stash.revert_reconcile`), so a snapshot that
            # names another tag is caught before it writes that tag onto these
            # scenes.
            self._stash.revert_reconcile(item["subject_id"], prior)
            # Recorded only AFTER the revert returns, exactly as every branch
            # below. A revert that raised partway leaves the row `applied` with
            # its snapshot, and pressing Undo again finishes the job -- see
            # `revert_reconcile`, which is idempotent for that reason.
            self._store.mark_reverted(fp)
            return "reverted"
        if item["subject_type"] == TAG_DESCRIPTION_SUBJECT:
            self._stash.revert_tag_description(item["subject_id"], prior)
            # Recorded only AFTER the revert succeeds, and answered with a
            # plain "reverted" for the same reason the performer branch below
            # is: one field written, the same one field in the snapshot,
            # nothing the revert leaves behind.
            self._store.mark_reverted(fp)
            return "reverted"
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

        The configured marker tag travels with this control (see
        `__init__`), so a scan a person presses Scan for pools the
        provisionally-organized files a scheduled scan pools -- the two
        differ in their limit and their registration, never in what they can
        see.

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
                                  limit=limit, marker=self._marker)
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
        return self._runner.start(producer.name, trigger="manual")

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
