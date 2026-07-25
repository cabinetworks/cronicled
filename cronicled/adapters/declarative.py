"""A SiteAdapter built entirely from a config dict.

The three owner sources cover every store shape seen so far: the creator appears
in a URL path segment, in a field on the result, or nowhere at all (in which case
candidates must be confirmed by scraping the clip page).
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

        valid = ("url_segment", "result_field", "none")
        if self._owner_source not in valid:
            raise ValueError("adapter %r: owner_source must be one of %s, got %r"
                             % (self.name, ", ".join(valid), self._owner_source))
        if self._owner_source == "url_segment":
            if not isinstance(self._owner_segment, int) or self._owner_segment < 0:
                raise ValueError("adapter %r: owner_segment must be a non-negative "
                                 "integer when owner_source is 'url_segment'"
                                 % self.name)
        if self._owner_source == "result_field" and not self._owner_field:
            raise ValueError("adapter %r: owner_field is required when "
                             "owner_source is 'result_field'" % self.name)

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
        """True when the clip is the artist's own (the owner matches) or its title
        or URL slug names them — the cross-store case."""
        if not artist_slug:
            return False
        owner = spaceless(self.owner_of(result))
        if owner and (owner == artist_slug or owner.startswith(artist_slug)):
            return True
        result = result or {}
        hay = spaceless((result.get("title") or "") + " "
                        + self.url_title_slug(result.get("url") or ""))
        return artist_slug in hay
