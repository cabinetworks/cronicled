import unittest

from cronicled.web.actions import Actions, UnknownProposal


class _FakeStash:
    def __init__(self, prior=None, fail=False):
        self.calls = []
        self._prior = prior if prior is not None else {"title": "old"}
        self._fail = fail

    def apply_scene(self, scene_id, match):
        self.calls.append(("apply", scene_id))
        if self._fail:
            raise RuntimeError("server said no")
        return {"prior": self._prior}

    def revert_scene(self, scene_id, prior):
        self.calls.append(("revert", scene_id, prior))
        return {}


class _FakeStore:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def items(self, folder=None, state=None, limit=None, offset=0):
        return [self.item] if self.item else []

    def mark_applied(self, fp, prior_state=None):
        self.calls.append(("applied", fp, prior_state))

    def mark_failed(self, fp, error):
        self.calls.append(("failed", fp, error))

    def dismiss(self, fp, reason=None):
        self.calls.append(("dismissed", fp, reason))

    def mute(self, subject_type, subject_id, reason=None):
        self.calls.append(("muted", subject_type, subject_id, reason))


def _item(**over):
    item = {"fingerprint": "fp-1", "state": "new", "subject_type": "scene",
            "subject_id": "42", "prior_state": None,
            "payload": {"path": "/l/a.mp4",
                        "creator": {"name": "N", "source": "folder",
                                    "competing": None,
                                    "rejected_folder": None},
                        "candidate": {"id": "c-1", "title": "T"},
                        "score": 0.81, "runners_up": []}}
    item.update(over)
    return item


class Approve(unittest.TestCase):
    def test_stores_the_snapshot_the_apply_returned(self):
        store, stash = _FakeStore(_item()), _FakeStash(prior={"title": "was"})
        Actions(store, stash).approve("fp-1")
        self.assertEqual(store.calls,
                         [("applied", "fp-1", {"title": "was"})])

    def test_a_failed_apply_is_recorded_as_failed_not_applied(self):
        # Marking it applied would offer an undo for a write that never
        # happened, and revert_scene would then restore a snapshot that does
        # not describe anything.
        store, stash = _FakeStore(_item()), _FakeStash(fail=True)
        Actions(store, stash).approve("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["failed"])

    def test_an_unknown_fingerprint_raises_rather_than_silently_doing_nothing(self):
        with self.assertRaises(UnknownProposal):
            Actions(_FakeStore(None), _FakeStash()).approve("fp-nope")


class Undo(unittest.TestCase):
    def test_reverts_with_the_stored_snapshot(self):
        item = _item(state="applied", prior_state={"title": "was"})
        store, stash = _FakeStore(item), _FakeStash()
        Actions(store, stash).undo("fp-1")
        self.assertEqual(stash.calls,
                         [("revert", "42", {"title": "was"})])

    def test_refuses_when_there_is_no_snapshot(self):
        # revert_scene raises on an empty snapshot by design. Reaching it with
        # one would turn a refusal a person can act on into a stack trace.
        item = _item(state="applied", prior_state=None)
        store, stash = _FakeStore(item), _FakeStash()
        with self.assertRaises(ValueError):
            Actions(store, stash).undo("fp-1")
        self.assertEqual(stash.calls, [])


class Reject(unittest.TestCase):
    def test_dismiss_rejects_this_proposal_only(self):
        store = _FakeStore(_item())
        Actions(store, _FakeStash()).dismiss("fp-1")
        self.assertEqual([c[0] for c in store.calls], ["dismissed"])

    def test_mute_rejects_the_subject_and_passes_its_identity_whole(self):
        store = _FakeStore(_item())
        Actions(store, _FakeStash()).mute("fp-1")
        self.assertEqual(store.calls[0][:3], ("muted", "scene", "42"))


if __name__ == "__main__":
    unittest.main()
