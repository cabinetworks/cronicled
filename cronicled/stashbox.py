"""stash-box client: enumerate what a source *has*, rather than what matches.

Every scraper action the media server offers answers "what matches this?".
None answers "what exists?" — which is the question behind a refusal that
means anything. stash-box's `queryScenes` takes a performer criterion and
reports a `count` alongside the page, so a performer's whole catalogue can be
read and *known* to have been read in full.

Reading it in full is the entire point, and the reason `Catalogue.complete`
exists as a separate fact from the scenes themselves: a view that stopped
early is not evidence of absence, and must never be reported as though it
were.

Like `cronicled.stash`, every call goes through an injected transport, so the
whole surface is testable without a network.
"""

from cronicled.stash import DEFAULT_TIMEOUT, Stash

# stash-box's own default is 25. A catalogue read is a whole-catalogue read,
# so it pays for itself in round trips saved.
PER_PAGE = 100

# A bound on a read that would otherwise be unbounded. At PER_PAGE that is
# 10,000 scenes for one performer — far past any real catalogue, which is the
# point: it is here to stop a runaway, not to trim a large but honest read.
# Hitting it makes the read incomplete, and an incomplete read can never be
# reported as an absence.
MAX_PAGES = 100

PERFORMER_SCENES = """
query($input: SceneQueryInput!) {
  queryScenes(input: $input) {
    count
    scenes { id title date urls { url } }
  }
}
"""


class Catalogue:
    """The scenes a performer has on the source, and whether that is all of
    them.

    `complete` is the field that carries the weight: only a `True` licenses a
    caller to say a file is *absent* from this performer's catalogue. It is
    kept beside the scenes rather than inferred from their length, because
    the length of a partial read and the length of a complete one look
    exactly alike.
    """

    def __init__(self, performer_id, scenes, complete):
        self.performer_id = performer_id
        self.scenes = tuple(scenes)
        self.complete = complete

    def __repr__(self):
        return "Catalogue(performer_id=%r, scenes=%d, complete=%r)" % (
            self.performer_id, len(self.scenes), self.complete)


class StashBox:
    def __init__(self, url, api_key, transport=None):
        # The GraphQL plumbing — hard deadline, error mapping, "data"
        # unwrapping — is the media-server client's, reused rather than
        # copied: it is the protocol that is shared, and a second hand-rolled
        # copy would be a second place for a transport bug to hide.
        self._client = Stash(url, api_key, transport=transport)
        self.url = self._client.url

    def performer_catalogue(self, performer_id, per_page=PER_PAGE,
                            max_pages=MAX_PAGES, timeout=DEFAULT_TIMEOUT):
        """Read every scene credited to `performer_id`, and say whether that
        was all of them.

        A performer the source holds *nothing* for is a complete answer, not a
        failed one — it is the strongest evidence of absence obtainable, and
        the case a caller most wants to act on. So an empty catalogue is
        `complete=True`, and `count` is what separates it from the read that
        merely came back empty.

        Two things end the read short, and both return what was read with
        `complete=False` rather than raising: the page cap, and a page that
        comes back empty while `count` says there is more. Neither is an error
        a caller can do anything about — the scenes already in hand are still
        worth having — but neither is a catalogue that can be used to say a
        file is *absent*, which is why the flag and not an exception is how
        they are reported.

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
        """
        scenes = []
        for page in range(1, max_pages + 1):
            variables = {"input": {
                "performers": {"value": [performer_id], "modifier": "INCLUDES"},
                "page": page,
                "per_page": per_page,
            }}
            result = self._client.gql(PERFORMER_SCENES, variables, timeout=timeout)
            block = result["queryScenes"]
            if not block["scenes"]:
                nothing_to_read = block["count"] == 0 and not scenes
                return Catalogue(performer_id, scenes, complete=nothing_to_read)
            scenes.extend(block["scenes"])
            if len(scenes) >= block["count"]:
                return Catalogue(performer_id, scenes, complete=True)
        return Catalogue(performer_id, scenes, complete=False)
