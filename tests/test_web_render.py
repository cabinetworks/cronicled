import re
import unittest

from cronicled.jobs import Job
from cronicled.web.render import environment, render
from cronicled.web.rows import to_row

_HOSTILE = '<script>alert("x")</script>'


def _job(**over):
    job = dict(id="job-1", producer="library-scan-x", cost="scraping",
               state="running", started_at="2026-07-27T00:00:00+00:00",
               finished_at=None, duration=None, message="working",
               recorded=0, skipped=0, error=None, traceback=None)
    job.update(over)
    return Job(**job)


def _row(runners_up=None, image=None, **over):
    # `runners_up` and `image` are their own parameters, not folded into
    # `**over`: both live under `payload` in the real item shape, and
    # `item.update(over)` only ever touches the top level. Routing either
    # through `over` would silently keep the default fixture's value on
    # every call that tried to override it -- exactly the kind of
    # blank-column bug this task exists to catch, just moved into the test
    # instead of the template. `image` defaults to `None` -- "no cover",
    # matching every existing caller here that never mentions a cover.
    payload = {
        "path": "/library/x/%s.mp4" % _HOSTILE,
        "creator": {"name": _HOSTILE, "source": "folder",
                    "competing": _HOSTILE, "rejected_folder": _HOSTILE},
        "candidate": {"id": "c-1", "title": _HOSTILE, "image": image,
                     "performers": [], "studio": None},
        "score": 0.812,
        # Nested under `candidate`, matching what `scan._runners_up` actually
        # emits and what `rows.to_row` requires -- see test_web_rows.py's
        # `test_a_malformed_runner_up_raises_rather_than_rendering_blank`. A
        # flattened top-level "title" here would raise a KeyError before
        # rendering ever got a chance to run.
        "runners_up": ([{"candidate": {"title": _HOSTILE}, "score": 0.61}]
                        if runners_up is None else runners_up),
    }
    item = {"fingerprint": "fp-1", "state": "new", "summary": "s",
            "confidence": 0.812, "payload": payload, "prior_state": None}
    item.update(over)
    return to_row(item)


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
                "confidence": 0.812, "payload": payload, "prior_state": None}

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


class ScanStatusEscaping(unittest.TestCase):
    # A job's `message` carries file names -- attacker-influenceable text,
    # the same reason a row's fields are escaped. This is the same backstop
    # `Autoescaping` runs for row fields, aimed at the one new field this
    # ticket adds to the page.

    def test_the_scan_messages_hostile_content_is_escaped(self):
        html = render("inbox.html", rows=[], counts={},
                      scan=_job(message=_HOSTILE))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_failed_scans_error_is_escaped_too(self):
        html = render("inbox.html", rows=[], counts={},
                      scan=_job(state="failed", error=_HOSTILE))
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
        html = render("inbox.html", rows=[], counts={}, scan=None,
                      scan_default_limit=25)
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
        html = render("inbox.html", rows=[], counts={},
                      scan=_job(state="running"), scan_default_limit=25)
        self.assertEqual(len(self._FORM_RE.findall(html)), 1)


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
        section = html[html.index("Refused (1)"):html.index("<h2>Scan</h2>")]
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


if __name__ == "__main__":
    unittest.main()
