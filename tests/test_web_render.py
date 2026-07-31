import re
import unittest
from zoneinfo import ZoneInfo

from cronicled.jobs import Job
from cronicled.schedule import LoopStatus, TickResult
from cronicled.tag_descriptions import Found
from cronicled.tag_descriptions import proposal as tag_description_proposal
from cronicled.tags import cluster_tags
from cronicled.tags import proposal as tag_proposal
from cronicled.web.render import environment, render
from cronicled.web.rows import (
    to_description_row, to_merge_row, to_mute_row, to_refusal_row, to_row,
    to_schedule_view, to_summary_view, to_tag_description_row,
)

_HOSTILE = '<script>alert("x")</script>'

# A real zone that observes daylight saving, and whose offset differs from UTC
# on both sides of every transition -- see tests/test_web_rows.py for why both
# properties matter to a test of a rendered timestamp.
ZONE = ZoneInfo("Europe/Madrid")


def _job(**over):
    job = dict(id="job-1", producer="library-scan-x", cost="scraping",
               state="running", started_at="2026-07-27T00:00:00+00:00",
               finished_at=None, duration=None, message="working",
               recorded=0, skipped=0, error=None, traceback=None)
    job.update(over)
    return Job(**job)


def _scene_item(runners_up=None, image=None, urls=None, payload_over=None,
                **over):
    # `runners_up`, `image` and `urls` are their own parameters, not folded
    # into `**over`: all three live under `payload` in the real item shape,
    # and `item.update(over)` only ever touches the top level. Routing any
    # of them through `over` would silently keep the default fixture's value
    # on every call that tried to override it -- exactly the kind of
    # blank-column bug this task exists to catch, just moved into the test
    # instead of the template. `image` defaults to `None` -- "no cover",
    # matching every existing caller here that never mentions a cover.
    #
    # `urls` defaults to NOTHING on the candidate, which is a real shape: a
    # name search commonly returns a title and no address at all, and it is
    # the shape that must render as plain text rather than as an anchor
    # pointing nowhere.
    payload = {
        "path": "/library/x/%s.mp4" % _HOSTILE,
        "creator": {"name": _HOSTILE, "source": "folder",
                    "competing": _HOSTILE, "rejected_folder": _HOSTILE},
        "candidate": {"id": "c-1", "title": _HOSTILE, "image": image,
                     "performers": [], "studio": None,
                     "urls": [] if urls is None else urls},
        "score": 0.812,
        # Nested under `candidate`, matching what `scan._runners_up` actually
        # emits and what `rows.to_row` requires -- see test_web_rows.py's
        # `test_a_malformed_runner_up_raises_rather_than_rendering_blank`. A
        # flattened top-level "title" here would raise a KeyError before
        # rendering ever got a chance to run.
        "runners_up": ([{"candidate": {"title": _HOSTILE}, "score": 0.61}]
                        if runners_up is None else runners_up),
    }
    payload.update(payload_over or {})
    item = {"fingerprint": "fp-1", "state": "new", "summary": "s",
            "confidence": 0.812, "payload": payload, "prior_state": None,
            "subject_type": "scene", "subject_id": "1"}
    item.update(over)
    return item


def _row(runners_up=None, image=None, base_url=None, urls=None,
         payload_over=None, **over):
    return to_row(_scene_item(runners_up=runners_up, image=image, urls=urls,
                              payload_over=payload_over, **over),
                  base_url=base_url)


class Autoescaping(unittest.TestCase):
    """The single reason this project has a runtime dependency at all."""

    def test_the_environment_autoescapes(self):
        # Asserted as the environment's DECISION for the template actually
        # rendered, not as the truthiness of the `autoescape` attribute.
        # `select_autoescape` returns a callable, so `assertTrue` on the
        # attribute holds for every configuration there is -- including one
        # that escapes nothing. Verified rather than reasoned: with
        # `enabled_extensions=()` and both defaults False, the attribute is
        # still a function, still truthy, this assertion still passed, and the
        # page rendered a live script tag. The rendering tests below caught it;
        # the test named for the property could not.
        env = environment()
        decided = (env.autoescape("inbox.html") if callable(env.autoescape)
                   else env.autoescape)
        self.assertIs(decided, True,
                      "autoescaping is why Jinja2 is a dependency here; "
                      "the bare default escapes nothing")

    def test_every_field_of_a_row_comes_back_inert(self):
        # Asserted over the whole page rather than field by field: a template
        # gaining an unescaped field is exactly the drift this must catch.
        html = render("inbox.html", rows=[_row()], counts={})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_runner_up_title_is_escaped_too(self):
        # Runners-up render in a nested loop, which is the easiest place for a
        # hand-written |safe to be added without anyone noticing.
        html = render("inbox.html", rows=[_row()], counts={})
        self.assertEqual(html.count("<script>"), 0)

    def test_no_template_disables_escaping(self):
        # A single |safe or {% autoescape false %} reopens the hole the
        # dependency was taken to close. This scan is the backstop for the
        # rendering tests above, which can only see fields the fixtures
        # populate -- one sitting on any other field would pass all of them.
        #
        # Matched by pattern rather than by substring, because the substrings
        # miss the spellings people actually write. Verified: `| safe` with
        # spaces -- the idiomatic form, and more likely than `|safe` -- was not
        # caught, and neither was `{% autoescape  false %}` with two spaces.
        # A backstop that only catches the unidiomatic spelling is not one.
        import pathlib
        import re
        unsafe = re.compile(r"\|\s*safe\b|autoescape\s+false\b")
        root = pathlib.Path("cronicled/web/templates")
        seen = 0
        for path in root.rglob("*.html"):
            seen += 1
            found = unsafe.search(path.read_text())
            self.assertIsNone(
                found, "%s disables escaping: %r"
                % (path, found.group(0) if found else None))
        # Without this the scan passes by finding no templates at all, which
        # is how it would read as green after a move or a rename.
        self.assertGreater(seen, 0, "found no templates to scan")

    def test_a_runner_up_title_actually_reaches_the_page(self):
        # Built through the real helper, not hand-written: a fixture carrying
        # a field production does not emit renders blank while staying
        # green, and blank reads as "nothing else was close" rather than as a
        # bug.
        from cronicled.scan import _runners_up
        from cronicled.scoring import Match
        candidates = [{"id": "c-1", "title": "The Lantern Room"},
                      {"id": "c-2", "title": "The Lantern"}]
        matches = [Match(value=0.81, contained=True, meaningful_count=3),
                   Match(value=0.61, contained=False, meaningful_count=2)]
        payload_runners = _runners_up(candidates, matches, winning_index=0)
        row = _row(runners_up=payload_runners)
        html = render("inbox.html", rows=[row], counts={})
        self.assertIn("The Lantern", html)


class ButtonWiring(unittest.TestCase):
    """Every control a row's action cell offers, pinned as a whole set --
    action path, label, and the fingerprint each form carries -- rather than
    sampled one at a time, so a control silently gaining, losing, or being
    repointed to the wrong path is caught.

    Three mutations matter enough to name explicitly:
      - the Undo button's `action` swapped from "/undo" to "/approve": the
        one control whose entire job is reversing a write would instead
        repeat it.
      - `{% if row.undoable %}` inverted to `{% if not row.undoable %}`:
        Undo would then be offered on exactly the rows where
        `Stash.revert_scene` raises (no snapshot), and withheld on exactly
        the rows where it would work.
      - Refresh's form gated behind `row.actionable` (or dropped from one of
        the two branches above) instead of standing outside both: an applied
        row with no snapshot is precisely the row ticket 86 needs a way off
        the block it would otherwise leave forever, and it is exactly the row
        neither branch above ever renders.

    The previous fixture here hardcoded `state="new"`/`prior_state=None`, so
    the Undo branch was never reached by any render -- these build both an
    undoable row and a non-undoable "applied" row through the same `_row`
    helper, via the same override path `test_web_rows.py` already relies on
    for `state`/`prior_state` (both live at the item's top level, unlike
    `runners_up`, so `**over` reaches them directly).
    """

    _FORM_RE = re.compile(
        r'<form method="post" action="(?P<action>[^"]+)">'
        r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
        r'<button>(?P<label>[^<]+)</button></form>')

    def _controls(self, row):
        html = render("inbox.html", rows=[row], counts={})
        return [(m.group("action"), m.group("label"), m.group("fp"))
                for m in self._FORM_RE.finditer(html)]

    def test_a_new_undecided_row_offers_exactly_approve_dismiss_mute_refresh(self):
        row = _row(state="new", prior_state=None)
        self.assertFalse(row.undoable)
        self.assertEqual(
            self._controls(row),
            [("/approve", "Approve", row.fingerprint),
             ("/dismiss", "Dismiss", row.fingerprint),
             ("/mute", "Mute", row.fingerprint),
             ("/refresh", "Refresh", row.fingerprint)])

    def test_an_applied_row_with_a_snapshot_offers_undo_and_refresh(self):
        row = _row(state="applied", prior_state={"title": "old"})
        self.assertTrue(row.undoable)
        self.assertEqual(self._controls(row),
                         [("/undo", "Undo", row.fingerprint),
                          ("/refresh", "Refresh", row.fingerprint)])

    def test_an_applied_row_without_a_snapshot_offers_only_refresh(self):
        # undoable is False (nothing to revert to) and state is not "new",
        # so neither of the first two template branches fires. Exactly what
        # revert_scene's own refusal on an empty snapshot demands: no button
        # may promise an undo the code cannot perform -- but this is exactly
        # the row ticket 86 is about, so it must not be a dead end either:
        # Refresh is the one control offered unconditionally, whatever a
        # row's own state (see the comment above the button in inbox.html).
        row = _row(state="applied", prior_state=None)
        self.assertFalse(row.undoable)
        self.assertEqual(self._controls(row),
                         [("/refresh", "Refresh", row.fingerprint)])


class CoverWarning(unittest.TestCase):
    """The warning a person needs before Approve writes something Undo
    cannot take back -- and the same fact, past tense, once the row is
    applied. Checked as text actually reaching the rendered page, not by
    inspecting `row.carries_cover` alone: a template that stopped
    rendering the warning while the field stayed `True` would still pass a
    test that only checked the dataclass.

    Two mutations matter enough to name explicitly:
      - dropping the warning for a row whose candidate DOES carry a cover
        -- silence exactly where the person needs it most;
      - showing it for every row regardless of `carries_cover` -- a
        warning true of nothing trains a person to stop reading it,
        including the genuinely-true one beside it (`contested`).
    """

    _NOT_YET_APPLIED = "cannot be fully undone"
    _ALREADY_APPLIED = "cannot be restored by Undo, even after reverting"

    def test_a_new_row_with_a_cover_warns_before_the_click(self):
        row = _row(state="new", image="data:image/jpeg;base64,realcover")
        html = render("inbox.html", rows=[row], counts={})
        self.assertIn(self._NOT_YET_APPLIED, html)

    def test_a_new_row_without_a_cover_never_warns(self):
        row = _row(state="new", image=None)
        html = render("inbox.html", rows=[row], counts={})
        self.assertNotIn(self._NOT_YET_APPLIED, html)
        self.assertNotIn(self._ALREADY_APPLIED, html)

    def test_an_applied_row_with_a_cover_reports_the_residual_not_a_clean_reversal(self):
        row = _row(state="applied", prior_state={"title": "old"},
                   image="data:image/jpeg;base64,realcover")
        html = render("inbox.html", rows=[row], counts={})
        self.assertIn(self._ALREADY_APPLIED, html)

    def test_an_applied_row_without_a_cover_never_warns(self):
        row = _row(state="applied", prior_state={"title": "old"}, image=None)
        html = render("inbox.html", rows=[row], counts={})
        self.assertNotIn(self._NOT_YET_APPLIED, html)
        self.assertNotIn(self._ALREADY_APPLIED, html)

    def test_the_base64_image_itself_never_reaches_the_page(self):
        # The row carries a boolean, not the image: rendering the actual
        # payload would put a base64 blob nobody asked for in the page, on
        # top of being the exact escaping-route mistake this ticket's
        # brief warns against -- a second rendering path around the
        # boolean this module is supposed to be the only route through.
        cover = "data:image/jpeg;base64," + ("Q" * 200)
        row = _row(state="new", image=cover)
        html = render("inbox.html", rows=[row], counts={})
        self.assertNotIn(cover, html)
        self.assertNotIn("base64", html)


class PerformersAndStudioOnThePage(unittest.TestCase):
    """What approving a proposal will actually write onto the scene now
    reaches the page -- the whole point of scraping the winning candidate's
    own URL rather than carrying only a title and a link.

    Built through a dedicated item helper, not `_row`: `_row`'s own `over`
    only ever replaces `item`'s TOP level (see its docstring), so a
    `payload={"candidate": ...}` passed through it would drop `path`,
    `creator` and `score` instead of overriding just the candidate.
    """

    def _item(self, candidate):
        payload = {
            "path": "/library/x/reel.mp4",
            "creator": {"name": "Someone", "source": "folder",
                       "competing": None, "rejected_folder": None},
            "candidate": candidate,
            "score": 0.812,
            "runners_up": [],
        }
        return {"fingerprint": "fp-1", "state": "new", "summary": "s",
                "confidence": 0.812, "payload": payload, "prior_state": None,
                "subject_id": "1"}

    def _candidate(self, **over):
        candidate = {"id": "c-1", "title": "The Lantern Room", "image": None,
                    "performers": [{"stored_id": None, "name": "Ivy Kingsley"}],
                    "studio": {"stored_id": None, "name": "Amber Vale"}}
        candidate.update(over)
        return candidate

    def test_a_performer_and_a_studio_reach_the_page(self):
        row = to_row(self._item(self._candidate()))
        html = render("inbox.html", rows=[row], counts={})
        self.assertIn("Ivy Kingsley", html)
        self.assertIn("Amber Vale", html)

    def test_a_thin_unenriched_candidate_shows_neither(self):
        row = to_row(self._item(self._candidate(performers=[], studio=None)))
        html = render("inbox.html", rows=[row], counts={})
        self.assertNotIn("Ivy Kingsley", html)
        self.assertNotIn("Amber Vale", html)

    def test_hostile_performer_and_studio_names_are_escaped(self):
        row = to_row(self._item(self._candidate(
            performers=[{"stored_id": None, "name": _HOSTILE}],
            studio={"stored_id": None, "name": _HOSTILE})))
        html = render("inbox.html", rows=[row], counts={})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class AgreeingStoresOnThePage(unittest.TestCase):
    """Two independent stores naming one scene is the strongest text
    evidence this tool produces, and it is worth nothing if the person
    approving the row cannot see it.

    Built through its own item helper for the same reason
    `PerformersAndStudioOnThePage` is: `_row`'s `over` replaces only the
    item's top level, so a `payload=` passed through it would drop `path`,
    `creator` and `score` rather than add a key to them.
    """

    def _row_with(self, agreeing, creator_name="Someone"):
        payload = {
            "path": "/library/x/reel.mp4",
            "creator": {"name": creator_name, "source": "folder",
                       "competing": None, "rejected_folder": None},
            "candidate": {"id": "c-1", "title": "The Lantern Room",
                         "image": None, "performers": [], "studio": None},
            "score": 0.812,
            "runners_up": [],
        }
        if agreeing is not None:
            payload["agreeing_stores"] = agreeing
        return to_row({"fingerprint": "fp-1", "state": "new", "summary": "s",
                       "confidence": 0.812, "payload": payload,
                       "prior_state": None, "subject_id": "1"})

    def test_every_agreeing_store_is_named_on_the_page(self):
        html = render("inbox.html",
                      rows=[self._row_with(["beta", "gamma"])], counts={})
        self.assertIn("beta", html)
        self.assertIn("gamma", html)

    def test_a_proposal_nothing_corroborated_says_nothing_about_it(self):
        # A row only one store answered must not grow an empty badge: a
        # label with no names after it reads as a field that failed to
        # render, and this is a page people approve writes from.
        html = render("inbox.html", rows=[self._row_with(None)], counts={})
        self.assertNotIn("corroborated", html)

    def test_a_hostile_store_name_is_escaped(self):
        # Store names come from configuration, but so does every other
        # field this page escapes; the backstop is worth the two lines
        # rather than an argument about which inputs are trusted.
        html = render("inbox.html", rows=[self._row_with([_HOSTILE])],
                      counts={})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


def _run(**over):
    """One row of `Store.recent_runs()`.

    Every key it emits is present here, including the ones a given test does
    not care about: a fixture thinner than the real shape lets the builder
    start reading a key nothing supplies, and the symptom of that is a page
    field that renders empty in production and is never asserted here. The
    whole-shape assertion in `tests/test_store.py` is what keeps this honest
    against the store itself.
    """
    row = dict(id="r-1", job="scene-scan", trigger="scheduled",
               started="2026-07-15T00:30:00+00:00",
               finished="2026-07-15T00:34:00+00:00",
               outcome="completed", counts={"recorded": 4, "skipped": 9},
               error=None)
    row.update(over)
    return row


def _summary_html(runs=(), waiting=None, schedule=None, zone=ZONE, **context):
    """The summary page as `web.app` renders it.

    The view comes from the REAL `to_summary_view` rather than a hand-written
    dict of the keys the template reads today -- a literal stand-in would go
    stale the day the builder grows a key, and the symptom would be a blank on
    the page with every assertion here still green.
    """
    view = to_summary_view(list(runs), {} if waiting is None else waiting,
                           schedule, zone=zone)
    fields = dict(scan=None, scan_default_limit=25)
    fields.update(context)
    return render("summary.html", summary=view, **fields)


class ScanStatusEscaping(unittest.TestCase):
    # A job's `message` carries file names -- attacker-influenceable text,
    # the same reason a row's fields are escaped. This is the same backstop
    # `Autoescaping` runs for row fields, aimed at the one new field this
    # ticket adds to the page.
    #
    # On the summary page since the Scan control moved there; the panel's
    # markup is one included partial, so this is the same block it always was.

    def test_the_scan_messages_hostile_content_is_escaped(self):
        html = _summary_html(scan=_job(message=_HOSTILE))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_failed_scans_error_is_escaped_too(self):
        html = _summary_html(scan=_job(state="failed", error=_HOSTILE))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class ScanControlWiring(unittest.TestCase):
    """The Scan control, pinned as a whole rather than by substring: the
    form's method, action, the visible number input's name, and the button
    -- the same reason `ButtonWiring` pins a row's controls as a whole set
    rather than sampling one field, so a control silently losing its
    `limit` field (falling back to whatever the code path does with none)
    is caught here rather than by a containment check on unrelated text."""

    _FORM_RE = re.compile(
        r'<form method="post" action="/scan">'
        r'<label>[^<]*<input type="number" name="limit" '
        r'value="(?P<value>[^"]*)" min="0"></label> <button>(?P<label>[^<]+)'
        r'</button></form>')

    def test_the_scan_control_offers_exactly_one_limited_start_button(self):
        html = _summary_html()
        matches = self._FORM_RE.findall(html)
        self.assertEqual(len(matches), 1)
        value, label = matches[0]
        self.assertEqual(value, "25")
        self.assertEqual(label, "Scan")

    def test_the_control_still_appears_while_a_scan_is_running(self):
        # A busy runner is refused when the form is POSTed (see
        # `tests/test_web_app.py`'s `ScanControl`) -- it is not hidden here,
        # so pressing it while one runs is a visible refusal, not a control
        # that silently vanished.
        html = _summary_html(scan=_job(state="running"))
        self.assertEqual(len(self._FORM_RE.findall(html)), 1)

    def test_the_inbox_no_longer_carries_a_second_copy_of_it(self):
        # One control for one action. Two -- one per page -- would be two
        # places a refusal is reported and one of them looked at, and the two
        # would drift the first time either was changed.
        html = render("inbox.html", rows=[], counts={})
        self.assertEqual(self._FORM_RE.findall(html), [])


class MutedDismissedRefusedSections(unittest.TestCase):
    """The four collapsed sections beneath the inbox: a count visible while
    collapsed, and exactly the control (or lack of one) each row type
    offers -- mirroring `ButtonWiring`'s "pin the whole set" reasoning for
    the main list's own controls.
    """

    _MUTE_FORM_RE = re.compile(
        r'<form method="post" action="/unmute">'
        r'<input type="hidden" name="subject_type" value="(?P<type>[^"]*)">'
        r'<input type="hidden" name="subject_id" value="(?P<id>[^"]*)">'
        r'<button>Unmute</button></form>')
    _UNDISMISS_FORM_RE = re.compile(
        r'<form method="post" action="/undismiss">'
        r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
        r'<button>Undismiss</button></form>')

    def test_counts_are_embedded_in_the_page_while_every_section_stays_collapsed(self):
        # <details> without an `open` attribute renders collapsed in a
        # browser, but its markup -- the summary text included -- is still
        # part of the page's own HTML. A mutation that only shows the count
        # once a section is expanded would not appear at all in a browser's
        # first paint, which is the failure this pins.
        html = render("inbox.html", rows=[], counts={},
                      muted=[{"subject_type": "scene", "subject_id": "1",
                             "reason": "r", "at": "t"}],
                      dismissed=[_row(state="dismissed")],
                      refused=[{"subject_type": "scene", "subject_id": "2",
                               "filename": "clip.mp4", "reason": "a tie",
                               "at": "t"}],
                      superseded=[_row(state="superseded")])
        self.assertIn("Muted (1)", html)
        self.assertIn("Dismissed (1)", html)
        self.assertIn("Refused (1)", html)
        self.assertIn("Superseded (1)", html)
        # None of the four is forced open -- collapsed is the default.
        self.assertNotIn("<details class=\"section\" open", html)

    def test_a_zero_count_still_shows_the_number(self):
        html = render("inbox.html", rows=[], counts={},
                      muted=[], dismissed=[], refused=[], superseded=[])
        self.assertIn("Muted (0)", html)
        self.assertIn("Dismissed (0)", html)
        self.assertIn("Refused (0)", html)
        self.assertIn("Superseded (0)", html)

    def test_a_muted_subject_offers_exactly_one_unmute_control(self):
        html = render("inbox.html", rows=[], counts={},
                      muted=[{"subject_type": "scene", "subject_id": "7",
                             "reason": "never identifiable", "at": "t"}],
                      dismissed=[], refused=[])
        self.assertEqual(self._MUTE_FORM_RE.findall(html), [("scene", "7")])

    def test_two_muted_subjects_each_get_their_own_control_not_a_bulk_one(self):
        # "No bulk actions" -- unmuting sixteen things at once is exactly
        # what this control must never offer.
        html = render("inbox.html", rows=[], counts={},
                      muted=[{"subject_type": "scene", "subject_id": "1",
                             "reason": "r", "at": "t"},
                            {"subject_type": "scene", "subject_id": "2",
                             "reason": "r", "at": "t"}],
                      dismissed=[], refused=[])
        self.assertEqual(self._MUTE_FORM_RE.findall(html),
                         [("scene", "1"), ("scene", "2")])

    def test_a_dismissed_row_offers_exactly_one_undismiss_control(self):
        row = _row(state="dismissed")
        html = render("inbox.html", rows=[], counts={},
                      muted=[], dismissed=[row], refused=[])
        self.assertEqual(self._UNDISMISS_FORM_RE.findall(html),
                         [row.fingerprint])

    def test_a_refused_row_offers_no_control_at_all(self):
        # "the fix is outside the tool" -- no button may promise an action
        # the tool cannot perform for a refusal.
        html = render("inbox.html", rows=[], counts={}, muted=[], dismissed=[],
                      refused=[{"subject_type": "scene", "subject_id": "9",
                               "filename": "clip.mp4", "reason": "a tie",
                               "at": "t"}])
        section = html[html.index("Refused (1)"):html.index("<h2>Schedule</h2>")]
        self.assertNotIn("<form", section)
        self.assertIn("clip.mp4", section)
        self.assertIn("a tie", section)

    def test_a_superseded_row_offers_no_control_at_all(self):
        # Superseding is a one-way retirement -- there is no "un-supersede"
        # the way there is an Undismiss or an Unmute, because a fresh scan
        # (not a click here) is what is supposed to replace this row. No
        # button may promise an action the tool does not offer.
        row = _row(state="superseded")
        html = render("inbox.html", rows=[], counts={}, muted=[], dismissed=[],
                      refused=[], superseded=[row])
        section = html[html.index("Superseded (1)"):html.index("Refused (")]
        self.assertNotIn("<form", section)
        self.assertIn(row.score_text, section)


class ARefusedRowShowsWhatEveryStoreReturned(unittest.TestCase):
    """The Refused section's per-store lines.

    Rendered from `to_refusal_row`'s own output rather than from hand-written
    dicts, so the `outcome` values the template branches on are the ones
    `rows.STORE_*` actually defines. A fixture that spelled them out here
    would keep passing after a constant was renamed, with the page rendering
    a blank line for every store -- Jinja renders a branch nothing matched as
    empty text rather than raising.
    """

    ANSWERED = {"store": "alpha", "rows": 40, "score": 0.342,
                "title": "Evening Ritual",
                "url": "https://alpha.example/clip/evening-ritual",
                "error": None}
    EMPTY = {"store": "beta", "rows": 0, "score": None, "title": None,
             "url": None, "error": None}
    FAILED = {"store": "gamma", "rows": None, "score": None, "title": None,
              "url": None, "error": "TimeoutError: timed out"}

    _ANCHOR_RE = re.compile(r'<a href="([^"]*)"[^>]*>([^<]*)</a>')

    def section(self, stores):
        row = to_refusal_row(
            {"subject_type": "scene", "subject_id": "9",
             "path": "/library/Nine Winters/clip.mp4",
             "reason": "alpha: nothing above the threshold (0.70)",
             "at": "t", "stores": stores})
        html = render("inbox.html", rows=[], counts={}, muted=[],
                      dismissed=[], refused=[row])
        return html[html.index("Refused (1)"):html.index("<h2>Schedule</h2>")]

    def test_each_of_the_three_states_says_its_own_specific_thing(self):
        """Asserted on what each state produces, not on "some text appeared".
        One catch-all sentence used for all three would satisfy three loose
        checks while telling a reader nothing about which store did what."""
        section = self.section([self.ANSWERED, self.EMPTY, self.FAILED])

        # Rows, none good enough: the count AND the score AND the candidate.
        self.assertIn("40 returned, best 0.342", section)
        self.assertIn("Evening Ritual", section)
        # Confirmed empty -- and no invented count or score alongside it.
        self.assertIn("returned nothing", section)
        # Raised: named as a failure, with what failed.
        self.assertIn("search failed", section)
        self.assertIn("TimeoutError: timed out", section)
        # ...and the empty store is not described as a failure, nor the
        # failing one as empty. Three lines, three meanings.
        self.assertEqual(section.count("returned nothing"), 1)
        self.assertEqual(section.count("search failed"), 1)

    def test_every_store_is_named_not_only_the_closest(self):
        section = self.section([self.ANSWERED, self.EMPTY, self.FAILED])
        for name in ("alpha", "beta", "gamma"):
            self.assertIn("<b>%s</b>" % name, section)

    def test_the_near_miss_title_links_to_that_candidates_page(self):
        section = self.section([self.ANSWERED])
        self.assertEqual(self._ANCHOR_RE.findall(section),
                         [("https://alpha.example/clip/evening-ritual",
                           "Evening Ritual")])

    def test_a_candidate_with_no_address_renders_as_text_with_no_anchor(self):
        """An anchor pointing nowhere is worse than plain text: it looks
        clickable, goes to the page it is on, and teaches a reviewer the
        links are not to be trusted. The same degradation a row with no
        configured media server already gets for its filename."""
        section = self.section([dict(self.ANSWERED, url=None)])

        self.assertIn("Evening Ritual", section)
        self.assertEqual(self._ANCHOR_RE.findall(section), [])
        self.assertNotIn("<a ", section)

    def test_an_empty_address_produces_no_anchor_either(self):
        section = self.section([dict(self.ANSWERED, url="")])
        self.assertNotIn("<a ", section)

    def test_a_refusal_with_no_stores_renders_no_store_lines_and_no_error(self):
        section = self.section([])
        self.assertNotIn('class="store"', section)
        self.assertIn("alpha: nothing above the threshold (0.70)", section)

    def test_a_stores_own_fields_are_escaped(self):
        hostile = {"store": _HOSTILE, "rows": 1, "score": 0.5,
                   "title": _HOSTILE, "url": None, "error": None}
        failed = dict(self.FAILED, error=_HOSTILE)

        section = self.section([hostile, failed])

        self.assertEqual(section.count("<script>"), 0)
        self.assertIn("&lt;script&gt;", section)


class NewSectionsEscaping(unittest.TestCase):
    """The same backstop `Autoescaping` runs for a proposal row's fields,
    aimed at the fields these four new sections add to the page."""

    def test_a_muted_reason_is_escaped(self):
        html = render("inbox.html", rows=[], counts={},
                      muted=[{"subject_type": "scene", "subject_id": "1",
                             "reason": _HOSTILE, "at": "t"}],
                      dismissed=[], refused=[])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_superseded_rows_fields_are_escaped(self):
        row = _row(state="superseded")  # built with hostile content by default
        html = render("inbox.html", rows=[], counts={}, muted=[],
                      dismissed=[], refused=[], superseded=[row])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_refusals_filename_and_reason_are_escaped(self):
        html = render("inbox.html", rows=[], counts={}, muted=[], dismissed=[],
                      refused=[{"subject_type": "scene", "subject_id": "1",
                               "filename": _HOSTILE, "reason": _HOSTILE,
                               "at": "t"}])
        self.assertEqual(html.count("<script>"), 0)

    def test_a_dismissed_rows_fields_are_escaped(self):
        row = _row(state="dismissed")  # built with hostile content by default
        html = render("inbox.html", rows=[], counts={}, muted=[],
                      dismissed=[row], refused=[])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class SceneLinks(unittest.TestCase):
    """Ticket 97: a row links to its scene on the media server when one is
    configured, and degrades to plain text -- never a broken link -- when
    it is not.
    """

    def test_a_rows_filename_links_to_its_scene_when_configured(self):
        row = _row(subject_id="42", base_url="http://media.example")
        html = render("inbox.html", rows=[row], counts={})
        self.assertIn(
            '<a href="http://media.example/scenes/42" target="_blank" '
            'rel="noopener noreferrer">', html)

    def test_the_link_opens_a_new_tab_and_never_a_get_back_here(self):
        # "A link is a GET to another origin ... it cannot be allowed to
        # become a way to trigger anything here" -- pinned as a whole
        # attribute set, not sampled, so a mutation dropping target="_blank"
        # or rel="noopener noreferrer" is caught the same way `ButtonWiring`
        # pins a control's whole attribute set.
        row = _row(subject_id="42", base_url="http://media.example")
        html = render("inbox.html", rows=[row], counts={})
        match = re.search(r'<a href="[^"]+"[^>]*>', html)
        self.assertIsNotNone(match)
        self.assertIn('target="_blank"', match.group(0))
        self.assertIn('rel="noopener noreferrer"', match.group(0))
        self.assertNotIn("<form", match.group(0))

    def test_no_configured_server_degrades_to_plain_text_not_a_broken_link(self):
        # Ticket 97: "the tool now starts read-only with no server
        # configured ... the link has to degrade rather than render
        # broken." No base_url at all here -- row.scene_url is None.
        row = _row(subject_id="42")
        html = render("inbox.html", rows=[row], counts={})
        self.assertNotIn("<a href", html)
        self.assertNotIn("scenes/42", html)


class CandidateLinks(unittest.TestCase):
    """The proposed title links to the candidate's own page on the store it
    came from, so the one piece of evidence a proposal is built on is a
    click away rather than a title retyped into a search by hand.
    """

    _WHOLE_ANCHOR = ('<a href="https://store.example/lantern-room" '
                     'target="_blank" rel="noopener noreferrer">')

    def _block(self, html):
        """The one proposal block on the page, whole. Assertions are made
        inside it rather than over the page, so a link belonging to a
        different section cannot satisfy them."""
        start = html.index('<div class="proposal')
        return html[start:html.index("<h2>Schedule</h2>", start)]

    def test_a_proposed_title_links_to_the_candidates_own_page(self):
        # The WHOLE element, not "the url appears somewhere": a substring
        # check passes when the anchor is malformed, nested wrongly, or
        # attached to the filename instead.
        html = render("inbox.html", counts={}, rows=[_row(
            urls=["https://store.example/lantern-room"])])
        self.assertIn(
            '<p class="title">%s&lt;script&gt;alert(&#34;x&#34;)'
            '&lt;/script&gt;</a></p>' % self._WHOLE_ANCHOR,
            self._block(html))

    def test_a_candidate_with_no_address_is_text_with_no_anchor_at_all(self):
        # The restrictive side pinned as well as the permissive one. A
        # candidate carrying no address is ordinary -- a name search often
        # returns a title and nothing else -- and must render as text, never
        # as an anchor with an empty href, which looks like a link and goes
        # nowhere.
        html = render("inbox.html", counts={}, rows=[_row(urls=[])])
        block = self._block(html)
        self.assertIn('<p class="title">&lt;script&gt;alert(&#34;x&#34;)'
                      '&lt;/script&gt;</p>', block)
        self.assertNotIn("<a ", block)

    def test_the_scene_link_and_the_candidate_link_are_not_confused(self):
        # HARM: these two addresses are the opposite ends of the decision --
        # the file as the library holds it today, and the record a person is
        # being asked to overwrite it with. Swapping them sends a reviewer
        # who wanted to check the candidate to the thing they were checking
        # it against, and they would see exactly what they expected.
        html = render("inbox.html", counts={}, rows=[_row(
            subject_id="42", base_url="http://media.example",
            urls=["https://store.example/lantern-room"])])
        block = self._block(html)
        self.assertIn(
            '<p class="file"><a href="http://media.example/scenes/42" '
            'target="_blank" rel="noopener noreferrer">', block)
        self.assertIn('<p class="title">%s' % self._WHOLE_ANCHOR, block)

    def test_every_runner_up_links_to_its_own_page(self):
        # Two losers with different addresses: a loop handing every
        # runner-up the first one's link, or the winner's, fails here. A
        # test asserting only the winner's link would not have covered this
        # at all.
        html = render("inbox.html", counts={}, rows=[_row(
            urls=["https://store.example/lantern-room"],
            runners_up=[
                {"candidate": {"title": "The Lantern",
                               "urls": ["https://store.example/lantern"]},
                 "score": 0.61},
                {"candidate": {"title": "Lantern Nights",
                               "urls": ["https://store.example/nights"]},
                 "score": 0.55}])])
        self.assertIn(
            '<a href="https://store.example/lantern" target="_blank" '
            'rel="noopener noreferrer">The Lantern</a> (0.610)', html)
        self.assertIn(
            '<a href="https://store.example/nights" target="_blank" '
            'rel="noopener noreferrer">Lantern Nights</a> (0.550)', html)

    def test_a_runner_up_with_no_address_stays_plain_text(self):
        html = render("inbox.html", counts={}, rows=[_row(
            urls=[], runners_up=[{"candidate": {"title": "The Lantern",
                                                "urls": []}, "score": 0.61}])])
        block = self._block(html)
        self.assertIn("The Lantern (0.610)", block)
        self.assertNotIn("<a ", block)

    def test_a_fingerprint_row_links_its_candidate_and_never_the_endpoint(self):
        # A box's endpoint is a GraphQL API address, not a page a person can
        # open. The match carries its own address and that is what is
        # linked; the endpoint must not reach the page as a link or as text
        # pretending to be one.
        html = render("inbox.html", counts={}, rows=[_row(payload_over={
            "identified_by": "fingerprint", "box": "a-box",
            "endpoint": "https://box.example/graphql",
            "remote_site_id": "6d3f-scene"},
            urls=["https://store.example/lantern-room"])])
        block = self._block(html)
        self.assertIn('<p class="title">%s' % self._WHOLE_ANCHOR, block)
        self.assertNotIn("box.example", block)
        self.assertNotIn("6d3f-scene", block)

    def test_a_fingerprint_row_with_no_candidate_address_renders_no_anchor(self):
        # THE guard against a second derivation: everything an
        # `endpoint + "/scenes/" + id` rule would need is in this payload,
        # and the page must still show plain text. Uncertainty may withhold
        # evidence and never supply it.
        html = render("inbox.html", counts={}, rows=[_row(payload_over={
            "identified_by": "fingerprint", "box": "a-box",
            "endpoint": "https://box.example/graphql",
            "remote_site_id": "6d3f-scene"}, urls=[])])
        block = self._block(html)
        self.assertNotIn("<a ", block)
        self.assertNotIn("box.example", block)


def _muted(item=None, subject_type="scene", subject_id="12962",
           reason="never identifiable", at="t", base_url=None, zone=ZONE):
    """One muted entry, built by the real `to_mute_row` from the real
    `Store.mutes()` shape -- never a hand-written dict.

    A hand-written entry is free to carry a key the row builder does not
    emit, which renders blank on the page while every assertion here stays
    green; that is precisely how the previous, thinner muted row went
    unnoticed.
    """
    return to_mute_row({"subject_type": subject_type,
                        "subject_id": subject_id, "reason": reason,
                        "at": at, "item": item}, base_url=base_url, zone=zone)


def _identity_elements(section):
    """The four elements that ARE a row's identity, each whole.

    Whole elements rather than substrings, and all four rather than a
    sample: "shows what a dismissed row shows" is a claim about the set,
    and a check that names three of them cannot see the fourth go missing.
    """
    def one(pattern):
        found = re.search(pattern, section, re.S)
        return found.group(0) if found else None
    return {
        "file": one(r'<p class="file"[^>]*>.*?</p>'),
        "title": one(r'<p class="title"[^>]*>.*?</p>'),
        "meta": one(r'<div class="meta">.*?</div>'),
        "score": one(r'<div class="score">.*?</div>'),
    }


def _section(html, name):
    start = html.index("%s (" % name)
    return html[start:html.index("</details>", start)]


class MutedRowsReadLikeDismissedOnes(unittest.TestCase):
    """A muted subject and a dismissed one are the same thing seen twice --
    something a person hid and may want back. They must read the same, and
    the only difference must be that the control says Unmute.
    """

    def _page(self, item, **over):
        kwargs = dict(
            rows=[], counts={}, refused=[],
            muted=[_muted(item=item, subject_id="42",
                          base_url="http://media.example")],
            dismissed=([] if item is None
                       else [to_row(item, base_url="http://media.example")]))
        kwargs.update(over)
        return render("inbox.html", **kwargs)

    def test_a_muted_row_shows_exactly_what_the_dismissed_row_shows(self):
        # The same item in both sections, so anything the muted row fails to
        # carry shows up as a difference rather than as an absence nobody
        # named. Every element compared whole.
        item = _scene_item(subject_id="42",
                           urls=["https://store.example/lantern-room"])
        html = self._page(item)
        self.assertEqual(_identity_elements(_section(html, "Muted")),
                         _identity_elements(_section(html, "Dismissed")))

    def test_the_muted_rows_identity_is_not_empty_to_begin_with(self):
        # Without this the comparison above passes on two sections that both
        # show nothing -- the exact shape of a green suite over a blank page.
        item = _scene_item(subject_id="42",
                           urls=["https://store.example/lantern-room"])
        elements = _identity_elements(_section(self._page(item), "Muted"))
        self.assertIsNotNone(elements["file"])
        self.assertIsNotNone(elements["title"])
        self.assertIn("<b>", elements["meta"])
        self.assertIn("0.812", elements["score"])
        self.assertIn("store.example/lantern-room", elements["title"])
        self.assertIn("media.example/scenes/42", elements["file"])

    def test_the_control_is_unmute_and_never_undismiss(self):
        # HARM: these two write to DIFFERENT tables. An Undismiss here
        # reports success and leaves the subject muted -- still hidden, and
        # now with the page saying it was restored. Pinned as the whole
        # form, because an Unmute posting the wrong fields is the same
        # failure with a friendlier label.
        item = _scene_item(subject_id="42")
        section = _section(self._page(item), "Muted")
        self.assertIn(
            '<form method="post" action="/unmute">'
            '<input type="hidden" name="subject_type" value="scene">'
            '<input type="hidden" name="subject_id" value="42">'
            '<button>Unmute</button></form>', section)
        self.assertNotIn("/undismiss", section)
        self.assertNotIn("/approve", section)

    def test_a_mute_with_nothing_behind_it_still_renders_and_says_so(self):
        # The genuine exception: a subject muted before anything was ever
        # found for it. It must go on saying so plainly rather than drawing
        # the rich shape with blank fields, which reads as a page that
        # failed to render.
        html = self._page(None, dismissed=[])
        section = _section(html, "Muted")
        self.assertIn(
            '<p class="file subject-unknown">'
            '<a href="http://media.example/scenes/42" target="_blank" '
            'rel="noopener noreferrer">scene 42</a> <span class="note">'
            '(muted before any proposal was ever recorded -- only the id '
            'is known)</span></p>', section)
        self.assertNotIn('<p class="title">', section)
        self.assertNotIn('<div class="score">', section)

    def test_that_exception_still_offers_the_control_that_lifts_the_mute(self):
        section = _section(self._page(None, dismissed=[]), "Muted")
        self.assertIn("<button>Unmute</button>", section)

    def test_a_muted_entry_still_shows_its_reason_and_when(self):
        # What the muted row has that a dismissed one does not, and it is
        # not lost to the richer identity above it.
        section = _section(self._page(_scene_item(subject_id="42")), "Muted")
        self.assertIn("never identifiable &middot; muted t", section)

    def test_a_muted_performer_renders_the_description_shape_not_a_scene(self):
        # A mute can be placed on any subject a producer proposes about.
        # Drawing a performer with the scene shape shows three blank fields;
        # putting one through the scene builder took the whole section out.
        item = {"fingerprint": "fp-d", "state": "muted",
                "subject_type": "performer", "subject_id": "7",
                "summary": "s", "confidence": None, "prior_state": None,
                "payload": {"name": "Wren Alderly", "field": "details",
                            "faults": ["markup"], "original": "<p>x</p>",
                            "cleaned": "x"}}
        section = _section(render(
            "inbox.html", rows=[], counts={}, refused=[], dismissed=[],
            muted=[_muted(item=item, subject_type="performer",
                          subject_id="7",
                          base_url="http://media.example")]), "Muted")
        self.assertIn(
            '<p class="file"><a href="http://media.example/performers/7" '
            'target="_blank" rel="noopener noreferrer">performer 7</a></p>',
            section)
        self.assertIn('<p class="title">Wren Alderly</p>', section)
        # No score column at all: nothing scored a description rewrite, and
        # an empty one reads as a value that failed to render.
        self.assertNotIn('<div class="score">', section)


class ApplifiedSectionRendering(unittest.TestCase):
    """Ticket 98: applied proposals move to their own collapsed section,
    with Undo and the cover warning still intact, while the main list
    excludes them (that exclusion is `cronicled.__main__`'s wiring, tested
    in `tests/test_main.py` -- this covers what the template does once an
    `applied` list actually reaches it).
    """

    def test_an_applied_rows_count_reaches_the_page(self):
        row = _row(state="applied", prior_state={"title": "old"})
        html = render("inbox.html", rows=[], counts={}, applied=[row])
        self.assertIn("Applied (1)", html)

    def test_an_empty_applied_section_says_so(self):
        html = render("inbox.html", rows=[], counts={}, applied=[])
        self.assertIn("Applied (0)", html)
        self.assertIn("Nothing applied yet.", html)

    def test_an_applied_row_still_offers_undo(self):
        row = _row(state="applied", prior_state={"title": "old"})
        html = render("inbox.html", rows=[], counts={}, applied=[row])
        section = html[html.index("Applied ("):html.index("Muted (")]
        self.assertIn('<form method="post" action="/undo">', section)
        self.assertIn(row.fingerprint, section)

    def test_the_cover_warning_travels_into_the_section(self):
        # "The cover-write warning travels with the row into the section.
        # It is the one write approving cannot take back." -- the fact this
        # ticket is most explicit must not be lost from row = _row that
        # carries a cover image once it lives inside Applied.
        row = _row(state="applied", prior_state={"title": "old"},
                  image="data:image/jpeg;base64,realcover")
        html = render("inbox.html", rows=[], counts={}, applied=[row])
        section = html[html.index("Applied ("):html.index("Muted (")]
        self.assertIn(
            "cannot be restored by Undo, even after reverting", section)

    def test_the_section_is_collapsed_by_default(self):
        row = _row(state="applied", prior_state={"title": "old"})
        html = render("inbox.html", rows=[], counts={}, applied=[row])
        self.assertNotIn('<details class="section" open', html)

    _HIGHLIGHT_CLASS = 'class="proposal just-applied"'

    # The exact opening tag of the Applied `<details>` element, anchored on
    # its own `<summary>` rather than found by a bare `open` substring search
    # -- an `open` attribute that a mutation moved onto a DIFFERENT section's
    # tag would still satisfy `'<details class="section" open' in html`, and
    # would still be a bug. Anchoring here means only Applied's own tag is
    # ever inspected.
    _APPLIED_DETAILS_TAG = re.compile(
        r'<details class="section"[^>]*>(?=\s*<summary>Applied \()')

    def _applied_details_tag(self, html):
        m = self._APPLIED_DETAILS_TAG.search(html)
        self.assertIsNotNone(m, "no Applied <details> tag found on the page")
        return m.group(0)

    def test_no_write_ever_opens_the_applied_section(self):
        # The section's open state is the operator's; nothing here may
        # override it. Same fixture as the section-count test above --
        # applied has a row, no `just_applied` at all -- to isolate this
        # rule from the confirmation-banner rules below.
        row = _row(state="applied", prior_state={"title": "old"})
        html = render("inbox.html", rows=[], counts={}, applied=[row])
        self.assertNotIn("open", self._applied_details_tag(html))

    def test_a_fresh_approve_leaves_the_section_closed(self):
        # This is the ticket: an approve used to spring this section open on
        # every request. `just_applied` is the fingerprint the redirect from
        # a successful /approve carries (see web/app.py), and it must no
        # longer reach this attribute at all.
        row = _row(state="applied", prior_state={"title": "old"},
                  fingerprint="fp-just-applied")
        html = render("inbox.html", rows=[], counts={}, applied=[row],
                      just_applied="fp-just-applied")
        self.assertNotIn("open", self._applied_details_tag(html))

    def test_a_fresh_approve_shows_a_confirmation_banner_with_undo(self):
        # The confirmation an open drawer used to provide moves here: the
        # just-applied row, highlighted, with its Undo -- rendered above the
        # fold, before the Applied `<details>` even appears in the markup,
        # so reaching it never requires opening anything.
        row = _row(state="applied", prior_state={"title": "old"},
                  fingerprint="fp-just-applied")
        html = render("inbox.html", rows=[], counts={}, applied=[row],
                      just_applied="fp-just-applied")
        applied_tag_at = html.index(self._applied_details_tag(html))
        banner_at = html.index(self._HIGHLIGHT_CLASS)
        self.assertLess(banner_at, applied_tag_at,
                        "the confirmation must render before the (closed) "
                        "Applied section, not inside it")
        banner = html[:applied_tag_at]
        self.assertIn('<form method="post" action="/undo">', banner)
        self.assertIn("fp-just-applied", banner)

    def test_a_non_undoable_just_applied_row_gets_no_undo_control_anywhere(self):
        # No `prior_state` -- `revert_scene` raises on an empty one, so
        # `row.undoable` is False and the row offers no Undo at all, in the
        # section or in the banner. The banner must not promise a control
        # the row itself does not have.
        row = _row(state="applied", prior_state=None,
                  fingerprint="fp-just-applied")
        html = render("inbox.html", rows=[], counts={}, applied=[row],
                      just_applied="fp-just-applied")
        self.assertIn(self._HIGHLIGHT_CLASS, html)
        self.assertNotIn('<form method="post" action="/undo">', html)

    def test_a_stale_just_applied_value_shows_no_banner_and_no_open(self):
        # A `?applied=` query value naming a row not (or no longer) in
        # `applied` -- a bookmarked link, another tab's stale redirect --
        # must not force anything open and must not draw a confirmation for
        # a row that is not there.
        row = _row(state="applied", prior_state={"title": "old"},
                  fingerprint="fp-1")
        html = render("inbox.html", rows=[], counts={}, applied=[row],
                      just_applied="fp-not-in-applied")
        self.assertNotIn("open", self._applied_details_tag(html))
        self.assertNotIn(self._HIGHLIGHT_CLASS, html)
        self.assertNotIn("Just applied:", html)

    def test_a_row_that_is_not_just_applied_is_not_marked(self):
        # Note the CSS rule itself (`.proposal.just-applied`, in the
        # `<style>` block) legitimately contains the substring
        # "just-applied" on every render -- asserting against the exact
        # rendered class attribute, not the bare word, is what keeps this
        # from passing regardless of the row's own state.
        row = _row(state="applied", prior_state={"title": "old"},
                  fingerprint="fp-1")
        html = render("inbox.html", rows=[], counts={}, applied=[row],
                      just_applied="fp-other")
        self.assertNotIn(self._HIGHLIGHT_CLASS, html)


class ReversalReopensItsOwnSection(unittest.TestCase):
    """A reversal (`/unmute`, `/undismiss`) redirects carrying `opened=`
    naming the section it acted in (see `cronicled.web.app._REOPEN_SECTION`),
    so reversing a backlog of mutes does not mean re-opening "Muted" by hand
    after every single one. Collapsed by default, the same as every section
    but Applied; only the ONE section named gets forced open, never both and
    never every section on the page.
    """

    def _muted(self):
        return [{"subject_type": "scene", "subject_id": "1",
                "reason": "never identifiable", "at": "t"}]

    def _dismissed(self):
        return [_row(state="dismissed")]

    def test_no_opened_value_leaves_both_sections_collapsed(self):
        html = render("inbox.html", rows=[], counts={},
                      muted=self._muted(), dismissed=self._dismissed())
        self.assertNotIn('<details class="section" open', html)

    def test_opened_muted_opens_only_the_muted_section(self):
        html = render("inbox.html", rows=[], counts={},
                      muted=self._muted(), dismissed=self._dismissed(),
                      opened="muted")
        # Exactly one section is forced open on the whole page, and its
        # `<details ...open>` tag is immediately followed by the Muted
        # section's own summary, not Dismissed's.
        self.assertEqual(html.count('<details class="section" open'), 1)
        open_at = html.index('<details class="section" open')
        self.assertIn("Muted (", html[open_at:open_at + 200])

    def test_opened_dismissed_opens_only_the_dismissed_section(self):
        html = render("inbox.html", rows=[], counts={},
                      muted=self._muted(), dismissed=self._dismissed(),
                      opened="dismissed")
        self.assertEqual(html.count('<details class="section" open'), 1)
        open_at = html.index('<details class="section" open')
        self.assertIn("Dismissed (", html[open_at:open_at + 200])

    def test_an_unrecognised_opened_value_opens_nothing(self):
        # A stray or foreign query value (`web/app.py`'s own whitelist would
        # already have dropped this before it reaches `render()` -- this
        # pins the template's own equality check as an independent second
        # guard, not merely the one upstream of it).
        html = render("inbox.html", rows=[], counts={},
                      muted=self._muted(), dismissed=self._dismissed(),
                      opened="applied")
        self.assertNotIn('<details class="section" open', html)


class TheSummaryViewPicksEachJobsLatestRun(unittest.TestCase):
    """`to_summary_view` reduces a run log to one row per job.

    The rule is "the FIRST occurrence of each job name in the list wins",
    which is only the latest run because `Store.recent_runs` is ordered
    `started DESC, rowid DESC`. Both halves are tested: the reduction here
    against a hand-ordered list, and the ordering itself against a real store
    in `tests/test_main.py`, where two runs of one job share a second.
    """

    def _view(self, runs, **over):
        fields = dict(waiting={}, schedule=None, zone=ZONE)
        fields.update(over)
        return to_summary_view(runs, fields["waiting"], fields["schedule"],
                               zone=fields["zone"])

    def test_the_first_run_named_for_a_job_is_the_one_shown(self):
        # Asymmetric on purpose: the two runs differ in every field a reader
        # would use to tell them apart, so "took the wrong one" cannot be
        # satisfied by a value both happen to share.
        view = self._view([
            _run(id="new", job="scene-scan", trigger="manual",
                 started="2026-07-15T00:30:00+00:00",
                 counts={"recorded": 7, "skipped": 0}),
            _run(id="old", job="scene-scan", trigger="scheduled",
                 started="2026-07-14T00:30:00+00:00",
                 counts={"recorded": 1, "skipped": 3}),
        ])
        self.assertEqual(
            [(j["id"], j["trigger"], j["started"], j["counts"])
             for j in view["jobs"]],
            [("new", "manual", "2026-07-15 02:30", {"recorded": 7,
                                                    "skipped": 0})])

    def test_a_job_further_down_the_log_still_gets_a_line(self):
        # A counting fixture of one cannot see a builder that stops after the
        # first job it meets, and a summary that reported only the busiest
        # pass would answer "did the tag pass run?" with silence.
        view = self._view([
            _run(id="a", job="scene-scan"),
            _run(id="b", job="scene-scan"),
            _run(id="c", job="tag-scan"),
            _run(id="d", job="performer-scan"),
            _run(id="e", job="tag-scan"),
        ])
        self.assertEqual([(j["job"], j["id"]) for j in view["jobs"]],
                         [("scene-scan", "a"), ("tag-scan", "c"),
                          ("performer-scan", "d")])

    def test_a_run_still_in_progress_takes_its_jobs_line(self):
        # A job running RIGHT NOW is that job's latest run. Skipping it for
        # the last completed one would answer "has tonight's pass started?"
        # with last night's, which is the one reading that makes a stalled
        # install look healthy.
        view = self._view([
            _run(id="open", finished=None, outcome=None, counts={},
                 error=None),
            _run(id="done", outcome="completed"),
        ])
        self.assertEqual([(j["id"], j["outcome"], j["finished"])
                          for j in view["jobs"]], [("open", None, None)])

    def test_a_row_with_no_job_is_refused_rather_than_given_a_heading(self):
        # Required fields raise. Defaulting the name would group every
        # unattributable run under one invented heading, which renders on the
        # page as a job that exists.
        with self.assertRaises(KeyError):
            self._view([{k: v for k, v in _run().items() if k != "job"}])

    def test_what_is_waiting_is_carried_through_exactly_as_given(self):
        # The builder does not interpret the mapping: a producer added to the
        # count reaches the page without an edit here, rather than being
        # dropped by a builder that knew three names.
        waiting = {"scene": 254, "tag": 675, "performer": 3}
        view = self._view([], waiting=waiting)
        self.assertEqual(view["waiting"],
                         {"scene": 254, "tag": 675, "performer": 3})

    def test_the_loops_own_status_is_converted_on_the_way_through(self):
        # Handed the RAW status, exactly as the scheduler reports it, and
        # converted here -- so the run times above and the schedule panel
        # below them cannot end up in two different zones.
        view = self._view([], schedule=_loop_status(
            last_tick_at="2026-07-15T00:30:00+00:00"))
        self.assertEqual(view["schedule"].last_tick_at,
                         "2026-07-15T02:30:00+02:00")

    def test_an_install_with_no_schedule_says_so_rather_than_inventing_one(self):
        self.assertIsNone(self._view([], schedule=None)["schedule"])


class ARecordedInstantKeepsItsDate(unittest.TestCase):
    """The boundary this page turns on.

    A STATED APPOINTMENT -- "03:00 every night" -- has no date and shortens to
    an hour and a minute. A RECORDED INSTANT has one, and shortening it away
    destroys the fact a reader is actually after: "did last night's pass run"
    cannot be answered by a bare `03:00` that might be from three weeks ago.

    Both sides of a daylight-saving transition in every case, because a
    fixture on one date cannot tell a zone from a constant offset.
    """

    def _started(self, stamp, zone=ZONE):
        return to_summary_view([_run(started=stamp)], {}, None,
                               zone=zone)["jobs"][0]

    def test_the_date_survives_the_shortening(self):
        job = self._started("2026-07-15T00:30:00+00:00")
        self.assertEqual(job["started"], "2026-07-15 02:30")

    def test_it_is_the_local_date_when_the_zone_moves_it(self):
        # 23:30 UTC is already the next day in Madrid. A builder that kept the
        # STORED date and converted only the clock would put an hour on the
        # page beside the wrong day -- worse than either half alone, because
        # the row looks entirely ordinary.
        job = self._started("2026-07-14T23:30:00+00:00")
        self.assertEqual(job["started"], "2026-07-15 01:30")

    def test_the_winter_side_of_the_transition_shifts_by_its_own_offset(self):
        # +01:00 in January against +02:00 in July: a constant offset applied
        # as though it were a zone passes exactly one of this pair.
        job = self._started("2026-01-15T00:30:00+00:00")
        self.assertEqual(job["started"], "2026-01-15 01:30")

    def test_the_finish_is_converted_too_not_only_the_start(self):
        # Two instants on one row. A test asserting the first would pass while
        # the second stayed in UTC, which is the failure worth designing
        # against -- a row half in the operator's hour reads as a run that
        # took two hours longer than it did.
        job = to_summary_view(
            [_run(started="2026-01-15T00:30:00+00:00",
                  finished="2026-07-15T00:30:00+00:00")],
            {}, None, zone=ZONE)["jobs"][0]
        self.assertEqual((job["started"], job["finished"]),
                         ("2026-01-15 01:30", "2026-07-15 02:30"))

    def test_a_run_that_has_not_finished_keeps_its_absence(self):
        # `None` is the honest answer and it must not become an invented time.
        job = self._started("2026-07-15T00:30:00+00:00")
        self.assertEqual(
            to_summary_view([_run(finished=None)], {}, None,
                            zone=ZONE)["jobs"][0]["finished"], None)

    def test_a_stamp_naming_no_instant_comes_back_exactly_as_stored(self):
        # A naive stamp names no instant, so converting it would invent the
        # offset it is missing. It reads unlike every row around it, which is
        # the signal that something upstream wrote a stamp it should not have.
        self.assertEqual(self._started("2026-07-15 00:30:00")["started"],
                         "2026-07-15 00:30:00")

    def test_the_page_names_the_zone_once_rather_than_per_row(self):
        # Nothing above carries an offset any more, so the page has to say
        # what its times are in -- and say it in one place, not on every row.
        html = _summary_html(runs=[_run(), _run(job="tag-scan")],
                             zone=ZoneInfo("America/New_York"))
        self.assertEqual(html.count("America/New_York"), 1)
        self.assertIn("Times are shown in America/New_York.", html)

    def test_the_plan_zone_and_hour_land_where_the_spec_says(self):
        # The worked example the design document states, end to end.
        view = to_summary_view(
            runs=[{"id": "r1", "job": "scene-scan", "trigger": "scheduled",
                   "started": "2026-07-30T07:00:00+00:00",
                   "finished": "2026-07-30T07:04:00+00:00",
                   "outcome": "completed",
                   "counts": {"proposed": 4, "refused": 9}, "error": None}],
            waiting={"scene": 254, "tag": 675, "performer": 3},
            schedule=None, zone=ZoneInfo("America/New_York"))
        job = view["jobs"][0]
        self.assertEqual(job["job"], "scene-scan")
        self.assertEqual(job["trigger"], "scheduled")
        self.assertEqual(job["counts"], {"proposed": 4, "refused": 9})
        self.assertEqual(job["started"], "2026-07-30 03:00")


class AFailedRunShowsItsError(unittest.TestCase):
    """What a row on the summary page puts in front of a reader.

    A failed run's counters hold whatever it had reached before it stopped, so
    drawing them as its result says "the pass ran and found this much" about a
    pass that did not finish -- and the person checking whether last night
    worked comes away believing it did.
    """

    def test_a_failed_run_shows_its_error(self):
        html = _summary_html(runs=[_run(outcome="failed", counts={},
                                        error="the box refused")])
        self.assertIn("FAILED &mdash; the box refused", html)

    def test_a_failed_run_does_not_show_the_counts_it_has(self):
        # Counts it DOES have, so this cannot pass because there were none:
        # the run got through four before it stopped.
        html = _summary_html(runs=[_run(outcome="failed",
                                        counts={"recorded": 4},
                                        error="the box refused")])
        self.assertIn("the box refused", html)
        self.assertNotIn("recorded 4", html)

    def test_a_completed_run_shows_its_counts(self):
        # The permissive side. A branch that showed the error for every
        # outcome would leave every successful pass reporting nothing.
        html = _summary_html(runs=[_run(outcome="completed",
                                        counts={"recorded": 4, "skipped": 9})])
        self.assertIn("recorded 4, skipped 9", html)

    def test_a_completed_run_that_found_nothing_says_so(self):
        # An empty counts map is a real answer -- "it ran and there was
        # nothing" -- and a blank line is not one.
        html = _summary_html(runs=[_run(outcome="completed", counts={})])
        self.assertIn("nothing recorded", html)

    def test_a_run_still_going_is_not_drawn_as_one_that_found_nothing(self):
        html = _summary_html(runs=[_run(finished=None, outcome=None,
                                        counts={}, error=None)])
        self.assertIn("still running", html)
        self.assertNotIn("nothing recorded", html)

    def test_a_hostile_error_and_job_name_are_escaped(self):
        # An error carries a media server's own text and a job name reaches
        # the page from the store; both are escaped for the reason every row
        # field is.
        html = _summary_html(runs=[_run(job=_HOSTILE, outcome="failed",
                                        error=_HOSTILE)])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class WhatIsWaitingIsALinkToTheInbox(unittest.TestCase):
    """A count a reader cannot follow is a count they learn to skip."""

    _LINK = re.compile(r'<p><a href="(?P<href>[^"]*)">(?P<name>[^<]*)</a> '
                       r'&mdash; (?P<count>\d+)</p>')

    def test_each_count_is_a_link_to_the_inbox(self):
        # The whole element, and all three of them: a test asserting one row's
        # href would pass while the others pointed anywhere at all.
        html = _summary_html(waiting={"scenes": 254, "tags": 675,
                                      "performers": 3})
        self.assertEqual(self._LINK.findall(html),
                         [("/inbox", "scenes", "254"),
                          ("/inbox", "tags", "675"),
                          ("/inbox", "performers", "3")])

    def test_nothing_waiting_says_so_rather_than_drawing_no_section(self):
        html = _summary_html(waiting={})
        self.assertIn("Nothing waiting.", html)
        self.assertEqual(self._LINK.findall(html), [])

    def test_a_page_with_no_runs_yet_says_that_too(self):
        self.assertIn("No pass has run yet.", _summary_html())


if __name__ == "__main__":
    unittest.main()


def _identified_row(**over):
    payload = {
        "path": "/library/x/%s.mp4" % _HOSTILE,
        "candidate": {"id": "c-1", "title": _HOSTILE, "image": None,
                     "performers": [], "studio": None},
        "identified_by": "fingerprint",
        "box": _HOSTILE,
        "remote_site_id": "r-77",
    }
    item = {"fingerprint": "fp-2", "state": "new", "summary": "s",
            "confidence": None, "payload": payload, "prior_state": None,
            "subject_id": "1"}
    item.update(over)
    return to_row(item)


class IdentifiedRowRendering(unittest.TestCase):
    """A proposal a stash-box identified renders as an identification.

    The template reads `creator` and `creator_source` off every row, and
    Jinja renders `None` as the literal text "None" rather than raising -- so
    without a branch the page would tell a person the file was attributed to
    "None (from the None)" and look, at a glance, exactly like a row that had
    been resolved.
    """

    def test_the_page_names_the_box_instead_of_a_creator(self):
        html = render("inbox.html", rows=[_identified_row()], counts={})
        self.assertIn("identified by fingerprint", html)
        self.assertNotIn("from the None", html)
        self.assertNotIn(">None<", html)

    def test_a_scored_row_still_names_its_creator_and_its_source(self):
        # The other side: the branch must not swallow the ordinary row.
        html = render("inbox.html", rows=[_row()], counts={})
        self.assertIn("from the folder", html)
        self.assertNotIn("identified by fingerprint", html)

    def test_the_box_name_is_escaped_like_every_other_field(self):
        html = render("inbox.html", rows=[_identified_row()], counts={})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_identified_row_renders_in_every_section_that_shows_rows(self):
        # Dismissed and Superseded render their own copy of the attribution
        # line rather than going through `proposal_block`. A branch added to
        # one and not the others would leave "None (from the None)" on a row
        # that had merely been dismissed.
        for section in ("dismissed", "superseded"):
            html = render("inbox.html", rows=[], counts={},
                          **{section: [_identified_row()]})
            self.assertIn("identified by fingerprint", html, section)
            self.assertNotIn("from the None", html, section)


def _loop_status(**over):
    """A `LoopStatus` the way the scheduler hands one out.

    Built from the real dataclass rather than a stand-in dict, so a field
    renamed on it fails here instead of rendering blank -- and so the
    defaults this fixture leaves alone are the ones production actually
    carries.
    """
    fields = dict(running=True, closed=False, ticks=7, failures=0,
                  consecutive_failures=0,
                  last_tick_at="2026-07-27T03:00:00+00:00",
                  last_error=None, last_error_at=None, last_traceback=None,
                  failing_to_start={}, last_result=None)
    fields.update(over)
    return LoopStatus(**fields)


def _tick_result(**over):
    fields = dict(at="2026-07-27T03:00:00+00:00", due=[], started={},
                  skipped={}, failed_to_start={})
    fields.update(over)
    return TickResult(**fields)


class TheSchedulePanel(unittest.TestCase):
    """What an operator asks the page when the inbox has stopped filling.

    Three questions, and the page has to answer all three: is the loop alive,
    when did it last look, and why has the thing they are waiting for not
    run. A panel that reported only what STARTED would answer the first two
    and leave the third -- the reason `due` returns reasons at all -- with
    nowhere to be seen.
    """

    def test_it_says_why_a_due_producer_did_not_run(self):
        status = _loop_status(last_result=_tick_result(
            due=["nightly-library-scan"],
            skipped={"nightly-library-scan":
                     "cost class saturated: scraping is already running "
                     "library-scan"}))
        html = render("inbox.html", rows=[], counts={}, schedule=status)
        self.assertIn("nightly-library-scan &mdash; did not run: cost class "
                      "saturated: scraping is already running library-scan",
                      html)

    def test_a_refusal_it_cannot_retry_is_shown_apart_from_a_skip(self):
        # A skip is a "not now" the next tick may resolve on its own; a
        # failure to start is the runner refusing in a way repeating will not
        # fix. Collapsing them would send somebody to wait out a condition
        # that is never going to clear.
        status = _loop_status(last_result=_tick_result(
            due=["nightly-library-scan"],
            skipped={"other-producer": "disabled by override"},
            failed_to_start={"nightly-library-scan":
                             "RunnerClosed: the runner is closed"}))
        html = render("inbox.html", rows=[], counts={}, schedule=status)
        self.assertIn("did not run: disabled by override", html)
        self.assertIn("could not start: RunnerClosed: the runner is closed",
                      html)

    def test_it_says_what_started_and_when_the_loop_last_looked(self):
        status = _loop_status(last_result=_tick_result(
            due=["nightly-library-scan"],
            started={"nightly-library-scan": "job-77"}))
        html = render("inbox.html", rows=[], counts={}, schedule=status)
        self.assertIn("started as job job-77", html)
        self.assertIn("2026-07-27T03:00:00+00:00", html)
        self.assertIn("due at that tick: nightly-library-scan", html)

    def test_a_loop_that_died_is_not_drawn_as_one_that_was_stopped(self):
        # The whole reason `running` and `closed` are two fields. Not running
        # and closed is a clean shutdown; not running and NOT closed is a
        # loop that died, and the only symptom otherwise is an inbox that
        # stopped filling.
        died = render("inbox.html", rows=[], counts={},
                      schedule=_loop_status(running=False, closed=False,
                                            failures=3))
        self.assertIn("NOT RUNNING", died)
        stopped = render("inbox.html", rows=[], counts={},
                         schedule=_loop_status(running=False, closed=True))
        self.assertNotIn("NOT RUNNING", stopped)
        self.assertIn("stopped", stopped)

    def test_nothing_scheduled_says_so_rather_than_drawing_an_idle_loop(self):
        # An install with no media server or no adapter schedules nothing.
        # Rendered as an empty panel it is indistinguishable from a healthy
        # schedule that has simply not been due yet, which is the one reading
        # that would leave somebody waiting for a scan that is never coming.
        html = render("inbox.html", rows=[], counts={}, schedule=None)
        self.assertIn("Nothing is scheduled", html)
        self.assertNotIn("last tick", html)

    def test_a_reason_from_the_schedule_is_escaped_like_every_other_field(self):
        status = _loop_status(last_result=_tick_result(
            due=[_HOSTILE], skipped={_HOSTILE: _HOSTILE}))
        html = render("inbox.html", rows=[], counts={}, schedule=status)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


# -- description proposals ------------------------------------------------- #


def _description_row(**over):
    item = {"fingerprint": "fp-d", "state": "new",
            "subject_type": "performer", "subject_id": "7",
            "summary": "s", "confidence": None, "prior_state": None,
            "payload": {"name": "Wren Alderly", "field": "details",
                        "faults": ["markup", "entity"],
                        "original": "<p>Marsh &amp; Holloway.</p>\n\nSecond.",
                        "cleaned": "Marsh & Holloway.\n\nSecond."}}
    item.update(over)
    return to_description_row(item, base_url=over.pop("base_url", None))


class DescriptionBlock(unittest.TestCase):
    _FORM_RE = re.compile(
        r'<form method="post" action="(?P<action>[^"]+)">'
        r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
        r'<button>(?P<label>[^<]+)</button></form>')

    def _html(self, row, **kwargs):
        return render("inbox.html", rows=[row], counts={}, **kwargs)

    def test_the_page_shows_the_text_now_and_the_text_proposed(self):
        # THE review. A page showing only the proposed value asks somebody to
        # approve a write to a field whose previous contents are not on the
        # screen -- so both are asserted present, escaped, and in full.
        html = self._html(_description_row())

        self.assertIn("&lt;p&gt;Marsh &amp;amp; Holloway.&lt;/p&gt;", html)
        self.assertIn("Marsh &amp; Holloway.", html)
        self.assertIn("Now", html)
        self.assertIn("Proposed", html)

    def test_the_faults_it_found_are_named_on_the_row(self):
        html = self._html(_description_row())
        self.assertIn("markup", html)
        self.assertIn("entity", html)

    def test_the_performer_is_named_and_linked(self):
        html = self._html(_description_row(base_url="http://media.example"))
        self.assertIn("Wren Alderly", html)
        self.assertIn("http://media.example/performers/7", html)

    def test_a_new_row_offers_exactly_approve_dismiss_mute_refresh(self):
        html = self._html(_description_row(state="new"))
        self.assertEqual(
            [(m.group("action"), m.group("label"), m.group("fp"))
             for m in self._FORM_RE.finditer(html)],
            [("/approve", "Approve", "fp-d"),
             ("/dismiss", "Dismiss", "fp-d"),
             ("/mute", "Mute", "fp-d"),
             ("/refresh", "Refresh", "fp-d")])

    def test_an_applied_row_with_a_snapshot_offers_undo_and_refresh(self):
        html = self._html(_description_row(
            state="applied", prior_state={"details": "<p>x</p>"}))
        self.assertEqual(
            [(m.group("action"), m.group("label"))
             for m in self._FORM_RE.finditer(html)],
            [("/undo", "Undo"), ("/refresh", "Refresh")])

    def test_the_two_kinds_of_proposal_render_side_by_side_on_one_page(self):
        # HARM: the page draws one list. A dispatcher that only ever picked
        # one shape would render every row of the other kind as a block with
        # its identifying fields blank -- which Jinja does silently, because
        # an undefined attribute is empty text rather than an error.
        html = render("inbox.html", counts={},
                      rows=[_row(), _description_row()])

        self.assertIn("Wren Alderly", html)
        self.assertIn("0.812", html)

    def test_a_description_row_carries_no_invented_score(self):
        # Nothing scored it. Any number would sit in the same column, in the
        # same type, as numbers the scorer really produced.
        html = self._html(_description_row())
        self.assertNotIn('<div class="score">', html)


# -- tag-description proposals ---------------------------------------------- #
#
# Every tag name and description below is invented.

TAG_DESCRIPTION = "Scenes lit only by a hand-carried lamp."


def _tag_description_row(**over):
    """One `TagDescriptionRow`, built through the real proposal function and
    the real row builder rather than hand-assembled: Jinja renders a missing
    attribute as empty text instead of raising, so a hand-written payload
    would fail by drawing a blank block rather than by saying anything."""
    base_url = over.pop("base_url", None)
    built = tag_description_proposal(
        {"id": "7", "name": "Lantern Work", "aliases": [],
         "description": None, "scene_count": 3},
        Found(description=over.pop("description", TAG_DESCRIPTION),
              box=over.pop("box", "first")),
        folder="library")
    item = {"fingerprint": "fp-t", "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "summary": built["summary"],
            "confidence": None, "prior_state": None,
            "payload": built["payload"]}
    item.update(over)
    return to_tag_description_row(item, base_url=base_url)


class TagDescriptionBlock(unittest.TestCase):
    _FORM_RE = re.compile(
        r'<form method="post" action="(?P<action>[^"]+)">'
        r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
        r'<button>(?P<label>[^<]+)</button></form>')

    def _html(self, row, **kwargs):
        return render("inbox.html", rows=[row], counts={}, **kwargs)

    def test_the_source_is_named_on_the_page(self):
        # HARM: a reviewer can judge whether a sentence reads well and cannot
        # judge whether anybody wrote it. The source is the only thing on
        # this row that answers that, so a page that dropped it would be
        # asking for approval of an unattributed sentence.
        html = self._html(_tag_description_row())

        self.assertIn("first", html)
        self.assertIn(TAG_DESCRIPTION, html)

    def test_the_empty_field_it_would_fill_is_shown_beside_the_text(self):
        html = self._html(_tag_description_row())

        self.assertIn("Now", html)

    def test_the_tag_is_named_and_linked(self):
        html = self._html(_tag_description_row(base_url="http://media.example"))

        self.assertIn("Lantern Work", html)
        self.assertIn("http://media.example/tags/7", html)

    def test_a_new_row_offers_exactly_approve_dismiss_mute_refresh(self):
        html = self._html(_tag_description_row(state="new"))

        self.assertEqual(
            [(m.group("action"), m.group("label"), m.group("fp"))
             for m in self._FORM_RE.finditer(html)],
            [("/approve", "Approve", "fp-t"),
             ("/dismiss", "Dismiss", "fp-t"),
             ("/mute", "Mute", "fp-t"),
             ("/refresh", "Refresh", "fp-t")])

    def test_an_applied_row_with_a_snapshot_offers_undo_and_refresh(self):
        html = self._html(_tag_description_row(
            state="applied", prior_state={"description": None}))

        self.assertEqual(
            [(m.group("action"), m.group("label"))
             for m in self._FORM_RE.finditer(html)],
            [("/undo", "Undo"), ("/refresh", "Refresh")])

    def test_all_three_kinds_of_proposal_render_on_one_page(self):
        # HARM: the page draws ONE list. A dispatcher missing a branch draws
        # the row through the scene shape, and Jinja fills every field it
        # does not have with empty text -- a block a person cannot judge and
        # can still approve.
        html = render("inbox.html", counts={},
                      rows=[_row(), _description_row(),
                            _tag_description_row()])

        self.assertIn("Wren Alderly", html)
        self.assertIn("Lantern Work", html)
        self.assertIn("0.812", html)

    def test_it_carries_no_invented_score(self):
        html = self._html(_tag_description_row())

        self.assertNotIn('<div class="score">', html)

    def test_a_hostile_tag_description_is_escaped(self):
        html = self._html(_tag_description_row(description=_HOSTILE,
                                               box=_HOSTILE))

        self.assertNotIn("<script>", html)


# -- tag-merge proposals ---------------------------------------------------- #


def _merge_row(tags=None, **over):
    """One `MergeRow`, built through the real producer and the real row
    builder rather than hand-assembled, for the reason `_row` above is: a
    hand-written payload can carry a shape the producer never emits, and
    Jinja renders a missing attribute as empty text instead of raising.
    """
    if tags is None:
        tags = [{"id": "1", "name": "Velvet Crane", "aliases": [], "description": None,
                 "scene_count": 12},
                {"id": "9", "name": "VelvetCrane", "aliases": [], "description": None,
                 "scene_count": 4}]
    built = tag_proposal(cluster_tags(tags)[0], "library", [])
    item = {"fingerprint": "fp-m", "state": "new",
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "summary": built["summary"],
            "confidence": None, "payload": built["payload"],
            "prior_state": None}
    item.update(over)
    return to_merge_row(item)


_THREE_SPELLINGS = [
    {"id": "1", "name": "IvyMayKingsley", "aliases": [], "description": None, "scene_count": 1},
    {"id": "2", "name": "Ivy MayKingsley", "aliases": [], "description": None, "scene_count": 2},
    {"id": "3", "name": "Ivy May Kingsley", "aliases": [], "description": None, "scene_count": 3},
]


class TagMergeSection(unittest.TestCase):
    """The section a merge is judged in. It is not the scene block, and the
    two things it has that the scene block cannot carry -- per-spelling item
    counts, and a warning about a write that cannot be taken back -- are what
    every test here is about.
    """

    _MERGE_FORM_RE = re.compile(
        r'<form method="post" action="/approve">'
        r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">'
        r'<button>Merge</button></form>')
    _UNDO_FORM_RE = re.compile(r'action="/undo"')

    def _render(self, merges, **over):
        kwargs = dict(rows=[], counts={}, muted=[], dismissed=[], refused=[],
                      superseded=[], applied=[], merges=merges)
        kwargs.update(over)
        return render("inbox.html", **kwargs)

    def test_the_section_carries_its_count_while_collapsed(self):
        self.assertIn("Tag merges (1)", self._render([_merge_row()]))
        self.assertIn("Tag merges (0)", self._render([]))

    def test_every_spelling_and_its_own_count_are_on_the_page(self):
        # The blast radius is the number that decides whether a merge is
        # safe, and it has to be in front of the reviewer rather than
        # something they go and look up.
        html = self._render([_merge_row()])

        self.assertIn("Velvet Crane", html)
        self.assertIn("12 scenes", html)
        self.assertIn("VelvetCrane", html)
        self.assertIn("4 scenes", html)

    def test_the_total_sits_where_a_score_would_and_says_what_it_counts(self):
        # An unlabelled number in that slot reads as a score, and the two
        # could hardly mean more different things: one is how sure the tool
        # is, the other is how much a person is about to move.
        html = self._render([_merge_row()])

        self.assertIn('<div class="score">16<div class="note">scenes</div>',
                      html)

    def test_the_irreversibility_warning_is_on_a_new_merge(self):
        # Before the write, when it can still change the answer. Rendered as
        # a warning, not as plain text beside it: the sentence that says
        # "this cannot be taken back" must not read like the list a reviewer
        # can skim past.
        html = self._render([_merge_row()])

        self.assertIn("cannot be undone", html)
        self.assertRegex(html, r'<div class="warn">[^<]*cannot be undone')

    def test_the_irreversibility_warning_is_still_there_after_the_merge(self):
        # The sources are gone by now. A page that stopped saying so would
        # leave a person hunting for the Undo that is not there.
        html = self._render([_merge_row(state="applied")])

        self.assertIn("cannot be undone", html)

    def test_no_merge_row_ever_offers_an_undo(self):
        for state in ("new", "seen", "applied", "failed"):
            with self.subTest(state=state):
                html = self._render([_merge_row(state=state)])
                self.assertEqual(self._UNDO_FORM_RE.findall(html), [], state)

    def test_a_decided_cluster_offers_exactly_one_merge_control(self):
        html = self._render([_merge_row()])

        self.assertEqual(self._MERGE_FORM_RE.findall(html), ["fp-m"])
        self.assertIn("Merge into <b>Velvet Crane</b>", html)
        self.assertIn("Deletes: VelvetCrane", html)

    def test_an_undecided_cluster_offers_no_merge_control_and_says_why(self):
        # Three spellings do not say which is canonical. Offering the button
        # would ask a person to authorise a write nothing has specified.
        html = self._render([_merge_row(tags=_THREE_SPELLINGS)])

        self.assertEqual(self._MERGE_FORM_RE.findall(html), [])
        self.assertIn("3 spellings of one tag", html)
        self.assertIn("three or more spellings share this form", html)

    def test_two_merges_each_get_their_own_control_not_a_bulk_one(self):
        rows = [_merge_row(fingerprint="a"), _merge_row(fingerprint="b")]

        self.assertEqual(self._MERGE_FORM_RE.findall(self._render(rows)),
                         ["a", "b"])

    def test_a_muted_cluster_offers_its_unmute_with_the_subject_pair(self):
        # A mute is keyed by (subject_type, subject_id), not by fingerprint,
        # so this control posts the pair -- and a tag cluster's subject is
        # the CLUSTER, never one of its tags.
        html = self._render([_merge_row(state="muted")])

        self.assertIn('name="subject_type" value="tag-cluster"', html)
        self.assertIn('name="subject_id" value="velvetcrane"', html)
        self.assertIn("<button>Unmute</button>", html)

    def test_a_hostile_tag_name_is_escaped(self):
        rows = [_merge_row(tags=[
            {"id": "1", "name": _HOSTILE + " x", "aliases": [], "description": None,
             "scene_count": 1},
            {"id": "2", "name": _HOSTILE + "x", "aliases": [], "description": None,
             "scene_count": 2}])]

        self.assertNotIn("<script>", self._render(rows))

    def test_a_description_the_survivor_would_inherit_is_on_the_page(self):
        # HARM: the merge deletes the only spelling carrying it. A page that
        # did not show the text asks for an irreversible write without
        # showing what it moves.
        rows = [_merge_row(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": None, "scene_count": 12},
            {"id": "2", "name": "LanternWork", "aliases": [],
             "description": TAG_DESCRIPTION, "scene_count": 4}])]

        html = self._render(rows)

        self.assertIn(TAG_DESCRIPTION, html)
        self.assertIn("LanternWork", html)

    def test_two_differing_descriptions_are_both_shown_as_a_warning(self):
        # HARM: the merge keeps only one of two sentences somebody wrote, and
        # nothing afterwards records the other. Shown where warnings live,
        # because approving loses it.
        other = "Filmed aboard a working passenger boat."
        rows = [_merge_row(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": TAG_DESCRIPTION, "scene_count": 12},
            {"id": "2", "name": "LanternWork", "aliases": [],
             "description": other, "scene_count": 4}])]

        html = self._render(rows)

        self.assertIn(TAG_DESCRIPTION, html)
        self.assertIn(other, html)
        self.assertIn("Nothing here picks between them", html)

    def test_a_hostile_description_on_a_merge_row_is_escaped(self):
        rows = [_merge_row(tags=[
            {"id": "1", "name": "Lantern Work", "aliases": [],
             "description": None, "scene_count": 1},
            {"id": "2", "name": "LanternWork", "aliases": [],
             "description": _HOSTILE, "scene_count": 2}])]

        self.assertNotIn("<script>", self._render(rows))


class EveryTimeOnThePageReadsInTheConfiguredZone(unittest.TestCase):
    """One page, both sections that carry a time, one zone.

    A test asserting a single row's format would pass while every other
    section stayed in UTC -- which is the failure worth designing against here,
    because a page half in the operator's hour and half in UTC is unreadable in
    a way a page entirely in UTC is not: nothing on it says which line is
    which. So this renders the WHOLE page once, with a mute and a schedule
    panel on it, and asserts across both.

    The values are hand-computed from Europe/Madrid's own offsets: +01:00 in
    January, +02:00 in July. Both sides of a transition, because a fixture on
    one date cannot tell a zone from a constant offset.
    """

    WINTER_UTC = "2026-01-15T00:30:00+00:00"
    WINTER_LOCAL = "2026-01-15T01:30:00+01:00"
    SUMMER_UTC = "2026-07-15T00:30:00+00:00"
    SUMMER_LOCAL = "2026-07-15T02:30:00+02:00"

    def _page(self, mute_at, tick_at, skipped_at):
        muted = [_muted(at=mute_at, zone=ZONE)]
        status = to_schedule_view(
            _loop_status(last_tick_at=tick_at,
                         last_result=_tick_result(
                             due=["nightly-library-scan"],
                             skipped={"nightly-library-scan":
                                      "last ran %s; next due at %s"
                                      % (skipped_at, skipped_at)})),
            zone=ZONE)
        return render("inbox.html", rows=[], counts={}, muted=muted,
                      schedule=status)

    def test_both_sections_read_local_on_the_summer_side(self):
        html = self._page(self.SUMMER_UTC, self.SUMMER_UTC, self.SUMMER_UTC)
        self.assertIn("muted %s" % self.SUMMER_LOCAL, html)
        self.assertIn("last tick %s" % self.SUMMER_LOCAL, html)
        self.assertIn("last ran %s; next due at %s"
                      % (self.SUMMER_LOCAL, self.SUMMER_LOCAL), html)
        # And nothing anywhere on the page is still showing the stored text.
        self.assertNotIn(self.SUMMER_UTC, html)

    def test_both_sections_read_local_on_the_winter_side(self):
        # The same page across a daylight-saving transition. An offset applied
        # as a constant passes exactly one of this pair.
        html = self._page(self.WINTER_UTC, self.WINTER_UTC, self.WINTER_UTC)
        self.assertIn("muted %s" % self.WINTER_LOCAL, html)
        self.assertIn("last tick %s" % self.WINTER_LOCAL, html)
        self.assertIn("last ran %s; next due at %s"
                      % (self.WINTER_LOCAL, self.WINTER_LOCAL), html)
        self.assertNotIn(self.WINTER_UTC, html)

    def test_no_section_is_left_behind_when_the_others_are_converted(self):
        # DIFFERENT stamps in each of the three places, so "converted the one
        # I asserted" cannot pass: each expected value is only producible by
        # converting the stamp that particular section was given.
        html = self._page(self.WINTER_UTC, self.SUMMER_UTC,
                          "2026-07-14T23:30:00+00:00")
        self.assertIn("muted %s" % self.WINTER_LOCAL, html)
        self.assertIn("last tick %s" % self.SUMMER_LOCAL, html)
        self.assertIn("last ran 2026-07-15T01:30:00+02:00", html)
