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

# `strip_ext` removes only the containers listed in VIDEO_EXTS -- shared
# vocabulary that also decides which files get scanned at all, so widening it
# would change unrelated behaviour. Any other container therefore survives into
# the token set and counts as evidence, inflating meaningful_count past the
# `< 2` trigger and switching the single-generic-word rule off: `Addict.mp4` is
# refused, `Addict.mpeg` applies. So drop a trailing extension-SHAPED suffix
# whatever the container: 2-5 alphanumerics after a dot with something in front
# of it, containing at least one letter. The letter requirement keeps a numeric
# part number intact ("Volume 2.10"), and the five-character ceiling keeps a
# real word intact ("Morning Ritual.Extended"). Applied once, not repeatedly --
# one trailing container is what a rename leaves behind, and looping would eat
# the tail of an ordinary dot-separated filename.
_EXT_SHAPED_RE = re.compile(r"^(?P<stem>.+)\.(?P<suffix>[A-Za-z0-9]{2,5})$")


class Match(NamedTuple):
    value: float
    contained: bool
    meaningful_count: int


class Decision(NamedTuple):
    match: Optional[Match]
    index: Optional[int]
    reason: str


# A one-generic-word match (e.g. a single common word shared with some other
# title) is not evidence of a real match on its own -- it must score very
# high before we trust it.
_GENERIC_WORD_THRESHOLD = 0.9

# Two eligible candidates within this margin of each other are a dilemma, not
# a decision -- refuse rather than silently pick whichever came first.
AMBIGUITY_MARGIN = 0.05


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


def _strip_unknown_ext(name):
    """Drop a trailing extension-shaped suffix `strip_ext` did not recognise,
    so a renamed container cannot pose as evidence. See `_EXT_SHAPED_RE`."""
    match = _EXT_SHAPED_RE.match(name)
    if not match or not any(c.isalpha() for c in match.group("suffix")):
        return name
    return match.group("stem")


def meaningful_tokens(name, folder, artist=None):
    """Tokens left after dropping stopwords, junk, and the artist's own name --
    the evidence that can actually distinguish one title from another."""
    result = (set(tokens(_strip_unknown_ext(strip_ext(name))))
              | set(tokens(clean_folder(folder))))
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

    # An empty string on either side is not a resemblance to be measured:
    # difflib rates two of them a perfect 1.0, which gave a blank candidate
    # title 0.3 against every file in the library -- eligible under any
    # threshold of 0.3 or below, and two blanks refused as the nonsense
    # "ambiguous: 0.300 vs 0.300". Skip such views rather than score them.
    scored = [_view_score(view, title_norm, title_tokens)
              for view in views if title_norm and normalize(view)]
    best = max(scored, default=0.0)

    meaningful = meaningful_tokens(name, folder, artist=artist)
    contained = len(meaningful) >= 2 and meaningful.issubset(set(title_tokens))
    if contained:
        best = max(best, 0.9)

    return Match(value=round(best, 3), contained=contained, meaningful_count=len(meaningful))


def _is_eligible(match, threshold):
    """A match is trustworthy enough to compete for the win. No meaningful
    token at all is barred outright, at any score; containment bypasses the
    threshold entirely (the scorer already vetted it); a single generic word
    needs near-certainty; everything else needs the threshold.

    The zero case is barred because the score and the evidence count are
    computed over different views of the name: the score sees the raw string,
    which still carries the artist's name, while `meaningful_tokens` subtracts
    it. So a file named after nobody but the artist scores 0.9+ on the
    artist's name alone and would take on the metadata of whichever of that
    artist's titles was offered. Zero evidence is not weak evidence."""
    if match.meaningful_count == 0:
        return False
    if match.contained:
        return True
    if match.meaningful_count < 2:
        return match.value >= _GENERIC_WORD_THRESHOLD
    return match.value >= threshold


def decide(matches, threshold=0.5):
    """Pick the one candidate confident enough to apply automatically, or
    refuse with a reason a person can act on. Never guesses between two
    plausible candidates -- ambiguity is refused, not resolved by picking
    whichever came first."""
    if not matches:
        return Decision(match=None, index=None, reason="no candidates offered")

    eligible = [(m.value, i, m) for i, m in enumerate(matches) if _is_eligible(m, threshold)]

    if not eligible:
        best = max(matches, key=lambda m: m.value)
        if best.meaningful_count == 0:
            reason = (
                "nothing to match on (meaningful_count=0): the name carries no "
                "word that is not the artist's or generic"
            )
        elif best.meaningful_count < 2 and best.value < _GENERIC_WORD_THRESHOLD:
            reason = (
                "best score %.3f rests on a single generic word "
                "(meaningful_count=%d); needs %.2f or above"
                % (best.value, best.meaningful_count, _GENERIC_WORD_THRESHOLD)
            )
        else:
            reason = "nothing above the threshold (%.2f); best score was %.3f" % (
                threshold, best.value,
            )
        return Decision(match=None, index=None, reason=reason)

    eligible.sort(key=lambda t: t[0], reverse=True)
    top_value, top_index, top_match = eligible[0]

    if len(eligible) > 1:
        runner_value = eligible[1][0]
        # Rounded to the same three places `score` rounds a value to, because
        # a raw float subtraction decides this by representation rather than
        # by intent: 0.900-0.850 comes out just OVER the margin and applies,
        # 0.850-0.800 comes out just under and refuses. Both are a gap of
        # 0.05, and the majority of the pairs `score` produces land on the
        # unsafe side.
        if round(top_value - runner_value, 3) <= AMBIGUITY_MARGIN:
            reason = "ambiguous: %.3f vs %.3f are too close to call" % (
                top_value, runner_value,
            )
            return Decision(match=None, index=None, reason=reason)

    reason = "chosen with score %.3f" % (top_value,)
    return Decision(match=top_match, index=top_index, reason=reason)
