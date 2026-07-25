"""Which creator a file belongs to scopes every later decision, so these tests
state the wrong attribution each rule prevents."""
import unittest

from cronicled.artist import CONTAINER_NAMES, creator_folder


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
