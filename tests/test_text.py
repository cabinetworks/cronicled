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
