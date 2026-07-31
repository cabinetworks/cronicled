import unittest

from cronicled.censorship import search_variants, decensor
from tests.fixtures.cast import CENSORSHIP


class SearchVariants(unittest.TestCase):
    def test_original_query_comes_first(self):
        out = search_variants("kestrel at dawn", CENSORSHIP)
        self.assertEqual(out[0], "kestrel at dawn")

    def test_expands_to_the_censored_spellings(self):
        out = search_variants("kestrel at dawn", CENSORSHIP)
        self.assertIn("k3strel at dawn", out)

    def test_expands_a_multi_word_phrase(self):
        # the engine works in normalized space, where '@' has become a space
        out = search_variants("the brass lantern", CENSORSHIP)
        self.assertIn("the br ss lantern", out)

    def test_does_not_match_inside_a_longer_word(self):
        # 'kestrel' must not fire inside 'kestrels'
        self.assertEqual(search_variants("kestrels at dawn", CENSORSHIP),
                         ["kestrels at dawn"])

    def test_empty_map_is_a_no_op(self):
        self.assertEqual(search_variants("kestrel at dawn", {}),
                         ["kestrel at dawn"])

    def test_stops_at_six_variants(self):
        # a canonical with many spellings must not explode into an unbounded
        # query list; the original query always stays first
        many = {"kestrel": ["k%dstrel" % i for i in range(8)]}
        out = search_variants("kestrel at dawn", many)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0], "kestrel at dawn")


class AmpersandVariant(unittest.TestCase):
    """Unconditional, never read from a per-store map: a store may index
    either spelling and nothing here has measured which, so a query
    carrying one is offered the other too. This is the ampersand fix the
    ticket asked for, kept in this mechanism rather than in the outbound
    normaliser -- see `cronicled.adapters.base.SiteAdapter.search_query`'s
    docstring for why sending the expanded form outright would be exactly
    as much a guess as sending the raw one."""

    def test_a_symbol_also_tries_the_spelled_out_form(self):
        out = search_variants("rock & roll", {})
        self.assertIn("rock and roll", out)
        self.assertEqual(out[0], "rock & roll")   # the raw query stays first

    def test_the_spelled_out_form_also_tries_the_symbol(self):
        out = search_variants("rock and roll", {})
        self.assertIn("rock & roll", out)
        self.assertEqual(out[0], "rock and roll")

    def test_a_symbol_glued_to_a_neighbour_is_left_alone(self):
        # not a stand-alone token -- see `_replace_phrase`'s own discipline
        self.assertEqual(search_variants("rock&roll", {}), ["rock&roll"])

    def test_and_inside_a_longer_word_is_left_alone(self):
        # 'sand' must not lose its first three letters because it ends in a
        # word that happens to spell 'and'
        self.assertEqual(search_variants("sand dunes", {}), ["sand dunes"])

    def test_neither_spelling_present_is_a_no_op(self):
        self.assertEqual(search_variants("kestrel at dawn", {}),
                         ["kestrel at dawn"])

    def test_the_ampersand_variant_counts_toward_the_six_cap(self):
        # The bound is on the WHOLE list, not on the per-store expansion
        # alone: a canonical with many spellings must yield one fewer slot
        # once the ampersand variant has taken one.
        many = {"kestrel": ["k%dstrel" % i for i in range(8)]}
        out = search_variants("kestrel & dawn", many)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0], "kestrel & dawn")
        self.assertIn("kestrel and dawn", out)


class Decensor(unittest.TestCase):
    def test_rewrites_a_censored_spelling_back_to_canonical(self):
        self.assertEqual(decensor("K3strel At Dawn", CENSORSHIP),
                         "kestrel at dawn")

    def test_leaves_an_ambiguous_variant_alone(self):
        # 'starling' is claimed by both kestrel and peregrine, so rewriting it
        # would be a guess
        self.assertEqual(decensor("Starling At Dawn", CENSORSHIP),
                         "starling at dawn")

    def test_empty_map_just_normalizes(self):
        self.assertEqual(decensor("K3strel At Dawn", {}), "k3strel at dawn")

    def test_rewrites_a_punctuation_substituted_phrase_back_to_canonical(self):
        # a site title spelled with punctuation must match an unpunctuated filename
        self.assertEqual(decensor("The Br@ss Lantern", CENSORSHIP),
                         "the brass lantern")


if __name__ == "__main__":
    unittest.main()
