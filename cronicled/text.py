"""Pure filename/name string operations (no I/O)."""
import html
import os
import re
import unicodedata

from cronicled.vocab import JUNK_TOKENS, STOPWORDS, VIDEO_EXTS


_BACKSLASH_ESCAPE_RE = re.compile(r"\\(['\"/\\])")

# A handful of ordinary European letters have no canonical decomposition into
# base letter + combining mark, so NFKD leaves them untouched (verified via
# unicodedata.decomposition()). Folded explicitly, before the NFKD pass, so
# `normalize` still treats them like their accented cousins that do decompose.
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
    preserved, not re-stripped). Idempotent and safe on plain text."""
    if not text:
        return text
    stripped = re.sub(r"<[^>]+>", " ", text)                 # tags -> space
    stripped = html.unescape(stripped)                        # &amp; -> &, &#39; -> ', ...
    stripped = _BACKSLASH_ESCAPE_RE.sub(r"\1", stripped)      # \' \" \/ \\ -> ' " / \
    return re.sub(r"\s+", " ", stripped).strip()


def strip_ext(name):
    root, ext = os.path.splitext(name)
    return root if ext.lower() in VIDEO_EXTS else name


def normalize(s):
    """Lowercase, fold combining-mark accents to their base letter, reduce
    every other non-alphanumeric character to a single space.

    Accents fold because filenames routinely drop them while store titles keep
    them. This only catches letters whose accent is a separate combining mark
    under NFKD decomposition (e.g. e-acute -> e + combining acute) - ordinary
    letters like l-with-stroke, o-with-stroke, the ae ligature, d-with-stroke,
    and the German sharp s do NOT decompose that way and would otherwise pass
    through unfolded, so `_LETTER_FOLD` handles that short, explicit list
    first. Non-Latin letters are preserved rather than deleted: dropping them
    would give a non-Latin name an empty slug, which matches nothing.
    """
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s.translate(_LETTER_FOLD))
    kept = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue          # a stripped accent
        kept.append(ch if ch.isalnum() else " ")
    return " ".join("".join(kept).lower().split())


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
