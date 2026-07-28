"""Scoring decides what gets written to a library without a human looking, so
the tests here state the wrong match each rule prevents."""
import unittest

from cronicled.scoring import (
    DEFAULT_THRESHOLD, Decision, Match, decide, meaningful_tokens, score,
    title_view)
from cronicled.vocab import CONTAINER_EXTS


class TitleView(unittest.TestCase):
    """The filename read as a title. Two things read it: the scorer, which
    weighs this view against every candidate, and the per-title fallback
    query in `scan.examine_sources`, which asks a store for it. The wrong
    match this prevents is the quiet one -- a store asked for one string
    while its answers are judged against another, so the right clip is never
    returned and the file refuses for a reason that names the wrong problem.
    """

    def test_a_container_extension_is_not_part_of_the_title(self):
        self.assertEqual(title_view("Morning Ritual.mp4"), "Morning Ritual")

    def test_a_series_prefix_goes_too(self):
        # A store asked for "Backyard Sessions Morning Ritual" by a creator
        # who files everything under that series gets the series page back,
        # not the clip -- the same repetition the scorer strips before
        # comparing.
        self.assertEqual(
            title_view("Backyard Sessions - Morning Ritual.mp4"),
            "Morning Ritual")

    def test_both_dash_conventions_are_read_as_the_boundary(self):
        for name in ("Backyard Sessions - Morning Ritual.mp4",
                     "Backyard Sessions -- Morning Ritual.mp4"):
            self.assertEqual(title_view(name), "Morning Ritual", name)

    def test_a_hyphenated_word_is_not_a_boundary(self):
        # No whitespace around the dash: a hyphenated name is one word, and
        # cutting it would ask a store for half a title.
        self.assertEqual(title_view("Wren-Copper Marchcroft.mp4"),
                         "Wren-Copper Marchcroft")

    def test_a_separator_with_nothing_after_it_keeps_the_whole_name(self):
        # Stripping here would leave nothing to search for at all.
        self.assertEqual(title_view("Morning Ritual - .mp4"),
                         "Morning Ritual - ")

    def test_an_unrecognised_suffix_stays_in_the_title(self):
        # The deliberate difference from `meaningful_tokens`, which drops an
        # extension-SHAPED suffix so it cannot inflate the evidence count.
        # Here the direction of harm is reversed: the count gate only ever
        # gets stricter for a token it refuses to believe, but a QUERY built
        # without ".Final" asks the store for less than the file says,
        # and a store that indexes the full title never answers.
        self.assertEqual(title_view("Morning Ritual.Final"),
                         "Morning Ritual.Final")
        self.assertEqual(
            meaningful_tokens("Morning Ritual.Final", ""),
            {"morning", "ritual"},
            "the evidence count still refuses to count an unknown suffix")


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

    def test_a_container_off_the_list_is_not_evidence(self):
        # These are recognised containers now, so the name loses them outright
        # rather than through the shaped-suffix rule below.
        for name in ("Addict.mpeg", "Addict.divx", "Addict.rm", "Addict.m2ts",
                     "Addict.ogm", "Addict.MPEG", "Addict.3gp"):
            self.assertEqual(meaningful_tokens(name, ""), {"addict"}, name)

    def test_a_suffix_on_no_list_at_all_is_still_not_evidence(self):
        # The other half, and the one that has to keep working however long
        # CONTAINER_EXTS grows: a suffix nothing recognises must not smuggle a
        # second "meaningful" token in either. Pinned against spellings the
        # list does not claim, and the assertion that it does not claim them is
        # part of the test — adding one of them to the list later would
        # otherwise quietly move this case to the branch above.
        for name in ("Addict.zqx", "Addict.vidx", "Addict.mp5"):
            self.assertNotIn(name[name.rindex("."):].lower(), CONTAINER_EXTS,
                             name)
            self.assertEqual(meaningful_tokens(name, ""), {"addict"}, name)

    def test_a_trailing_numeric_fragment_is_not_an_extension(self):
        # "Volume 2.10" is a part number, not a container
        self.assertEqual(meaningful_tokens("Volume 2.10", ""),
                         {"volume", "2", "10"})

    def test_a_trailing_word_is_not_an_extension(self):
        # only extension-SHAPED suffixes go; a real word stays evidence
        self.assertIn("extended",
                      meaningful_tokens("Morning Ritual.Extended", ""))

    def test_the_extension_shape_is_bounded_at_both_ends(self):
        # the suffix length bounds decide how much of a name can vanish, and
        # both directions are load-bearing: too low and a one-letter part
        # marker stops counting as evidence, too high and a real trailing
        # word does. Fewer tokens is a smaller set, and a smaller set is the
        # one that slips past the threshold as containment.
        for name, expected in (
            ("Winter Study.B", {"winter", "study", "b"}),          # 1 char
            ("Winter Study.rm", {"winter", "study"}),              # 2 chars
            ("Winter Study.mpeg2", {"winter", "study"}),           # 5 chars
            ("Winter Study.Sequel", {"winter", "study", "sequel"}),  # 6 chars
        ):
            self.assertEqual(meaningful_tokens(name, ""), expected, name)

    def test_the_folder_contributes_tokens(self):
        got = meaningful_tokens("Part 2.mp4", "Garden Sessions")
        self.assertIn("garden", got)
        self.assertIn("sessions", got)

    def test_a_series_prefix_is_not_evidence(self):
        # This is the answer to "which view defines the file's meaningful
        # tokens": the narrowest one. A series or studio prefix is identical
        # on every file filed under it, so it separates none of them from
        # each other -- the same reason the artist's own name is subtracted
        # two tests up. The artist is not the only name that repeats.
        self.assertEqual(
            meaningful_tokens("Backyard Sessions - Morning Ritual.mp4", ""),
            {"morning", "ritual"})

    def test_both_dash_conventions_lose_the_prefix(self):
        # The scorer's prefix view reads a single OR a doubled dash as the
        # boundary, and the evidence set has to read it the same way or the
        # two halves of the same file disagree about what the title is.
        for name in ("Backyard Sessions - Morning Ritual.mp4",
                     "Backyard Sessions -- Morning Ritual.mp4"):
            self.assertEqual(meaningful_tokens(name, ""),
                             {"morning", "ritual"}, name)

    def test_a_prefix_and_a_renamed_container_both_go_at_once(self):
        # One filename can carry both, and each strip has to survive the
        # other: leaving either in is a token counted as evidence that
        # distinguishes nothing.
        self.assertEqual(
            meaningful_tokens("Backyard Sessions - Morning Ritual.mpeg", ""),
            {"morning", "ritual"})

    def test_a_dash_that_is_not_a_prefix_boundary_keeps_the_whole_name(self):
        # The other side of the guard, and the one that decides how much of a
        # name can vanish. A dash with no whitespace around it is a hyphenated
        # word, not a separator, and a name with no dash at all has no prefix
        # to lose -- both must keep every token, or the count gate starts
        # firing on files that carry plenty of evidence.
        for name, expected in (
            ("Morning Ritual.mp4", {"morning", "ritual"}),
            ("Wren-Copper Marchcroft.mp4", {"wren", "copper", "marchcroft"}),
            # nothing after the separator: a prefix that is the whole name is
            # not a prefix, and stripping it would leave no evidence at all
            ("Morning Ritual - .mp4", {"morning", "ritual"}),
        ):
            self.assertEqual(meaningful_tokens(name, ""), expected, name)


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

    def test_a_folder_naming_only_the_artist_is_not_scored_as_a_view(self):
        # A folder that says nothing but the creator's name is the same
        # fact `_evidence` already treats as no evidence at all -- every file
        # of theirs carries it. Scored as a raw string it is not: "Velvet
        # Crane" resembles a title that happens to mention Velvet Crane
        # (studios routinely credit the performer in the title) regardless
        # of which file is asked, which is exactly ticket #96's bug. The
        # fix must make an artist-only folder score IDENTICALLY to no
        # folder at all, not merely differently from before.
        artist = "Velvet Crane"
        title = "Garden Sessions"
        name = "clip04.mp4"
        artist_only_folder = score(name, "Velvet Crane", title, artist=artist)
        no_folder = score(name, "", title, artist=artist)
        self.assertEqual(artist_only_folder, no_folder)

    def test_a_folder_with_real_evidence_still_contributes(self):
        # The other half: a folder is not always noise. One naming a real
        # collection adds tokens the filename does not have, and that is
        # evidence in its own right -- dropping the folder view outright
        # (rather than only when it reduces to the artist's name) would
        # throw this away too.
        artist = "Velvet Crane"
        title = "Garden Sessions"
        name = "clip04.mp4"
        with_collection = score(name, "Velvet Crane - Garden Sessions", title,
                                artist=artist)
        artist_only_folder = score(name, "Velvet Crane", title, artist=artist)
        self.assertGreater(with_collection.value, artist_only_folder.value)
        self.assertGreater(with_collection.value, 0.5)

    def test_a_folder_of_nothing_but_stopwords_is_not_scored_as_a_view(self):
        # A folder can reduce to nothing without being empty: "The Of And"
        # is a non-empty string but every token in it is a stopword, so it
        # carries the same amount of real evidence an empty folder does --
        # none. It must be barred from standalone scoring for the same
        # reason an artist-only folder is, and checked separately because a
        # fix keyed only off "the folder has no tokens at all" (an empty
        # string) would still let a pure-stopword folder through: `tokens()`
        # drops stopwords, but `clean_folder` does not, so the raw string
        # scored here is not empty even though it says nothing.
        name = "clip.mp4"
        folder = "The Of And"
        title = "The Of And Backyard Evening"
        with_folder = score(name, folder, title)
        no_folder = score(name, "", title)
        self.assertEqual(with_folder, no_folder)

    def test_two_files_in_one_creators_folder_no_longer_score_identically(self):
        # The bug itself: `max([stripped_name, cleaned_folder])` let a folder
        # that is just the creator's name outscore every filename in it,
        # identically, whenever a candidate title happened to mention that
        # creator. Two DIFFERENT files, same folder, same candidate: the
        # scores must differ, and neither may land anywhere near what the
        # folder alone used to hand out (0.86 here, verified against the
        # pre-fix arithmetic).
        artist = "Velvet Crane"
        folder = "Velvet Crane"
        title = "Velvet Crane Presents A Night Out"
        a = score("Morning Song.mp4", folder, title, artist=artist)
        b = score("River Talk.mp4", folder, title, artist=artist)
        self.assertNotEqual(a.value, b.value)
        self.assertLess(a.value, 0.5)
        self.assertLess(b.value, 0.5)

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
        #
        # That the suffix is UNRECOGNISED is what this rests on, and
        # CONTAINER_EXTS is now a list that grows -- so the assumption is
        # asserted rather than assumed. Add ".dawn" to it and this test would
        # otherwise start passing for a different reason than it was written
        # for, while the guard it names went uncovered.
        self.assertNotIn(".dawn", CONTAINER_EXTS)
        for name in ("Morning Ritual.Dawn", "Morning.Ritual.Dawn"):
            m = score(name, "", "Morning Ritual Dusk")
            self.assertFalse(m.contained, name)
            self.assertLess(m.value, 0.9, name)
            self.assertIsNone(decide([m], threshold=0.95).match, name)

    def test_a_recognised_container_scores_the_same_whichever_one_it_is(self):
        # The false negative the split exists to remove. The same file, the
        # same title, muxed differently: 0.933 and contained as `.mp4`, 0.686
        # and not contained as `.mpeg`, which is correct metadata held behind
        # a review queue over nothing at all. Every container the broad list
        # claims now answers exactly as `.mp4` does.
        # Named one by one rather than looped over CONTAINER_EXTS: a loop over
        # the constant stops visiting whatever the constant stops holding, so
        # dropping an entry would leave this passing. Confirmed by mutation.
        baseline = score("Morning Ritual.mp4", "", "Morning Ritual At Dawn")
        self.assertTrue(baseline.contained)
        for ext in (".mpeg", ".mpg", ".mpe", ".m1v", ".m2v", ".mpv", ".mp2",
                    ".m2ts", ".mts", ".vob", ".ogv", ".ogm", ".asf",
                    ".divx", ".xvid", ".rm", ".rmvb", ".3gp", ".3g2", ".f4v",
                    ".qt", ".dv", ".mxf", ".wtv", ".amv",
                    ".mkv", ".wmv", ".avi", ".mov", ".m4v", ".flv", ".ts",
                    ".webm", ".MPEG", ".Divx"):
            m = score("Morning Ritual" + ext, "", "Morning Ritual At Dawn")
            self.assertEqual(m, baseline, ext)

    def test_a_container_the_list_does_not_claim_still_withholds_containment(self):
        # And the asymmetry survives the widening, which is the thing most
        # easily lost while doing it. An unrecognised suffix still counts for
        # nothing in the evidence count AND still grants no containment -- the
        # confident half of the rule got bigger, the uncertain half did not
        # change what it is allowed to do.
        m = score("Morning Ritual.zqx", "", "Morning Ritual At Dawn")
        self.assertEqual(m.meaningful_count, 2)
        self.assertFalse(m.contained)

    def test_the_prefix_stripped_view_is_scored_at_all(self):
        # The whole reason that view exists: a filename repeating its series
        # before the title is compared against a bare catalogue title, and the
        # repetition drags both halves of the score down. Deleting the view
        # entirely used to leave the suite green -- this file is an EXACT
        # match once the prefix is gone, and 0.537 with it still attached.
        m = score("Backyard Sessions - Morning Ritual.mp4", "",
                  "Morning Ritual")
        self.assertEqual(m.value, 1.0)
        self.assertLess(score("Backyard Sessions Morning Ritual.mp4", "",
                              "Morning Ritual").value, 0.6,
                        "without a separator there is no prefix to strip")

    def test_a_series_prefix_cannot_pad_the_evidence_count(self):
        # The count gate asks whether this filename carries enough evidence to
        # tell one title from another. A series prefix is not that evidence --
        # every file under it carries the same one -- but it is two tokens
        # long, and counting it clears the `< 2` gate on the strength of text
        # that separates this file from nothing. What is actually being
        # matched on here is the single word "Ritual", and the rule that
        # refuses a single common word has to see that.
        m = score("Backyard Sessions - Ritual.mp4", "Velvet Crane",
                  "Morning Ritual", artist="Velvet Crane")
        self.assertEqual(m.meaningful_count, 1)
        self.assertEqual(m.value, 0.88)          # short of the 0.9 that rule wants
        self.assertIsNone(decide([m]).match)

    def test_a_series_prefix_cannot_manufacture_containment(self):
        # The counterpart, and the asymmetry the extension strip already
        # holds: dropping the prefix shrinks the evidence set, and a smaller
        # set is MORE of a subset -- which is the path that bypasses the
        # threshold outright. Here the file is "Evening Tea" in the series
        # "Morning Coffee", and the candidate is a DIFFERENT title that
        # happens to contain both of the surviving words. The strip that makes
        # the count honest must not be able to hand that candidate a bypass.
        m = score("Morning Coffee - Evening Tea.mp4", "",
                  "Evening Tea Ceremony")
        self.assertEqual(m.meaningful_count, 2)
        self.assertFalse(m.contained)
        self.assertIsNone(decide([m], threshold=0.95).match)

    def test_a_known_container_is_gone_before_containment_is_judged(self):
        # the counterpart: a container we recognise is confidently not
        # content, so stripping it must still leave containment intact
        m = score("Morning Ritual.mp4", "", "Morning Ritual At Dawn")
        self.assertTrue(m.contained)
        self.assertEqual(m.meaningful_count, 2)

    def test_renaming_the_container_cannot_defeat_the_generic_word_rule(self):
        # "Addict" alone is one generic word wherever it appears; the rule
        # that refuses it must not switch off because the file was renamed --
        # to a container the list claims, or to one it has never heard of.
        for name in ("Addict.mp4", "Addict.mpeg", "Addict.divx",
                     "Addict.rm", "Addict.m2ts", "Addict.zqx", "Addict.vidx"):
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
        # asserting only `value == round(value, 3)` is vacuous in the
        # direction that matters -- it is equally true of a 2-place value. So
        # pin that the score actually CARRIES a third decimal place, because
        # `decide` compares its ambiguity gap rounded to three places on the
        # assumption that its inputs are that precise. Coarsen `score` and
        # that comparison quietly stops matching its inputs.
        for name, title in (("Morning Ritual.mp4", "Morning Something Else"),
                            ("Morning Ritual Part Two.mp4", "Morning Ritual")):
            m = score(name, "", title)
            self.assertEqual(m.value, round(m.value, 3), name)
            self.assertNotEqual(m.value, round(m.value, 2), name)


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
        # An explicit threshold, because this test is about the MARGIN and
        # nothing else. Left to the default it broke when the default moved,
        # since the runner-up in the last pair fell below it and stopped
        # being eligible at all - a fixture that was quietly testing two
        # rules while claiming to test one.
        for top, runner in ((0.900, 0.850), (0.850, 0.800),
                            (0.800, 0.750), (0.700, 0.650)):
            d = decide([self._m(top), self._m(runner)], threshold=0.5)
            self.assertIsNone(d.match, "%.3f vs %.3f" % (top, runner))
            self.assertIn("ambiguous", d.reason)

    def test_a_gap_just_over_the_margin_is_decided(self):
        for top, runner in ((0.900, 0.849), (0.850, 0.799), (0.700, 0.649)):
            d = decide([self._m(top), self._m(runner)], threshold=0.5)
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

    def test_the_runner_up_is_the_second_best_not_the_last(self):
        # with three eligible candidates the near-tie is between the top two;
        # comparing the top against the worst of the field instead sees a
        # comfortable 0.30 gap and writes the winner despite a live dilemma
        # 0.02 away from it
        for matches in ([self._m(0.90), self._m(0.88), self._m(0.60)],
                        [self._m(0.60), self._m(0.90), self._m(0.88)]):
            d = decide(matches)
            self.assertIsNone(d.match, matches)
            self.assertIn("ambiguous", d.reason)
            self.assertIn("0.880", d.reason)

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

    def test_a_decision_says_how_many_candidates_competed(self):
        # Refusals arrive as match=None and mean opposite things. "Nothing
        # cleared the bar" is consistent with the source listing no entry for
        # the file at all; "two cleared it and I cannot say which" is the
        # opposite -- entries that look like this file are RIGHT THERE. A
        # consumer that treats a refusal as evidence of absence
        # (stashbox.listing_verdict) has to tell them apart, and the only
        # thing that currently distinguishes them is the wording of `reason`.
        # scan.py already refuses to read a fact off that prose, for the
        # reason stated there: the wording is free to change and nothing would
        # notice. So the count is carried as a number.
        self.assertEqual(decide([]).contenders, 0)
        self.assertEqual(decide([self._m(0.4)]).contenders, 0)
        self.assertEqual(decide([self._m(0.9, meaningful_count=0)]).contenders, 0)
        self.assertEqual(decide([self._m(0.7, meaningful_count=1)]).contenders, 0)
        # refused as ambiguous -- both cleared the bar
        self.assertEqual(decide([self._m(0.80), self._m(0.78)]).contenders, 2)
        # a winner, and the loser it beat by more than the margin. The count
        # is of candidates that COMPETED, not of the one that won, so a
        # decision that names a winner still reports both.
        self.assertEqual(decide([self._m(0.9), self._m(0.2)]).contenders, 1)
        self.assertEqual(
            decide([self._m(0.9), self._m(0.75), self._m(0.2)]).contenders, 2)

    def test_a_decision_says_whether_the_candidates_were_interrogated(self):
        # `contenders == 0` is returned for four different refusals and only
        # one of them is evidence about the source's listing. Two of the
        # others never weighed a single candidate title: the caller offered
        # nothing, or the filename carried no word that is not the artist's
        # or generic --
        # which `_is_eligible` bars at ANY score, so no title was ever
        # compared. A consumer reading only `contenders` cannot tell those
        # from "500 entries were checked and none is close", and would report
        # a fabricated absence over a listing it never questioned.
        self.assertFalse(decide([]).interrogated, "the caller asked nothing")
        self.assertFalse(
            decide([self._m(0.9, meaningful_count=0)]).interrogated,
            "there was nothing to ask with")
        # No score and no containment CLAIM buys a look either, and the order
        # matters for the same reason it does in `_is_eligible`: the zero is
        # checked before anything that could excuse it. A containment flag on
        # a candidate with no meaningful token is barred there at any score,
        # so it was never weighed here -- and letting it vouch for a look
        # would be a guard skipped by a value that reads as extra confidence.
        self.assertFalse(
            decide([self._m(1.0, contained=True,
                            meaningful_count=0)]).interrogated)

        # BOTH sides of the boundary. meaningful_count=1 is a real
        # interrogation -- the token was compared against every title and fell
        # short of a bar a higher score would have cleared -- and a guard
        # drifting to `> 1` would silently stop licensing an absence for every
        # one-word filename. That direction is as wrong as the loose one and
        # much quieter.
        self.assertTrue(
            decide([self._m(0.7, meaningful_count=1)]).interrogated)
        self.assertTrue(decide([self._m(0.4)]).interrogated)
        self.assertTrue(decide([self._m(0.80), self._m(0.78)]).interrogated)
        self.assertTrue(decide([self._m(0.9)]).interrogated)

    def test_one_candidate_without_evidence_withholds_the_whole_claim(self):
        # `meaningful_count` comes from the name, the folder and the artist,
        # never from the candidate title, so every entry of a list built for
        # one file carries the same one. A list where they DIFFER is a caller
        # pooling candidates scored for different files, and no reading of it
        # supports an absence -- so `all`, not `any`. Uncertainty may withhold
        # evidence, never supply it.
        #
        # Asserted on every branch that builds a Decision, because the field
        # is only consulted on one of them: an inconsistent value on the
        # others is invisible downstream and would be free to drift.
        mixed = [self._m(0.9), self._m(0.5, meaningful_count=0)]
        self.assertFalse(decide(mixed).interrogated)
        self.assertIsNotNone(decide(mixed).match, "a winner was still picked")
        self.assertFalse(
            decide([self._m(0.80), self._m(0.78),
                    self._m(0.5, meaningful_count=0)]).interrogated)
        self.assertFalse(
            decide([self._m(0.4), self._m(0.5, meaningful_count=0)]).interrogated)

    def test_a_missing_contender_count_raises(self):
        # Same shape as the meaningful_count guard above, and the same reason:
        # 0 is not a neutral default here, it is precisely the value that
        # licenses a downstream absence claim. A Decision assembled without
        # one must fail loudly rather than assert "nothing competed".
        with self.assertRaises(TypeError):
            Decision(match=None, index=None, reason="nothing above the threshold",
                     interrogated=True)

    def test_a_missing_interrogation_flag_raises(self):
        # The other half of the same licence, and the same asymmetry: True is
        # the value that lets an absence be claimed, so a Decision assembled
        # without one must fail rather than assert that a look happened.
        with self.assertRaises(TypeError):
            Decision(match=None, index=None, reason="nothing above the threshold",
                     contenders=0)


class TheDefaultThresholdIsTheMeasuredOne(unittest.TestCase):
    """Raising the default broke no test, which is why this exists: the
    number deciding whether a file is written without a person looking was
    not observed by anything.

    It is 0.7 because that is what a real library said. Against 5924 scenes
    across 99 creators, 0.5 applied a wrong entry 16% of the time when the
    right one was absent from the catalogue; 0.7 cuts that to 6% for three
    points of recall. The whole table is in `scoring.py` beside the constant,
    including a note on the measurement error that first pointed at 0.6."""

    def test_the_default_is_the_measured_value(self):
        self.assertEqual(DEFAULT_THRESHOLD, 0.7)

    def test_decide_uses_it_when_the_caller_names_no_threshold(self):
        # The constant is only worth pinning if it is the one in force.
        just_over = Match(value=0.71, contained=False, meaningful_count=2)
        just_under = Match(value=0.69, contained=False, meaningful_count=2)
        self.assertIsNotNone(decide([just_over]).match)
        self.assertIsNone(decide([just_under]).match)

    def test_an_explicit_threshold_still_wins(self):
        # The measurement picked a default, not a policy. An operator whose
        # library is shaped differently has to be able to say so.
        m = Match(value=0.65, contained=False, meaningful_count=2)
        self.assertIsNone(decide([m]).match)
        self.assertIsNotNone(decide([m], threshold=0.5).match)
