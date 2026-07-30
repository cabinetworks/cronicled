"""stash-box client: enumerate what a source *lists*, rather than what matches.

Every scraper action the media server offers answers "what matches this?".
None answers "what does the source list?" — which is the question behind a
refusal that means anything. stash-box's `queryScenes` takes a performer
criterion and reports a `count` alongside the page, so every entry a source
lists for a performer can be read and *known* to have been read in full.

What that establishes, and what it does not, is this module's whole care.
stash-box is a **contributor-submitted index**: it holds what somebody took
the trouble to enter, which for most performers is a subset — often a small
one — of what they actually released. So a complete read establishes that
*this source lists no entry* matching a file. It never establishes that no
such scene exists, and a file missing from the listing has most likely just
never been submitted. Nothing here — type name, field name, docstring or
reason string — may read as though the performer's body of work had been
read, because the person acting on it cannot check that from the sentence.

Reading the listing in full is still the entire point, and the reason
`SourceListing.complete` exists as a separate fact from the scenes
themselves: a read that stopped early is not evidence of anything, and must
never be reported as though it were.

The other question worth asking a source is "do you already have this exact
file?", which `findScenesBySceneFingerprints` answers for a whole batch of
hashes at once. Its answers are a different kind of evidence from a title
match and are kept in a different type for that reason: a hash *identifies*,
a title only *scores*, and the two must not end up in the same field.

Like `cronicled.stash`, every call goes through an injected transport, so the
whole surface is testable without a network.
"""

from cronicled.censorship import decensor
from cronicled.scoring import DEFAULT_THRESHOLD, decide, score
from cronicled.stash import DEFAULT_TIMEOUT, Stash, StashError

# stash-box's own default is 25. A listing read is a whole-listing read, so it
# pays for itself in round trips saved.
PER_PAGE = 100

# A bound on a read that would otherwise be unbounded. At PER_PAGE that is
# 10,000 scenes for one performer — far past any listing a real source holds,
# which is the point: it is here to stop a runaway, not to trim a large but
# honest read. Hitting it makes the read incomplete, and an incomplete read
# can never be reported as an absence.
MAX_PAGES = 100

PERFORMER_SCENES = """
query($input: SceneQueryInput!) {
  queryScenes(input: $input) {
    count
    scenes { id title date urls { url } }
  }
}
"""


# The endpoint takes a LIST of fingerprint sets and answers with one block of
# scenes per set, in the order they were submitted. That is the whole reason
# it exists: a scan holding hundreds of hashes asks once. Asking per
# fingerprint instead is a rate-limit incident against a public service, and
# nothing about the answers would look any different, which is why the batch
# is pinned by a test rather than by intent.
SCENES_BY_FINGERPRINT = """
query($fingerprints: [[FingerprintQueryInput!]!]!) {
  findScenesBySceneFingerprints(fingerprints: $fingerprints) {
    id title date urls { url }
  }
}
"""

# The tag catalogue behind a box's own tag browser. `queryTags` is the only
# query here that is not about a scene, and it is a WHOLE-CATALOGUE read for
# the same reason `PERFORMER_SCENES` is a whole-listing read: the question is
# "what does this source hold?", which a per-tag lookup cannot answer without
# one request per tag. A library has thousands of tags and a box has thousands
# more; asking per tag is a rate-limit incident against a public service, and
# the answers would be identical.
#
# `description` is the field this exists for, `aliases` is what makes it
# findable -- measured against a real library, matching on names alone found
# far fewer than matching on the alias keys as well.
BOX_TAGS = """
query($input: TagQueryInput!) {
  queryTags(input: $input) {
    count
    tags { id name description aliases }
  }
}
"""

# The algorithms stash-box's `FingerprintAlgorithm` enum accepts, spelled its
# way. Checked here rather than left to the server because the enum is
# case-sensitive and unknown values are rejected at parse time, which fails
# the WHOLE batch: one typo would take every well-formed fingerprint beside it
# down. Both halves of a fingerprint are strings, so this is also the only
# thing that can notice a transposed pair.
FINGERPRINT_ALGORITHMS = ("MD5", "OSHASH", "PHASH")


def _checked_fingerprint(fingerprint):
    """`(algorithm, hash)`, or `ValueError`.

    A malformed entry is never quietly dropped from the batch: the caller
    would get back a mapping with no key for a fingerprint it did submit,
    which reads as "never asked" about something that was asked. Raising is
    the visible failure; skipping is the silent one.
    """
    try:
        algorithm, value = fingerprint
    except (TypeError, ValueError):
        raise ValueError("a fingerprint must be an (algorithm, hash) pair, got %r"
                         % (fingerprint,))
    if algorithm not in FINGERPRINT_ALGORITHMS:
        raise ValueError("unknown fingerprint algorithm %r — the source accepts %s"
                         % (algorithm, ", ".join(FINGERPRINT_ALGORITHMS)))
    if not isinstance(value, str) or not value:
        raise ValueError("fingerprint %s has no hash: %r" % (algorithm, value))
    return (algorithm, value)


class FingerprintHit:
    """A scene the source says carries a fingerprint that was submitted.

    Deliberately **not** a `cronicled.scoring.Match`, and deliberately without
    a `value`. A hash match *identifies*; a title match *scores*, and the two
    are different kinds of evidence. The legacy tool wrote a flat `1.0` for a
    fingerprint hit into the same field a computed similarity goes in, after
    which nothing downstream could tell an identity from a very good guess —
    and every threshold, margin and ambiguity rule that reads that field was
    silently being asked to arbitrate between the two.

    `algorithm` rides on the hit rather than being left to the key it came
    from, because the claim differs by algorithm and a hit gets separated from
    its key the moment a caller pools the hits for one file: an `OSHASH` match
    says *these are the same bytes*, a `PHASH` match only says *these look
    alike*.
    """

    def __init__(self, scene, algorithm, hash):
        self.scene = scene
        self.algorithm = algorithm
        self.hash = hash

    def __repr__(self):
        return "FingerprintHit(scene=%r, algorithm=%r, hash=%r)" % (
            self.scene.get("id"), self.algorithm, self.hash)


class SourceListing:
    """The entries a source lists for a performer, and whether that is all of
    the entries it lists.

    Named for the *source*, not for the performer, and that is not a
    stylistic preference. The scenes here are what contributors submitted to
    this one index; they are not the performer's catalogue, output or body of
    work, and a type called after any of those invites every caller and every
    message downstream to claim something the read cannot support. A whole
    listing is a subset of a career, and usually a small one.

    `complete` is the field that carries the weight: only a `True` licenses a
    caller to say a file is not listed here. It is kept beside the scenes
    rather than inferred from their length, because the length of a partial
    read and the length of a complete one look exactly alike. It means every
    entry this source holds for the performer was read — nothing more.

    `total` and `pages_read` are what let an incomplete read be reported as
    partial rather than merely disqualifying: `total` is the entry count the
    source reported (see `performer_listing`'s own docstring for which of
    possibly several reported counts this is, and why), and `pages_read` is
    how many page requests it took to get here. Neither changes what a
    partial read may claim -- `complete=False` still blocks every claim
    `listing_verdict` would otherwise make -- they only say how partial: "3
    of 9" is something a person can act on (raise the page cap, try later),
    "3" alone is only a reason to stop trusting the read.
    """

    def __init__(self, performer_id, scenes, complete, *, total, pages_read):
        self.performer_id = performer_id
        self.scenes = tuple(scenes)
        self.complete = complete
        self.total = total
        self.pages_read = pages_read

    def __repr__(self):
        return ("SourceListing(performer_id=%r, scenes=%d, complete=%r, "
                "total=%r, pages_read=%r)" % (
                    self.performer_id, len(self.scenes), self.complete,
                    self.total, self.pages_read))


class TagCatalogue:
    """The tags a source holds, and whether that is all of them.

    `complete` is a separate fact from the tags for exactly the reason
    `SourceListing.complete` is: a read that stopped early and a read that
    finished look identical from the length of what came back, and the
    difference decides what may be concluded from a tag NOT being here.

    A description found in a partial read is still found -- a page that was
    never fetched cannot un-find a tag already in hand. What a partial read
    may not support is the negative: "no configured source describes this
    tag" is a claim about the whole catalogue, and a caller that counted an
    unread page as an absence would report a backlog of tags nothing can
    help with, when the help was on the page it did not read.
    """

    def __init__(self, tags, complete):
        self.tags = tuple(tags)
        self.complete = complete

    def __repr__(self):
        return "TagCatalogue(tags=%d, complete=%r)" % (len(self.tags),
                                                       self.complete)


class Verdict:
    """Whether this source lists a file, and why that is the answer.

    `unlisted` is three-valued, and that is the whole point of the type.
    `True` and `False` are claims about the source's listing; `None` is the
    honest answer when the evidence supports neither. Collapsing `None` into
    `False` would make "we could not tell" indistinguishable from "it is
    there".

    The field is `unlisted` rather than `absent` because it is read on its
    own — in a log line, in a review comment, beside a filename — where
    "absent" takes whatever object the reader has in mind, and the object
    they have in mind is the performer's work. `unlisted` names the listing
    as the subject and cannot be read as a claim about the world: an entry
    nobody ever submitted is unlisted here and is not absent anywhere. The
    scheme is unchanged; only the word that travels with the value is.

    The asymmetry with the rest of this project is worth stating: elsewhere a
    wrong answer costs a wrong write, which a later run can correct. This one
    is read by a person and acted on by a person, and a reviewer sent to look
    for a mis-filing that does not exist has no undo. That is also why the
    reason for `unlisted=True` carries the limit of the evidence inside the
    sentence rather than leaving it to these docs: the sentence is what gets
    quoted, and it is quoted without them.

    `reason` is the part that actually gets read, so it carries the same
    rules: it never claims a completeness that was not achieved, and never a
    reach the source does not have.
    """

    def __init__(self, unlisted, reason):
        self.unlisted = unlisted
        self.reason = reason

    def __repr__(self):
        return "Verdict(unlisted=%r, reason=%r)" % (self.unlisted, self.reason)


def listing_verdict(listing, decision, *, performer_id, attribution_certain=True):
    """What `listing` and `decision` together are allowed to claim about a file.

    `listing` is a `SourceListing`; `decision` is a
    `cronicled.scoring.Decision` made over candidates drawn from it.
    `performer_id` names the performer `decision`'s candidates were actually
    scored against, and must equal `listing.performer_id` — a mismatch
    refuses outright rather than reporting a verdict at all. Nothing about a
    bare `Decision` says which performer's scenes it was scored over, so
    without this a caller that mixed up which listing goes with which
    decision — the wrong pair pulled off two ends of a batch, say — would get
    back a confident answer naming the performer `listing` carries, over
    candidates actually drawn from somebody else's. The check costs little
    because the information was already here: every reason string below
    already names `listing.performer_id`, so asking the caller to also state
    which performer it scored against, and comparing the two, is the whole
    fix.
    `attribution_certain` says whether the performer whose listing was read is
    agreed to be this file's creator — a caller working from
    `cronicled.artist.resolve` computes it as `resolution.competing is None
    and resolution.rejected_folder is None`. `competing` is the resolver's
    report that the folder and the filename did not name different people;
    `rejected_folder` is its report that the folder's own text never
    competed at all because a guard threw it out first. That second signal
    matters here for the same reason the first one does: `_is_name`'s guards
    are heuristics tuned against real filing conventions, not a proof that
    the rejected text names nobody, so an attribution resting on the
    filename after the folder was thrown out is not the same as the folder
    and the filename having been checked and found to agree. Reading only
    `competing` treats the two as the same thing and calls the weaker one
    settled.

    The strongest thing obtainable here is *this source does not list it*. It
    is worth obtaining: a scorer must always pick a winner from what it is
    handed, and against a real library it applied a wrong entry 6% of the time
    when the right one was not in the candidate list at all. A read that is
    known to be whole is the only evidence that separates those cases, and it
    is the most useful signal available. It is still evidence about an index
    somebody filled in by hand, never about what a performer released.

    Five things have to hold before a file may be called unlisted, and each
    one that fails downgrades the claim rather than weakening it:

    * **the attribution is not contested.** An enumeration of the wrong
      performer's listing answers a question about the wrong person, and
      answering it confidently is worse than not answering. This blocks
      `False` as well as `True`: a title match found in a listing that may
      belong to someone else is a wrong identification, which is the exact
      failure the resolver's disagreement is warning about.
    * **the scorer did not find it.** A decided match is a presence, and it
      stays a presence whether or not the read finished — a page that was
      never fetched cannot un-find an entry already in hand. Completeness
      gates only the negative claim.
    * **the refusal was for want of a candidate, not a surplus of them.** A
      refusal with contenders is a dilemma: entries that look like this file
      are in the listing, and reporting that as an absence would send a
      reviewer hunting a mis-filing while the candidates sit in the same
      reply. Read off `Decision.contenders` and never off `decision.reason` —
      `scan.py` states the rule and the reason for it.
    * **the listing was interrogated.** `contenders == 0` is also what a
      refusal that never weighed a single entry returns: a filename carrying
      no word that is not the artist's or generic is barred at any score, and
      a caller that offered no candidates asked nothing at all. Both come back
      looking exactly like "nothing in this listing is close", and an absence
      claimed from either is this function's own harm arriving through the
      door built to prevent it — a reviewer sent to hunt a mis-filing the
      scorer never looked for, against a listing it never questioned. The one
      exception is a listing holding nothing: there is no entry to weigh
      anything against, and the empty read is itself the evidence.
    * **the read finished.** The page that was never read is precisely where
      the matching entry would be.

    Ordering matters only for which reason a caller is shown, and it runs from
    the most fundamental defect outward: not knowing whose listing this is
    beats anything measured inside it, and evidence in hand (a match, a set of
    contenders) is more use to a reviewer than the fact that more pages exist.

    An un-interrogated listing is reported ahead of an unfinished read for a
    sharper reason than tidiness: "stopped early" invites the caller to read
    the rest, and reading the rest cannot change an answer that was never
    asked. That is a retry which can never come good, and naming it would be
    worse than saying nothing.
    """
    # A bool and nothing else. The mis-wiring this exists to catch is
    # `attribution_certain=resolution.competing`, where `competing` holds the
    # losing NAME when the attribution is contested — a truthy string, which
    # would switch the guard off by exactly the value that should switch it
    # on. Defaulting a non-bool to "contested" instead would be fail-closed
    # but silent, and would leave the mis-wired caller with a verdict that
    # simply never claims anything and no clue why.
    if attribution_certain is not True and attribution_certain is not False:
        raise TypeError(
            "attribution_certain must be True or False, got %r"
            % (attribution_certain,))

    # The transposition this exists to catch: a caller holding several
    # (listing, decision) pairs at once mismatches them, and every branch
    # below would still be reachable -- just never on the fact the file
    # actually needed. `listing_verdict` cannot see how `decision` was
    # produced, so the caller states it and this refuses rather than
    # silently judging one performer's listing against another's candidates.
    if performer_id != listing.performer_id:
        raise ValueError(
            "decision was scored against performer %r but listing is for "
            "performer %r -- refusing to judge one performer's listing "
            "against another's decision" % (performer_id, listing.performer_id))

    if not attribution_certain:
        return Verdict(None, (
            "the folder and the filename name different creators, so whose "
            "listing was read is unsettled and neither answer is available"))

    if decision.match is not None:
        return Verdict(False, (
            "this source's listing for performer %s has this file: %s"
            % (listing.performer_id, decision.reason)))

    if decision.contenders:
        return Verdict(None, (
            "%d entries in this source's listing for performer %s competed "
            "for this file and none of them won, so nothing here says it is "
            "missing: %s"
            % (decision.contenders, listing.performer_id, decision.reason)))

    # `listing.scenes` and not `listing.complete`: the exception is a listing
    # with nothing in it, which is the strongest evidence obtainable here and
    # the answer most worth acting on. A complete read of 500 entries that
    # were never weighed is not an exception, it is the defect.
    if not decision.interrogated and listing.scenes:
        return Verdict(None, (
            "performer %s's %d entries in this source's listing were never "
            "weighed against this file, so they are not grounds for calling "
            "it missing: %s"
            % (listing.performer_id, len(listing.scenes), decision.reason)))

    if not listing.complete:
        return Verdict(None, (
            "the read of this source's listing for performer %s stopped "
            "early after %d of %s scenes across %d page%s, so the file is "
            "not ruled out by what was not read"
            % (listing.performer_id, len(listing.scenes),
               listing.total if listing.total is not None else "an unknown",
               listing.pages_read,
               "" if listing.pages_read == 1 else "s")))

    # The one sentence in this module a person acts on, so it carries its own
    # limit: quoted into a ticket with none of these docs around it, it must
    # still say that the source holds only what was submitted to it. Without
    # that clause it reads as "this scene is not this performer's", which the
    # read cannot show and which sends a reviewer hunting a mis-filing that
    # was never there.
    return Verdict(True, (
        "this source's listing for performer %s was read in full (%d entries) "
        "and this file is not in it — but the listing holds only what "
        "contributors have submitted, so a file missing from it may simply "
        "never have been submitted: %s"
        % (listing.performer_id, len(listing.scenes), decision.reason)))


def check(box, performer_id, name, folder, resolution, *,
         threshold=DEFAULT_THRESHOLD, censorship=None, per_page=PER_PAGE,
         max_pages=MAX_PAGES, timeout=DEFAULT_TIMEOUT):
    """Read `performer_id`'s whole stash-box listing and say what that does
    and does not establish about `name`/`folder`.

    This is a SECOND opinion, not a re-check of whatever a site-adapter scan
    already decided: it scores `name`/`folder` against THIS source's own
    listing -- entries no site search ever saw -- using the exact same
    `cronicled.scoring` rules a scan already trusts. So its `Decision` is
    independent of, and may disagree with, whatever `cronicled.scan.examine`
    concluded for the same file against a different candidate set -- neither
    call is fed the other's result, and this one asks nothing about it.
    Deliberately never "a catalogue" here or anywhere downstream of it: see
    `SourceListing`'s own docstring for why that word is barred -- a listing
    is what contributors happened to submit, not a performer's output, and
    the two read very differently to whoever this is quoted to.

    `resolution` is the `cronicled.artist.Resolution` that named
    `performer_id`'s creator. Its `name` is subtracted from the evidence the
    same way `cronicled.scan.examine` subtracts it before scoring (see
    `scoring.score`'s `artist` argument), and its `competing` and
    `rejected_folder` are read together exactly the way `listing_verdict`
    documents: both `None` means the attribution is settled; a `competing`
    name means the folder and the filename named different people; a
    `rejected_folder` means the folder had text at all but a guard -- not a
    check against evidence -- is why it did not win, which is not the same
    as the folder having agreed. Either one downgrades the claim, because in
    either case the listing being enumerated may not even be this file's
    creator's.

    Returns a `Verdict` -- see `listing_verdict` for the whole of what
    `unlisted` may and may not be read to mean. Most importantly: `True`
    says only that THIS SOURCE'S LISTING has no matching entry -- not that
    the performer never made it, and not that the file does not exist
    anywhere. A file missing from a listing is at least as likely to mean
    "no contributor entered it" or "it is filed under someone else here" as
    it is to mean "it does not exist" -- `listing_verdict`'s own reason
    string carries that distinction into the one sentence a reviewer
    actually reads; nothing here may repeat the claim in a shorter, less
    careful form.

    Reading a whole listing is many pages against a rate-limited public
    service -- exactly the cost `cronicled.jobs.COST_CLASS_LIMITS["box"]`
    exists to ration. Nothing here enforces that; it is the caller's job to
    run this only from inside a producer registered with `cost="box"` (see
    `cronicled.stashbox_scan.StashBoxCheckProducer`), never inline inside a
    `scraping`-classed scan, where it would spend a second rate-limited
    service's budget under a job the "box" limit never counts.
    """
    listing = box.performer_listing(performer_id, per_page=per_page,
                                    max_pages=max_pages, timeout=timeout)
    matches = [score(name, folder, decensor(scene.get("title") or "", censorship or {}),
                     artist=resolution.name)
               for scene in listing.scenes]
    decision = decide(matches, threshold)
    # Both signals, not just the one a competing NAME sets: a folder whose
    # text a guard threw out never competed either, and the guard is a
    # heuristic, not a check against evidence -- see `listing_verdict`'s own
    # docstring for why treating that as settled is the same mistake as
    # ignoring `competing` outright.
    attribution_certain = (resolution.competing is None
                           and resolution.rejected_folder is None)
    return listing_verdict(listing, decision, performer_id=performer_id,
                           attribution_certain=attribution_certain)


def base_url(endpoint):
    """A configured stash-box endpoint, reduced to what `StashBox` takes.

    The media server stores a box's address as the GraphQL endpoint itself
    (`.../graphql`), because that is what it POSTs to. `StashBox` inherits
    `Stash`'s constructor, which APPENDS `/graphql` to whatever it is given.
    Handing one straight to the other asks a box for `/graphql/graphql`,
    which no box serves — a whole configured source silently contributing
    nothing, reported as an unreachable host.

    Stated as its own function, with its own test, rather than as a slice
    inside a caller: it is the seam between two spellings of one address, and
    a seam nothing names is a seam nothing checks.
    """
    trimmed = endpoint.rstrip("/")
    suffix = "/graphql"
    return trimmed[:-len(suffix)] if trimmed.endswith(suffix) else trimmed


class StashBox:
    def __init__(self, url, api_key, transport=None):
        # The GraphQL plumbing — hard deadline, error mapping, "data"
        # unwrapping — is the media-server client's, reused rather than
        # copied: it is the protocol that is shared, and a second hand-rolled
        # copy would be a second place for a transport bug to hide.
        self._client = Stash(url, api_key, transport=transport)
        self.url = self._client.url

    def performer_listing(self, performer_id, per_page=PER_PAGE,
                          max_pages=MAX_PAGES, timeout=DEFAULT_TIMEOUT):
        """Read every scene *this source lists* against `performer_id`, and
        say whether that was all of them.

        All of them means all of the entries the source holds. Contributors
        submit those entries by hand, so a whole read is a whole read of an
        index and not of a performer's work, and `complete=True` licenses
        nothing beyond "the source lists no more than these".

        A performer the source holds *nothing* for is a complete answer, not a
        failed one — it is the strongest evidence obtainable here, and the case
        a caller most wants to act on. So an empty listing is `complete=True`,
        and `count` is what separates it from the read that merely came back
        empty.

        Two things end the read short, and both return what was read with
        `complete=False` rather than raising: the page cap, and a page that
        comes back empty while `count` says there is more. Neither is an error
        a caller can do anything about — the scenes already in hand are still
        worth having — but neither is a listing that can be used to say a file
        is *unlisted*, which is why the flag and not an exception is how they
        are reported.

        The empty page is the one that would otherwise be an infinite loop: a
        source whose `count` overstates what it will hand back (a deleted
        scene still in the tally, a permission filter applied after counting)
        would be asked for page after page for ever. An empty page is
        therefore always the end of the read; only whether it counts as a
        whole one varies, and that needs both halves of the claim — nothing
        promised *and* nothing read. A `count` that says 0 after earlier pages
        already handed scenes back is a source contradicting itself, and a
        source whose tally cannot be trusted cannot be used to vouch for what
        it did not send.

        A transport failure part way through raises rather than returning the
        pages already read with `complete=False`. That is the one short read a
        caller *can* act on: `StashError.transient` says whether retrying is
        worth it, and folding it into the flag would throw that away and make
        a wedged host indistinguishable from an honest partial read.

        `total` on the returned listing is the `count` the FIRST page
        reported, held fixed for the rest of the read rather than
        overwritten by whatever a later page says. That matters exactly when
        a later page disagrees -- the count-drops-to-zero contradiction this
        method already treats as reason to distrust the read -- where the
        latest figure is the untrustworthy one and the first is still the
        source's original claim about how much there is. `pages_read` is
        simply how many requests it took to get here, whichever way the read
        ended.
        """
        scenes = []
        total = None
        for page in range(1, max_pages + 1):
            variables = {"input": {
                "performers": {"value": [performer_id], "modifier": "INCLUDES"},
                "page": page,
                "per_page": per_page,
            }}
            result = self._client.gql(PERFORMER_SCENES, variables, timeout=timeout)
            block = result["queryScenes"]
            if total is None:
                total = block["count"]
            if not block["scenes"]:
                nothing_to_read = block["count"] == 0 and not scenes
                return SourceListing(performer_id, scenes, complete=nothing_to_read,
                                     total=total, pages_read=page)
            scenes.extend(block["scenes"])
            if len(scenes) >= block["count"]:
                return SourceListing(performer_id, scenes, complete=True,
                                     total=total, pages_read=page)
        return SourceListing(performer_id, scenes, complete=False,
                             total=total, pages_read=max_pages)

    def all_tags(self, per_page=PER_PAGE, max_pages=MAX_PAGES,
                 timeout=DEFAULT_TIMEOUT):
        """Read this source's whole tag catalogue, and say whether that was
        all of it.

        Paged on the source's own `count`, and bounded by `max_pages` for the
        reason `performer_listing` is bounded: a source whose count overstates
        what it will hand back would otherwise be asked for page after page
        for ever. Both ways the read can stop short -- the page cap, and an
        empty page arriving while `count` says there is more -- return what
        was read with `complete=False` rather than raising. The tags already
        in hand are worth having; what they cannot support is a claim about
        what the source does NOT hold.

        A transport failure raises, and is not folded into the flag, on the
        same terms `performer_listing` states: `StashError.transient` says
        whether a retry is worth it, and a flag would make a wedged host
        indistinguishable from an honest partial read.

        A source holding no tags at all is a complete answer, not a failed
        one -- `count` is what separates it from the read that merely came
        back empty.
        """
        tags = []
        for page in range(1, max_pages + 1):
            variables = {"input": {"page": page, "per_page": per_page}}
            block = self._client.gql(BOX_TAGS, variables,
                                     timeout=timeout)["queryTags"]
            if not block["tags"]:
                return TagCatalogue(tags, complete=block["count"] == 0 and not tags)
            tags.extend(block["tags"])
            if len(tags) >= block["count"]:
                return TagCatalogue(tags, complete=True)
        return TagCatalogue(tags, complete=False)

    def known_by_fingerprint(self, fingerprints, timeout=DEFAULT_TIMEOUT):
        """Ask the source, in **one** request, which scenes carry each of
        `fingerprints`.

        `fingerprints` is an iterable of `(algorithm, hash)` pairs; the result
        maps each submitted pair — as a tuple, whatever it arrived as — to the
        list of `FingerprintHit`s the source returned for it.

        Every fingerprint submitted appears in the result, and one that
        matched nothing maps to an empty list rather than being left out.
        "I asked and there is nothing" and "I never asked" are the two facts
        this client exists to keep apart, and an absent key collapses them
        into the same `dict.get` returning `None`. For the same reason a
        transport failure raises rather than returning everything mapped to
        empty: that would report a question that was never answered as an
        answered one, and throw away `StashError.transient` on the way.

        A repeated fingerprint is asked about once — identical hashes are one
        question, and a scan's batch will hold duplicates whenever two files
        are the same bytes.

        Nothing is asked at all for an empty batch. There is no question in
        it, and the round trip could only spend rate limit.

        The endpoint answers positionally and says nothing about which block
        belongs to which set, so a reply of a different length than the batch
        makes the alignment unknowable and raises. Zipping would truncate to
        the shorter of the two silently and file scenes under hashes that
        never matched them — a wrong identification, which is worse than no
        answer, and the only outcome here that nothing downstream could catch.
        """
        wanted = [_checked_fingerprint(fp) for fp in fingerprints]
        # first-seen order, asked once; the request order is what the reply is
        # matched back against below
        unique = list(dict.fromkeys(wanted))
        if not unique:
            return {}
        variables = {"fingerprints": [[{"hash": value, "algorithm": algorithm}]
                                      for algorithm, value in unique]}
        result = self._client.gql(SCENES_BY_FINGERPRINT, variables, timeout=timeout)
        blocks = result["findScenesBySceneFingerprints"]
        if len(blocks) != len(unique):
            raise StashError(
                "the source answered %d fingerprint sets for a batch of %d — "
                "which block belongs to which fingerprint is unknowable"
                % (len(blocks), len(unique)))
        return {(algorithm, value): [FingerprintHit(scene, algorithm, value)
                                     for scene in block]
                for (algorithm, value), block in zip(unique, blocks)}
