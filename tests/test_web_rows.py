import unittest

from cronicled.web.rows import Row, to_row, to_rows


def _item(**over):
    payload = {
        "path": "/library/Nine Winters/nine-winters-the-lantern-room.mp4",
        "creator": {"name": "Nine Winters", "source": "folder",
                    "competing": None, "rejected_folder": None},
        "candidate": {"id": "c-1", "title": "The Lantern Room"},
        "score": 0.8123,
        "runners_up": [{"title": "The Lantern", "score": 0.61}],
    }
    payload.update(over.pop("payload", {}))
    item = {"fingerprint": "fp-1", "state": "new", "summary": "s",
            "confidence": 0.8123, "payload": payload, "prior_state": None}
    item.update(over)
    return item


class RowContent(unittest.TestCase):
    def test_shows_the_filename_not_the_whole_path(self):
        # The directory is the reviewer's own filing; the leaf is what is
        # being judged. Showing the path makes every row mostly identical.
        self.assertEqual(to_row(_item()).filename,
                         "nine-winters-the-lantern-room.mp4")

    def test_carries_the_proposed_title_and_creator_with_its_source(self):
        row = to_row(_item())
        self.assertEqual(row.proposed_title, "The Lantern Room")
        self.assertEqual(row.creator, "Nine Winters")
        self.assertEqual(row.creator_source, "folder")

    def test_reports_a_folder_filename_disagreement(self):
        # The single most important field: it means "do not approve this".
        row = to_row(_item(payload={"creator": {
            "name": "Nine Winters", "source": "folder",
            "competing": "Ada Marsh", "rejected_folder": None}}))
        self.assertTrue(row.contested)
        self.assertIn("Ada Marsh", row.disagreement)

    def test_reports_a_folder_the_guards_threw_out(self):
        row = to_row(_item(payload={"creator": {
            "name": "Ada Marsh", "source": "filename",
            "competing": None, "rejected_folder": "Downloads"}}))
        self.assertTrue(row.contested)
        self.assertIn("Downloads", row.disagreement)

    def test_an_uncontested_row_says_so_without_inventing_text(self):
        row = to_row(_item())
        self.assertFalse(row.contested)
        self.assertIsNone(row.disagreement)

    def test_keeps_the_runners_up_that_lost(self):
        row = to_row(_item())
        self.assertIsInstance(row.runners_up, tuple)
        self.assertEqual([r["title"] for r in row.runners_up],
                         ["The Lantern"])

    def test_an_applied_row_is_undoable_only_with_a_snapshot(self):
        self.assertTrue(to_row(_item(state="applied",
                                     prior_state={"title": "old"})).undoable)
        # No snapshot means revert_scene would raise. Offering the button
        # would promise an undo the code cannot perform.
        self.assertFalse(to_row(_item(state="applied",
                                      prior_state=None)).undoable)
        self.assertFalse(to_row(_item(state="new")).undoable)


class RowRequirements(unittest.TestCase):
    def test_a_payload_missing_its_creator_raises(self):
        # No default. An absent creator rendered as blank reads as "nobody
        # disagreed", which is the reading that gets a wrong row approved.
        # The key named in the error must be "creator" itself, not some
        # field nested under it -- a `.get("creator", {})` still raises
        # eventually (building the row indexes into the empty dict), but
        # points at the wrong absence and would misdirect anyone debugging
        # a malformed payload.
        broken = _item()
        del broken["payload"]["creator"]
        with self.assertRaises(KeyError) as cm:
            to_row(broken)
        self.assertEqual(cm.exception.args[0], "creator")

    def test_the_score_is_shown_at_the_precision_it_was_decided_at(self):
        # Rounding to 2 places would print 0.81 for a value that missed a 0.82
        # threshold, making a refusal look like an acceptance.
        self.assertEqual(to_row(_item()).score_text, "0.812")


class ToRowsTest(unittest.TestCase):
    def test_converts_every_item_and_keeps_their_order(self):
        # `to_rows` is the batch entry point the interface names alongside
        # `to_row`; nothing above exercises it directly.
        items = [_item(fingerprint="fp-1"), _item(fingerprint="fp-2")]
        rows = to_rows(items)
        self.assertEqual([r.fingerprint for r in rows], ["fp-1", "fp-2"])
