import unittest

from cronicled.stashbox import StashBox


def _transport(responses):
    """A fake transport. `responses` is a list of dicts or exceptions, returned in
    order; the calls it received are recorded on the function object."""
    calls = []

    def send(body, timeout):
        calls.append((body, timeout))
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    send.calls = calls
    return send


def _page(count, scenes):
    """One `queryScenes` reply, shaped the way stash-box shapes it."""
    return {"data": {"queryScenes": {"count": count, "scenes": list(scenes)}}}


class PerformerCatalogue(unittest.TestCase):
    def test_reads_every_page_and_reports_the_read_complete(self):
        t = _transport([
            _page(3, [{"id": "s1"}, {"id": "s2"}]),
            _page(3, [{"id": "s3"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1", per_page=2)

        self.assertEqual([s["id"] for s in catalogue.scenes], ["s1", "s2", "s3"])
        self.assertTrue(catalogue.complete)

    def test_a_read_that_hits_the_page_cap_is_not_complete(self):
        # Six scenes, two per page, but only two pages are allowed. The four
        # scenes it did read are worth keeping; the claim that they are all of
        # them is not.
        t = _transport([_page(6, [{"id": "s1"}, {"id": "s2"}])])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1", per_page=2, max_pages=2)

        self.assertEqual(len(catalogue.scenes), 4)
        self.assertFalse(catalogue.complete)
        self.assertEqual(len(t.calls), 2, "stopped asking at the cap")

    def test_a_page_that_returns_nothing_ends_the_read_as_incomplete(self):
        # The count says six and the server hands back nothing. Whatever is
        # wrong, asking again forever is not the answer, and the four scenes
        # in hand are not evidence of what the other two are.
        t = _transport([
            _page(6, [{"id": "s1"}, {"id": "s2"}]),
            _page(6, [{"id": "s3"}, {"id": "s4"}]),
            _page(6, []),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1", per_page=2, max_pages=10)

        self.assertEqual(len(catalogue.scenes), 4)
        self.assertFalse(catalogue.complete)
        self.assertEqual(len(t.calls), 3, "stopped at the empty page")


if __name__ == "__main__":
    unittest.main()
