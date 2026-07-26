"""Date extraction from free text (title/filename): ISO-ish numeric, the
spelled-out month forms, and all-numeric NN-NN-YYYY dates — resolved by value
when one component is > 12, else by the configured day/month order."""
import re
from datetime import date

from cronicled.text import clean_folder, strip_ext

# Whole-string date shapes — used to reject a date that sits where a performer
# name would otherwise be parsed (e.g. '2023 September 11 - Title'; a
# filesystem/library convention, not a performer). Covers ISO-ish numeric and
# spelled-out month forms.
# Public because `artist` needs the same month spellings for a guard of its
# own ("Sep 2023" is a filing convention, not a creator) and a second copy
# would drift from this one.
MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?")
_DATE_PATTERNS = (
    re.compile(r"^(?:19|20)\d{2}[-._/ ](?:0?[1-9]|1[0-2])[-._/ ](?:0?[1-9]|[12]\d|3[01])$"),
    re.compile(r"^(?:0?[1-9]|[12]\d|3[01])[-._/ ](?:0?[1-9]|1[0-2])[-._/ ](?:19|20)\d{2}$"),
    re.compile(r"^(?:19|20)\d{2}\s+(?:%s)\s+\d{1,2}$" % MONTH_PATTERN, re.I),
    re.compile(r"^(?:%s)\.?\s+\d{1,2},?\s+(?:19|20)\d{2}$" % MONTH_PATTERN, re.I),
    re.compile(r"^\d{1,2}\s+(?:%s)\.?\s+(?:19|20)\d{2}$" % MONTH_PATTERN, re.I),
)


def looks_like_date(text):
    """True when `text` is, in its entirety, a date — ISO-ish numeric (2023-09-11,
    2023 09 11) or a spelled-out month form (2023 September 11, 11 Sep 2023,
    September 11, 2023). A bare month with no day/year is NOT a date."""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return any(p.match(s) for p in _DATE_PATTERNS)


# First-3-letters -> month number (unique across all months, incl. 'may'/'sep').
_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}
# Extraction is intentionally narrower than looks_like_date's detection: only
# 20xx years are extracted (a 19xx date-shaped token in a media library is far
# likelier a coincidental number than a real release date), and an impossible
# calendar date is rejected.
_DAY = r"(0?[1-9]|[12]\d|3[01])"
_YEAR = r"(20\d{2})"
_ISO_SEARCH = re.compile(r"(?<!\d)%s[-._/](0[1-9]|1[0-2])[-._/]%s(?!\d)"
                         % (_YEAR, _DAY))                                 # 2021-06-20
_SPELLED_SEARCH = (  # (pattern, index of year/month-token/day groups)
    (re.compile(r"(?<!\d)%s\s+(%s)\.?\s+%s(?!\d)" % (_YEAR, MONTH_PATTERN, _DAY), re.I), (1, 2, 3)),
    (re.compile(r"\b(%s)\.?\s+%s,?\s+%s\b" % (MONTH_PATTERN, _DAY, _YEAR), re.I), (3, 1, 2)),
    (re.compile(r"(?<!\d)%s\s+(%s)\.?\s+%s\b" % (_DAY, MONTH_PATTERN, _YEAR), re.I), (3, 2, 1)),
)


# All-numeric NN-NN-YYYY (05-12-2020). Same separators as _ISO_SEARCH; the
# day/month order is worked out per-match by _numeric_date.
_NUMERIC_SEARCH = re.compile(r"(?<!\d)(\d{1,2})[-._/](\d{1,2})[-._/]%s(?!\d)" % _YEAR)
# Whole-string forms, matching what looks_like_date detects — including the SPACE
# separator the search patterns above deliberately exclude. A bare '2023 09 11'
# inside a title is far likelier to be three unrelated numbers than a date, but a
# string that is a date in its entirety (the 'DATE - Title' prefix position) is
# not a guess at all, so it is read here rather than dropped.
_WHOLE_ISO = re.compile(r"^%s[-._/ ](0?[1-9]|1[0-2])[-._/ ]%s$" % (_YEAR, _DAY))
_WHOLE_NUMERIC = re.compile(r"^(\d{1,2})[-._/ ](\d{1,2})[-._/ ]%s$" % _YEAR)
# How to read an all-numeric NN-NN-YYYY whose order is genuinely ambiguous, i.e.
# both leading components are <= 12. Day-first is the default because a media
# library's date-prefixed filenames overwhelmingly come from DD-MM-YYYY
# sources; the caller's configured order overrides it, and 'none' declines to
# guess.
DATE_ORDERS = ("dmy", "mdy", "none")
DEFAULT_DATE_ORDER = "dmy"
_date_order = DEFAULT_DATE_ORDER


def set_date_order(order):
    """Set the process-wide day/month order used for ambiguous all-numeric
    dates. This is process-wide module state, set once at startup, rather
    than a parameter threaded through every parser: a deliberate choice so
    the deep callers of `parse_date` don't each have to carry it."""
    global _date_order
    if order not in DATE_ORDERS:
        raise ValueError("date order must be one of %s, got %r"
                         % ("/".join(DATE_ORDERS), order))
    _date_order = order


def _iso(year, month, day):
    """Validated ISO 'YYYY-MM-DD' (zero-padded) or None for an impossible calendar
    date (e.g. Feb 31)."""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def date_prefix_parts(text):
    """('date prefix', 'Title') when `text` is a 'DATE - Title' shape, else None.
    The single source of the date/title-delimiter convention, shared by
    date_prefix_title (which wants the title), has_date_prefix (detection) and
    unparsed_date_prefix (which wants the date text back)."""
    s = re.sub(r"\s+", " ", re.sub(r"[+_]+", " ", text or "")).strip()
    if " - " in s:
        head, tail = s.split(" - ", 1)
        if looks_like_date(head.strip()):
            return head.strip(), tail.strip()
    return None


def date_prefix_title(text):
    """If `text` is a 'DATE - Title' shape, return the Title with the date prefix
    stripped; else None."""
    parts = date_prefix_parts(text)
    return parts[1] if parts else None


def has_date_prefix(basename, folder):
    """True when the filename or folder is a 'DATE - Title' shape (no
    performer to attribute; the right side is the real title)."""
    return any(date_prefix_title(src) is not None
               for src in (clean_folder(folder), strip_ext(basename or "")))


def date_prefix_date(text, order=None):
    """The date carried by a 'DATE - Title' prefix, else None. Parsing the prefix
    on its own (rather than searching the whole filename) is what lets the spaced
    forms through: there the string is known to be nothing but a date."""
    parts = date_prefix_parts(text)
    return parse_date(parts[0], order) if parts else None


def unparsed_date_prefix(basename, folder):
    """The 'DATE - Title' prefix of the filename/folder that looks like a date
    but yields no extractable date, else None. That combination is the silent
    data-loss case — the prefix is confidently stripped off the title, so it
    must not also vanish without a word; callers should state it in whatever
    they report back."""
    for src in (clean_folder(folder), strip_ext(basename or "")):
        parts = date_prefix_parts(src)
        if parts and not parse_date(parts[0]):
            return parts[0]
    return None


def _numeric_date(first, second, year, order):
    """ISO date for an all-numeric NN-NN-YYYY, or None. Order is decided by value
    wherever it can be — a component > 12 can only be the day — and only falls back
    to `order` when both are <= 12 and the shape is truly ambiguous."""
    a, b = int(first), int(second)
    if a > 12 and b > 12:
        return None                                   # neither can be a month
    if a > 12:
        day, month = a, b
    elif b > 12:
        day, month = b, a
    elif order == "mdy":
        day, month = b, a
    elif order == "dmy":
        day, month = a, b
    else:
        return None                                   # ambiguous, and told not to guess
    return _iso(year, month, day)


def parse_date(text, order=None):
    """First valid date found in `text` as ISO 'YYYY-MM-DD', else None. Recognizes
    ISO-ish numeric (2021-06-20, 2021.06.20), the spelled-out month forms (2023
    September 11, September 11 2023, 11 Sep 2023), and all-numeric NN-NN-YYYY
    (05-12-2020) — the last resolved by value when one component is > 12, else by
    `order` ('dmy'/'mdy'/'none', defaulting to the configured order; see
    set_date_order). Never guesses a year or an impossible calendar date.

    A string that is a date in its entirety may also separate its parts with
    spaces ('2023 09 11'); mid-title that shape is not read as a date."""
    if not text:
        return None
    order = order if order is not None else _date_order
    whole = re.sub(r"\s+", " ", text).strip()
    m = _WHOLE_ISO.match(whole)
    if m:
        iso = _iso(m.group(1), m.group(2), m.group(3))
        if iso:
            return iso
    m = _WHOLE_NUMERIC.match(whole)
    if m:
        iso = _numeric_date(m.group(1), m.group(2), m.group(3), order)
        if iso:
            return iso
    m = _ISO_SEARCH.search(text)
    if m:
        iso = _iso(m.group(1), m.group(2), m.group(3))
        if iso:
            return iso
    for rx, (yi, mi, di) in _SPELLED_SEARCH:
        m = rx.search(text)
        if m:
            iso = _iso(m.group(yi), _MONTH_NUM[m.group(mi)[:3].lower()], m.group(di))
            if iso:
                return iso
    for m in _NUMERIC_SEARCH.finditer(text):
        iso = _numeric_date(m.group(1), m.group(2), m.group(3), order)
        if iso:
            return iso
    return None
