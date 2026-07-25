"""The cast is load-bearing: a fixture that loses its property silently guts the
tests that depend on it. These assertions are the guard."""
import unittest

from tests.fixtures.cast import PERFORMERS, TITLES, CENSORSHIP


class CastProperties(unittest.TestCase):
    def test_contained_title_is_inside_the_containing_one(self):
        self.assertIn(TITLES["contained"], TITLES["containing"])

    def test_prefix_clash_shares_a_first_token_with_two_word(self):
        self.assertEqual(PERFORMERS["prefix_clash"].split()[0],
                         PERFORMERS["two_word"].split()[0])
        self.assertNotEqual(PERFORMERS["prefix_clash"], PERFORMERS["two_word"])

    def test_unrelated_title_shares_no_token_with_the_others(self):
        def toks(text):
            return {"".join(c for c in w if c.isalnum())
                    for w in text.lower().split()} - {""}
        others = set()
        for key in ("contained", "containing", "generic_token"):
            others |= toks(TITLES[key])
        self.assertEqual(toks(TITLES["unrelated"]) & others, set())

    def test_one_censored_variant_is_claimed_by_two_canonicals(self):
        claims = {}
        for canon, forms in CENSORSHIP.items():
            for f in forms:
                claims.setdefault(f, set()).add(canon)
        self.assertTrue(any(len(c) > 1 for c in claims.values()))

    def test_at_least_one_canonical_is_a_multi_word_phrase(self):
        # multi-word keys exercise phrase replacement, which single-token keys
        # cannot; losing this entry would silently narrow what the fixtures test
        self.assertTrue(any(" " in canon for canon in CENSORSHIP),
                        "CENSORSHIP must keep a multi-word canonical")
