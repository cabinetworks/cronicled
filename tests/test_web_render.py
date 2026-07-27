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
        self.assertTrue(environment().autoescape,
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
        # dependency was taken to close, and would pass every test above if it
        # sat on a field the fixtures do not populate.
        import pathlib
        root = pathlib.Path("cronicled/web/templates")
        for path in root.rglob("*.html"):
            body = path.read_text()
            self.assertNotIn("|safe", body, "%s disables escaping" % path)
            self.assertNotIn("autoescape false", body,
                             "%s disables escaping" % path)

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
