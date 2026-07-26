import unittest

from cronicled.stash import StashError
from cronicled.stashbox import PERFORMER_SCENES, StashBox


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
    def test_a_catalogue_that_fits_on_one_page_is_complete(self):
        # The ordinary case: the count matches what the first page handed
        # back, so there is nothing left to ask for.
        t = _transport([_page(2, [{"id": "s1"}, {"id": "s2"}])])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1", per_page=100)

        self.assertEqual([s["id"] for s in catalogue.scenes], ["s1", "s2"])
        self.assertTrue(catalogue.complete)
        self.assertEqual(len(t.calls), 1, "did not ask for a second page")

    def test_a_performer_with_nothing_is_a_complete_answer(self):
        # Nothing promised and nothing served. This is the most valuable
        # answer the module produces — an empty catalogue is the strongest
        # evidence of absence obtainable — and complete=True is what licenses
        # a caller to act on it. Reporting it as a failed read would refuse to
        # vouch for the one case where absence is certain.
        t = _transport([_page(0, [])])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1")

        self.assertEqual(catalogue.scenes, ())
        self.assertTrue(catalogue.complete)
        self.assertEqual(len(t.calls), 1, "did not page past an empty catalogue")

    def test_an_empty_first_page_with_a_count_that_says_otherwise_is_not_complete(self):
        # The source promises scenes and serves none, from the very first
        # page. This looks exactly like the genuinely-empty catalogue above
        # and must not be reported like one: "nothing exists" is the strongest
        # claim this module makes, and here the source itself contradicts it.
        # A count of one is the boundary — the smallest overstatement, and the
        # one a guard drifting loose would swallow first.
        for count in (1, 6):
            with self.subTest(count=count):
                t = _transport([_page(count, [])])
                box = StashBox("https://box.test", "k", transport=t)

                catalogue = box.performer_catalogue("p1", per_page=2, max_pages=10)

                self.assertEqual(catalogue.scenes, ())
                self.assertFalse(catalogue.complete)
                self.assertEqual(len(t.calls), 1, "stopped at the empty page")

    def test_a_count_that_drops_to_zero_mid_read_is_not_complete(self):
        # Two scenes arrived, then the source said it has none. Its tally
        # contradicts what it already sent, and a tally that cannot be trusted
        # cannot vouch for the pages it never handed over.
        t = _transport([
            _page(6, [{"id": "s1"}, {"id": "s2"}]),
            _page(0, []),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1", per_page=2, max_pages=10)

        self.assertEqual(len(catalogue.scenes), 2)
        self.assertFalse(catalogue.complete)
        self.assertEqual(len(t.calls), 2, "stopped at the empty page")

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

    def test_a_transport_failure_part_way_through_raises(self):
        # Pages one and two are discarded rather than returned with
        # complete=False. The two short reads the flag covers are answers a
        # working source gave; this is not one, and StashError.transient says
        # whether retrying is worth it — a fact the flag has nowhere to put.
        # Folding it in would make a wedged host look like an honest partial.
        t = _transport([
            _page(6, [{"id": "s1"}, {"id": "s2"}]),
            _page(6, [{"id": "s3"}, {"id": "s4"}]),
            StashError("the source went away", transient=True),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        with self.assertRaises(StashError) as caught:
            box.performer_catalogue("p1", per_page=2, max_pages=10)

        self.assertTrue(caught.exception.transient, "retryability survives the raise")
        self.assertEqual(len(t.calls), 3, "stopped at the failed page")


class RequestShape(unittest.TestCase):
    def test_every_request_is_asserted_whole(self):
        # Whole bodies, not sampled keys: a renamed criterion, a page that
        # stopped advancing, or a filter quietly gaining a field would each
        # point the read somewhere other than this performer's catalogue, and
        # a key-by-key assertion notices none of them.
        t = _transport([
            _page(3, [{"id": "s1"}, {"id": "s2"}]),
            _page(3, [{"id": "s3"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        box.performer_catalogue("p1", per_page=2, timeout=7)

        self.assertEqual([body for body, _ in t.calls], [
            {"query": PERFORMER_SCENES, "variables": {"input": {
                "performers": {"value": ["p1"], "modifier": "INCLUDES"},
                "page": 1,
                "per_page": 2,
            }}},
            {"query": PERFORMER_SCENES, "variables": {"input": {
                "performers": {"value": ["p1"], "modifier": "INCLUDES"},
                "page": 2,
                "per_page": 2,
            }}},
        ])
        self.assertEqual([timeout for _, timeout in t.calls], [7, 7])

    def test_the_client_only_ever_reads(self):
        # stash-box accepts submissions and edits, and the transport this
        # client borrows will carry either. Nothing but this stops a later
        # change from writing to a source this module has no business writing
        # to.
        t = _transport([
            _page(3, [{"id": "s1"}, {"id": "s2"}]),
            _page(3, [{"id": "s3"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        surface = sorted(name for name in dir(box)
                         if not name.startswith("_") and callable(getattr(box, name)))
        self.assertEqual(surface, ["performer_catalogue"],
                         "a new call on this client must be exercised here too")
        box.performer_catalogue("p1", per_page=2)

        self.assertTrue(t.calls, "the surface was actually exercised")
        for body, _ in t.calls:
            self.assertNotIn("mutation", body["query"].lower())


if __name__ == "__main__":
    unittest.main()
