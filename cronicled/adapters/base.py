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

        A FALLBACK, never the shape a scan searches in. The per-creator query
        runs first and costs one lookup per creator rather than one per file
        (see `cronicled.search`'s module docstring for the measurement that
        motivated it); only a file that pass would otherwise refuse reaches
        this method, through `cronicled.scan.examine_sources`, which spends
        one such query per (file, store) before recording that refusal.
        `seed` is the resolved creator's name and `title_query` the filename
        read as a title — `cronicled.scoring.title_view`, the same derivation
        the scorer weighs, never a second one built at the call site.
        """
        return seed + " " + title_query
