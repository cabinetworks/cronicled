import unittest

from cronicled.text import (normalize, spaceless, strip_html, strip_ext,
                            clean_folder, tokens, slug_match)
from cronicled.vocab import CONTAINER_EXTS, ENCODE_MARKERS, SCAN_EXTS
from tests.fixtures.cast import PERFORMERS, TITLES


# -- what these two lists are, stated independently of the module ---------- #
#
# Both are restated here rather than read from `cronicled.vocab`, and the tests
# below drive their loops from THESE. A loop over the constant shrinks with the
# constant: drop an entry and the loop simply stops visiting it, so the suite
# goes on passing while a container quietly becomes title text again. Measured
# — deleting `.divx` and `.xvid`, or the whole transport-stream row, left an
# earlier version of this file green.
#
# Restating them is a check rather than a mirror because these are external
# facts: the spellings a container arrives under in a real library. Both
# directions of an edit are then deliberate, which is what each list wants.
# Dropping an entry from the broad one reintroduces the false negative the
# split exists to remove; adding one STRENGTHENS a claim — "definitely not part
# of a title" — and that claim is what lets a match bypass the threshold.
WALKED = frozenset({".mp4", ".mkv", ".wmv", ".avi", ".mov", ".m4v", ".flv",
                    ".ts", ".webm"})
NOT_TITLE_TEXT = WALKED | frozenset({
    ".mpeg", ".mpg", ".mpe", ".m1v", ".m2v", ".mpv", ".mp2",
    ".m2ts", ".mts", ".vob", ".ogv", ".ogm", ".asf",
    ".divx", ".xvid", ".rm", ".rmvb",
    ".3gp", ".3g2", ".f4v", ".qt", ".dv", ".mxf", ".wtv", ".amv",
})

# -- the encode markers, restated for the same reason and a sharper one ---- #
#
# This list decides what gets DELETED from a creator's name, so both directions
# of an edit have to be deliberate. A loop driven from the constant visits
# exactly the entries that are in it: one silently dropped stops being tested,
# and one silently added is never seen at all — and an added entry is a
# spelling this project now removes from the end of every folder that has it.
#
# These are external facts too: the spellings an encode marker arrives under in
# a real library. What is NOT here is as load-bearing as what is — "hd", "sd",
# "mp3", "flac" and "opus" are left out because each is also an ordinary word
# or an audio container, and a name is not worth a marker.
ENCODE_TOKENS = frozenset({
    "h264", "h265", "h266", "x264", "x265", "x266", "hevc", "avc",
    "8bit", "10bit", "12bit",
    "240p", "360p", "480p", "576p", "720p", "1080p", "1440p", "2160p",
    "4k", "8k", "uhd",
    "sdr", "hdr",
    "webdl", "bluray", "remux",
    "aac", "ac3", "eac3", "dts", "truehd",
})


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

    def test_empty_and_none_are_empty(self):
        # The anomalous case ticket #24 flags: this helper used to return
        # its input unchanged (None stayed None) while `normalize` and
        # `clean_folder` both already returned "". Nothing in the codebase
        # relied on getting None back — every caller that could pass one
        # already guarded with `x or ""` before this helper ever saw it —
        # so unifying the contract here changes no caller's behaviour.
        self.assertEqual(strip_html(""), "")
        self.assertEqual(strip_html(None), "")


class StripExt(unittest.TestCase):
    def test_drops_a_video_extension(self):
        self.assertEqual(strip_ext("Copper Kettle.mp4"), "Copper Kettle")

    def test_keeps_a_non_video_suffix(self):
        self.assertEqual(strip_ext("Copper Kettle Vol.2"), "Copper Kettle Vol.2")

    def test_it_reads_the_broad_list_and_not_the_walkers_one(self):
        """The whole point of the split. These are containers nothing would
        spend a walk on, and every one of them left on a name is a token the
        matcher weighs as though it were part of the title.

        Driven from the table below rather than from `CONTAINER_EXTS`: a loop
        over the constant shrinks when the constant does, so it can only ever
        report that the code agrees with itself.
        """
        for ext in sorted(NOT_TITLE_TEXT - WALKED):
            self.assertEqual(strip_ext("Copper Kettle" + ext), "Copper Kettle",
                             ext)

    def test_the_case_a_filename_is_written_in_does_not_decide_this(self):
        self.assertEqual(strip_ext("Copper Kettle.MPEG"), "Copper Kettle")

    def test_a_suffix_on_neither_list_survives(self):
        # The uncertain half, which `scoring` handles separately and more
        # cautiously. A suffix this list does not claim is not confidently a
        # container, so nothing here may delete it.
        for name in ("Copper Kettle.zqx", "Copper Kettle.vidx",
                     "Copper Kettle.Extended"):
            self.assertEqual(strip_ext(name), name)

    def test_empty_and_none_are_empty(self):
        # The other anomalous case ticket #24 flags: this helper used to
        # raise TypeError on None (os.path.splitext(None) does not accept
        # it), which every caller that could see a None here already worked
        # around with an explicit `name or ""` (cronicled.artist,
        # cronicled.dates). Now it agrees with `normalize` and
        # `clean_folder` directly, and those guards become redundant rather
        # than load-bearing.
        self.assertEqual(strip_ext(""), "")
        self.assertEqual(strip_ext(None), "")


class TheTwoExtensionLists(unittest.TestCase):
    """One list used to answer both "which files does a walk open" and "which
    trailing token is not title text". They pull opposite ways: the first is
    paid for in work per entry and wants to stay small, the second is paid for
    in false negatives per OMISSION and wants to be exhaustive. While they were
    one list, widening it for the second question widened the first with it,
    so it was never widened."""

    def test_everything_worth_scanning_is_also_known_not_to_be_a_title(self):
        # This direction is sound and is why one list is built from the other.
        self.assertTrue(SCAN_EXTS < CONTAINER_EXTS)

    def test_the_walkers_list_stays_the_conservative_one(self):
        # An entry added here is a file every future walk opens, so it is a
        # decision to make on purpose rather than by widening the list beside
        # it. Asserted whole: a sampled check cannot see an entry ADDED.
        self.assertEqual(SCAN_EXTS, WALKED)

    def test_the_broad_list_holds_every_container_it_claims(self):
        # Also whole, and for both reasons at once. An entry silently lost is
        # a file scored differently from its own twin in another container; an
        # entry silently gained is a spelling this project now asserts can
        # never be part of a title, which is what grants a bypass of the
        # threshold.
        self.assertEqual(CONTAINER_EXTS, NOT_TITLE_TEXT)

    def test_neither_list_reaches_beyond_video_containers(self):
        # Audio, images and subtitles are a further question about which files
        # are in scope at all. Answering it inside either of these lists is
        # how one list came to answer two.
        for ext in (".mp3", ".flac", ".wav", ".jpg", ".png", ".webp",
                    ".srt", ".vtt", ".ass", ".nfo", ".txt"):
            self.assertNotIn(ext, CONTAINER_EXTS, ext)
            self.assertNotIn(ext, NOT_TITLE_TEXT, ext)

    def test_every_entry_is_a_dotted_lowercase_suffix(self):
        # `strip_ext` compares against `os.path.splitext`'s output lowercased,
        # so an entry without its dot, or with a capital, is an entry that can
        # never match and reads as coverage that does not exist.
        for ext in sorted(NOT_TITLE_TEXT):
            self.assertEqual(ext, ext.lower(), ext)
            self.assertTrue(ext.startswith("."), ext)
            self.assertNotIn(".", ext[1:], ext)


class CleanFolder(unittest.TestCase):
    def test_strips_a_parenthesised_qualifier(self):
        self.assertEqual(clean_folder("%s (h265)" % PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_strips_a_bracketed_qualifier(self):
        self.assertEqual(clean_folder("%s [1080p]" % PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_leaves_an_unqualified_name_alone(self):
        self.assertEqual(clean_folder(PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_strips_a_bracketed_qualifier_that_is_no_kind_of_encode_marker(self):
        # The bracketed rule's own test, using text the marker list does not
        # claim, so it goes on failing if the brackets stop being read even
        # once every marker inside brackets is stripped by the other rule.
        self.assertEqual(clean_folder("%s (Archive)" % PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_empty_and_none_are_empty(self):
        self.assertEqual(clean_folder(""), "")
        self.assertEqual(clean_folder(None), "")


class CleanFolderStripsBareEncodeMarkers(unittest.TestCase):
    """The bracketed rule above handles "(h265)". The identical marker with no
    brackets is the more common filing shape by far, and it used to pass
    straight through every guard and be recorded as the creator's name — a
    name no store has, on more than half the scenes of one measured library.

    The other direction is the dangerous one and it is silent. A folder left
    with a marker on it is visibly wrong; a name quietly shortened by one
    token still reads as a name, so nothing downstream ever questions it.
    """

    def test_strips_a_bare_marker(self):
        self.assertEqual(clean_folder("%s x265" % PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_strips_every_marker_the_list_claims(self):
        # Driven from the table at the top of this file, not from
        # ENCODE_MARKERS: a loop over the constant shrinks when the constant
        # does and can only ever report that the code agrees with itself.
        for marker in sorted(ENCODE_TOKENS):
            self.assertEqual(
                clean_folder("%s %s" % (TITLES["contained"], marker)),
                TITLES["contained"], marker)

    def test_a_trailing_token_the_list_does_not_claim_survives_untouched(self):
        # The test this class exists for. Every one of these is a shape a
        # pattern would eat: an initial, a numeral that belongs to the name, a
        # token merely SHAPED like a codec, an ordinary word, a one-word
        # handle standing alone.
        for folder in ("Velvet Crane J", "Copper Kettle 3", "Velvet Crane x999",
                       "Copper Kettle Remastered", "Marsh"):
            self.assertEqual(clean_folder(folder), folder, folder)

    def test_a_marker_leading_the_name_is_not_stripped(self):
        folder = "1080p %s" % PERFORMERS["two_word"]
        self.assertEqual(clean_folder(folder), folder)

    def test_a_marker_in_the_middle_of_the_name_is_not_stripped(self):
        # Something put it between two pieces of the name, so it is not a
        # qualifier tacked on the end and this leaves it alone.
        folder = "Velvet 1080p Crane"
        self.assertEqual(clean_folder(folder), folder)

    def test_strips_a_run_of_trailing_markers(self):
        # Two markers, and two different ones: a fixture of one cannot tell
        # "removes the run" from "removes exactly one".
        self.assertEqual(clean_folder("%s 1080p x265" % PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_the_run_stops_at_the_first_token_the_list_does_not_claim(self):
        # Three tokens after the name and only the last removable: the run
        # must not step over the word to reach the marker behind it.
        self.assertEqual(clean_folder("Copper Kettle x265 Archive 1080p"),
                         "Copper Kettle x265 Archive")

    def test_a_marker_is_recognised_however_it_is_punctuated_or_cased(self):
        # One list entry per marker, not one per spelling: the separators a
        # marker is written with are folded out before the lookup.
        for folder in ("%s H.265", "%s WEB-DL", "%s 10-Bit", "%s Blu_Ray"):
            self.assertEqual(clean_folder(folder % PERFORMERS["two_word"]),
                             PERFORMERS["two_word"], folder)

    def test_a_marker_glued_to_a_neighbour_is_not_a_whole_token(self):
        # Whole WHITESPACE-separated tokens only. Widening the split to the
        # separators a name is also written with is how a hyphenated name
        # loses half of itself and a dot-separated one loses its surname.
        for template in ("%s-1080p", "%s.1080p", "%s_1080p"):
            folder = template % PERFORMERS["two_word"]
            self.assertEqual(clean_folder(folder), folder, folder)

    def test_a_token_carrying_a_stray_bracket_is_not_a_bare_marker(self):
        # An unbalanced bracket is no qualifier the other rule can read, and
        # the token it leaves is not the bare marker this one claims. Brackets
        # are deliberately not folded away here: were they, a marker in
        # brackets would be strippable by EITHER rule and neither would fail
        # on its own when the other was removed.
        folder = "%s (h265" % PERFORMERS["two_word"]
        self.assertEqual(clean_folder(folder), folder)

    def test_a_bracketed_qualifier_goes_before_the_trailing_run_is_read(self):
        # A bare marker with a bracketed year after it: the brackets come off
        # first, which is what puts the marker at the end where this rule can
        # see it. Reading the run first leaves the marker in the name.
        self.assertEqual(clean_folder("%s 1080p (2024)" % PERFORMERS["two_word"]),
                         PERFORMERS["two_word"])

    def test_a_folder_that_is_nothing_but_a_bare_marker_cleans_to_nothing(self):
        # The same answer the bracketed rule has always given for "(h265)",
        # and what `artist.creator_folder` reads to walk past such a directory
        # instead of attributing a creator to it.
        self.assertEqual(clean_folder("x265"), "")
        self.assertEqual(clean_folder("2160p HEVC"), "")


class TheEncodeMarkerList(unittest.TestCase):
    """A closed list, for the reason CONTAINER_EXTS is a closed list and with
    the stakes reversed: an omission here leaves a marker visible on a name,
    while an addition silently deletes a token from every folder that ends in
    it. The cheaper mistake is the visible one."""

    def test_it_holds_exactly_what_it_claims(self):
        # Asserted whole, both directions. Sampling cannot see an entry ADDED,
        # and an added entry is the one that costs a name.
        self.assertEqual(ENCODE_MARKERS, ENCODE_TOKENS)

    def test_every_entry_is_a_bare_lowercase_alphanumeric_token(self):
        # A token is lowercased and has `.`, `-` and `_` folded out before it
        # is looked up here, so an entry carrying any of those — or a capital,
        # or a space, or no characters at all — can never match, and reads as
        # coverage that does not exist.
        for marker in sorted(ENCODE_TOKENS):
            self.assertEqual(marker, marker.lower(), marker)
            self.assertTrue(marker.isalnum(), marker)

    def test_it_claims_nothing_that_could_be_part_of_a_name(self):
        # The first three are name text. The rest are encode-ADJACENT and left
        # out on purpose: each is also an ordinary word or an audio container,
        # and this list may only hold spellings that could not be a name.
        for token in ("crane", "marsh", "kettle",
                      "hd", "sd", "mp3", "flac", "opus", "web", "ray", "bit"):
            self.assertNotIn(token, ENCODE_MARKERS, token)


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
        self.assertEqual(normalize("café naïve"), "cafe naive")

    def test_preserves_non_latin_letters(self):
        # deleting them would give a non-Latin name an empty slug, which
        # matches nothing at all
        self.assertEqual(normalize("Мир"), "мир")

    def test_still_collapses_punctuation(self):
        self.assertEqual(normalize("Velvet.Crane - Copper_Kettle"),
                         "velvet crane copper kettle")

    def test_accented_and_unaccented_forms_match(self):
        # the accent sits mid-word, so plain deletion (the old behaviour)
        # breaks the substring containment slug_match relies on; this only
        # passes once accents fold to their base letter first
        self.assertTrue(slug_match("naïve", "naive"))


class NormalizeNonDecomposableLetters(unittest.TestCase):
    """NFKD does not decompose these into a base letter plus a combining
    mark, so `normalize` folds them through an explicit translation table
    instead. Verified counterexamples: Polish l-with-stroke, Scandinavian
    o-with-stroke, the ae/German sharp-s ligatures, and d-with-stroke."""

    def test_folds_l_with_stroke(self):
        self.assertEqual(normalize("łŁ"), "ll")

    def test_folds_o_with_stroke(self):
        self.assertEqual(normalize("øØ"), "oo")

    def test_folds_ae_ligature(self):
        self.assertEqual(normalize("æÆ"), "aeae")

    def test_folds_german_sharp_s(self):
        self.assertEqual(normalize("ß"), "ss")

    def test_folds_d_with_stroke(self):
        self.assertEqual(normalize("đĐ"), "dd")

    def test_non_latin_script_still_preserved_not_transliterated(self):
        self.assertEqual(normalize("Мир"), "мир")

    def test_folds_an_accented_compound_of_a_mapped_letter(self):
        # o-with-stroke-and-acute: NFKD decomposes this into o-with-stroke
        # plus a combining acute *before* the table ever sees a bare letter,
        # so the fold has to happen after the combining-mark strip, not before
        self.assertEqual(normalize("ǿ"), "o")

    def test_folds_a_second_accented_compound_of_a_mapped_letter(self):
        # ae-with-acute: same mechanism, a different mapped letter (ae)
        self.assertEqual(normalize("ǽ"), "ae")


class NormalizeDeliberateNonFold(unittest.TestCase):
    def test_turkish_dotless_i_is_preserved_not_folded_to_ascii_i(self):
        # dotted and dotless i are distinct letters in Turkish; folding one to
        # the other would be a guess, not a normalization, so it is left alone
        self.assertEqual(normalize("ı"), "ı")


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
