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
