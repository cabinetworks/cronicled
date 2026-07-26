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
    """The one candidate confident enough to apply, or a refusal.

    Two of these fields exist for a single consumer: a caller deciding whether
    a refusal is evidence that a file has no entry in the catalogue at all.
    `match=None` cannot answer that, because it covers at least four refusals
    and only one of them is a statement about the catalogue.

    `contenders` counts the candidates that were trustworthy enough to compete
    for the win. It separates "nothing cleared the bar" -- consistent with the
    file having no entry -- from "several did and which one is right could not
    be decided", where entries that look like this file are right there.

    `interrogated` says whether the catalogue was actually questioned at all.
    `contenders == 0` is also what comes back when the *filename* carried no
    word that is not the artist's or generic, which `_is_eligible` bars at any
    score so that not one candidate title was ever weighed, and when the
    *caller* offered no candidates. Those two are statements about the file
    and about the caller; neither says anything about the source, and an
    absence built on either is fabricated -- it sends a reviewer to hunt a
    mis-filing the scorer never looked for.

    Both are read as data and never off `reason` -- `scan.py` states that rule
    and the reason for it: the wording is free to change and nothing would
    notice.

    Neither has a default. 0 and True are not neutral values here: they are
    precisely the pair that licenses a downstream absence claim, so a Decision
    assembled without them must fail rather than assert that nothing competed
    or that a look happened.
    """
    match: Optional[Match]
    index: Optional[int]
    reason: str
    contenders: int
    interrogated: bool


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


def _evidence(name, folder, artist=None):
    """Both readings of the evidence: `kept` holds a trailing extension-shaped
    suffix, `stripped` drops it. Callers pick per use -- see `score`."""
    from_folder = set(tokens(clean_folder(folder)))
    base = strip_ext(name)
    kept = set(tokens(base)) | from_folder
    stripped = set(tokens(_strip_unknown_ext(base))) | from_folder
    if artist:
        from_artist = set(tokens(artist))
        kept -= from_artist
        stripped -= from_artist
    return kept, stripped


def meaningful_tokens(name, folder, artist=None):
    """Tokens left after dropping stopwords, junk, and the artist's own name --
    the evidence that can actually distinguish one title from another."""
    return _evidence(name, folder, artist=artist)[1]


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

    # A container listed in VIDEO_EXTS is confidently not content, so
    # `strip_ext` removes it outright and neither set below ever sees it. An
    # extension-SHAPED suffix we do not recognise might be a real word, so the
    # two sets disagree about it and each is asked only what it can safely
    # answer: uncertainty may withhold evidence, never supply it.
    #
    # `meaningful_count` reads the stripped set, so a renamed container cannot
    # inflate the count past the single-generic-word rule.
    #
    # `contained` reads the UN-stripped set, because containment is a subset
    # test and dropping a token can only make a set more of a subset. Judged on
    # the stripped set, discarding the one token that distinguishes the file
    # from a wrong title turns the remainder into a subset of that title --
    # floored to 0.9 and eligible without ever meeting the threshold.
    # "Morning Ritual.Dawn" against "Morning Ritual Dusk" is that shape, and
    # dot-separated naming makes it common. The count gate stays on the
    # stripped set, the strictly smaller of the two, so the strip can only ever
    # cost containment, never grant it.
    kept, meaningful = _evidence(name, folder, artist=artist)
    contained = len(meaningful) >= 2 and kept.issubset(set(title_tokens))
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


def _shortfall(match, threshold):
    """How far a rejected candidate was from being eligible -- so a refusal
    names the candidate that nearly qualified, not merely the highest-scoring
    one. Naming the wrong one points the user at renaming the file, a dead end,
    instead of at the threshold, the one lever that would have worked.

    Zero evidence has no finite shortfall: no score makes it eligible."""
    if match.meaningful_count == 0:
        return float("inf")
    if match.meaningful_count < 2:
        return _GENERIC_WORD_THRESHOLD - match.value
    return threshold - match.value


# The score a candidate must reach before it is applied without a person
# looking. Measured, not chosen: against a real library of 5924 scenes across
# 99 creators, scoring each file against that creator's whole catalogue with
# its own entry removed — so every application in that condition is wrong by
# construction — and again with it present.
#
#     threshold   applied a wrong entry     found the right entry
#                 when the right one was    when it was present
#                 absent from the catalogue
#     ---------   ------------------------  ---------------------
#       0.50                  16%                    80%
#       0.60                  12%                    79%
#       0.70                   6%                    77%
#       0.80                   3%                    74%
#       0.90                   3%                    71%
#
# 0.70 is the knee: it cuts the wrong-application rate by nearly two thirds
# for three points of recall. 0.60 gives up almost nothing and buys almost
# nothing. Below 0.80 each further step costs about as much recall as it buys
# precision, and 0.90 buys none at all.
#
# The asymmetry sets the direction — refusing costs a review, while a wrong
# automatic write costs a file nobody looks at again — but it does not justify
# paying any price, and this is where the price stops being worth paying.
#
# A caution about how this was measured, because it changed the answer: an
# earlier pass capped each creator's catalogue at 40 titles to run faster, and
# reported 0.70 costing eight points of recall rather than three. A smaller
# candidate set is an easier problem, and the cap quietly made the measurement
# answer a different question than the one it was labelled with.
#
# What no threshold here fixes: almost all of that harm is a file whose entry
# is not in the catalogue at all. The scorer has no way to say "none of these",
# so it takes the best available — confidently, not narrowly, which is why the
# ambiguity rule never sees these. Tracked separately.
DEFAULT_THRESHOLD = 0.7


def decide(matches, threshold=DEFAULT_THRESHOLD):
    """Pick the one candidate confident enough to apply automatically, or
    refuse with a reason a person can act on. Never guesses between two
    plausible candidates -- ambiguity is refused, not resolved by picking
    whichever came first."""
    if not matches:
        return Decision(match=None, index=None,
                        reason="no candidates offered", contenders=0,
                        interrogated=False)

    # Whether the catalogue was actually questioned with this file's evidence.
    # A candidate with no meaningful token is barred by `_is_eligible` at any
    # score, so it is never weighed against anything -- the refusal it produces
    # is a fact about the filename, not about the titles it was handed.
    #
    # `meaningful_count` is computed from the name, the folder and the artist
    # and never from the candidate title, so it is the same for every entry of
    # a list built for one file. `all` rather than `any` for the list that is
    # somehow not: a mixed list is a caller pooling candidates scored for
    # different files, and there is no reading of it that supports an absence.
    # Uncertainty may withhold evidence, never supply it.
    interrogated = all(m.meaningful_count > 0 for m in matches)

    eligible = [(m.value, i, m) for i, m in enumerate(matches) if _is_eligible(m, threshold)]

    if not eligible:
        # The candidate that came CLOSEST to being eligible, not the one with
        # the highest raw value: in a mixed list those are different
        # candidates, and only the near-miss tells the user something they can
        # act on. Ties break towards the higher score.
        best = min(matches, key=lambda m: (_shortfall(m, threshold), -m.value))
        if best.meaningful_count == 0:
            reason = (
                "nothing to match on (meaningful_count=0): the name carries no "
                "word that is not the artist's or generic"
            )
        elif best.meaningful_count < 2:
            reason = (
                "best score %.3f rests on a single generic word "
                "(meaningful_count=%d); needs %.2f or above"
                % (best.value, best.meaningful_count, _GENERIC_WORD_THRESHOLD)
            )
        else:
            reason = "nothing above the threshold (%.2f); best score was %.3f" % (
                threshold, best.value,
            )
        return Decision(match=None, index=None, reason=reason, contenders=0,
                        interrogated=interrogated)

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
            return Decision(match=None, index=None, reason=reason,
                            contenders=len(eligible),
                            interrogated=interrogated)

    reason = "chosen with score %.3f" % (top_value,)
    return Decision(match=top_match, index=top_index, reason=reason,
                    contenders=len(eligible), interrogated=interrogated)
