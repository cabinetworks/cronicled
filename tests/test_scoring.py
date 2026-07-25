"""Scoring decides what gets written to a library without a human looking, so
the tests here state the wrong match each rule prevents."""
import unittest

from cronicled.scoring import Match, decide, meaningful_tokens, score


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


class Deciding(unittest.TestCase):
    def _m(self, value, contained=False, meaningful_count=2):
        return Match(value=value, contained=contained,
                     meaningful_count=meaningful_count)

    def test_a_clear_winner_is_chosen(self):
        d = decide([self._m(0.8), self._m(0.2)])
        self.assertEqual(d.index, 0)

    def test_nothing_above_the_threshold_is_refused(self):
        d = decide([self._m(0.4), self._m(0.2)])
        self.assertIsNone(d.match)
        self.assertIn("threshold", d.reason)

    def test_one_generic_word_needs_a_very_high_score(self):
        # "Addict" matching some other title's "addict" is not evidence they
        # are the same clip -- this is the rule that stops that
        d = decide([self._m(0.7, meaningful_count=1)])
        self.assertIsNone(d.match)
        d = decide([self._m(0.95, meaningful_count=1)])
        self.assertIsNotNone(d.match)

    def test_containment_wins_regardless_of_the_threshold(self):
        d = decide([self._m(0.55, contained=True)], threshold=0.9)
        self.assertIsNotNone(d.match)

    def test_two_close_candidates_are_refused_not_guessed(self):
        # the legacy code took whichever the scraper returned first, silently
        d = decide([self._m(0.80), self._m(0.78)])
        self.assertIsNone(d.match)
        self.assertIn("ambiguous", d.reason)

    def test_two_contained_candidates_are_ambiguous(self):
        # both floor at 0.9, so this tie is a normal outcome, not a rarity
        d = decide([self._m(0.9, contained=True), self._m(0.9, contained=True)])
        self.assertIsNone(d.match)
        self.assertIn("ambiguous", d.reason)

    def test_a_clear_margin_over_the_runner_up_is_decided(self):
        d = decide([self._m(0.9), self._m(0.5)])
        self.assertEqual(d.index, 0)

    def test_only_eligible_candidates_count_towards_ambiguity(self):
        # a near-tie below the threshold is not a dilemma, it is two rejects
        d = decide([self._m(0.8), self._m(0.3), self._m(0.29)])
        self.assertEqual(d.index, 0)

    def test_an_empty_candidate_list_is_refused_with_a_reason(self):
        d = decide([])
        self.assertIsNone(d.match)
        self.assertTrue(d.reason)

    def test_every_outcome_carries_a_reason(self):
        for matches in ([], [self._m(0.1)], [self._m(0.9)],
                        [self._m(0.8), self._m(0.79)]):
            self.assertTrue(decide(matches).reason,
                            "no reason given for %r" % (matches,))

    def test_a_missing_meaningful_count_raises(self):
        # the legacy default was 2 -- the exact value that skips the
        # generic-word rule, so a malformed candidate failed OPEN
        with self.assertRaises((TypeError, ValueError)):
            decide([Match(value=0.9, contained=False)])
