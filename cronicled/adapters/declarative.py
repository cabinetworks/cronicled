"""A SiteAdapter built entirely from a config dict.

The three owner sources cover every store shape seen so far: the creator appears
in a URL path segment, in a field on the result, or nowhere at all (in which case
candidates must be confirmed by scraping the clip page).

`title_match_counts_as_ownership` is a fourth, orthogonal axis: whether a title
or URL-slug MENTION of the artist is trustworthy evidence that the clip is
theirs, on top of whatever `owner_source` supplies. It is deliberately not
derived from either `owner_source` or `catalog_resolvable`:

- `owner_source` answers "where does the primary owner value come from", not
  "is a bare name mention elsewhere on the page evidence of anything" — a
  store can carry a solid `result_field` owner and still surface a title
  match that means nothing (a fan edit, a collab clip sold by someone else),
  or carry no fielded owner at all and still have title mentions that are
  reliable.
- `catalog_resolvable` answers "can a name search identify this store's
  creators at all", a question about search, not about what a single
  result's title implies. Folding the two would make one flag carry two
  independent answers — the same mistake `competing`/`rejected_folder` exist
  to keep apart elsewhere in this codebase.

There is no default. A spec that omits it fails to load rather than silently
inheriting the permissive (title-match-counts) reading: an adapter that
cannot state whether its title matches are trustworthy must not get the
permissive answer by default, because that default is exactly how a
candidate ends up admitted as a creator's own work on evidence the store
cannot support. A config written before this field existed will need one
line added, not a swap for a worse behaviour.
"""
from cronicled.text import spaceless, strip_html
from cronicled.adapters.base import SiteAdapter


class DeclarativeAdapter(SiteAdapter):
    def __init__(self, spec):
        self.name = spec["name"]
        self.display = spec.get("display") or spec["name"]
        self.scraper_id = spec.get("scraper_id") or ""
        self.catalog_resolvable = bool(spec.get("catalog_resolvable", True))
        self.censorship = spec.get("censorship") or {}
        self.aliases = spec.get("aliases") or {}
        self._owner_source = spec.get("owner_source") or "none"
        self._owner_segment = spec.get("owner_segment")
        self._owner_field = spec.get("owner_field") or []
        self._search_omits_seed = bool(spec.get("search_omits_seed", False))
        owner_segment_example = spec.get("owner_segment_example")

        valid = ("url_segment", "result_field", "none")
        if self._owner_source not in valid:
            raise ValueError("adapter %r: owner_source must be one of %s, got %r"
                             % (self.name, ", ".join(valid), self._owner_source))
        if self._owner_source == "url_segment":
            if not isinstance(self._owner_segment, int) or self._owner_segment < 0:
                raise ValueError("adapter %r: owner_segment must be a non-negative "
                                 "integer when owner_source is 'url_segment'"
                                 % self.name)
            if owner_segment_example is not None:
                # `owner_segment` counts from after the scheme and INCLUDES
                # the host -- one off from what a reader familiar with URL
                # *paths* would assume, and that mismatch has already caused
                # a real misconfiguration. This is optional (an existing
                # config with no example keeps working unchanged), but when
                # given, it is checked right now, at load time, rather than
                # left to be documented and hoped for: a wrong index is
                # otherwise silent right up until a scan mutes every file for
                # a store this adapter was supposed to identify.
                url = owner_segment_example.get("url")
                expected = owner_segment_example.get("owner")
                actual = self.artist_from_url(url)
                if actual != expected:
                    raise ValueError(
                        "adapter %r: owner_segment_example says owner_segment "
                        "%d on %r should resolve to %r, but it resolves to "
                        "%r -- owner_segment counts from after the scheme and "
                        "INCLUDES the host, not from the start of the path "
                        "(see docs/adapters.md)"
                        % (self.name, self._owner_segment, url, expected, actual))
        if self._owner_source == "result_field" and not self._owner_field:
            raise ValueError("adapter %r: owner_field is required when "
                             "owner_source is 'result_field'" % self.name)

        if "title_match_counts_as_ownership" not in spec:
            raise ValueError(
                "adapter %r: title_match_counts_as_ownership is required — "
                "declares whether a title or URL-slug mention of the artist "
                "is trustworthy evidence of ownership on this store (true), "
                "or whether it proves nothing, as on a store whose name "
                "search surfaces fan or collaboration clips sold by someone "
                "else (false). There is no default: an adapter that does "
                "not say does not get the permissive answer." % self.name)
        self._title_match_counts_as_ownership = bool(
            spec["title_match_counts_as_ownership"])
    @property
    def owner_source(self):
        """Which of the three mechanisms this adapter resolves an owner
        with: `'url_segment'`, `'result_field'`, or `'none'`. Public so
        asking "which mechanisms does this config actually use" -- which
        the shipped example's own tests do -- doesn't mean reaching into a
        private attribute."""
        return self._owner_source

    def _segments(self, url):
        # drop scheme, then query and fragment: a tracking parameter must not
        # become part of the title slug
        path = (url or "").split("://")[-1].split("?")[0].split("#")[0]
        return [s for s in path.rstrip("/").split("/") if s]

    def artist_from_url(self, url):
        if self._owner_source != "url_segment" or self._owner_segment is None:
            return ""
        segs = self._segments(url)
        idx = self._owner_segment
        return segs[idx] if 0 <= idx < len(segs) else ""

    def owner_of(self, result):
        result = result or {}
        if self._owner_source == "url_segment":
            return self.artist_from_url(result.get("url") or "")
        if self._owner_source == "result_field":
            node = result
            for key in self._owner_field:
                node = (node or {}).get(key) if isinstance(node, dict) else None
            return strip_html(node or "")
        return ""

    def url_title_slug(self, url):
        segs = self._segments(url)
        return segs[-1] if segs else ""

    def clip_features_artist(self, result, artist_slug):
        """True when the clip is the artist's own (the owner matches) or,
        only when this store's spec says a bare mention is trustworthy
        (`title_match_counts_as_ownership`), its title or URL slug names
        them — the cross-store case.

        The owner-field/segment check always applies regardless of that
        flag: it is a distinct, stronger signal (the store's own attribution
        of the result), not the weak substring inference the flag governs.
        """
        if not artist_slug:
            return False
        owner = spaceless(self.owner_of(result))
        if owner and (owner == artist_slug or owner.startswith(artist_slug)):
            return True
        if not self._title_match_counts_as_ownership:
            return False
        result = result or {}
        hay = spaceless((result.get("title") or "") + " "
                        + self.url_title_slug(result.get("url") or ""))
        return artist_slug in hay

    def search_query(self, seed, title_query):
        """The base per-title query (`seed + " " + title_query`), unless this
        store's spec sets `search_omits_seed` — for a store where narrowing
        by the creator seed costs recall and buys nothing, so the query is
        the title alone.

        Reached only by the per-title fallback, and only for a file the
        per-creator pass could not resolve — see `SiteAdapter.search_query`
        for the whole ordering."""
        if self._search_omits_seed:
            return title_query
        return super(DeclarativeAdapter, self).search_query(seed, title_query)
