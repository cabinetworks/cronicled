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

    # Two substitution maps, doing genuinely different jobs, whose names do
    # not say which is which. An operator whose file says one thing and whose
    # store says another reaches for "aliases", because that is what the word
    # means everywhere else — and a title-word substitution filed there has
    # already been reported. Both are spelled out here, at the point of use:
    #
    # `censorship` is about TITLE TEXT. It maps a canonical word to the forms
    # a platform substitutes for it, and travels per-store, bound to that
    # adapter's own `cronicled.scan.Source` — one store censoring a word says
    # nothing about what any other store does. See `cronicled.censorship`.
    censorship = {}             # {canonical: [substituted_form, ...]}

    # `aliases` is about ARTIST RESOLUTION and has nothing to do with titles.
    # It maps a folder name as it is actually filed on disk to the creator's
    # full name, and is read by `cronicled.artist.resolve` through
    # `cronicled.artist.Aliases`. The value is a PERSON'S NAME, never a slug
    # and never a title word.
    #
    # Unlike `censorship` it is NOT applied per store: the resolver runs once
    # per file, off that file's own folder, before any store has been
    # searched, so there is exactly one alias answer per file and no store to
    # key it on. `cronicled.runscan.configured_aliases` therefore pools every
    # configured adapter's map into one, and refuses a key two adapters both
    # declare rather than letting whichever sorted first decide.
    aliases = {}                # {as_filed_folder_name: creator_full_name}

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

        Case-folded before it is returned, and that is the ONE normalisation
        applied here. Search is case-insensitive essentially everywhere, so
        folding costs nothing, and it closes the one gap that used to exist
        between this string and `cronicled.text.normalize` — the scorer's own
        equality test — for nothing but letter case.

        Deliberately NOT run through `normalize` itself: that also strips
        punctuation, folds accented letters, and expands `&` to `and`, and
        each of those is a genuine, unmeasured guess for a QUERY rather than
        for a comparison. Punctuation may be the one token distinguishing
        this title from a wrong one; letter-folding may match a store that
        folds accents and miss one that does not; and expanding `&` outright
        would commit to one spelling when a store may index the other —
        see `cronicled.censorship.search_variants`, which tries both instead
        of guessing, for that one. So this string and the scorer's own
        comparison stay deliberately different beyond case, documented here
        and in `cronicled.scoring.title_view` and `cronicled.text.normalize`.
        """
        return (seed + " " + title_query).lower()
