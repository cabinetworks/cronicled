"""`catalog_search` builds the production `search` callable
`cronicled.scan.examine` / `ScanProducer` are handed. No test here opens a
socket: the media client is a fake that answers `scrape_scenes_by_query`
from a script and refuses every other call, the same discipline
`tests/test_scan.py`'s `FakeStash` holds `cronicled.scan.ScanProducer` to.

`CENSORSHIP` is the shared invented substitution map from
`tests.fixtures.cast`, reused here rather than a second copy so a change to
its shape (an added ambiguous variant, say) is exercised by every test that
depends on it, not just `tests/test_censorship.py`'s.
"""
import unittest

from cronicled.search import catalog_search
from tests.fixtures.cast import CENSORSHIP

# Deliberately a name containing the fixture's censored canonical ("kestrel"),
# so `search_variants` has something to expand. Invented; no real performer.
CREATOR = "Kestrel Hollow"


class _Adapter:
    """The two attributes `catalog_search` reads off a `SiteAdapter` — a
    plain fake rather than `DeclarativeAdapter`, so this file exercises the
    search wiring without also depending on the adapter's own validation
    rules."""

    def __init__(self, scraper_id, censorship=None):
        self.scraper_id = scraper_id
        self.censorship = censorship or {}


class _SpyStash:
    """Answers `scrape_scenes_by_query` from a script keyed by
    `(scraper_id, query)` — `[]` for anything not scripted, matching the
    real client's "no match is an empty list" contract — and refuses every
    other call. `catalog_search`'s callable reads and looks things up; it
    must never write to the media server, and this fake makes that a
    property every test in this file holds rather than one test alone.
    """

    def __init__(self, script=None):
        self._script = dict(script or {})
        self.calls = []

    def scrape_scenes_by_query(self, scraper_id, query):
        self.calls.append((scraper_id, query))
        return list(self._script.get((scraper_id, query), []))

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the search callable called %r on the media server; it "
                "reads and looks things up, it never writes" % (name,))
        return refuse


def row(title, url):
    """One ScrapedScene-shaped candidate, as `Stash.scrape_scenes_by_query`
    would answer it — trimmed to what this file's tests read, plus enough
    of the rest to prove dedup compares the WHOLE row, not just `title`."""
    return {"title": title, "url": url, "urls": [url], "code": None,
            "details": None, "director": None, "date": None, "image": None,
            "studio": None, "tags": [], "performers": []}


class CatalogSearchBindings(unittest.TestCase):
    def test_it_asks_the_adapters_own_scraper_id(self):
        adapter = _Adapter("scraper-alpha")
        stash = _SpyStash({("scraper-alpha", CREATOR): [row("A", "u1")]})
        search = catalog_search(stash, adapter)

        self.assertEqual(search(CREATOR), [row("A", "u1")])
        self.assertEqual(stash.calls, [("scraper-alpha", CREATOR)])

    def test_a_different_adapter_would_have_asked_a_different_scraper(self):
        # HARM: swapping in the wrong adapter's scraper_id asks a store this
        # name was never configured against — silently either muting a real
        # creator (the right store never answers) or scoring against an
        # unrelated store's catalogue (the wrong store answers something).
        one = _Adapter("scraper-alpha")
        other = _Adapter("scraper-beta")
        stash = _SpyStash({("scraper-alpha", CREATOR): [row("A", "u1")],
                           ("scraper-beta", CREATOR): [row("B", "u2")]})

        self.assertEqual(catalog_search(stash, one)(CREATOR), [row("A", "u1")])
        self.assertEqual(catalog_search(stash, other)(CREATOR), [row("B", "u2")])

    def test_it_never_writes_to_the_media_server(self):
        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        stash = _SpyStash({("scraper-alpha", CREATOR): [row("A", "u1")]})
        # Would raise via `_SpyStash.__getattr__` the instant anything but
        # `scrape_scenes_by_query` was called.
        catalog_search(stash, adapter)(CREATOR)


class CatalogSearchQueryExpansion(unittest.TestCase):
    def test_a_name_with_no_censored_word_is_asked_once(self):
        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        stash = _SpyStash()

        catalog_search(stash, adapter)("Harbour Lights")

        self.assertEqual(stash.calls, [("scraper-alpha", "Harbour Lights")])

    def test_a_censored_canonical_in_the_name_is_expanded_to_every_variant(self):
        # HARM: dropping `search_variants` from the query path leaves a real
        # store's censorship map inert — a search built from the uncensored
        # name never reaches a store that indexes only the censored
        # spelling, and the file mutes as "no candidates" for a creator the
        # store actually lists.
        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        stash = _SpyStash()

        catalog_search(stash, adapter)(CREATOR)

        queries = [q for _scraper_id, q in stash.calls]
        self.assertIn(CREATOR, queries)              # the original, case kept
        self.assertIn("k3strel hollow", queries)      # one censored spelling
        self.assertIn("starling hollow", queries)     # the other

    def test_no_censorship_map_at_all_is_asked_once_too(self):
        adapter = _Adapter("scraper-alpha")  # censorship defaults to {}
        stash = _SpyStash()

        catalog_search(stash, adapter)(CREATOR)

        self.assertEqual(stash.calls, [("scraper-alpha", CREATOR)])


class CatalogSearchDeduplication(unittest.TestCase):
    def test_the_same_row_answering_two_variants_is_not_duplicated(self):
        # HARM: an undeduplicated pair is the SAME scene appearing twice in
        # the candidate list `scoring.decide` is handed. Two identical
        # entries can turn a genuine winner into "ambiguous: X vs X" — a
        # refusal manufactured by how the query happened to be phrased, not
        # by anything uncertain about the file.
        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        same_scene = row("Nightfall Errand", "https://example.invalid/clip/x")
        stash = _SpyStash({
            ("scraper-alpha", CREATOR): [same_scene],
            ("scraper-alpha", "k3strel hollow"): [dict(same_scene)],
            ("scraper-alpha", "starling hollow"): [dict(same_scene)],
        })

        results = catalog_search(stash, adapter)(CREATOR)

        self.assertEqual(results, [same_scene])

    def test_a_row_differing_only_in_image_is_still_a_duplicate(self):
        # HARM: two candidates with the same title, urls and code but a
        # byte-different cover image score identically, and the scorer's
        # ambiguity rule refuses when the top two sit within a small margin.
        # A duplicated winner manufactures a tie out of a file that had
        # exactly one good answer -- a refusal produced by which cover
        # encoding a re-scrape happened to return, not by real doubt.
        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        first = row("Nightfall Errand", "https://example.invalid/clip/x")
        first["image"] = "data:image/jpeg;base64,AAAA"
        second = dict(first)
        second["image"] = "data:image/jpeg;base64,ZZZZ"
        stash = _SpyStash({
            ("scraper-alpha", CREATOR): [first],
            ("scraper-alpha", "k3strel hollow"): [second],
        })

        results = catalog_search(stash, adapter)(CREATOR)

        self.assertEqual(results, [first])

    def test_genuinely_different_rows_are_both_kept(self):
        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        a = row("Nightfall Errand", "https://example.invalid/clip/a")
        b = row("Morning Ledger", "https://example.invalid/clip/b")
        stash = _SpyStash({
            ("scraper-alpha", CREATOR): [a],
            ("scraper-alpha", "k3strel hollow"): [b],
        })

        results = catalog_search(stash, adapter)(CREATOR)

        self.assertEqual(results, [a, b])

    def test_a_second_call_does_not_share_the_first_calls_list(self):
        adapter = _Adapter("scraper-alpha")
        stash = _SpyStash({("scraper-alpha", CREATOR): [row("A", "u1")]})
        search = catalog_search(stash, adapter)

        first = search(CREATOR)
        second = search(CREATOR)
        first.append("mutated by the first caller")

        self.assertNotIn("mutated by the first caller", second)


class CatalogSearchFailure(unittest.TestCase):
    def test_a_failing_variant_propagates_rather_than_being_swallowed(self):
        # HARM: `scan.examine` treats a raising `search` as a transient
        # network failure to report, never as "the catalogue has nothing" —
        # that is the one distinction the whole module exists to preserve
        # (see `Outcome`'s docstring). Swallowing a failure here so a later
        # variant could still answer would let a real outage masquerade as
        # an ordinary miss and MUTE the file for good.
        class Unreachable(Exception):
            pass

        class _RaisingStash:
            def __init__(self):
                self.calls = []

            def scrape_scenes_by_query(self, scraper_id, query):
                self.calls.append((scraper_id, query))
                raise Unreachable("scraper unreachable")

        adapter = _Adapter("scraper-alpha", CENSORSHIP)
        stash = _RaisingStash()

        with self.assertRaises(Unreachable):
            catalog_search(stash, adapter)(CREATOR)

        # stopped at the first variant instead of trying the rest
        self.assertEqual(stash.calls, [("scraper-alpha", CREATOR)])


if __name__ == "__main__":
    unittest.main()
