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

# Calls here are plain reads/writes against the server's own database, not a
# scrape through a headless browser, so they don't need a scraper's patience.
DEFAULT_TIMEOUT = 30


class StashError(Exception):
    """A failed media-server call. `transient` marks the retryable kind —
    transport trouble, a wedged host, a 5xx — as opposed to the server
    rejecting the request itself (a GraphQL error, a 4xx). Callers use the
    split to decide whether a failure is worth retrying or is a permanent no
    for this input."""

    def __init__(self, message, transient=False):
        Exception.__init__(self, message)
        self.transient = transient


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
            # 5xx is the server having a bad time (worth retrying); 4xx is the
            # server refusing this request and will refuse it again.
            raise StashError("HTTP %s from the media server: %s" % (e.code, detail),
                             transient=e.code >= 500)
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
        scan time) also catches metadata a human set between scan and apply."""
        q = """
        query($id: ID!){
          findScene(id:$id){ id title date
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
        the server."""
        existing = self.scene_existing(scene_id)
        existing_pids = [p["id"] for p in (existing.get("performers") or [])]
        existing_tids = [t["id"] for t in (existing.get("tags") or [])]
        existing_studio_id = (existing.get("studio") or {}).get("id")

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
                "skipped": skipped}

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
