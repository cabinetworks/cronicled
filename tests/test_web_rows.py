import unittest

from cronicled.scan import _runners_up
from cronicled.scoring import Match
from cronicled.web.rows import Row, to_row, to_rows


def _real_runners_up(losers, winner_title="The Lantern Room",
                      winner_score=0.8123):
    """Build a `runners_up` payload value the way `scan._runners_up` actually
    does, rather than hand-writing the dict it returns.

    A hand-written `{"title": ..., "score": ...}` literal is exactly what let
    a flattened shape drift from production, which nests the title under
    `candidate`. Calling the real function on invented candidates and matches
    means this fixture cannot drift from what `to_row` will actually be
    handed. `losers` is a list of `(title, score)` pairs for the candidates
    that lost; an empty list is a proposal with no rivals, which is normal.
    """
    candidates = [{"id": "c-0", "title": winner_title}]
    matches = [Match(value=winner_score, contained=True, meaningful_count=3)]
    for i, (title, value) in enumerate(losers, start=1):
        candidates.append({"id": "c-%d" % i, "title": title})
        matches.append(Match(value=value, contained=True, meaningful_count=2))
    return _runners_up(candidates, matches, winning_index=0)


def _item(**over):
    payload = {
        "path": "/library/Nine Winters/nine-winters-the-lantern-room.mp4",
        "creator": {"name": "Nine Winters", "source": "folder",
                    "competing": None, "rejected_folder": None},
        "candidate": {"id": "c-1", "title": "The Lantern Room"},
        "score": 0.8123,
        "runners_up": _real_runners_up([("The Lantern", 0.61)]),
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

    def test_runner_up_title_is_read_from_the_nested_candidate(self):
        # `scan._runners_up` nests each loser's title under `candidate`
        # (`{"candidate": {...}, "score": value}`) rather than at the top
        # level. The normalised view has to read it from there; reading a
        # top-level "title" that production never sets would leave every
        # runner-up silently titleless once rendered.
        row = to_row(_item(payload={"runners_up": _real_runners_up(
            [("The Lantern", 0.61), ("Winter Echoes", 0.55)])}))
        self.assertEqual(
            [(r["title"], r["score"]) for r in row.runners_up],
            [("The Lantern", 0.61), ("Winter Echoes", 0.55)])

    def test_no_rivals_is_a_normal_empty_list_not_an_error(self):
        # A proposal with nothing else in contention is the common case,
        # not a malformed one.
        row = to_row(_item(payload={"runners_up": _real_runners_up([])}))
        self.assertEqual(row.runners_up, ())

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
