"""Pure filename/name string operations (no I/O)."""
import html
import os
import re

from cronicled.vocab import JUNK_TOKENS, STOPWORDS, VIDEO_EXTS


_BACKSLASH_ESCAPE_RE = re.compile(r"\\(['\"/\\])")


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
    s = re.sub(r"[^0-9a-zA-Z]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


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
