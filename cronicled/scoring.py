"""Score a candidate catalogue title against a media filename.

This score decides, downstream, whether metadata gets written to a user's
library without a human reviewing it -- so every rule here exists to prevent
a specific wrong match, not just to produce a plausible-looking number.
"""
import difflib
import re
from typing import NamedTuple, Optional

from cronicled.text import clean_folder, normalize, strip_ext, tokens

# A leading "Artist Name - Clip Title" filename repeats the series/artist name
# before the actual title, which drags recall and similarity down when
# compared against a bare title. Detected as the first " - " (or " -- ")
# separator, surrounded by whitespace, found anywhere in the name: everything
# before it is treated as the series prefix and dropped. Only the first such
# separator counts as the prefix boundary -- a title that legitimately
# contains " - " again later (e.g. "Part One - Extra") keeps that part intact
# in the stripped view.
_SERIES_SEP_RE = re.compile(r"\s+-{1,2}\s+")


class Match(NamedTuple):
    value: float
    contained: bool
    meaningful_count: int


def _without_series_prefix(name):
    """Strip a leading 'Series - ' prefix from a stripped-extension filename,
    if present. Returns None when there's nothing to strip (no separator, the
    separator is at the very start, or nothing remains after it), so callers
    can tell "no prefix" apart from "prefix stripped to an empty string"."""
    match = _SERIES_SEP_RE.search(name)
    if not match or match.start() == 0:
        return None
    rest = name[match.end():].strip()
    return rest or None


def meaningful_tokens(name, folder, artist=None):
    """Tokens left after dropping stopwords, junk, and the artist's own name --
    the evidence that can actually distinguish one title from another."""
    result = set(tokens(strip_ext(name))) | set(tokens(clean_folder(folder)))
    if artist:
        result -= set(tokens(artist))
    return result


def _recall(view_tokens, title_tokens):
    if not view_tokens:
        return 0.0
    title_set = set(title_tokens)
    hits = sum(1 for t in view_tokens if t in title_set)
    return hits / len(view_tokens)


def _similarity(view_norm, title_norm):
    return difflib.SequenceMatcher(None, view_norm, title_norm).ratio()


def _view_score(view_raw, title_norm, title_tokens):
    view_norm = normalize(view_raw)
    view_tokens = tokens(view_raw)
    recall = _recall(view_tokens, title_tokens)
    similarity = _similarity(view_norm, title_norm)
    return 0.7 * recall + 0.3 * similarity


def score(name, folder, title, artist=None):
    stripped_name = strip_ext(name)
    cleaned_folder = clean_folder(folder)
    title_norm = normalize(title)
    title_tokens = tokens(title)

    views = [stripped_name, cleaned_folder]
    prefix_stripped = _without_series_prefix(stripped_name)
    if prefix_stripped is not None:
        views.append(prefix_stripped)

    best = max(_view_score(view, title_norm, title_tokens) for view in views)

    meaningful = meaningful_tokens(name, folder, artist=artist)
    contained = len(meaningful) >= 2 and meaningful.issubset(set(title_tokens))
    if contained:
        best = max(best, 0.9)

    return Match(value=round(best, 3), contained=contained, meaningful_count=len(meaningful))
