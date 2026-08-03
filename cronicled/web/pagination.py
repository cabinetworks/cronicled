"""Bounding how many rows a single render puts in front of a browser.

Measured on a real library: the server answers in under a second and still
hands the browser eighty-eight thousand DOM elements, because every page
renders one row per waiting proposal with no bound at all. The browser, not
the server, is what stalls on that -- see the ticket this module exists
for. Nothing here changes what a query costs; it changes how much of one
render's answer reaches the template.

`PAGE_SIZE` is shared by every bounded section on every page, on purpose:
one constant that stays a bound no matter how large a library grows,
rather than a page's own row count multiplying every section's width by
the same factor. A section built directly from the store (the generic row
list, and a per-inbox terminal-state page) asks for its window with
`limit`/`offset` and never fetches the rest at all -- see
`cronicled.store.Store.item_count`'s own docstring for why that total is a
separate query rather than `len(store.items(...))`. A section assembled in
Python from more than one store read (a tag merge, a tag/performer
reconciliation, or a low-count-tag group -- each already reads across
several states; see `cronicled.__main__`'s own `_merge_rows` and
neighbours) is windowed here instead, over the list it already had to
build in full to get its own deterministic order right; that list was
always fetched whole before this ticket, so windowing it here adds no new
cost, it only bounds what gets converted into HTML.

A ROW BUILT FOR ONE PAGE STAYS THE PAGE'S OWN, addressable object, never
summarised away. Every row type this project renders (`Row`,
`DescriptionRow`, `TagDescriptionRow`, `MergeRow`, `ReconcileRow`,
`UnusedTagRow`) already carries its own `fingerprint`, and the window
functions here never do anything but slice a list -- so "the fingerprints
this page rendered" is always recoverable, without a second query, as
`[row.fingerprint for row in <the list handed to the template>]`. That
matters for more than this ticket: a future control that lets a person act
on several rows at once has to submit exactly the fingerprints a page
showed (the same guarantee `web.actions.Actions
.bulk_apply_tag_descriptions` already holds for one section, generalised),
and that submission is round-tripped through the rendered form itself
-- through hidden fields carrying each shown row's own fingerprint, the
same shape `inbox.html`'s existing bulk form already uses -- never
reconstructed later by re-running this same `limit`/`offset` against the
store, because the store can have changed in between and a re-run then
answers a different question than "what was this person shown".
"""


# Chosen well below where a browser stalls -- the measured 8,546-form,
# 88,000-element page was about 2,955 rows, so roughly 30 DOM nodes per row
# on average across every kind this page renders -- and well above the
# point where paginating itself becomes the chore: 200 rows is a long
# single scroll, not a dozen clicks to get through an evening's review.
PAGE_SIZE = 200


def page_number(raw):
    """`raw` (a query-string value, or `None`) as a 1-based page number.

    Anything that is not a positive integer -- missing, empty, non-numeric,
    zero, negative -- reads as page 1. A malformed or foreign `?page=` value
    must not 400 a page that would otherwise render fine, and must not
    silently wrap onto some other page nobody asked for; page 1 is the same
    page a bare `/inbox` already shows today.
    """
    if raw is None:
        return 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def total_pages(total, page_size=PAGE_SIZE):
    """How many pages of `page_size` it takes to cover `total` rows.

    At least 1 even when `total` is 0 -- an empty section is still "page 1
    of 1", never "page 1 of 0", which would leave a pager computing a
    negative last page.
    """
    if total <= 0:
        return 1
    return -(-total // page_size)  # ceil division without importing math


def offset_for(page, page_size=PAGE_SIZE):
    """The store `OFFSET` a 1-based `page` corresponds to."""
    return (page - 1) * page_size


def window(full_list, page, page_size=PAGE_SIZE):
    """`full_list`, sliced to `page`'s own window.

    For a section that is assembled whole in Python before it can be
    ordered or grouped at all -- see this module's own docstring for which
    sections that is and why they differ from the generic row list, which
    asks the store for a window directly instead of calling this.
    """
    start = offset_for(page, page_size)
    return full_list[start:start + page_size]
