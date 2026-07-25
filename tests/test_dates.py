"""Date extraction from free text (title/filename): ISO-ish numeric, the
spelled-out month forms, and all-numeric NN-NN-YYYY — resolved by value when
one component is > 12, else by the configured day/month order."""
import unittest

from cronicled.dates import (
    looks_like_date, parse_date, date_prefix_parts, date_prefix_title,
    date_prefix_date, has_date_prefix, unparsed_date_prefix, set_date_order,
    DATE_ORDERS, DEFAULT_DATE_ORDER,
)
from tests.fixtures.cast import PERFORMERS, TITLES


class DateOrderIsolated(unittest.TestCase):
    """Base class for every test case below. `_date_order` is process-wide
    module state (a deliberate choice in the ported design — one setting
    rather than a parameter threaded through every parser), so a test that
    changes it must not leak the change to whatever test happens to run next.
    Every subclass gets the order forced to the default on the way in AND
    restored on the way out, so isolation holds regardless of run order or
    an earlier test failing before its own cleanup runs."""

    def setUp(self):
        set_date_order(DEFAULT_DATE_ORDER)
        self.addCleanup(set_date_order, DEFAULT_DATE_ORDER)


class LooksLikeDate(DateOrderIsolated):
    def test_iso_and_numeric(self):
        self.assertTrue(looks_like_date("2023-09-11"))
        self.assertTrue(looks_like_date("2023.09.11"))
        self.assertTrue(looks_like_date("2023 09 11"))

    def test_spelled_out_forms(self):
        self.assertTrue(looks_like_date("2023 September 11"))
        self.assertTrue(looks_like_date("11 September 2023"))
        self.assertTrue(looks_like_date("September 11, 2023"))
        self.assertTrue(looks_like_date("11 Sep 2023"))

    def test_bare_month_is_not_a_date(self):
        # no day or year at all — detection is deliberately wider than
        # extraction, but a bare month is outside even that wider net.
        self.assertFalse(looks_like_date("September"))

    def test_ordinary_title_is_not_a_date(self):
        self.assertFalse(looks_like_date(TITLES["unrelated"]))

    def test_performer_name_is_not_a_date(self):
        self.assertFalse(looks_like_date(PERFORMERS["two_word"]))


class ParseDateIsoAndSpelled(DateOrderIsolated):
    def test_iso_numeric(self):
        self.assertEqual(parse_date("recording 2021-06-20"), "2021-06-20")
        self.assertEqual(parse_date("2021.06.20 backup"), "2021-06-20")

    def test_spelled_year_month_day(self):
        self.assertEqual(parse_date("2023 September 11 - archive"), "2023-09-11")

    def test_spelled_month_day_year(self):
        self.assertEqual(parse_date("clip from September 11, 2023"), "2023-09-11")

    def test_spelled_day_month_year(self):
        self.assertEqual(parse_date("11 September 2023 recording"), "2023-09-11")

    def test_abbreviated_and_dotted_month(self):
        self.assertEqual(parse_date("11 Sep. 2023"), "2023-09-11")
        self.assertEqual(parse_date("2023 Nov 30 recording"), "2023-11-30")

    def test_no_date(self):
        self.assertIsNone(parse_date("no date here"))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))

    def test_bare_month_no_day_or_year_is_not_a_date(self):
        self.assertIsNone(parse_date("September"))

    def test_iso_single_digit_day_is_zero_padded(self):
        self.assertEqual(parse_date("2021-06-5 clip"), "2021-06-05")

    def test_numeric_does_not_shadow_iso(self):
        # an ISO date elsewhere in the string still wins over a numeric shape
        self.assertEqual(parse_date("2021-06-20 clip"), "2021-06-20")

    def test_first_valid_date_wins_over_an_earlier_invalid_one(self):
        self.assertEqual(parse_date("13-13-2020 rip 25-12-2020"), "2020-12-25")


class ParseDateAllNumeric(DateOrderIsolated):
    def test_component_greater_than_12_resolves_regardless_of_order(self):
        # 25 can only be a day, whichever position it's in and whatever the
        # configured order is — no ambiguity, so no order dependence.
        for order in DATE_ORDERS:
            self.assertEqual(parse_date("25-12-2020 - clip", order=order),
                             "2020-12-25")
            self.assertEqual(parse_date("12-25-2020 - clip", order=order),
                             "2020-12-25")

    def test_ambiguous_numeric_uses_day_first_by_default(self):
        self.assertEqual(parse_date("05-12-2020 - clip"), "2020-12-05")
        self.assertEqual(parse_date("clip 03/04/2023"), "2023-04-03")

    def test_ambiguous_numeric_honours_month_first_order(self):
        self.assertEqual(parse_date("05-12-2020 - clip", order="mdy"), "2020-05-12")

    def test_ambiguous_numeric_skipped_when_order_is_none(self):
        self.assertIsNone(parse_date("05-12-2020 - clip", order="none"))

    def test_unambiguous_numeric_extracted_even_when_order_is_none(self):
        # 'none' refuses to guess; it does not discard a date that needs no guess.
        self.assertEqual(parse_date("25-12-2020 - clip", order="none"), "2020-12-25")

    def test_impossible_numeric_date_rejected(self):
        self.assertIsNone(parse_date("31-02-2020 - clip"))   # no such day in Feb
        self.assertIsNone(parse_date("13-13-2020 - clip"))   # neither can be a month

    def test_nineteen_hundreds_year_not_extracted(self):
        self.assertIsNone(parse_date("clip 1999-12-05"))
        self.assertIsNone(parse_date("25-12-1999 - clip"))
        self.assertIsNone(parse_date("1999 September 11"))

    def test_impossible_calendar_date_rejected(self):
        self.assertIsNone(parse_date("2021-02-30"))
        self.assertIsNone(parse_date("2023-02-31"))
        self.assertIsNone(parse_date("31 February 2023"))


class ParseDateSpaceSeparatedWholeString(DateOrderIsolated):
    def test_whole_string_iso(self):
        self.assertEqual(parse_date("2023 09 11"), "2023-09-11")

    def test_whole_string_numeric(self):
        self.assertEqual(parse_date("11 09 2023"), "2023-09-11")
        self.assertEqual(parse_date("05 12 2020"), "2020-12-05")

    def test_whole_string_honours_order(self):
        self.assertEqual(parse_date("05 12 2020", order="mdy"), "2020-05-12")
        self.assertIsNone(parse_date("05 12 2020", order="none"))

    def test_whole_string_impossible_date_rejected(self):
        self.assertIsNone(parse_date("2023 02 31"))

    def test_not_searched_inside_free_text(self):
        # three space-separated numbers mid-title are far likelier to be
        # unrelated numbers than a date, so only a string that IS a date in
        # its entirety is read this way.
        self.assertIsNone(parse_date("recording 2023 09 11 archive"))
        self.assertIsNone(parse_date("summary 05 12 2020 collection"))


class DateOrderSetting(DateOrderIsolated):
    """The process-wide default order, set once by the caller so the deep
    callers of parse_date don't each need the parameter threaded through."""

    def test_default_is_day_first(self):
        self.assertEqual(DEFAULT_DATE_ORDER, "dmy")

    def test_setting_order_changes_parse_default(self):
        set_date_order("mdy")
        self.assertEqual(parse_date("05-12-2020"), "2020-05-12")
        set_date_order("none")
        self.assertIsNone(parse_date("05-12-2020"))

    def test_explicit_argument_beats_the_setting(self):
        set_date_order("none")
        self.assertEqual(parse_date("05-12-2020", order="dmy"), "2020-12-05")

    def test_unknown_order_is_rejected(self):
        self.assertRaises(ValueError, set_date_order, "ymd")


class DatePrefixParts(DateOrderIsolated):
    def test_iso_date_prefix_splits_date_and_title(self):
        text = "2023-09-11 - %s" % TITLES["unrelated"]
        self.assertEqual(date_prefix_parts(text), ("2023-09-11", TITLES["unrelated"]))
        self.assertEqual(date_prefix_title(text), TITLES["unrelated"])
        self.assertEqual(date_prefix_date(text), "2023-09-11")

    def test_spelled_out_date_prefix_splits_date_and_title(self):
        text = "2023 September 11 - %s" % TITLES["unrelated"]
        self.assertEqual(date_prefix_parts(text),
                         ("2023 September 11", TITLES["unrelated"]))
        self.assertEqual(date_prefix_title(text), TITLES["unrelated"])
        self.assertEqual(date_prefix_date(text), "2023-09-11")

    def test_artist_prefix_is_not_a_date_prefix(self):
        text = "%s - %s" % (PERFORMERS["two_word"], TITLES["unrelated"])
        self.assertIsNone(date_prefix_parts(text))
        self.assertIsNone(date_prefix_title(text))

    def test_no_dash_is_not_a_prefix(self):
        self.assertIsNone(date_prefix_parts(TITLES["unrelated"]))


class HasDatePrefix(DateOrderIsolated):
    def test_date_prefixed_filename(self):
        self.assertTrue(has_date_prefix(
            "2023 September 11 - %s.mp4" % TITLES["unrelated"], ""))
        self.assertTrue(has_date_prefix(
            "2023-09-11 - %s.mp4" % TITLES["unrelated"], ""))

    def test_date_prefixed_folder(self):
        self.assertTrue(has_date_prefix(
            "%s.mp4" % TITLES["unrelated"], "2023 September 11 - clips"))

    def test_artist_prefix_is_not_a_date_prefix(self):
        self.assertFalse(has_date_prefix(
            "%s - %s.mp4" % (PERFORMERS["two_word"], TITLES["unrelated"]), ""))

    def test_no_date_at_all(self):
        self.assertFalse(has_date_prefix("%s.mp4" % TITLES["unrelated"], ""))


class UnparsedDatePrefix(DateOrderIsolated):
    """The date-shaped prefix is confidently stripped off the title even when
    it fails to parse, so that combination must not vanish silently."""

    def test_date_looking_prefix_that_fails_to_parse_is_reported(self):
        basename = "2021-02-30 - %s.mp4" % TITLES["unrelated"]   # no such day
        self.assertEqual(unparsed_date_prefix(basename, ""), "2021-02-30")

    def test_none_when_the_prefix_parses_fine(self):
        basename = "2023-09-11 - %s.mp4" % TITLES["unrelated"]
        self.assertIsNone(unparsed_date_prefix(basename, ""))

    def test_none_when_there_is_no_date_prefix_at_all(self):
        self.assertIsNone(unparsed_date_prefix("%s.mp4" % TITLES["unrelated"], ""))


if __name__ == "__main__":
    unittest.main()
