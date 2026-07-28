"""Media-server GraphQL client: reads, find-or-create, and scene apply.

The client never opens a socket directly — every call goes through
`transport(body, timeout) -> dict`, injected at construction time (default: an
HTTP POST to the server's GraphQL endpoint). That seam is what lets the whole
surface be tested without a network: a test's fake transport records what it
was sent and returns a canned parsed-JSON payload, exercising the exact
interpretation code (error mapping, "data" unwrapping) that the real
transport's response would also go through.

Store search and StashBox-style scraping do not live here: this client only
knows how to talk to a single media-server instance about its own scenes,
tags, performers and studios. A scraper source id and store-specific search
belong to the adapter layer, which is configured per install.
"""
import json
import re
import threading
import urllib.error
import urllib.request

from cronicled.text import strip_html

# Generous on purpose: some calls here page through everything the server
# holds (unorganized_scenes(limit=None), all_tags()), and a real library can
# hold thousands of scenes under a single tag — a tight default would abandon
# a slow-but-healthy read via the hard deadline (see HARD_DEADLINE_SLACK)
# before it ever gets the chance to finish.
DEFAULT_TIMEOUT = 180


class StashError(Exception):
    """A failed media-server call. `transient` marks the retryable kind —
    transport trouble, a wedged host, a 5xx, a 429 or a 408 — as opposed to
    the server rejecting the request itself (a GraphQL error, any other
    4xx). Callers use the split to decide whether a failure is worth
    retrying or is a permanent no for this input."""

    def __init__(self, message, transient=False):
        Exception.__init__(self, message)
        self.transient = transient


# HTTP statuses that mean "come back later" rather than "this request is
# wrong", even though they are nominally 4xx. 429 Too Many Requests is
# explicitly a request the server would accept on a second try, just not
# this one; 408 Request Timeout is the server giving up on a slow client
# rather than judging what it asked. Both belong with the 5xx family
# (`e.code >= 500`) below, not with the "will refuse it again" 4xx default.
#
# Getting this wrong in the direction of "permanent" is the quiet failure:
# `apply_scene` drops the performer/studio, marks the scene organized, and
# nothing ever revisits it, because the scene looks finished. Getting it
# wrong the other way — retrying forever against a server that is asking
# for a slower pace — is real too, but nothing here retries yet (see
# `cronicled.jobs`), so the only question this module answers is the
# classification, not the retry loop.
RETRYABLE_STATUSES = frozenset({408, 429})


# urlopen(timeout=) bounds socket ops but NOT name resolution — resolving a
# misbehaving host can hang indefinitely, and under any concurrency that wedges
# every caller waiting on the same worker (a thread pool joins the stuck worker
# on shutdown, never returning). gql runs the request on a throwaway daemon
# thread and abandons it once it overruns `timeout + HARD_DEADLINE_SLACK`, so a
# wedged request degrades to an ordinary error instead of hanging forever.
# Slack sits above the socket timeout so a normal slow call ends via the clean
# urlopen timeout rather than an abandoned thread.
HARD_DEADLINE_SLACK = 30


class Stash:
    def __init__(self, url, api_key, transport=None):
        self.url = url.rstrip("/") + "/graphql"
        self.api_key = api_key
        self._transport = transport if transport is not None else self._perform

    def _perform(self, body, timeout):
        """Default transport: HTTP POST to the media server + JSON parse.
        Blocking; `timeout` bounds socket ops only (not name resolution — see
        HARD_DEADLINE_SLACK). Raises StashError on any transport-level failure;
        returns the parsed GraphQL payload ("data" and/or "errors").
        Interpreting that payload is `gql`'s job, not the transport's, so an
        injected transport can skip the network entirely and still exercise the
        same interpretation code."""
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("ApiKey", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            # 5xx is the server having a bad time (worth retrying); 429 and
            # 408 are worth retrying for the same reason despite the 4xx
            # number (see RETRYABLE_STATUSES); every other 4xx is the server
            # refusing this request and will refuse it again.
            raise StashError("HTTP %s from the media server: %s" % (e.code, detail),
                             transient=e.code >= 500 or e.code in RETRYABLE_STATUSES)
        except urllib.error.URLError as e:
            raise StashError("cannot reach the media server at %s: %s" % (self.url, e),
                             transient=True)
        except OSError as e:
            # socket.timeout (a TimeoutError), connection reset, etc. — NOT a
            # URLError, so left uncaught it would escape as a bare OSError
            # instead of the StashError callers already know how to handle.
            raise StashError("transport error reaching the media server at %s: %s"
                             % (self.url, e), transient=True)

    def gql(self, query, variables=None, timeout=DEFAULT_TIMEOUT):
        body = {"query": query, "variables": variables or {}}
        # Bound the ENTIRE request (incl. name resolution) with a hard
        # wall-clock deadline; abandon the worker thread if it overruns so a
        # wedged request can never hang the caller (see HARD_DEADLINE_SLACK).
        box, done = {}, threading.Event()

        def work():
            try:
                box["payload"] = self._transport(body, timeout)
            except BaseException as e:  # relay to the caller thread verbatim
                box["exc"] = e
            finally:
                done.set()

        threading.Thread(target=work, name="stash-gql", daemon=True).start()
        if not done.wait(timeout + HARD_DEADLINE_SLACK):
            raise StashError("request to %s exceeded its %ss hard deadline "
                             "(host wedged?) — abandoned"
                             % (self.url, timeout + HARD_DEADLINE_SLACK), transient=True)
        exc = box.get("exc")
        if exc is not None:
            if isinstance(exc, StashError):
                raise exc
            # unknown failure: assume retryable rather than condemning the input
            raise StashError("unexpected error from the media server at %s: %s"
                             % (self.url, exc), transient=True)
        payload = box["payload"]
        if payload.get("errors"):
            # the server answered and said no — a rejection of this input, not a blip.
            raise StashError("; ".join(x.get("message", str(x)) for x in payload["errors"]))
        return payload["data"]

    # -- reads ----------------------------------------------------------- #

    def _find_scenes(self, scene_filter, limit):
        """(count, scenes) for one scene_filter, selecting everything a scan
        needs. `limit` None fetches the lot (per_page -1)."""
        per_page = -1 if limit is None else limit
        q = """
        query($f: FindFilterType, $s: SceneFilterType){
          findScenes(filter:$f, scene_filter:$s){
            count
            scenes{ id title date files{ basename path }
                    studio{ id name } performers{ id name } tags{ id name } }
          }
        }"""
        f = {"per_page": per_page, "page": 1, "sort": "id", "direction": "ASC"}
        data = self.gql(q, {"f": f, "s": scene_filter})
        return data["findScenes"]["count"], data["findScenes"]["scenes"]

    def unorganized_scenes(self, limit):
        return self._find_scenes({"organized": False}, limit)

    def tagged_scenes(self, tag_id, limit):
        """Every scene carrying `tag_id`, organized or not. The pool for a
        tag-driven scan: a cohort worth revisiting can already be marked
        organized from an earlier guessed-metadata pass, so `organized` must
        not constrain it."""
        return self._find_scenes({"tags": {"value": [tag_id], "modifier": "INCLUDES"}},
                                 limit)

    def performers_with_stash_ids(self):
        """Every performer this server holds, with just enough to link a
        NAME to a stash-box id: `id`, `name`, and `stash_ids` (`endpoint`,
        `stash_id`) -- the same object shape `scene_existing` already reads
        for a scene, read here for a performer instead. Most rows will carry
        an empty `stash_ids` list; that is a normal answer, not a gap to
        raise on -- a performer nobody has ever scraped or applied from a
        stash-box source simply has none yet.

        Paged the same way `all_tags` pages: a whole-library, no-filter read
        is the point (see `cronicled.performer_ids.derive_performer_ids`,
        this method's one caller), and a real library holds performers in
        the hundreds to low thousands, not a count worth trimming."""
        q = """
        query($f: FindFilterType){
          findPerformers(filter:$f){
            count performers{ id name stash_ids{ endpoint stash_id } }
          }
        }"""
        out, page = [], 1
        while True:
            data = self.gql(q, {"f": {"per_page": 500, "page": page,
                                      "sort": "name", "direction": "ASC"}})
            rows = data["findPerformers"]["performers"]
            out.extend(rows)
            if len(rows) < 500 or len(out) >= data["findPerformers"]["count"]:
                return out
            page += 1

    def tag_id_by_name(self, name):
        """A tag's id on THIS server, looked up by exact name — ids are
        installation-specific. None when the server has no such tag."""
        q = """query($f: TagFilterType){
          findTags(tag_filter:$f, filter:{per_page:5}){ tags{ id name scene_count } } }"""
        tags = ((self.gql(q, {"f": {"name": {"value": name, "modifier": "EQUALS"}}})
                 or {}).get("findTags", {}).get("tags") or [])
        return tags[0]["id"] if tags else None

    def scene_existing(self, scene_id):
        """The scene's CURRENT metadata, read fresh right before an apply so
        the write can merge rather than overwrite. Reading at write time (not
        scan time) also catches metadata a human set between scan and apply.

        Selects every field the apply path can write, so this same read also
        supplies apply_scene's undo snapshot (see `apply_scene`'s docstring).

        `stash_ids` is `[StashID!]!`, an OBJECT list, so it needs a selection
        set. Written bare it is not a narrower read -- the server rejects the
        whole query with a validation error, `scene_existing` raises, and every
        apply fails. It shipped that way and no test noticed, because the
        transport under test is a double that returns canned dictionaries and
        never parses the query it is handed. Only `endpoint` and `stash_id` are
        taken: `updated_at` is the server's own bookkeeping and is not part of
        `StashIDInput`, so carrying it into a snapshot would put a field into
        the restore payload that the mutation cannot accept."""
        q = """
        query($id: ID!){
          findScene(id:$id){ id title details date urls organized rating100
            code director
            stash_ids{ endpoint stash_id }
            studio{ id name } performers{ id name } tags{ id name } }
        }"""
        return self.gql(q, {"id": scene_id}).get("findScene") or {}

    # -- find-or-create helpers ------------------------------------------ #

    # kind -> (query, result field, alias field). Aliases are read so a name
    # that only exists as some entity's alias still resolves to it.
    _FIELDS = {"studio": ("findStudios", "studios", "aliases"),
               "performer": ("findPerformers", "performers", "alias_list"),
               "tag": ("findTags", "tags", "aliases")}
    _CREATE = {"studio": ("studioCreate", "StudioCreateInput"),
               "performer": ("performerCreate", "PerformerCreateInput"),
               "tag": ("tagCreate", "TagCreateInput")}

    # A name search returning more than this many rows is almost certainly a
    # too-generic query to be find-or-creating against — log a guard.
    _FIND_GUARD = 200

    def _find_first(self, kind, name):
        """Locate an existing entity by exact (case-insensitive) name, else by
        alias. Pages through the search results until the match is found or
        the search is exhausted, so a match beyond the first page is never
        missed (which would otherwise fall through to _create and produce a
        duplicate).

        A name that exists only as another entity's ALIAS resolves to that
        entity: an alias is the server's own "same thing under a different
        spelling" marker, and creating the name outright is refused (e.g.
        "name 'Strapon' is used as alias for 'Strap-on'"), which would
        otherwise fail the whole scene apply. An exact name match always wins
        over an alias hit, even one found earlier or on an earlier page — so
        the alias hit is only returned once the search is exhausted without a
        name match."""
        fn, field, alias_field = self._FIELDS[kind]
        q = "query($f: FindFilterType){ %s(filter:$f){ count %s{ id name %s } } }" % (
            fn, field, alias_field)
        low = name.strip().lower()
        page, per_page, seen, guarded, alias_hit = 1, 100, 0, False, None
        while True:
            block = self.gql(q, {"f": {"q": name, "per_page": per_page, "page": page}})[fn]
            rows = block[field]
            for r in rows:
                if r["name"].strip().lower() == low:
                    return r["id"]
                if alias_hit is None and any(
                        (a or "").strip().lower() == low for a in (r.get(alias_field) or [])):
                    alias_hit = r["id"]
            count = block.get("count")
            if count is not None and count > self._FIND_GUARD and not guarded:
                guarded = True
                print("warn: %s search for %r returned %d rows — very generic "
                      "find-or-create target" % (kind, name, count))
            seen += len(rows)
            if not rows or len(rows) < per_page or (count is not None and seen >= count):
                return alias_hit
            page += 1

    # The server's create rejection when the name is already someone's alias,
    # e.g. "name 'Strapon' is used as alias for 'Strap-on'" — the owner is
    # quoted last, so match greedily up to the closing quote (names may
    # contain apostrophes).
    _ALIAS_CLASH = re.compile(r"""used as alias for ['"](.+)['"]\s*$""")

    def _create(self, kind, name):
        mut, inp = self._CREATE[kind]
        q = "mutation($in: %s!){ %s(input:$in){ id } }" % (inp, mut)
        try:
            return self.gql(q, {"in": {"name": name}})[mut]["id"]
        except StashError as e:
            found = self._find_first(kind, name)  # likely a UNIQUE clash / alias
            if not found:
                # search-ranking miss: the rejection names the alias owner, so
                # look that up directly rather than failing the whole apply
                m = self._ALIAS_CLASH.search(str(e))
                if m:
                    found = self._find_first(kind, m.group(1))
            if found:
                return found
            raise

    def find_or_create(self, kind, name, stored_id=None):
        if stored_id:
            return stored_id
        name = (name or "").strip()
        if not name:
            return None
        return self._find_first(kind, name) or self._create(kind, name)

    def apply_scene(self, scene_id, match, overwrite_studio=False, drop_tag_ids=()):
        """Write resolved metadata onto a scene. The server's sceneUpdate
        REPLACES the performer/tag arrays, so we read the scene's current
        metadata first and UNION (existing ids + newly-resolved ids) — never
        wiping performers/tags a human already set. Studio is only set when
        the scene has none, unless `overwrite_studio` is passed. `drop_tag_ids`
        are removed from the result — the one exception to the union, used to
        take a scene out of a tag-based cohort once a real match lands on it.

        Resolution is tolerant of a name the server permanently refuses: that
        one entity is dropped from the write and reported in the returned
        `skipped` list rather than costing the scene its title, date, cover
        and every other performer/tag. A transient failure still raises — the
        row fails and a retry can re-run it. Everything is resolved before the
        single sceneUpdate below, so a raise still leaves no partial state on
        the server.

        The returned `prior` is a JSON-serialisable snapshot of the scene as
        it stood immediately before this write — every field this method can
        write, shaped as the restore input the server would accept to put it
        back (`studio_id`/`performer_ids`/`tag_ids` flattened from the read,
        the rest passed through as-is). It exists so a later undo has
        something to replay. The one field this apply writes that the
        snapshot CANNOT cover is the cover image: a scene's current cover is
        only exposed as a URL, not as the base64 payload `cover_image`
        accepts, so there is no representation to snapshot it with — an
        applied cover cannot be undone. If the method raises before the
        single sceneUpdate call, nothing was replaced, so there is nothing to
        return at all (the exception propagates and no `prior` is produced)."""
        existing = self.scene_existing(scene_id)
        existing_pids = [p["id"] for p in (existing.get("performers") or [])]
        existing_tids = [t["id"] for t in (existing.get("tags") or [])]
        existing_studio_id = (existing.get("studio") or {}).get("id")
        prior = {
            "title": existing.get("title"),
            "details": existing.get("details"),
            "date": existing.get("date"),
            "urls": existing.get("urls") or [],
            "organized": existing.get("organized"),
            "rating100": existing.get("rating100"),
            "code": existing.get("code"),
            "director": existing.get("director"),
            "stash_ids": existing.get("stash_ids") or [],
            "studio_id": existing_studio_id,
            "performer_ids": existing_pids,
            "tag_ids": existing_tids,
        }

        skipped = []

        def resolve(kind, name, stored_id):
            try:
                return self.find_or_create(kind, name, stored_id)
            except StashError as e:
                if e.transient:  # retryable — don't condemn the name, fail the row
                    raise
                skipped.append({"kind": kind, "name": name, "error": str(e)})
                return None

        st = match.get("studio") or {}
        studio_id = resolve("studio", st.get("name"), st.get("stored_id")) if st.get("name") else None
        resolved_pids = []
        for p in match.get("performers") or []:
            pid = resolve("performer", p.get("name"), p.get("stored_id"))
            if pid and pid not in resolved_pids:
                resolved_pids.append(pid)
        resolved_tids = []
        for t in match.get("tags") or []:
            tid = resolve("tag", t.get("name"), t.get("stored_id"))
            if tid and tid not in resolved_tids:
                resolved_tids.append(tid)

        # every name we tried was refused: this isn't one odd tag, it's a match
        # that doesn't land at all — fail the row with the server's own reason
        # rather than quietly writing the scalars and calling it applied
        if skipped and not (studio_id or resolved_pids or resolved_tids):
            raise StashError(skipped[0]["error"])

        # merge: keep every existing id, append only genuinely-new resolved ids
        new_pids = [x for x in resolved_pids if x not in existing_pids]
        new_tids = [x for x in resolved_tids if x not in existing_tids]
        merged_pids = existing_pids + new_pids
        merged_tids = existing_tids + new_tids
        # ...then take out the cohort tag, if this apply retires one
        dropped = set(drop_tag_ids) & set(merged_tids)
        if dropped:
            merged_tids = [t for t in merged_tids if t not in dropped]

        inp = {"id": scene_id, "organized": True}
        # sanitize free-text at write time so scraped HTML never lands on the server
        if match.get("title"):
            inp["title"] = strip_html(match["title"])
        if match.get("details"):
            inp["details"] = strip_html(match["details"])
        if match.get("date"):
            inp["date"] = match["date"]
        urls = match.get("urls") or ([match["url"]] if match.get("url") else [])
        if urls:
            inp["urls"] = urls
        if match.get("stash_ids"):  # canonical external id link(s), if supplied
            inp["stash_ids"] = match["stash_ids"]
        # studio: only claim an unset slot unless explicitly told to overwrite
        write_studio = studio_id if (studio_id and (overwrite_studio or not existing_studio_id)) else None
        if write_studio:
            inp["studio_id"] = write_studio
        # only rewrite the arrays when there's something new to add (else leave
        # them untouched — writing the same list back is a harmless but
        # pointless no-op)
        if new_pids:
            inp["performer_ids"] = merged_pids
        if new_tids or dropped:
            inp["tag_ids"] = merged_tids
        if match.get("image"):
            inp["cover_image"] = match["image"]

        self.gql("mutation($in: SceneUpdateInput!){ sceneUpdate(input:$in){ id } }", {"in": inp})
        return {"studio_id": write_studio or existing_studio_id,
                "performers": len(merged_pids), "tags": len(merged_tids),
                "skipped": skipped, "prior": prior}

    def revert_scene(self, scene_id, prior):
        """Undo one apply_scene by restoring the scene to exactly the state
        `prior` (as returned in apply_scene's result) describes.

        This RESTORES; it does not merge. That is the opposite of
        apply_scene's union semantics (existing ids + newly-resolved ids) —
        every field prior holds is written back verbatim, replacing whatever
        is there now, including wiping out performers/tags/etc. added since
        the snapshot was taken. `prior` is assumed already-resolved (ids, not
        names), so there is nothing here to find-or-create.

        Everything is assembled into one update input before anything is
        sent, and it goes out as a single sceneUpdate, mirroring apply_scene's
        write-once discipline: a failure leaves no partially-reverted scene.

        Raises ValueError on a missing or empty snapshot rather than quietly
        doing nothing — a revert that no-ops is indistinguishable from one
        that worked, which is exactly the ambiguity undo cannot afford.
        """
        if not prior:
            raise ValueError(
                "cannot revert scene %s: snapshot is missing or empty" % scene_id)
        inp = {"id": scene_id}
        inp.update(prior)
        self.gql("mutation($in: SceneUpdateInput!){ sceneUpdate(input:$in){ id } }",
                 {"in": inp})
        return {"studio_id": prior.get("studio_id"),
                "performers": len(prior.get("performer_ids") or []),
                "tags": len(prior.get("tag_ids") or [])}

    # -- tags (consolidation) --------------------------------------------- #

    def all_tags(self):
        """Every tag with id, name, aliases and scene_count (paged)."""
        q = """
        query($f: FindFilterType){
          findTags(filter:$f){ count tags{ id name aliases scene_count } }
        }"""
        out, page = [], 1
        while True:
            data = self.gql(q, {"f": {"per_page": 500, "page": page,
                                      "sort": "name", "direction": "ASC"}})
            rows = data["findTags"]["tags"]
            out.extend(rows)
            if len(rows) < 500 or len(out) >= data["findTags"]["count"]:
                return out
            page += 1

    def merge_tags(self, destination_id, source_ids, aliases=None):
        """Merge `source_ids` into `destination_id` via the native tagsMerge
        (moves all scene/marker/etc. associations, deletes the sources). When
        `aliases` is given it REPLACES the destination's alias list, so pass
        the full desired set (existing + merged names)."""
        inp = {"source": [str(s) for s in source_ids], "destination": str(destination_id)}
        if aliases is not None:
            inp["values"] = {"id": str(destination_id), "aliases": list(aliases)}
        q = "mutation($in: TagsMergeInput!){ tagsMerge(input:$in){ id name aliases } }"
        return self.gql(q, {"in": inp})["tagsMerge"]

    def update_tag_aliases(self, tag_id, aliases):
        """Replace a tag's alias list (tagUpdate) — the write for an alias
        top-up: fold in aliases an external source knows about without
        merging or deleting anything."""
        q = "mutation($in: TagUpdateInput!){ tagUpdate(input:$in){ id aliases } }"
        self.gql(q, {"in": {"id": str(tag_id), "aliases": list(aliases)}})

    # -- scraping ---------------------------------------------------------- #

    # The ScrapedScene selection both scraping methods below use — factored
    # into ONE constant rather than written twice, because the two calls
    # return the SAME type for the SAME purpose (candidate metadata a
    # proposal may carry forward), and a selection maintained in two places
    # can drift apart with nobody noticing: a field added to one query and
    # not the other becomes present on a candidate the name search returns
    # and silently absent on the same candidate scraped again by URL (or the
    # reverse), and nothing raises either way — the field is just missing on
    # one path. Sharing this string is what makes that drift structurally
    # impossible instead of merely a thing to remember.
    #
    # Every field `apply_scene` can write onto a scene: `title`, `details`,
    # `date`, `urls`, `code`, `director`, `studio`, `performers`, `tags` and
    # the cover `image`. `studio`, `performers` and `tags` each carry
    # `stored_id` alongside `name` — the server's own way of saying "this
    # scraped entity is already one you have" — which is exactly the shape
    # `apply_scene`'s `find_or_create` resolution takes for each of them.
    #
    # `url` (singular) is also selected, alongside `urls`, even though the
    # schema marks it deprecated in favour of the plural field. That is
    # deliberate rather than an oversight: `cronicled.adapters.declarative
    # .DeclarativeAdapter.owner_of` — the one existing consumer of a
    # scraped-result URL in this codebase, and the shape
    # `config/adapters.example.json`'s own example adapter is configured
    # with — reads a creator's name out of `result.get("url")`, singular,
    # for every adapter configured with `owner_source: "url_segment"`.
    # Selecting only `urls` would leave that field absent from every result
    # either method returns, which does not raise anywhere: it reads back as
    # an unresolved creator and the scan mutes the file, silently, for every
    # scene either scraper path ever answers. `apply_scene` itself needs no
    # help here — its own `urls`/`url` fallback already prefers the plural
    # field — so `url` is selected for the adapter's sake, not the apply
    # path's. Reconciling the adapter side to prefer `urls[0]` instead is a
    # real option, but it belongs to whichever task wires a scraper's
    # results through the adapter layer, not to this one.
    #
    # `image`, when present, is a base64 data URL. Applying it sets the
    # scene's cover, and that half of an apply cannot be undone:
    # `apply_scene`'s snapshot has no way to represent a scene's CURRENT
    # cover (the server only exposes it as a URL, not as the payload an
    # update accepts), so there is nothing to restore it from. See
    # `apply_scene`'s docstring. That is a decision already taken, not a gap
    # in either method here — selecting `image` is deliberate, and nothing
    # here undoes it.
    _SCRAPED_SCENE_SELECTION = """
        title code details director urls url date image
        studio{ stored_id name }
        tags{ stored_id name }
        performers{ stored_id name }
    """

    def scrape_scenes_by_query(self, scraper_id, query):
        """Ask one configured scraper for scenes matching a free-text
        `query`, via `scrapeSingleScene`. Returns a list of scene dicts —
        `[]` when the scraper found nothing, which is a normal answer, not
        an error, so nothing downstream needs a None check before iterating.

        `scraper_id` sources the call (the other half of
        `ScraperSourceInput`, a stash-box endpoint, is never used here — this
        client only knows how to ask a media-server-configured scraper). The
        text goes in as the input's free-text `query`, the one
        `ScrapeSingleSceneInput` field this method uses.

        The selection set is `_SCRAPED_SCENE_SELECTION`, shared with
        `scrape_scene_url` below — see that constant's own comment for why a
        shared string, not a separately-maintained copy, is what keeps the
        two from returning different fields for the same object.

        A name search's answers are typically much thinner than what
        scraping the same candidate's own page later returns: this method's
        selection set is the full shape the schema allows, but the
        scraper's search index commonly has only a title and a URL to offer
        for any one hit, leaving `performers`, `studio`, `date` and the rest
        present as keys with `None`/empty values rather than absent
        entirely. That is why a caller that wants the fuller record calls
        `scrape_scene_url` on the winning candidate's own URL rather than
        trusting this method's answer as final.

        `image`, when present, is a base64 data URL. Applying it sets the
        scene's cover, and that half of an apply cannot be undone:
        `apply_scene`'s snapshot has no way to represent a scene's CURRENT
        cover (the server only exposes it as a URL, not as the payload an
        update accepts), so there is nothing to restore it from. See
        `apply_scene`'s docstring. That is a decision already taken, not a
        gap in this method — selecting `image` is deliberate, and nothing
        here undoes it.
        """
        q = """
        query($source: ScraperSourceInput!, $input: ScrapeSingleSceneInput!){
          scrapeSingleScene(source:$source, input:$input){%s}
        }""" % self._SCRAPED_SCENE_SELECTION
        data = self.gql(q, {"source": {"scraper_id": scraper_id},
                            "input": {"query": query}})
        return data["scrapeSingleScene"] or []

    def scrape_scene_url(self, url):
        """Scrape ONE clip page directly, via `scrapeSceneURL`, and return
        the fuller `ScrapedScene` the page itself carries — or `None` when
        the server has no scraper configured that matches this URL, or the
        page it fetched had nothing to offer. `None` is a normal answer, not
        an error: the caller (`cronicled.scan.examine`'s enrichment step)
        already treats "nothing new to add" as leaving its thin candidate
        exactly as it was.

        Unlike `scrape_scenes_by_query`, this takes no `scraper_id`: the
        server itself matches the URL against whichever configured scraper
        claims it, which is the whole point of asking by URL instead of by
        a scraper id this client would otherwise have to guess from the
        link alone.

        The selection set is `_SCRAPED_SCENE_SELECTION` — the SAME constant
        `scrape_scenes_by_query` uses, not a second copy of the same field
        list. That is load-bearing, not tidiness: this method exists so a
        thin name-search result can be replaced by a fuller record of the
        SAME object, and the caller can only do that safely if both calls
        promise the same shape. A selection set that drifted between the
        two — a field added here and not there, or the reverse — would make
        `examine`'s enrichment step silently produce a candidate with a
        DIFFERENT field surface than the thin one it replaces, depending on
        which of the two calls happened to answer a given file.

        `image`, exactly as in `scrape_scenes_by_query`, is a base64 data
        URL when present. Applying it sets the scene's cover, and that half
        of an apply cannot be undone: `apply_scene`'s snapshot has no way to
        represent a scene's CURRENT cover (the server only exposes it as a
        URL, not as the payload an update accepts), so there is nothing to
        restore it from. Selecting `image` here is deliberate, not a gap,
        and nothing here undoes it — see `apply_scene`'s docstring.
        """
        q = """
        query($url: String!){
          scrapeSceneURL(url:$url){%s}
        }""" % self._SCRAPED_SCENE_SELECTION
        data = self.gql(q, {"url": url})
        return data.get("scrapeSceneURL")

    # The one field the fingerprint lookup selects that
    # `_SCRAPED_SCENE_SELECTION` does not: the BOX's own id for the scene it
    # recognised. It is deliberately NOT folded into the shared constant.
    #
    # The shared constant's own comment explains the drift it exists to
    # prevent: `scrape_scenes_by_query` and `scrape_scene_url` feed the SAME
    # consumer — `scan.examine`'s enrichment replaces a thin candidate from
    # the first with a fuller record of the same object from the second — so
    # a field present on one and absent on the other silently changes a
    # candidate's field surface depending on which call answered. The
    # fingerprint lookup is not in that pair: nothing ever replaces one of
    # its results with a text-scraped one, or the reverse, so it cannot
    # produce that drift.
    #
    # What adding `remote_site_id` to the shared constant WOULD do is put a
    # `remote_site_id: None` key on every candidate a text scrape returns —
    # a site scraper has no box id to give — which changes every proposal's
    # payload, hence its fingerprint, hence re-proposes every file in the
    # library once. A superset here costs nothing and is read by exactly the
    # one caller that has a use for it.
    _REMOTE_SITE_ID = "remote_site_id"

    def stash_boxes(self):
        """Every stash-box this server is configured against, as a list of
        `{"name", "endpoint"}` dicts, in the order the SERVER lists them —
        which is the operator's own configured order, and the order a caller
        should try them in.

        An install with none configured answers `[]`. That is an ordinary
        state, not a failure: identifying a file by its fingerprints is an
        addition to the text path, never a replacement for it, so a caller
        with no box to ask simply has nothing to ask and falls through.

        `StashBox` also carries `api_key` and `max_requests_per_minute`.
        Neither is selected: the api key is a secret this client has no use
        for (the server holds it and uses it on our behalf when we name the
        endpoint), and the rate limit is the server's own business for the
        same reason. Selecting a secret in order to throw it away would put
        it in a response body, and in whatever ever logs one.
        """
        q = """
        query{ configuration{ general{ stashBoxes{ name endpoint } } } }"""
        general = self.gql(q)["configuration"]["general"]
        return list(general["stashBoxes"] or [])

    def scrape_scenes_by_fingerprint(self, endpoint, scene_ids):
        """Ask ONE stash-box to identify a batch of scenes by their own
        fingerprints, and return what it recognised for each — a list of
        match lists, ONE PER REQUESTED SCENE, IN THE ORDER REQUESTED.

        This is identity, not similarity. The server computes each scene's
        hashes from the file itself and asks the box which scene those
        belong to; nothing here searches for text and nothing here is
        scored. An empty inner list is the box saying "I have never seen
        this file", which is an ordinary answer — most files, most boxes.

        `endpoint` is a configured box's own address (see `stash_boxes`),
        passed as `ScraperSourceInput.stash_box_endpoint`. That is the same
        source argument the text-scraping queries take, with the box half
        supplied instead of the `scraper_id` half — which is exactly what
        points the scene-scraping machinery at a box rather than at a site
        scraper.

        THE ORDER IS THE ONLY THING TYING A MATCH TO ITS SCENE. The reply
        carries no scene id of its own: entry `i` is the answer for
        `scene_ids[i]` and there is no other way to associate the two. So
        the reply is checked to be exactly as long as the request and the
        call FAILS if it is not, rather than being zipped against the ids
        (which would silently truncate) or padded (which would silently
        shift). A misalignment here writes one file's box metadata onto a
        different file, which is the most expensive silent failure this
        method can produce and the one thing it cannot detect after the
        fact.

        An empty `scene_ids` issues no request at all and answers `[]`: a
        batch of nothing is not a question worth asking a box, and the
        length check above would otherwise be comparing two empties.

        The selection set is `_SCRAPED_SCENE_SELECTION` — the same fields a
        text scrape returns, so a match can be carried into a proposal's
        payload and applied by exactly the same code — plus
        `remote_site_id`, the box's own id for the scene it recognised. See
        `_REMOTE_SITE_ID` for why that one field is a superset here rather
        than an addition to the shared constant.
        """
        ids = [str(scene_id) for scene_id in scene_ids]
        if not ids:
            return []
        q = """
        query($source: ScraperSourceInput!, $input: ScrapeMultiScenesInput!){
          scrapeMultiScenes(source:$source, input:$input){%s %s}
        }""" % (self._SCRAPED_SCENE_SELECTION, self._REMOTE_SITE_ID)
        data = self.gql(q, {"source": {"stash_box_endpoint": endpoint},
                            "input": {"scene_ids": ids}})
        per_scene = data["scrapeMultiScenes"]
        if per_scene is None or len(per_scene) != len(ids):
            raise StashError(
                "asked %s to identify %d scenes and it answered with %s match "
                "lists; the reply carries no scene ids, so nothing can be "
                "matched to the scene it belongs to"
                % (endpoint, len(ids),
                   "no list at all" if per_scene is None else len(per_scene)))
        return [list(matches or []) for matches in per_scene]

    def scene_scrapers(self):
        """The configured scrapers that can scrape a scene, as a list of
        `{"id", "name"}` dicts — enough for a caller to tell an operator
        what is actually available, rather than failing on a scraper id
        nobody can check against anything.

        `listScrapers` takes `types: [ScrapeContentType!]!`; passing `SCENE`
        is what scopes this to scrapers offering scene scraping specifically,
        rather than every scraper the server has configured for any purpose.
        """
        q = """
        query($types: [ScrapeContentType!]!){
          listScrapers(types:$types){ id name }
        }"""
        data = self.gql(q, {"types": ["SCENE"]})
        return data["listScrapers"] or []
