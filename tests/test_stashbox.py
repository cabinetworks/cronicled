import re
import unittest

from cronicled.scoring import Decision, Match, decide
from cronicled.stash import StashError
from cronicled.stashbox import (
    PERFORMER_SCENES, SCENES_BY_FINGERPRINT, Catalogue, StashBox,
    absence_verdict)


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

    def test_a_short_page_is_not_the_end_of_the_read(self):
        # The COUNT is the authority on completeness, not the page length.
        # Every other test here is also consistent with the ordinary
        # pagination idiom -- `len(page) < per_page` means the last page --
        # and that idiom is wrong for this source: three scenes on a page of
        # five while the count says nine is the "permission filter applied
        # after counting" shape this module's docstring anticipates.
        #
        # Under the page-length rule the read stops here and returns three
        # scenes as complete=True. A truncated catalogue reported as read in
        # full is the one input that turns every downstream absence into a
        # confident lie, so both the scene count and the number of requests
        # are pinned.
        t = _transport([
            _page(9, [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]),
            _page(9, [{"id": "s%d" % n} for n in range(4, 9)]),
            _page(9, [{"id": "s9"}]),
        ])
        box = StashBox("https://box.test", "k", transport=t)

        catalogue = box.performer_catalogue("p1", per_page=5, max_pages=10)

        self.assertEqual(len(catalogue.scenes), 9)
        self.assertTrue(catalogue.complete)
        self.assertEqual(len(t.calls), 3, "kept asking past the short page")

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

        catalogue = box.performer_catalogue("p1", per_page=5, max_pages=10)

        self.assertEqual(len(catalogue.scenes), 3)
        self.assertFalse(catalogue.complete)
        self.assertEqual(len(t.calls), 2, "stopped at the empty page")

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

        # The query constant is asserted against itself in the bodies below,
        # so mutating it moves both sides and nothing notices. A fake
        # transport cannot check it against a real schema either, so the
        # properties the read depends on are pinned as properties.
        #
        # `title` is the one that matters, and the only one here whose loss is
        # SILENT. Dropped, a real server returns every scene titleless, every
        # candidate scores near zero, `contenders` is 0 and the read is
        # complete -- absent=True for every file in the library, over a
        # catalogue that genuinely was read in full. The others fail loudly:
        # no `count` is a KeyError on the first page, and a wrong endpoint
        # name is a GraphQL error.
        self.assertIn("queryScenes(input: $input)", PERFORMER_SCENES)
        self.assertIn("SceneQueryInput!", PERFORMER_SCENES)
        requested = set(re.findall(r"[A-Za-z_]+", PERFORMER_SCENES))
        for field in ("count", "id", "title"):
            self.assertIn(field, requested,
                          "the read depends on %s" % (field,))
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


PERFORMER = "pf-8821"


def _catalogue(scenes=2, complete=True, performer_id=PERFORMER):
    return Catalogue(performer_id,
                     [{"id": "s%d" % n} for n in range(scenes)], complete)


def _m(value, contained=False, meaningful_count=2):
    return Match(value=value, contained=contained,
                 meaningful_count=meaningful_count)


def _refused():
    """A scoring refusal with nothing that came close: no candidate in the
    catalogue was a plausible entry for this file."""
    return decide([_m(0.41)])


def _decided():
    return decide([_m(0.9)])


def _ambiguous():
    """A refusal of the OTHER kind: two candidates both cleared the bar."""
    return decide([_m(0.80), _m(0.78)])


def _nothing_to_ask_with(scenes=2):
    """A refusal of a THIRD kind, and the one that looks most like the first.

    The filename carried no word that is not the artist's or generic, so every
    candidate is barred at any score and not one catalogue title was ever
    weighed. The scores are deliberately high: nothing about the numbers
    distinguishes this from a near miss, and `contenders` is 0 for both."""
    return decide([_m(0.95, meaningful_count=0) for _ in range(scenes)])


def _one_generic_word():
    """A refusal that DID interrogate the catalogue, on thin evidence. The one
    meaningful token was compared against every title and fell short of a bar
    a higher score would have cleared."""
    return decide([_m(0.7, meaningful_count=1)])


class AbsenceVerdict(unittest.TestCase):
    """What an absence is allowed to claim.

    The scorer must always pick a winner from what it is handed, and against a
    real library it applied a wrong entry 6% of the time when the right one
    was not in the catalogue at all. A catalogue read in full lets a refusal
    say something the scorer never can -- "this performer's catalogue was read
    whole and this file is not in it" -- which tells a reviewer the file is
    either filed under the wrong performer or genuinely not at the source.

    That claim is made to a person, and a wrong one has no undo. So every test
    here is about the conditions under which it may NOT be made.
    """

    def test_a_complete_read_and_a_refusal_is_an_absence(self):
        # The case the whole ticket exists for. The catalogue was read whole,
        # nothing in it is this file, and the reason says so in those terms --
        # naming the performer, because "not in it" is worthless to a reviewer
        # who cannot tell whose catalogue was searched.
        verdict = absence_verdict(_catalogue(complete=True), _refused())

        self.assertIs(verdict.absent, True)
        self.assertIn("in full", verdict.reason)
        self.assertIn(PERFORMER, verdict.reason)

    def test_an_empty_catalogue_read_in_full_is_still_an_absence(self):
        # A performer the source holds nothing for, and therefore no candidate
        # to score. This is the strongest evidence of absence obtainable and
        # the answer most worth acting on; an implementation that needed
        # scenes in hand before it would commit would refuse exactly here.
        verdict = absence_verdict(_catalogue(scenes=0, complete=True), decide([]))

        self.assertIs(verdict.absent, True)
        self.assertIn("in full", verdict.reason)

    def test_a_partial_read_can_never_be_an_absence(self):
        # THE test. `absent` is three-valued on purpose: True and False are
        # claims, None is the honest answer when the evidence supports
        # neither. Collapsing None into False makes "we could not tell"
        # indistinguishable from "it is there", and the page the read never
        # reached is exactly where the file's entry would be.
        #
        # Both shapes of partial read are here. The empty one matters most: it
        # is byte-for-byte the same view as the genuinely-empty catalogue
        # above apart from one flag, so anything that reads the scene list
        # instead of the flag passes the case above and gets this one
        # catastrophically wrong.
        for scenes in (0, 2):
            with self.subTest(scenes=scenes):
                verdict = absence_verdict(
                    _catalogue(scenes=scenes, complete=False), _refused())

                self.assertIsNone(
                    verdict.absent,
                    "a partial read reported as a definite answer")
                self.assertIsNot(verdict.absent, False)
                self.assertIn("stopped early", verdict.reason)
                self.assertNotIn("in full", verdict.reason)

    def test_a_decided_match_is_not_an_absence(self):
        verdict = absence_verdict(_catalogue(complete=True), _decided())

        self.assertIs(verdict.absent, False)
        self.assertIn("has this file", verdict.reason)
        self.assertNotIn("in full", verdict.reason)

    def test_a_partial_read_that_found_the_file_still_found_it(self):
        # Completeness gates the NEGATIVE claim only. A page that was never
        # fetched cannot un-find an entry already in hand, so "it is there"
        # survives a short read -- and an implementation that checked the flag
        # before looking at the decision would downgrade this to None and
        # report a found file as unknown.
        verdict = absence_verdict(_catalogue(complete=False), _decided())

        self.assertIs(verdict.absent, False)

    def test_a_contested_attribution_never_claims_an_absence(self):
        # The resolver reports it when a folder names one creator and the
        # filename names another, and this is that signal's first consumer.
        # A contested attribution means the catalogue that was enumerated may
        # belong to someone else entirely -- so "not in it" answers a question
        # about the wrong person, confidently, which is worse than not
        # answering. The view here is COMPLETE: completeness is not the thing
        # in doubt, whose catalogue it is is.
        verdict = absence_verdict(_catalogue(complete=True), _refused(),
                                  attribution_certain=False)

        self.assertIsNone(verdict.absent)
        self.assertIsNot(verdict.absent, False)
        self.assertIn("different creators", verdict.reason)
        self.assertNotIn("in full", verdict.reason)

    def test_a_contested_attribution_does_not_claim_a_presence_either(self):
        # The mirror. A match found in a catalogue that may be the wrong
        # person's is a wrong identification, not a confirmation -- it is the
        # very failure the resolver's disagreement is warning about. Neither
        # direction is claimable when it is unknown whose catalogue was read.
        verdict = absence_verdict(_catalogue(complete=True), _decided(),
                                  attribution_certain=False)

        self.assertIsNone(verdict.absent)
        self.assertIn("different creators", verdict.reason)

    def test_a_refusal_between_two_contenders_is_not_an_absence(self):
        # A refusal is not one thing. "Nothing cleared the bar" is consistent
        # with the file having no entry here; "two cleared it and I cannot say
        # which" is the opposite claim -- entries that look like this file are
        # right there in the catalogue. Both arrive as match=None, and
        # reporting the second as an absence would send a reviewer hunting a
        # mis-filing while the two candidate entries sit in the same reply.
        verdict = absence_verdict(_catalogue(complete=True), _ambiguous())

        self.assertIsNone(verdict.absent)
        self.assertIsNot(verdict.absent, True)
        self.assertIn("competed", verdict.reason)
        self.assertNotIn("in full", verdict.reason)

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
            absence_verdict(_catalogue(), looks_like_a_near_miss).absent)
        self.assertIs(
            absence_verdict(_catalogue(), looks_ambiguous).absent, True)

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
        verdict = absence_verdict(_catalogue(scenes=500, complete=True),
                                  _nothing_to_ask_with(500))

        self.assertIsNone(verdict.absent)
        self.assertIsNot(verdict.absent, True)
        self.assertIn("never weighed", verdict.reason)
        self.assertNotIn("in full", verdict.reason)
        # Whose catalogue, how much of it, and which refusal -- the same three
        # facts the absence branch carries, for the same reason: a reviewer
        # who cannot see what was skipped cannot judge whether it mattered.
        self.assertIn(PERFORMER, verdict.reason)
        self.assertIn("500", verdict.reason)
        self.assertIn("meaningful_count=0", verdict.reason)

    def test_a_caller_that_offered_no_candidates_is_not_an_absence(self):
        # The same defect from the other side, and it is a statement about the
        # CALLER: candidate-building that silently produced nothing looks
        # identical to a catalogue that holds nothing close. The view here is
        # stocked and complete, which is exactly what makes the wrong answer
        # so confident.
        verdict = absence_verdict(_catalogue(scenes=500, complete=True),
                                  decide([]))

        self.assertIsNone(verdict.absent)
        self.assertIsNot(verdict.absent, True)
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

        verdict = absence_verdict(_catalogue(complete=True),
                                  competed_on_thin_ground)

        self.assertIsNone(verdict.absent)
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

        verdict = absence_verdict(_catalogue(complete=True),
                                  found_on_thin_ground)

        self.assertIs(verdict.absent, False)
        self.assertIn("has this file", verdict.reason)

    def test_thin_evidence_is_still_evidence(self):
        # The loose side of the same boundary, and the quieter failure. One
        # meaningful token IS an interrogation: it was compared against every
        # title and fell short of a bar a higher score would have cleared,
        # which is a fact about the catalogue. A guard that swept this in with
        # the two cases above would stop answering for every short filename
        # and nothing would say why.
        verdict = absence_verdict(_catalogue(complete=True), _one_generic_word())

        self.assertIs(verdict.absent, True)
        self.assertIn("in full", verdict.reason)

    def test_a_question_never_asked_is_reported_before_a_read_never_finished(self):
        # Both defects at once, and only the reason distinguishes them -- so
        # the ordering is the whole test. "Stopped early after 3 scenes"
        # invites the caller to raise the page cap and read the rest, and
        # reading the rest cannot change an answer that was never asked. That
        # is a retry which can never come good, and pointing at it is worse
        # than saying nothing.
        verdict = absence_verdict(_catalogue(scenes=3, complete=False),
                                  _nothing_to_ask_with(3))

        self.assertIsNone(verdict.absent)
        self.assertIn("never weighed", verdict.reason)
        self.assertNotIn("stopped early", verdict.reason)

    def test_only_the_completed_read_claims_a_completed_read(self):
        # A reason that claims completeness it did not achieve is the same
        # wrong assertion as the flag itself, made in the part a person
        # actually reads. Exactly one of the branches may say "in full", and
        # a single catch-all string that satisfied every assertion above
        # cannot also satisfy this.
        reasons = [
            absence_verdict(_catalogue(), _refused()).reason,
            absence_verdict(_catalogue(complete=False), _refused()).reason,
            absence_verdict(_catalogue(), _decided()).reason,
            absence_verdict(_catalogue(), _ambiguous()).reason,
            absence_verdict(_catalogue(), _refused(),
                            attribution_certain=False).reason,
            absence_verdict(_catalogue(), _nothing_to_ask_with()).reason,
        ]

        self.assertEqual([r for r in reasons if "in full" in r],
                         [reasons[0]])
        self.assertEqual(len(set(reasons)), len(reasons), reasons)

    def test_the_verdict_carries_a_claim_and_a_reason_and_nothing_else(self):
        # Asserted whole rather than probed key by key: the failure worth
        # preventing is a field being ADDED -- a confidence, a score, a
        # candidate -- which would re-create the thing this layer exists to
        # replace, and a sampled assertion is blind to exactly that.
        verdict = absence_verdict(_catalogue(), _refused())

        self.assertEqual(sorted(vars(verdict)), ["absent", "reason"])

    def test_certainty_cannot_be_passed_positionally(self):
        # The natural mis-wiring is `absence_verdict(view, decision,
        # resolution.competing)` -- and `competing` holds a NAME when the
        # attribution is contested, which is truthy, so the guard would be
        # switched off by exactly the value that should switch it on.
        with self.assertRaises(TypeError):
            absence_verdict(_catalogue(), _refused(), False)

    def test_a_certainty_that_is_not_a_boolean_raises(self):
        # Same mis-wiring, spelled as a keyword. Silently treating a truthy
        # name as "certain" is the fail-open direction, and treating it as
        # contested would hide the wiring bug behind a verdict that simply
        # never claims anything.
        for bad in ("Velvet Crane", 1, 0, None, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    absence_verdict(_catalogue(), _refused(),
                                    attribution_certain=bad)


if __name__ == "__main__":
    unittest.main()
