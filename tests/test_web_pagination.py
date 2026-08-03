"""`cronicled.web.pagination` -- the shared page size and the pure window/
count arithmetic every bounded section builds on. See the module's own
docstring for why one shared bound exists at all, and `web.app._pager` for
where these are actually assembled into what a template reads.
"""
import unittest

from cronicled.web.pagination import (PAGE_SIZE, offset_for, page_number,
                                      total_pages, window)


class PageNumber(unittest.TestCase):
    def test_none_is_page_one(self):
        self.assertEqual(page_number(None), 1)

    def test_a_missing_value_the_caller_represents_as_an_empty_string(self):
        self.assertEqual(page_number(""), 1)

    def test_an_ordinary_value_parses(self):
        self.assertEqual(page_number("7"), 7)

    def test_a_non_numeric_value_is_page_one(self):
        self.assertEqual(page_number("nope"), 1)

    def test_zero_is_page_one(self):
        # Never page 0 -- there is no such page, and a caller building
        # `offset_for(0)` would get a NEGATIVE offset.
        self.assertEqual(page_number("0"), 1)

    def test_negative_is_page_one(self):
        self.assertEqual(page_number("-3"), 1)

    def test_a_float_shaped_string_is_page_one(self):
        # `int("2.5")` raises -- this must land on the same safe default as
        # any other unparseable value, not propagate the exception.
        self.assertEqual(page_number("2.5"), 1)

    def test_whitespace_padded_digits_still_parse(self):
        # `int()` itself tolerates this; asserted so a future rewrite that
        # switches to a stricter parse does not silently start rejecting it.
        self.assertEqual(page_number(" 4 "), 4)


class TotalPages(unittest.TestCase):
    def test_zero_rows_is_one_page(self):
        # Never zero pages -- a pager computing `page < 0` would be wrong.
        self.assertEqual(total_pages(0), 1)

    def test_negative_total_is_also_one_page(self):
        self.assertEqual(total_pages(-5), 1)

    def test_exactly_one_page_worth(self):
        self.assertEqual(total_pages(200, page_size=200), 1)

    def test_one_row_over_a_page_needs_a_second_page(self):
        # The ceiling, not the floor: 201 rows at 200/page is 2 pages, not 1.
        self.assertEqual(total_pages(201, page_size=200), 2)

    def test_a_page_size_short_of_a_second_full_page(self):
        self.assertEqual(total_pages(399, page_size=200), 2)

    def test_exactly_two_full_pages(self):
        self.assertEqual(total_pages(400, page_size=200), 2)

    def test_uses_the_default_page_size_when_not_given(self):
        self.assertEqual(total_pages(PAGE_SIZE + 1), 2)
        self.assertEqual(total_pages(PAGE_SIZE), 1)


class OffsetFor(unittest.TestCase):
    def test_page_one_is_offset_zero(self):
        self.assertEqual(offset_for(1, page_size=200), 0)

    def test_page_two_is_offset_one_page_size(self):
        self.assertEqual(offset_for(2, page_size=200), 200)

    def test_page_three(self):
        self.assertEqual(offset_for(3, page_size=50), 100)

    def test_uses_the_default_page_size_when_not_given(self):
        self.assertEqual(offset_for(2), PAGE_SIZE)


class Window(unittest.TestCase):
    def test_the_first_page_is_the_first_page_size_items(self):
        items = list(range(250))
        self.assertEqual(window(items, 1, page_size=100), list(range(100)))

    def test_the_second_page_continues_from_where_the_first_left_off(self):
        items = list(range(250))
        self.assertEqual(window(items, 2, page_size=100), list(range(100, 200)))

    def test_a_partial_final_page(self):
        items = list(range(250))
        self.assertEqual(window(items, 3, page_size=100), list(range(200, 250)))

    def test_a_page_past_the_end_is_empty_not_an_error(self):
        items = list(range(250))
        self.assertEqual(window(items, 99, page_size=100), [])

    def test_windowing_preserves_order_rather_than_re_sorting(self):
        items = [5, 3, 1, 4, 2]
        self.assertEqual(window(items, 1, page_size=3), [5, 3, 1])

    def test_every_item_is_reachable_across_every_page_exactly_once(self):
        items = list(range(250))
        pages = total_pages(len(items), page_size=100)
        seen = []
        for page in range(1, pages + 1):
            seen.extend(window(items, page, page_size=100))
        self.assertEqual(seen, items)


if __name__ == "__main__":
    unittest.main()
