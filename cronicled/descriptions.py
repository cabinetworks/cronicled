"""Descriptions that arrived carrying their own markup, and the cleaned text
to put in their place.

Measured before it was designed: of 651 performers on a real library, THREE
carry markup or an escaped entity in their description. That number is the
whole shape of this module. A proposer that reports one false positive
immediately outnumbers half of what it correctly finds, so detection here is
built to be CERTAIN rather than eager — a `<` or an `&` on its own is not
evidence of anything, because both occur in ordinary prose ("if a < b", "R&D",
"AT&T"), and a rule that treated them as evidence would produce a page of
plausible-looking proposals that silently delete text.

Two faults, and they are not the same thing:

* **markup** — a tag that was meant to be rendered and is being displayed
  literally;
* **entities** — an escape displaying as itself rather than as the character
  it names.

Why this does not use `cronicled.text.strip_html`
-------------------------------------------------
There IS existing HTML stripping in this project and it is deliberately not
reused here, because it is built for the opposite job. `strip_html` sanitises
scraped scene text on the way to the media server, where being eager is nearly
free: the alternative is raw HTML landing on the server, and nobody is
reviewing the result. Three of its properties make it wrong for a REVIEWED
proposal about a person's description:

1. Its tag pattern is `<[^>]+>`, which matches any `<` followed by any later
   `>`. `"if a < b and c > d then"` becomes `"if a   d then"` — four words
   deleted from prose that had no markup in it at all. As a detector it would
   fire on that sentence; as a cleaner it would delete it.
2. It unescapes through `html.unescape`, which decodes HTML5 named references
   with NO terminating semicolon: `"me&not you"` becomes `"me¬ you"`.
   Ordinary prose is full of `&` followed by a word.
3. It collapses every run of whitespace, newlines included, so a bio with
   paragraphs comes back as one line. That is a silent rewrite of text nobody
   asked about.

It also strips backslash escapes, a third fault this ticket did not ask about.
So `strip_html` stays exactly where it is, doing exactly what it does, and
this module answers a different question with its own closed vocabularies.

Which way this fails
--------------------
Toward leaving the text alone, every time. A description this cannot
confidently clean produces NO proposal: `assess` reports the fault and
withholds the replacement. Removing MORE is not the safe direction — a
reviewer looking at a proposal that has quietly deleted a sentence has no way
to see what is missing, whereas a description left with `<p>` in it looks
exactly as wrong as it already did.
"""
import re
from dataclasses import dataclass

# Proposals about a description are keyed to a subject that is not a scene.
# Named rather than inlined so this module, the store, the apply path and a
# test all agree on one string.
SUBJECT_TYPE = "performer"

# The media server's own field name for a performer's description. The one
# place it is written, so the read query, the write mutation and the undo
# snapshot cannot drift apart into two spellings of one field.
FIELD = "details"

# What `assess` reports having found. Two constants rather than two literals,
# for the reason `scan`'s mute reasons are constants: a caller, a payload and
# a test cannot drift apart, and collapsing them into one catch-all has to be
# done deliberately.
FAULT_MARKUP = "markup"
FAULT_ENTITY = "entity"

# Why a fault was found and no replacement offered. Each is a case where a
# confident answer does not exist, and inventing one would write over a
# person's description with a guess.
REFUSE_DOUBLE_ENCODED = (
    "the description is doubly encoded: cleaning it once leaves text that "
    "still reads as markup or as an escape, and there is no way to tell an "
    "escape that should be decoded from text about markup that should be "
    "left exactly as it is")
REFUSE_UNSAFE_REFERENCE = (
    "the description carries a numeric character reference that does not name "
    "a printable character")
REFUSE_NOTHING_LEFT = (
    "the description is markup and whitespace only, so cleaning it would "
    "leave the field empty")

# The tag names this module recognises. A CLOSED list, and the closure is the
# guard: `<[^>]+>` matches ordinary prose spanning a `<` and a later `>` (see
# this module's docstring), so what makes a match evidence is that the name
# between the brackets is one somebody actually writes markup with.
#
# `script`, `style` and `iframe` are deliberately absent. Their content is not
# prose, and removing the tags while keeping what sits between them would
# produce a "cleaned" description made of code. A description carrying one is
# therefore not detected at all, which leaves it exactly as it is — the
# direction this module fails in.
_TAG_NAMES = (
    "a", "abbr", "article", "aside", "b", "blockquote", "br", "code", "dd",
    "div", "dl", "dt", "em", "font", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "i", "img", "li", "nav", "ol", "p", "pre", "q", "s",
    "section", "small", "span", "strike", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
)

# Longest first so `<pre>` cannot be read as `<p>` followed by junk. (The
# engine would backtrack into the right answer anyway; ordering says so
# without relying on that.)
_TAG = r"</?(?:%s)(?:\s[^<>]*)?/?>" % "|".join(
    sorted(_TAG_NAMES, key=len, reverse=True))

_TAG_RE = re.compile(_TAG, re.IGNORECASE)

# A tag, with the horizontal whitespace it touches, so removing it can leave
# exactly one space behind instead of two — and so a run of adjacent tags
# (`</p><p>`) leaves one space rather than one per tag. `[ \t]` is
# horizontal whitespace only: newlines are structure and are never absorbed.
_TAG_RUN_RE = re.compile(r"[ \t]*(?:(?:%s)[ \t]*)+" % _TAG, re.IGNORECASE)

# The named entities this module recognises, and the character each names.
# Closed and lowercase, for the same reason `_TAG_NAMES` is closed: `&` is a
# word in ordinary prose. `html.unescape` is not used anywhere here — it
# decodes references with no terminating semicolon, so `"me&not you"` becomes
# `"me¬ you"`.
_NAMED = {
    "amp": "&",
    "apos": "'",
    "bull": "•",
    "copy": "©",
    "deg": "°",
    "gt": ">",
    "hellip": "…",
    "ldquo": "“",
    "lsquo": "‘",
    "lt": "<",
    "mdash": "—",
    "middot": "·",
    "ndash": "–",
    "nbsp": " ",
    "quot": '"',
    "rdquo": "”",
    "reg": "®",
    "rsquo": "’",
    "trade": "™",
}

_ENTITY_RE = re.compile(
    r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|%s);" % "|".join(sorted(_NAMED)))

# Horizontal whitespace sitting either side of a line break. Removed so that
# a tag replaced by a space at the end of a line does not leave the line ending
# in one. The newline itself is kept: paragraph structure is content.
_EDGE_RE = re.compile(r"[ \t]*\n[ \t]*")


@dataclass(frozen=True)
class Cleanup:
    """What examining one description concluded.

    Three outcomes, and the difference between the last two is the point of
    this type existing at all:

    * `assess` returns `None` — nothing is wrong with this description. The
      overwhelmingly common answer (648 of 651 on the library this was
      measured against).
    * a `Cleanup` with `cleaned` set — a fault was found AND there is a
      replacement this module is confident about. `cleaned` is the whole
      field, never a fragment.
    * a `Cleanup` with `cleaned` as `None` — a fault was found and no
      replacement is offered. `reason` says which of the three refusals it
      was. Conflating this with "nothing wrong" would lose the only record
      that anything was noticed; conflating it with a proposal would write a
      guess over a person's description.

    `faults` names what was found, as a tuple of `FAULT_MARKUP` /
    `FAULT_ENTITY`, in that order — a description can carry either or both.
    """

    original: str
    faults: tuple
    cleaned: str = None
    reason: str = ""


def _faults(text):
    """Which of the two faults `text` carries, as a tuple.

    Asked of the ORIGINAL to decide whether there is anything to do, and
    asked again of the cleaned result to decide whether the answer is stable
    — see `assess`. One function for both so the two questions cannot be
    answered by two different vocabularies.
    """
    found = []
    if _TAG_RE.search(text):
        found.append(FAULT_MARKUP)
    if _ENTITY_RE.search(text):
        found.append(FAULT_ENTITY)
    return tuple(found)


def _character(reference):
    """The single character a `&...;` reference names, or `None` when this
    module will not vouch for it.

    `None` for a numeric reference outside Unicode, for a surrogate, and for
    anything not printable — a control character, a zero-width joiner, a
    line separator. Each of those would be written into a person's
    description as an invisible edit, and "the escape was displaying wrongly"
    is not enough to justify one. A named reference is looked up in `_NAMED`
    and can only be one of the characters listed there.
    """
    body = reference[1:-1]          # strip the leading & and trailing ;
    if not body.startswith("#"):
        return _NAMED[body]
    digits = body[1:]
    try:
        if digits[:1] in ("x", "X"):
            code = int(digits[1:], 16)
        else:
            code = int(digits, 10)
        char = chr(code)
    except (ValueError, OverflowError):
        return None
    return char if char.isprintable() else None


def _decode(text):
    """`text` with every recognised entity replaced by the character it
    names, or `None` when one of them is a reference this will not vouch for.

    ONE pass. The result is never re-scanned for further entities: a value
    that decodes into another escape is the double-encoded case, and it is
    settled by `assess`'s stability check rather than by decoding until
    nothing changes — which would silently pick one of the two readings.
    """
    out = []
    at = 0
    for match in _ENTITY_RE.finditer(text):
        char = _character(match.group(0))
        if char is None:
            return None
        out.append(text[at:match.start()])
        out.append(char)
        at = match.end()
    out.append(text[at:])
    return "".join(out)


def _tidy(text):
    """Whitespace, after tags have been removed from `text`.

    Horizontal whitespace either side of a line break is dropped and the
    whole value is stripped; NEWLINES ARE KEPT, exactly as many and exactly
    where they were. That is the difference from `cronicled.text.strip_html`,
    which collapses them and turns a bio with paragraphs into one line.

    Nothing visible is deleted: every rule here removes whitespace that is
    either invisible where it sits (before a newline, at either end) or was
    put there by a removed tag.
    """
    return _EDGE_RE.sub("\n", text).strip()


def assess(description):
    """What, if anything, to propose for one description.

    `None` when there is nothing wrong with it — including for `None` and for
    the empty string, which are a performer with no description rather than a
    faulty one. Otherwise a `Cleanup`; see that class for the three-way split
    and for why "found a fault, offering no replacement" is its own answer
    rather than silence.

    THE ORDER, and it is a decision rather than an accident: tags are removed
    FIRST, and entities are decoded ONCE, afterwards, over the result.
    Decoding first would let `&lt;p&gt;` become `<p>` and then be stripped as
    though it had been markup all along — deleting text that was rendering
    exactly as its author encoded it.

    THE DOUBLE-ENCODED CASE — an entity encoding a tag, `&lt;b&gt;bold&lt;/b&gt;`
    — RESOLVES TO NO PROPOSAL. Stated here rather than left to fall out of the
    order above, because both available answers are ones this project's own
    rules rule out:

    * decode and then strip (entities first) gives `bold`, which has deleted
      four visible characters that were displaying correctly. Over-stripping
      is not the safe direction: the reviewer sees a plausible proposal and
      cannot see what is gone.
    * decode and stop (tags first, which is what this does) gives the literal
      text `<b>bold</b>`, which THIS SAME FUNCTION would report as markup on
      the next run and propose stripping. A cleanup whose own output is a
      fault is a proposal that comes back every night, and approving it twice
      reaches the deletion above by a longer road.

    So the rule is stated as a property instead of as an ordering: **a
    proposal is offered only when the cleaned value is a fixed point** — when
    running this over the result would find nothing further. That is checked
    directly, below, and it settles `&amp;amp;` (which decodes to `&amp;`) by
    the same rule and for the same reason, without either case being named in
    the code. A test that runs the two cleanups in the other order fails
    against it, because the other order produces a value where this produces
    none.

    The other two refusals are the same instinct applied to two narrower
    facts: a numeric reference that does not name a printable character
    (`_character`), and a description that is markup and whitespace only, for
    which "cleaned" would mean "blank".
    """
    if not description:
        return None
    faults = _faults(description)
    if not faults:
        return None

    decoded = _decode(_TAG_RUN_RE.sub(" ", description))
    if decoded is None:
        return Cleanup(original=description, faults=faults,
                       reason=REFUSE_UNSAFE_REFERENCE)
    cleaned = _tidy(decoded)
    if not cleaned:
        return Cleanup(original=description, faults=faults,
                       reason=REFUSE_NOTHING_LEFT)
    if _faults(cleaned):
        return Cleanup(original=description, faults=faults,
                       reason=REFUSE_DOUBLE_ENCODED)
    return Cleanup(original=description, faults=faults, cleaned=cleaned)


# --- the producer ----------------------------------------------------------
#
# The second producer in this project, and deliberately the same SHAPE as the
# first (`cronicled.scan.ScanProducer`): a `name`, a `cost`, a declared
# `every`, and a `produce(ctx)` generator yielding whole proposals for the
# runner to record. What it does NOT copy from the scan is the machinery the
# scan needs and this does not — see `DescriptionProducer` for why there is no
# `select` here, no worker pool and no store.

# The name this producer is registered and scheduled under. Fixed rather than
# per-instance: unlike a scan, nothing here is parameterised by a limit a
# person typed, so there is no second registration for the same work and no
# way for a manual run to replace a scheduled one.
PRODUCER_NAME = "performer-descriptions"


class DescriptionProducer:
    """Proposes the cleaned text for every performer description carrying
    markup or an escaped entity.

    ONE read of the media server for the whole library, and no lookup against
    anything else — the fault is visible in the text itself, so nothing needs
    to be searched for, scored or confirmed. That is why this is `local` and
    not `scraping`: `COST_CLASS_LIMITS` rations `scraping` to one job because
    it drives a headless browser inside the media server, and nothing here
    does. Running unlimited alongside a scan is the point; queueing this
    behind a twenty-minute scrape would be a limit protecting nothing.

    NO `select`, and its absence is a decision rather than an omission.
    `cronicled.scan.select` exists to ration a rate-limited lookup PER FILE:
    dropping a subject before it is examined is what stops a scan spending its
    budget re-deciding files a reviewer has already seen. Here every
    description arrives in the single read that already happened, so
    examining one costs nothing that dropping it would save. The store's own
    rules still hold, unchanged and in one place: `Store.record` refuses a
    proposal for a muted subject and refuses one a reviewer has dismissed, and
    an unchanged description produces the same fingerprint and touches the
    existing row rather than making a second.

    NO worker pool either, for the same kind of reason: `assess` is a regex
    over a short string, and the whole library's descriptions are one list in
    memory by the time the first one is examined.

    NO `store`: the runner holds it and records what this yields (see
    `cronicled.jobs.JobRunner`). A scan needs one of its own because it asks
    the store questions BEFORE it works a file, and because it writes mutes
    and refusals; this asks nothing and writes neither.

    Refusals are counted and reported in the closing log line rather than
    written to the store as standing refusals. A `Store.record_refusal` row
    per performer would put hundreds of entries in front of a reviewer to
    describe a handful of genuinely undecidable descriptions — and the two
    facts a person actually wants ("how many were looked at" and "how many
    could not be settled") fit in the one message the runner keeps.
    """

    name = PRODUCER_NAME
    cost = "local"

    def __init__(self, stash, *, folder="library", every=None, at=None,
                 zone=None):
        self._stash = stash
        self._folder = folder
        # The cadence this producer DECLARES, read off the object by
        # `cronicled.schedule.resolve`. Set unconditionally, so a producer's
        # cadence is a value that was decided rather than an attribute that
        # happens to be missing — `resolve` refuses an enabled producer with
        # no cadence at start-up, which is the whole reason this is here and
        # not defaulted to something.
        self.every = every
        # The other way of saying when: a stated time of day (`at`) read in a
        # named zone (`zone`), read off this object by the same `resolve` and
        # set unconditionally for the same reason. An unattended pass declares
        # these instead of `every`, because an interval measured from the last
        # run drifts to whatever hour the process last restarted at — see
        # `cronicled.__main__` for the three appointments and why they differ.
        self.at = at
        self.zone = zone

    def produce(self, ctx):
        """Yield one proposal per description this can confidently clean.

        Every performer is examined; only the ones with something wrong that
        has a confident answer are yielded. The closing line reports all four
        counts, because "0 proposed" alone reads identically for a library
        with clean descriptions and for one where every faulty description was
        refused as undecidable, and those call for opposite responses.
        """
        performers = self._stash.performers_with_descriptions()
        ctx.log("looking at %d performer descriptions" % len(performers))
        proposed = refused = 0
        for performer in performers:
            # Indexed, not `.get`: `performers_with_descriptions` selects both
            # fields on every row it returns, so a row missing either is a
            # malformed payload rather than a performer with no description
            # (which is `details` being None, an ordinary answer `assess`
            # already reads as "nothing wrong").
            cleanup = assess(performer[FIELD])
            if cleanup is None:
                continue
            if cleanup.cleaned is None:
                refused += 1
                ctx.log("performer %s: %s"
                        % (performer["id"], cleanup.reason))
                continue
            proposed += 1
            yield proposal(performer, cleanup, folder=self._folder)
        ctx.log("finished: %d proposed, %d could not be cleaned confidently, "
                "%d descriptions looked at"
                % (proposed, refused, len(performers)))


def proposal(performer, cleanup, *, folder):
    """One store proposal for one description, whole.

    The payload carries BOTH texts. That is the entire review: a row showing
    only the cleaned value gives a reviewer nothing to judge it against, and
    they would be approving a write to a field whose previous contents they
    cannot see. It is also what `cronicled.web.actions.Actions.approve` checks
    the server's current value against before writing, so a description edited
    between the scan and the click is refused rather than overwritten with
    text derived from what it used to say.

    `confidence` is `None`, deliberately, and it is not the same thing as
    zero. Nothing here is scored — the fault is either present in the text or
    it is not — so any number would be one this producer invented and then
    displayed in the same column, in the same type, as numbers the scorer
    really produced. `Store.record` documents `None` as a legitimate value for
    exactly this.
    """
    return {
        "folder": folder,
        "subject_type": SUBJECT_TYPE,
        "subject_id": str(performer["id"]),
        "summary": "%s: description contains %s" % (
            performer["name"], " and ".join(cleanup.faults)),
        "confidence": None,
        "payload": {
            "name": performer["name"],
            "field": FIELD,
            "faults": list(cleanup.faults),
            "original": cleanup.original,
            "cleaned": cleanup.cleaned,
        },
    }
