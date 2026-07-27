"""The four things a person can do to a proposal.

Separated from request handling so the write paths can be tested without a
socket, and so the handler cannot reach the store or the media server
directly and grow a fifth kind of write nobody reviewed.
"""


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


class Actions:
    def __init__(self, store, stash):
        self._store = store
        # `stash` is None when the entry point was started without a
        # configured media server -- see cronicled/__main__.py. Every method
        # here that would otherwise write to it checks that explicitly and
        # raises a message naming what is missing, rather than falling
        # through to an AttributeError on `None`.
        self._stash = stash

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
