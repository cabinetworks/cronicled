"""The site-adapter interface.

An adapter tells the matcher how to read a clip search result from one store:
where the creator's name lives, what the title slug is, and whether a catalog
search can resolve a creator at all. No store is implemented in this repo —
adapters are configured; see `registry.load_adapters`.
"""


class SiteAdapter(object):
    name = ""                   # registry key
    display = ""                # human label
    scraper_id = ""             # the media server's scraper id
    catalog_resolvable = True   # can a name search identify the creator?
    censorship = {}             # {canonical: [substituted_form, ...]}
    aliases = {}                # {abbreviation_slug: real_store_slug}

    def owner_of(self, result):
        raise NotImplementedError

    def artist_from_url(self, url):
        raise NotImplementedError

    def url_title_slug(self, url):
        raise NotImplementedError

    def clip_features_artist(self, result, artist_slug):
        """True when `result` is evidence that `artist_slug` features in (or
        owns) the clip.

        A store's own attribution of the result (however this adapter reads
        it) is always trustworthy evidence. A bare MENTION of the artist's
        name in the title or URL slug is a much weaker inference, and is not
        always safe: on a store whose name search surfaces fan or
        collaboration clips sold by somebody else, a title naming a creator
        is not evidence they own the clip. A hand-written adapter for a
        store like that should override this method to ignore title/slug
        mentions entirely rather than let the generic substring check admit
        them — see `cronicled.adapters.declarative.DeclarativeAdapter`,
        whose `title_match_counts_as_ownership` spec field makes that
        distinction configurable rather than requiring a code override."""
        raise NotImplementedError

    def search_query(self, seed, title_query):
        """The targeted per-title query: the creator handle plus the title, which
        narrows a catalog search to that creator's own clips.

        Not wired to anything: `cronicled.search.catalog_search` — the
        production `search` callable `scan.examine` actually calls — queries
        once per CREATOR, not once per title, so it never builds this query
        at all. Reading this method as live because it exists would be a
        mistake; it describes the other shape a search could have taken, not
        the one that was chosen. See `cronicled.search`'s module docstring.
        """
        return seed + " " + title_query
