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
        "candidate": {"id": "c-1", "title": "The Lantern Room", "image": None,
                     "performers": [{"stored_id": None, "name": "Ivy Kingsley"}],
                     "studio": {"stored_id": None, "name": "Amber Vale"}},
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

    def test_a_malformed_runner_up_raises_rather_than_rendering_blank(self):
        # The pressure here is to "harden" this with .get(..., "") the next
        # time something malformed turns up. That would not be safer: a
        # runner-up with a blank title renders as an empty entry in the "also
        # considered" column, and a column that looks empty reads as "nothing
        # else was close" — the reassuring reading, and the one that gets a row
        # approved. The same silent-blank failure this normalisation exists to
        # prevent, arrived at from the other direction. A malformed entry is a
        # wiring error and must propagate, which is `scan.py`'s stated policy
        # for a malformed candidate too.
        with self.assertRaises(KeyError):
            to_row(_item(payload={"runners_up": [{"score": 0.61}]}))
        with self.assertRaises(KeyError):
            to_row(_item(payload={
                "runners_up": [{"candidate": {"title": "The Lantern"}}]}))

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


class CoverImage(unittest.TestCase):
    # `carries_cover` is the field a person reads before clicking Approve
    # (and the same one `Actions.undo` reads to report what it could not
    # restore) -- see `rows.carries_cover`'s docstring for why it is
    # indexed, not `.get`.

    def test_a_candidate_carrying_an_image_reports_a_cover(self):
        row = to_row(_item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room",
            "image": "data:image/jpeg;base64,notarealcover",
            "performers": [], "studio": None}}))
        self.assertTrue(row.carries_cover)

    def test_a_candidate_with_no_image_reports_no_cover(self):
        row = to_row(_item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room", "image": None,
            "performers": [], "studio": None}}))
        self.assertFalse(row.carries_cover)

    def test_a_candidate_with_an_empty_image_reports_no_cover(self):
        row = to_row(_item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room", "image": "",
            "performers": [], "studio": None}}))
        self.assertFalse(row.carries_cover)


class PerformersAndStudio(unittest.TestCase):
    """What approving a proposal will actually write onto the scene -- the
    whole reason `cronicled.scan.examine` now scrapes the winning
    candidate's own URL rather than carrying only a title and a link.
    """

    def test_a_candidate_carrying_performers_and_a_studio_shows_both(self):
        row = to_row(_item())
        self.assertEqual(row.performers, ("Ivy Kingsley",))
        self.assertEqual(row.studio, "Amber Vale")

    def test_several_performers_are_kept_in_order(self):
        row = to_row(_item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room", "image": None,
            "studio": None,
            "performers": [{"stored_id": None, "name": "Ivy Kingsley"},
                          {"stored_id": None, "name": "Wren Ashcombe"}]}}))
        self.assertEqual(row.performers, ("Ivy Kingsley", "Wren Ashcombe"))

    def test_a_thin_unenriched_candidate_shows_neither(self):
        # HARM this guards: a row must never show a creator or a store it
        # did not actually get. A candidate whose enrichment never ran (or
        # failed and was left thin -- see `cronicled.scan.examine`'s
        # `enrich` argument) carries `performers: []` and `studio: None`
        # exactly as a search result always has, and the row must report
        # that honestly rather than inventing something to show.
        row = to_row(_item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room", "image": None,
            "performers": [], "studio": None}}))
        self.assertEqual(row.performers, ())
        self.assertIsNone(row.studio)

    def test_a_candidate_missing_the_performers_key_raises(self):
        # Same discipline as `test_a_candidate_missing_the_image_key_raises`:
        # both scraping methods select `performers` on every candidate they
        # return, so a real candidate always answers this key. A payload
        # missing it entirely must not be read back as "no performers",
        # which is indistinguishable from the ordinary case.
        broken = _item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room", "image": None,
            "studio": None}})
        with self.assertRaises(KeyError) as cm:
            to_row(broken)
        self.assertEqual(cm.exception.args[0], "performers")

    def test_a_candidate_missing_the_studio_key_raises(self):
        broken = _item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room", "image": None,
            "performers": []}})
        with self.assertRaises(KeyError) as cm:
            to_row(broken)
        self.assertEqual(cm.exception.args[0], "studio")


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

    def test_a_candidate_missing_the_image_key_raises(self):
        # `Stash.scrape_scenes_by_query`'s own query selects `image` on
        # every candidate it returns, so a real candidate always answers
        # this key -- `None` is "no cover", not "unknown". A candidate
        # missing the key entirely is a payload from somewhere that never
        # asked the question, and defaulting that to "no cover" is exactly
        # the silent, safe-looking guess this project has been bitten by
        # before: it would skip the warning on a proposal the code simply
        # never checked.
        broken = _item(payload={"candidate": {
            "id": "c-1", "title": "The Lantern Room"}})
        with self.assertRaises(KeyError) as cm:
            to_row(broken)
        self.assertEqual(cm.exception.args[0], "image")

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


class FailedApplyIsNotADeadEnd(unittest.TestCase):
    """A failed apply wrote NOTHING, so the proposal is exactly as live as it
    was before the attempt. The page used to offer controls only for `new`,
    so a row that failed lost every button and gave no reason -- it could not
    be retried, dismissed or muted, and nothing said why. Observed on a real
    library: an invalid query made every apply fail, and the rows it touched
    became unreachable.
    """

    def _failed(self, error="HTTP 422 from the media server"):
        return _item(state="failed", error=error)

    def test_a_failed_row_still_offers_its_decisions(self):
        row = to_row(self._failed())
        self.assertTrue(row.actionable)
        # Not undoable: nothing was written, so there is nothing to revert.
        self.assertFalse(row.undoable)

    def test_a_failed_row_carries_the_reason_it_failed(self):
        row = to_row(self._failed("HTTP 422: field needs a selection"))
        self.assertEqual(row.error, "HTTP 422: field needs a selection")

    def test_an_applied_row_is_the_only_closed_one(self):
        # Stated as "not applied" rather than a list of open states, so a
        # state added later inherits its controls instead of silently losing
        # them the way `failed` did.
        self.assertFalse(to_row(_item(state="applied",
                                      prior_state={"t": 1})).actionable)
        for state in ("new", "seen", "failed"):
            self.assertTrue(to_row(_item(state=state)).actionable, state)

    def test_a_row_that_never_failed_carries_no_error(self):
        self.assertIsNone(to_row(_item()).error)


class ARevertedRowStopsOfferingUndo(unittest.TestCase):
    """The visible half of the same fault: `undoable` requires `applied`, so
    recording the revert is what takes the button away. A reverted row is not
    closed, though -- the proposal is as live as it was before it was applied,
    so approving again, dismissing or muting all remain open.
    """

    def test_a_reverted_row_offers_no_undo(self):
        row = to_row(_item(state="reverted", prior_state={"title": "was"}))
        self.assertFalse(row.undoable)

    def test_a_reverted_row_is_still_actionable(self):
        # Undoing by mistake is ordinary. Nothing about a revert closes the
        # decision -- only an apply does.
        row = to_row(_item(state="reverted", prior_state={"title": "was"}))
        self.assertTrue(row.actionable)

    def test_the_snapshot_is_kept_after_a_revert(self):
        # It is the only record of what the scene looked like before the
        # apply. Keeping it is safe precisely because Undo needs `applied`.
        item = _item(state="reverted", prior_state={"title": "was"})
        self.assertTrue(item["prior_state"])
        self.assertFalse(to_row(item).undoable)
