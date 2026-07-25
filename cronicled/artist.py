"""Work out which creator a file belongs to, from its folder and its name.

Two halves. `creator_folder` finds the ancestor directory a file's
attribution scores against; `resolve` turns that folder and the filename
into a `Resolution` -- a name, where it came from, and the name that lost
if the two disagreed.

A library layout often inserts one or more generic directories between the
creator's own folder and the file itself -- "Clips", "Downloads",
"Videos/Uploads" and the like. `creator_folder` walks up past those to find
the folder that actually names the creator, so scoring compares a filename
against *that creator's* catalogue rather than a container's name.

``CONTAINER_NAMES`` is a small, closed, English-only set of generic
directory names. It recognises common single-word containers, in their
usual English spelling, and nothing more: it will not recognise a numbered
set ("Set 03"), a non-English equivalent, or a season/date directory. When
a container is missing from the set, the failure is to attribute the file
to that folder as if it were the creator -- a visible, checkable mistake (a
"creator" that turns out to be "Downloads"), not a silent one.

The set grew from real filing conventions rather than being assembled up
front: every entry below except `misc` is carried over from a legacy
implementation's list, grown over time from libraries actually seen in the
wild ("Vids"/"Vid" as shorthand for "Videos"/"Video" among them). `misc` is
this rewrite's own addition -- a reasonable guess, not observed evidence,
and worth distinguishing from the rest for that reason. Keep extending the
set the same way it was built: from a real container name a test or a
report turns up, not from speculating about what else might exist.

`resolve` is deliberately unwilling. Every rejection rule below exists
because a real filing convention produced a wrong attribution: a date
prefix read as a person, a title that happens to contain a dash, a guest
credited instead of the creator. Resolving nothing is a cheap, visible
failure -- the file simply stays unattributed until someone looks. A
confident wrong name is not: it files work under someone else's name and
nothing downstream ever questions it. When in doubt, this module declines.

Nothing here touches the filesystem. `creator_folder` splits a path as a
string; `resolve` is given the folder name, not a path.
"""
import posixpath
import re
from dataclasses import dataclass

from cronicled.dates import looks_like_date
from cronicled.text import clean_folder, normalize, spaceless, strip_ext

CONTAINER_NAMES = frozenset({
    "video", "videos",
    "clip", "clips",
    "vid", "vids",
    "movie", "movies",
    "media",
    "download", "downloads",
    "upload", "uploads",
    "content",
    "files",
    "misc",  # judgment call, not carried over from the legacy list
})


def _container_name(name):
    """The lowercased, qualifier-stripped form of `name`, for comparison
    against CONTAINER_NAMES."""
    return clean_folder(name).lower()


def creator_folder(path, max_up=4):
    """The nearest ancestor directory of `path` that is not a generic
    container, with any qualifier (an encode tag, a bracketed year, ...)
    stripped.

    Walks up at most `max_up` ancestor directories from the file, skipping
    any whose `clean_folder`-ed, lowercased name is in CONTAINER_NAMES, and
    returns the first that is not. If every ancestor checked is a
    container -- or there simply is no such ancestor -- falls back to the
    immediate parent, also stripped, rather than continuing to walk toward
    the filesystem root.

    `path` is treated purely as a string, split with `posixpath`. This
    never touches the filesystem: no ``os.path.exists``, no stat, no
    resolving of ``..`` or symlinks.
    """
    directory = posixpath.dirname(path)
    if not directory or directory == "/":
        return ""

    immediate_parent = None
    ancestor = directory
    for _ in range(max_up):
        if not ancestor or ancestor == "/":
            break
        name = posixpath.basename(ancestor)
        if immediate_parent is None:
            immediate_parent = name
        if _container_name(name) not in CONTAINER_NAMES:
            return clean_folder(name)
        ancestor = posixpath.dirname(ancestor)

    return clean_folder(immediate_parent) if immediate_parent is not None else ""


# --- Resolving a creator's name -------------------------------------------

# A word that leads a title, not a name. "The Long Wait - Part Two" is a
# title that happens to contain a dash; "How I Spent My Weekend - Diary" is
# a sentence. No person's name starts with one of these, so a candidate
# that does is a title being misread.
ARTICLE_LEADS = frozenset({"a", "an", "the", "my", "your", "no",
                           "how", "when", "why"})

# A name is at least this many characters (ignoring case, spacing and
# punctuation) and at most this many words. Two characters is an initialism
# or a typo, not something to attribute work to; five words or more is a
# sentence.
MIN_NAME_CHARS = 3
MAX_NAME_WORDS = 4

# "Creator - Title". Only a dash with whitespace on both sides splits, so a
# hyphenated name ("Anne-Marie Smith.mp4") is left whole. En and em dashes
# count: libraries use all three interchangeably.
_DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")

# "... feat Velvet Crane". The marker must start a word and be followed by
# whitespace, so "Feathers" and "Ftesting" are not markers.
_FEAT_RE = re.compile(r"(?:^|\s)(?:feat|ft|featuring)\.?\s+(\S.*)$", re.I)


@dataclass(frozen=True)
class Resolution:
    """Who a file is attributed to, and what was passed over to say so.

    `source` names where `name` came from -- "folder", "filename", "alias" --
    or is None when nothing resolved. `competing` carries the name that lost:
    when the folder and the filename each yield a plausible but different
    person, the folder wins and the filename's answer is recorded here.

    `rejected_folder` carries a folder whose text a guard threw out -- it was
    not a name at all, so it never competed. Set whenever a non-empty folder
    name neither won nor matched an alias, whatever `name` ended up being.

    The two are kept apart on purpose. `competing` is a *name*: something a
    consumer may legitimately search a catalogue for, or offer as the other
    reading. `rejected_folder` is raw folder text that failed the guards --
    "Downloads", "AB", "2023 September 11" -- and treating it as a name is
    the mistake this module exists to prevent. Folding it into `competing`
    would make that field mean two incompatible things and force every
    consumer to re-run the guards to tell which it had.

    Both disagreements are the most useful signal available, so both are
    reported rather than dropped. A library where they show up often is one
    whose filing convention is not what the operator assumed -- files sitting
    in the wrong creator's folder, or a naming convention that puts the guest
    first. The legacy resolver silently took the first candidate that
    qualified, which threw that away and left the mis-filing invisible. A
    rejected folder is the sharper of the two: with it empty, a filename-
    sourced attribution reads as "the folder had nothing to say", when in
    fact the folder named someone else and was overruled.
    """
    name: str | None = None
    source: str | None = None
    competing: str | None = None
    rejected_folder: str | None = None


def _is_name(text):
    """True when `text` could be a person's name.

    Five independent rejections, each from a filing convention that produced
    a wrong attribution:

    * too short -- under MIN_NAME_CHARS, an initialism or noise;
    * too long -- over MAX_NAME_WORDS, a sentence or a title;
    * date-shaped -- "2023 September 11" is a date convention, not a person;
    * article-led -- see ARTICLE_LEADS;
    * a container word -- "Downloads" is a directory, not a creator.

    (The sixth, the guest guard, belongs to the filename and lives in
    `_featured_name`.) Each is separately load-bearing: removing any one of
    them makes a specific test in tests/test_artist.py fail.
    """
    text = (text or "").strip()
    words = normalize(text).split()
    if not words:
        return False                                    # nothing to judge
    if len(spaceless(text)) < MIN_NAME_CHARS:
        return False                                    # guard: too short
    if len(words) > MAX_NAME_WORDS:
        return False                                    # guard: too long
    if looks_like_date(text):
        return False                                    # guard: a date
    if words[0] in ARTICLE_LEADS:
        return False                                    # guard: article-led
    if _container_name(text) in CONTAINER_NAMES:
        return False                                    # guard: a container
    return True


def _same_name(a, b):
    """True when `a` and `b` are the same name once spacing, case, punctuation
    and accent composition are taken out -- "Velvet Crane", "velvetcrane" and
    an NFD-spelled folder against its NFC filename all agree.

    Equality, deliberately, not `text.slug_match`'s containment. Containment
    is safe where both sides are already specific full names; here one side is
    a folder that cleared only a 3-character, 4-word gate, so it is routinely
    the *less* specific of the two. Under containment, a folder "Ivy" beside a
    filename "Ivy Kingsley Waters" counts as the same person: the shorter name
    wins and no disagreement is reported. That is the failure this module is
    built to avoid -- a whole catalogue searched under a fragment, with
    nothing in the resolution saying so. Two names that differ by more than
    spelling are two names, and the caller gets told.
    """
    a, b = spaceless(a), spaceless(b)
    return bool(a) and a == b


def _featured_name(text):
    """The name after a `feat` marker, but only when that same name also
    leads `text`; otherwise None.

    A name after `feat` is a guest. Crediting the guest instead of the
    creator is precisely the wrong answer -- it files someone's work under
    the person who visited. So the marker is trusted only when it merely
    repeats a name already leading the filename ("Velvet Crane Morning
    Ritual Feat Velvet Crane"), which carries no new claim. Otherwise this
    resolves nothing and the file stays unattributed, which is the cheap
    failure.

    The lead is compared token by token, not as a prefix of the run-together
    string, so "Velvet Cranes Diary Feat Velvet Crane" does not count as led
    by "Velvet Crane".
    """
    match = _FEAT_RE.search(text)
    if not match:
        return None
    featured = match.group(1).strip()
    if not _is_name(featured):
        return None
    featured_words = normalize(featured).split()
    lead_words = normalize(text[:match.start()]).split()
    if lead_words[:len(featured_words)] != featured_words:
        return None                                     # guard: a guest
    return featured


def _filename_candidate(name):
    """The creator named by the filename, or None.

    Two conventions, in order. A spaced dash splits "Creator - Title", and
    the left side is the candidate; that convention wins outright, so a
    `feat` later in the same name is never consulted -- the left of the dash
    has already said who this belongs to. Failing a dash, a `feat` marker is
    read under the rule in `_featured_name`.
    """
    text = strip_ext(name or "").strip()
    if not text:
        return None
    parts = _DASH_SPLIT_RE.split(text, 1)
    if len(parts) == 2:
        left = parts[0].strip()
        return left if _is_name(left) else None
    return _featured_name(text)


def _alias_name(folder, aliases):
    """The full name `folder` is an operator-registered alias for, else None.

    Matching is exact on the normalised, spaceless form, so "V-Crane",
    "v crane" and "vcrane" all reach the same entry -- that much is just
    spelling. Nothing further: no prefix matching, no edit distance, no
    deriving initials. "vc" does not become "Velvet Crane".

    Guessing what an abbreviation stands for is how files end up attributed
    to the wrong person, and the guess is invisible once made -- two
    creators sharing initials is not unusual, and neither answer looks wrong
    in a report. An operator adding one line to the alias map is cheap and
    is a decision someone actually made. So an unlisted abbreviation
    resolves to nothing, on purpose.

    The mapped value is returned as written and is not put through
    `_is_name`: the operator wrote it deliberately, and a real name that
    happens to be two characters long is theirs to declare.
    """
    if not aliases:
        return None
    key = spaceless(folder)
    if not key:
        return None
    for alias, full in aliases.items():
        if spaceless(alias) == key:
            return full
    return None


def resolve(name, folder, aliases=None):
    """Attribute the file `name` sitting in `folder` to a creator.

    `folder` is a folder *name* -- normally whatever `creator_folder`
    returned for the file's path -- not a path, and may be empty. `aliases`
    maps an as-filed folder name to the full name it stands for.

    Tried in order: an alias on the folder, the folder itself, then the
    filename. The folder beats the filename because someone chose to file
    the video there; that is a more deliberate signal than a name typed into
    a filename, which is as often the title, the site or the guest. Each
    candidate has to survive `_is_name` (and, for a `feat` marker,
    `_featured_name`) or it is not a name at all.

    When the folder and the filename both name someone, and they are not the
    same person by `_same_name` (so "Velvet Crane" and "velvetcrane" agree,
    while "Ivy" and "Ivy Kingsley Waters" do not), the folder wins and the
    filename's name is returned in `competing`. When the folder yields text
    that no guard will accept as a
    name, it is returned in `rejected_folder`. Neither is ever silently
    dropped -- see `Resolution`.

    Returns a `Resolution`; all of its fields are None when nothing resolved
    and there was no folder to reject, which is a real answer and not an
    error.
    """
    folder_text = clean_folder(folder or "")
    from_filename = _filename_candidate(name)

    aliased = _alias_name(folder_text, aliases)
    if aliased is not None:
        resolved, source = aliased, "alias"
    elif _is_name(folder_text):
        resolved, source = folder_text, "folder"
    elif from_filename is not None:
        resolved, source = from_filename, "filename"
    else:
        resolved, source = None, None

    # The folder had something to say and it was not used: say so, or the
    # filename's answer looks unopposed when it is not.
    rejected_folder = folder_text or None
    if source in ("folder", "alias"):
        rejected_folder = None

    competing = None
    if (source != "filename" and from_filename is not None
            and not _same_name(resolved, from_filename)):
        competing = from_filename
    return Resolution(resolved, source, competing, rejected_folder)
