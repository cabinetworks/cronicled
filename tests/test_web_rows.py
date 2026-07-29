import unittest

from cronicled.scan import _runners_up
from cronicled.scoring import Match
from cronicled.web.rows import (
    IDENTIFIED_SCORE_TEXT, Row, scene_url, to_mute_row, to_mute_rows,
    to_refusal_row, to_refusal_rows, to_row, to_rows,
)


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
            "confidence": 0.8123, "payload": payload, "prior_state": None,
            "subject_id": "42"}
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

    def test_another_store_matching_the_same_file_is_reported_as_contested(self):
        """`cronicled.scan.examine_sources` records another configured
        store's own winning candidate under `competing_store` when more than
        one store clears the threshold for the same file (see
        `_choose_winner`) -- reported the same way a folder/filename
        disagreement already is, so a reviewer sees it before approving."""
        row = to_row(_item(payload={"competing_store": [
            {"store": "otherstore",
             "candidate": {"id": "c-9", "title": "Morning Ritual (Alt Cut)"},
             "score": 0.91},
        ]}))
        self.assertTrue(row.contested)
        self.assertIn("otherstore", row.disagreement)

    def test_two_competing_stores_are_both_named(self):
        row = to_row(_item(payload={"competing_store": [
            {"store": "alpha", "candidate": {"id": "c-9", "title": "X"},
             "score": 0.91},
            {"store": "beta", "candidate": {"id": "c-10", "title": "Y"},
             "score": 0.85},
        ]}))
        self.assertIn("alpha", row.disagreement)
        self.assertIn("beta", row.disagreement)

    def test_agreeing_stores_are_carried_onto_the_row(self):
        """`cronicled.scan.examine_sources` records every OTHER store that
        named the proposed candidate under `agreeing_stores` when two or
        more stores were too close to call and said the same thing. All of
        them reach the page, in a fixture where dropping any one of them,
        or keeping only the first, would look different."""
        row = to_row(_item(payload={
            "agreeing_stores": ["beta", "gamma"]}))
        self.assertEqual(row.agreeing_stores, ("beta", "gamma"))

    def test_stores_agreeing_is_corroboration_and_never_a_warning(self):
        """HARM: agreement shown where warnings live is a warning that
        fires on the BEST rows this tool produces, and a warning a reviewer
        sees on good rows is one they learn to click past on the bad ones.
        """
        row = to_row(_item(payload={"agreeing_stores": ["beta"]}))
        self.assertFalse(row.contested)
        self.assertIsNone(row.disagreement)

    def test_no_agreeing_stores_key_at_all_is_an_empty_tuple(self):
        """Every proposal made before cross-store agreement existed, and
        every one only a single store answered, has no such key -- and must
        not raise, and must not read as corroborated by an unnamed
        somebody."""
        self.assertEqual(to_row(_item()).agreeing_stores, ())

    def test_no_competing_store_key_at_all_is_not_contested_by_itself(self):
        """The common, single-answer case: no `competing_store` key present
        at all (every proposal made before this ticket, and every proposal
        with only one winning store now) must not read as contested on its
        own."""
        row = to_row(_item())
        self.assertNotIn("also matched", row.disagreement or "")

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


class SceneUrlHelper(unittest.TestCase):
    """`scene_url` — the one place this project writes the path shape for a
    scene page, shared by every row builder that links to one."""

    def test_builds_the_scene_page_from_the_base_url_and_the_subject_id(self):
        self.assertEqual(scene_url("http://media.example", "42"),
                         "http://media.example/scenes/42")

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self):
        self.assertEqual(scene_url("http://media.example/", "42"),
                         "http://media.example/scenes/42")

    def test_no_base_url_is_none_not_a_broken_link(self):
        # Ticket 97: the tool now starts read-only with no server
        # configured, and the link has to degrade rather than render
        # broken.
        self.assertIsNone(scene_url(None, "42"))

    def test_an_empty_base_url_is_also_none(self):
        self.assertIsNone(scene_url("", "42"))


class RowSceneUrl(unittest.TestCase):
    """A proposal row links to its own scene on the media server -- ticket
    97's second half, alongside the muted subject's name."""

    def test_a_row_carries_its_scene_url_when_a_server_is_configured(self):
        row = to_row(_item(subject_id="55"), base_url="http://media.example")
        self.assertEqual(row.scene_url, "http://media.example/scenes/55")

    def test_a_row_has_no_scene_url_without_a_configured_server(self):
        # The default, and the case every existing caller of `to_row`
        # exercises -- must not raise or invent a link.
        row = to_row(_item())
        self.assertIsNone(row.scene_url)

    def test_to_rows_threads_the_base_url_to_every_row(self):
        items = [_item(fingerprint="fp-1", subject_id="1"),
                 _item(fingerprint="fp-2", subject_id="2")]
        rows = to_rows(items, base_url="http://media.example")
        self.assertEqual([r.scene_url for r in rows],
                         ["http://media.example/scenes/1",
                          "http://media.example/scenes/2"])


class ToRefusalRowSceneUrl(unittest.TestCase):
    def _entry(self, **over):
        entry = {"subject_type": "scene", "subject_id": "1",
                 "path": "/library/Nine Winters/nine-winters-clip.mp4",
                 "reason": "a tie between two candidates",
                 "at": "2026-07-27T00:00:00"}
        entry.update(over)
        return entry

    def test_a_refusal_carries_its_scene_url_when_configured(self):
        row = to_refusal_row(self._entry(subject_id="9"),
                             base_url="http://media.example")
        self.assertEqual(row["scene_url"], "http://media.example/scenes/9")

    def test_a_refusal_has_no_scene_url_without_a_configured_server(self):
        row = to_refusal_row(self._entry())
        self.assertIsNone(row["scene_url"])

    def test_to_refusal_rows_threads_the_base_url(self):
        entries = [self._entry(subject_id="1"), self._entry(subject_id="2")]
        rows = to_refusal_rows(entries, base_url="http://media.example")
        self.assertEqual([r["scene_url"] for r in rows],
                         ["http://media.example/scenes/1",
                          "http://media.example/scenes/2"])


class ToMuteRowTest(unittest.TestCase):
    """`Store.mutes()`'s dict shape -> what the Muted section shows --
    ticket 97's headline case: a muted subject named by its filename, not a
    bare id, whenever one can be recovered.
    """

    def _entry(self, **over):
        entry = {"subject_type": "scene", "subject_id": "1",
                 "reason": "never identifiable",
                 "at": "2026-07-27T00:00:00", "payload": None}
        entry.update(over)
        return entry

    def test_a_recoverable_payload_is_shown_by_filename(self):
        row = to_mute_row(self._entry(
            payload={"path": "/library/Nine Winters/reel.mp4"}))
        self.assertEqual(row["filename"], "reel.mp4")

    def test_no_payload_at_all_reports_no_filename(self):
        # The genuine exception: muted ahead of any proposal ever being
        # recorded for the subject. This must stay `None`, not silently
        # become an empty string or the subject id -- that is the
        # template's job to mark as the exception, not this one's to hide.
        row = to_mute_row(self._entry(payload=None))
        self.assertIsNone(row["filename"])

    def test_carries_the_subject_reason_and_when(self):
        row = to_mute_row(self._entry())
        self.assertEqual(row["subject_type"], "scene")
        self.assertEqual(row["subject_id"], "1")
        self.assertEqual(row["reason"], "never identifiable")
        self.assertEqual(row["at"], "2026-07-27T00:00:00")

    def test_carries_its_scene_url_when_configured(self):
        row = to_mute_row(self._entry(subject_id="9"),
                          base_url="http://media.example")
        self.assertEqual(row["scene_url"], "http://media.example/scenes/9")

    def test_has_no_scene_url_without_a_configured_server(self):
        row = to_mute_row(self._entry())
        self.assertIsNone(row["scene_url"])

    def test_to_mute_rows_converts_every_entry_and_keeps_their_order(self):
        entries = [self._entry(subject_id="1"), self._entry(subject_id="2")]
        rows = to_mute_rows(entries)
        self.assertEqual([r["subject_id"] for r in rows], ["1", "2"])

    def test_a_payload_missing_its_path_raises_rather_than_hiding_the_filename(self):
        # Same discipline as `to_row`'s own `filename`: a payload that
        # exists but cannot answer `path` is malformed, not "no filename".
        with self.assertRaises(KeyError):
            to_mute_row(self._entry(payload={"title": "no path here"}))


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


class ToRefusalRowTest(unittest.TestCase):
    """`Store.refusals()`'s dict shape -> what the Refused section shows."""

    def _entry(self, **over):
        entry = {"subject_type": "scene", "subject_id": "1",
                 "path": "/library/Nine Winters/nine-winters-clip.mp4",
                 "reason": "a tie between two candidates",
                 "at": "2026-07-27T00:00:00"}
        entry.update(over)
        return entry

    def test_shows_the_filename_not_the_whole_path(self):
        # The same editorial choice `to_row.filename` makes: the directory
        # is the reviewer's own filing, not part of judging a refusal.
        row = to_refusal_row(self._entry())
        self.assertEqual(row["filename"], "nine-winters-clip.mp4")

    def test_carries_the_subject_reason_and_when(self):
        row = to_refusal_row(self._entry())
        self.assertEqual(row["subject_type"], "scene")
        self.assertEqual(row["subject_id"], "1")
        self.assertEqual(row["reason"], "a tie between two candidates")
        self.assertEqual(row["at"], "2026-07-27T00:00:00")

    def test_to_refusal_rows_converts_every_entry_and_keeps_their_order(self):
        entries = [self._entry(subject_id="1"), self._entry(subject_id="2")]
        rows = to_refusal_rows(entries)
        self.assertEqual([r["subject_id"] for r in rows], ["1", "2"])


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


class ContestedOnlyWhenSomethingActuallyDisagrees(unittest.TestCase):
    """Measured against a real library: six contested warnings, one of which
    meant anything. A warning firing on nearly every row stops being read, and
    then the one that matters goes past unread with the rest.

    The resolver still records everything it passed over; what changes is
    which of those is worth interrupting a person about.
    """

    def _creator(self, **over):
        c = {"name": "Nine Winters", "source": "filename",
             "competing": None, "rejected_folder": None}
        c.update(over)
        return c

    def test_a_folder_that_is_the_filename_repeated_is_not_a_disagreement(self):
        # One file per folder is an ordinary layout. Such a folder was never
        # going to name anyone -- it failed the guards for being a title, not
        # because the filing convention is wrong -- and it says nothing the
        # filename beside it does not already say.
        row = to_row(_item(payload={
            "path": "/library/Nine Winters - The Lantern Room/"
                    "Nine Winters - The Lantern Room.mp4",
            "creator": self._creator(
                rejected_folder="Nine Winters - The Lantern Room")}))
        self.assertFalse(row.contested)
        self.assertIsNone(row.disagreement)

    def test_a_folder_naming_someone_else_is_still_a_disagreement(self):
        # The case the field exists for, and the reason the rule above is
        # equality rather than containment: a folder genuinely naming a
        # different creator is a mis-filing, and hiding it is the expensive
        # mistake.
        row = to_row(_item(payload={
            "path": "/library/Ada Marsh/Nine Winters - The Lantern Room.mp4",
            "creator": self._creator(rejected_folder="Ada Marsh")}))
        self.assertTrue(row.contested)
        self.assertIn("Ada Marsh", row.disagreement)

    def test_a_competing_name_containing_the_winner_is_the_same_person(self):
        row = to_row(_item(payload={"creator": self._creator(
            competing="Nine Winters - The Lantern Room")}))
        self.assertFalse(row.contested)

    def test_a_competing_name_that_is_a_different_person_still_warns(self):
        # The one real signal in the measured set: a store contending with a
        # creator. This is what the whole field is for.
        row = to_row(_item(payload={"creator": self._creator(
            competing="Ada Marsh")}))
        self.assertTrue(row.contested)
        self.assertIn("Ada Marsh", row.disagreement)

    def test_both_kinds_can_still_be_reported_together(self):
        row = to_row(_item(payload={
            "path": "/library/Downloads/Nine Winters - The Lantern Room.mp4",
            "creator": self._creator(competing="Ada Marsh",
                                     rejected_folder="Downloads")}))
        self.assertTrue(row.contested)
        self.assertIn("Ada Marsh", row.disagreement)
        self.assertIn("Downloads", row.disagreement)


def _identified_item(**over):
    """A proposal a stash-box identified by fingerprint, as
    `scan.fingerprint_outcome` writes it: no creator, no score, no
    runners-up, and no `confidence` on the item at all."""
    payload = {
        "path": "/library/Nine Winters/nine-winters-the-lantern-room.mp4",
        "candidate": {"id": "c-1", "title": "The Lantern Room", "image": None,
                     "performers": [{"stored_id": None, "name": "Ivy Kingsley"}],
                     "studio": {"stored_id": None, "name": "Amber Vale"}},
        "identified_by": "fingerprint",
        "box": "north-box",
        "remote_site_id": "r-77",
    }
    payload.update(over.pop("payload", {}))
    item = {"fingerprint": "fp-2", "state": "new", "summary": "s",
            "confidence": None, "payload": payload, "prior_state": None,
            "subject_id": "43"}
    item.update(over)
    return item


class IdentifiedRow(unittest.TestCase):
    """A row for a proposal nothing scored.

    The whole reason this shape exists is that a fingerprint hit did not
    score 1.0 -- it was identified -- so every field a reader would take for
    the scorer's own output has to be absent rather than filled in with
    something plausible.
    """

    def test_it_carries_no_score_at_all(self):
        # HARM: a number here is one this page invented, shown in the same
        # column, in the same type, as numbers the scorer really produced --
        # and read by a person deciding whether to approve.
        row = to_row(_identified_item())
        self.assertIsNone(row.score)
        self.assertEqual(row.score_text, IDENTIFIED_SCORE_TEXT)

    def test_it_names_the_box_that_recognised_the_file(self):
        self.assertEqual(to_row(_identified_item()).identifying_box,
                         "north-box")

    def test_it_claims_no_creator_because_none_was_ever_resolved(self):
        # The file was never searched for by name, so there is no attribution
        # to show. Naming one anyway would report a resolution that never ran.
        row = to_row(_identified_item())
        self.assertIsNone(row.creator)
        self.assertIsNone(row.creator_source)

    def test_it_still_shows_what_approving_it_would_write(self):
        row = to_row(_identified_item())
        self.assertEqual(row.proposed_title, "The Lantern Room")
        self.assertEqual(row.performers, ("Ivy Kingsley",))
        self.assertEqual(row.studio, "Amber Vale")

    def test_nothing_about_it_is_contested(self):
        # Boxes that disagreed never produced a proposal at all, and boxes
        # that agreed are agreement -- not a warning to spend on every row.
        row = to_row(_identified_item())
        self.assertFalse(row.contested)
        self.assertIsNone(row.disagreement)
        self.assertEqual(row.runners_up, ())

    def test_it_names_no_agreeing_stores(self):
        # Boxes and stores are two different mechanisms agreeing about two
        # different things. A box's own agreement travels in the payload's
        # `agreeing_boxes`; this field is the one for stores, and a
        # fingerprint row was never searched against a store at all.
        self.assertEqual(to_row(_identified_item()).agreeing_stores, ())

    def test_a_scored_row_names_no_box(self):
        # The other side of the same discriminator: the two shapes must not
        # be readable as one, in either direction.
        row = to_row(_item())
        self.assertIsNone(row.identifying_box)
        self.assertEqual(row.score_text, "0.812")

    def test_the_two_shapes_do_not_produce_the_same_row(self):
        scored = to_row(_item())
        identified = to_row(_identified_item())
        self.assertNotEqual(
            (scored.score, scored.score_text, scored.identifying_box,
             scored.creator),
            (identified.score, identified.score_text,
             identified.identifying_box, identified.creator))

    def test_a_payload_that_claims_identification_but_names_no_box_raises(self):
        # HARM: "identified by nobody" is precisely the row a person would
        # approve without noticing anything was missing.
        item = _identified_item()
        del item["payload"]["box"]
        with self.assertRaises(KeyError):
            to_row(item)

    def test_a_payload_with_neither_a_score_nor_an_identification_raises(self):
        # Absence of `identified_by` means a SCORED proposal -- which is what
        # every payload written before this existed is -- so it must go down
        # the branch that indexes `score` and `creator` and raises when they
        # are missing, never down the one that shows a row with neither.
        item = _item()
        del item["payload"]["score"]
        with self.assertRaises(KeyError):
            to_row(item)
