"""Which creator a file belongs to scopes every later decision, so these tests
state the wrong attribution each rule prevents."""
import unittest

from cronicled.artist import CONTAINER_NAMES, creator_folder, resolve


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


class Aliases(unittest.TestCase):
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
