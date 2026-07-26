import unittest

from cronicled.scoring import Match
from cronicled.stash import StashError
from cronicled.stashbox import (
    PERFORMER_SCENES, SCENES_BY_FINGERPRINT, StashBox)


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


def _blocks(blocks):
    """One `findScenesBySceneFingerprints` reply: a list of scene lists,
    positionally aligned with the fingerprint sets that were submitted."""
    return {"data": {"findScenesBySceneFingerprints": [list(b) for b in blocks]}}


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


class FingerprintLookup(unittest.TestCase):
    def test_a_whole_batch_goes_out_in_one_request(self):
        # `findScenesBySceneFingerprints` takes a LIST of fingerprint sets for
        # exactly this reason. Asking once per fingerprint would be a
        # rate-limit incident against a public service that every other test
        # here would sail straight past, so the count is asserted, and the
        # body is asserted whole beside it: a hash sent under the wrong
        # algorithm, or an extra field on the query input, would each ask a
        # different question than the caller did.
        t = _transport([_blocks([[{"id": "s1"}], []])])
        box = StashBox("https://box.test", "k", transport=t)

        box.known_by_fingerprint(
            [("PHASH", "aaaa1111"), ("OSHASH", "bbbb2222")], timeout=11)

        self.assertEqual(len(t.calls), 1, "one request for the whole batch")
        # The batch endpoint by name, taking a list OF LISTS. A fake transport
        # cannot check a query against a real schema, so the two properties
        # that make per-fingerprint alignment possible at all are pinned here:
        # the sibling `findScenesByFullFingerprints` takes one flat set and
        # answers one flat list, which would compile, run, and lose every
        # fingerprint's identity on the way back.
        self.assertIn("findScenesBySceneFingerprints(fingerprints: $fingerprints)",
                      SCENES_BY_FINGERPRINT)
        self.assertIn("[[FingerprintQueryInput!]!]!", SCENES_BY_FINGERPRINT)
        self.assertEqual([body for body, _ in t.calls], [{
            "query": SCENES_BY_FINGERPRINT,
            "variables": {"fingerprints": [
                [{"hash": "aaaa1111", "algorithm": "PHASH"}],
                [{"hash": "bbbb2222", "algorithm": "OSHASH"}],
            ]},
        }])
        self.assertEqual([timeout for _, timeout in t.calls], [11])

    def test_results_are_matched_back_to_the_fingerprint_that_asked(self):
        # The endpoint answers positionally and says nothing about which set
        # each block belongs to, so the alignment is the client's to keep. The
        # three fingerprints deliberately get three DIFFERENT answers — one
        # hit, none, two hits — so any reordering, any off-by-one, and any
        # flattening of the blocks lands scenes under the wrong hash. This
        # also pins the empty middle: a fingerprint that matched nothing is
        # PRESENT with an empty list. "I asked and there is nothing" and "I
        # never asked" are the two facts this whole client exists to keep
        # apart, and an absent key collapses them.
        t = _transport([_blocks([
            [{"id": "s1"}],
            [],
            [{"id": "s2"}, {"id": "s3"}],
        ])])
        box = StashBox("https://box.test", "k", transport=t)

        found = box.known_by_fingerprint([
            ("PHASH", "aaaa1111"), ("OSHASH", "bbbb2222"), ("MD5", "cccc3333")])

        self.assertEqual(
            {fp: [hit.scene["id"] for hit in hits] for fp, hits in found.items()},
            {
                ("PHASH", "aaaa1111"): ["s1"],
                ("OSHASH", "bbbb2222"): [],
                ("MD5", "cccc3333"): ["s2", "s3"],
            })

    def test_a_hit_says_which_algorithm_matched(self):
        # A perceptual hash matching is "these look alike"; an oshash matching
        # is "this is the same file". A caller holding a hit must be able to
        # tell which claim it has without going back to the key it came from,
        # so the algorithm rides on the hit — and the two submitted here
        # differ, so an implementation that hardcodes one or reads the wrong
        # position cannot pass.
        t = _transport([_blocks([[{"id": "s1"}], [{"id": "s2"}, {"id": "s3"}]])])
        box = StashBox("https://box.test", "k", transport=t)

        found = box.known_by_fingerprint(
            [("OSHASH", "aaaa1111"), ("PHASH", "bbbb2222")])

        self.assertEqual(
            {fp: [(hit.algorithm, hit.hash) for hit in hits]
             for fp, hits in found.items()},
            {
                ("OSHASH", "aaaa1111"): [("OSHASH", "aaaa1111")],
                ("PHASH", "bbbb2222"): [("PHASH", "bbbb2222"),
                                        ("PHASH", "bbbb2222")],
            })

    def test_a_hit_is_not_a_score(self):
        # A hash match IDENTIFIES; a title match SCORES. The legacy tool wrote
        # a flat 1.0 for a fingerprint hit into the same field a computed
        # similarity goes in, after which nothing downstream could tell an
        # identity from a good guess. The attribute set is asserted WHOLE, not
        # probed key by key, because the failure being prevented is a field
        # being ADDED — and a sampled assertion is blind to exactly that.
        t = _transport([_blocks([[{"id": "s1", "title": "T"}]])])
        box = StashBox("https://box.test", "k", transport=t)

        hit = box.known_by_fingerprint([("PHASH", "aaaa1111")])[
            ("PHASH", "aaaa1111")][0]

        self.assertEqual(sorted(vars(hit)), ["algorithm", "hash", "scene"])
        self.assertNotIsInstance(hit, Match)
        self.assertEqual(hit.scene, {"id": "s1", "title": "T"})

    def test_an_empty_batch_asks_nothing(self):
        # Nothing to ask about is not a question worth a round trip, and an
        # empty fingerprint list sent to the endpoint is a request that can
        # only waste a rate-limit slot.
        t = _transport([_blocks([])])
        box = StashBox("https://box.test", "k", transport=t)

        self.assertEqual(box.known_by_fingerprint([]), {})
        self.assertEqual(t.calls, [], "issued no request at all")

    def test_a_repeated_fingerprint_is_asked_about_once(self):
        # Two files with the same oshash are the same bytes, and the batch a
        # scan assembles will hold duplicates. Asking twice buys nothing and
        # spends the budget the batching exists to protect; the answer is
        # still complete for every fingerprint submitted.
        t = _transport([_blocks([[{"id": "s1"}], []])])
        box = StashBox("https://box.test", "k", transport=t)

        found = box.known_by_fingerprint([
            ("PHASH", "aaaa1111"), ("OSHASH", "bbbb2222"), ("PHASH", "aaaa1111")])

        self.assertEqual([body for body, _ in t.calls], [{
            "query": SCENES_BY_FINGERPRINT,
            "variables": {"fingerprints": [
                [{"hash": "aaaa1111", "algorithm": "PHASH"}],
                [{"hash": "bbbb2222", "algorithm": "OSHASH"}],
            ]},
        }])
        self.assertEqual(
            {fp: [hit.scene["id"] for hit in hits] for fp, hits in found.items()},
            {("PHASH", "aaaa1111"): ["s1"], ("OSHASH", "bbbb2222"): []})

    def test_a_reply_that_does_not_line_up_with_the_batch_raises(self):
        # The only thing tying a block to a fingerprint is its position, so a
        # reply of the wrong length means the alignment is unknowable. zip()
        # would silently truncate to the shorter of the two and hand back
        # scenes filed under hashes that never matched them — a wrong
        # identification, which is the one outcome worse than no answer. Both
        # directions are pinned: short DROPS fingerprints, long MISFILES them.
        for blocks in ([[{"id": "s1"}]],
                       [[{"id": "s1"}], [], [{"id": "s2"}], []]):
            with self.subTest(replied=len(blocks)):
                t = _transport([_blocks(blocks)])
                box = StashBox("https://box.test", "k", transport=t)

                with self.assertRaises(StashError) as caught:
                    box.known_by_fingerprint([("PHASH", "aaaa1111"),
                                              ("OSHASH", "bbbb2222"),
                                              ("MD5", "cccc3333")])

                # Not transient: the source answered, it answered
                # incoherently, and it will answer the same way again. Marking
                # it retryable would spend the rate-limit budget the batching
                # exists to protect on a request that cannot come good.
                self.assertFalse(caught.exception.transient)

    def test_a_transport_failure_raises_rather_than_reporting_nothing_found(self):
        # The failure mode this guards is the quiet one: swallowing the error
        # and returning every fingerprint mapped to an empty list would say
        # "asked, nothing there" about a question that was never answered —
        # and StashError.transient, which says whether a retry is worth it,
        # would go with it.
        t = _transport([StashError("the source went away", transient=True)])
        box = StashBox("https://box.test", "k", transport=t)

        with self.assertRaises(StashError) as caught:
            box.known_by_fingerprint([("PHASH", "aaaa1111")])

        self.assertTrue(caught.exception.transient, "retryability survives the raise")

    def test_a_fingerprint_that_is_not_one_raises_before_anything_is_sent(self):
        # A malformed entry must not be silently dropped from the batch: the
        # caller would get a mapping missing a key it asked about, which reads
        # as "never asked" for a fingerprint that was. Nor may it go out as-is
        # — an algorithm the source does not know fails the WHOLE batch at the
        # server, taking every well-formed fingerprint beside it down. The
        # lowercase spelling is in the list because a GraphQL enum is
        # case-sensitive and upcasing it here would be guessing on the
        # caller's behalf; the swapped pair is here because both halves are
        # strings, so nothing else would notice the transposition.
        box = StashBox("https://box.test", "k", transport=_transport([_blocks([])]))
        for bad in (("SHA256", "aaaa1111"),   # not an algorithm the source has
                    ("phash", "aaaa1111"),    # right algorithm, wrong spelling
                    ("aaaa1111", "PHASH"),    # transposed
                    ("PHASH", ""),            # no hash at all
                    ("PHASH", None),
                    ("PHASH",),
                    ("PHASH", "aaaa1111", "extra"),
                    "PHASH"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    box.known_by_fingerprint([("MD5", "cccc3333"), bad])

    def test_a_well_formed_fingerprint_is_accepted(self):
        # The other side of the guard above. A rule that drifts too STRICT
        # refuses real evidence and is quieter about it than one that drifts
        # loose, so every algorithm the source accepts is pinned as accepted
        # here — otherwise dropping one from the list breaks nothing.
        t = _transport([_blocks([[], [], []])])
        box = StashBox("https://box.test", "k", transport=t)

        found = box.known_by_fingerprint(
            [("MD5", "a"), ("OSHASH", "b"), ("PHASH", "c")])

        self.assertEqual(sorted(found), [("MD5", "a"), ("OSHASH", "b"),
                                         ("PHASH", "c")])


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
            _blocks([[{"id": "s4"}]]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        surface = sorted(name for name in dir(box)
                         if not name.startswith("_") and callable(getattr(box, name)))
        self.assertEqual(surface, ["known_by_fingerprint", "performer_catalogue"],
                         "a new call on this client must be exercised here too")
        box.performer_catalogue("p1", per_page=2)
        box.known_by_fingerprint([("PHASH", "aaaa1111")])

        self.assertTrue(t.calls, "the surface was actually exercised")
        for body, _ in t.calls:
            self.assertNotIn("mutation", body["query"].lower())


if __name__ == "__main__":
    unittest.main()
