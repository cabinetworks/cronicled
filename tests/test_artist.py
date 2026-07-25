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

    def test_the_walk_is_bounded(self):
        # all containers to the root: fall back rather than walking to "/"
        got = creator_folder("/Videos/Clips/Media/Downloads/Uploads/clip01.mp4")
        self.assertEqual(got, "Uploads")

    def test_a_file_with_no_parent_yields_empty(self):
        self.assertEqual(creator_folder("clip01.mp4"), "")

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

    def test_an_unlisted_abbreviation_does_not_resolve_to_a_guess(self):
        # guessing what an abbreviation means is how files get attributed to
        # the wrong person; not resolving is the correct failure
        r = resolve("clip01.mp4", "vc", aliases={"vcrane": "Velvet Crane"})
        self.assertIsNone(r.name)
