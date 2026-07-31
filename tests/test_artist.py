"""Which creator a file belongs to scopes every later decision, so these tests
state the wrong attribution each rule prevents."""
import unittest
from unittest import mock

import cronicled.artist
from cronicled.artist import (Aliases, CONTAINER_NAMES, MAX_NAME_WORDS,
                              MIN_NAME_CHARS, Resolution, creator_folder,
                              resolve)


class CreatorFolder(unittest.TestCase):
    def test_the_immediate_parent_is_the_usual_answer(self):
        self.assertEqual(creator_folder("/lib/Velvet Crane/clip01.mp4"),
                         "Velvet Crane")

    def test_a_generic_container_is_skipped(self):
        # the file belongs to the creator, not to a directory called Clips
        self.assertEqual(creator_folder("/lib/Velvet Crane/Clips/clip01.mp4"),
                         "Velvet Crane")

    def test_several_stacked_containers_are_skipped(self):
        self.assertEqual(
            creator_folder("/lib/Velvet Crane/Videos/Downloads/clip01.mp4"),
            "Velvet Crane")

    def test_a_qualifier_is_stripped_at_the_boundary(self):
        # "(h265)" describes the encode, not the creator -- and stripping it
        # here means no caller has to remember to
        self.assertEqual(creator_folder("/lib/Velvet Crane (h265)/clip01.mp4"),
                         "Velvet Crane")

    def test_a_qualified_folder_is_still_recognised_as_a_container(self):
        self.assertEqual(
            creator_folder("/lib/Velvet Crane/Clips [2024]/clip01.mp4"),
            "Velvet Crane")

    def test_a_qualifier_only_directory_is_walked_past(self):
        # "[2024]" cleans to nothing, so it names no creator -- a container by
        # the same logic as "Clips". Returning it would strand every file
        # under a bracketed-year subfolder with no folder attribution at all.
        self.assertEqual(
            creator_folder("/lib/Velvet Crane/[2024]/Clips/clip01.mp4"),
            "Velvet Crane")

    def test_an_encode_tag_only_directory_is_walked_past(self):
        self.assertEqual(
            creator_folder("/lib/Velvet Crane/(h265)/clip01.mp4"),
            "Velvet Crane")

    def test_the_walk_is_bounded(self):
        # the bound has to be the only thing deciding this, so the path is one
        # where a bounded and an unbounded walk differ: four containers stop
        # the walk short, and a fifth step would have reached the creator
        path = "/lib/Velvet Crane/Clips/Videos/Media/Downloads/clip01.mp4"
        self.assertEqual(creator_folder(path), "Downloads")
        self.assertEqual(creator_folder(path, max_up=99), "Velvet Crane")

    def test_all_containers_to_the_root_falls_back_to_the_parent(self):
        # a real case, but not a test of the bound: an unbounded walk gives
        # the same answer, because there is nothing above but containers
        got = creator_folder("/Videos/Clips/Media/Downloads/Uploads/clip01.mp4")
        self.assertEqual(got, "Uploads")

    def test_a_file_with_no_parent_yields_empty(self):
        self.assertEqual(creator_folder("clip01.mp4"), "")

    def test_a_windows_style_path_yields_nothing(self):
        # paths are split posix-style, as strings; a backslash is an ordinary
        # filename character, so this has no parent directory to walk. Stated
        # as a test because it is a deliberate limit, not an oversight.
        self.assertEqual(
            creator_folder("C:\\lib\\Velvet Crane\\clip01.mp4"), "")

    def test_container_matching_ignores_case(self):
        self.assertEqual(creator_folder("/lib/Velvet Crane/CLIPS/clip01.mp4"),
                         "Velvet Crane")

    def test_a_vids_container_is_skipped(self):
        # "Vids" is a real library shorthand for "Videos", not a creator name
        self.assertEqual(creator_folder("/lib/Velvet Crane/Vids/clip01.mp4"),
                         "Velvet Crane")

    def test_the_container_set_is_defined_once(self):
        # the legacy code tested this membership in two files with two
        # different normalisations; they agreed only by accident of call order
        self.assertIn("clips", CONTAINER_NAMES)
        self.assertIn("videos", CONTAINER_NAMES)

    def test_a_bare_encode_marker_is_stripped_at_the_boundary(self):
        # the same tag as above with the brackets left off, which is how it is
        # actually filed most of the time
        self.assertEqual(creator_folder("/lib/Velvet Crane x265/clip01.mp4"),
                         "Velvet Crane")

    def test_a_bare_marker_only_directory_is_walked_past(self):
        # it cleans away to nothing exactly as "(h265)" does, so it names no
        # creator and the walk continues rather than handing back ""
        self.assertEqual(creator_folder("/lib/Velvet Crane/x265/clip01.mp4"),
                         "Velvet Crane")

    def test_a_trailing_token_that_is_no_marker_stays_on_the_folder(self):
        # the direction that costs names: "3" belongs to this creator's folder
        # and a shortened name would still read as a name
        self.assertEqual(creator_folder("/lib/Velvet Crane 3/clip01.mp4"),
                         "Velvet Crane 3")


class Resolving(unittest.TestCase):
    def test_a_dash_split_names_the_creator(self):
        r = resolve("Velvet Crane - Morning Ritual.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")
        self.assertEqual(r.source, "filename")

    def test_the_folder_is_preferred_over_the_filename(self):
        # someone chose to file it there; that is the more deliberate signal
        r = resolve("Velvet Crane - Morning Ritual.mp4", "Copper Wren")
        self.assertEqual(r.name, "Copper Wren")
        self.assertEqual(r.source, "folder")

    def test_disagreement_is_reported_not_discarded(self):
        # the legacy resolver silently took the first that qualified
        r = resolve("Velvet Crane - Morning Ritual.mp4", "Copper Wren")
        self.assertEqual(r.competing, "Velvet Crane")

    def test_agreement_reports_no_competitor(self):
        r = resolve("Velvet Crane - Morning Ritual.mp4", "Velvet Crane")
        self.assertIsNone(r.competing)

    def test_the_bare_folder_is_the_fallback(self):
        r = resolve("clip01.mp4", "Velvet Crane")
        self.assertEqual(r.name, "Velvet Crane")
        self.assertEqual(r.source, "folder")

    def test_nothing_resolvable_is_a_real_answer(self):
        r = resolve("clip01.mp4", "")
        self.assertIsNone(r.name)
        self.assertIsNone(r.source)


class EncodeMarkersOnTheFolder(unittest.TestCase):
    """A bare marker passes every guard `_is_name` enforces -- it is long
    enough, it is one word, it is not a date, it leads with no article and it
    is no container -- so before it was stripped it was returned as the
    creator's name. Measured on one library: six folders, 3384 of 6288 scenes,
    attributed to a person who does not exist. The search that name produces
    scores no worse, which is exactly why nothing caught it."""

    def test_a_bare_marker_is_not_part_of_the_creators_name(self):
        r = resolve("clip01.mp4", "Velvet Crane x265")
        self.assertEqual(r, Resolution("Velvet Crane", "folder", None, None))

    def test_a_folder_that_is_only_a_bare_marker_resolves_to_nobody(self):
        # It must not become "" and then be read as a name, and it must not be
        # read as one under its own spelling either. Asserted whole: a sampled
        # check would not notice the empty string arriving in `name` or in
        # `rejected_folder`.
        self.assertEqual(resolve("clip01.mp4", "x265"), Resolution())

    def test_a_folder_ending_in_an_unclaimed_token_resolves_to_all_of_it(self):
        # The silent direction. Nothing about "3" says encode marker, so the
        # creator keeps it -- a name shortened by one token still reads as a
        # name and no report ever questions it.
        r = resolve("clip01.mp4", "Velvet Crane 3")
        self.assertEqual(r, Resolution("Velvet Crane 3", "folder", None, None))


class RejectedFolder(unittest.TestCase):
    """A folder a guard threw out is still evidence, and is reported.

    Without this the module *substitutes*: the filename's answer is returned
    and the folder that disagreed is recorded nowhere, so a reviewer reading
    the resolution cannot tell a file with no folder evidence from one whose
    folder said someone else.
    """

    def test_a_guard_rejected_folder_is_recorded_not_dropped(self):
        # /lib/The Velvet Crane/Copper Wren - Guest Spot.mp4 -- a collaboration
        # filed under the owner and named after the guest. The article guard
        # throws the folder out and the guest wins; saying so is the whole
        # point, because every later step scores against Copper Wren's
        # catalogue and nothing downstream can know the folder objected.
        r = resolve("Copper Wren - Guest Spot.mp4", "The Velvet Crane")
        self.assertEqual(r.name, "Copper Wren")
        self.assertEqual(r.source, "filename")
        self.assertEqual(r.rejected_folder, "The Velvet Crane")

    def test_a_rejected_folder_is_recorded_when_nothing_resolves_either(self):
        r = resolve("clip01.mp4", "Downloads")
        self.assertIsNone(r.name)
        self.assertEqual(r.rejected_folder, "Downloads")

    def test_a_too_short_folder_is_recorded(self):
        r = resolve("clip01.mp4", "AB")
        self.assertEqual(r.rejected_folder, "AB")

    def test_a_folder_that_won_is_not_also_reported_as_rejected(self):
        r = resolve("clip01.mp4", "Velvet Crane")
        self.assertIsNone(r.rejected_folder)

    def test_a_folder_an_alias_matched_is_not_a_rejection(self):
        # the alias consumed the folder; nothing was thrown out
        r = resolve("clip01.mp4", "vc", aliases={"vc": "Velvet Crane"})
        self.assertEqual(r.source, "alias")
        self.assertIsNone(r.rejected_folder)

    def test_no_folder_at_all_is_not_a_rejection(self):
        r = resolve("Velvet Crane - Morning Ritual.mp4", "")
        self.assertIsNone(r.rejected_folder)

    def test_a_rejected_folder_is_reported_cleaned_not_raw(self):
        # a deliberate choice, pinned: rejected_folder shows what the guards
        # judged, and the guards never see the bracketed qualifier at all
        r = resolve("clip01.mp4", "Downloads (2024)")
        self.assertEqual(r.rejected_folder, "Downloads")


class Disagreement(unittest.TestCase):
    """`competing` is suppressed by identity, not by containment.

    A folder only has to clear a 3-character / 4-word gate, so it is often the
    *less* specific of the two candidates. Containment ("Ivy" is inside "Ivy
    Kingsley Waters") would call those the same person, pick the shorter, and
    report no disagreement -- scoping every later search to the wrong name and
    saying nothing about it.
    """

    def test_a_folder_that_is_a_fragment_of_the_filename_still_disagrees(self):
        r = resolve("Ivy Kingsley Waters - Morning Ritual.mp4", "Ivy")
        self.assertEqual(r.name, "Ivy")
        self.assertEqual(r.source, "folder")
        self.assertEqual(r.competing, "Ivy Kingsley Waters")

    def test_a_filename_that_extends_the_folder_still_disagrees(self):
        r = resolve("Velvet Crane Studios - Morning Ritual.mp4", "Velvet Crane")
        self.assertEqual(r.name, "Velvet Crane")
        self.assertEqual(r.competing, "Velvet Crane Studios")

    def test_spacing_and_punctuation_do_not_count_as_disagreement(self):
        # the worked example: "Velvet Crane" and "velvetcrane" are one person
        r = resolve("velvetcrane - Morning Ritual.mp4", "Velvet Crane")
        self.assertEqual(r.name, "Velvet Crane")
        self.assertIsNone(r.competing)

    def test_a_composed_and_a_decomposed_accent_are_one_person(self):
        # same name, folder written NFD and filename NFC -- a routine
        # difference between what a filesystem stores and what a scraper emits
        folder = "Zo\u0065\u0308 Marchcroft"                    # NFD
        filename = "Zo\u00eb Marchcroft - Morning Ritual.mp4"    # NFC
        self.assertNotEqual(folder, "Zo\u00eb Marchcroft")       # really differ
        r = resolve(filename, folder)
        self.assertEqual(r.source, "folder")
        self.assertIsNone(r.competing)

    def test_a_bracketed_site_tag_on_the_filename_does_not_manufacture_a_disagreement(self):
        # the worked example: a scraper-inserted site tag ahead of the name
        # must not make the same person compete with themselves
        r = resolve("[SiteTag] Velvet Crane - Morning Ritual.mp4", "Velvet Crane")
        self.assertEqual(r.name, "Velvet Crane")
        self.assertIsNone(r.competing)

    def test_an_encode_qualifier_on_the_filename_does_not_manufacture_a_disagreement(self):
        r = resolve("Velvet Crane (h265) - Morning Ritual.mp4", "Velvet Crane")
        self.assertEqual(r.name, "Velvet Crane")
        self.assertIsNone(r.competing)


class Guards(unittest.TestCase):
    def test_a_date_left_of_the_dash_is_not_a_creator(self):
        # "2023 September 11 - Foot cam" is a date convention, not a person
        r = resolve("2023 September 11 - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_an_article_led_left_side_is_a_title_containing_a_dash(self):
        r = resolve("The Long Wait - Part Two.mp4", "")
        self.assertIsNone(r.name)

    def test_a_sentence_is_not_a_name(self):
        r = resolve("How I Spent My Entire Weekend Alone - Diary.mp4", "")
        self.assertIsNone(r.name)

    def test_a_very_short_folder_is_not_a_name(self):
        r = resolve("clip01.mp4", "AB")
        self.assertIsNone(r.name)

    def test_a_container_word_is_never_a_creator(self):
        r = resolve("clip01.mp4", "Downloads")
        self.assertIsNone(r.name)

    def test_a_guest_is_not_credited_as_the_creator(self):
        # crediting the featured performer is exactly the wrong answer
        r = resolve("Copper Wren Presents A Show Feat Velvet Crane.mp4", "")
        self.assertIsNone(r.name)

    def test_a_feature_marker_is_trusted_when_it_also_leads(self):
        r = resolve("Velvet Crane Morning Ritual Feat Velvet Crane.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")


class DateShapes(unittest.TestCase):
    """The date guard has to cover the filing styles people actually use.

    A date read as a person is the worst kind of wrong answer here: it is a
    creator who does not exist, so every file filed that way gets its own
    fictional catalogue. The counterweight is that a month word is also an
    ordinary given name, and rejecting those would decline real people --
    so the tests below run in both directions.
    """

    def test_a_compact_iso_date_is_not_a_creator(self):
        r = resolve("20230911 - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_a_bare_year_folder_is_not_a_creator(self):
        r = resolve("clip01.mp4", "2023")
        self.assertIsNone(r.name)

    def test_an_abbreviated_month_with_a_year_is_not_a_creator(self):
        r = resolve("Sep 2023 - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_a_spelled_month_with_a_year_is_not_a_creator(self):
        r = resolve("September 2023 - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_a_year_before_the_month_is_not_a_creator_either(self):
        r = resolve("2023 September - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_a_month_word_used_as_a_given_name_still_resolves(self):
        # "March Hollis" is a person; the guard must not reach a month word
        # that is doing duty as a first name
        r = resolve("March Hollis - Morning Ritual.mp4", "")
        self.assertEqual(r.name, "March Hollis")

    def test_a_month_word_used_as_a_folder_name_still_resolves(self):
        # a plain invented surname -- not "Winters", which pairs a month with
        # a season and reads as a deliberate stage name rather than a case
        # the month-word guard has to stay clear of
        r = resolve("clip01.mp4", "May Fenwick")
        self.assertEqual(r.name, "May Fenwick")

    def test_a_compact_month_year_is_not_a_creator(self):
        # no whitespace at all between the month and the year -- one
        # character away from the guard already in place, and the same
        # filing family: "Sep2023 - Title.mp4" is a date, not a person
        r = resolve("Sep2023 - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_a_compact_month_year_with_the_longer_abbreviation_is_not_a_creator(self):
        r = resolve("Sept2023 - Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_bare_month_names_still_resolve_as_names(self):
        # closing the compact-date gap must not start swallowing a real
        # creator whose whole name is a month word
        for candidate in ("April", "June", "July", "Jan"):
            with self.subTest(candidate=candidate):
                r = resolve("clip01.mp4", candidate)
                self.assertEqual(r.name, candidate)

    def test_month_led_names_still_resolve_as_names(self):
        for candidate in ("June Carter", "August Rain"):
            with self.subTest(candidate=candidate):
                r = resolve(candidate + " - Morning Ritual.mp4", "")
                self.assertEqual(r.name, candidate)


class DateGuardEdges(unittest.TestCase):
    """Three edges of the month-year and all-digit date guards that the
    existing suite happens not to pin.

    Each is a one-line narrowing that a mutation survives today: the guard
    still rejects every fixture already in the suite, just not by the same
    mechanism, so nothing catches the drift until a fixture built for that
    exact edge is added.
    """

    def test_the_month_year_match_is_anchored_at_both_ends(self):
        # without the trailing '$' this would match on the leading "September
        # 2023" and reject a real name -- the over-strict direction, and the
        # one that scopes a real creator's files to nothing
        r = resolve("September 2023 Hollis - Morning Ritual.mp4", "")
        self.assertEqual(r.name, "September 2023 Hollis")

    def test_the_month_year_match_survives_repeated_whitespace(self):
        # the guard matches against a whitespace-collapsed copy of the text.
        # The dash-split path already collapses via `clean_folder`, so this
        # has to go through a `feat` marker instead -- the one path whose
        # captured text isn't cleaned -- to actually exercise the guard's own
        # collapse: without it, "Sep  2023" (two spaces) would slip past the
        # optional single-space separator and resolve as a name.
        r = resolve("Sep  2023 Feat Sep  2023.mp4", "")
        self.assertIsNone(r.name)

    def test_the_all_digit_guard_ignores_separators(self):
        # `spaceless` folds "-" to nothing before the digit check runs, so
        # "2023-09" is caught; narrowing that to a plain `.isdigit()` on the
        # raw text would miss the separator and let it through
        r = resolve("clip01.mp4", "2023-09")
        self.assertIsNone(r.name)


class GuardBoundaries(unittest.TestCase):
    """The permissive side of the two numeric guards.

    Only the restrictive side was pinned, so a guard that drifted one step
    tighter -- rejecting exactly three characters, or exactly four words --
    would break no test. That failure is quieter than the loose one it
    replaces: it hands back no name at all, so every downstream search is
    scoped to nothing and every assertion that something is rejected still
    passes. The fixtures are tied to the constants so they cannot drift.
    """

    def test_a_name_of_exactly_the_minimum_length_is_accepted(self):
        self.assertEqual(len("Ivy"), MIN_NAME_CHARS)
        r = resolve("clip01.mp4", "Ivy")
        self.assertEqual(r.name, "Ivy")

    def test_a_name_of_exactly_the_maximum_word_count_is_accepted(self):
        folder = "Ivy Kingsley Waters Marchcroft"
        self.assertEqual(len(folder.split()), MAX_NAME_WORDS)
        r = resolve("clip01.mp4", folder)
        self.assertEqual(r.name, folder)


class FilenameConventions(unittest.TestCase):
    """How a filename is split, in both directions."""

    def test_a_hyphenated_name_is_left_whole(self):
        # no whitespace round the hyphen, so it is part of the name and not a
        # separator -- "Wren-Copper" is half of who this belongs to
        r = resolve("Wren-Copper Marchcroft - Morning Ritual.mp4", "")
        self.assertEqual(r.name, "Wren-Copper Marchcroft")

    def test_a_doubled_dash_does_not_separate(self):
        # pinned, not endorsed: the split wants exactly one dash with
        # whitespace either side, so "A -- B" yields nothing rather than
        # guessing at a second convention. Declining is the cheap failure.
        r = resolve("Velvet Crane -- Morning Ritual.mp4", "")
        self.assertIsNone(r.name)

    def test_an_en_dash_separates(self):
        r = resolve("Velvet Crane – Morning Ritual.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")

    def test_an_em_dash_separates(self):
        r = resolve("Velvet Crane — Morning Ritual.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")

    def test_only_the_first_dash_splits(self):
        # later dashes belong to the title; splitting on all of them would
        # make the creator whatever the last segment happened to be
        r = resolve("Velvet Crane - Morning Ritual - Part Two.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")

    def test_ft_is_read_as_a_feature_marker(self):
        r = resolve("Velvet Crane Morning Ritual Ft Velvet Crane.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")

    def test_featuring_is_read_as_a_feature_marker(self):
        r = resolve("Velvet Crane Morning Ritual Featuring Velvet Crane.mp4", "")
        self.assertEqual(r.name, "Velvet Crane")

    def test_a_guest_after_ft_is_still_not_credited(self):
        # the marker being recognised is what makes the guest guard reachable
        r = resolve("Copper Wren Presents A Show Ft Velvet Crane.mp4", "")
        self.assertIsNone(r.name)


class GuardIsolation(unittest.TestCase):
    """The word-count guard needs a case only it rejects.

    `test_a_sentence_is_not_a_name` is over-determined: "How I Spent My
    Entire Weekend Alone" is both too long AND article-led ("how"), so
    deleting the word-count guard alone leaves that test passing and the
    guard unpinned. This is the same case with a lead word that is not an
    article, so the word count is the only thing rejecting it.
    """

    def test_a_long_title_is_rejected_on_length_alone(self):
        r = resolve("Morning Ritual In The Woods - Diary.mp4", "")
        self.assertIsNone(r.name)


class AliasMatching(unittest.TestCase):
    def test_an_alias_resolves_to_the_full_name(self):
        r = resolve("clip01.mp4", "vcrane", aliases={"vcrane": "Velvet Crane"})
        self.assertEqual(r.name, "Velvet Crane")
        self.assertEqual(r.source, "alias")

    def test_alias_matching_ignores_case_and_punctuation(self):
        r = resolve("clip01.mp4", "V-Crane", aliases={"vcrane": "Velvet Crane"})
        self.assertEqual(r.name, "Velvet Crane")

    def test_an_alias_key_written_un_normalised_still_matches(self):
        # the operator wrote "V-Crane" in the map and the folder on disk is
        # "vcrane"; normalisation is applied to both sides, not just the folder
        r = resolve("clip01.mp4", "vcrane", aliases={"V-Crane": "Velvet Crane"})
        self.assertEqual(r.name, "Velvet Crane")

    def test_an_unlisted_abbreviation_does_not_resolve_to_a_guess(self):
        # guessing what an abbreviation means is how files get attributed to
        # the wrong person; not resolving is the correct failure
        r = resolve("clip01.mp4", "vc", aliases={"vcrane": "Velvet Crane"})
        self.assertIsNone(r.name)


class AliasWiring(unittest.TestCase):
    """A malformed alias map is refused, not resolved by luck.

    Every case here used to be silent: an ambiguous key resolved by dict
    iteration order, an unusable value resolved to itself or to nothing. A
    wiring mistake has to fail where it was made.
    """

    def test_two_keys_that_normalise_alike_are_refused(self):
        with self.assertRaises(ValueError) as caught:
            resolve("clip01.mp4", "vcrane",
                    aliases={"vcrane": "Velvet Crane", "v crane": "Copper Wren"})
        message = str(caught.exception)
        self.assertIn("vcrane", message)      # both keys named, so the
        self.assertIn("v crane", message)     # operator knows what to delete

    def test_a_collision_is_refused_even_when_both_keys_agree_on_the_name(self):
        # the docstring's claim, pinned: the rule an operator can hold in
        # their head is one key per normalised form, not "unless it wouldn't
        # have mattered" -- so two spellings of the same key are refused even
        # when they'd have resolved to the same creator either way
        with self.assertRaises(ValueError):
            resolve("clip01.mp4", "vcrane",
                    aliases={"vcrane": "Velvet Crane", "v crane": "Velvet Crane"})

    def test_a_collision_is_refused_whoever_is_being_resolved(self):
        # the map is wrong regardless of this file; finding out only when the
        # ambiguous key happens to come up is finding out far too late
        with self.assertRaises(ValueError):
            resolve("clip01.mp4", "Copper Wren",
                    aliases={"vcrane": "Velvet Crane", "V-Crane": "Copper Wren"})

    def test_a_key_that_normalises_to_nothing_is_refused(self):
        # it can never match; leaving it in the map looks like coverage
        with self.assertRaises(ValueError):
            resolve("clip01.mp4", "vcrane", aliases={"--": "Velvet Crane"})

    def test_an_empty_alias_value_is_refused(self):
        # name='' with source='alias' is not an attribution
        with self.assertRaises(ValueError):
            resolve("clip01.mp4", "vcrane", aliases={"vcrane": ""})

    def test_a_non_string_alias_value_is_refused(self):
        with self.assertRaises(ValueError):
            resolve("clip01.mp4", "vcrane", aliases={"vcrane": 42})

    def test_a_null_alias_value_is_refused(self):
        # it used to fall through, and "vcrane" was then resolved as a creator
        # in its own right -- a half-written line quietly becoming a name
        with self.assertRaises(ValueError):
            resolve("clip01.mp4", "vcrane", aliases={"vcrane": None})


class TheAliasIndexAsAValue(unittest.TestCase):
    """The index a lookup needs is derived from a map that does not change
    during a run, and `resolve` was rebuilding it on every call — for a scan,
    once per file. `Aliases` is that index as a value the caller builds once.

    Two things had to be decided rather than assumed, and both are pinned
    here: that a prebuilt index answers exactly as the mapping it came from
    (or it is an optimisation that changes attributions), and that it is a
    SNAPSHOT rather than a live view of the mapping — which is the whole
    argument against hiding the same saving inside `resolve` as a cache. A
    cache keyed on a mutable mapping's identity would answer from a stale
    index after the caller edited it, silently, and this states plainly which
    of the two readings applies to which argument.
    """

    MAP = {"vcrane": "Velvet Crane", "V-Wren": "Copper Wren"}

    def test_a_prebuilt_index_answers_exactly_as_its_mapping_does(self):
        # Every shape the matching rules distinguish, asked both ways: an
        # unnormalised key, an unnormalised folder, a miss, and an empty
        # folder. A saving that changed any of these answers would be
        # attributing files differently to buy time.
        built = Aliases(self.MAP)
        for name, folder in (("clip01.mp4", "vcrane"),
                             ("clip01.mp4", "V-Crane"),
                             ("clip01.mp4", "vwren"),
                             ("clip01.mp4", "nobody"),
                             ("clip01.mp4", ""),
                             ("Ivy Kingsley - Morning Ritual.mp4", "vcrane"),
                             ("Ivy Kingsley - Morning Ritual.mp4", "nobody")):
            self.assertEqual(resolve(name, folder, built),
                             resolve(name, folder, self.MAP),
                             (name, folder))

    def test_it_is_a_snapshot_and_the_mapping_is_not(self):
        source = {"vcrane": "Velvet Crane"}
        built = Aliases(source)
        source["vcrane"] = "Somebody Else"

        self.assertEqual(resolve("clip01.mp4", "vcrane", built).name,
                         "Velvet Crane", "the index was taken at build time")
        self.assertEqual(resolve("clip01.mp4", "vcrane", source).name,
                         "Somebody Else", "a mapping is read on every call")

    def test_a_prebuilt_index_is_not_rebuilt_on_a_lookup(self):
        # The saving itself, counted rather than timed. A timing assertion
        # would be flaky and would not say what regressed; this fails naming
        # the number of rebuilds a lookup caused.
        built = Aliases(self.MAP)
        rebuilds = []
        real = cronicled.artist._alias_index

        def counting(mapping):
            rebuilds.append(mapping)
            return real(mapping)

        with mock.patch("cronicled.artist._alias_index", counting):
            for _ in range(5):
                resolve("clip01.mp4", "vcrane", built)
            self.assertEqual(rebuilds, [], "a lookup rebuilt the index")
            resolve("clip01.mp4", "vcrane", self.MAP)
            self.assertEqual(len(rebuilds), 1,
                             "a plain mapping is still indexed per call")

    def test_a_malformed_map_is_refused_when_the_index_is_built(self):
        # The gap this closes. Building the index is something a caller does
        # at load, with the map in front of it; resolving is something it does
        # per file, in a run. Every refusal `_alias_index` makes now lands at
        # the first of those.
        for bad in ({"vcrane": "Velvet Crane", "v crane": "Copper Wren"},
                    {"--": "Velvet Crane"},
                    {"vcrane": ""},
                    {"vcrane": 42},
                    {"vcrane": None}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Aliases(bad)

    def test_no_map_at_all_is_a_valid_empty_index(self):
        # An operator who has registered no alias has an empty index, not a
        # broken one. `resolve` is given None by every caller that has no map.
        for empty in (None, {}):
            built = Aliases(empty)
            self.assertEqual(len(built), 0)
            # "vc" is too short to be a name in its own right, so nothing but
            # an alias could have resolved it -- which is what makes this a
            # test of the empty index rather than of the folder guard.
            self.assertIsNone(resolve("clip01.mp4", "vc", built).name)
            # and a folder that IS a name still resolves as a folder, not as
            # an alias, so an empty index withholds rather than blocks
            self.assertEqual(resolve("clip01.mp4", "Velvet Crane", built).source,
                             "folder")

    def test_two_indexes_built_from_the_same_map_are_equal(self):
        self.assertEqual(Aliases(self.MAP), Aliases(dict(self.MAP)))
        self.assertNotEqual(Aliases(self.MAP), Aliases({"vc": "Velvet Crane"}))
        self.assertNotEqual(Aliases(self.MAP), self.MAP)

    def test_it_does_not_print_the_operators_map(self):
        # The keys are an operator's own folder names and the values are
        # people's names. A repr reaches log lines and tracebacks, so it
        # carries the size and nothing else.
        text = repr(Aliases(self.MAP))
        self.assertIn("2", text)
        for secret in ("vcrane", "Velvet Crane", "V-Wren", "Copper Wren"):
            self.assertNotIn(secret, text)


def _owners_of(script):
    """A fake `owners_of` collaborator, for a test to pass to `resolve`: no
    search runs, no socket opens. `script` maps a candidate name to the list
    of owner names its (fake) search would return; a name with no entry
    answers `[]`, matching a real search that genuinely found nothing.

    Calls are recorded on `.calls` so a test can assert exactly which
    candidates were checked -- in particular, that the SINGLE-candidate case
    never calls this at all (see `EvidenceBackedResolution.
    test_a_single_candidate_never_spends_a_lookup`), which is the cost bound
    `_resolve_by_evidence` promises.
    """
    calls = []

    def owners_of(name):
        calls.append(name)
        return list(script.get(name, []))

    owners_of.calls = calls
    return owners_of


class EvidenceBackedResolution(unittest.TestCase):
    """`resolve(..., owners_of=...)` -- the fix for issue #66: a file named
    "<store> - <creator> - <title>", filed under a folder named for the
    store, used to resolve to the store, because the store's own name is
    just as plausible a *shape* as the creator's. `owners_of` lets `resolve`
    check a candidate against the catalogue instead of assuming the first
    one -- see `_resolve_by_evidence`.

    "Amberlight" stands in for the measured store, "Wren Ashcombe" for the
    measured creator; both invented, matching no real store or performer.
    """

    FILENAME = "Amberlight - Wren Ashcombe - Morning Session.mp4"

    def test_a_supported_deeper_candidate_beats_an_unsupported_folder(self):
        # HARM this fixes: without a search, the folder ("Amberlight", the
        # store) would win by the old default, exactly the wrong attribution
        # the live scan produced.
        owners_of = _owners_of({
            "Amberlight": [],
            "Wren Ashcombe": ["Wren Ashcombe"] * 19,
        })

        r = resolve(self.FILENAME, "Amberlight", owners_of=owners_of)

        self.assertEqual(r.name, "Wren Ashcombe")
        self.assertEqual(r.source, "filename")
        # A resolved winner is not an unresolved candidate -- `unconfirmed`
        # is reserved for the refusal path and must stay empty here.
        self.assertEqual(r.unconfirmed, ())

    def test_the_losing_candidate_is_reported_not_dropped(self):
        owners_of = _owners_of({
            "Amberlight": [],
            "Wren Ashcombe": ["Wren Ashcombe"] * 19,
        })

        r = resolve(self.FILENAME, "Amberlight", owners_of=owners_of)

        self.assertEqual(r.competing, "Amberlight")
        # It competed and lost on evidence, not on a guard -- so it is NOT
        # also reported as a rejected folder (see `Resolution`'s docstring).
        self.assertIsNone(r.rejected_folder)

    def test_the_folder_can_still_win_when_the_catalogue_backs_it(self):
        owners_of = _owners_of({
            "Amberlight": ["Amberlight", "Amberlight"],
            "Wren Ashcombe": [],
        })

        r = resolve(self.FILENAME, "Amberlight", owners_of=owners_of)

        self.assertEqual(r.name, "Amberlight")
        self.assertEqual(r.source, "folder")
        self.assertEqual(r.competing, "Wren Ashcombe")

    def test_zero_supported_candidates_is_unresolved_not_a_guess(self):
        # HARM this guards: falling back to "take the first segment anyway"
        # here would silently reproduce the exact bug this task fixes,
        # just relabelled as "nothing was supported".
        owners_of = _owners_of({"Amberlight": [], "Wren Ashcombe": []})

        r = resolve(self.FILENAME, "Amberlight", owners_of=owners_of)

        self.assertIsNone(r.name)
        self.assertIsNone(r.source)
        # both candidates were actually checked -- neither was skipped nor
        # was a decision made without asking
        self.assertEqual(set(owners_of.calls), {"Amberlight", "Wren Ashcombe"})
        # HARM this specifically guards against: without `unconfirmed`, a
        # caller sees `name=None, rejected_folder=None` -- indistinguishable
        # from "nothing in the folder or filename even looked like a name" --
        # when in fact TWO plausible names were found and checked. Folder
        # first, matching the order they were built and asked in.
        self.assertEqual(r.unconfirmed, ("Amberlight", "Wren Ashcombe"))
        self.assertIsNone(r.rejected_folder)

    def test_two_supported_candidates_is_unresolved_not_picked_by_order(self):
        # Ambiguity is reported, never resolved by iteration order: two
        # DIFFERENT names the catalogue each separately confirm is a
        # genuine conflict, not a tie to break by which was folder and
        # which was filename.
        filename = "Amberlight - Rowantide - Morning Session.mp4"
        owners_of = _owners_of({
            "Amberlight": ["Amberlight"],
            "Rowantide": ["Rowantide"],
        })

        r = resolve(filename, "Amberlight", owners_of=owners_of)

        self.assertIsNone(r.name)
        self.assertIsNone(r.source)
        # Both were actually confirmed, not merely offered -- a different
        # reason from the zero-supported case above, and `unconfirmed`
        # deliberately does not distinguish them (see
        # `_resolve_by_evidence`'s own docstring): both are "this run could
        # not settle on one", which is the fact a refusal needs to report.
        self.assertEqual(r.unconfirmed, ("Amberlight", "Rowantide"))

    def test_a_single_candidate_never_spends_a_lookup(self):
        # The common, unambiguous file: folder and filename agree, so there
        # is nothing to check. This is the cost bound -- a search-backed
        # resolve() must not cost every file a lookup just because it CAN.
        owners_of = _owners_of({})

        r = resolve("Wren Ashcombe - Morning Session.mp4", "Wren Ashcombe",
                     owners_of=owners_of)

        self.assertEqual(r.name, "Wren Ashcombe")
        self.assertEqual(owners_of.calls, [])

    def test_an_extended_name_resolves_with_real_support(self):
        # "Wren" is a short form of "Wren Ashcombe"; a PREFIX match, trusted
        # only with more than one supporting result (see MIN_PREFIX_SUPPORT).
        filename = "Amberlight - Wren - Morning Session.mp4"
        owners_of = _owners_of({
            "Amberlight": [],
            "Wren": ["Wren Ashcombe", "Wren Ashcombe"],
        })

        r = resolve(filename, "Amberlight", owners_of=owners_of)

        self.assertEqual(r.name, "Wren")
        self.assertEqual(r.source, "filename")

    def test_a_single_prefix_hit_is_a_fluke_not_support(self):
        filename = "Amberlight - Wren - Morning Session.mp4"
        owners_of = _owners_of({
            "Amberlight": [],
            "Wren": ["Wren Ashcombe"],
        })

        r = resolve(filename, "Amberlight", owners_of=owners_of)

        self.assertIsNone(r.name)

    def test_owners_of_absent_keeps_the_old_folder_wins_default(self):
        # No search collaborator at all: resolve() stays the pure function
        # it always was. Without one, only the FIRST dash segment is ever
        # read from the filename ("Amberlight" -- see `_filename_candidate`),
        # which here agrees with the folder, so there is no competitor to
        # report either; this is the exact pre-existing behaviour this task
        # leaves untouched for every caller that has no search to give.
        r = resolve(self.FILENAME, "Amberlight")

        self.assertEqual(r.name, "Amberlight")
        self.assertEqual(r.source, "folder")
        self.assertIsNone(r.competing)
        self.assertEqual(r.unconfirmed, ())
