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
import time
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
# for a slower pace — is bounded rather than avoided: `gql` retries a
# transient failure at most `RETRY_ATTEMPTS` times in total and then raises,
# so "asking for a slower pace" costs one extra request per call and never an
# unbounded spin. Which calls are eligible at all is `gql`'s `retryable`
# argument; see its docstring.
RETRYABLE_STATUSES = frozenset({408, 429})


# The whole retry bound, in two numbers.
#
# RETRY_ATTEMPTS is a TOTAL, not a count of extra tries: 2 means the original
# call plus one repeat. It is deliberately the smallest number that fixes the
# observed fault — an intermittent scraper failure that the identical query
# answers a moment later — because the evidence for intermittence is a failure
# to reproduce (16 queries against the same store, all successful), which is
# weaker than a reproduction. Being wrong about that costs one extra request
# per failing call; a larger bound would multiply the cost of being wrong by
# whatever it was raised to, for no evidence anyone has.
#
# RETRY_DELAY is why the retry is worth anything against a server asking for a
# slower pace (429/408): repeating immediately is the one behaviour a
# rate-limited server is explicitly telling the client not to do. It must be
# strictly positive for that reason, and small, because it is paid inside a
# caller's own wall-clock budget.
#
# The bound a caller can quote, and the one the deadline machinery below
# interacts with: one `gql` call can take at most
#
#     RETRY_ATTEMPTS * (timeout + HARD_DEADLINE_SLACK)
#         + (RETRY_ATTEMPTS - 1) * RETRY_DELAY
#
# because each attempt carries its OWN unmultiplied hard deadline and the
# delays fall between them. The per-attempt deadline is what must not grow:
# an attempt handed a deadline scaled by the attempt number would make a
# wedged host cost the caller a multiple of the bound it asked for.
RETRY_ATTEMPTS = 2
RETRY_DELAY = 1.0


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
    def __init__(self, url, api_key, transport=None, sleep=None):
        """`sleep(seconds)` is the wait between retry attempts, injected on
        the same terms and for the same reason as `transport`: the retry in
        `gql` is only testable at all if the delay it pays can be observed
        instead of endured, and a suite that endured it would pay
        `RETRY_DELAY` for every failure it exercises. Defaults to
        `time.sleep`."""
        self.url = url.rstrip("/") + "/graphql"
        self.api_key = api_key
        self._transport = transport if transport is not None else self._perform
        self._sleep = sleep if sleep is not None else time.sleep

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

    def gql(self, query, variables=None, timeout=DEFAULT_TIMEOUT,
            retryable=False):
        """Run one GraphQL operation and return its `data` block, retrying a
        transient failure when — and only when — the CALLER declares the
        operation retryable.

        `retryable` is a claim about the operation, made by the method that
        builds it, and it means two things at once because they are the same
        property: this operation is safe to repeat, and a failure of it is
        evidence about the round trip rather than about the request. Default
        False, so every call that does not opt in behaves exactly as it did
        before this argument existed — one attempt, and a GraphQL `errors`
        array is a permanent no.

        WHY BY OPERATION AND NOT BY MESSAGE TEXT. The fault this exists for is
        a scraper answering a single object where the schema promises a list;
        the media server reports it as a GraphQL error carrying the upstream
        scraper's own wording, down to a Go type name. Matching that wording
        would be precise about this one fault and would stop matching the day
        the upstream project rephrases it — and it would stop SILENTLY, because
        a retry that no longer happens raises nothing at all. What a reader
        would see is a store that answers slightly less often than it used to,
        which is not a symptom anyone can trace back to a string literal here.
        Keying on the operation instead depends on nothing another project
        controls.

        WHAT THE MESSAGE RULE WOULD HAVE COST, stated because the operation
        rule is the broader of the two: a scrape that the scraper refuses for a
        real, repeatable reason is now attempted twice instead of once. That is
        one extra request against a store that was going to fail anyway, paid
        only on the failing path, and bounded by `RETRY_ATTEMPTS`. The message
        rule would have avoided that single request and bought it with a rule
        that breaks without a symptom. It is the asymmetry this project keeps
        choosing: a visible cost over a silent one.

        The narrowness is deliberate and lives in the callers. Only the two
        site-scraper reads pass `retryable=True` — see
        `scrape_scenes_by_query` and `scrape_scene_url`. Every read whose
        filter this project builds itself (`_find_scenes`, `all_tags`, the
        find-or-create lookups) and every mutation keeps the default, the
        mutations for a second reason: a write whose transport failed may have
        landed on the server anyway, so repeating `performerCreate` can make
        two performers where the operator asked for one. A silent duplicate is
        worse than a visible failure, so nothing that writes opts in.

        WHY THE LOOP IS HERE AND NOT AT THE JOB LAYER. One policy, in the one
        place that already holds everything it needs:

        - `transient` is decided here. Nothing above this method can see it in
          a form worth acting on — by the time a store's failure reaches
          `cronicled.jobs`, `scan.examine_sources` has already folded it into
          a `store_errors` line, which is text: the store, the query and the
          variant that failed are gone.
        - A job-layer retry could therefore only repeat the whole unit of
          work. That re-searches every HEALTHY store, re-examines every file
          already examined, and re-yields proposals the runner has already
          persisted — a cost measured in `files x stores` requests to fix one
          store's one query, and a correctness problem on top of the cost.
        - The bound and the wall-clock it multiplies are both defined here
          (see `RETRY_ATTEMPTS` and `HARD_DEADLINE_SLACK`), so the one place
          that can state what a caller may be made to wait is the place that
          decides how many attempts it makes.

        Because this is the only loop, no call can be retried twice: a caller
        wanting different behaviour changes `retryable`, not a second policy
        somewhere else.

        A failure that exhausted more than one attempt says so in its message.
        The bound has to be visible in what gets RECORDED — `store_errors` in
        `cronicled.scan` is what a person reads afterwards — or a store that
        failed every attempt is indistinguishable from one that was asked
        once, and "the retry is not working" is not a conclusion anyone can
        reach from the original upstream sentence alone.
        """
        attempts = RETRY_ATTEMPTS if retryable else 1
        # ONE bound, in one place: `attempt < attempts` below. This was first
        # written as `for attempt in range(1, attempts + 1)`, which reads as
        # safer and is not. The only path that reaches a second iteration is
        # the `continue` guarded by `attempt < attempts`, so the range's upper
        # limit is unreachable — and two independent bounds meant NEITHER was
        # observable. Measured: mutating the range to unlimited changed nothing
        # any test could see, and mutating the guard away left the loop falling
        # off the end and RETURNING NONE, a silent wrong answer where the
        # caller expected a raise. Removing the redundant bound is what makes
        # the remaining one load-bearing. Termination is still plain: `attempt`
        # rises by one each pass, every path out of an iteration returns or
        # raises, and the only path that loops requires `attempt < attempts`.
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._attempt(query, variables, timeout, retryable)
            except StashError as e:
                if e.transient and attempt < attempts:
                    self._sleep(RETRY_DELAY)
                    continue
                if attempt > 1:
                    # Re-raised rather than mutated in place: `e` is whatever
                    # the transport or the errors branch built, and the
                    # `transient` flag is carried across unchanged because it
                    # describes the fault, not this loop's verdict on it.
                    raise StashError(
                        "%s — gave up after %d attempts" % (e, attempt),
                        transient=e.transient)
                raise

    def _attempt(self, query, variables, timeout, retryable):
        """ONE attempt: the request, its own hard deadline, and the
        interpretation of what came back. Split out from `gql` so the retry
        loop wraps a whole attempt — deadline included — which is what keeps
        each attempt's deadline the one the caller asked for rather than a
        multiple of it.

        `retryable` reaches this far for one reason: it is what the GraphQL
        `errors` branch below classifies on. See `gql`'s docstring."""
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
            # The server answered and said no. For an operation this project
            # builds the whole of — a filter, an update input — that is a
            # rejection of this input and will be repeated verbatim, so it is
            # permanent. For an operation whose server-side work is delegated
            # to a third-party scraper (`retryable`), the same array is the
            # scraper misbehaving on a request that carried nothing but free
            # text: there is no malformed input to reject, and the identical
            # query answers next time. See `gql`'s docstring.
            raise StashError(
                "; ".join(x.get("message", str(x)) for x in payload["errors"]),
                transient=retryable)
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

    def scene_ids(self):
        """`(count, ids)` — every scene id this server holds, and the number
        it says it holds.

        The read that answers "does this scene still exist", asked once for
        the whole library rather than once per subject. The two alternatives
        were tried against a running server and neither can do this job:

        * `findScenes(ids: [...])` ERRORS as soon as one id in the list is
          missing (`scene with id N not found`) rather than omitting it, so a
          batch containing one deleted scene answers nothing about the other
          thousands.
        * `findScene(id:)` answers `{"findScene": null}` with no error for a
          missing id — definitive, but one request per subject.

        `per_page: -1` returns the lot in a single request (6289 ids on one
        measured library, with `count` agreeing), which is the same read shape
        `_find_scenes` already uses for an unbounded pool.

        IDS ONLY. `_find_scenes` selects a scene's whole shape because a scan
        goes on to examine it; nothing here does, and selecting titles, files,
        studios, performers and tags for every scene in a library to learn
        which ones exist is thousands of rows of payload for one bit each.
        That is also why this does not route through `_find_scenes`: it takes
        a `scene_filter` this has none to give and selects a shape this must
        not ask for.

        BOTH halves are returned, deliberately. `count` is the server's own
        statement of how many scenes there are, and the only thing a caller
        can check the id list against — a short list with no error looks
        exactly like a complete one, and mistaking a partial read for a
        library that lost thousands of scenes is the expensive direction here.
        Comparing them is the caller's job (see
        `cronicled.scan.sweep_gone`), because the caller is what decides what
        to do about a disagreement; this method reports what the server said
        and interprets none of it.
        """
        q = """
        query($f: FindFilterType){
          findScenes(filter:$f){ count scenes{ id } }
        }"""
        f = {"per_page": -1, "page": 1, "sort": "id", "direction": "ASC"}
        block = self.gql(q, {"f": f})["findScenes"]
        # Stringified here, once, for the same reason every subject id this
        # project stores is a string: the store keys a mute and a refusal by
        # the string form, so a caller comparing an int id against those
        # would find no match and read a present scene as a deleted one.
        return block["count"], [str(scene["id"]) for scene in block["scenes"]]

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

    def performers_with_descriptions(self):
        """Every performer this server holds, with just enough to judge a
        DESCRIPTION: `id`, `name`, and `details` -- the field Stash stores a
        performer's free-text description in, the same field name a scene's
        own description is read under in `scene_existing` above.

        `details` may be `None` or `""` for a performer nobody has written
        one for. That is a normal answer, not a gap to raise on, and
        `cronicled.descriptions.assess` reads both as "nothing wrong".

        Paged exactly as `performers_with_stash_ids` pages, and for the same
        reason: a whole-library, no-filter read is the point -- the fault
        being looked for is visible in the text itself, so there is no filter
        the server could apply that would not also hide it.
        """
        q = """
        query($f: FindFilterType){
          findPerformers(filter:$f){
            count performers{ id name details }
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

    def performers_with_aliases(self):
        """Every performer this server holds, with just enough to recognise
        one of their NAMES: `id`, `name`, and `alias_list` -- the field Stash
        records a performer's other spellings in, and the same field name
        `_FIELDS["performer"]` below already selects when it resolves a single
        name against the server. Spelled that way here rather than as
        `aliases`, which is the field a TAG and a STUDIO carry; the three
        types do not share it.

        `alias_list` is `[String!]!`, a SCALAR list, so it takes no selection
        set -- unlike `stash_ids` in `performers_with_stash_ids` above, which
        is an object list and is rejected outright written bare. A performer
        nobody has recorded another spelling for answers `[]`; that is a
        normal answer, not a gap to raise on.

        The aliases are the point of this read rather than a bonus on top of
        it. Measured against a real library, matching tags against performer
        NAMES alone found far fewer than matching against names plus aliases,
        which is the same result `cronicled.tag_descriptions` measured for a
        stash-box's tag catalogue -- a person's other spellings are where most
        of the mis-filed tags actually sit.

        Paged exactly as `performers_with_stash_ids` and
        `performers_with_descriptions` page, and for the same reason: a
        whole-library, no-filter read is the point. `cronicled
        .performer_tags.index_performers` turns the whole list into one index
        and every tag is looked up in it, because a search per tag would be
        one request per tag against an answer one read already holds.
        """
        q = """
        query($f: FindFilterType){
          findPerformers(filter:$f){
            count performers{ id name alias_list }
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

    def performer_description(self, performer_id):
        """One performer's description as it stands RIGHT NOW.

        Read immediately before a write so the write can be checked against
        what is actually there, and so the undo snapshot records the text the
        write really replaced -- not the text a scan saw hours earlier. See
        `apply_performer_description`.

        A performer id the server does not know answers `None`, which
        `apply_performer_description` reports as a mismatch rather than
        silently writing to nothing.
        """
        q = "query($id: ID!){ findPerformer(id:$id){ id details } }"
        found = self.gql(q, {"id": performer_id}).get("findPerformer") or {}
        return found.get("details")

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

    @staticmethod
    def _merge_stash_ids(scene_id, existing, incoming):
        """The scene's catalogue links with `incoming` merged in BY ENDPOINT.

        sceneUpdate replaces the whole `stash_ids` list, so writing only the
        incoming pair would delete every link this tool did not make — a
        second catalogue, or one somebody entered by hand — and delete it
        silently. That is the same failure the performer/tag union exists to
        prevent, and the merge is by endpoint for the same reason it is by
        id there: an endpoint is what makes two entries the same link.

        An existing entry for the SAME endpoint carrying a DIFFERENT id is
        not a merge and is not resolved here. It is the media server and the
        catalogue disagreeing about which scene this file is, which is the
        most useful thing a reviewer could be told about it — so it is
        refused with both ids named, and nothing at all is written. This
        project reports every other two-source disagreement rather than
        letting whoever wrote last decide it, and a wrong link is precisely
        the corrupted record nobody notices: the row reads as applied, the
        scene reads as catalogued, and a later re-scrape pulls the other
        scene's metadata onto this file.

        Comparison is against every held id for that endpoint, not against
        one picked out of the list. A scene carrying two entries for one
        endpoint is already malformed, and choosing which of them to compare
        with would be an iteration order deciding an attribution.
        """
        merged = [dict(entry) for entry in existing]
        for entry in incoming:
            endpoint, stash_id = entry["endpoint"], entry["stash_id"]
            held = [held_entry["stash_id"] for held_entry in merged
                    if held_entry["endpoint"] == endpoint]
            if not held:
                merged.append({"endpoint": endpoint, "stash_id": stash_id})
                continue
            disagreeing = sorted(set(held) - {stash_id})
            if disagreeing:
                raise StashError(
                    "scene %s is already linked to %s as %s, but this "
                    "proposal identifies it there as %s -- the media server "
                    "and the catalogue disagree about which scene this file "
                    "is, so nothing was written"
                    % (scene_id, endpoint, ", ".join(disagreeing), stash_id))
        return merged

    # The one write every scene-level path here makes. Spelled ONCE, for the
    # reason `_SCRAPED_SCENE_SELECTION` below is: four callers send this same
    # mutation, and a copy per caller is four strings free to drift into
    # different mutations with nothing noticing which one a given path used.
    _SCENE_UPDATE = "mutation($in: SceneUpdateInput!){ sceneUpdate(input:$in){ id } }"

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

        # Resolved from the read alone, and BEFORE any find-or-create runs:
        # a disagreement between the scene's own links and this proposal's is
        # knowable without asking the server anything else, and refusing here
        # costs it no studio, performer or tag created on the way to a write
        # that was never going to happen.
        merged_stash_ids = self._merge_stash_ids(
            scene_id, prior["stash_ids"], match.get("stash_ids") or ())

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
            inp["stash_ids"] = merged_stash_ids
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

        self.gql(self._SCENE_UPDATE, {"in": inp})
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
        self.gql(self._SCENE_UPDATE, {"in": inp})
        return {"studio_id": prior.get("studio_id"),
                "performers": len(prior.get("performer_ids") or []),
                "tags": len(prior.get("tag_ids") or [])}

    # -- performer descriptions ------------------------------------------- #

    _PERFORMER_UPDATE = ("mutation($in: PerformerUpdateInput!)"
                         "{ performerUpdate(input:$in){ id } }")

    def apply_performer_description(self, performer_id, description, *,
                                    expected):
        """Replace one performer's description, refusing if it is not the
        text the proposal was derived from.

        `expected` is REQUIRED and has no default, on the same terms
        `cronicled.runscan.build_producer` requires a `limit`: a caller who
        forgot it and a caller who meant "write regardless" must not be able
        to write the same thing, and the second is not offered here at all.
        The cleaned text a proposal carries is a function of the exact
        description it was computed from -- strip the tags out of THIS text
        and you get THAT text -- so writing it over a description somebody
        has edited since would replace their edit with a cleaned-up version
        of what they edited away. Refused as a `StashError`, which
        `web.actions.Actions.approve` already records as a failed apply with
        the reason attached, leaving the proposal live and re-runnable.

        The read happens HERE, one line before the write, rather than in the
        caller: taking the comparison and the snapshot anywhere else opens a
        window between them and the write. That is the same ordering
        `apply_scene` uses and for the same reason.

        The returned `prior` is the undo snapshot, shaped as the input the
        server would accept to put the description back -- one field, so
        `revert_performer_description` can replay it verbatim. Unlike a
        scene's snapshot there is no field this write touches that the
        snapshot cannot represent, so an applied description IS fully
        undoable, with no equivalent of the cover-image caveat.
        """
        current = self.performer_description(performer_id)
        if current != expected:
            raise StashError(
                "performer %s's description is not the text this proposal was "
                "made from; it has changed since the scan, so applying would "
                "overwrite the current text with a cleaned-up version of the "
                "old one" % (performer_id,))
        self.gql(self._PERFORMER_UPDATE,
                 {"in": {"id": performer_id, "details": description}})
        return {"prior": {"details": current}}

    def revert_performer_description(self, performer_id, prior):
        """Undo one `apply_performer_description` by writing back exactly the
        text `prior` holds -- every character of it, whitespace included.

        Raises `ValueError` on a snapshot that is missing, empty, or does not
        carry the field, rather than quietly doing nothing: a revert that
        no-ops is indistinguishable from one that worked, which is the one
        ambiguity undo cannot afford. `apply_scene`'s own `revert_scene`
        refuses on the same terms.

        A snapshot whose `details` is `None` or `""` is NOT missing -- it is a
        performer who had no description before the apply, and restoring
        exactly that is the whole job.
        """
        if not prior or "details" not in prior:
            raise ValueError(
                "cannot revert performer %s: snapshot is missing, empty, or "
                "carries no description" % (performer_id,))
        self.gql(self._PERFORMER_UPDATE,
                 {"in": {"id": performer_id, "details": prior["details"]}})
        return {"details": prior["details"]}

    # -- performer enrichment ---------------------------------------------- #
    #
    # `cronicled.enrichment` is the ONE place that decides which fields count
    # as "blank" and what may fill one in -- this class only reads and writes
    # whatever field names it is given, the same separation `stash.py` already
    # keeps from `descriptions.py` (see `performer_description` above, which
    # hardcodes `details` rather than importing that module's own `FIELD`).
    # `ENRICHMENT_FIELDS` is this method's own copy of the field list for the
    # same reason: a low-level client has no business depending on a
    # higher-level producer's policy module, and a cross-check test
    # (`tests/test_enrichment.py`) pins the two against each other so they
    # cannot drift apart unnoticed.
    ENRICHMENT_FIELDS = (
        "details", "disambiguation", "piercings", "tattoos", "eye_color",
        "country", "gender", "measurements", "career_length", "birthdate",
        "ethnicity", "alias_list", "urls", "height_cm",
    )

    # Fields whose blank value is `[]`/`None` respectively rather than the
    # ordinary scalar `None`/`""` every other field in `ENRICHMENT_FIELDS`
    # shares -- read here once so `performers_for_enrichment`'s selection set
    # and the blank-value it reports for each field cannot drift into two
    # separate lists that disagree about which fields are which shape.
    _ENRICHMENT_LIST_FIELDS = ("alias_list", "urls")

    def performers_for_enrichment(self):
        """Every performer this server holds, with `id`, `name`,
        `stash_ids{endpoint stash_id}`, `image_path`, and every field in
        `ENRICHMENT_FIELDS` -- everything `cronicled.enrichment` needs to
        decide which of a performer's fields are blank and, for the ones that
        are, propose a value for them.

        `image_path` is the READ-side field name; `performerUpdate` (see
        `apply_performer_enrichment` below) accepts the photo under a
        DIFFERENT name, `image` -- Stash's own asymmetry, not a choice made
        here, and the reason the two are never spelled the same way in this
        module. The value returned is always a URL, never `None`: a
        performer with no photo still gets Stash's autogenerated placeholder,
        distinguished only by a `default=true` marker in the query string --
        see `cronicled.enrichment._lacks_image`, which is the one place that
        marker is read.

        `stash_ids` needs its own selection set (an OBJECT list, like
        `performers_with_stash_ids` above); every other field here is a
        scalar or a scalar list and is written bare, exactly as
        `performers_with_aliases` already writes `alias_list` bare.

        Paged exactly as every other whole-library performer read in this
        class pages, and for the same reason: there is no filter the server
        could apply that would not also hide the very gap this exists to
        find.
        """
        q = """
        query($f: FindFilterType){
          findPerformers(filter:$f){
            count performers{ id name stash_ids{ endpoint stash_id }
              image_path %s }
          }
        }""" % " ".join(self.ENRICHMENT_FIELDS)
        out, page = [], 1
        while True:
            data = self.gql(q, {"f": {"per_page": 500, "page": page,
                                      "sort": "name", "direction": "ASC"}})
            rows = data["findPerformers"]["performers"]
            out.extend(rows)
            if len(rows) < 500 or len(out) >= data["findPerformers"]["count"]:
                return out
            page += 1

    def performer_enrichment_fields(self, performer_id, fields):
        """The CURRENT value of exactly `fields` for one performer, read
        immediately before a write so `apply_performer_enrichment` can check
        each one is still blank -- the same "read fresh, right before the
        write" ordering `performer_description` uses and for the same reason:
        taking this reading anywhere else opens a window between the check
        and the write.

        `fields` names ordinary `ENRICHMENT_FIELDS` entries; `"image"`, the
        WRITE-side name, is translated to `image_path` for the read and
        translated back in the result, so a caller working entirely in
        proposal-field names never has to know the two differ.

        A performer id the server does not know answers every field `None` --
        `findPerformer` itself answers `None`, the same "not a mismatch to
        raise on here" reading `performer_description` gives it; the caller
        (`apply_performer_enrichment`) is what turns that into a refusal.
        """
        read_fields = ["image_path" if f == "image" else f for f in fields]
        q = "query($id: ID!){ findPerformer(id:$id){ id %s } }" % (
            " ".join(read_fields))
        found = self.gql(q, {"id": performer_id}).get("findPerformer") or {}
        return {field: found.get(read) for field, read in zip(fields, read_fields)}

    def _enrichment_is_blank(self, field, value):
        if field in self._ENRICHMENT_LIST_FIELDS:
            return not value
        return value in (None, "")

    def apply_performer_enrichment(self, performer_id, fields):
        """Write `fields` (a `{field_name: value}` mapping, `"image"` among
        them written under that name) onto a performer -- but ONLY the ones
        still blank at write time, and never a value the field already
        holds.

        Every field in `fields` is re-read (via
        `performer_enrichment_fields`) immediately before the write. If ANY
        of them is no longer blank -- something else filled it in since the
        proposal was made -- this raises `StashError` naming which, and
        writes NOTHING at all, rather than writing the fields that are still
        blank and silently dropping the rest. That is a stricter rule than it
        has to be: a partial write is not unsafe on its own terms (each field
        written is still additive), but it would leave the store's own
        `mark_applied` recording one snapshot for a proposal that only
        partly happened, and a reviewer with no way to tell "everything
        proposed was written" from "some of it was, quietly" apart. The same
        all-or-nothing discipline `apply_performer_description` already
        applies to its own single field, generalised to a set of them rather
        than invented fresh.

        Returns `{"prior": {field: <the blank value that field is written
        back to on Undo>, ...}}` -- `[]` for `alias_list`/`urls`, `None` for
        everything else, because an additive proposal's own precondition
        IS that value: there is no other prior state a field being enriched
        could have had. `revert_performer_enrichment` writes exactly this
        back.
        """
        if not fields:
            raise ValueError("apply_performer_enrichment: no fields given")
        current = self.performer_enrichment_fields(performer_id, list(fields))
        already_set = [f for f in fields
                      if not self._enrichment_is_blank(f, current.get(f))]
        if already_set:
            raise StashError(
                "performer %s's %s %s no longer blank; %s changed since the "
                "scan, so applying would overwrite %s with a guess made "
                "before the change" % (
                    performer_id, " and ".join(sorted(already_set)),
                    "is" if len(already_set) == 1 else "are",
                    "it" if len(already_set) == 1 else "they",
                    "it" if len(already_set) == 1 else "them"))
        inp = {"id": performer_id}
        for field, value in fields.items():
            inp[field] = value
        self.gql(self._PERFORMER_UPDATE, {"in": inp})
        prior = {f: ([] if f in self._ENRICHMENT_LIST_FIELDS else None)
                for f in fields}
        return {"prior": prior}

    def revert_performer_enrichment(self, performer_id, prior):
        """Undo one `apply_performer_enrichment` by writing every field in
        `prior` back to the blank value it holds -- `[]` for a list field,
        `None` for everything else, exactly as `apply_performer_enrichment`
        recorded it.

        Raises `ValueError` on a missing or empty snapshot, on the same terms
        `revert_performer_description`/`revert_scene` already refuse one:
        a revert that no-ops is indistinguishable from one that worked.
        """
        if not prior:
            raise ValueError(
                "cannot revert performer %s: enrichment snapshot is missing "
                "or empty" % (performer_id,))
        inp = {"id": performer_id}
        inp.update(prior)
        self.gql(self._PERFORMER_UPDATE, {"in": inp})
        return dict(prior)

    # -- tags (consolidation) --------------------------------------------- #

    def all_tags(self):
        """Every tag with id, name, aliases, description and scene_count
        (paged).

        `description` is selected because "this tag has no description" is a
        fact about the library that only the library can answer, and every
        caller that would otherwise guess at it has to guess wrong in the
        expensive direction: a tag read as undescribed when it is described
        gets a proposal to overwrite text somebody wrote. It is the server's
        own field name, spelled once here.
        """
        q = """
        query($f: FindFilterType){
          findTags(filter:$f){
            count tags{ id name aliases description scene_count } }
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

    def tag_description(self, tag_id):
        """One tag's description as it stands RIGHT NOW.

        The tag counterpart of `performer_description`, and here for the same
        reason: it is read immediately before a write so the write can be
        checked against what is actually there, rather than against what a
        pass saw hours or days earlier.

        A tag id the server does not know answers `None`, which every caller
        below reports as a mismatch rather than silently writing to nothing.
        """
        q = "query($id: ID!){ findTag(id:$id){ id description } }"
        found = self.gql(q, {"id": str(tag_id)}).get("findTag") or {}
        return found.get("description")

    _TAG_UPDATE = ("mutation($in: TagUpdateInput!)"
                   "{ tagUpdate(input:$in){ id } }")

    def apply_tag_description(self, tag_id, description, *, expected):
        """Write one tag's description, refusing if the field is not what the
        proposal was made against.

        `expected` is REQUIRED and has no default, on exactly the terms
        `apply_performer_description` requires its own: a caller who forgot it
        and a caller who meant "write regardless" must not be able to write
        the same thing, and the second is not offered here at all.

        The comparison is against the WHOLE stored value, verbatim. A
        proposal to fill an empty description is a proposal about a field
        that was empty; if somebody has written one since, the text this
        would write is a third party's sentence overwriting a person's own,
        and there is no way to tell from the result that it happened.

        The read happens HERE, one line before the write, rather than in the
        caller: taking the comparison and the snapshot anywhere else opens a
        window between them and the write.
        """
        current = self.tag_description(tag_id)
        if current != expected:
            raise StashError(
                "tag %s's description is not the text this proposal was made "
                "against; it has changed since the pass ran, so applying "
                "would overwrite it" % (tag_id,))
        self.gql(self._TAG_UPDATE,
                 {"in": {"id": str(tag_id), "description": description}})
        return {"prior": {"description": current}}

    def revert_tag_description(self, tag_id, prior):
        """Undo one `apply_tag_description` by writing back exactly what
        `prior` holds.

        Raises `ValueError` on a snapshot that is missing, empty, or does not
        carry the field, rather than quietly doing nothing: a revert that
        no-ops is indistinguishable from one that worked. A snapshot whose
        `description` is `None` or `""` is NOT missing -- it is a tag that had
        no description before the apply, and restoring exactly that is the
        whole job.
        """
        if not prior or "description" not in prior:
            raise ValueError(
                "cannot revert tag %s: snapshot is missing, empty, or carries "
                "no description" % (tag_id,))
        self.gql(self._TAG_UPDATE,
                 {"in": {"id": str(tag_id), "description": prior["description"]}})
        return {"description": prior["description"]}

    def merge_tags(self, destination_id, source_ids, aliases=None,
                   description=None):
        """Merge `source_ids` into `destination_id` via the native tagsMerge
        (moves all scene/marker/etc. associations, deletes the sources). When
        `aliases` is given it REPLACES the destination's alias list, so pass
        the full desired set (existing + merged names).

        `description` carries text onto the survivor in the SAME mutation
        that deletes the losing spellings. That is what it is for: tagsMerge
        keeps the destination's own fields, so a description that lives only
        on a spelling being deleted is destroyed by the merge, silently and
        with no record of what it said.

        It is only ever a carry-over onto a survivor that has none. The
        destination's description is read one line before the write and a
        NON-EMPTY one refuses the whole merge, rather than the write
        proceeding and replacing it. The proposal that asked for this was
        made against a survivor with an empty field, so a survivor that has
        gained one since is a person having written it in the meantime -- and
        overwriting that with a sentence lifted off another tag is the one
        outcome here nobody could detect afterwards. Refusing leaves the
        merge proposal live and re-runnable, with its reason attached.
        """
        inp = {"source": [str(s) for s in source_ids], "destination": str(destination_id)}
        values = {}
        if aliases is not None:
            values["aliases"] = list(aliases)
        if description is not None:
            current = self.tag_description(destination_id)
            if current:
                raise StashError(
                    "tag %s already has a description, so this merge will not "
                    "carry another one onto it; it has gained one since the "
                    "pass ran" % (destination_id,))
            values["description"] = description
        if values:
            values["id"] = str(destination_id)
            inp["values"] = values
        q = "mutation($in: TagsMergeInput!){ tagsMerge(input:$in){ id name aliases } }"
        return self.gql(q, {"in": inp})["tagsMerge"]

    def tag_scene_count(self, tag_id):
        """How many scenes carry this tag RIGHT NOW.

        The counting sibling of `tag_description`, and here for the same
        reason: it is read immediately before a write so the write can be
        checked against what is actually there rather than against what a pass
        saw hours or days earlier.

        A tag id the server does not know answers `None`, which `delete_tag`
        below reports as a mismatch rather than treating as "on no scenes" --
        the one reading under which a deletion of something already gone would
        report success.
        """
        q = "query($id: ID!){ findTag(id:$id){ id scene_count } }"
        found = self.gql(q, {"id": str(tag_id)}).get("findTag") or {}
        return found.get("scene_count")

    def delete_tag(self, tag_id, *, expected_scene_count):
        """Delete one tag, refusing if it is not on the number of scenes the
        proposal was made against.

        `expected_scene_count` is REQUIRED and has no default, on exactly the
        terms `apply_tag_description` requires its `expected`: a caller who
        forgot it and a caller who meant "delete regardless" must not be able
        to write the same thing, and the second is not offered here at all.

        The check is the whole reason this method exists rather than the caller
        issuing the mutation itself. A deletion proposal says "this tag is on
        nought scenes" or "on exactly one", and it can be days old; a tag that
        has been put to work since is one whose count has moved, and deleting it
        on the strength of last week's number would take a working tag off
        every scene carrying it with nothing recording which. Refusing raises
        `StashError`, which `web.actions.Actions.approve` records as a failed
        apply with the reason attached, leaving the proposal live.

        The read happens HERE, one line before the write, rather than in the
        caller: taking the comparison anywhere else opens a window between it
        and the write. That is the same ordering `apply_scene`,
        `apply_tag_description` and `merge_tags` all use.

        Nothing is snapshotted and nothing is returned for an undo. A deletion
        cannot be taken back -- see `cronicled.tag_hygiene.DELETE_WARNING` for
        the three separate reasons -- and returning an empty snapshot would let
        a row offer a button that restores nothing.
        """
        current = self.tag_scene_count(tag_id)
        if current != expected_scene_count:
            raise StashError(
                "tag %s is on %r scenes, not the %r this proposal was made "
                "against; it has changed since the pass ran, so deleting it "
                "now would remove a tag that is doing work nobody reviewed"
                % (tag_id, current, expected_scene_count))
        q = "mutation($id: ID!){ tagDestroy(input:{id:$id}) }"
        self.gql(q, {"id": str(tag_id)})

    def update_tag_aliases(self, tag_id, aliases):
        """Replace a tag's alias list (tagUpdate) — the write for an alias
        top-up: fold in aliases an external source knows about without
        merging or deleting anything."""
        q = "mutation($in: TagUpdateInput!){ tagUpdate(input:$in){ id aliases } }"
        self.gql(q, {"in": {"id": str(tag_id), "aliases": list(aliases)}})

    # -- tags that are a performer ---------------------------------------- #

    # The write both paths below make, and the ONLY modes it may carry.
    #
    # `bulkSceneUpdate` takes a list of scene ids plus, per list field, a
    # `BulkUpdateIds` of `{ids, mode}` -- so "add this performer and remove
    # this tag, on these scenes" is one request rather than a read and a write
    # per scene. `sceneUpdate` (`_SCENE_UPDATE`) REPLACES the arrays it
    # carries, which is why every caller of that one reads the scene first;
    # ADD and REMOVE are relative, so they need no read and cannot delete
    # something a read taken minutes ago did not know about.
    #
    # `BulkUpdateIdMode` offers a third mode, `SET`, and it is the reason the
    # check below exists rather than a comment saying "we never pass SET".
    # `SET` replaces the whole list: sent one performer id it strips every
    # OTHER performer from every scene in the batch, and sent one tag id every
    # other tag -- thousands of records, in one request, with nothing reading
    # them back. "The two call sites do not pass it" is a fact about today's
    # callers; refusing it here is a fact about the method.
    _BULK_SCENE_UPDATE = ("mutation($in: BulkSceneUpdateInput!)"
                          "{ bulkSceneUpdate(input:$in){ id } }")
    _BULK_MODES = ("ADD", "REMOVE")

    # How many scene ids go in one request. CHOSEN, not measured: no limit was
    # found on a running server, and a number that came from a guess is one
    # nobody can revisit unless it says so. Two things pull against each other
    # here. Larger chunks mean fewer requests -- the whole point of the ticket,
    # which starts from roughly 8000 requests for one reconciliation. Smaller
    # chunks mean less of the library in an unknown state when a request fails:
    # a refused chunk is not recorded in the undo snapshot (see
    # `reconcile_tag_to_performer`), so its scenes are exactly the ones an undo
    # cannot speak for. 200 takes that 8000 down to ~40 requests while capping
    # the unrecorded window at 200 scenes. Raise it on evidence -- a measured
    # server limit, or a measured request cost -- not on taste.
    _BULK_SCENE_CHUNK = 200

    def _scene_chunks(self, scene_ids):
        """`scene_ids` split into request-sized runs, in the order given."""
        for start in range(0, len(scene_ids), self._BULK_SCENE_CHUNK):
            yield scene_ids[start:start + self._BULK_SCENE_CHUNK]

    def _bulk_scene_write(self, scene_ids, fields):
        """One `bulkSceneUpdate` over `scene_ids`, carrying `fields`.

        `fields` maps a `BulkSceneUpdateInput` list field (`performer_ids`,
        `tag_ids`) to its `{"ids": [...], "mode": ...}`. Every field must carry
        a mode and it must be one of `_BULK_MODES`; a missing mode raises
        rather than defaulting, because the default that would be convenient
        here is a value that skips the check.
        """
        for name, value in fields.items():
            mode = value.get("mode")
            if mode not in self._BULK_MODES:
                raise ValueError(
                    "refusing to bulk-update %s on %d scenes with mode %r: "
                    "only %s may be sent from here. SET replaces the whole "
                    "list, so it would strip every other id from every scene "
                    "in the batch."
                    % (name, len(scene_ids), mode,
                       " and ".join(self._BULK_MODES)))
        inp = {"ids": [str(scene_id) for scene_id in scene_ids]}
        inp.update(fields)
        self.gql(self._BULK_SCENE_UPDATE, {"in": inp})

    def reconcile_tag_to_performer(self, tag_id, performer_id):
        """Attach `performer_id` to every scene carrying `tag_id`, and take
        the tag off those scenes.

        THE TAG IS NOT DELETED, and nothing here can delete it. Deleting it is
        a separate decision (see `cronicled.performer_tags`): a tag may
        legitimately share a name with a performer, and this is the half of
        the work that can be taken back.

        The worklist is read HERE, fresh, and never taken from the proposal:
        a proposal can be days old, and a scene tagged or untagged since is
        exactly what a proposal-time list gets wrong in both directions.

        ONE bulk write per chunk of scenes, carrying ONLY `performer_ids` with
        mode ADD and `tag_ids` with mode REMOVE. No `organized`, no title, no
        rating, nothing else this input accepts -- `apply_scene` sets
        `organized: True` on everything it writes, which is right for a scene
        whose metadata a person just approved and would be a second,
        unasked-for write to thousands of scenes here. Both fields go out in
        one mutation, so no scene is ever left carrying the performer AND the
        tag, or neither.

        ONE READ, not one per scene. The worklist read already selects each
        scene's performers and tags, and those are the only two facts the write
        and the snapshot need -- so the per-scene read the loop used to take is
        not a fresher answer, it is the same answer bought once per scene.
        `sceneUpdate` needed it because it REPLACES both arrays; ADD and REMOVE
        are relative and cannot delete what they were never told about.

        A scene the read does not report as carrying the tag is SKIPPED and
        reported, not written to. The server's own filter should not return
        one, and this does not trust it to: REMOVE of a tag a scene lacks is a
        no-op, but ADD of the performer is not, so a row that came back for any
        other reason would have the performer attached to it on the strength of
        a filter nobody checked.

        The residual, named rather than hidden: a scene untagged between the
        read and its chunk's write still gets the performer. That window used
        to be one request wide and is now one chunk wide, and closing it costs
        the per-scene read this exists to remove. It is bounded by
        `_BULK_SCENE_CHUNK` and by the seconds between two requests.

        On a failed chunk the run STOPS and the failure is returned rather than
        raised. Returned, because everything already written is what the undo
        snapshot has to cover: raising would discard the record of the scenes
        this call really did change, which is the one thing that makes a
        partial run recoverable. Stopping rather than continuing, because a
        server refusing one write is evidence about the server, and the
        remaining scenes are still exactly where they were.

        The failed chunk itself is recorded in NEITHER half of the snapshot and
        its scene ids are returned with the failure. Whether the server wrote
        some of them before it refused is not knowable from here, and a
        snapshot that claimed them would have an undo detach a performer from
        scenes that never got one.

        The returned `prior` is the undo snapshot, and it distinguishes the
        two halves: `untagged` is every scene the tag was taken off, `attached`
        is the subset that did not ALREADY carry the performer. A scene that
        had them both would be left without a performer somebody else attached
        if the undo detached from everything it untagged.
        """
        tag_id, performer_id = str(tag_id), str(performer_id)
        _, worklist = self.tagged_scenes(tag_id, None)
        attached, untagged, skipped, failures = [], [], [], []
        # `(scene_id, already)` for every scene that is really to be written,
        # decided entirely from the one read above.
        todo = []
        for scene in worklist:
            scene_id = str(scene["id"])
            tag_ids = [str(t["id"]) for t in (scene.get("tags") or ())]
            if tag_id not in tag_ids:
                skipped.append(scene_id)
                continue
            performer_ids = [str(p["id"])
                             for p in (scene.get("performers") or ())]
            todo.append((scene_id, performer_id in performer_ids))
        for chunk in self._scene_chunks(todo):
            scene_ids = [scene_id for scene_id, _ in chunk]
            try:
                self._bulk_scene_write(
                    scene_ids,
                    {"performer_ids": {"ids": [performer_id], "mode": "ADD"},
                     "tag_ids": {"ids": [tag_id], "mode": "REMOVE"}})
            except Exception as exc:
                failures.append({"scenes": scene_ids,
                                 "error": "%s: %s" % (type(exc).__name__, exc)})
                break
            # Recorded only AFTER the write returns, so a snapshot never
            # claims a scene the server refused.
            for scene_id, already in chunk:
                if not already:
                    attached.append(scene_id)
                untagged.append(scene_id)
        return {
            "prior": {"tag_id": tag_id, "performer_id": performer_id,
                      "attached": attached, "untagged": untagged},
            "skipped": skipped,
            # At most one entry, because the loop above stops at the first
            # failure. A list rather than a single field so a caller reads
            # "were there failures" the same way whether or not that ever
            # changes.
            "failures": failures,
            "worklist": [str(scene["id"]) for scene in worklist],
        }

    _RECONCILE_SNAPSHOT_FIELDS = ("tag_id", "performer_id", "attached",
                                  "untagged")

    def revert_reconcile(self, tag_id, prior):
        """Undo one `reconcile_tag_to_performer`: put the tag back on the
        scenes it was taken off, and detach the performer from the scenes it
        was attached to.

        BOTH halves, and only where each one applies. `prior["untagged"]` gets
        the tag back; `prior["attached"]` -- the subset that did not already
        carry the performer -- gets it detached. A revert that detached from
        everything it re-tagged would remove a performer somebody else had
        attached before the reconciliation ever ran.

        Raises `ValueError` on a snapshot that is missing, empty, or does not
        carry all four fields, on the same terms `revert_scene` and
        `revert_tag_description` refuse one: a revert that no-ops is
        indistinguishable from one that worked, which is the single ambiguity
        undo cannot afford. A snapshot naming a DIFFERENT tag is refused for a
        sharper reason -- it would write another tag onto these scenes.

        Over EXACTLY the recorded ids, never over the tag's current scenes.
        The tag has moved on -- the reconciliation took it off these scenes,
        and anything carrying it now is either a scene this run failed to reach
        or one somebody tagged since. Re-reading the tag would restore a
        performer onto neither.

        Two bulk writes, not one: the two halves cover different sets, and one
        request over their union would detach the performer from every scene
        that already carried it -- the exact harm the snapshot's two halves
        exist to prevent. The tag goes back FIRST, so at no point between the
        two is a scene left with neither the tag nor the performer; the
        overlap simply carries both for the moment in between, which is the
        state it was in before the reconciliation ran.

        Idempotent by the modes themselves rather than by reading first: ADD of
        a tag a scene already carries and REMOVE of a performer it no longer
        has are both no-ops on the server. So a revert that fails partway can
        be pressed again and the scenes it already restored cost one request's
        share of a no-op. That matters because a failure here raises, and the
        proposal stays `applied` with its snapshot intact.

        What that costs, said plainly: the returned lists are the scenes each
        half was WRITTEN OVER, not the scenes that were found to need it. With
        no read there is nothing to compare against, and a count of "actually
        changed" would be invented here.
        """
        if not prior or not all(field in prior
                                for field in self._RECONCILE_SNAPSHOT_FIELDS):
            raise ValueError(
                "cannot revert the reconciliation of tag %s: snapshot is "
                "missing, empty, or does not name the tag, the performer and "
                "both halves of what changed" % (tag_id,))
        if str(prior["tag_id"]) != str(tag_id):
            raise ValueError(
                "cannot revert the reconciliation of tag %s: this snapshot "
                "belongs to tag %s, and applying it would put that tag onto "
                "these scenes" % (tag_id, prior["tag_id"]))
        tag_id = str(tag_id)
        performer_id = str(prior["performer_id"])
        # Both halves in the snapshot's own order. No set is iterated on the
        # way to a request, so the sequence written is a function of the
        # snapshot and not of a hash.
        untagged = [str(scene) for scene in prior["untagged"]]
        attached = [str(scene) for scene in prior["attached"]]
        for chunk in self._scene_chunks(untagged):
            self._bulk_scene_write(
                chunk, {"tag_ids": {"ids": [tag_id], "mode": "ADD"}})
        for chunk in self._scene_chunks(attached):
            self._bulk_scene_write(
                chunk,
                {"performer_ids": {"ids": [performer_id], "mode": "REMOVE"}})
        return {"detached": attached, "retagged": untagged}

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

        RETRYABLE (`gql(retryable=True)`), and this is the call the argument
        was added for. A scraper answering a single object where the schema
        promises a list has been observed twice, on different files, against a
        store whose identical queries succeeded sixteen times out of sixteen
        when they were retried by hand. The request carries no structure this
        project could get wrong — one free-text `query` string — so a GraphQL
        error here is the scraper's own fault, not a rejection of the input,
        and repeating a read costs nothing but the request. See `gql`'s
        docstring for why the rule is the operation and not the wording of the
        error.
        """
        q = """
        query($source: ScraperSourceInput!, $input: ScrapeSingleSceneInput!){
          scrapeSingleScene(source:$source, input:$input){%s}
        }""" % self._SCRAPED_SCENE_SELECTION
        data = self.gql(q, {"source": {"scraper_id": scraper_id},
                            "input": {"query": query}}, retryable=True)
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

        RETRYABLE (`gql(retryable=True)`), on the same terms as
        `scrape_scenes_by_query`: it drives the SAME third-party scraper code,
        over a request carrying nothing but a URL, and it is a read. Its
        caller's treatment of failure is what makes including it worth the
        request rather than merely harmless — `scan.examine`'s enrichment step
        swallows the exception and keeps its thin candidate, so a blip here
        does not surface as an error at all; it surfaces as a proposal whose
        payload silently lacks the creator, studio and date the fuller record
        would have carried. That is the quiet degradation a bounded retry
        exists for.

        The one scraping call that is deliberately NOT retryable is
        `scrape_scenes_by_fingerprint`. It asks a stash-box's own database
        which scene a set of hashes belongs to rather than running a scraper
        plugin's code, no fault of this shape has been seen from it, and its
        own misalignment guard raises a permanent error on purpose — retrying
        a reply that could not be matched to the scenes it was asked about
        would repeat a request whose answer was already unusable. Extending
        the carve-out to it needs evidence about it, not consistency with a
        different failure.
        """
        q = """
        query($url: String!){
          scrapeSceneURL(url:$url){%s}
        }""" % self._SCRAPED_SCENE_SELECTION
        data = self.gql(q, {"url": url}, retryable=True)
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

    # The field that decides whether a match may identify the file at all.
    # A hit here means the box matched one of the file's own hashes; WHICH
    # algorithm matched is the whole of what tells an `MD5`/`OSHASH` hit
    # (the same bytes) from a `PHASH` hit (a perceptual hash, designed to
    # also match a re-encode or a trim of a DIFFERENT file that merely
    # looks similar). See `cronicled.scan._resolve_claims` for the rule
    # that reads it -- this client only carries the field back, unfiltered,
    # for every match the box returns.
    #
    # Kept out of `_SCRAPED_SCENE_SELECTION` for the same reason
    # `_REMOTE_SITE_ID` is: that constant's other caller, `scrape_scenes_by_
    # query`, never has a file's own hash to report, so folding this in
    # would put a `fingerprints: []` key on every text-scraped candidate --
    # changing every scored proposal's stored payload, hence the content
    # hash `cronicled.store.record` keys it by, hence re-proposing the
    # whole library once for a field only this caller can use. (That
    # stored content hash and a file's own fingerprint are two unrelated
    # meanings of the same word; this comment means the latter throughout.)
    _FINGERPRINTS = "fingerprints{ algorithm hash }"

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

    def stash_box_credentials(self):
        """Every configured stash-box as `{"name", "endpoint", "api_key"}`,
        in the order the SERVER lists them -- the operator's own configured
        order, and the order a caller should ask them in.

        This is `stash_boxes` plus the one field that method deliberately
        does not select, and the split is the point rather than duplication.
        `stash_boxes` serves callers that only NAME an endpoint for the media
        server to use on their behalf: the server already holds the key, so
        selecting it there would put a secret in a response body, and in
        whatever logs one, in order to throw it away. This serves the caller
        that talks to the box ITSELF, over its own GraphQL, where the key is
        the thing without which the request is anonymous. Selecting a secret
        because it will be used is a different act from selecting one that
        will not be.

        An install with no box configured answers `[]`, exactly as
        `stash_boxes` does and for the same reason: it is an ordinary state.
        A caller with no box to ask has nothing to ask.

        A list, never a mapping. The configured order IS the answer to
        "which box wins", so it must not be carried in a container that could
        put it at the mercy of a key's hash.
        """
        q = """
        query{ configuration{ general{
          stashBoxes{ name endpoint api_key } } } }"""
        general = self.gql(q)["configuration"]["general"]
        return list(general["stashBoxes"] or [])

    def scrape_scenes_by_fingerprint(self, endpoint, scene_ids):
        """Ask ONE stash-box to identify a batch of scenes by their own
        fingerprints, and return what it recognised for each — a list of
        match lists, ONE PER REQUESTED SCENE, IN THE ORDER REQUESTED.

        Nothing here searches for text and nothing here is scored — but
        that no longer means every match is identity. The server computes
        each scene's own hashes and asks the box which scene THOSE belong
        to, over three algorithms: `MD5` and `OSHASH` mean the box matched
        the same bytes; `PHASH` is a perceptual hash, designed on purpose
        to also match a re-encode or a trimmed copy — which means it
        matches just as readily against a DIFFERENT file that merely looks
        similar. A box's match names every fingerprint it matched on (see
        `_FINGERPRINTS`), unfiltered, and it is
        `cronicled.scan._resolve_claims` that decides whether a given match
        may identify the file or only support a proposal a scorer weighs.
        This method used to claim "this is identity, not similarity" here;
        that claim is exactly what a real library disproved — a `PHASH`
        collision between two unrelated videos was presented to a reviewer
        as an identified file — so this docstring no longer makes it. An
        empty inner list is the box saying "I have never seen this file",
        which is an ordinary answer — most files, most boxes.

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
        `remote_site_id`, the box's own id for the scene it recognised, and
        `fingerprints`, the algorithm/hash pairs the box matched on. See
        `_REMOTE_SITE_ID` and `_FINGERPRINTS` for why both are supersets
        here rather than additions to the shared constant.
        """
        ids = [str(scene_id) for scene_id in scene_ids]
        if not ids:
            return []
        q = """
        query($source: ScraperSourceInput!, $input: ScrapeMultiScenesInput!){
          scrapeMultiScenes(source:$source, input:$input){%s %s %s}
        }""" % (self._SCRAPED_SCENE_SELECTION, self._REMOTE_SITE_ID,
                self._FINGERPRINTS)
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
