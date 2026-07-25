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
        raise NotImplementedError

    def search_query(self, seed, title_query):
        """The targeted per-title query: the creator handle plus the title, which
        narrows a catalog search to that creator's own clips."""
        return seed + " " + title_query
