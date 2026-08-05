import re
import unittest

from cronicled.artist import Resolution
from cronicled.scoring import Decision, Match, decide
from cronicled.stash import StashError
from cronicled.stashbox import (
    BOX_TAGS, PERFORMER_PROFILE, PERFORMER_SCENES, PERFORMER_SEARCH,
    SCENES_BY_FINGERPRINT, SourceListing, StashBox, _career_length,
    _joined_modifications, base_url, check, listing_verdict)


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


def _tag_page(count, tags):
    """One `queryTags` reply, shaped the way stash-box shapes it."""
    return {"data": {"queryTags": {"count": count, "tags": list(tags)}}}


def _box_tag(name, description="a description", aliases=()):
    """One tag row as `queryTags` returns it -- every field the query selects,
    always, so a fixture can never answer a question the real reply would
    leave unanswered."""
    return {"id": "t-" + name, "name": name, "description": description,
            "aliases": list(aliases)}


# The fields both queries ask for on a scene, stated HERE rather than read off
# the query — reading it off the query is the defect these pins exist to close.
# Neither query's selection set is reachable any other way: the request bodies
# below are asserted against the query constants themselves, so a field dropped
# from a constant moves both sides of that comparison at once, and a fake
# transport answers whatever the test scripted rather than whatever the query
# actually requested. Confirmed by mutation: dropping any one of these from
# either constant leaves the whole suite green.
#
# What a query does not select, the source never sends, and the scene dicts
# this client hands back simply lack the key. `id` is how an entry is pointed
# at afterwards and `title` is the only field a title match can be scored on,
# so those two are the sharp ones. `date` and `urls` are what a person is shown
# beside a candidate to check it; dropped, every entry from this source arrives
# dateless and linkless, and nothing anywhere says why.
SCENE_FIELDS = ("id", "title", "date", "urls", "url")


def _names_in(query):
    """Every bare name appearing in `query`. Coarse on purpose: nothing here
    can check a query against a real schema, so what is pinned is that the
    names a caller depends on are still being asked for at all."""
    return set(re.findall(r"[A-Za-z_]+", query))


class PerformerListing(unittest.TestCase):
    def test_a_listing_that_fits_on_one_page_is_complete(self):
        # The ordinary case: the count matches what the first page handed
        # back, so there is nothing left to ask for.
        t = _transport([_page(2, [{"id": "s1"}, {"id": "s2"}])])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1", per_page=100)

        self.assertEqual([s["id"] for s in listing.scenes], ["s1", "s2"])
        self.assertTrue(listing.complete)
        self.assertEqual(len(t.calls), 1, "did not ask for a second page")
        self.assertEqual(listing.total, 2)
        self.assertEqual(listing.pages_read, 1)

    def test_a_performer_with_nothing_is_a_complete_answer(self):
        # Nothing promised and nothing served. This is the most valuable
        # answer the module produces — an empty listing is the strongest
        # evidence of absence obtainable — and complete=True is what licenses
        # a caller to act on it. Reporting it as a failed read would refuse to
        # vouch for the one case where absence is certain.
        t = _transport([_page(0, [])])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1")

        self.assertEqual(listing.scenes, ())
        self.assertTrue(listing.complete)
        self.assertEqual(len(t.calls), 1, "did not page past an empty listing")
        self.assertEqual(listing.total, 0)
        self.assertEqual(listing.pages_read, 1)

    def test_an_empty_first_page_with_a_count_that_says_otherwise_is_not_complete(self):
        # The source promises scenes and serves none, from the very first
        # page. This looks exactly like the genuinely-empty listing above
        # and must not be reported like one: "nothing exists" is the strongest
        # claim this module makes, and here the source itself contradicts it.
        # A count of one is the boundary — the smallest overstatement, and the
        # one a guard drifting loose would swallow first.
        for count in (1, 6):
            with self.subTest(count=count):
                t = _transport([_page(count, [])])
                box = StashBox("https://box.test", "k", transport=t)

                listing = box.performer_listing("p1", per_page=2, max_pages=10)

                self.assertEqual(listing.scenes, ())
                self.assertFalse(listing.complete)
                self.assertEqual(len(t.calls), 1, "stopped at the empty page")
                self.assertEqual(listing.total, count)
                self.assertEqual(listing.pages_read, 1)

    def test_a_count_that_drops_to_zero_mid_read_is_not_complete(self):
        # Two scenes arrived, then the source said it has none. Its tally
        # contradicts what it already sent, and a tally that cannot be trusted
        # cannot vouch for the pages it never handed over.
        t = _transport([
            _page(6, [{"id": "s1"}, {"id": "s2"}]),
            _page(0, []),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1", per_page=2, max_pages=10)

        self.assertEqual(len(listing.scenes), 2)
        self.assertFalse(listing.complete)
        self.assertEqual(len(t.calls), 2, "stopped at the empty page")
        # `total` holds the FIRST count the source reported (6), not the
        # contradicting 0 that ended the read -- the later figure is the
        # untrustworthy one here, and the first is still the source's
        # original claim about how much there is.
        self.assertEqual(listing.total, 6)
        self.assertEqual(listing.pages_read, 2)

    def test_reads_every_page_and_reports_the_read_complete(self):
        t = _transport([
            _page(3, [{"id": "s1"}, {"id": "s2"}]),
            _page(3, [{"id": "s3"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1", per_page=2)

        self.assertEqual([s["id"] for s in listing.scenes], ["s1", "s2", "s3"])
        self.assertTrue(listing.complete)
        self.assertEqual(listing.total, 3)
        self.assertEqual(listing.pages_read, 2)

    def test_a_read_that_hits_the_page_cap_is_not_complete(self):
        # Six scenes, two per page, but only two pages are allowed. The four
        # scenes it did read are worth keeping; the claim that they are all of
        # them is not.
        t = _transport([_page(6, [{"id": "s1"}, {"id": "s2"}])])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1", per_page=2, max_pages=2)

        self.assertEqual(len(listing.scenes), 4)
        self.assertFalse(listing.complete)
        self.assertEqual(len(t.calls), 2, "stopped asking at the cap")
        # The ratio a caller can act on: 4 read of the 6 the source promised,
        # across the 2 pages the cap allowed.
        self.assertEqual(listing.total, 6)
        self.assertEqual(listing.pages_read, 2)

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

        listing = box.performer_listing("p1", per_page=2, max_pages=10)

        self.assertEqual(len(listing.scenes), 4)
        self.assertFalse(listing.complete)
        self.assertEqual(len(t.calls), 3, "stopped at the empty page")
        self.assertEqual(listing.total, 6)
        self.assertEqual(listing.pages_read, 3)

    def test_a_short_page_is_not_the_end_of_the_read(self):
        # The COUNT is the authority on completeness, not the page length.
        # Every other test here is also consistent with the ordinary
        # pagination idiom -- `len(page) < per_page` means the last page --
        # and that idiom is wrong for this source: three scenes on a page of
        # five while the count says nine is the "permission filter applied
        # after counting" shape this module's docstring anticipates.
        #
        # Under the page-length rule the read stops here and returns three
        # scenes as complete=True. A truncated listing reported as read in
        # full is the one input that turns every downstream absence into a
        # confident lie, so both the scene count and the number of requests
        # are pinned.
        t = _transport([
            _page(9, [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]),
            _page(9, [{"id": "s%d" % n} for n in range(4, 9)]),
            _page(9, [{"id": "s9"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1", per_page=5, max_pages=10)

        self.assertEqual(len(listing.scenes), 9)
        self.assertTrue(listing.complete)
        self.assertEqual(len(t.calls), 3, "kept asking past the short page")
        self.assertEqual(listing.total, 9)
        self.assertEqual(listing.pages_read, 3)

    def test_a_short_page_the_source_never_makes_up_is_not_complete(self):
        # The same short page, and this time the source never delivers the
        # rest: it hands back three of a promised nine and then nothing. The
        # read is over -- an empty page always ends it -- but three scenes are
        # not nine, and nothing here may be called complete.
        #
        # The disjoint witness for the test above. A page-length rule returns
        # complete=True from the FIRST page in both, so a mutation cannot
        # satisfy one by breaking the other.
        t = _transport([
            _page(9, [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]),
            _page(9, []),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        listing = box.performer_listing("p1", per_page=5, max_pages=10)

        self.assertEqual(len(listing.scenes), 3)
        self.assertFalse(listing.complete)
        self.assertEqual(len(t.calls), 2, "stopped at the empty page")
        self.assertEqual(listing.total, 9)
        self.assertEqual(listing.pages_read, 2)

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
            box.performer_listing("p1", per_page=2, max_pages=10)

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
        # Its selection set, for the same reason and by the same means: see
        # SCENE_FIELDS. Nothing else in this file touched it at all, so until
        # now this query could have come back asking for nothing but an id.
        requested = _names_in(SCENES_BY_FINGERPRINT)
        for field in SCENE_FIELDS:
            self.assertIn(field, requested,
                          "a hit carries the scene this query selects, and %s "
                          "is not being asked for" % (field,))
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
        # point the read somewhere other than this performer's listing, and
        # a key-by-key assertion notices none of them.
        t = _transport([
            _page(3, [{"id": "s1"}, {"id": "s2"}]),
            _page(3, [{"id": "s3"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        box.performer_listing("p1", per_page=2, timeout=7)

        # The query constant is asserted against itself in the bodies below,
        # so mutating it moves both sides and nothing notices. A fake
        # transport cannot check it against a real schema either, so the
        # properties the read depends on are pinned as properties.
        #
        # `title` is the one that matters, and the only one here whose loss is
        # SILENT. Dropped, a real server returns every scene titleless, every
        # candidate scores near zero, `contenders` is 0 and the read is
        # complete -- unlisted=True for every file in the library, over a
        # listing that genuinely was read in full. The others fail loudly:
        # no `count` is a KeyError on the first page, and a wrong endpoint
        # name is a GraphQL error.
        self.assertIn("queryScenes(input: $input)", PERFORMER_SCENES)
        self.assertIn("SceneQueryInput!", PERFORMER_SCENES)
        requested = _names_in(PERFORMER_SCENES)
        self.assertIn("count", requested, "the read depends on count")
        # The rest of the selection set, which the three fields named above
        # left uncovered: `date` and `urls` could both be dropped with the
        # suite still green. See SCENE_FIELDS.
        for field in SCENE_FIELDS:
            self.assertIn(field, requested,
                          "an entry from this listing carries what the query "
                          "selects, and %s is not being asked for" % (field,))
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
            _tag_page(1, [_box_tag("velvet crane")]),
            _profile_reply(_box_performer(id="p1")),
            _search_reply(0, []),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        surface = sorted(name for name in dir(box)
                         if not name.startswith("_") and callable(getattr(box, name)))
        self.assertEqual(surface,
                         ["all_tags", "known_by_fingerprint", "performer_listing",
                          "performer_profile", "search_performers"],
                         "a new call on this client must be exercised here too")
        box.performer_listing("p1", per_page=2)
        box.known_by_fingerprint([("PHASH", "aaaa1111")])
        box.all_tags(per_page=100)
        box.performer_profile("p1")
        box.search_performers("Wren Alderly")

        self.assertTrue(t.calls, "the surface was actually exercised")
        for body, _ in t.calls:
            self.assertNotIn("mutation", body["query"].lower())


class TagCatalogueRead(unittest.TestCase):
    """A source's whole tag catalogue, and whether that was all of it.

    Every tag name and description here is invented.
    """

    def test_a_catalogue_that_fits_on_one_page_is_complete(self):
        t = _transport([_tag_page(2, [_box_tag("Lantern Work"),
                                      _box_tag("Brass Ferry")])])
        box = StashBox("https://box.test", "k", transport=t)

        got = box.all_tags(per_page=100)

        self.assertEqual([row["name"] for row in got.tags],
                         ["Lantern Work", "Brass Ferry"])
        self.assertTrue(got.complete)
        self.assertEqual(len(t.calls), 1, "did not ask for a second page")

    def test_it_pages_until_the_sources_own_count_is_satisfied(self):
        t = _transport([_tag_page(3, [_box_tag("Lantern Work"),
                                      _box_tag("Brass Ferry")]),
                        _tag_page(3, [_box_tag("Slate Harbour")])])
        box = StashBox("https://box.test", "k", transport=t)

        got = box.all_tags(per_page=2)

        self.assertEqual(len(got.tags), 3)
        self.assertTrue(got.complete)
        self.assertEqual([body["variables"]["input"]["page"]
                          for body, _ in t.calls], [1, 2])

    def test_a_source_holding_no_tags_is_a_complete_answer(self):
        t = _transport([_tag_page(0, [])])
        box = StashBox("https://box.test", "k", transport=t)

        got = box.all_tags()

        self.assertEqual(got.tags, ())
        self.assertTrue(got.complete)
        self.assertEqual(len(t.calls), 1, "did not page past an empty reply")

    def test_an_empty_page_the_count_contradicts_is_not_complete(self):
        # The source promises tags and serves none. Reported as complete, the
        # pass would count every tag in the library as one nothing describes.
        # A count of one is the boundary -- the smallest overstatement.
        for count in (1, 9):
            with self.subTest(count=count):
                t = _transport([_tag_page(count, [])])
                box = StashBox("https://box.test", "k", transport=t)

                got = box.all_tags(per_page=2, max_pages=10)

                self.assertEqual(got.tags, ())
                self.assertFalse(got.complete)
                self.assertEqual(len(t.calls), 1, "stopped at the empty page")

    def test_an_empty_page_after_real_ones_is_not_complete_even_at_count_zero(self):
        # The source's `count` is re-read on every page and can shrink between
        # them -- tags deleted while the read is in flight. Shrunk all the way
        # to zero, the empty second page satisfies "count == 0" while two tags
        # from page one are already in hand, so a flag decided on the count
        # alone calls this a whole catalogue. It is not one: whatever lay
        # between what was read and what the source now holds was never
        # fetched, and "no configured source describes this tag" must not rest
        # on it. The `and not tags` half of the flag is the only thing
        # separating this from the genuinely-empty source above.
        t = _transport([_tag_page(3, [_box_tag("Lantern Work"),
                                      _box_tag("Brass Ferry")]),
                        _tag_page(0, [])])
        box = StashBox("https://box.test", "k", transport=t)

        got = box.all_tags(per_page=2, max_pages=10)

        self.assertEqual(len(got.tags), 2, "kept what it had already read")
        self.assertFalse(got.complete)

    def test_a_read_that_hits_the_page_cap_keeps_what_it_read_and_says_so(self):
        # Both halves matter and they are different facts: the tags already
        # in hand are worth having, and what was not read is unknown rather
        # than absent.
        t = _transport([_tag_page(99, [_box_tag("Lantern Work")])])
        box = StashBox("https://box.test", "k", transport=t)

        got = box.all_tags(per_page=1, max_pages=2)

        self.assertEqual(len(got.tags), 2)
        self.assertFalse(got.complete)
        self.assertEqual(len(t.calls), 2, "stopped at the page cap")

    def test_a_transport_failure_raises_rather_than_reading_as_partial(self):
        # `StashError.transient` says whether a retry is worth it, and
        # folding it into the flag would make a wedged host indistinguishable
        # from an honest partial read.
        t = _transport([StashError("host wedged", transient=True)])
        box = StashBox("https://box.test", "k", transport=t)

        with self.assertRaises(StashError) as caught:
            box.all_tags()
        self.assertTrue(caught.exception.transient)

    def test_the_query_asks_for_the_three_fields_the_index_is_built_from(self):
        # Stated here rather than read off the constant: reading it off the
        # query is the defect this pin exists to close, because a field
        # dropped from the constant would move both sides of the comparison
        # at once. Without `description` there is nothing to propose; without
        # `aliases` most of the real coverage disappears and nothing fails.
        requested = _names_in(BOX_TAGS)

        self.assertIn("queryTags", requested)
        self.assertIn("TagQueryInput", requested)
        self.assertIn("count", requested)
        for field in ("name", "description", "aliases"):
            self.assertIn(field, requested,
                          "the index is built from %s and it is not being "
                          "asked for" % (field,))


class BaseUrl(unittest.TestCase):
    """The seam between two spellings of one address."""

    def test_a_stored_graphql_endpoint_is_reduced_to_its_base(self):
        # The media server stores the GraphQL endpoint itself; `StashBox`
        # appends `/graphql` of its own. Left alone, every configured source
        # is asked for `/graphql/graphql` and silently contributes nothing.
        self.assertEqual(base_url("https://box.test/graphql"),
                         "https://box.test")

    def test_a_trailing_slash_does_not_hide_the_suffix(self):
        self.assertEqual(base_url("https://box.test/graphql/"),
                         "https://box.test")

    def test_an_address_that_is_already_a_base_is_left_alone(self):
        self.assertEqual(base_url("https://box.test"), "https://box.test")

    def test_only_the_trailing_suffix_is_removed(self):
        # A path segment that merely contains the word must survive: trimming
        # it would point the client at a different host's root.
        self.assertEqual(base_url("https://box.test/graphql/v2"),
                         "https://box.test/graphql/v2")

    def test_the_client_built_from_it_asks_the_right_address(self):
        # The two halves joined, so nothing has to be believed about how
        # `StashBox` spells its own url.
        self.assertEqual(
            StashBox(base_url("https://box.test/graphql"), "k").url,
            "https://box.test/graphql")


PERFORMER = "pf-8821"


def _listing(scenes=2, complete=True, performer_id=PERFORMER, total=None,
             pages_read=1):
    scene_list = [{"id": "s%d" % n} for n in range(scenes)]
    if total is None:
        total = len(scene_list)
    return SourceListing(performer_id, scene_list, complete,
                         total=total, pages_read=pages_read)


def _m(value, contained=False, meaningful_count=2):
    return Match(value=value, contained=contained,
                 meaningful_count=meaningful_count)


def _refused():
    """A scoring refusal with nothing that came close: no candidate in the
    listing was a plausible entry for this file."""
    return decide([_m(0.41)])


def _decided():
    return decide([_m(0.9)])


def _ambiguous():
    """A refusal of the OTHER kind: two candidates both cleared the bar."""
    return decide([_m(0.80), _m(0.78)])


def _nothing_to_ask_with(scenes=2):
    """A refusal of a THIRD kind, and the one that looks most like the first.

    The filename carried no word that is not the artist's or generic, so every
    candidate is barred at any score and not one listing title was ever
    weighed. The scores are deliberately high: nothing about the numbers
    distinguishes this from a near miss, and `contenders` is 0 for both."""
    return decide([_m(0.95, meaningful_count=0) for _ in range(scenes)])


def _one_generic_word():
    """A refusal that DID interrogate the listing, on thin evidence. The one
    meaningful token was compared against every title and fell short of a bar
    a higher score would have cleared."""
    return decide([_m(0.7, meaningful_count=1)])


def _every_branch_reason():
    """One reason from each branch of `listing_verdict`, the claiming branch
    first. Every one of these is prose shown to a person, so the properties
    that hold across all of them are worth asserting across all of them."""
    return [
        listing_verdict(_listing(), _refused(), performer_id=PERFORMER).reason,
        listing_verdict(_listing(complete=False), _refused(),
                        performer_id=PERFORMER).reason,
        listing_verdict(_listing(), _decided(), performer_id=PERFORMER).reason,
        listing_verdict(_listing(), _ambiguous(), performer_id=PERFORMER).reason,
        listing_verdict(_listing(), _refused(), performer_id=PERFORMER,
                        attribution_certain=False).reason,
        listing_verdict(_listing(), _nothing_to_ask_with(),
                        performer_id=PERFORMER).reason,
    ]


class ListingVerdict(unittest.TestCase):
    """What a completed read is allowed to claim.

    The scorer must always pick a winner from what it is handed, and against a
    real library it applied a wrong entry 6% of the time when the right one
    was not in the candidate list at all. A listing read in full lets a refusal
    say something the scorer never can -- "every entry this source lists for
    this performer was read and this file matches none of them" -- which tells
    a reviewer the file may be filed under the wrong performer.

    What it does NOT say is that the scene is not this performer's. The source
    is an index contributors fill in by hand, so it holds a subset of what a
    performer released and a file missing from it has most likely just never
    been submitted. Both halves are made to a person, a wrong one has no undo,
    and the half that is easy to lose is the second. So the tests here cover
    the conditions under which the claim may NOT be made, and the words it is
    made in.
    """

    def test_a_complete_read_and_a_refusal_is_an_absence(self):
        # The case the whole ticket exists for. The listing was read whole,
        # nothing in it is this file, and the reason says so in those terms --
        # naming the performer, because "not in it" is worthless to a reviewer
        # who cannot tell whose listing was searched.
        verdict = listing_verdict(_listing(complete=True), _refused(),
                                  performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, True)
        self.assertIn("in full", verdict.reason)
        self.assertIn(PERFORMER, verdict.reason)

    def test_an_empty_listing_read_in_full_is_still_an_absence(self):
        # A performer the source holds nothing for, and therefore no candidate
        # to score. This is the strongest evidence obtainable here and the
        # answer most worth acting on; an implementation that needed scenes in
        # hand before it would commit would refuse exactly here.
        verdict = listing_verdict(_listing(scenes=0, complete=True), decide([]),
                                  performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, True)
        self.assertIn("in full", verdict.reason)

    def test_a_partial_read_can_never_be_an_absence(self):
        # THE test. `unlisted` is three-valued on purpose: True and False are
        # claims, None is the honest answer when the evidence supports
        # neither. Collapsing None into False makes "we could not tell"
        # indistinguishable from "it is there", and the page the read never
        # reached is exactly where the file's entry would be.
        #
        # Both shapes of partial read are here. The empty one matters most: it
        # is byte-for-byte the same view as the genuinely-empty listing
        # above apart from one flag, so anything that reads the scene list
        # instead of the flag passes the case above and gets this one
        # catastrophically wrong.
        for scenes in (0, 2):
            with self.subTest(scenes=scenes):
                verdict = listing_verdict(
                    _listing(scenes=scenes, complete=False), _refused(),
                    performer_id=PERFORMER)

                self.assertIsNone(
                    verdict.unlisted,
                    "a partial read reported as a definite answer")
                self.assertIsNot(verdict.unlisted, False)
                self.assertIn("stopped early", verdict.reason)
                self.assertNotIn("in full", verdict.reason)

    def test_a_partial_read_names_how_partial(self):
        # The ratio is what turns "stopped early" into something a person
        # can act on: 3 read of 9 promised says there is more to ask for and
        # names how much; the number of pages says how it stopped so far.
        # Without it, "stopped early after 3 scenes" is only a reason to
        # distrust the read, never a reason to go back for the rest.
        verdict = listing_verdict(
            _listing(scenes=3, complete=False, total=9, pages_read=4),
            _refused(), performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIn("3", verdict.reason)
        self.assertIn("9", verdict.reason)
        self.assertIn("4", verdict.reason)

    def test_a_decided_match_is_not_an_absence(self):
        verdict = listing_verdict(_listing(complete=True), _decided(),
                                  performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, False)
        self.assertIn("has this file", verdict.reason)
        self.assertNotIn("in full", verdict.reason)

    def test_a_partial_read_that_found_the_file_still_found_it(self):
        # Completeness gates the NEGATIVE claim only. A page that was never
        # fetched cannot un-find an entry already in hand, so "it is there"
        # survives a short read -- and an implementation that checked the flag
        # before looking at the decision would downgrade this to None and
        # report a found file as unknown.
        verdict = listing_verdict(_listing(complete=False), _decided(),
                                  performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, False)

    def test_a_contested_attribution_never_claims_an_absence(self):
        # The resolver reports it when a folder names one creator and the
        # filename names another, and this is that signal's first consumer.
        # A contested attribution means the listing that was enumerated may
        # belong to someone else entirely -- so "not in it" answers a question
        # about the wrong person, confidently, which is worse than not
        # answering. The view here is COMPLETE: completeness is not the thing
        # in doubt, whose listing it is is.
        verdict = listing_verdict(_listing(complete=True), _refused(),
                                  performer_id=PERFORMER,
                                  attribution_certain=False)

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, False)
        self.assertIn("different creators", verdict.reason)
        self.assertNotIn("in full", verdict.reason)

    def test_a_contested_attribution_does_not_claim_a_presence_either(self):
        # The mirror. A match found in a listing that may be the wrong
        # person's is a wrong identification, not a confirmation -- it is the
        # very failure the resolver's disagreement is warning about. Neither
        # direction is claimable when it is unknown whose listing was read.
        verdict = listing_verdict(_listing(complete=True), _decided(),
                                  performer_id=PERFORMER,
                                  attribution_certain=False)

        self.assertIsNone(verdict.unlisted)
        self.assertIn("different creators", verdict.reason)

    def test_a_refusal_between_two_contenders_is_not_an_absence(self):
        # A refusal is not one thing. "Nothing cleared the bar" is consistent
        # with the file having no entry here; "two cleared it and I cannot say
        # which" is the opposite claim -- entries that look like this file are
        # right there in the listing. Both arrive as match=None, and
        # reporting the second as an absence would send a reviewer hunting a
        # mis-filing while the two candidate entries sit in the same reply.
        verdict = listing_verdict(_listing(complete=True), _ambiguous(),
                                  performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("competed", verdict.reason)
        self.assertNotIn("in full", verdict.reason)

    def test_a_single_contender_is_not_an_absence_either(self):
        # `if decision.contenders:` fires at contenders == 1, not only at 2
        # or more. `decide()` itself never returns exactly one contender
        # beside a refusal -- a lone eligible candidate always wins outright
        # -- so nothing on the ordinary decision path exercises this
        # boundary, and `contenders > 1` would pass every test above just as
        # well. A future caller that hand-builds a `Decision`, or a `decide`
        # whose shape changes, must not have this read as "nothing competed"
        # the moment there is exactly one entry that did -- that is the
        # loose side of the boundary, and it is the direction that
        # fabricates an absence.
        one_contender = Decision(
            match=None, index=None,
            reason="a lone candidate that still lost to an ambiguity "
                   "elsewhere",
            contenders=1, interrogated=True)

        verdict = listing_verdict(_listing(complete=True), one_contender,
                                  performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("competed", verdict.reason)

    def test_candidates_in_hand_beat_an_unfinished_read_too(self):
        # The contenders check already runs ahead of the completeness check
        # in the code, matching the docstring's own ordering rule -- but
        # nothing pinned THIS combination: an ambiguous refusal over an
        # INCOMPLETE listing. A mutation that reordered the two checks would
        # report "the read stopped early" here, inviting a retry that could
        # never resolve an ambiguity two candidates already produced.
        verdict = listing_verdict(_listing(scenes=3, complete=False),
                                  _ambiguous(), performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIn("competed", verdict.reason)
        self.assertNotIn("stopped early", verdict.reason)

    def test_the_kind_of_refusal_is_read_from_the_count_not_the_prose(self):
        # scan.py already states the rule: a fact worth acting on is asked of
        # the data, never inferred from a reason string, because the wording
        # is free to change and nothing would notice. Here the wording is
        # deliberately set to contradict the count -- a refusal whose prose
        # reads like a near miss but which had two contenders, and one whose
        # prose reads ambiguous but had none. The count wins both times.
        looks_like_a_near_miss = Decision(
            match=None, index=None,
            reason="nothing above the threshold (0.70); best score was 0.410",
            contenders=2, interrogated=True)
        looks_ambiguous = Decision(
            match=None, index=None,
            reason="ambiguous: 0.800 vs 0.780 are too close to call",
            contenders=0, interrogated=True)

        self.assertIsNone(
            listing_verdict(_listing(), looks_like_a_near_miss,
                            performer_id=PERFORMER).unlisted)
        self.assertIs(
            listing_verdict(_listing(), looks_ambiguous,
                            performer_id=PERFORMER).unlisted, True)

    def test_a_file_with_nothing_to_ask_with_is_not_an_absence(self):
        # The ticket's own harm, arriving through the door built to stop it.
        # `contenders == 0` here does NOT mean "500 entries were checked and
        # none is close" -- it means the filename carried no word that is not
        # the artist's or generic, which bars every candidate at any score, so
        # not one of those 500 titles was ever weighed. The right entry could
        # be sitting in them and the outcome would be identical.
        #
        # Reported as an absence, the reason contradicts itself -- "read in
        # full (500 scenes) and this file is not in it: nothing to match on"
        # -- and the first clause is the one a reviewer acts on. They get sent
        # to hunt a mis-filing the tool never looked for.
        verdict = listing_verdict(_listing(scenes=500, complete=True),
                                  _nothing_to_ask_with(500),
                                  performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("never weighed", verdict.reason)
        self.assertNotIn("in full", verdict.reason)
        # Whose listing, how much of it, and which refusal -- the same three
        # facts the absence branch carries, for the same reason: a reviewer
        # who cannot see what was skipped cannot judge whether it mattered.
        self.assertIn(PERFORMER, verdict.reason)
        self.assertIn("500", verdict.reason)
        self.assertIn("meaningful_count=0", verdict.reason)

    def test_a_caller_that_offered_no_candidates_is_not_an_absence(self):
        # The same defect from the other side, and it is a statement about the
        # CALLER: candidate-building that silently produced nothing looks
        # identical to a listing that holds nothing close. The view here is
        # stocked and complete, which is exactly what makes the wrong answer
        # so confident.
        verdict = listing_verdict(_listing(scenes=500, complete=True),
                                  decide([]), performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("never weighed", verdict.reason)
        self.assertNotIn("in full", verdict.reason)
        self.assertIn("no candidates offered", verdict.reason)

    def test_candidates_in_hand_are_reported_ahead_of_a_thin_interrogation(self):
        # Both refusals block the absence, so only the reason separates them,
        # and the reason is the whole output here. Entries that competed for
        # this file are sitting in the reply and a reviewer can look at them;
        # "nothing was weighed" says there is nothing to look at, which is
        # false while two candidates are in hand. Evidence in hand beats a
        # complaint about the evidence that was not.
        competed_on_thin_ground = Decision(
            match=None, index=None,
            reason="ambiguous: 0.800 vs 0.780 are too close to call",
            contenders=2, interrogated=False)

        verdict = listing_verdict(_listing(complete=True),
                                  competed_on_thin_ground,
                                  performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIn("competed", verdict.reason)
        self.assertNotIn("never weighed", verdict.reason)

    def test_an_unasked_question_cannot_un_find_a_found_file(self):
        # The mirror of the partial-read case, and the regression this fix is
        # most likely to introduce: the interrogation check gates the NEGATIVE
        # claim only. An entry in hand is a presence however thin the rest of
        # the list was, and a guard placed one branch too early would downgrade
        # a found file to "unknown" -- withholding an answer that was already
        # obtained, which is the opposite of what this function is for.
        found_on_thin_ground = Decision(
            match=_m(0.9), index=0, reason="chosen with score 0.900",
            contenders=1, interrogated=False)

        verdict = listing_verdict(_listing(complete=True),
                                  found_on_thin_ground, performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, False)
        self.assertIn("has this file", verdict.reason)

    def test_thin_evidence_is_still_evidence(self):
        # The loose side of the same boundary, and the quieter failure. One
        # meaningful token IS an interrogation: it was compared against every
        # title and fell short of a bar a higher score would have cleared,
        # which is a fact about the listing. A guard that swept this in with
        # the two cases above would stop answering for every short filename
        # and nothing would say why.
        verdict = listing_verdict(_listing(complete=True), _one_generic_word(),
                                  performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, True)
        self.assertIn("in full", verdict.reason)

    def test_a_question_never_asked_is_reported_before_a_read_never_finished(self):
        # Both defects at once, and only the reason distinguishes them -- so
        # the ordering is the whole test. "Stopped early after 3 scenes"
        # invites the caller to raise the page cap and read the rest, and
        # reading the rest cannot change an answer that was never asked. That
        # is a retry which can never come good, and pointing at it is worse
        # than saying nothing.
        verdict = listing_verdict(_listing(scenes=3, complete=False),
                                  _nothing_to_ask_with(3),
                                  performer_id=PERFORMER)

        self.assertIsNone(verdict.unlisted)
        self.assertIn("never weighed", verdict.reason)
        self.assertNotIn("stopped early", verdict.reason)

    def test_only_the_completed_read_claims_a_completed_read(self):
        # A reason that claims completeness it did not achieve is the same
        # wrong assertion as the flag itself, made in the part a person
        # actually reads. Exactly one of the branches may say "in full", and
        # a single catch-all string that satisfied every assertion above
        # cannot also satisfy this.
        reasons = _every_branch_reason()

        self.assertEqual([r for r in reasons if "in full" in r],
                         [reasons[0]])
        self.assertEqual(len(set(reasons)), len(reasons), reasons)

    def test_the_absence_is_worded_as_what_was_read_and_what_that_shows(self):
        # The sentence, whole, because the sentence IS the output here and a
        # sampled assertion is blind to what it leaves out. What it used to
        # say was "performer pf's catalogue was read in full (500 scenes) and
        # this file is not in it" -- an assertion about the performer's body
        # of work, which no read of this source can support. The source is a
        # contributor-submitted index; a scene nobody entered is missing from
        # it exactly as a scene that does not exist is.
        #
        # The limit rides INSIDE the reason rather than in the docs because
        # this string is what gets pasted into a ticket, and it is pasted
        # without them. Read alone it must still teach a reviewer that a gap
        # in the index is not a gap in the world -- otherwise they go hunting
        # a mis-filing for a scene that was simply never submitted, and that
        # search has no undo either.
        verdict = listing_verdict(_listing(scenes=500, complete=True),
                                  _refused(), performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, True)
        self.assertEqual(
            verdict.reason,
            "this source's listing for performer pf-8821 was read in full "
            "(500 entries) and this file is not in it — but the listing holds "
            "only what contributors have submitted, so a file missing from it "
            "may simply never have been submitted: nothing above the "
            "threshold (0.70); best score was 0.410")

    def test_the_absence_carries_its_limit_however_it_is_worded(self):
        # The companion to the assertion above, and the one that survives a
        # rewrite of it: pinning the sentence verbatim is satisfied by pasting
        # whatever the code now says back into the test, which is exactly what
        # a re-introduced overclaim would do. These are the properties that
        # make the sentence honest, asserted independently of its phrasing --
        # it names the source's own listing as what was read, and it says in
        # the same breath that the listing holds only what was submitted to
        # it.
        reason = listing_verdict(_listing(scenes=500, complete=True),
                                 _refused(), performer_id=PERFORMER).reason

        self.assertIn("this source's listing", reason)
        self.assertIn("submitted", reason)
        self.assertIn("only what contributors", reason)

    def test_no_reason_claims_a_performer_s_catalogue_was_read(self):
        # Across every branch, not just the claiming one: "catalogue",
        # "career", "body of work" and "everything" all name the performer's
        # output, and this module never reads that -- it reads one index's
        # record of it. The word is barred rather than the phrasing pinned
        # because the harm does not depend on which sentence carries it, and
        # a future branch will be written by somebody who did not read this
        # file.
        for reason in _every_branch_reason():
            with self.subTest(reason=reason):
                for overclaim in ("catalogue", "body of work", "career",
                                  "every scene", "all of this performer"):
                    self.assertNotIn(overclaim, reason.lower(), reason)

    def test_the_verdict_carries_a_claim_and_a_reason_and_nothing_else(self):
        # Asserted whole rather than probed key by key: the failure worth
        # preventing is a field being ADDED -- a confidence, a score, a
        # candidate -- which would re-create the thing this layer exists to
        # replace, and a sampled assertion is blind to exactly that.
        verdict = listing_verdict(_listing(), _refused(), performer_id=PERFORMER)

        self.assertEqual(sorted(vars(verdict)), ["reason", "unlisted"])

    def test_certainty_cannot_be_passed_positionally(self):
        # The natural mis-wiring is `listing_verdict(listing, decision,
        # resolution.competing)` -- and `competing` holds a NAME when the
        # attribution is contested, which is truthy, so the guard would be
        # switched off by exactly the value that should switch it on.
        with self.assertRaises(TypeError):
            listing_verdict(_listing(), _refused(), False,
                            performer_id=PERFORMER)

    def test_a_certainty_that_is_not_a_boolean_raises(self):
        # Same mis-wiring, spelled as a keyword. Silently treating a truthy
        # name as "certain" is the fail-open direction, and treating it as
        # contested would hide the wiring bug behind a verdict that simply
        # never claims anything.
        for bad in ("Velvet Crane", 1, 0, None, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    listing_verdict(_listing(), _refused(),
                                    performer_id=PERFORMER,
                                    attribution_certain=bad)

    def test_a_decision_scored_against_a_different_performer_is_refused(self):
        # Nothing about a bare `Decision` says which performer's scenes it
        # was scored over. A caller juggling several (listing, decision)
        # pairs at once -- the wrong one pulled off two ends of a batch, say
        # -- would otherwise get a confident answer naming the performer
        # `listing` carries, over candidates actually drawn from somebody
        # else's. `performer_id` is the caller's own claim about who the
        # decision was scored against; a mismatch against `listing`'s own
        # performer refuses outright rather than reporting anything.
        with self.assertRaises(ValueError):
            listing_verdict(_listing(performer_id="pf-other"), _refused(),
                            performer_id=PERFORMER)

    def test_a_decision_scored_against_the_right_performer_is_not_refused(self):
        # The permissive side: the ordinary, matching case must still work,
        # or the mismatch guard has swallowed every call along with the one
        # it exists to catch.
        verdict = listing_verdict(_listing(performer_id=PERFORMER), _refused(),
                                  performer_id=PERFORMER)

        self.assertIs(verdict.unlisted, True)


class Check(unittest.TestCase):
    """`check` assembles the whole pipeline `listing_verdict` needs from a
    live source: read the listing, score THIS source's own titles against
    the file, decide, and hand the result to `listing_verdict` with the
    resolver's own attribution-certainty signal. Every property here is
    about that ASSEMBLY, not about `listing_verdict` itself (see
    `ListingVerdict` above for that, already pinned by mutation): a mistake
    here would feed the right function the wrong inputs and every one of
    its careful branches would still be reachable, just never on the fact
    this file actually needed.
    """

    def test_reads_the_named_performers_listing(self):
        t = _transport([_page(1, [{"id": "s1", "title": "Nothing Close"}])])
        box = StashBox("https://box.test", "k", transport=t)

        check(box, "pf-performer", "Morning Ritual.mp4", "Velvet Crane",
             Resolution(name="Velvet Crane"))

        body, _ = t.calls[0]
        self.assertEqual(body["variables"]["input"]["performers"]["value"],
                         ["pf-performer"])

    def test_a_matching_title_in_the_listing_is_not_an_absence(self):
        t = _transport([_page(1, [{"id": "s1", "title": "Morning Ritual"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane", Resolution(name="Velvet Crane"))

        self.assertIs(verdict.unlisted, False)

    def test_a_complete_listing_with_nothing_close_is_an_absence(self):
        t = _transport([_page(1, [{"id": "s1", "title": "A Totally Different Scene"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane", Resolution(name="Velvet Crane"))

        self.assertIs(verdict.unlisted, True)
        self.assertIn("in full", verdict.reason)

    def test_an_incomplete_listing_can_never_be_an_absence(self):
        # THE property a mutation must kill: claiming completeness the read
        # did not achieve is worse than the shrug it replaces -- there is no
        # undo for a person's wasted afternoon.
        t = _transport([_page(9, [{"id": "s1", "title": "A Totally Different Scene"}])])
        box = StashBox("https://box.test", "k", transport=t)
        # per_page/max_pages chosen so the read stops after one page with
        # more promised than delivered -- see performer_listing's own tests
        # for this shape.
        verdict = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane", Resolution(name="Velvet Crane"),
                        per_page=1, max_pages=1)

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertNotIn("in full", verdict.reason)

    def test_a_contested_attribution_can_never_be_used_as_though_settled(self):
        # THE other property a mutation must kill: enumerating the wrong
        # performer's listing and reporting an absence answers a question
        # about the wrong person, confidently -- worse than not answering.
        t = _transport([_page(1, [{"id": "s1", "title": "A Totally Different Scene"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane",
                        Resolution(name="Velvet Crane", competing="Ivy Thorn"))

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("different creators", verdict.reason)

    def test_an_uncontested_attribution_is_read_as_certain(self):
        # The permissive side of the same guard: a resolution with NO
        # competing name must still be allowed to claim an absence, or the
        # contested check has silently swallowed the common, unambiguous
        # case along with the one it exists to catch.
        t = _transport([_page(1, [{"id": "s1", "title": "A Totally Different Scene"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane",
                        Resolution(name="Velvet Crane", competing=None))

        self.assertIs(verdict.unlisted, True)

    def test_a_rejected_folder_can_never_be_used_as_though_settled(self):
        # `competing` is only set when the folder WON and a differing
        # filename candidate existed. It stays None when the folder's own
        # text never competed at all -- thrown out by a guard, not checked
        # against evidence -- and the filename resolved on its own. A guard
        # is a heuristic tuned against real filing conventions, not a proof
        # that the rejected text names nobody, so this is not the same as
        # the folder and the filename having been compared and agreeing.
        # Reading only `competing` would call this settled.
        t = _transport([_page(1, [{"id": "s1", "title": "A Totally Different Scene"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Velvet Crane - Morning Ritual.mp4",
                        "2023 September 11",
                        Resolution(name="Velvet Crane", source="filename",
                                  competing=None,
                                  rejected_folder="2023 September 11"))

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("different creators", verdict.reason)

    def test_the_resolved_name_is_subtracted_as_artist_evidence(self):
        # A file named after nobody but its own creator must not read as a
        # match on the creator's name alone -- the same zero-evidence rule
        # `cronicled.scan.examine` relies on `scoring.score`'s `artist`
        # argument for. Without it, "Velvet Crane.mp4" would contain-match
        # any title carrying the creator's own name.
        t = _transport([_page(1, [{"id": "s1", "title": "Velvet Crane Diary"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Velvet Crane.mp4",
                        "Velvet Crane", Resolution(name="Velvet Crane"))

        self.assertIsNone(verdict.unlisted)
        self.assertIsNot(verdict.unlisted, True)
        self.assertIn("nothing to match on", verdict.reason)

    def test_censorship_is_applied_to_the_listings_own_titles(self):
        censorship = {"nightfall": ["n1ghtfall"]}
        t = _transport([_page(1, [{"id": "s1", "title": "Kestrel N1ghtfall"}])])
        box = StashBox("https://box.test", "k", transport=t)

        censored = check(box, "pf-performer", "Kestrel Nightfall.mp4",
                         "Kestrel Hollow", Resolution(name="Kestrel Hollow"),
                         censorship=censorship)
        uncensored = check(box, "pf-performer", "Kestrel Nightfall.mp4",
                           "Kestrel Hollow", Resolution(name="Kestrel Hollow"))

        self.assertIs(censored.unlisted, False)
        self.assertIsNot(uncensored.unlisted, False)

    def test_the_threshold_argument_reaches_the_decision(self):
        # A score that clears a low threshold but not a high one -- proof
        # the argument actually reaches `decide` rather than a
        # module-level default being used regardless of what was passed.
        t = _transport([_page(1, [{"id": "s1", "title": "Morning Rituals"}])])
        box = StashBox("https://box.test", "k", transport=t)

        lenient = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane", Resolution(name="Velvet Crane"),
                        threshold=0.1)
        strict = check(box, "pf-performer", "Morning Ritual.mp4",
                       "Velvet Crane", Resolution(name="Velvet Crane"),
                       threshold=0.999)

        self.assertIs(lenient.unlisted, False)
        self.assertIsNot(strict.unlisted, False)

    def test_two_scenes_in_one_listing_agreeing_on_title_is_not_an_absence(self):
        # The same listing carrying the same clip twice (a resubmission, or
        # two contributors entering the same scene) used to reach
        # `scoring.decide` as an unresolved tie and come back here as
        # `contenders > 0` -- correctly not an absence, but also not the
        # decided match this actually is. `decide` now recognises the
        # agreement, the same rule `cronicled.scan._choose_winner` already
        # applies between stores, one level down to two entries of ONE
        # store's own listing.
        t = _transport([_page(1, [{"id": "s1", "title": "Morning Ritual Dawn"},
                                  {"id": "s2", "title": "morning ritual dawn!"}])])
        box = StashBox("https://box.test", "k", transport=t)

        verdict = check(box, "pf-performer", "Morning Ritual.mp4",
                        "Velvet Crane", Resolution(name="Velvet Crane"))

        self.assertIs(verdict.unlisted, False)


def _profile_reply(row):
    return {"data": {"findPerformer": row}}


def _search_reply(count, rows):
    return {"data": {"queryPerformers": {"count": count, "performers": rows}}}


def _box_performer(id="pf-1", name="Wren Alderly", disambiguation=None,
                   aliases=(), gender="FEMALE", ethnicity=None, country=None,
                   eye_color=None, height=None, birth_date=None,
                   career_start=None, career_end=None, tattoos=(),
                   piercings=(), urls=(), images=()):
    """One `findPerformer`/`queryPerformers` row, shaped the way
    `PERFORMER_PROFILE`/`PERFORMER_SEARCH` shape it -- every fixture here is
    invented; the names belong to nobody."""
    return {
        "id": id, "name": name, "disambiguation": disambiguation,
        "aliases": list(aliases), "gender": gender, "ethnicity": ethnicity,
        "country": country, "eyeColor": eye_color, "height": height,
        "birthDate": birth_date, "careerStartYear": career_start,
        "careerEndYear": career_end,
        "tattoos": [dict(location=l, description=d) for l, d in tattoos],
        "piercings": [dict(location=l, description=d) for l, d in piercings],
        "urls": [{"url": u} for u in urls],
        "images": [{"url": u} for u in images],
    }


class PerformerProfile(unittest.TestCase):
    def test_a_known_id_maps_every_field_this_module_can_read(self):
        row = _box_performer(
            id="pf-1", name="Wren Alderly", disambiguation="the elder",
            aliases=["Wren A."], gender="FEMALE", ethnicity="not stated",
            country="not stated", eye_color="not stated", height=170,
            birth_date="1990-01-01", career_start=2015, career_end=2020,
            tattoos=[("forearm", "a wren in flight")],
            piercings=[("ear", "single lobe")],
            urls=["https://example.test/wren"],
            images=["https://example.test/wren.jpg"])
        t = _transport([_profile_reply(row)])
        box = StashBox("https://box.test", "k", transport=t)

        profile = box.performer_profile("pf-1")

        self.assertEqual(profile["id"], "pf-1")
        self.assertEqual(profile["name"], "Wren Alderly")
        self.assertEqual(profile["aliases"], ["Wren A."])
        self.assertEqual(profile["fields"], {
            "disambiguation": "the elder",
            "gender": "FEMALE",
            "ethnicity": "not stated",
            "country": "not stated",
            "eye_color": "not stated",
            "height_cm": 170,
            "birthdate": "1990-01-01",
            "career_length": "2015-2020",
            "tattoos": "forearm: a wren in flight",
            "piercings": "ear: single lobe",
            "alias_list": ["Wren A."],
            "urls": ["https://example.test/wren"],
            "image": "https://example.test/wren.jpg",
        })
        self.assertEqual(t.calls[0][0]["query"], PERFORMER_PROFILE)
        self.assertEqual(t.calls[0][0]["variables"], {"id": "pf-1"})

    def test_details_and_measurements_are_never_offered(self):
        # The two fields this module's own docstring names as deliberately
        # absent -- see `_performer_from_box`. Asserted on the whole shape,
        # not by checking their VALUES are None: a `None` value and an absent
        # key both read as "nothing offered" to `cronicled.enrichment`'s
        # merge, but only the absent key is the claim this test makes.
        row = _box_performer()
        t = _transport([_profile_reply(row)])
        box = StashBox("https://box.test", "k", transport=t)

        profile = box.performer_profile("pf-1")

        self.assertNotIn("details", profile["fields"])
        self.assertNotIn("measurements", profile["fields"])

    def test_an_unknown_id_answers_none(self):
        t = _transport([_profile_reply(None)])
        box = StashBox("https://box.test", "k", transport=t)

        self.assertIsNone(box.performer_profile("pf-missing"))

    def test_an_absent_optional_field_is_none_or_empty_never_missing(self):
        row = _box_performer(aliases=[], urls=[], images=[], tattoos=[],
                             piercings=[], career_start=None)
        t = _transport([_profile_reply(row)])
        box = StashBox("https://box.test", "k", transport=t)

        profile = box.performer_profile("pf-1")

        self.assertEqual(profile["fields"]["alias_list"], [])
        self.assertEqual(profile["fields"]["urls"], [])
        self.assertIsNone(profile["fields"]["image"])
        self.assertIsNone(profile["fields"]["tattoos"])
        self.assertIsNone(profile["fields"]["piercings"])
        self.assertIsNone(profile["fields"]["career_length"])


class PerformerSearch(unittest.TestCase):
    def test_the_request_names_the_search_term(self):
        t = _transport([_search_reply(0, [])])
        box = StashBox("https://box.test", "k", transport=t)

        box.search_performers("Wren Alderly")

        self.assertEqual(t.calls[0][0]["query"], PERFORMER_SEARCH)
        self.assertEqual(t.calls[0][0]["variables"]["input"]["name"],
                         "Wren Alderly")

    def test_every_row_the_source_offers_comes_back_mapped(self):
        rows = [_box_performer(id="pf-1", name="Wren Alderly"),
               _box_performer(id="pf-2", name="Wren Alderly Jr")]
        t = _transport([_search_reply(2, rows)])
        box = StashBox("https://box.test", "k", transport=t)

        got = box.search_performers("Wren")

        self.assertEqual([p["id"] for p in got], ["pf-1", "pf-2"])

    def test_no_matches_is_an_empty_list_not_an_error(self):
        t = _transport([_search_reply(0, [])])
        box = StashBox("https://box.test", "k", transport=t)

        self.assertEqual(box.search_performers("Nobody Like This"), [])


class JoinedModifications(unittest.TestCase):
    def test_location_and_description_are_joined(self):
        self.assertEqual(
            _joined_modifications([{"location": "forearm",
                                   "description": "a wren in flight"}]),
            "forearm: a wren in flight")

    def test_location_alone_is_kept(self):
        self.assertEqual(
            _joined_modifications([{"location": "forearm",
                                   "description": None}]),
            "forearm")

    def test_description_alone_is_kept(self):
        self.assertEqual(
            _joined_modifications([{"location": None,
                                   "description": "a small mark"}]),
            "a small mark")

    def test_an_entry_with_neither_is_dropped(self):
        self.assertIsNone(_joined_modifications(
            [{"location": None, "description": None}]))

    def test_no_entries_is_none(self):
        self.assertIsNone(_joined_modifications([]))
        self.assertIsNone(_joined_modifications(None))

    def test_multiple_entries_are_joined_with_a_separator(self):
        self.assertEqual(
            _joined_modifications([
                {"location": "forearm", "description": "a wren"},
                {"location": "ankle", "description": "a small star"}]),
            "forearm: a wren; ankle: a small star")


class CareerLength(unittest.TestCase):
    def test_a_start_and_end_year(self):
        self.assertEqual(_career_length(2015, 2020), "2015-2020")

    def test_a_start_year_with_no_end(self):
        self.assertEqual(_career_length(2015, None), "2015-")

    def test_no_start_year_is_none_even_with_an_end(self):
        # An end year with no start is not a career length this can state --
        # see `_career_length`'s own docstring.
        self.assertIsNone(_career_length(None, 2020))
        self.assertIsNone(_career_length(None, None))


if __name__ == "__main__":
    unittest.main()
