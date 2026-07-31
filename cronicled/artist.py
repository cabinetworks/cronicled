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

`resolve`'s folder-wins default is itself a guess, and a measured one: a file
named "<store> - <creator> - <title>", filed straight under a folder named
for the store, used to resolve to the store, because the store's own name
passed every guard `_is_name` enforces just as cleanly as the creator's did.
An optional `owners_of` collaborator lets `resolve` check a candidate instead
of assuming it: when the folder and the filename (or the filename's own
several segments) name more than one plausible person, each is asked of the
catalogue, and only a candidate the catalogue actually attributes results to
wins -- see `_resolve_by_evidence`. Nothing calls a search when there is only
one plausible candidate, so the common, unambiguous file costs exactly what
it always did.
"""
import posixpath
import re
from dataclasses import dataclass

from cronicled.dates import MONTH_PATTERN, looks_like_date
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
    any whose `clean_folder`-ed, lowercased name is in CONTAINER_NAMES -- and
    any that `clean_folder` empties entirely, a directory that is nothing but
    a qualifier -- and returns the first that is neither. If every ancestor
    checked is skipped that way -- or there simply is no such ancestor --
    falls back to the immediate parent, also stripped, rather than continuing
    to walk toward the filesystem root.

    `path` is treated purely as a string, split with `posixpath`. This
    never touches the filesystem: no ``os.path.exists``, no stat, no
    resolving of ``..`` or symlinks. One consequence worth stating: a
    Windows-style path ("C:\\lib\\Velvet Crane\\clip.mp4") has no posix
    separator, so it has no parent directory here and yields "". Splitting
    posix-style is deliberate -- library paths reach this module already
    normalised -- so that is a limit, not a bug, but it is silent, hence
    the note and the test.
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
        cleaned = clean_folder(name)
        # A directory that is nothing but a qualifier -- "[2024]", "(h265)" --
        # cleans away to nothing. It names no creator, so it is a container by
        # the same reasoning as "Clips" and the walk continues past it.
        # Returning it would hand back "" and strand every file beneath a
        # bracketed-year subfolder with no folder attribution at all.
        if cleaned and _container_name(name) not in CONTAINER_NAMES:
            return cleaned
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

# A month with a year and no day -- "Sep 2023", "2023 September", and the
# same two shapes with no space at all ("Sep2023", "2023September"). A filing
# style, not a person. The separator is optional rather than absent because a
# single space still has to be recognised -- only its presence, not its
# length, is variable here: the whitespace collapse below already reduces any
# run of spaces to one before this matches, so `\s?` (zero or one) is enough
# and deliberately narrower than `\s*`.
#
# This and the all-digits check in `_is_name` cover two date shapes
# `dates.looks_like_date` deliberately does not, and they are kept here rather
# than widened into it. `looks_like_date` also drives date *extraction*:
# `dates.unparsed_date_prefix` reports any prefix that looks like a date but
# yields none, so teaching detection a shape extraction cannot read would turn
# every such filename into a spurious report. This guard only ever says "not a
# name", which is safe to widen. The month spellings come from `dates` so the
# two cannot drift apart.
_MONTH_YEAR_RE = re.compile(
    r"^(?:(?:%(month)s)\.?\s?(?:19|20)\d{2}"
    r"|(?:19|20)\d{2}\s?(?:%(month)s)\.?)$" % {"month": MONTH_PATTERN}, re.I)

# "Creator - Title". Only a single dash with whitespace on both sides splits,
# so a hyphenated name ("Wren-Copper Marchcroft.mp4") is left whole. En and em
# dashes count: libraries use all three interchangeably. A doubled dash
# ("Velvet Crane -- Morning Ritual") is deliberately not a separator -- one
# convention, read one way; the file goes unattributed rather than the left of
# a dash quietly meaning two different things.
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
    person, the folder wins and the filename's answer is recorded here --
    UNLESS `resolve` was given a search to check candidates against, in which
    case the candidate the catalogue actually supports wins and whichever
    plausible name lost -- folder or filename, either can now lose -- is
    recorded here instead. Either way, a name that competed and lost is
    reported, never silently dropped.

    `rejected_folder` carries a folder whose text a guard threw out -- it was
    not a name at all, so it never competed. Set whenever a non-empty folder
    name neither won nor matched an alias, whatever `name` ended up being. A
    folder that WAS a plausible name but lost an evidence-backed competition
    is not this -- it competed, so it belongs in `competing`, the same as a
    losing filename ever has.

    The two are kept apart on purpose. `competing` is a *name*: something a
    consumer may legitimately search a catalogue for, or offer as the other
    reading. `rejected_folder` is folder text that failed the guards --
    "Downloads", "AB", "2023 September 11" -- and treating it as a name is
    the mistake this module exists to prevent. Folding it into `competing`
    would make that field mean two incompatible things and force every
    consumer to re-run the guards to tell which it had.

    `rejected_folder` is the *cleaned* folder text -- the same `clean_folder`
    pass the folder candidate itself goes through, so a bracketed qualifier
    is already stripped -- not the raw, on-disk directory name. Both
    readings are defensible; this one is picked because `rejected_folder`
    exists to show what the guards judged, and the guards themselves never
    see the qualifier at all.

    Both disagreements are the most useful signal available, so both are
    reported rather than dropped. A library where they show up often is one
    whose filing convention is not what the operator assumed -- files sitting
    in the wrong creator's folder, or a naming convention that puts the guest
    first. The legacy resolver silently took the first candidate that
    qualified, which threw that away and left the mis-filing invisible. A
    rejected folder is the sharper of the two: with it empty, a filename-
    sourced attribution reads as "the folder had nothing to say", when in
    fact the folder named someone else and was overruled.

    `unconfirmed` carries every plausible candidate `_resolve_by_evidence`
    actually asked the catalogue about when NONE of them came back
    supported -- see `_is_supported`. This is a THIRD, different fact from
    the two above, and collapsing it into either one is exactly the mistake
    this field exists to stop: unlike `rejected_folder`, every name in here
    passed `_is_name` -- nothing here failed a guard -- and unlike
    `competing`, nothing here won either, so there is no winner to report it
    as the runner-up of. Before this field existed, a caller receiving
    `name=None` with `rejected_folder=None` (because the folder itself WAS a
    plausible candidate, just an unconfirmed one) could not tell "nothing in
    the folder or filename even looked like a name" from "one or more names
    were found and checked, and the catalogue backed none of them" -- and
    wrote a reason claiming the former when the truth was the latter, which
    reads to an operator as "this tool cannot read filenames" beside a file
    whose name is right there. Empty (`()`) whenever the evidence check
    never ran (no `owners_of`, or fewer than two candidates on offer) or it
    resolved to exactly one supported candidate -- the ordinary cases, where
    there is nothing unresolved to report here at all. Ordered folder-first,
    the same order the candidates were built in and asked of the catalogue,
    for a reader's benefit only -- nothing about which one is unconfirmed
    depends on that order.
    """
    name: str | None = None
    source: str | None = None
    competing: str | None = None
    rejected_folder: str | None = None
    unconfirmed: tuple = ()


def _is_name(text):
    """True when `text` could be a person's name.

    Five independent rejections, each from a filing convention that produced
    a wrong attribution:

    * too short -- under MIN_NAME_CHARS, an initialism or noise;
    * too long -- over MAX_NAME_WORDS, a sentence or a title;
    * date-shaped -- "2023 September 11" is a date convention, not a person.
      Three forms: what `dates.looks_like_date` recognises, an all-digit
      candidate ("20230911", "2023"), and a month with a year but no day
      ("Sep 2023"). A month word *alone* is not rejected: "March Hollis" and
      "May Winters" are people, and declining them would be its own wrong
      answer;
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
    if spaceless(text).isdigit():
        return False                                    # guard: a date, compact
    if _MONTH_YEAR_RE.match(" ".join(text.split())):
        return False                                    # guard: a date, no day
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

    The left side is run through `clean_folder` before it is judged, the same
    cleaning the folder side already gets. Without it, a qualifier that a
    scraper tacks onto the filename but not the folder -- a bracketed site
    tag, an encode marker -- makes the same person read as two: folder
    "Velvet Crane" against filename "[SiteTag] Velvet Crane - Clip.mp4" would
    disagree with themselves. Cleaning both sides the same way removes that
    asymmetry without loosening `_same_name`'s identity rule.
    """
    text = strip_ext(name or "").strip()
    if not text:
        return None
    parts = _DASH_SPLIT_RE.split(text, 1)
    if len(parts) == 2:
        left = clean_folder(parts[0].strip())
        return left if _is_name(left) else None
    return _featured_name(text)


def _filename_candidates(name):
    """Every plausible creator name the filename offers, most specific
    (leftmost) first -- the plural counterpart to `_filename_candidate`,
    used only when `resolve` has a search to check candidates against (see
    `_resolve_by_evidence`). Where `_filename_candidate` commits to the
    FIRST dash segment alone, this offers every segment before the last one,
    because a filename can name more than one thing before its title:
    "<store> - <creator> - <title>" splits into three segments, and the
    creator is the SECOND, not the first -- a real, measured case where
    `_filename_candidate`'s single answer was the store, not the person who
    made the clip.

    The last segment is never a candidate -- it is read as the title, the
    same convention `_filename_candidate` and every dash-delimited test in
    this module already assume. Each of the segments before it is checked
    against `_is_name` independently, so one that fails (a date, a
    container word) does not stop a later one from still being offered --
    unlike `_filename_candidate`, which gives up the instant its one segment
    fails the guard.

    No dash at all falls back to the single `feat`-guarded name
    `_featured_name` finds, exactly as `_filename_candidate` does, since
    there is only ever one such candidate to offer.
    """
    text = strip_ext(name or "").strip()
    if not text:
        return []
    segments = _DASH_SPLIT_RE.split(text)
    if len(segments) >= 2:
        candidates = []
        for segment in segments[:-1]:
            cleaned = clean_folder(segment.strip())
            if _is_name(cleaned):
                candidates.append(cleaned)
        return candidates
    featured = _featured_name(text)
    return [featured] if featured is not None else []


# How many of a candidate's search results the catalogue must attribute to
# that SAME name before the candidate is trusted -- see `_owner_support` and
# `_resolve_by_evidence`. The two thresholds are unequal on purpose:
#
# * an EXACT match -- the owner's name, once spacing/case/punctuation are
#   stripped, equals the candidate outright -- is trusted from a single
#   supporting result. Nothing about an exact match is a guess.
# * a PREFIX match -- the owner's name only EXTENDS the candidate ("Ivy"
#   inside a store's own "Ivy Kingsley Waters") -- needs more than one
#   supporting result, so a single mis-tagged or cross-store clip cannot
#   manufacture an extended name that was never really filed under it.
#
# Measured on live data: querying the correct creator's name returned 19
# results the adapter attributed to that same name (exact); querying the
# wrong candidate (a store's own name, repeated in every filename) returned
# 0. Neither number is close to a boundary, so these thresholds are a
# reasonable floor rather than a value tuned to that one measurement.
MIN_EXACT_SUPPORT = 1
MIN_PREFIX_SUPPORT = 2


def _owner_support(candidate, owner_names):
    """How many of `owner_names` -- the owner `owners_of(candidate)` read off
    each of the candidate's search results -- support `candidate`, split into
    an EXACT count and a PREFIX count. See `MIN_EXACT_SUPPORT` for what each
    means and why they differ.

    Compared the same way `_same_name` compares two names -- spacing, case
    and punctuation stripped -- so "Ivy Kingsley" from a filename and
    "IvyKingsley" from a store's own field are the same owner. A blank owner
    name (a result the adapter could not attribute to anyone) counts toward
    neither: it is evidence about nobody, not evidence that the candidate is
    wrong.
    """
    slug = spaceless(candidate)
    exact = prefix = 0
    for owner in owner_names:
        owner_slug = spaceless(owner)
        if not owner_slug:
            continue
        if owner_slug == slug:
            exact += 1
        elif owner_slug.startswith(slug):
            prefix += 1
    return exact, prefix


def _is_supported(exact, prefix):
    """True when a candidate's `_owner_support` clears the bar `resolve`
    requires before trusting it -- see `MIN_EXACT_SUPPORT`."""
    return exact >= MIN_EXACT_SUPPORT or prefix >= MIN_PREFIX_SUPPORT


def _resolve_by_evidence(folder_text, name, owners_of):
    """Resolve `folder_text`/`name` by checking each plausible candidate
    against the catalogue, when more than one is on offer.

    Builds the full candidate set -- `folder_text` itself, if it is a name,
    then every segment `_filename_candidates` offers that is not already the
    same name (by `_same_name`) as one already in the set -- folder first,
    matching the priority `resolve` has always given it. With at most one
    distinct candidate, there is nothing to check: it is the answer, exactly
    as the search-free rule would give, and `owners_of` is never called --
    the common, unambiguous file costs nothing extra. This is also why a
    single-creator store (the store's own folder name IS the creator) still
    resolves: there is only ever one candidate on offer, so it wins without
    ever being questioned.

    With two or more, each is asked of `owners_of(candidate)` -- a real
    catalogue search -- and `_owner_support` counts how many of the results
    the catalogue attributes to that same name. A candidate is SUPPORTED
    when that count clears `_is_supported`'s bar.

    Exactly one supported candidate wins; the runner-up -- the first other
    candidate on offer, whichever source it came from -- is reported as
    `competing`, never dropped (see `Resolution`'s docstring: a losing
    candidate here is evidence the filing convention was not what was
    assumed, not a tie to break by discarding one side).

    Zero supported candidates and more than one both come back unresolved
    (`None, None, None, unconfirmed`) -- the same NAME/SOURCE/COMPETING
    outcome, for two different reasons, but no longer indistinguishable from
    each other or from "nothing to check in the first place": the fourth
    return value is every candidate that was actually built and asked of the
    catalogue, folder first. Zero supported: nothing in the catalogue backs
    any reading. More than one supported: two candidates are BOTH
    catalogue-confirmed, and picking between them by whichever was checked
    first is exactly the ordering mistake this project has already removed
    from candidate scoring, alias-key collisions and (until now) this
    module's own folder-vs-filename default. Neither is distinguished
    further from the other -- both are "this run could not settle on one
    name", which is the fact `unconfirmed` exists to carry, and a caller
    that wants a sharper split than that has not been asked for one yet.
    Reporting nothing as NAME is still the cheap, visible failure
    `resolve`'s own docstring commits to; what changed is that the REASON is
    no longer thrown away with it. Neither path ever falls back to a
    candidate's position in the list -- `unconfirmed`'s own order is for a
    reader, not a decision.

    This is NOT a mute. `resolve` returning `name=None` here has always
    meant "refuse and let a later scan reconsider" (see
    `cronicled.scan._unresolved_creator`); populating `unconfirmed` changes
    what the refusal SAYS, not whether the file is muted.
    """
    candidates = []
    if _is_name(folder_text):
        candidates.append((folder_text, "folder"))
    for segment in _filename_candidates(name):
        if not any(_same_name(segment, existing) for existing, _ in candidates):
            candidates.append((segment, "filename"))

    if not candidates:
        return None, None, None, ()
    if len(candidates) == 1:
        resolved, source = candidates[0]
        return resolved, source, None, ()

    supported = [(candidate_name, source) for candidate_name, source in candidates
                 if _is_supported(*_owner_support(candidate_name, owners_of(candidate_name)))]
    if len(supported) != 1:
        unconfirmed = tuple(candidate_name for candidate_name, _ in candidates)
        return None, None, None, unconfirmed

    resolved, source = supported[0]
    competing = next((candidate_name for candidate_name, _ in candidates
                      if not _same_name(candidate_name, resolved)), None)
    return resolved, source, competing, ()


def _alias_index(aliases):
    """`aliases` re-keyed by the normalised spaceless form of each key, with
    the wiring mistakes refused rather than resolved by luck.

    Three are refused, each with a `ValueError` naming the key or keys at
    fault:

    * **two keys that normalise alike** -- "vcrane" and "v crane" are the
      same lookup, and a linear scan returned whichever the dict happened to
      yield first. That is the legacy anti-pattern this rewrite exists to
      remove ("where two owners tie it takes max(), which returns whichever
      came first in iteration order") reappearing in the alias path: an
      operator's duplicated line would attribute files by authoring accident.
      Refused even when the two agree on a name, because the rule an operator
      can hold in their head is one key per normalised form;
    * **a key that normalises to nothing** -- it can never match, so it sits
      in the map looking like coverage that does not exist;
    * **an empty or non-string value** -- not a name to attribute work to.
      Letting it through gives either `name=''` with `source='alias'`, which
      is incoherent, or (for None) a fall-through in which the folder itself
      is resolved as a creator, so a half-written line quietly becomes an
      attribution.

    Validation runs over the whole map, not just the key being looked up: a
    partial check passes a map that is wrong, and the point is to fail where
    the mistake was made rather than months later in a report nobody
    re-checks. `Aliases` is what makes "where the mistake was made" mean the
    moment the map is loaded instead of whichever file happened to be resolved
    first.
    """
    index = {}
    keys = {}
    for key, full in (aliases or {}).items():
        slug = spaceless(key)
        if not slug:
            raise ValueError(
                "alias key %r normalises to nothing, so it can never match"
                % (key,))
        if slug in index:
            raise ValueError(
                "alias keys %r and %r both normalise to %r; keep one"
                % (keys[slug], key, slug))
        if not isinstance(full, str) or not full.strip():
            raise ValueError(
                "alias %r must map to a non-empty name, got %r" % (key, full))
        index[slug] = full
        keys[slug] = key
    return index


class Aliases:
    """An operator's alias map, normalised and checked ONCE.

    The index a lookup needs is derived from the map, and the map does not
    change during a run — but `resolve` was rebuilding it per call, which for
    a scan is per file. Measured on this machine, resolving one file against a
    500-entry map cost 642 us of re-normalising keys that had not moved, and a
    50,000-file scan against a 200-entry map spent 12.7 seconds doing it.

    The alternative was to hide a cache inside `resolve`. It was not taken.
    The map arrives as a plain mapping, which is mutable and unhashable, so
    such a cache could only be keyed on object identity — and would then go on
    answering from a stale index after a caller edited the dict it handed in,
    which is a wrong attribution produced by an optimisation and invisible in
    every report. A value the caller builds says plainly that the index is
    fixed at the moment it is built.

    Building it here also moves the one failure this map can produce to where
    an operator can act on it. A duplicated or empty alias line is wrong for
    EVERY file; discovered inside a lookup it surfaces mid-run, on whichever
    file happened to be resolved first, as that file's bad luck. Constructing
    this raises at load, before a run starts and before a single lookup is
    spent against a map nobody can trust. See `_alias_index` for the three
    mistakes refused and why each is refused rather than resolved.

    An empty or absent map is a real answer, not an error: an operator who has
    registered no alias has a valid, empty index.
    """

    __slots__ = ("_index",)

    def __init__(self, mapping=None):
        self._index = _alias_index(mapping)

    def full_name(self, folder):
        """The name `folder` is registered as an alias for, else None."""
        key = spaceless(folder)
        if not key:
            return None
        return self._index.get(key)

    def __len__(self):
        return len(self._index)

    def __eq__(self, other):
        if not isinstance(other, Aliases):
            return NotImplemented
        return self._index == other._index

    def __repr__(self):
        # The keys are an operator's own folder names and the values are
        # people's names, so neither belongs in a log line or a traceback.
        return "Aliases(%d entries)" % (len(self._index),)


def _alias_name(folder, aliases):
    """The full name `folder` is an operator-registered alias for, else None.

    Matching is exact on the normalised, spaceless form, so "V-Crane",
    "v crane" and "vcrane" all reach the same entry whichever side wrote
    which -- that much is just spelling. Nothing further: no prefix matching,
    no edit distance, no deriving initials. "vc" does not become "Velvet
    Crane".

    Guessing what an abbreviation stands for is how files end up attributed
    to the wrong person, and the guess is invisible once made -- two
    creators sharing initials is not unusual, and neither answer looks wrong
    in a report. An operator adding one line to the alias map is cheap and
    is a decision someone actually made. So an unlisted abbreviation
    resolves to nothing, on purpose.

    A value that survives `_alias_index` is returned as written and is not
    put through `_is_name`: the operator wrote it deliberately, and a real
    name that happens to be two characters long is theirs to declare. The
    guards judge names the code inferred, not names a human supplied.

    A plain mapping is indexed here and thrown away, which is correct for one
    call and wasteful for a scan -- see `Aliases`.
    """
    if not isinstance(aliases, Aliases):
        aliases = Aliases(aliases)
    return aliases.full_name(folder)


def resolve(name, folder, aliases=None, *, owners_of=None):
    """Attribute the file `name` sitting in `folder` to a creator.

    `folder` is a folder *name* -- normally whatever `creator_folder`
    returned for the file's path -- not a path, and may be empty. `aliases`
    maps an as-filed folder name to the full name it stands for; a malformed
    one raises `ValueError` (see `_alias_index`) rather than resolving.

    `aliases` is either an `Aliases` or a plain mapping this builds one from.
    Both say the same thing and answer identically; only what they cost
    differs, and the difference is per FILE. A caller resolving more than one
    file -- which is every caller that matters -- builds one `Aliases` and
    passes it to all of them, and gets the malformed-map failure at load
    rather than on an arbitrary file mid-run. The mapping is accepted because
    a single call has nothing to amortise and should not have to say so.

    `owners_of`, when given, is a one-argument callable: `owners_of(name)`
    runs a real catalogue search for `name` and returns the owner attributed
    to each of its results (see `cronicled.scan.examine`, the caller that
    builds one from the same `search` it already has). It is the ONLY thing
    that can make this function issue a lookup, and it is asked at most once
    per plausible candidate a file's folder and filename actually disagree
    about -- see `_resolve_by_evidence`. Omitted (the default), `resolve`
    stays exactly the pure function it always was: no lookup is possible, so
    ambiguity falls back to the folder-wins default described below.

    Tried in order: an alias on the folder, then -- with `owners_of` given --
    whichever candidate the catalogue actually supports (see
    `_resolve_by_evidence`), then the folder itself, then the filename. The
    folder beats the filename by default because someone chose to file the
    video there; that is a more deliberate signal than a name typed into a
    filename, which is as often the title, the site or the guest. That
    default is exactly what `owners_of` exists to override when it disagrees
    with the catalogue: a folder that names a store rather than the person
    who made the clip passes `_is_name` just as cleanly as the creator's own
    name does, and nothing about the text alone tells the two apart. Each
    candidate has to survive `_is_name` (and, for a `feat` marker,
    `_featured_name`) or it is not a name at all.

    When the folder and the filename both name someone, and they are not the
    same person by `_same_name` (so "Velvet Crane" and "velvetcrane" agree,
    while "Ivy" and "Ivy Kingsley Waters" do not), the winner's rival is
    returned in `competing` -- the folder's rival when `owners_of` is absent
    or only one candidate is on offer, or whichever plausible name actually
    lost the evidence check otherwise. When the folder yields text that no
    guard will accept as a name, it is returned in `rejected_folder` instead.
    Neither is ever silently dropped -- see `Resolution`.

    Returns a `Resolution`; all of its fields are None (and `unconfirmed`
    empty) when nothing resolved and there was no folder to reject, which is
    a real answer and not an error -- and also the answer when `owners_of`
    found no candidate at all to check. When `owners_of` found candidates but
    could not settle on exactly one supported, `unconfirmed` carries them
    instead of leaving that indistinguishable from "nothing to check"; see
    `_resolve_by_evidence` and `Resolution`'s own docstring.
    """
    folder_text = clean_folder(folder or "")
    from_filename = _filename_candidate(name)
    folder_is_name = _is_name(folder_text)
    unconfirmed = ()

    aliased = _alias_name(folder_text, aliases)
    if aliased is not None:
        resolved, source = aliased, "alias"
        competing = (from_filename if from_filename is not None
                     and not _same_name(resolved, from_filename) else None)
    elif owners_of is not None:
        resolved, source, competing, unconfirmed = _resolve_by_evidence(
            folder_text, name, owners_of)
    elif folder_is_name:
        resolved, source = folder_text, "folder"
        competing = (from_filename if from_filename is not None
                     and not _same_name(resolved, from_filename) else None)
    elif from_filename is not None:
        resolved, source, competing = from_filename, "filename", None
    else:
        resolved, source, competing = None, None, None

    # The folder had something to say and it was not used: say so, or the
    # filename's answer looks unopposed when it is not. Not when the folder
    # itself passed `_is_name`, though -- it competed and lost (either to the
    # OLD unconditional folder-wins rule never applying, or, with
    # `owners_of`, to a candidate the catalogue actually supported) rather
    # than failing a guard, so it belongs in `competing`, not here.
    rejected_folder = folder_text or None
    if source in ("folder", "alias") or folder_is_name:
        rejected_folder = None
    return Resolution(resolved, source, competing, rejected_folder, unconfirmed)
