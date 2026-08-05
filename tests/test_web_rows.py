import re
import unittest
from dataclasses import asdict, replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from cronicled.schedule import Entry, LoopStatus, TickResult, resolve
from cronicled.scan import _runners_up, candidate_url
from cronicled.scoring import Match
from cronicled.tag_descriptions import Found
from cronicled.tag_descriptions import proposal as tag_description_proposal
from cronicled.tags import MERGE_IS_IRREVERSIBLE, UNDECIDED_MANY, cluster_tags
from cronicled.tags import proposal as tag_proposal
from cronicled.web.rows import (
    IDENTIFIED_SCORE_TEXT, KIND_DESCRIPTION, KIND_ENRICHMENT, KIND_SCENE,
    KIND_TAG_DESCRIPTION, STORE_ANSWERED, STORE_EMPTY, STORE_FAILED,
    Appointments, Row, ScheduledProducer, local, local_times, scene_url,
    tag_url, to_description_row, to_enrichment_row, to_merge_row,
    to_merge_rows, to_mute_row, to_mute_rows, to_refusal_row, to_refusal_rows,
    to_row, to_rows, to_schedule_view, to_tag_description_row,
)

# A REAL zone that observes daylight saving, and one whose offset differs from
# UTC on BOTH sides of every transition (+01:00 in winter, +02:00 in summer).
# Both properties are load-bearing and neither is decoration:
#
# - a fixed-offset zone would let an implementation that adds a constant pass
#   every assertion here, however carefully they were written;
# - a zone whose winter offset is zero (Europe/Lisbon, say) would make a
#   winter-dated assertion identical to leaving the timestamp in UTC, so half
#   the tests below would go on passing against a renderer that converted
#   nothing at all.
ZONE = ZoneInfo("Europe/Madrid")


def _real_runners_up(losers, winner_title="The Lantern Room",
                      winner_score=0.8123, winner_urls=None):
    """Build a `runners_up` payload value the way `scan._runners_up` actually
    does, rather than hand-writing the dict it returns.

    A hand-written `{"title": ..., "score": ...}` literal is exactly what let
    a flattened shape drift from production, which nests the title under
    `candidate`. Calling the real function on invented candidates and matches
    means this fixture cannot drift from what `to_row` will actually be
    handed. `losers` is a list of `(title, score)` pairs for the candidates
    that lost -- or `(title, score, urls)` triples where the loser's own
    address matters; an empty list is a proposal with no rivals, which is
    normal.
    """
    candidates = [{"id": "c-0", "title": winner_title,
                   "urls": winner_urls or []}]
    matches = [Match(value=winner_score, contained=True, meaningful_count=3)]
    for i, loser in enumerate(losers, start=1):
        title, value = loser[0], loser[1]
        urls = loser[2] if len(loser) > 2 else []
        candidates.append({"id": "c-%d" % i, "title": title, "urls": urls})
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
    # `subject_type` is a NOT NULL column on `item` (see cronicled/store.py's
    # schema), so every real row `Store.items()` returns carries one. It is in
    # this fixture for that reason rather than for `to_rows`' sake: a helper
    # standing in for a store row must not be more forgiving than the store.
    item = {"fingerprint": "fp-1", "state": "new", "summary": "s",
            "confidence": 0.8123, "payload": payload, "prior_state": None,
            "subject_type": "scene", "subject_id": "42"}
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


class RowCandidateUrl(unittest.TestCase):
    """Where the PROPOSED title came from -- the candidate's own page on the
    store that offered it, which is the one piece of evidence the proposal is
    built on and was previously only reachable by retyping a title into a
    search by hand.

    Not to be confused with `scene_url` above: that is the file as the media
    server holds it today, this is the record a person is being asked to
    overwrite it with. Confusing the two sends a reviewer to check a
    candidate and shows them the thing they were checking it against.
    """

    def _with_candidate(self, **fields):
        candidate = {"id": "c-1", "title": "The Lantern Room", "image": None,
                     "performers": [], "studio": None}
        candidate.update(fields)
        return to_row(_item(payload={"candidate": candidate}))

    def test_a_store_candidate_links_to_its_own_page(self):
        row = self._with_candidate(urls=["https://store.example/lantern-room"])
        self.assertEqual(row.candidate_url,
                         "https://store.example/lantern-room")

    def test_a_candidate_with_no_address_at_all_has_none(self):
        # A name search commonly returns a title and nothing else. `None` is
        # the honest answer and renders as text; anything else here would be
        # an address this row invented.
        self.assertIsNone(self._with_candidate().candidate_url)

    def test_an_empty_url_is_no_url_and_never_an_empty_anchor(self):
        # Both spellings of "the store gave us the key and nothing in it".
        # An anchor with an empty href looks like a link and goes to the
        # page it is already on.
        self.assertIsNone(self._with_candidate(urls=[], url="").candidate_url)

    def test_it_matches_the_precedence_an_apply_writes_with(self):
        # `urls` wins over the deprecated singular `url`, which is the
        # precedence `Stash.apply_scene` uses when deciding what to write
        # onto the scene. A second derivation here -- one reading `url`
        # first, or reading only one of the two -- would offer a link to a
        # page an approve would then NOT write, which is worse than no link:
        # it is evidence the reviewer did not actually have.
        row = self._with_candidate(urls=["https://store.example/plural"],
                                   url="https://store.example/singular")
        self.assertEqual(row.candidate_url, "https://store.example/plural")

    def test_the_singular_url_is_still_read_when_the_plural_is_empty(self):
        row = self._with_candidate(urls=[],
                                   url="https://store.example/singular")
        self.assertEqual(row.candidate_url, "https://store.example/singular")

    def test_it_is_the_projects_one_rule_and_not_a_second_reading(self):
        # Asserted as agreement with the shared rule on a candidate whose two
        # address fields DISAGREE -- the only input on which a re-derivation
        # and the real thing can be told apart at all.
        candidate = {"id": "c-1", "title": "The Lantern Room", "image": None,
                     "performers": [], "studio": None,
                     "urls": ["https://store.example/plural"],
                     "url": "https://store.example/singular"}
        self.assertEqual(to_row(_item(payload={"candidate": candidate})).candidate_url,
                         candidate_url(candidate))

    def test_every_runner_up_carries_its_own_address(self):
        # A losing candidate is exactly the one an operator opens before
        # overriding a decision. Two losers with DIFFERENT addresses, so a
        # builder handing every runner-up the first loser's link -- or the
        # winner's -- fails here rather than sending a reviewer to the wrong
        # page.
        row = to_row(_item(payload={"runners_up": _real_runners_up(
            [("The Lantern", 0.61, ["https://store.example/lantern"]),
             ("Lantern Nights", 0.55, ["https://store.example/nights"])],
            winner_urls=["https://store.example/winner"])}))
        self.assertEqual([r["url"] for r in row.runners_up],
                         ["https://store.example/lantern",
                          "https://store.example/nights"])

    def test_a_runner_up_with_no_address_has_none_rather_than_the_winners(self):
        row = to_row(_item(payload={"runners_up": _real_runners_up(
            [("The Lantern", 0.61)],
            winner_urls=["https://store.example/winner"])}))
        self.assertIsNone(row.runners_up[0]["url"])

    def test_a_fingerprint_identified_row_links_the_candidate_not_the_endpoint(self):
        # A box's `endpoint` is a GraphQL API address, not a page a person
        # can open, and there is no rule anywhere in this project that turns
        # one into the other. The match itself comes back through the same
        # selection set a text scrape uses and carries its own address, so
        # THAT is what is linked -- and the endpoint and the box's id must
        # not be assembled into a second, guessed one.
        row = to_row(_item(payload={
            "identified_by": "fingerprint", "box": "a-box",
            "endpoint": "https://box.example/graphql",
            "remote_site_id": "6d3f-scene",
            "candidate": {"id": "c-1", "title": "The Lantern Room",
                          "image": None, "performers": [], "studio": None,
                          "urls": ["https://store.example/lantern-room"]}}))
        self.assertEqual(row.candidate_url,
                         "https://store.example/lantern-room")

    def test_a_fingerprint_identification_with_no_candidate_address_has_none(self):
        # THE guard against a second derivation: everything needed to build
        # `endpoint + "/scenes/" + remote_site_id` is sitting in this
        # payload, and the honest answer is still `None`. Uncertainty may
        # withhold evidence and never supply it.
        row = to_row(_item(payload={
            "identified_by": "fingerprint", "box": "a-box",
            "endpoint": "https://box.example/graphql",
            "remote_site_id": "6d3f-scene",
            "candidate": {"id": "c-1", "title": "The Lantern Room",
                          "image": None, "performers": [], "studio": None}}))
        self.assertIsNone(row.candidate_url)


class ToRefusalRowSceneUrl(unittest.TestCase):
    def _entry(self, **over):
        entry = {"subject_type": "scene", "subject_id": "1",
                 "path": "/library/Nine Winters/nine-winters-clip.mp4",
                 "reason": "a tie between two candidates",
                 "at": "2026-07-27T00:00:00", "stores": []}
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
    """`Store.mutes()`'s dict shape -> what the Muted section shows.

    A muted subject and a dismissed one are the same thing seen twice --
    something a person hid that they may want back -- so a muted entry
    carries the SAME row the Dismissed section renders, and the only
    difference between the two is the control.
    """

    def _entry(self, **over):
        entry = {"subject_type": "scene", "subject_id": "1",
                 "reason": "never identifiable",
                 "at": "2026-07-27T00:00:00", "item": None}
        entry.update(over)
        return entry

    def test_a_recovered_item_carries_every_field_a_dismissed_row_carries(self):
        # The requirement itself: the Muted section showed a subject id and
        # a sentence, while the Dismissed section beside it showed the file,
        # the proposed title, the attribution and the score. Every one of
        # those is asserted here against a distinct real value, so dropping
        # any single one fails -- checking only the filename would have
        # passed the thin row that started this.
        row = to_mute_row(
            self._entry(subject_id="42",
                        item=_item(state="muted", subject_id="42",
                                   payload={"candidate": {
                                       "id": "c-1",
                                       "title": "The Lantern Room",
                                       "image": None, "performers": [],
                                       "studio": None,
                                       "urls": ["https://store.example/l"]}})),
            base_url="http://media.example", zone=ZONE)["row"]

        self.assertEqual(row.kind, KIND_SCENE)
        self.assertEqual(row.filename, "nine-winters-the-lantern-room.mp4")
        self.assertEqual(row.proposed_title, "The Lantern Room")
        self.assertEqual(row.creator, "Nine Winters")
        self.assertEqual(row.creator_source, "folder")
        self.assertEqual(row.score_text, "0.812")
        self.assertEqual(row.scene_url, "http://media.example/scenes/42")
        self.assertEqual(row.candidate_url, "https://store.example/l")

    def test_a_muted_performer_becomes_the_description_row_it_is(self):
        # A mute is keyed by (subject_type, subject_id) and one click on a
        # description proposal's Mute button puts a performer in this list.
        # Putting one through the scene builder raised a `KeyError` that took
        # out the whole Muted section -- and with it the only control that
        # lifts the mute. Dispatched on the item's own `subject_type`, the
        # same way every other section dispatches.
        row = to_mute_row(
            {"subject_type": "performer", "subject_id": "7",
             "reason": "leave this one alone", "at": "2026-07-27T00:00:00",
             "item": _description_item()},
            base_url="http://media.example", zone=ZONE)["row"]

        self.assertEqual(row.kind, KIND_DESCRIPTION)
        self.assertEqual(row.name, "Wren Alderly")
        self.assertEqual(row.performer_url,
                         "http://media.example/performers/7")

    def test_no_item_at_all_has_no_row_rather_than_a_blank_one(self):
        # The genuine exception: muted ahead of any proposal ever being
        # recorded for the subject. This must stay `None`, not become a row
        # of empty fields -- blank fields read as a page that failed to
        # render, and this case is real and must go on saying so. Marking it
        # visibly as the exception is the template's job, not this one's.
        self.assertIsNone(to_mute_row(self._entry(item=None),
                                     zone=ZONE)["row"])

    def test_an_item_that_cannot_answer_raises_rather_than_reading_as_none(self):
        # `Store.mutes()` answers either `None` -- nothing was ever proposed
        # for this subject -- or a whole decoded row. Anything else is a
        # wiring fault, and folding it into the `None` branch would draw the
        # honest exception ("muted before any proposal was ever recorded")
        # over a subject that HAS one: a real proposal hidden behind a
        # sentence saying there is none, which is worse than the crash.
        with self.assertRaises(KeyError):
            to_mute_row(self._entry(item={}), zone=ZONE)

    def test_carries_the_subject_reason_and_when(self):
        row = to_mute_row(self._entry(), zone=ZONE)
        self.assertEqual(row["subject_type"], "scene")
        self.assertEqual(row["subject_id"], "1")
        self.assertEqual(row["reason"], "never identifiable")
        self.assertEqual(row["at"], "2026-07-27T00:00:00")

    def test_carries_its_scene_url_when_configured(self):
        # Read by the no-item branch, which has no row to take an address
        # from and still knows which subject it is.
        row = to_mute_row(self._entry(subject_id="9"),
                          base_url="http://media.example", zone=ZONE)
        self.assertEqual(row["subject_url"], "http://media.example/scenes/9")

    def test_a_muted_performer_with_no_item_links_to_the_performer_page(self):
        row = to_mute_row(
            {"subject_type": "performer", "subject_id": "7", "reason": "r",
             "at": "t", "item": None}, base_url="http://media.example",
            zone=ZONE)
        self.assertEqual(row["subject_url"],
                         "http://media.example/performers/7")

    def test_a_muted_enrichment_subject_also_links_to_the_performer_page(self):
        # HARM: `performer-enrichment` is a SECOND subject type about a
        # performer, and a mute reaching it with no item at all would
        # otherwise fall through to `scene_url` -- the same mistake fixed for
        # `descriptions.SUBJECT_TYPE` above.
        row = to_mute_row(
            {"subject_type": "performer-enrichment", "subject_id": "9",
             "reason": "r", "at": "t", "item": None},
            base_url="http://media.example", zone=ZONE)
        self.assertEqual(row["subject_url"],
                         "http://media.example/performers/9")

    def test_a_muted_enrichment_subject_becomes_the_enrichment_row_it_is(self):
        row = to_mute_row(
            {"subject_type": "performer-enrichment", "subject_id": "9",
             "reason": "r", "at": "t", "item": _enrichment_item()},
            base_url="http://media.example", zone=ZONE)["row"]
        self.assertEqual(row.kind, KIND_ENRICHMENT)
        self.assertEqual(row.name, "Wren Alderly")

    def test_has_no_scene_url_without_a_configured_server(self):
        row = to_mute_row(self._entry(), zone=ZONE)
        self.assertIsNone(row["subject_url"])

    def test_to_mute_rows_converts_every_entry_and_keeps_their_order(self):
        entries = [self._entry(subject_id="1"), self._entry(subject_id="2")]
        rows = to_mute_rows(entries, zone=ZONE)
        self.assertEqual([r["subject_id"] for r in rows], ["1", "2"])

    def test_a_payload_missing_its_path_raises_rather_than_hiding_the_filename(self):
        # Same discipline as `to_row`'s own `filename`: a payload that
        # exists but cannot answer `path` is malformed, not "no filename".
        broken = _item()
        del broken["payload"]["path"]
        with self.assertRaises(KeyError):
            to_mute_row(self._entry(item=broken), zone=ZONE)


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
                 "at": "2026-07-27T00:00:00", "stores": []}
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


class ARefusedRowShowsEveryStoreSearched(unittest.TestCase):
    """`Store.refusals()`'s `stores` -> one line per store in the Refused
    section.

    The defect this is the display half of: a refusal showed one sentence
    naming the one store that scored highest, which reads as though the
    others were never searched. Seeing 0.236 / 0.342 / 0.259 says something
    the single line cannot -- that nothing anywhere resembles the file, which
    points at the query rather than at the threshold.
    """

    ANSWERED = {"store": "alpha", "rows": 40, "score": 0.342,
                "title": "Evening Ritual",
                "url": "https://alpha.example/clip/evening-ritual",
                "error": None}
    EMPTY = {"store": "beta", "rows": 0, "score": None, "title": None,
             "url": None, "error": None}
    FAILED = {"store": "gamma", "rows": None, "score": None, "title": None,
              "url": None, "error": "TimeoutError: timed out"}

    def _entry(self, stores):
        return {"subject_type": "scene", "subject_id": "1",
                "path": "/library/Nine Winters/nine-winters-clip.mp4",
                "reason": "alpha: nothing above the threshold (0.70)",
                "at": "2026-07-27T00:00:00", "stores": stores}

    def _views(self, stores):
        return to_refusal_row(self._entry(stores))["stores"]

    def test_every_store_gets_its_own_line_as_one_whole_shape(self):
        """The whole tuple of whole dicts. A check that `alpha` is there
        passes while `beta` and `gamma` are dropped -- which is the bug."""
        self.assertEqual(self._views([self.ANSWERED, self.EMPTY, self.FAILED]), (
            {"store": "alpha", "outcome": STORE_ANSWERED, "rows": 40,
             "score": 0.342, "title": "Evening Ritual",
             "url": "https://alpha.example/clip/evening-ritual",
             "error": None},
            {"store": "beta", "outcome": STORE_EMPTY, "rows": 0,
             "score": None, "title": None, "url": None, "error": None},
            {"store": "gamma", "outcome": STORE_FAILED, "rows": None,
             "score": None, "title": None, "url": None,
             "error": "TimeoutError: timed out"},
        ))

    def test_the_three_states_get_three_different_outcomes(self):
        """Each asserted against the specific value it must produce, and
        against each OTHER. One catch-all value would satisfy three separate
        "is it set" checks while collapsing all three into one line."""
        answered, empty, failed = self._views(
            [self.ANSWERED, self.EMPTY, self.FAILED])

        self.assertEqual(answered["outcome"], STORE_ANSWERED)
        self.assertEqual(empty["outcome"], STORE_EMPTY)
        self.assertEqual(failed["outcome"], STORE_FAILED)
        self.assertEqual(len({answered["outcome"], empty["outcome"],
                              failed["outcome"]}), 3)

    def test_a_store_that_answered_and_then_failed_leads_with_the_failure(self):
        """Both facts are recorded and both are carried, but the line is the
        failure's: "40 returned, best 0.342" reads as "this store has nothing
        like your file", and a store whose follow-up query never completed
        has not shown that."""
        both = dict(self.ANSWERED, error="RuntimeError: connection refused")

        view = self._views([both])[0]

        self.assertEqual(view["outcome"], STORE_FAILED)
        self.assertEqual(view["rows"], 40)
        self.assertEqual(view["score"], 0.342)

    def test_the_order_recorded_is_the_order_shown(self):
        """`scan._store_reports` ordered these with the scores in front of
        it. A second ordering rule here would be free to disagree with that
        one. The fixture is not in name order, so a re-sort is visible."""
        views = self._views([self.FAILED, self.ANSWERED, self.EMPTY])
        self.assertEqual([v["store"] for v in views],
                         ["gamma", "alpha", "beta"])

    def test_the_near_miss_address_is_carried_through_unchanged(self):
        view = self._views([self.ANSWERED])[0]
        self.assertEqual(view["url"],
                         "https://alpha.example/clip/evening-ritual")

    def test_a_candidate_with_no_address_carries_none_rather_than_a_guess(self):
        """Uncertainty withholds evidence rather than supplying it. There is
        no address to derive for a candidate that carries none, and the title
        is still shown -- as text, by the template's own `identifier`."""
        addressless = dict(self.ANSWERED, url=None)

        view = self._views([addressless])[0]

        self.assertIsNone(view["url"])
        self.assertEqual(view["title"], "Evening Ritual")

    def test_an_empty_address_is_no_address(self):
        """A store that answered with `""` rather than omitting the field
        must not become an anchor pointing at the page it is on."""
        view = self._views([dict(self.ANSWERED, url="")])[0]
        self.assertFalse(view["url"])

    def test_a_refusal_no_store_search_stands_behind_shows_no_lines(self):
        self.assertEqual(self._views([]), ())


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

    def test_it_names_the_algorithm_that_grounded_the_identification(self):
        # HARM: "identified by fingerprint" alone covers a byte-identical
        # match and a perceptual one alike, which is the whole defect this
        # field exists to close. A reviewer reads this row, not the payload.
        row = to_row(_identified_item(
            payload={"algorithms": ["OSHASH"]}))
        self.assertEqual(row.identified_by_algorithm, "OSHASH")

    def test_several_exact_algorithms_are_all_named(self):
        row = to_row(_identified_item(
            payload={"algorithms": ["MD5", "OSHASH"]}))
        self.assertEqual(row.identified_by_algorithm, "MD5/OSHASH")

    def test_a_payload_recorded_before_this_field_existed_names_none(self):
        # `.get`, not indexed: `_identified_item`'s own default payload has
        # no `algorithms` key at all -- exactly what every payload recorded
        # before this field existed looks like -- and absence must read as
        # "not stated", never guessed at either extreme.
        row = to_row(_identified_item())
        self.assertIsNone(row.identified_by_algorithm)

    def test_a_scored_row_names_no_algorithm(self):
        row = to_row(_item())
        self.assertIsNone(row.identified_by_algorithm)

    def test_a_perceptual_disagreement_is_carried_onto_the_row(self):
        row = to_row(_identified_item(payload={
            "perceptual_disagreement":
                "south-box says r-12 ('A Different Scene')"}))
        self.assertEqual(row.perceptual_disagreement,
                         "south-box says r-12 ('A Different Scene')")

    def test_the_ordinary_identified_row_names_no_disagreement(self):
        row = to_row(_identified_item())
        self.assertIsNone(row.perceptual_disagreement)

    def test_a_scored_row_never_carries_a_perceptual_disagreement(self):
        row = to_row(_item())
        self.assertIsNone(row.perceptual_disagreement)

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


# -- description proposals ------------------------------------------------- #


def _description_item(**over):
    item = {"fingerprint": "fp-d", "state": "new",
            "subject_type": "performer", "subject_id": "7",
            "summary": "Wren Alderly: description contains markup",
            "confidence": None, "prior_state": None,
            "payload": {"name": "Wren Alderly", "field": "details",
                        "faults": ["markup", "entity"],
                        "original": "<p>Marsh &amp; Holloway.</p>",
                        "cleaned": "Marsh & Holloway."}}
    item.update(over)
    return item


# -- tag-description proposals ---------------------------------------------- #
#
# Every tag name and description below is invented.

TAG_DESCRIPTION = "Scenes lit only by a hand-carried lamp."


def _tag_description_item(**over):
    """One tag-description item, built through
    `cronicled.tag_descriptions.proposal` rather than hand-written, so the row
    builder cannot be tested against a payload shape nothing ever emits."""
    built = tag_description_proposal(
        {"id": "7", "name": "Lantern Work", "aliases": [],
         "description": None, "scene_count": 3},
        Found(description=TAG_DESCRIPTION, box="first"), folder="library")
    item = {"fingerprint": "fp-t", "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "summary": built["summary"],
            "confidence": None, "payload": built["payload"],
            "prior_state": None}
    item.update(over)
    return item


class TagDescriptionRowShape(unittest.TestCase):
    def test_the_whole_row(self):
        # The WHOLE dataclass, not sampled fields: a field added without
        # anyone deciding to add it -- an `undoable` on a state that cannot
        # undo, most of all -- is exactly what a field-by-field check cannot
        # see.
        self.assertEqual(asdict(to_tag_description_row(
            _tag_description_item(), base_url="http://media.example")), {
            "kind": KIND_TAG_DESCRIPTION,
            "fingerprint": "fp-t",
            "state": "new",
            "subject_id": "7",
            "name": "Lantern Work",
            "tag_url": "http://media.example/tags/7",
            "source_box": "first",
            "original": "",
            "description": TAG_DESCRIPTION,
            "undoable": False,
            "actionable": True,
            "error": None,
        })

    def test_the_source_is_on_the_row_and_not_only_in_the_summary(self):
        # HARM: a reviewer can tell whether a sentence reads well; they
        # cannot tell whether anybody wrote it. Naming the index it came from
        # is the only thing that makes that answerable, so it is a field the
        # page reads rather than prose it has to parse.
        self.assertEqual(to_tag_description_row(_tag_description_item()).source_box,
                         "first")

    def test_a_payload_missing_any_field_raises_rather_than_showing_blank(self):
        # `source_box` is the expensive one: a provenance that silently
        # rendered blank looks exactly like a sentence with no author, which
        # is the one thing this proposal exists to be able to deny.
        for missing in ("name", "original", "description", "source_box"):
            with self.subTest(missing=missing):
                payload = dict(_tag_description_item()["payload"])
                del payload[missing]
                with self.assertRaises(KeyError):
                    to_tag_description_row(
                        _tag_description_item(payload=payload))

    def test_an_applied_row_with_a_snapshot_offers_its_undo(self):
        row = to_tag_description_row(_tag_description_item(
            state="applied", prior_state={"description": None}))
        self.assertTrue(row.undoable)
        self.assertFalse(row.actionable)

    def test_an_applied_row_with_no_snapshot_offers_no_undo(self):
        # HARM: `revert_tag_description` raises on an empty snapshot, so the
        # button would promise an undo the code cannot perform.
        row = to_tag_description_row(_tag_description_item(
            state="applied", prior_state=None))
        self.assertFalse(row.undoable)

    def test_no_server_configured_leaves_the_link_off_rather_than_broken(self):
        self.assertIsNone(to_tag_description_row(_tag_description_item()).tag_url)


class TagUrl(unittest.TestCase):
    def test_it_addresses_the_tag_page_and_not_a_scene_or_a_performer(self):
        # HARM: a link to the wrong subject kind renders as an ordinary
        # working link and lands on somebody else's record.
        self.assertEqual(tag_url("http://media.example", "7"),
                         "http://media.example/tags/7")

    def test_no_base_url_is_no_link(self):
        self.assertIsNone(tag_url(None, "7"))


# -- tag-merge proposals ---------------------------------------------------- #


def _merge_item(**over):
    """One tag-merge item, shaped as `cronicled.tags.proposal` really builds
    it -- via that function on a real cluster, never hand-written.

    A hand-written payload literal is how a row builder comes to read a shape
    the producer does not emit, and nothing raises: the field is simply
    missing. Building the fixture through the producer's own function means
    this cannot drift from what `to_merge_row` will actually be handed.
    """
    tags = over.pop("tags", [
        {"id": "1", "name": "Velvet Crane", "aliases": [], "description": None, "scene_count": 12},
        {"id": "9", "name": "VelvetCrane", "aliases": [], "description": None, "scene_count": 4},
    ])
    built = tag_proposal(cluster_tags(tags)[0], "library", [])
    item = {"fingerprint": "fp-m", "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "summary": built["summary"],
            "confidence": None, "payload": built["payload"],
            "prior_state": None}
    item.update(over)
    return item


class ToDescriptionRowTest(unittest.TestCase):
    def test_it_carries_both_texts_so_there_is_something_to_judge_against(self):
        # THE row's reason for existing. A row carrying only the cleaned value
        # asks a reviewer to approve a write to a field whose previous
        # contents they cannot see -- so both are asserted, in full, rather
        # than one of them plus "the tag is gone".
        row = to_description_row(_description_item())

        self.assertEqual(row.original, "<p>Marsh &amp; Holloway.</p>")
        self.assertEqual(row.cleaned, "Marsh & Holloway.")
        self.assertEqual(row.name, "Wren Alderly")
        self.assertEqual(row.faults, ("markup", "entity"))
        self.assertEqual(row.kind, KIND_DESCRIPTION)

    def test_a_payload_missing_either_text_raises_rather_than_showing_blank(self):
        # An empty "before" panel reads as "this field was empty and is being
        # filled in" -- the one reading that gets a destructive rewrite
        # approved without a second look.
        for missing in ("original", "cleaned", "name", "faults"):
            with self.subTest(missing=missing):
                payload = dict(_description_item()["payload"])
                del payload[missing]
                with self.assertRaises(KeyError):
                    to_description_row(_description_item(payload=payload))

    def test_it_links_to_the_performer_not_to_a_scene(self):
        row = to_description_row(_description_item(),
                                 base_url="http://media.example")
        self.assertEqual(row.performer_url, "http://media.example/performers/7")

    def test_no_configured_server_leaves_the_link_out(self):
        self.assertIsNone(to_description_row(_description_item()).performer_url)

    def test_an_applied_row_with_a_snapshot_offers_undo(self):
        row = to_description_row(_description_item(
            state="applied", prior_state={"details": "<p>x</p>"}))
        self.assertTrue(row.undoable)
        self.assertFalse(row.actionable)

    def test_an_applied_row_without_a_snapshot_offers_no_undo(self):
        # `revert_performer_description` raises on an empty snapshot, so the
        # button would promise something the code cannot do.
        row = to_description_row(_description_item(state="applied"))
        self.assertFalse(row.undoable)

    def test_a_failed_row_keeps_its_controls_and_says_why(self):
        row = to_description_row(_description_item(
            state="failed", error="server said no"))
        self.assertTrue(row.actionable)
        self.assertEqual(row.error, "server said no")


def _enrichment_item(**over):
    item = {"fingerprint": "fp-e", "state": "new",
            "subject_type": "performer-enrichment", "subject_id": "9",
            "summary": "Wren Alderly: 2 fields from stash-box (by name)",
            "confidence": None, "prior_state": None,
            "payload": {"name": "Wren Alderly",
                        "source": "stash-box (by name)",
                        "fields": {"gender": "FEMALE",
                                   "country": "Freedonia"}}}
    item.update(over)
    return item


class ToEnrichmentRowTest(unittest.TestCase):
    def test_the_whole_row(self):
        # The WHOLE dataclass, not sampled fields -- see `TagDescriptionRow
        # Shape.test_the_whole_row`'s own reasoning for why.
        self.assertEqual(asdict(to_enrichment_row(
            _enrichment_item(), base_url="http://media.example")), {
            "kind": KIND_ENRICHMENT,
            "fingerprint": "fp-e",
            "state": "new",
            "subject_id": "9",
            "name": "Wren Alderly",
            "performer_url": "http://media.example/performers/9",
            "source": "stash-box (by name)",
            "fields": (("gender", "FEMALE"), ("country", "Freedonia")),
            "undoable": False,
            "actionable": True,
            "error": None,
        })

    def test_it_links_to_the_performer_not_to_a_scene(self):
        row = to_enrichment_row(_enrichment_item(),
                                base_url="http://media.example")
        self.assertEqual(row.performer_url,
                         "http://media.example/performers/9")

    def test_no_configured_server_leaves_the_link_out(self):
        self.assertIsNone(to_enrichment_row(_enrichment_item()).performer_url)

    def test_a_payload_missing_any_field_raises_rather_than_showing_blank(self):
        for missing in ("name", "source", "fields"):
            with self.subTest(missing=missing):
                payload = dict(_enrichment_item()["payload"])
                del payload[missing]
                with self.assertRaises(KeyError):
                    to_enrichment_row(_enrichment_item(payload=payload))

    def test_an_applied_row_with_a_snapshot_offers_undo(self):
        row = to_enrichment_row(_enrichment_item(
            state="applied", prior_state={"gender": None, "country": None}))
        self.assertTrue(row.undoable)
        self.assertFalse(row.actionable)

    def test_an_applied_row_without_a_snapshot_offers_no_undo(self):
        # `revert_performer_enrichment` raises on an empty snapshot, so the
        # button would promise something the code cannot do.
        row = to_enrichment_row(_enrichment_item(state="applied"))
        self.assertFalse(row.undoable)

    def test_a_failed_row_keeps_its_controls_and_says_why(self):
        row = to_enrichment_row(_enrichment_item(
            state="failed", error="server said no"))
        self.assertTrue(row.actionable)
        self.assertEqual(row.error, "server said no")


class ToRowsDispatch(unittest.TestCase):
    def test_each_item_becomes_the_row_its_own_subject_type_needs(self):
        # HARM: a description item put through the scene builder raises on
        # `payload["path"]`, taking out the whole page for every other row on
        # it. The reverse -- a scene row drawn with the description shape --
        # would show a block with no filename, no title and no score, which is
        # a proposal a person cannot judge and can still approve.
        rows = to_rows([_item(fingerprint="fp-s"),
                        _description_item(fingerprint="fp-d"),
                        _enrichment_item(fingerprint="fp-e"),
                        _tag_description_item(fingerprint="fp-t")])

        self.assertEqual([r.kind for r in rows],
                         [KIND_SCENE, KIND_DESCRIPTION, KIND_ENRICHMENT,
                          KIND_TAG_DESCRIPTION])
        self.assertEqual([r.fingerprint for r in rows],
                         ["fp-s", "fp-d", "fp-e", "fp-t"])

    def test_an_enrichment_item_never_falls_through_to_the_scene_builder(self):
        # HARM: the `else` branch is the scene builder, which INDEXES
        # `payload["path"]`. An enrichment item reaching it raises and takes
        # out the whole page for every other row on it.
        row = to_rows([_enrichment_item()])[0]

        self.assertEqual(row.source, "stash-box (by name)")
        self.assertFalse(hasattr(row, "filename"))

    def test_a_tag_description_never_falls_through_to_the_scene_builder(self):
        # HARM: the `else` branch is the scene builder, which INDEXES
        # `payload["path"]`. A tag-description item reaching it raises and
        # takes out the whole page for every other row on it -- so the third
        # branch is asserted by the shape it produces, not only by the kind
        # string it carries.
        row = to_rows([_tag_description_item()])[0]

        self.assertEqual(row.source_box, "first")
        self.assertFalse(hasattr(row, "filename"))

    def test_the_base_url_reaches_both_kinds_of_row(self):
        rows = to_rows([_item(subject_id="3"), _description_item()],
                       base_url="http://media.example")
        self.assertEqual(rows[0].scene_url, "http://media.example/scenes/3")
        self.assertEqual(rows[1].performer_url,
                         "http://media.example/performers/7")

    def test_a_scene_row_still_says_which_kind_it_is(self):
        # Without this the template's branch reads an undefined attribute for
        # every scene row -- which Jinja renders as empty text rather than
        # raising, so it would take the description branch's `else` by luck.
        self.assertEqual(to_row(_item()).kind, KIND_SCENE)


_THREE_SPELLINGS = [
    {"id": "1", "name": "IvyMayKingsley", "aliases": [], "description": None, "scene_count": 1},
    {"id": "2", "name": "Ivy MayKingsley", "aliases": [], "description": None, "scene_count": 2},
    {"id": "3", "name": "Ivy May Kingsley", "aliases": [], "description": None, "scene_count": 3},
]


class MergeRowShape(unittest.TestCase):
    """A merge row is not a scene row, and the differences are the point."""

    def test_the_whole_row_for_a_decided_cluster(self):
        # The WHOLE dataclass, not sampled fields. A field added here without
        # anyone deciding to add it -- an `undoable`, most of all -- is
        # exactly what a field-by-field check cannot see.
        self.assertEqual(asdict(to_merge_row(_merge_item())), {
            "fingerprint": "fp-m",
            "state": "new",
            "subject_type": "tag-cluster",
            "subject_id": "velvetcrane",
            "key": "velvetcrane",
            "members": (
                {"id": "1", "name": "Velvet Crane", "scene_count": 12,
                 "description": None},
                {"id": "9", "name": "VelvetCrane", "scene_count": 4,
                 "description": None},
            ),
            "canonical": "Velvet Crane",
            "losing": ("VelvetCrane",),
            "undecided": None,
            "inherit": None,
            "inherit_from_tag": None,
            "inherit_from_box": None,
            "conflicting": (),
            "total_scenes": 16,
            "counts_cover": "scenes",
            "warning": MERGE_IS_IRREVERSIBLE,
            "appliable": True,
            "actionable": True,
            "undismissable": False,
            "unmutable": False,
            "error": None,
        })

    def test_the_description_a_survivor_would_inherit_reaches_the_row(self):
        # HARM: the merge deletes the only spelling carrying the text. A row
        # that did not show it asks a person to approve an irreversible write
        # without showing them what it moves.
        item = _merge_item(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": None, "scene_count": 12},
            {"id": "9", "name": "LanternWork", "aliases": [],
             "description": TAG_DESCRIPTION, "scene_count": 4}])

        row = to_merge_row(item)

        self.assertEqual(row.inherit, TAG_DESCRIPTION)
        self.assertEqual(row.inherit_from_tag, "LanternWork")
        self.assertIsNone(row.inherit_from_box)
        self.assertEqual(row.conflicting, ())

    def test_two_differing_descriptions_both_reach_the_row_and_neither_wins(self):
        item = _merge_item(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": TAG_DESCRIPTION, "scene_count": 12},
            {"id": "9", "name": "LanternWork", "aliases": [],
             "description": "Filmed aboard a working passenger boat.",
             "scene_count": 4}])

        row = to_merge_row(item)

        self.assertIsNone(row.inherit)
        self.assertEqual([c["name"] for c in row.conflicting],
                         ["Lantern Work", "LanternWork"])

    def test_a_merge_recorded_before_descriptions_existed_still_renders(self):
        # HARM: those rows are in the store and on the page. Indexed, the
        # missing block takes the whole Merges section down with a KeyError;
        # an empty block is the truthful reading -- that proposal asked no
        # source anything and found no description at risk.
        item = _merge_item()
        del item["payload"]["description"]

        row = to_merge_row(item)

        self.assertIsNone(row.inherit)
        self.assertEqual(row.conflicting, ())

    def test_the_whole_row_for_an_undecided_cluster(self):
        row = asdict(to_merge_row(_merge_item(tags=_THREE_SPELLINGS)))

        self.assertEqual(row["canonical"], None)
        self.assertEqual(row["undecided"], UNDECIDED_MANY)
        # Nothing is losing, because nothing has been decided.
        self.assertEqual(row["losing"], ())
        self.assertEqual(row["total_scenes"], 6)
        # And no Merge control: there is no surviving spelling to merge into,
        # so offering the button would ask a person to authorise a write
        # nothing has specified.
        self.assertFalse(row["appliable"])
        # Dismiss and Mute are still theirs to press.
        self.assertTrue(row["actionable"])

    def test_a_merge_row_has_no_undoable_field_at_all(self):
        # Not "undoable is False": the FIELD is absent, so no template can
        # read one and no future edit can set one to True by accident. A
        # merge cannot be reversed (see `tags.MERGE_IS_IRREVERSIBLE`), and
        # the cheapest guarantee is that there is nothing there to read.
        self.assertNotIn("undoable", asdict(to_merge_row(_merge_item())))
        self.assertFalse(hasattr(to_merge_row(_merge_item()), "undoable"))

    def test_the_warning_says_the_write_cannot_be_undone(self):
        # A property of the text, not a comparison with the constant the code
        # built the row from -- both sides of that move together.
        self.assertIn("cannot be undone", to_merge_row(_merge_item()).warning)

    def test_the_warning_is_carried_after_the_merge_too(self):
        # The sources are gone by then, and a page that stopped saying so
        # would leave a person hunting for the Undo that is not there.
        row = to_merge_row(_merge_item(state="applied"))
        self.assertIn("cannot be undone", row.warning)

    def test_an_applied_merge_offers_no_controls(self):
        row = to_merge_row(_merge_item(state="applied"))
        self.assertFalse(row.appliable)
        self.assertFalse(row.actionable)
        self.assertFalse(row.undismissable)
        self.assertFalse(row.unmutable)

    def test_a_dismissed_merge_offers_only_its_reversal(self):
        row = to_merge_row(_merge_item(state="dismissed"))
        self.assertTrue(row.undismissable)
        self.assertFalse(row.actionable)
        self.assertFalse(row.unmutable)

    def test_a_muted_merge_offers_only_its_reversal(self):
        row = to_merge_row(_merge_item(state="muted"))
        self.assertTrue(row.unmutable)
        self.assertFalse(row.actionable)
        self.assertFalse(row.undismissable)

    def test_a_failed_merge_still_has_a_decision_left_in_it(self):
        # Stated as "not closed" rather than as a list of open states, so a
        # state added later inherits its controls instead of silently losing
        # them -- which is how a failed scene row became a dead end once.
        row = to_merge_row(_merge_item(state="failed", error="server said no"))
        self.assertTrue(row.actionable)
        self.assertTrue(row.appliable)
        self.assertEqual(row.error, "server said no")

    def test_a_payload_missing_a_scene_count_raises(self):
        item = _merge_item()
        del item["payload"]["members"][0]["scene_count"]
        with self.assertRaises(KeyError):
            to_merge_row(item)

    def test_to_merge_rows_converts_every_item_and_keeps_their_order(self):
        rows = to_merge_rows([_merge_item(fingerprint="a"),
                              _merge_item(fingerprint="b")])
        self.assertEqual([r.fingerprint for r in rows], ["a", "b"])


# Every value below was computed by hand from the zone's own offsets and is
# written out in full, never derived by calling the code under test. Madrid is
# +01:00 in winter and +02:00 in summer; its 2026 transitions are 29 March
# (02:00 -> 03:00) and 25 October (03:00 -> 02:00).
_WINTER_UTC = "2026-01-15T00:30:00+00:00"
_WINTER_LOCAL = "2026-01-15T01:30:00+01:00"
_SUMMER_UTC = "2026-07-15T00:30:00+00:00"
_SUMMER_LOCAL = "2026-07-15T02:30:00+02:00"
# The repeated hour, both readings of it. TWO DIFFERENT INSTANTS an hour apart
# whose local wall clocks both say 02:30, told apart by nothing but the offset.
_REPEATED_FIRST_UTC = "2026-10-25T00:30:00+00:00"
_REPEATED_FIRST_LOCAL = "2026-10-25T02:30:00+02:00"
_REPEATED_SECOND_UTC = "2026-10-25T01:30:00+00:00"
_REPEATED_SECOND_LOCAL = "2026-10-25T02:30:00+01:00"


class LocalTimestamps(unittest.TestCase):
    """`local` relabels a stored UTC instant in the configured zone. It never
    moves the instant, and nothing it returns goes back into a write.
    """

    def test_a_winter_stamp_takes_the_winter_offset(self):
        self.assertEqual(local(_WINTER_UTC, ZONE), _WINTER_LOCAL)

    def test_a_summer_stamp_takes_the_summer_offset(self):
        # The pair is the test. One date cannot tell a zone's offset from a
        # constant, however exactly the expected value is written out: an
        # implementation adding a fixed hour passes the winter case above and
        # fails here, and one adding two passes here and fails there.
        self.assertEqual(local(_SUMMER_UTC, ZONE), _SUMMER_LOCAL)

    def test_the_two_offsets_it_produces_actually_differ(self):
        # The discriminating assertion behind the pair, stated rather than
        # left implicit: if these two ever agreed, both tests above would be
        # pinning one offset twice and the zone would be doing nothing.
        self.assertNotEqual(local(_WINTER_UTC, ZONE)[-6:],
                            local(_SUMMER_UTC, ZONE)[-6:])

    def test_an_instant_that_crosses_midnight_moves_the_date_too(self):
        # 23:30 UTC on the 14th is 01:30 on the 15th in a zone two hours
        # ahead. A renderer that swapped the offset text without converting
        # would leave the date on the 14th and read plausibly.
        self.assertEqual(local("2026-07-14T23:30:00+00:00", ZONE),
                         "2026-07-15T01:30:00+02:00")

    def test_the_hour_a_clock_repeats_stays_two_distinguishable_instants(self):
        # WHY STORAGE STAYS UTC, shown rather than argued. These are two real
        # instants an hour apart. Their local wall clocks are the same text.
        # What still tells them apart after conversion is the offset -- and
        # that is precisely what a local time written into the database would
        # not have kept, leaving two rows nothing could order.
        first = local(_REPEATED_FIRST_UTC, ZONE)
        second = local(_REPEATED_SECOND_UTC, ZONE)
        self.assertEqual(first, _REPEATED_FIRST_LOCAL)
        self.assertEqual(second, _REPEATED_SECOND_LOCAL)
        self.assertEqual(first[:19], second[:19])
        self.assertNotEqual(first, second)

    def test_the_instant_is_never_moved_only_relabelled(self):
        # Not a vacuous `x == convert(x)` shape: the two sides are read by
        # different means -- one is the string this produced, parsed back; the
        # other is the stamp that went in, parsed by the store's own rule.
        self.assertEqual(
            datetime.fromisoformat(local(_SUMMER_UTC, ZONE)),
            datetime.fromisoformat(_SUMMER_UTC))

    def test_a_stamp_with_no_offset_is_shown_exactly_as_stored(self):
        # It names no instant, so converting it would mean assuming one and
        # then shifting it -- putting an hour on the page that was never
        # recorded anywhere. Verbatim is the honest answer, and it looks
        # unlike the rows around it, which is the signal that matters.
        self.assertEqual(local("2026-07-15T00:30:00", ZONE),
                         "2026-07-15T00:30:00")

    def test_something_that_is_not_a_timestamp_comes_back_untouched(self):
        self.assertEqual(local("never", ZONE), "never")
        self.assertIsNone(local(None, ZONE))

    def test_a_datetime_is_accepted_as_well_as_a_string(self):
        self.assertEqual(
            local(datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc), ZONE),
            _SUMMER_LOCAL)

    def test_the_zone_is_required_and_cannot_be_forgotten_into_utc(self):
        # A default would hand a caller who forgot it UTC, which is the exact
        # output of the conversion not existing -- so the mistake would look
        # like the feature working.
        with self.assertRaises(TypeError):
            local(_SUMMER_UTC)


class LocalTimestampsInsideProse(unittest.TestCase):
    """`local_times` rewrites the instants inside the sentences the scheduler
    hands the page ("last ran ...; next due at ..."), because a page where one
    line reads in the operator's hour and the next reads in UTC is worse than
    one that reads entirely in UTC: the reader cannot tell which is which.
    """

    def test_every_instant_in_a_sentence_is_converted_not_just_the_first(self):
        # BOTH, and this is the shape of the failure the class exists for: a
        # substitution that stopped after one match would leave the sentence
        # half in each zone and read perfectly well.
        self.assertEqual(
            local_times("last ran %s; next due at %s"
                        % (_WINTER_UTC, _SUMMER_UTC), ZONE),
            "last ran %s; next due at %s" % (_WINTER_LOCAL, _SUMMER_LOCAL))

    def test_the_text_around_them_is_left_alone(self):
        self.assertEqual(
            local_times("last ran %s; next due at %s" % (_SUMMER_UTC,
                                                         _SUMMER_UTC), ZONE),
            "last ran %s; next due at %s" % (_SUMMER_LOCAL, _SUMMER_LOCAL))

    def test_a_reason_with_no_instant_in_it_is_unchanged(self):
        self.assertEqual(local_times("disabled by override", ZONE),
                         "disabled by override")
        self.assertEqual(
            local_times("cost class saturated: already running as job 3", ZONE),
            "cost class saturated: already running as job 3")

    def test_a_naive_stamp_inside_prose_is_left_as_it_was_written(self):
        # The same rule `local` applies to a field: a stamp with no offset
        # names no instant. Here it also protects against rewriting something
        # that is not a timestamp at all, such as a version or a path.
        self.assertEqual(local_times("last ran 2026-07-15T00:30:00", ZONE),
                         "last ran 2026-07-15T00:30:00")

    def test_an_instant_written_with_z_is_converted_too(self):
        self.assertEqual(local_times("last ran 2026-07-15T00:30:00Z", ZONE),
                         "last ran %s" % _SUMMER_LOCAL)

    def test_nothing_and_an_empty_string_survive(self):
        self.assertIsNone(local_times(None, ZONE))
        self.assertEqual(local_times("", ZONE), "")


class MuteRowTimestamps(unittest.TestCase):
    def _entry(self, at):
        return {"subject_type": "scene", "subject_id": "1",
                "reason": "never identifiable", "at": at, "item": None}

    def test_the_muted_time_is_shown_in_the_configured_zone(self):
        self.assertEqual(to_mute_row(self._entry(_SUMMER_UTC),
                                     zone=ZONE)["at"], _SUMMER_LOCAL)

    def test_it_holds_across_a_transition_and_not_on_one_offset(self):
        self.assertEqual(to_mute_row(self._entry(_WINTER_UTC),
                                     zone=ZONE)["at"], _WINTER_LOCAL)

    def test_every_row_is_converted_and_not_only_the_first(self):
        # A counting fixture of one cannot tell "converts the list" from
        # "converts the head of the list", so there are two -- and they carry
        # DIFFERENT stamps, so a builder that converted the first and reused
        # its answer for the rest would fail here as well.
        rows = to_mute_rows([self._entry(_WINTER_UTC),
                             self._entry(_SUMMER_UTC)], zone=ZONE)
        self.assertEqual([r["at"] for r in rows],
                         [_WINTER_LOCAL, _SUMMER_LOCAL])


class _Declared:
    """A producer as `resolve` reads one: a name, and whatever timing it
    declares for itself."""

    def __init__(self, name, every=None, at=None, zone=None):
        self.name = name
        self.every = every
        self.at = at
        self.zone = zone


def _entries(*producers, overrides=None):
    """The resolved schedule the loop hands over, built by the REAL `resolve`.

    Hand-written `Entry` literals would be a fixture more capable than the
    thing it stands in for: `resolve` refuses an entry naming both a cadence
    and a time, a time with no zone, and a producer with no schedule at all,
    so a literal could state a schedule the loop can never be holding and the
    view would be tested against it. The three defaults are the three
    unattended passes, at the three staggered times `cronicled.__main__`
    really declares.
    """
    if not producers:
        producers = (
            _Declared("nightly-library-scan", at=time(3, 0), zone=ZONE),
            _Declared("performer-descriptions", at=time(3, 20), zone=ZONE),
            _Declared("tag-merge", at=time(3, 40), zone=ZONE),
        )
    return resolve(producers, overrides)


def _status(**over):
    """A `LoopStatus` the loop could really have produced, built from the real
    dataclasses rather than a hand-written stand-in.

    A double here would be free to carry a field `LoopStatus` does not have --
    or to lack one it does -- and `to_schedule_view` uses
    `dataclasses.replace`, which would then either raise in production or
    silently pass a field through unconverted in the test.
    """
    fields = dict(
        running=True, closed=False, ticks=7, failures=1,
        consecutive_failures=0,
        appointments=_entries(),
        last_tick_at=_SUMMER_UTC,
        last_error="ValueError: nothing passed by %s" % _WINTER_UTC,
        last_error_at=_WINTER_UTC,
        last_traceback="Traceback...\n  at %s" % _WINTER_UTC,
        failing_to_start={"tag-merge": 3},
        last_result=TickResult(
            at=_SUMMER_UTC,
            due=["nightly-library-scan"],
            started={"nightly-library-scan": "job-1"},
            skipped={"performer-descriptions":
                     "last ran %s; next due at %s" % (_WINTER_UTC,
                                                      _SUMMER_UTC),
                     "tag-merge": "disabled by override"},
            failed_to_start={"tag-merge":
                             "RuntimeError: closed at %s" % _WINTER_UTC}),
    )
    fields.update(over)
    return LoopStatus(**fields)


class ScheduleViewTimestamps(unittest.TestCase):
    """Every time the Schedule section shows, in the configured zone.

    FIVE places carry one, not one place, and they are asserted together for
    the reason the acceptance criterion names: a test reading `last_tick_at`
    alone would pass while every reason underneath it stayed in UTC, which is
    the half an operator actually reads when asking why something has not run.
    """

    def test_the_last_tick_is_shown_in_the_zone(self):
        self.assertEqual(to_schedule_view(_status(), zone=ZONE).last_tick_at,
                         _SUMMER_LOCAL)

    def test_the_last_error_time_is_shown_in_the_zone(self):
        self.assertEqual(to_schedule_view(_status(), zone=ZONE).last_error_at,
                         _WINTER_LOCAL)

    def test_the_error_message_carries_its_own_instant_converted(self):
        # `schedule._previous_occurrence` and `due` both name an instant in
        # the message they raise with, and the page prints that message beside
        # the time it happened -- the two disagreeing by an offset is the
        # confusion this whole change exists to remove.
        self.assertEqual(to_schedule_view(_status(), zone=ZONE).last_error,
                         "ValueError: nothing passed by %s" % _WINTER_LOCAL)

    def test_the_moment_the_tick_decided_against_is_shown_in_the_zone(self):
        view = to_schedule_view(_status(), zone=ZONE)
        self.assertEqual(view.last_result.at, _SUMMER_LOCAL)

    def test_the_reason_a_producer_was_skipped_is_shown_in_the_zone(self):
        view = to_schedule_view(_status(), zone=ZONE)
        self.assertEqual(
            view.last_result.skipped,
            {"performer-descriptions":
             "last ran %s; next due at %s" % (_WINTER_LOCAL, _SUMMER_LOCAL),
             "tag-merge": "disabled by override"})

    def test_the_reason_a_producer_would_not_start_is_shown_in_the_zone(self):
        view = to_schedule_view(_status(), zone=ZONE)
        self.assertEqual(view.last_result.failed_to_start,
                         {"tag-merge":
                          "RuntimeError: closed at %s" % _WINTER_LOCAL})

    def test_no_section_is_left_in_utc(self):
        # The whole-shape assertion behind the six above: every field of the
        # view, at once, against values computed by hand. A field added to
        # `LoopStatus` and forgotten here shows up as a difference rather than
        # as a line that quietly stayed in UTC on the page.
        view = to_schedule_view(_status(), zone=ZONE)
        self.assertEqual(asdict(view), {
            "running": True, "closed": False, "ticks": 7, "failures": 1,
            "consecutive_failures": 0,
            "last_tick_at": _SUMMER_LOCAL,
            "last_error": "ValueError: nothing passed by %s" % _WINTER_LOCAL,
            "last_error_at": _WINTER_LOCAL,
            # The one field deliberately NOT converted, and the only one the
            # template does not render: frames are read as they were recorded.
            "last_traceback": "Traceback...\n  at %s" % _WINTER_UTC,
            # The declaration, phrased rather than relabelled: three stated
            # hours, no dates, no seconds, no offsets, and the zone said once
            # for the whole panel instead of on every line.
            "appointments": {
                "zone": "Europe/Madrid",
                "producers": (
                    {"name": "nightly-library-scan", "when": "03:00"},
                    {"name": "performer-descriptions", "when": "03:20"},
                    {"name": "tag-merge", "when": "03:40"},
                ),
            },
            "failing_to_start": {"tag-merge": 3},
            "last_result": {
                "at": _SUMMER_LOCAL,
                "due": ["nightly-library-scan"],
                "started": {"nightly-library-scan": "job-1"},
                "skipped": {
                    "performer-descriptions":
                        "last ran %s; next due at %s" % (_WINTER_LOCAL,
                                                         _SUMMER_LOCAL),
                    "tag-merge": "disabled by override"},
                "failed_to_start": {
                    "tag-merge":
                        "RuntimeError: closed at %s" % _WINTER_LOCAL},
            },
        })

    def test_the_loops_own_record_is_not_touched(self):
        # The direction that matters: this builds a copy for the page. The
        # values the scheduler goes on comparing against the store stay in UTC
        # however often the page is drawn.
        status = _status()
        to_schedule_view(status, zone=ZONE)
        self.assertEqual(status.last_tick_at, _SUMMER_UTC)
        self.assertEqual(status.last_result.at, _SUMMER_UTC)
        self.assertEqual(status.last_result.skipped["performer-descriptions"],
                         "last ran %s; next due at %s" % (_WINTER_UTC,
                                                          _SUMMER_UTC))

    def test_the_same_type_comes_back_so_the_template_keeps_every_field(self):
        view = to_schedule_view(_status(), zone=ZONE)
        self.assertIsInstance(view, LoopStatus)
        self.assertIsInstance(view.last_result, TickResult)

    def test_nothing_scheduled_stays_nothing_scheduled(self):
        # `None` is the answer for an install with no media server, which the
        # page says out loud rather than drawing as a healthy idle schedule.
        self.assertIsNone(to_schedule_view(None, zone=ZONE))

    def test_a_loop_that_has_not_ticked_yet_converts_nothing_and_survives(self):
        view = to_schedule_view(
            _status(last_tick_at=None, last_error=None, last_error_at=None,
                    last_traceback=None, last_result=None), zone=ZONE)
        self.assertIsNone(view.last_tick_at)
        self.assertIsNone(view.last_error_at)
        self.assertIsNone(view.last_result)
        self.assertEqual(view.ticks, 7)

    def test_a_loop_that_has_not_ticked_yet_still_states_its_appointments(self):
        # THE FIELD THAT IS NOT A DIAGNOSTIC. Every other field here is an
        # observation about ticks, and before the first tick they are all
        # empty; the schedule is true already, and it is the only thing on the
        # panel worth reading at that moment.
        view = to_schedule_view(
            _status(last_tick_at=None, last_error=None, last_error_at=None,
                    last_traceback=None, last_result=None), zone=ZONE)
        self.assertEqual([p.when for p in view.appointments.producers],
                         ["03:00", "03:20", "03:40"])


# An hour and a minute, and nothing else. Written as a pattern rather than as
# three separate "no date"/"no seconds"/"no offset" assertions because it is
# ONE rule -- and because a pattern goes on holding for a fourth producer that
# nobody thought to add an assertion for.
_STATED_HOUR = re.compile(r"^\d{2}:\d{2}$")


class AStatedAppointmentShortensToAnHourAndAMinute(unittest.TestCase):
    """The half of the boundary this ticket adds.

    A recorded instant keeps its date (`local_minute`, and the tests over it in
    tests/test_web_render.py); a STATED appointment has no date to keep, and
    carrying one would invent it. The two must not be confused in either
    direction.
    """

    def view(self, *producers, page_zone=ZONE, **over):
        status = _status(appointments=_entries(*producers), **over)
        return to_schedule_view(status, zone=page_zone).appointments

    def test_a_stated_time_is_an_hour_and_a_minute(self):
        # 03:00 rather than 3:0: both zero-pads are load-bearing and one
        # fixture kills either mutation, because both fields are single-digit.
        self.assertEqual(
            self.view(_Declared("nightly", at=time(3, 0), zone=ZONE)).producers,
            (ScheduledProducer(name="nightly", when="03:00"),))

    def test_the_minute_shown_is_the_producers_own(self):
        # A second fixture whose minute is not zero. Without it, a renderer
        # that printed the hour and a literal ":00" would pass every
        # assertion above and put all three unattended passes at the top of
        # the hour on a page an operator uses to check the stagger.
        self.assertEqual(
            self.view(_Declared("late", at=time(3, 20), zone=ZONE))
            .producers[0].when, "03:20")

    def test_no_date_no_seconds_and_no_offset_reach_the_page(self):
        # The three ways this could over-state a declaration, as one rule.
        # An appointment has no date, so any date on it is invented; the
        # seconds and the offset are noise on a fact stated to the minute.
        for stated in self.view().producers:
            self.assertRegex(stated.when, _STATED_HOUR)

    def test_the_appointment_is_stated_in_its_own_zone_not_the_pages(self):
        # The page's zone converts INSTANTS. An appointment is not an instant:
        # "03:00 in Madrid" is a wall-clock time, and turning it into another
        # zone needs a date this does not have and must not invent -- so the
        # hour is unmoved and the zone reported is the one it was declared in,
        # even when the page is being drawn for a reader somewhere else.
        appointments = self.view(page_zone=ZoneInfo("America/New_York"))
        self.assertEqual(appointments.zone, "Europe/Madrid")
        self.assertEqual(appointments.producers[0].when, "03:00")

    def test_it_does_not_move_with_the_date_the_loop_last_ticked_on(self):
        # THE DAYLIGHT-SAVING TRAP. Madrid is +01:00 in January and +02:00 in
        # July, so an implementation that reached for a date -- the tick's,
        # today's, any -- and converted through it would show two different
        # hours for one declaration, and neither of them 03:00. Both sides of
        # the transition, and the stated value written out by hand.
        winter = self.view(last_tick_at=_WINTER_UTC)
        summer = self.view(last_tick_at=_SUMMER_UTC)
        self.assertEqual([p.when for p in winter.producers],
                         ["03:00", "03:20", "03:40"])
        self.assertEqual([p.when for p in summer.producers],
                         [p.when for p in winter.producers])

    def test_the_zone_is_named_once_and_never_on_a_line(self):
        appointments = self.view()
        self.assertEqual(appointments.zone, "Europe/Madrid")
        self.assertEqual(len(appointments.producers), 3)
        for stated in appointments.producers:
            self.assertNotIn("Madrid", stated.when)

    def test_the_producers_are_listed_by_name_not_as_they_were_registered(self):
        # By CONTENT, so the panel reads the same on every visit. The two
        # fixtures are deliberately in the opposite order alphabetically and
        # by appointment, so a sort on either the registry's order or on the
        # hour fails here rather than agreeing by luck.
        appointments = self.view(_Declared("zulu", at=time(3, 0), zone=ZONE),
                                 _Declared("alpha", at=time(4, 0), zone=ZONE))
        self.assertEqual([(p.name, p.when) for p in appointments.producers],
                         [("alpha", "04:00"), ("zulu", "03:00")])

    def test_nothing_scheduled_lists_nothing_and_claims_no_zone(self):
        self.assertEqual(self.view(_Declared("only", every=60)),
                         Appointments(zone=None, producers=(
                             ScheduledProducer(name="only",
                                               when="every 60s"),)))

    def test_the_loops_own_schedule_is_not_rewritten_by_drawing_the_page(self):
        # The direction `to_schedule_view` takes everywhere: this builds a
        # copy for a reader, and what the scheduler goes on comparing against
        # the store is untouched however often the page is drawn.
        status = _status()
        to_schedule_view(status, zone=ZONE)
        self.assertEqual(status.appointments["tag-merge"],
                         Entry(producer="tag-merge", every=None, enabled=True,
                               at=time(3, 40), zone=ZONE))


class AnIntervalHasNoAppointmentToShow(unittest.TestCase):
    """A producer declaring a cadence has no hour, and one is not manufactured
    for it out of the last run plus the interval: that is a PREDICTION, and
    printing it where a statement goes makes it wrong the first night a run is
    late. The two read differently instead.
    """

    def when(self, **declared):
        return to_schedule_view(
            _status(appointments=_entries(_Declared("hourly", **declared))),
            zone=ZONE).appointments.producers[0].when

    def test_a_cadence_says_it_is_a_cadence(self):
        self.assertEqual(self.when(every=3600), "every 3600s")

    def test_it_is_not_dressed_up_as_an_hour_of_the_day(self):
        # The rule in its own right, and the one that matters: whatever a
        # cadence reads as, it must not read as a time somebody stated.
        self.assertNotRegex(self.when(every=3600), _STATED_HOUR)

    def test_a_whole_number_of_seconds_loses_its_decimal_point(self):
        self.assertEqual(self.when(every=3600.0), "every 3600s")

    def test_a_fractional_cadence_keeps_every_digit_it_was_given(self):
        # Nothing is rounded for display. A cadence the page and the scheduler
        # disagree about is a cadence an operator cannot check.
        self.assertEqual(self.when(every=0.5), "every 0.5s")

    def test_a_cadence_contributes_no_zone_because_it_names_none(self):
        self.assertIsNone(to_schedule_view(
            _status(appointments=_entries(_Declared("hourly", every=3600))),
            zone=ZONE).appointments.zone)


class ADisabledProducerIsStillListed(unittest.TestCase):
    """A producer somebody switched off has no appointment and must not
    disappear: a line missing from this panel reads as a producer that was
    never wired up, which is the one thing the panel exists to tell apart."""

    def appointments(self, **over):
        producers = (_Declared("off", at=time(4, 0), zone=ZoneInfo("UTC")),
                     _Declared("on", at=time(3, 0), zone=ZONE))
        return to_schedule_view(
            _status(appointments=_entries(
                *producers, overrides={"off": {"enabled": False}})),
            zone=ZONE).appointments

    def test_it_says_it_is_off_rather_than_naming_an_hour(self):
        self.assertEqual([(p.name, p.when)
                          for p in self.appointments().producers],
                         [("off", "disabled"), ("on", "03:00")])

    def test_its_zone_does_not_decide_the_panels(self):
        # A disabled producer keeps whatever timing it declared -- an override
        # of `{"enabled": false}` alone changes nothing else -- so its zone is
        # still on the entry. It must not be counted: the panel's heading
        # describes the appointments that will actually be kept, and this one
        # will not be.
        self.assertEqual(self.appointments().zone, "Europe/Madrid")


class AnEntryTheSchedulerWouldRefuseIsNotRendered(unittest.TestCase):
    """`resolve` cannot produce any of these, and they are refused again here
    for the reason `due` refuses them again: a page must not state a schedule
    the loop will raise on every tick over. A blank line, or an hour shown for
    a producer nothing can run, would put the failure inside the one panel an
    operator opens to find it.
    """

    def render(self, **fields):
        entry = Entry(producer="broken", **fields)
        return to_schedule_view(_status(appointments={"broken": entry}),
                                zone=ZONE)

    def test_an_enabled_entry_with_no_schedule_at_all_raises(self):
        with self.assertRaisesRegex(ValueError, "neither a cadence"):
            self.render(every=None, at=None)

    def test_an_enabled_entry_naming_both_raises(self):
        with self.assertRaisesRegex(ValueError, "both a cadence"):
            self.render(every=3600, at=time(3, 0), zone=ZONE)

    def test_a_stated_time_with_no_zone_raises_rather_than_guessing_one(self):
        # The host's zone is a property of the deployment, not of the
        # schedule. Rendering "03:00" with no zone named, on a panel whose
        # heading states one, would attribute this appointment to a zone
        # nothing declared it in.
        with self.assertRaisesRegex(ValueError, "no zone"):
            self.render(every=None, at=time(3, 0), zone=None)

    def test_a_disabled_entry_with_no_schedule_is_fine_and_says_so(self):
        # The one shape of the three that IS reachable: `resolve` exempts a
        # producer explicitly disabled from needing a schedule at all.
        view = self.render(every=None, at=None, enabled=False)
        self.assertEqual(view.appointments.producers[0].when, "disabled")


class TwoZonesAreReportedRatherThanOneChosen(unittest.TestCase):
    """An operator's per-producer override can state a time in a zone that is
    not the deployment's. One heading cannot be true of both, and picking
    either by iteration order would hide the disagreement rather than report
    it -- leaving the panel saying 3am about a pass that runs at a different
    3am, which is the confusion one configured zone exists to remove.
    """

    def appointments(self):
        return to_schedule_view(_status(appointments=_entries(
            _Declared("here", at=time(3, 0), zone=ZONE),
            _Declared("elsewhere", at=time(3, 0), zone=ZoneInfo("UTC")))),
            zone=ZONE).appointments

    def test_no_single_zone_is_claimed_for_the_panel(self):
        self.assertIsNone(self.appointments().zone)

    def test_each_line_names_the_zone_its_own_hour_is_read_in(self):
        # Both, and the pair is the test: the two hours are the same text and
        # are hours apart, so a panel that showed them without their zones
        # would be stating that two passes run at the same moment.
        self.assertEqual([(p.name, p.when)
                          for p in self.appointments().producers],
                         [("elsewhere", "03:00 (UTC)"),
                          ("here", "03:00 (Europe/Madrid)")])
