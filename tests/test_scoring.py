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

    def test_an_unknown_container_extension_is_not_evidence(self):
        # only the containers we scan get stripped by name; renaming a file to
        # any other container must not smuggle a second "meaningful" token in
        for name in ("Addict.mpeg", "Addict.divx", "Addict.rm", "Addict.m2ts",
                     "Addict.ogm", "Addict.MPEG", "Addict.3gp"):
            self.assertEqual(meaningful_tokens(name, ""), {"addict"}, name)

    def test_a_trailing_numeric_fragment_is_not_an_extension(self):
        # "Volume 2.10" is a part number, not a container
        self.assertEqual(meaningful_tokens("Volume 2.10", ""),
                         {"volume", "2", "10"})

    def test_a_trailing_word_is_not_an_extension(self):
        # only extension-SHAPED suffixes go; a real word stays evidence
        self.assertIn("extended",
                      meaningful_tokens("Morning Ritual.Extended", ""))

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

    def test_partial_token_overlap_is_not_containment(self):
        # containment means the filename's evidence is a SUBSET of the title,
        # not that the two merely overlap. Relaxed to "any token in common",
        # a wrong title floors to 0.9 and bypasses the threshold entirely.
        m = score("Morning Ritual.mp4", "", "Morning Coffee")
        self.assertEqual(m.meaningful_count, 2)
        self.assertFalse(m.contained)
        self.assertLess(m.value, 0.9)

    def test_most_but_not_all_tokens_shared_is_not_containment(self):
        # two of three tokens shared is still the wrong clip
        m = score("Morning Ritual Dawn.mp4", "", "Morning Ritual Dusk")
        self.assertEqual(m.meaningful_count, 3)
        self.assertFalse(m.contained)
        self.assertLess(m.value, 0.9)

    def test_a_contained_match_is_floored_at_nine_tenths(self):
        # containment is evidence in its own right, independent of what the
        # arithmetic produced -- and the floor is what manufactures the ties
        # the ambiguity rule then refuses
        m = score("Morning Ritual.mp4", "",
                  "Morning Ritual In Winter Light Extended")
        self.assertTrue(m.contained)
        self.assertGreaterEqual(m.value, 0.9)

    def test_an_extension_shaped_tail_cannot_manufacture_containment(self):
        # dropping a trailing extension-shaped suffix shrinks the evidence
        # set, and a smaller set is MORE likely to be a subset of a title --
        # which is the path that bypasses the threshold entirely. So the
        # strip that stops a rename posing as evidence must never be able to
        # hand a wrong title containment instead. "Dawn" is exactly what
        # separates this file from "Dusk", and it is the token being dropped.
        for name in ("Morning Ritual.Dawn", "Morning.Ritual.Dawn"):
            m = score(name, "", "Morning Ritual Dusk")
            self.assertFalse(m.contained, name)
            self.assertLess(m.value, 0.9, name)
            self.assertIsNone(decide([m], threshold=0.95).match, name)

    def test_a_known_container_is_gone_before_containment_is_judged(self):
        # the counterpart: a container we recognise is confidently not
        # content, so stripping it must still leave containment intact
        m = score("Morning Ritual.mp4", "", "Morning Ritual At Dawn")
        self.assertTrue(m.contained)
        self.assertEqual(m.meaningful_count, 2)

    def test_renaming_the_container_cannot_defeat_the_generic_word_rule(self):
        # "Addict" alone is one generic word wherever it appears; the rule
        # that refuses it must not switch off because the file was renamed
        for name in ("Addict.mp4", "Addict.mpeg", "Addict.divx",
                     "Addict.rm", "Addict.m2ts"):
            m = score(name, "", "Addict To The Sound")
            self.assertEqual(m.meaningful_count, 1, name)
            self.assertIsNone(decide([m]).match, name)

    def test_a_file_named_only_after_the_artist_is_never_applied(self):
        # the score is computed over the raw name, which still carries the
        # artist's name, while meaningful_tokens correctly subtracts it -- so
        # the artist's name inflates the score while contributing no evidence.
        # A bare dump named after the artist would otherwise take on the
        # metadata of whichever of that artist's titles happened to be offered.
        for name, title in (("Velvet Crane 1080p.mp4", "Velvet Crane"),
                            ("Velvet Crane.mp4", "Velvet Crane Session Two")):
            m = score(name, "", title, artist="Velvet Crane")
            self.assertEqual(m.meaningful_count, 0, name)
            self.assertGreater(m.value, 0.85, name)   # high enough to apply
            self.assertIsNone(decide([m]).match, name)

    def test_meaningful_count_is_reported(self):
        m = score("Morning Ritual 1080p.mp4", "", "anything")
        self.assertEqual(m.meaningful_count, 2)

    def test_a_candidate_with_no_title_scores_zero(self):
        # difflib rates two empty strings a perfect match, so a blank
        # candidate scored 0.3 against every file in the library -- eligible
        # under any threshold of 0.3 or below
        for title in ("", "   ", "!!!"):
            self.assertEqual(score("Morning Ritual.mp4", "", title).value, 0.0,
                             repr(title))

    def test_a_file_with_no_name_or_folder_scores_zero(self):
        self.assertEqual(score("", "", "Morning Ritual").value, 0.0)

    def test_two_blank_candidates_are_not_a_dilemma(self):
        # "ambiguous: 0.300 vs 0.300" told the user to choose between two
        # candidates that say nothing
        blank = score("Morning Ritual.mp4", "", "")
        self.assertIsNone(decide([blank, blank], threshold=0.3).match)
        self.assertNotIn("ambiguous", decide([blank, blank], threshold=0.3).reason)

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

    def test_no_meaningful_tokens_is_never_eligible(self):
        # zero evidence is not weak evidence -- no score and no containment
        # claim can make it applicable
        for m in (self._m(0.99, meaningful_count=0),
                  self._m(1.0, meaningful_count=0),
                  self._m(1.0, contained=True, meaningful_count=0)):
            self.assertIsNone(decide([m], threshold=0.1).match, m)

    def test_zero_evidence_is_a_distinct_refusal_from_a_generic_word(self):
        # "a single generic word" is false when there is no word at all, and
        # it points the user at a file rename that cannot help
        zero = decide([self._m(0.99, meaningful_count=0)]).reason
        generic = decide([self._m(0.7, meaningful_count=1)]).reason
        self.assertNotEqual(zero, generic)
        self.assertNotIn("generic word", zero)
        self.assertIn("meaningful_count=0", zero)

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

    def test_the_containment_floor_is_what_makes_two_titles_ambiguous(self):
        # raw, these two score 0.933 and 0.858 -- far enough apart that the
        # wrong one would be applied. The floor lifts both to at least 0.9,
        # which is what lets the ambiguity rule see the dilemma.
        a = score("Morning Ritual.mp4", "", "Morning Ritual At Dawn")
        b = score("Morning Ritual.mp4", "",
                  "Morning Ritual In Winter Light Extended")
        self.assertTrue(a.contained)
        self.assertTrue(b.contained)
        d = decide([a, b])
        self.assertIsNone(d.match)
        self.assertIn("ambiguous", d.reason)

    def test_a_gap_of_exactly_the_margin_is_refused(self):
        # nominally identical gaps land on opposite sides of a raw float
        # comparison -- 0.900-0.850 is 0.050000000000000044 while
        # 0.850-0.800 is 0.049999999999999930 -- so the same dilemma is
        # refused or auto-written depending on where the pair happens to sit
        for top, runner in ((0.900, 0.850), (0.850, 0.800),
                            (0.800, 0.750), (0.700, 0.650)):
            d = decide([self._m(top), self._m(runner)])
            self.assertIsNone(d.match, "%.3f vs %.3f" % (top, runner))
            self.assertIn("ambiguous", d.reason)

    def test_a_gap_just_over_the_margin_is_decided(self):
        for top, runner in ((0.900, 0.849), (0.850, 0.799), (0.700, 0.649)):
            d = decide([self._m(top), self._m(runner)])
            self.assertEqual(d.index, 0, "%.3f vs %.3f" % (top, runner))

    def test_a_clear_margin_over_the_runner_up_is_decided(self):
        d = decide([self._m(0.9), self._m(0.5)])
        self.assertEqual(d.index, 0)

    def test_only_eligible_candidates_count_towards_ambiguity(self):
        # a reject WITHIN the margin is still not a dilemma -- it was never a
        # candidate for writing. The 0.78 here is refused for resting on one
        # generic word, so the 0.80 wins outright despite the near-tie.
        d = decide([self._m(0.80), self._m(0.78, meaningful_count=1)])
        self.assertEqual(d.index, 0)

    def test_the_winner_is_not_assumed_to_be_first(self):
        # index is the field telling the caller WHICH candidate to write; an
        # off-by-one writes the wrong metadata silently
        low, mid, win = self._m(0.2), self._m(0.4), self._m(0.9)
        d = decide([low, mid, win])
        self.assertEqual(d.index, 2)
        self.assertIs(d.match, win)

    def test_the_winner_is_not_assumed_to_be_first_among_eligible(self):
        runner, win = self._m(0.6), self._m(0.95)
        d = decide([runner, win])
        self.assertEqual(d.index, 1)
        self.assertIs(d.match, win)

    def test_an_empty_candidate_list_is_refused_with_a_reason(self):
        d = decide([])
        self.assertIsNone(d.match)
        self.assertTrue(d.reason)

    def test_every_outcome_carries_a_reason(self):
        for matches in ([], [self._m(0.1)], [self._m(0.9)],
                        [self._m(0.8), self._m(0.79)]):
            self.assertTrue(decide(matches).reason,
                            "no reason given for %r" % (matches,))

    def test_the_threshold_reason_names_the_threshold_and_the_best_score(self):
        # the threshold is the one lever that would have changed this outcome
        d = decide([self._m(0.42)], threshold=0.75)
        self.assertIn("0.75", d.reason)
        self.assertIn("0.420", d.reason)

    def test_the_generic_word_reason_names_the_count(self):
        d = decide([self._m(0.7, meaningful_count=1)])
        self.assertIn("generic word", d.reason)
        self.assertIn("meaningful_count=1", d.reason)

    def test_the_ambiguity_reason_names_both_scores(self):
        d = decide([self._m(0.80), self._m(0.78)])
        self.assertIn("0.800", d.reason)
        self.assertIn("0.780", d.reason)

    def test_the_chosen_reason_names_the_winning_score(self):
        self.assertIn("0.900", decide([self._m(0.9)]).reason)

    def test_every_outcome_gives_its_own_distinct_reason(self):
        # "reason" is the question a user asks constantly; one catch-all
        # string that happens to contain every word we assert on is no answer
        reasons = [
            decide([]).reason,
            decide([self._m(0.9, meaningful_count=0)]).reason,
            decide([self._m(0.7, meaningful_count=1)]).reason,
            decide([self._m(0.4)]).reason,
            decide([self._m(0.8), self._m(0.78)]).reason,
            decide([self._m(0.9)]).reason,
        ]
        self.assertEqual(len(set(reasons)), len(reasons), reasons)

    def test_the_refusal_names_the_candidate_that_came_closest(self):
        # the second nearly qualified -- a threshold of 0.45 would have taken
        # it. The first can never qualify at that score however the file is
        # renamed, so naming it sends the user down a dead end.
        d = decide([self._m(0.60, meaningful_count=1),
                    self._m(0.49, meaningful_count=4)])
        self.assertIsNone(d.match)
        self.assertIn("threshold", d.reason)
        self.assertIn("0.490", d.reason)
        self.assertNotIn("generic word", d.reason)

    def test_a_missing_meaningful_count_raises(self):
        # the legacy default was 2 -- the exact value that skips the
        # generic-word rule, so a malformed candidate failed OPEN
        with self.assertRaises((TypeError, ValueError)):
            decide([Match(value=0.9, contained=False)])
