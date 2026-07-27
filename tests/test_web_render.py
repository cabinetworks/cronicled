import unittest

from cronicled.web.render import environment, render
from cronicled.web.rows import to_row

_HOSTILE = '<script>alert("x")</script>'


def _row(runners_up=None, **over):
    # `runners_up` is its own parameter, not folded into `**over`: it lives
    # under `payload` in the real item shape, and `item.update(over)` only
    # ever touches the top level. Routing it through `over` would silently
    # keep the default fixture's runners_up on every call that tried to
    # override it -- exactly the kind of blank-column bug this task exists to
    # catch, just moved into the test instead of the template.
    payload = {
        "path": "/library/x/%s.mp4" % _HOSTILE,
        "creator": {"name": _HOSTILE, "source": "folder",
                    "competing": _HOSTILE, "rejected_folder": _HOSTILE},
        "candidate": {"id": "c-1", "title": _HOSTILE},
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


if __name__ == "__main__":
    unittest.main()
