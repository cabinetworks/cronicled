"""Pure filename/name string operations (no I/O)."""
import html
import os
import re
import unicodedata

from cronicled.vocab import (CONTAINER_EXTS, ENCODE_MARKERS, JUNK_TOKENS,
                             STOPWORDS)


_BACKSLASH_ESCAPE_RE = re.compile(r"\\(['\"/\\])")

# A handful of ordinary European letters have no canonical decomposition into
# base letter + combining mark, so NFKD leaves them untouched (verified via
# unicodedata.decomposition()). Folded explicitly, AFTER the combining-mark
# strip (see `normalize`): an accented compound of one of these letters (e.g.
# o-with-stroke-and-acute) DOES decompose under NFKD, into the bare letter
# plus a combining mark, so the table has to see the bare letter left behind
# once that mark is gone, not the original compound.
_LETTER_FOLD = str.maketrans({
    "ł": "l", "Ł": "L",
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "đ": "d", "Đ": "D",
    "ß": "ss", "\u1e9e": "SS",  # ẞ, the rarely-used capital sharp s
})


def strip_html(text):
    """Clean scraped free-text: strip HTML tags, unescape HTML entities and stray
    backslash escapes, and collapse whitespace. Free-text fields pulled from a
    scrape can carry raw ``<p>...</p>`` / ``<br>`` / ``<a>`` tags, ``&amp;``-style
    entities, and backslash-escaped quotes (``I\\'m`` instead of ``I'm``); this
    cleans all three.

    Tags become a single space so words don't run together across them; entities
    are unescaped after tag removal (so a literal ``&lt;p&gt;`` in the text is
    preserved, not re-stripped). Idempotent and safe on plain text.

    Falsy input (``None`` or ``""``) returns ``""``, the same contract
    `normalize` and `clean_folder` use elsewhere in this module: an absent
    string and an empty one are the same fact for a caller of a string
    helper, and every caller here already treats them that way (see
    `cronicled.adapters.declarative`'s own `node or ""` guard, which this
    makes redundant rather than wrong)."""
    if not text:
        return ""
    stripped = re.sub(r"<[^>]+>", " ", text)                 # tags -> space
    stripped = html.unescape(stripped)                        # &amp; -> &, &#39; -> ', ...
    stripped = _BACKSLASH_ESCAPE_RE.sub(r"\1", stripped)      # \' \" \/ \\ -> ' " / \
    return re.sub(r"\s+", " ", stripped).strip()


def strip_ext(name):
    """Drop a trailing container extension, leaving anything else alone.

    Consults CONTAINER_EXTS, the broad list, and never SCAN_EXTS: the question
    here is "is this trailing token part of the title", not "is this a file
    worth walking". Answering it from the walker's list left a file muxed as
    `.mpeg` carrying a token its `.mp4` twin did not, and scored the two
    differently for it.

    Falsy input (``None`` or ``""``) returns ``""``, the same contract
    `normalize` and `clean_folder` use. Before this, `None` reached
    `os.path.splitext` and raised `TypeError`, which every caller that could
    see a `None` here already worked around with an explicit `name or ""`
    (see `cronicled.artist` and `cronicled.dates`) — those guards are now
    redundant, not wrong, and are left in place rather than removed as part
    of this fix.
    """
    if not name:
        return ""
    root, ext = os.path.splitext(name)
    return root if ext.lower() in CONTAINER_EXTS else name


_QUALIFIER_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

# Separators a marker is spelled with -- "H.265", "web-dl", "10-bit". Folded
# out before the ENCODE_MARKERS lookup so one entry covers every spelling.
# Brackets are NOT here: a bracketed marker belongs to `_QUALIFIER_RE`, and
# folding brackets away would let either rule cover for the other.
_MARKER_SEPARATORS = str.maketrans({".": "", "-": "", "_": ""})


def _is_encode_marker(token):
    """True when `token` is, whole, a known encode marker -- see
    ENCODE_MARKERS for what the list claims and why it is closed.

    Membership in a list, never a match against a pattern. The token is
    lowercased and its separators folded out, and that is the only latitude
    given: a token merely SHAPED like a marker is not one, because the shape
    of a marker is also the shape of an initial, a volume number and a
    one-word handle.
    """
    return token.lower().translate(_MARKER_SEPARATORS) in ENCODE_MARKERS


def _strip_encode_markers(text):
    """`text` with any run of trailing encode markers removed.

    Only at the end, and only whole whitespace-separated tokens. A marker in
    the MIDDLE of a folder name is not a qualifier tacked on -- something put
    it there between two pieces of the name -- so the run stops at the first
    token the list does not claim and everything at or before it survives,
    marker or not. A marker glued to a neighbour ("Velvet Crane-1080p") is not
    a whole token and is likewise left alone: splitting inside a token is how
    a hyphenated name loses half of itself.
    """
    words = text.split()
    while words and _is_encode_marker(words[-1]):
        words.pop()
    return " ".join(words)


def clean_folder(name):
    """Strip a qualifier from a folder name, so a quality/encoding tag tacked
    onto the end doesn't stop the rest of the name from being read as-is.

    Two separate rules, each doing its own half:

    * a parenthesised or bracketed qualifier, anywhere in the name
      ('Velvet Crane (h265)' -> 'Velvet Crane');
    * a run of trailing tokens that are known encode markers, brackets or no
      ('Velvet Crane h265' -> 'Velvet Crane'). Bare markers are the far more
      common filing shape, and until they were stripped the marker rode along
      into the resolved creator's name -- a name no store has, recorded as
      the attribution and read as fact by everything downstream.

    The second rule is a CLOSED LIST and not a pattern, deliberately: see
    ENCODE_MARKERS. An unlisted trailing token is returned untouched, however
    marker-shaped it looks, because an initial, a volume number and a one-word
    handle all look exactly like one.

    A name that is nothing BUT a qualifier cleans away to "" -- the same
    answer the bracketed rule has always given, and what
    `artist.creator_folder` reads to walk past such a directory instead of
    attributing files to it.
    """
    text = re.sub(r"\s+", " ", _QUALIFIER_RE.sub(" ", name or "")).strip()
    return _strip_encode_markers(text)


def normalize(s):
    """Lowercase, fold combining-mark accents to their base letter, reduce
    every other non-alphanumeric character to a single space.

    Accents fold because filenames routinely drop them while store titles keep
    them. This only catches letters whose accent is a separate combining mark
    under NFKD decomposition (e.g. e-acute -> e + combining acute) - ordinary
    letters like l-with-stroke, o-with-stroke, the ae ligature, d-with-stroke,
    and the German sharp s do NOT decompose that way, so `_LETTER_FOLD` folds
    that short, explicit list separately, AFTER the combining-mark strip
    below: an accented compound of one of them (e.g. o-with-stroke-and-acute)
    decomposes under NFKD into the bare letter plus a combining mark, and only
    once that mark is stripped does the bare letter match the table.

    One deliberate non-fold: Turkish dotless i (u+0131) has no decomposition
    and is not in the table, so it passes through unfolded. Folding it to
    ASCII "i" would be a guess - dotted and dotless i are distinct letters in
    Turkish, and this function does not collapse distinctions it can't be
    sure are safe to collapse.

    Non-Latin letters are preserved rather than deleted: dropping them
    would give a non-Latin name an empty slug, which matches nothing.
    """
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    kept = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue          # a stripped accent
        kept.append(ch)
    folded = "".join(kept).translate(_LETTER_FOLD)
    return " ".join("".join(c if c.isalnum() else " " for c in folded).lower().split())


def tokens(s, drop_junk=True, drop_stop=True):
    out = []
    for t in normalize(s).split():
        if drop_junk and t in JUNK_TOKENS:
            continue
        if drop_stop and t in STOPWORDS:
            continue
        out.append(t)
    return out


def spaceless(s):
    return normalize(s).replace(" ", "")


def slug_match(a, b):
    a, b = spaceless(a), spaceless(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)
