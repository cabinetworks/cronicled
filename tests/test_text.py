import unittest

from cronicled.text import (normalize, spaceless, strip_html, strip_ext,
                            tokens, slug_match)
from tests.fixtures.cast import PERFORMERS, TITLES


class Normalize(unittest.TestCase):
    def test_lowercases_and_collapses_punctuation(self):
        self.assertEqual(normalize("Velvet.Crane - Copper_Kettle"),
                         "velvet crane copper kettle")

    def test_collapses_runs_of_whitespace(self):
        self.assertEqual(normalize("Velvet    Crane"), "velvet crane")

    def test_empty_and_none_are_empty(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")


class Spaceless(unittest.TestCase):
    def test_removes_every_space(self):
        self.assertEqual(spaceless(PERFORMERS["two_word"]), "velvetcrane")

    def test_matches_across_separators(self):
        self.assertEqual(spaceless("Velvet.Crane"), spaceless("velvet crane"))


class SlugMatch(unittest.TestCase):
    def test_matches_ignoring_case_and_separators(self):
        self.assertTrue(slug_match("Velvet Crane", "velvet-crane"))

    def test_rejects_a_different_performer_sharing_a_first_token(self):
        self.assertFalse(slug_match(PERFORMERS["two_word"],
                                    PERFORMERS["prefix_clash"]))


class StripHtml(unittest.TestCase):
    def test_removes_tags_and_decodes_entities(self):
        self.assertEqual(strip_html("<p>Copper &amp; Kettle</p>"),
                         "Copper & Kettle")

    def test_leaves_plain_text_alone(self):
        self.assertEqual(strip_html(TITLES["contained"]), TITLES["contained"])

    def test_unescapes_backslash_escaped_quotes(self):
        # scraped JSON-ish text arrives with escaped quotes: I\'m -> I'm
        self.assertEqual(strip_html("I\\'m here"), "I'm here")

    def test_collapses_runs_of_whitespace(self):
        self.assertEqual(strip_html("<p>Copper</p>   <p>Kettle</p>"),
                         "Copper Kettle")


class StripExt(unittest.TestCase):
    def test_drops_a_video_extension(self):
        self.assertEqual(strip_ext("Copper Kettle.mp4"), "Copper Kettle")

    def test_keeps_a_non_video_suffix(self):
        self.assertEqual(strip_ext("Copper Kettle Vol.2"), "Copper Kettle Vol.2")


class SlugMatchPolicy(unittest.TestCase):
    """Bidirectional containment, NOT equality: a store handle is often the
    performer name plus a suffix, or vice versa. The plan originally specified
    equality; the shipped behaviour is containment and it is what works."""

    def test_matches_when_one_contains_the_other(self):
        self.assertTrue(slug_match("Velvet Crane", "velvet-crane-official"))
        self.assertTrue(slug_match("velvetcraneofficial", "Velvet Crane"))

    def test_matches_ignoring_case_and_separators(self):
        self.assertTrue(slug_match("Velvet Crane", "velvet-crane"))

    def test_rejects_unrelated_names(self):
        self.assertFalse(slug_match("Velvet Crane", "Harbour Lights"))

    def test_known_limitation_short_names_over_match(self):
        # documented, not desired: a short handle is a substring of many words.
        # The matcher engine must not rely on slug_match alone for short names.
        self.assertTrue(slug_match("ana", "banana"))


class NormalizeUnicode(unittest.TestCase):
    def test_folds_accents_to_their_base_letter(self):
        # filenames routinely drop accents that store titles keep
        self.assertEqual(normalize("café naïve"), "cafe rocio")

    def test_preserves_non_latin_letters(self):
        self.assertEqual(normalize("Мария"), "мария")

    def test_still_collapses_punctuation(self):
        self.assertEqual(normalize("Velvet.Crane - Copper_Kettle"),
                         "velvet crane copper kettle")

    def test_accented_and_unaccented_forms_match(self):
        self.assertTrue(slug_match("café", "cafe"))


class Tokens(unittest.TestCase):
    def test_drops_stopwords_by_default(self):
        self.assertNotIn("the", tokens("The Copper Kettle"))

    def test_keeps_stopwords_when_asked(self):
        self.assertIn("the", tokens("The Copper Kettle", drop_stop=False))

    def test_drops_junk_format_tokens_by_default(self):
        # resolution/container noise in a filename is not part of the title
        self.assertEqual(tokens("Copper Kettle 1080p mp4"), ["copper", "kettle"])

    def test_keeps_junk_tokens_when_asked(self):
        got = tokens("Copper Kettle 1080p", drop_junk=False)
        self.assertIn("1080p", got)


if __name__ == "__main__":
    unittest.main()
