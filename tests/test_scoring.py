"""Scoring decides what gets written to a library without a human looking, so
the tests here state the wrong match each rule prevents."""
import unittest

from cronicled.scoring import Match, meaningful_tokens, score


class MeaningfulTokens(unittest.TestCase):
    def test_stopwords_and_junk_do_not_count_as_evidence(self):
        # "my" and "1080p" are in every library; they identify nothing
        self.assertEqual(meaningful_tokens("My Addict 1080p.mp4", ""), {"addict"})

    def test_the_artists_own_name_is_not_evidence(self):
        # every file by an artist contains their name -- it cannot distinguish
        # one of their clips from another
        got = meaningful_tokens("Velvet Crane - Morning Ritual.mp4", "",
                                artist="Velvet Crane")
        self.assertEqual(got, {"morning", "ritual"})

    def test_the_folder_contributes_tokens(self):
        got = meaningful_tokens("Part 2.mp4", "Garden Sessions")
        self.assertIn("garden", got)
        self.assertIn("sessions", got)


class Scoring(unittest.TestCase):
    def test_an_exact_match_scores_one(self):
        # the legacy scorer could not reach 1.0 because it sorted one side of
        # the similarity comparison only; an exact match is exact
        m = score("Morning Ritual.mp4", "", "Morning Ritual")
        self.assertEqual(m.value, 1.0)

    def test_an_unrelated_title_scores_low(self):
        m = score("Morning Ritual.mp4", "", "Copper Kettle Repair")
        self.assertLess(m.value, 0.3)

    def test_a_partial_match_lands_between(self):
        m = score("Morning Ritual Part Two.mp4", "", "Morning Ritual")
        self.assertGreater(m.value, 0.3)
        self.assertLess(m.value, 1.0)

    def test_the_best_of_filename_and_folder_wins(self):
        # the real title may be on either; taking the best is the point
        m = score("clip01.mp4", "Morning Ritual", "Morning Ritual")
        self.assertGreater(m.value, 0.8)

    def test_containment_is_reported(self):
        m = score("Morning Ritual.mp4", "", "Morning Ritual At Dawn")
        self.assertTrue(m.contained)

    def test_containment_needs_at_least_two_meaningful_tokens(self):
        # one generic word inside a longer title is not containment evidence
        m = score("Addict.mp4", "", "Addict To The Sound")
        self.assertFalse(m.contained)

    def test_meaningful_count_is_reported(self):
        m = score("Morning Ritual 1080p.mp4", "", "anything")
        self.assertEqual(m.meaningful_count, 2)

    def test_score_is_rounded_to_three_places(self):
        m = score("Morning Ritual.mp4", "", "Morning Something Else")
        self.assertEqual(m.value, round(m.value, 3))
