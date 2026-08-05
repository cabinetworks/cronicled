import io
import json
import re
import socket
import threading
import unittest
import urllib.error
from unittest import mock

from cronicled.stash import (DEFAULT_TIMEOUT, HARD_DEADLINE_SLACK,
                             RETRY_ATTEMPTS, RETRY_DELAY, Stash, StashError)


def _transport(responses):
    """A fake transport. `responses` is a list of dicts or exceptions, returned in
    order; the calls it received are recorded on the function object."""
    calls = []

    def send(body, timeout):
        calls.append((body, timeout))
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    send.calls = calls
    return send


# -- what the server calls these things ------------------------------------ #
#
# Stated ONCE, here, and stated INDEPENDENTLY of cronicled/stash.py's own
# `_FIELDS`/`_CREATE` maps: for each entity kind, the search field, the block
# the rows come back in, the field that kind's ALIASES arrive in, the create
# mutation and the input type that mutation takes. These are the media
# server's names, not this project's — they are external facts, which is what
# makes restating them a check rather than a mirror.
#
# Every fake in this file shapes its replies from this table. That matters: a
# fake carrying its own private copy of the same map, or reading the client's
# map at run time, agrees with the client no matter what the client's map says
# — the copy travels WITH a mutation instead of contradicting it, and the fake
# can then only ever confirm whatever the code does. One table, used by the
# fakes and asserted against the client's maps by name, is the whole of the
# fix.
#
# The one entry that cannot be guessed from its neighbours is the performer's
# alias field: studios and tags use `aliases`, performers use `alias_list`.
SERVER_ENTITY_API = {
    "studio": ("findStudios", "studios", "aliases",
               "studioCreate", "StudioCreateInput"),
    "performer": ("findPerformers", "performers", "alias_list",
                  "performerCreate", "PerformerCreateInput"),
    "tag": ("findTags", "tags", "aliases",
            "tagCreate", "TagCreateInput"),
}

# The two projections the fakes need: (search field, result block, alias field)
# and the create mutation's name.
SERVER_FIND = {kind: row[:3] for kind, row in SERVER_ENTITY_API.items()}
SERVER_CREATE_MUTATION = {kind: row[3] for kind, row in SERVER_ENTITY_API.items()}


class Gql(unittest.TestCase):
    def test_returns_the_data_block(self):
        t = _transport([{"data": {"version": {"version": "0.0.1"}}}])
        got = Stash("http://example.test", "k", transport=t).gql("query{version{version}}")
        self.assertEqual(got["version"]["version"], "0.0.1")

    def test_sends_the_query_and_variables(self):
        t = _transport([{"data": {}}])
        Stash("http://example.test", "k", transport=t).gql("query($a:ID){x}", {"a": 1})
        body, _ = t.calls[0]
        self.assertIn("query($a:ID)", body["query"])
        self.assertEqual(body["variables"], {"a": 1})

    def test_graphql_errors_become_StashError(self):
        t = _transport([{"errors": [{"message": "boom"}]}])
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t).gql("query{x}")
        self.assertIn("boom", str(ctx.exception))

    def test_one_call_reaches_the_injected_transport_exactly_once(self):
        # Named for what it asserts. It was called "no test opens a socket",
        # which is a claim about the whole suite that one client with one
        # injected transport cannot make -- and which is not the property this
        # suite should have anyway: the web tests serve real HTTP on a local
        # port and cannot be exercised without binding one. The property that
        # IS enforced, across every test rather than inside this one, lives in
        # tests/nonetwork.py: no test resolves a name that is not loopback.
        t = _transport([{"data": {}}])
        Stash("http://example.test", "k", transport=t).gql("query{x}")
        self.assertEqual(len(t.calls), 1)


class Transience(unittest.TestCase):
    def test_a_transient_error_is_marked_transient(self):
        err = StashError("timeout", transient=True)
        self.assertTrue(err.transient)

    def test_a_permanent_error_is_not(self):
        self.assertFalse(StashError("bad name").transient)


# -- error classification ------------------------------------------------- #
#
# `transient` is the single flag that decides whether a failed call is worth
# retrying or whether the name it was carrying is given up on for good, so
# every test below starts from the CONDITION (an HTTP status, a socket
# failure, an `errors` array, the hard deadline) and asserts what the client
# concludes from it. Constructing a StashError and reading back the flag the
# constructor was handed pins the dataclass, not the decision — the whole
# table could be inverted underneath such a test without a single failure.
#
# Both directions of a wrong call are expensive, which is why they are pinned
# separately rather than as one "errors are classified" test:
#
#   permanent when it should be transient — apply_scene drops that performer
#   or studio from the write, records it in `skipped`, marks the scene
#   ORGANIZED and moves on. The scene now looks done, so no later pass
#   revisits it: the metadata is silently and permanently incomplete.
#
#   transient when it should be permanent — a name the server will never
#   accept is retried forever and the batch never terminates.


def _client():
    """A client with NO injected transport, so the call goes through the real
    `_perform`. The classification under test lives there; an injected
    transport would bypass the exact `except` chain these tests exist to pin."""
    return Stash("http://example.test", "k")


def _raising_urlopen(exc):
    """Stand in for urllib.request.urlopen and fail the way `exc` says."""

    def fake_urlopen(req, timeout=None):
        raise exc

    return fake_urlopen


def _answering_urlopen(payload):
    """Stand in for urlopen with a real HTTP 200 whose body is `payload`."""
    body = json.dumps(payload).encode()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    def fake_urlopen(req, timeout=None):
        return _Resp()

    return fake_urlopen


def _http_error(code):
    """The HTTPError urlopen raises for `code`, with a readable body (the
    client reads the response detail into the message)."""
    return urllib.error.HTTPError("http://example.test/graphql", code,
                                  "reason", None, io.BytesIO(b"server said so"))


class ErrorClassification(unittest.TestCase):
    """One condition per test; each asserts what the client CONCLUDES."""

    def _failure_from(self, urlopen):
        """Drive a real gql() call whose urlopen behaves as given, and return
        the StashError it produced."""
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(StashError) as ctx:
                _client().gql("query{x}")
        return ctx.exception

    def test_a_5xx_from_the_server_is_retryable(self):
        # HARM: condemning a 5xx drops the performer/studio from the apply and
        # marks the scene organized, so a server that was merely having a bad
        # minute costs that scene its metadata permanently — nothing revisits
        # a scene that already looks done.
        for code in (500, 502, 503):
            with self.subTest(code=code):
                err = self._failure_from(_raising_urlopen(_http_error(code)))
                self.assertTrue(err.transient,
                                "HTTP %s is the server having a bad time and "
                                "must be retryable" % code)

    def test_a_4xx_from_the_server_is_permanent(self):
        # HARM: retrying a refusal the server will repeat verbatim means the
        # batch never terminates — the same rejected name is sent forever.
        for code in (400, 401, 404, 422):
            with self.subTest(code=code):
                err = self._failure_from(_raising_urlopen(_http_error(code)))
                self.assertFalse(err.transient,
                                 "HTTP %s is the server refusing this request "
                                 "and will refuse it again" % code)

    def test_a_429_is_retryable(self):
        # HARM: condemning a 429 drops the performer/studio from the apply
        # and marks the scene organized even though the server only asked to
        # come back later and would accept the identical request a moment
        # after — a rejection this project has not actually observed yet
        # (the ticket that added this test is explicit about that), but one
        # whose quiet failure mode is worth guarding regardless.
        err = self._failure_from(_raising_urlopen(_http_error(429)))
        self.assertTrue(err.transient,
                        "HTTP 429 means try again, not never")

    def test_a_408_is_retryable(self):
        # HARM: a request timeout is the server giving up on a slow
        # round-trip, not a judgement on what was asked; condemning it is the
        # same quiet metadata loss a misclassified 429 causes.
        err = self._failure_from(_raising_urlopen(_http_error(408)))
        self.assertTrue(err.transient,
                        "HTTP 408 is a timeout, worth retrying")

    def test_a_4xx_other_than_429_and_408_stays_permanent(self):
        # HARM: widening the retryable carve-out by accident would let a
        # genuinely-refused name spin forever, which is the failure the
        # existing 4xx-is-permanent rule exists to prevent.
        for code in (400, 401, 404, 422):
            with self.subTest(code=code):
                err = self._failure_from(_raising_urlopen(_http_error(code)))
                self.assertFalse(err.transient,
                                 "HTTP %s must stay permanent even though "
                                 "429 and 408 are now carved out" % code)

    def test_the_boundary_between_the_two_families_is_499_500(self):
        # HARM: an off-by-one here silently reclassifies a whole family in one
        # direction or the other — every 5xx condemned, or every 4xx retried
        # forever — and nothing else in the suite would notice.
        permanent = self._failure_from(_raising_urlopen(_http_error(499)))
        retryable = self._failure_from(_raising_urlopen(_http_error(500)))
        self.assertFalse(permanent.transient, "499 is still a client-side refusal")
        self.assertTrue(retryable.transient, "500 is the first server-side fault")

    def test_an_unreachable_host_is_retryable(self):
        # HARM: a host that is down or a name that will not resolve says
        # nothing whatsoever about the performer's name — condemning it loses
        # metadata for a reason that has already fixed itself by next run.
        err = self._failure_from(_raising_urlopen(
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))))
        self.assertTrue(err.transient, "an unreachable host is worth retrying")

    def test_a_timed_out_socket_is_retryable(self):
        # HARM: a timeout is the definition of "unknown outcome" — the server
        # may not even have seen the request. Treating it as a refusal
        # condemns a name nobody ever rejected.
        # socket.timeout is an alias of the builtin TimeoutError on every
        # Python this project runs on; both spellings are driven anyway so the
        # day that stops being true the test still covers what it claims to.
        for label, exc in (("socket.timeout", socket.timeout("timed out")),
                           ("TimeoutError", TimeoutError("timed out"))):
            with self.subTest(raised=label):
                err = self._failure_from(_raising_urlopen(exc))
                self.assertTrue(err.transient, "a timeout is worth retrying")

    def test_a_dropped_connection_is_retryable(self):
        # HARM: a reset connection is transport trouble, not a verdict on the
        # request. It is also not a URLError, so it reaches the client as a
        # bare OSError — one that must still arrive as a retryable StashError
        # rather than escaping the StashError contract entirely.
        err = self._failure_from(_raising_urlopen(
            ConnectionResetError(104, "Connection reset by peer")))
        self.assertTrue(err.transient, "a dropped connection is worth retrying")

    def test_a_200_carrying_a_graphql_errors_array_is_permanent(self):
        # HARM: the server answered, understood, and said no — this is the
        # rejection that must NOT be retried ("name 'X' is used as alias
        # for..."), or the batch spins on it forever.
        err = self._failure_from(_answering_urlopen(
            {"errors": [{"message": "name 'Strapon' is used as alias for 'Strap-on'"}]}))
        self.assertFalse(err.transient,
                         "a GraphQL errors array is the server rejecting this "
                         "input, not a blip")
        self.assertIn("Strapon", str(err))  # the server's own reason survives

    def test_an_unrecognised_failure_is_assumed_retryable(self):
        # HARM: a failure the client has no rule for is exactly the case where
        # it does not know the name was refused. Assuming permanent there
        # discards metadata on the strength of a guess; assuming retryable
        # costs at worst a repeat.
        err = self._failure_from(_raising_urlopen(ValueError("something new")))
        self.assertTrue(err.transient,
                        "an unknown failure must not condemn the input")

    def test_the_hard_deadline_is_retryable(self):
        # HARM: the deadline fires on a wedged host, which is the most
        # transient condition there is. Condemning on it would mean a single
        # wedged moment silently strips names off every scene in the batch.
        release = threading.Event()

        def wedged_transport(body, timeout):
            release.wait(30)  # never released before the deadline is asserted
            return {"data": {}}

        stash = Stash("http://example.test", "k", transport=wedged_transport)
        try:
            # slack of 0 with timeout 0 makes the deadline fire immediately —
            # the condition is the deadline expiring, not how long it took
            with mock.patch("cronicled.stash.HARD_DEADLINE_SLACK", 0):
                with self.assertRaises(StashError) as ctx:
                    stash.gql("query{x}", timeout=0)
            self.assertIn("deadline", str(ctx.exception))  # the right raise fired
            self.assertTrue(ctx.exception.transient,
                            "an abandoned request is worth retrying")
        finally:
            release.set()


class _SceneTransport:
    """Fake transport for apply_scene tests. apply_scene issues a variable
    number of calls depending on what's in `match` (a find per studio/
    performer/tag, a create for any not found, then one sceneUpdate), so this
    routes each call by operation name rather than by call order.

    `existing` is the dict returned for the findScene read (the scene's
    current metadata). `found` maps kind -> {name: id} for entities the fake
    server already has, so _find_first resolves them without a create call.
    A name absent from `found[kind]` falls through to a create mutation,
    whose outcome is looked up in `create` by (kind, name): an id (the create
    succeeds), or an exception instance the create call raises instead (used
    to simulate the server refusing the name, permanently or transiently).
    """

    _FIND = SERVER_FIND
    _CREATE_MUTATION = SERVER_CREATE_MUTATION

    def __init__(self, existing, found=None, create=None):
        self.existing = existing
        self.found = found or {}
        self.create = create or {}
        self.scene_update_input = None
        # Every query body sent, in order. `scene_update_input` records only
        # the final write, so it cannot tell "nothing was written" from
        # "nothing was written AND nothing was created on the way there" --
        # and an entity created for an apply that then refused is rubbish
        # left on the server for a proposal nobody approved.
        self.queries = []
        # the variables of every findScene read, in order: the apply's merge
        # and its undo snapshot both come from that read, so WHICH scene it
        # asked about is part of what a test needs to be able to see
        self.scene_read_variables = []

    def __call__(self, body, timeout):
        q = body["query"]
        self.queries.append(q)
        if "findScene(" in q:
            self.scene_read_variables.append(body["variables"])
            return {"data": {"findScene": self.existing}}
        for kind, (fn, field, alias_field) in self._FIND.items():
            if fn + "(" in q:
                return self._find(kind, fn, field, alias_field, body)
        for kind, mut in self._CREATE_MUTATION.items():
            if mut in q:
                return self._create(kind, mut, body)
        if "sceneUpdate" in q:
            self.scene_update_input = body["variables"]["in"]
            return {"data": {"sceneUpdate": {"id": self.scene_update_input["id"]}}}
        raise AssertionError("test transport does not recognize query: %s" % q)

    def _find(self, kind, fn, field, alias_field, body):
        name = body["variables"]["f"]["q"]
        entity_id = self.found.get(kind, {}).get(name)
        if entity_id is None:
            return {"data": {fn: {"count": 0, field: []}}}
        return {"data": {fn: {"count": 1,
                              field: [{"id": entity_id, "name": name, alias_field: []}]}}}

    def _create(self, kind, mut, body):
        name = body["variables"]["in"]["name"]
        outcome = self.create.get((kind, name))
        if isinstance(outcome, Exception):
            raise outcome
        return {"data": {mut: {"id": outcome}}}


def _scene_transport(**scene_fields):
    """Fake transport for the snapshot tests: answers the scene read with
    `scene_fields` (defaulted the same way _SceneTransport's `existing` shape
    is elsewhere in this file) and accepts whatever sceneUpdate is sent,
    recording it. Every performer/tag/studio find-or-create query returns
    "not found" so apply_scene falls through to a create, which this fake
    always grants — the snapshot tests care about what was read and
    returned, not about entity resolution."""
    existing = {"id": "s1", "title": None, "details": None, "date": None,
                "urls": [], "organized": False, "rating100": None, "code": None,
                "director": None, "stash_ids": [], "studio": None,
                "performers": [], "tags": []}
    existing.update(scene_fields)

    def send(body, timeout):
        q = body["query"]
        if "findScene(" in q:
            return {"data": {"findScene": existing}}
        if "findStudios(" in q:
            return {"data": {"findStudios": {"count": 0, "studios": []}}}
        if "findPerformers(" in q:
            return {"data": {"findPerformers": {"count": 0, "performers": []}}}
        if "findTags(" in q:
            return {"data": {"findTags": {"count": 0, "tags": []}}}
        if "studioCreate" in q:
            return {"data": {"studioCreate": {"id": "new-studio"}}}
        if "performerCreate" in q:
            return {"data": {"performerCreate": {"id": "new-performer"}}}
        if "tagCreate" in q:
            return {"data": {"tagCreate": {"id": "new-tag"}}}
        if "sceneUpdate" in q:
            return {"data": {"sceneUpdate": {"id": body["variables"]["in"]["id"]}}}
        raise AssertionError("test transport does not recognize query: %s" % q)

    return send


class _MutableScene:
    """A fake media server that actually holds scene state: findScene reads
    answer from it, sceneUpdate mutations write onto it the way a real server
    would (replacing whatever fields/arrays are sent, leaving every field the
    mutation omits untouched), and studio/performer/tag find-or-create calls
    resolve against a tiny name registry, creating a fresh id the first time a
    name is seen. `writes` counts every sceneUpdate; `snapshot()` returns a
    JSON-round-tripped (so independent, mutation-proof) copy of the current
    state, shaped exactly like apply_scene's `prior` so the two are directly
    comparable in a round-trip assertion. Without a server that genuinely
    mutates like this, a revert test would only be checking the client
    against itself."""

    _FIND = SERVER_FIND
    _CREATE_MUTATION = SERVER_CREATE_MUTATION

    def __init__(self, **fields):
        state = {"title": None, "details": None, "date": None, "urls": [],
                  "organized": False, "rating100": None, "code": None,
                  "director": None, "stash_ids": [], "studio_id": None,
                  "performer_ids": [], "tag_ids": []}
        # accept the same nested shape scene_existing()'s read uses
        # (studio{id}, performers{id}, tags{id}) so callers can build a fake
        # scene the same way they'd read one back
        if "studio" in fields:
            fields["studio_id"] = (fields.pop("studio") or {}).get("id")
        if "performers" in fields:
            fields["performer_ids"] = [p["id"] for p in fields.pop("performers")]
        if "tags" in fields:
            fields["tag_ids"] = [t["id"] for t in fields.pop("tags")]
        state.update(fields)
        self._state = state
        self.writes = 0
        self._registry = {"studio": {}, "performer": {}, "tag": {}}
        self._next_id = 1
        self.transport = self._handle

    def snapshot(self):
        return json.loads(json.dumps(self._state))

    def _handle(self, body, timeout):
        q = body["query"]
        if "findScene(" in q:
            return {"data": {"findScene": self._read_scene()}}
        for kind, (fn, field, alias_field) in self._FIND.items():
            if fn + "(" in q:
                return self._find(kind, fn, field, alias_field, body)
        for kind, mut in self._CREATE_MUTATION.items():
            if mut in q:
                return self._create(kind, mut, body)
        if "sceneUpdate" in q:
            return self._apply_update(body)
        raise AssertionError("test transport does not recognize query: %s" % q)

    def _read_scene(self):
        s = self._state
        return {
            "id": "s1", "title": s["title"], "details": s["details"],
            "date": s["date"], "urls": list(s["urls"]), "organized": s["organized"],
            "rating100": s["rating100"], "code": s["code"], "director": s["director"],
            "stash_ids": list(s["stash_ids"]),
            "studio": {"id": s["studio_id"]} if s["studio_id"] else None,
            "performers": [{"id": pid} for pid in s["performer_ids"]],
            "tags": [{"id": tid} for tid in s["tag_ids"]],
        }

    def _find(self, kind, fn, field, alias_field, body):
        name = body["variables"]["f"]["q"]
        entity_id = self._registry[kind].get(name.strip().lower())
        if entity_id is None:
            return {"data": {fn: {"count": 0, field: []}}}
        return {"data": {fn: {"count": 1,
                              field: [{"id": entity_id, "name": name, alias_field: []}]}}}

    def _create(self, kind, mut, body):
        name = body["variables"]["in"]["name"]
        new_id = "%s-%d" % (kind, self._next_id)
        self._next_id += 1
        self._registry[kind][name.strip().lower()] = new_id
        return {"data": {mut: {"id": new_id}}}

    def _apply_update(self, body):
        # replace exactly what's sent, leave everything else in self._state
        # alone — this is what makes the fake a genuine (if tiny) server
        # rather than an echo chamber
        inp = dict(body["variables"]["in"])
        scene_id = inp.pop("id")
        inp.pop("cover_image", None)  # no representation held in this fake
        self._state.update(inp)
        self.writes += 1
        return {"data": {"sceneUpdate": {"id": scene_id}}}


def _mutable_scene(**fields):
    return _MutableScene(**fields)


class ApplyScene(unittest.TestCase):
    def test_performers_and_tags_are_unioned_not_replaced(self):
        existing = {"id": "1", "studio": None,
                    "performers": [{"id": "1", "name": "Harbor Fox"}],
                    "tags": [{"id": "10", "name": "Existing Tag"}]}
        t = _SceneTransport(existing, found={"performer": {"Quiet Otter": "2"},
                                             "tag": {"New Tag": "20"}})
        match = {"performers": [{"name": "Quiet Otter"}], "tags": [{"name": "New Tag"}]}
        Stash("http://example.test", "k", transport=t).apply_scene("1", match)
        self.assertEqual(t.scene_update_input["performer_ids"], ["1", "2"])
        self.assertEqual(t.scene_update_input["tag_ids"], ["10", "20"])

    def test_drop_tag_ids_removes_a_tag_and_still_writes_with_nothing_new(self):
        existing = {"id": "1", "studio": None, "performers": [],
                    "tags": [{"id": "10", "name": "Keep"}, {"id": "99", "name": "Cohort"}]}
        t = _SceneTransport(existing)
        Stash("http://example.test", "k", transport=t).apply_scene("1", {}, drop_tag_ids=["99"])
        self.assertEqual(t.scene_update_input["tag_ids"], ["10"])

    def test_studio_is_set_when_the_scene_has_none(self):
        existing = {"id": "1", "studio": None, "performers": [], "tags": []}
        t = _SceneTransport(existing, found={"studio": {"New Studio": "6"}})
        result = Stash("http://example.test", "k", transport=t).apply_scene(
            "1", {"studio": {"name": "New Studio"}})
        self.assertEqual(t.scene_update_input["studio_id"], "6")
        self.assertEqual(result["studio_id"], "6")

    def test_studio_is_not_overwritten_by_default(self):
        existing = {"id": "1", "studio": {"id": "5", "name": "Old Studio"},
                    "performers": [], "tags": []}
        t = _SceneTransport(existing, found={"studio": {"New Studio": "6"}})
        result = Stash("http://example.test", "k", transport=t).apply_scene(
            "1", {"studio": {"name": "New Studio"}})
        self.assertNotIn("studio_id", t.scene_update_input)
        self.assertEqual(result["studio_id"], "5")

    def test_studio_is_overwritten_when_asked(self):
        existing = {"id": "1", "studio": {"id": "5", "name": "Old Studio"},
                    "performers": [], "tags": []}
        t = _SceneTransport(existing, found={"studio": {"New Studio": "6"}})
        result = Stash("http://example.test", "k", transport=t).apply_scene(
            "1", {"studio": {"name": "New Studio"}}, overwrite_studio=True)
        self.assertEqual(t.scene_update_input["studio_id"], "6")
        self.assertEqual(result["studio_id"], "6")

    def test_a_permanently_refused_name_is_skipped_and_the_rest_still_lands(self):
        existing = {"id": "1", "studio": None, "performers": [], "tags": []}
        t = _SceneTransport(existing,
                            found={"performer": {"Harbor Fox": "2"}},
                            create={("performer", "Bad Name"):
                                    StashError("name 'Bad Name' is not allowed")})
        match = {"title": "A Title",
                 "performers": [{"name": "Harbor Fox"}, {"name": "Bad Name"}]}
        result = Stash("http://example.test", "k", transport=t).apply_scene("1", match)
        self.assertEqual([s["name"] for s in result["skipped"]], ["Bad Name"])
        self.assertEqual(t.scene_update_input["performer_ids"], ["2"])
        self.assertEqual(t.scene_update_input["title"], "A Title")

    def test_raises_when_every_name_is_refused(self):
        existing = {"id": "1", "studio": None, "performers": [], "tags": []}
        t = _SceneTransport(existing,
                            create={("performer", "Bad Name"):
                                    StashError("name 'Bad Name' is not allowed")})
        match = {"performers": [{"name": "Bad Name"}]}
        with self.assertRaises(StashError):
            Stash("http://example.test", "k", transport=t).apply_scene("1", match)
        self.assertIsNone(t.scene_update_input)  # no partial write

    def test_a_transient_failure_raises_rather_than_being_swallowed(self):
        existing = {"id": "1", "studio": None, "performers": [], "tags": []}
        t = _SceneTransport(existing,
                            create={("performer", "Wedged Name"):
                                    StashError("timeout", transient=True)})
        match = {"performers": [{"name": "Wedged Name"}]}
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t).apply_scene("1", match)
        self.assertTrue(ctx.exception.transient)
        self.assertIsNone(t.scene_update_input)  # no partial write


# -- what the caller does with the flag ------------------------------------ #
#
# Classification only matters through its consequence, and apply_scene is where
# the consequence lands: a permanently refused name is dropped from the write
# and reported in `skipped` (the rest of the scene still gets its title, date
# and every other performer), while a transient failure raises so the row can
# be retried whole and nothing partial is written.
#
# The tests above pin those two behaviours; what they do NOT show is what the
# choice between them is made from. So the fixture below is built ONCE and
# driven twice, with `transient` as the only difference: same scene, same
# match, same performer name, same exception class, same message, refused at
# the same call. Anything a future refactor might key off instead — the
# message text, the exception type, which name it was, how far into the match
# the failure happened — is held identical across the two runs, so keying off
# any of them collapses the two outcomes into one and fails a test here.

# One refusal message, used verbatim for BOTH outcomes. It deliberately reads
# like neither a timeout nor a validation error: nothing in the text hints at
# which side of the line it belongs on, so only the flag can say.
REFUSAL_MESSAGE = "the media server said no to 'Bad Name'"


def _refused_apply(transient):
    """The identical apply, refused the identical way, differing ONLY in
    `transient`. Returns (stash, transport, match) so the caller drives the
    call and asserts on the outcome — result or raise.

    "Harbor Fox" resolves normally alongside the refused name, so the
    permanent run has something left to write (and so it cannot reach
    apply_scene's separate "every name was refused" raise), and so the
    transient run is genuinely choosing to abandon a row that was otherwise
    resolving fine."""
    existing = {"id": "1", "studio": None, "performers": [], "tags": []}
    t = _SceneTransport(
        existing,
        found={"performer": {"Harbor Fox": "2"}},
        create={("performer", "Bad Name"): StashError(REFUSAL_MESSAGE,
                                                      transient=transient)})
    match = {"title": "A Title",
             "performers": [{"name": "Harbor Fox"}, {"name": "Bad Name"}]}
    return Stash("http://example.test", "k", transport=t), t, match


class SkipOrRaiseTurnsOnTheFlag(unittest.TestCase):
    """The retry flag alone decides whether a failed name is skipped or the
    whole row raises."""

    def test_a_permanent_refusal_drops_that_name_and_writes_the_rest(self):
        # HARM: failing the whole row on a name the server will never accept
        # means the scene never gets its title, date, cover or any of its
        # other performers — for one bad name that no retry can fix.
        stash, t, match = _refused_apply(transient=False)
        result = stash.apply_scene("1", match)
        self.assertEqual([s["name"] for s in result["skipped"]], ["Bad Name"])
        self.assertEqual(result["skipped"][0]["error"], REFUSAL_MESSAGE)
        self.assertEqual(t.scene_update_input["performer_ids"], ["2"])
        self.assertEqual(t.scene_update_input["title"], "A Title")

    def test_a_transient_failure_of_the_same_name_raises_instead(self):
        # HARM: swallowing a blip writes the scene WITHOUT that performer and
        # marks it organized. Nothing revisits a scene that already looks
        # done, so a momentary wobble costs it that name permanently.
        stash, t, match = _refused_apply(transient=True)
        with self.assertRaises(StashError) as ctx:
            stash.apply_scene("1", match)
        self.assertTrue(ctx.exception.transient)  # the flag survives the raise
        self.assertIsNone(t.scene_update_input,
                          "the row must be retryable whole — nothing partial "
                          "may land on the server")

    def test_only_the_flag_distinguishes_the_two(self):
        # THE test: it is what fails if a future refactor starts deciding from
        # the exception's message text or its type rather than from the flag.
        permanent, perm_t, perm_match = _refused_apply(transient=False)
        transient, trans_t, trans_match = _refused_apply(transient=True)

        # everything except the flag is held identical between the two runs
        perm_err = perm_t.create[("performer", "Bad Name")]
        trans_err = trans_t.create[("performer", "Bad Name")]
        self.assertEqual(str(perm_err), str(trans_err))       # same message
        self.assertIs(type(perm_err), type(trans_err))        # same type
        self.assertEqual(perm_match, trans_match)             # same call
        self.assertEqual(perm_t.found, trans_t.found)         # same fixture
        self.assertEqual(perm_t.existing, trans_t.existing)
        self.assertNotEqual(perm_err.transient, trans_err.transient)  # only this

        result = permanent.apply_scene("1", perm_match)
        with self.assertRaises(StashError) as ctx:
            transient.apply_scene("1", trans_match)

        # ...and the outcomes are opposites
        self.assertEqual([s["name"] for s in result["skipped"]], ["Bad Name"])
        self.assertIsNotNone(perm_t.scene_update_input,
                             "a permanent refusal must still write the scene")
        self.assertTrue(ctx.exception.transient)
        self.assertIsNone(trans_t.scene_update_input,
                          "a transient failure must write nothing at all")


# -- catalogue links ------------------------------------------------------- #
#
# Invented endpoints. `.invalid` is reserved by RFC 2606 and can never
# resolve, so nothing here can reach a real catalogue even by accident.

CATALOGUE = "https://one.example.invalid/graphql"
OTHER_CATALOGUE = "https://two.example.invalid/graphql"

LINK = {"endpoint": CATALOGUE, "stash_id": "r-77"}
OTHER_LINK = {"endpoint": OTHER_CATALOGUE, "stash_id": "r-31"}
# The same catalogue naming a DIFFERENT scene -- the disagreement, not a
# second link.
CLASHING_LINK = {"endpoint": CATALOGUE, "stash_id": "r-99"}


def _linkable_scene(stash_ids):
    return {"id": "1", "studio": None, "performers": [], "tags": [],
            "stash_ids": list(stash_ids)}


class CatalogueLinkWrite(unittest.TestCase):
    """What `apply_scene` sends when the match carries a `{endpoint,
    stash_id}` link, and what it refuses to send."""

    def apply(self, existing_links, match, create=None):
        t = _SceneTransport(_linkable_scene(existing_links), create=create)
        Stash("http://example.test", "k", transport=t).apply_scene("1", match)
        return t

    def test_the_link_is_written_and_the_whole_update_input_is_this(self):
        # Asserted as ONE whole dict, never key by key. A field slipped into
        # a scene update past a green suite has already happened on this
        # project once, and `rating100` unlisted here would blank the rating
        # on every scene an apply touches.
        t = self.apply([], {"title": "Winter Ledger", "stash_ids": [LINK]})

        self.assertEqual(t.scene_update_input,
                         {"id": "1", "organized": True,
                          "title": "Winter Ledger", "stash_ids": [LINK]})

    def test_a_match_with_no_link_writes_no_stash_ids_key_at_all(self):
        # The whole input again: "no link" has to mean the key is absent, not
        # present and empty. An empty list is a WRITE -- sceneUpdate replaces
        # the list it is sent -- so writing one would delete whatever links
        # the scene already had, on every apply that had none to add.
        t = self.apply([OTHER_LINK], {"title": "Winter Ledger"})

        self.assertEqual(t.scene_update_input,
                         {"id": "1", "organized": True,
                          "title": "Winter Ledger"})

    def test_a_link_from_another_catalogue_survives_the_write(self):
        # HARM: sceneUpdate REPLACES the whole list, so sending only the new
        # pair deletes every link this tool did not make -- silently, and on
        # a scene the row says was merely enriched. A fixture starting from a
        # scene with no links could not tell a merge from a replace at all.
        t = self.apply([OTHER_LINK], {"stash_ids": [LINK]})

        self.assertEqual(t.scene_update_input,
                         {"id": "1", "organized": True,
                          "stash_ids": [OTHER_LINK, LINK]})

    def test_the_same_link_already_present_is_written_back_once(self):
        # The permissive side of the guard, which a refusal-only test leaves
        # open: the same catalogue naming the SAME scene is agreement, and
        # re-applying a proposal must not start refusing because the link it
        # writes is already there. Nor may it duplicate the entry.
        t = self.apply([LINK], {"stash_ids": [LINK]})

        self.assertEqual(t.scene_update_input,
                         {"id": "1", "organized": True, "stash_ids": [LINK]})

    def test_the_same_catalogue_naming_a_different_scene_is_refused(self):
        # HARM: overwriting here settles, by whoever wrote last, a
        # disagreement between the media server and the catalogue about which
        # scene this file IS -- and a wrong link is the corrupted record
        # nobody notices, because the row reads as applied and the scene
        # reads as catalogued. Both ids have to reach the person who has to
        # go and look at the two.
        t = _SceneTransport(_linkable_scene([CLASHING_LINK]))
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t).apply_scene(
                "1", {"title": "Winter Ledger", "stash_ids": [LINK]})

        for fragment in (CATALOGUE, "r-99", "r-77"):
            self.assertIn(fragment, str(ctx.exception))
        self.assertFalse(ctx.exception.transient,
                         "a retry cannot make the two sources agree")
        self.assertIsNone(t.scene_update_input,
                          "the title must not land either -- nothing is "
                          "written when the link is in dispute")

    def test_a_link_in_dispute_costs_the_server_no_created_entity(self):
        # The refusal is knowable from the scene read alone, so it happens
        # before any find-or-create. Creating a studio, performer or tag on
        # the way to a write that was never going to happen leaves rubbish on
        # the server for a proposal that was refused.
        #
        # Asserted on the MUTATIONS SENT, not on the raise: this fake resolves
        # an unknown performer by creating it, and `apply_scene` turns a
        # refused name into a `skipped` entry rather than an error, so a
        # bare `assertRaises(StashError)` here is satisfied by the
        # disagreement raise whether the create happened first or not.
        t = _SceneTransport(_linkable_scene([CLASHING_LINK]))
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t).apply_scene(
                "1", {"stash_ids": [LINK],
                      "performers": [{"name": "Velvet Crane"}]})

        self.assertIn("disagree", str(ctx.exception))  # this raise, not another
        self.assertEqual([q for q in t.queries if "mutation" in q], [],
                         "nothing may be created for a write that was "
                         "refused before it could begin")

    def test_a_match_naming_one_catalogue_twice_over_is_refused(self):
        # HARM: two entries for one endpoint is not a merge under any
        # reading -- it is the match disagreeing with ITSELF about which
        # scene this is, and appending both leaves the scene claiming two
        # different records at one catalogue for good.
        #
        # This is the case that decides whether the comparison is against
        # what is being BUILT or only against what the scene already held:
        # here the scene holds nothing, so a check that consulted only the
        # existing list would find no disagreement and write both.
        t = _SceneTransport(_linkable_scene([]))
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t).apply_scene(
                "1", {"stash_ids": [LINK, CLASHING_LINK]})

        self.assertIn("disagree", str(ctx.exception))
        for fragment in (CATALOGUE, "r-77", "r-99"):
            self.assertIn(fragment, str(ctx.exception))
        self.assertIsNone(t.scene_update_input)

    def test_a_link_for_a_third_catalogue_joins_the_other_two(self):
        # Merging by ENDPOINT, not by position or by count: two links held,
        # one added, and the one added belongs to neither of them.
        t = self.apply([OTHER_LINK, {"endpoint": "https://three.example.invalid/graphql",
                                     "stash_id": "r-5"}],
                       {"stash_ids": [LINK]})

        self.assertEqual(t.scene_update_input["stash_ids"],
                         [OTHER_LINK,
                          {"endpoint": "https://three.example.invalid/graphql",
                           "stash_id": "r-5"},
                          LINK])


class CatalogueLinkUndo(unittest.TestCase):
    """A link written and not reverted is a change the operator cannot take
    back, so the round trip is asserted against a server that really holds
    the state rather than against the snapshot the client returned."""

    def test_undo_restores_a_scene_that_carried_no_links(self):
        # The empty case is the one an apply is most likely to hit, and the
        # one a snapshot is most likely to lose: `[]` and "absent" look alike
        # everywhere except in the restore input.
        server = _mutable_scene(stash_ids=[])
        stash = Stash("http://example.test", "k", transport=server.transport)
        before = server.snapshot()

        info = stash.apply_scene("s1", {"stash_ids": [LINK]})
        self.assertEqual(server.snapshot()["stash_ids"], [LINK])

        stash.revert_scene("s1", info["prior"])
        self.assertEqual(server.snapshot(), before)

    def test_undo_restores_the_links_that_were_there_before(self):
        # A scene that already carried a link from elsewhere: the undo has to
        # put back exactly that list -- not an empty one, and not the merged
        # one the apply wrote.
        server = _mutable_scene(stash_ids=[OTHER_LINK])
        stash = Stash("http://example.test", "k", transport=server.transport)
        before = server.snapshot()

        info = stash.apply_scene("s1", {"stash_ids": [LINK]})
        self.assertEqual(server.snapshot()["stash_ids"], [OTHER_LINK, LINK])

        stash.revert_scene("s1", info["prior"])
        self.assertEqual(server.snapshot(), before)
        self.assertEqual(server.snapshot()["stash_ids"], [OTHER_LINK])


class PriorStateSnapshot(unittest.TestCase):
    def test_apply_returns_the_state_it_replaced(self):
        # the snapshot is what makes an apply reversible; it must describe the
        # scene as it was BEFORE the write, not after
        stash = Stash("http://example.test", "k", transport=_scene_transport(
            title="Old Title", date="2019-01-01",
            performers=[{"id": "p1"}], tags=[{"id": "t1"}],
            studio={"id": "st1"}, organized=False))
        info = stash.apply_scene("s1", {"title": "New Title",
                                        "performers": [], "tags": []})
        prior = info["prior"]
        self.assertEqual(prior["title"], "Old Title")
        self.assertEqual(prior["date"], "2019-01-01")
        self.assertEqual(prior["performer_ids"], ["p1"])
        self.assertEqual(prior["tag_ids"], ["t1"])
        self.assertEqual(prior["studio_id"], "st1")
        self.assertIs(prior["organized"], False)

    def test_snapshot_covers_every_field_the_apply_can_write(self):
        # a field the apply writes but the snapshot omits is a silent hole in
        # undo, and it will not be discovered until someone needs it
        writable = {"title", "details", "date", "urls", "stash_ids",
                    "studio_id", "performer_ids", "tag_ids", "organized"}
        stash = Stash("http://example.test", "k", transport=_scene_transport())
        prior = stash.apply_scene("s1", {"performers": [], "tags": []})["prior"]
        self.assertTrue(writable.issubset(set(prior)),
                        "snapshot missing: %s" % sorted(writable - set(prior)))

    def test_snapshot_is_json_serialisable(self):
        # the store will persist it verbatim
        import json
        stash = Stash("http://example.test", "k", transport=_scene_transport())
        prior = stash.apply_scene("s1", {"performers": [], "tags": []})["prior"]
        json.loads(json.dumps(prior))


# Every key apply_scene's `prior` snapshot can hold (see apply_scene's prior=
# dict in cronicled/stash.py).
FULL_PRIOR_KEYS = {"title", "details", "date", "urls", "organized", "rating100",
                   "code", "director", "stash_ids", "studio_id", "performer_ids",
                   "tag_ids"}

# apply_scene has no write path for these three: its `inp` (the sceneUpdate
# input it builds) never sets rating100, code or director — confirmed by
# reading apply_scene end to end, and matching Task 1's own
# test_snapshot_covers_every_field_the_apply_can_write, whose "writable" set
# excludes exactly these three. An apply-driven round trip can therefore never
# make them differ, so dropping one from a snapshot has nothing to visibly
# fail to restore here. They stay in the snapshot (apply_scene's existing
# contract, unchanged by this ticket) but are excluded from the per-field
# proof below because that proof requires the field to actually move.
_NOT_WRITABLE_BY_APPLY_SCENE = {"rating100", "code", "director"}

# A starting scene with a distinctive, non-empty value in every field the
# snapshot captures, and a match that changes every one of them apply_scene
# can actually write (studio needs overwrite_studio=True below, since the
# scene already has one and apply only claims an unset slot otherwise).
RICH_STATE = dict(
    title="Old Title", details="Old details", date="2019-01-01",
    urls=["http://old.example/1"], organized=False, rating100=50,
    code="OLD-CODE", director="Old Director",
    stash_ids=[{"endpoint": "http://old.example", "stash_id": "old-stash-id"}],
    studio={"id": "st1"}, performers=[{"id": "p1"}], tags=[{"id": "t1"}])

RICH_MATCH = dict(
    title="New Title", details="New details", date="2024-05-05",
    urls=["http://new.example/1"],
    stash_ids=[{"endpoint": "http://new.example", "stash_id": "new-stash-id"}],
    studio={"name": "New Studio"},
    performers=[{"name": "Velvet Crane"}], tags=[{"name": "JOI"}])


class RevertRoundTrip(unittest.TestCase):
    def test_apply_then_revert_restores_the_original_state(self):
        # the round trip is the proof the snapshot is complete: if a field is
        # missing from it, the scene does not come back the same. The match
        # here changes every field apply_scene can write (title, details,
        # date, urls, stash_ids, studio, performers, tags — organized always
        # flips too) so a regression in any one field's restore path would be
        # visible, not hidden behind an untouched field passing through
        # unchanged on both sides of the round trip.
        server = _mutable_scene(**RICH_STATE)
        stash = Stash("http://example.test", "k", transport=server.transport)
        before = server.snapshot()

        info = stash.apply_scene("s1", RICH_MATCH, overwrite_studio=True)
        self.assertNotEqual(server.snapshot(), before)   # the apply really changed it

        stash.revert_scene("s1", info["prior"])
        self.assertEqual(server.snapshot(), before)

    def test_every_captured_field_is_needed_to_restore(self):
        # the snapshot's completeness is the claim this whole change rests on;
        # assert it per-field rather than trusting one spot check. rating100/
        # code/director are excluded — see _NOT_WRITABLE_BY_APPLY_SCENE above.
        for field in sorted(FULL_PRIOR_KEYS - _NOT_WRITABLE_BY_APPLY_SCENE):
            with self.subTest(field=field):
                server = _mutable_scene(**RICH_STATE)
                stash = Stash("http://example.test", "k", transport=server.transport)
                before = server.snapshot()
                info = stash.apply_scene("s1", RICH_MATCH, overwrite_studio=True)
                partial = {k: v for k, v in info["prior"].items() if k != field}
                stash.revert_scene("s1", partial)
                self.assertNotEqual(server.snapshot(), before,
                                    "dropping %r still restored cleanly — the "
                                    "round trip does not actually cover it" % field)

    def test_revert_writes_once(self):
        # a partial revert is worse than none: resolve first, then one write
        server = _mutable_scene(title="Old Title")
        stash = Stash("http://example.test", "k", transport=server.transport)
        info = stash.apply_scene("s1", {"title": "New", "performers": [], "tags": []})
        server.writes = 0
        stash.revert_scene("s1", info["prior"])
        self.assertEqual(server.writes, 1)

    def test_revert_of_an_empty_snapshot_is_refused(self):
        stash = Stash("http://example.test", "k", transport=_scene_transport())
        with self.assertRaises(ValueError):
            stash.revert_scene("s1", None)


# -- tag writes (irreversible) -------------------------------------------- #

class _TagTransport:
    """Fake transport for the tag-write tests, in the style of
    _SceneTransport: it recognizes the two tag mutations, records the exact
    input each one was sent, and answers with a minimal payload shaped like
    the real server's. Opens no socket.

    The recording is the point. merge_tags and update_tag_aliases have no
    read-back and no undo — once the mutation body leaves the client the
    damage is done on the server — so the only thing a test can check is the
    body itself.
    """

    def __init__(self, merged=None, description=None):
        self.calls = []  # (query, input) for every mutation sent, in order
        self.merged = merged or {"id": "tag-canonical", "name": "Lantern Drift",
                                 "aliases": []}
        # What the destination tag's description is on the server right now.
        # `None` -- a tag nobody has described -- is what the real server
        # answers for the overwhelmingly common case, and is the state that
        # makes a carry-over legitimate.
        self.description = description
        self.reads = []

    def __call__(self, body, timeout):
        q = body["query"]
        if "findTag(" in q:
            # A READ, recorded apart from the mutations: `only()` would stop
            # meaning "exactly one mutation" if reads were counted with them,
            # and every existing test here depends on that meaning.
            self.reads.append(body["variables"]["id"])
            return {"data": {"findTag": {"id": body["variables"]["id"],
                                         "description": self.description}}}
        inp = body["variables"]["in"]
        self.calls.append((q, inp))
        if "tagsMerge" in q:
            return {"data": {"tagsMerge": self.merged}}
        if "tagUpdate" in q:
            return {"data": {"tagUpdate": {"id": inp["id"],
                                           "aliases": inp.get("aliases")}}}
        raise AssertionError("test transport does not recognize query: %s" % q)

    def only(self):
        """The single mutation input sent — fails if it was not exactly one."""
        if len(self.calls) != 1:
            raise AssertionError("expected exactly 1 mutation, got %d"
                                 % len(self.calls))
        return self.calls[0][1]


# Invented tag vocabulary: a canonical tag, a misspelling of it that a scrape
# created, and the alias set that should survive the merge.
CANONICAL_TAG_ID = "tag-canonical"
TYPO_TAG_ID = "tag-typo"
SECOND_TYPO_TAG_ID = "tag-typo-2"
MERGED_ALIASES = ["Lantren Drift", "lantern-drift", "Lanterndrift"]


def _tag_stash(transport):
    return Stash("http://example.test", "k", transport=transport)


class MergeTags(unittest.TestCase):
    def test_sources_go_in_source_and_the_destination_in_destination(self):
        # HARM: swapping these deletes the canonical tag and keeps the typo,
        # dragging every scene association onto the misspelling. tagsMerge is
        # a permanent server-side delete — nothing in this module can undo it,
        # and the only evidence of the mistake is which id landed in which
        # role in this one mutation body.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID,
                                 [TYPO_TAG_ID, SECOND_TYPO_TAG_ID])
        inp = t.only()
        self.assertEqual(inp["destination"], CANONICAL_TAG_ID)
        self.assertEqual(inp["source"], [TYPO_TAG_ID, SECOND_TYPO_TAG_ID])
        # and, explicitly: the tag being kept is never among those being
        # destroyed, whatever else the input holds
        self.assertNotIn(CANONICAL_TAG_ID, inp["source"])

    def test_the_alias_set_is_sent_with_the_merge(self):
        # HARM: without it the merged-away spellings are lost with the tags
        # that carried them, and the next scrape recreates the very duplicates
        # this merge was run to remove.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                 aliases=MERGED_ALIASES)
        inp = t.only()
        self.assertEqual(inp["values"],
                         {"id": CANONICAL_TAG_ID, "aliases": MERGED_ALIASES})
        # the alias write must target the tag that survives, not one being
        # merged away, or it lands on a tag the same call is deleting
        self.assertEqual(inp["values"]["id"], inp["destination"])

    def test_an_empty_alias_list_is_sent_as_an_explicit_clear(self):
        # HARM: a truthiness check (`if aliases:`) collapses [] into None, so
        # deliberately clearing a tag's aliases becomes a silent no-op — the
        # caller is told it worked and the stale aliases stay on the server.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID], aliases=[])
        inp = t.only()
        self.assertIn("values", inp)
        self.assertEqual(inp["values"]["aliases"], [])

    def test_aliases_none_omits_the_values_block_entirely(self):
        # HARM: the other half of the same distinction. `values` REPLACES the
        # destination's alias list, so sending it when the caller passed
        # nothing would wipe the destination's existing aliases as a side
        # effect of a merge that was never asked to touch them.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID])
        self.assertNotIn("values", t.only())

    def test_the_merge_actually_issues_a_mutation(self):
        # HARM: a no-op merge leaves the duplicate tags in place while the
        # caller records the consolidation as done, so the pair is never
        # revisited.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID])
        self.assertEqual(len(t.calls), 1)
        self.assertIn("tagsMerge", t.calls[0][0])


# An invented description, and a second one that is a visibly different
# sentence so no assertion below can pass by comparing a value against itself.
CARRIED_DESCRIPTION = "Scenes lit only by a hand-carried lamp."
OTHER_DESCRIPTION = "Filmed aboard a working passenger boat."


class MergeTagsCarriesADescription(unittest.TestCase):
    """The merge deletes the losing spellings. Whatever description lived on
    one of them goes with it unless this carries it across."""

    def test_the_description_rides_in_the_same_mutation_as_the_merge(self):
        # HARM: a second write after the merge can fail on its own, leaving
        # the spelling that held the text already deleted and nothing
        # recording what it said. One mutation cannot half-happen.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                 description=CARRIED_DESCRIPTION)
        inp = t.only()
        self.assertEqual(inp["values"], {"id": CANONICAL_TAG_ID,
                                         "description": CARRIED_DESCRIPTION})
        # and it lands on the tag that survives, never on one being deleted
        self.assertEqual(inp["values"]["id"], inp["destination"])

    def test_no_description_leaves_the_values_block_alone(self):
        # HARM: the other half. `values` REPLACES what it names, so sending a
        # block for a merge that was never asked to touch the description
        # would blank the survivor's own text as a side effect.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID])
        self.assertNotIn("values", t.only())
        self.assertEqual(t.reads, [], "read the destination for no reason")

    def test_aliases_and_a_description_travel_together(self):
        # HARM: one overwriting the other's `values` block silently drops
        # whichever was built first.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                 aliases=MERGED_ALIASES,
                                 description=CARRIED_DESCRIPTION)
        self.assertEqual(t.only()["values"],
                         {"id": CANONICAL_TAG_ID, "aliases": MERGED_ALIASES,
                          "description": CARRIED_DESCRIPTION})

    def test_a_survivor_that_has_gained_a_description_refuses_the_merge(self):
        # HARM: the proposal was made against a survivor with an EMPTY field.
        # Somebody has written one since, and overwriting it with a sentence
        # lifted off another tag is the one outcome here nobody could detect
        # afterwards. The whole merge refuses, so the proposal stays live.
        t = _TagTransport(description=OTHER_DESCRIPTION)
        with self.assertRaises(StashError):
            _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                     description=CARRIED_DESCRIPTION)
        self.assertEqual(t.calls, [], "the merge must not have been sent")

    def test_the_survivor_is_read_before_the_write_and_not_by_the_caller(self):
        # HARM: taking the comparison anywhere else opens a window between it
        # and the write. Read here, one line before, against the tag that
        # survives -- reading a losing spelling would compare the wrong text.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                 description=CARRIED_DESCRIPTION)
        self.assertEqual(t.reads, [CANONICAL_TAG_ID])

    def test_an_empty_description_on_the_survivor_is_not_a_description(self):
        # HARM: a server answering `""` is a tag nobody has described, and
        # refusing on it would block every legitimate carry-over on an
        # install whose server spells "empty" that way.
        for empty in (None, ""):
            with self.subTest(current=empty):
                t = _TagTransport(description=empty)
                _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                         description=CARRIED_DESCRIPTION)
                self.assertEqual(t.only()["values"]["description"],
                                 CARRIED_DESCRIPTION)


    def test_a_description_supplied_as_empty_is_still_a_supplied_description(self):
        # The sentinel that separates "this merge was asked to touch the
        # description" from "it was not" is `None`, never truthiness. Read as
        # truthiness, `""` joins the second class: the survivor is not read,
        # the guard that supplying a description arms never runs, and the
        # caller's argument is dropped on the floor with the merge going ahead
        # regardless. A supplied value being silently discarded is the failure
        # this pins -- the whole values block, so the key going missing is
        # visible rather than sampled around.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                 description="")

        self.assertEqual(t.only()["values"],
                         {"id": CANONICAL_TAG_ID, "description": ""})
        self.assertEqual(t.reads, [CANONICAL_TAG_ID],
                         "supplying a description must arm the staleness read")


class TagDescriptionRead(unittest.TestCase):
    def test_it_answers_what_the_server_holds_right_now(self):
        t = _TagTransport(description=CARRIED_DESCRIPTION)
        self.assertEqual(_tag_stash(t).tag_description(CANONICAL_TAG_ID),
                         CARRIED_DESCRIPTION)

    def test_a_tag_the_server_does_not_know_answers_none(self):
        # HARM: reported as anything else, an apply against a deleted tag
        # would either write to nothing and report success, or raise where a
        # refusal with a reason belongs.
        def missing(body, timeout):
            return {"data": {"findTag": None}}
        self.assertIsNone(_tag_stash(missing).tag_description("gone"))


class ApplyTagDescription(unittest.TestCase):
    def test_it_writes_the_text_when_the_field_is_what_the_proposal_saw(self):
        t = _TagTransport(description=None)
        result = _tag_stash(t).apply_tag_description(
            CANONICAL_TAG_ID, CARRIED_DESCRIPTION, expected=None)
        self.assertEqual(t.only(), {"id": CANONICAL_TAG_ID,
                                    "description": CARRIED_DESCRIPTION})
        self.assertEqual(result, {"prior": {"description": None}})

    def test_a_field_written_since_the_pass_refuses_rather_than_overwriting(self):
        # HARM: a description somebody wrote replaced by one lifted from a
        # third-party index, with nothing recording that it happened.
        t = _TagTransport(description=OTHER_DESCRIPTION)
        with self.assertRaises(StashError):
            _tag_stash(t).apply_tag_description(
                CANONICAL_TAG_ID, CARRIED_DESCRIPTION, expected=None)
        self.assertEqual(t.calls, [], "nothing may have been written")

    def test_the_snapshot_is_the_text_the_write_replaced(self):
        # HARM: a snapshot built from the proposal rather than from the read
        # would restore what the pass saw, not what the write displaced.
        t = _TagTransport(description="")
        result = _tag_stash(t).apply_tag_description(
            CANONICAL_TAG_ID, CARRIED_DESCRIPTION, expected="")
        self.assertEqual(result["prior"], {"description": ""})

    def test_expected_has_no_default(self):
        # HARM: a caller who forgot it and a caller who meant "write
        # regardless" must not be able to write the same thing.
        with self.assertRaises(TypeError):
            _tag_stash(_TagTransport()).apply_tag_description(
                CANONICAL_TAG_ID, CARRIED_DESCRIPTION)


class RevertTagDescription(unittest.TestCase):
    def test_it_writes_back_exactly_what_the_snapshot_holds(self):
        t = _TagTransport()
        _tag_stash(t).revert_tag_description(
            CANONICAL_TAG_ID, {"description": OTHER_DESCRIPTION})
        self.assertEqual(t.only(), {"id": CANONICAL_TAG_ID,
                                    "description": OTHER_DESCRIPTION})

    def test_a_tag_that_had_no_description_is_restored_to_having_none(self):
        # HARM: an undo that skipped this leaves the third-party sentence in
        # place while reporting the write taken back.
        for prior in (None, ""):
            with self.subTest(prior=prior):
                t = _TagTransport()
                _tag_stash(t).revert_tag_description(
                    CANONICAL_TAG_ID, {"description": prior})
                self.assertEqual(t.only()["description"], prior)

    def test_a_snapshot_that_cannot_describe_anything_refuses(self):
        # HARM: a revert that no-ops is indistinguishable from one that
        # worked, which is the one ambiguity undo cannot afford.
        for prior in (None, {}, {"aliases": []}):
            with self.subTest(prior=prior):
                t = _TagTransport()
                with self.assertRaises(ValueError):
                    _tag_stash(t).revert_tag_description(CANONICAL_TAG_ID, prior)
                self.assertEqual(t.calls, [])


class UpdateTagAliases(unittest.TestCase):
    def test_it_writes_the_list_it_was_given(self):
        # HARM: writing [] (or any list other than the caller's) instead
        # replaces the tag's aliases with nothing — one call wipes every
        # spelling variant a user curated by hand, with no snapshot taken and
        # no undo path in this module.
        t = _TagTransport()
        _tag_stash(t).update_tag_aliases(CANONICAL_TAG_ID, MERGED_ALIASES)
        inp = t.only()
        self.assertEqual(inp["aliases"], MERGED_ALIASES)
        self.assertEqual(inp["id"], CANONICAL_TAG_ID)

    def test_it_actually_issues_a_mutation(self):
        # HARM: making this a no-op survives the rest of the suite, so an
        # alias top-up would silently never persist — every later run would
        # recompute the same "missing" aliases and believe it had written them.
        t = _TagTransport()
        _tag_stash(t).update_tag_aliases(CANONICAL_TAG_ID, MERGED_ALIASES)
        self.assertEqual(len(t.calls), 1)
        self.assertIn("tagUpdate", t.calls[0][0])


# -- scraping --------------------------------------------------------------- #
#
# scrape_scenes_by_query is the only thing in this project that can execute a
# search: scan.examine's `search` callable has had no production
# implementation, so this is the seam a later task wires the scan to, and
# nothing here has ever run against a real server. That raises the stakes on
# the one failure mode this project has already hit twice: a selection set
# whose test only compares the request against the very query constant the
# client built, so a field silently dropped from both sides at once leaves
# the suite green. Two queries elsewhere in this codebase were once reducible
# to a bare `id` that way. So every test below that touches the selection set
# or the argument shape reads the ACTUAL request scrape_scenes_by_query sent
# — through _QueryRecorder, parsed by the same structural helpers the rest of
# this file's binding tests use (_bound_arguments, _declared_type_of,
# _selection_of) — and asserts field names and argument bindings, never a
# second copy of the query text.

SCRAPER_ID = "scraper-velvet-index"
SCRAPE_QUERY_TEXT = "Velvet Crane - Copper Kettle"


def _scraped_scene(**fields):
    """One ScrapedScene-shaped row, as scrapeSingleScene would answer it."""
    row = {"title": "Copper Kettle", "code": None, "details": None,
           "director": None, "urls": [], "date": None, "image": None,
           "studio": None, "tags": [], "performers": []}
    row.update(fields)
    return row


class ScrapeScenesByQuery(unittest.TestCase):
    def test_it_returns_the_scenes_the_scraper_found(self):
        scene = _scraped_scene(title="Copper Kettle")
        t = _transport([{"data": {"scrapeSingleScene": [scene]}}])
        got = _read_stash(t).scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)
        self.assertEqual(got, [scene])

    def test_no_match_is_an_empty_list_not_an_error(self):
        # HARM: scan.examine treats an empty catalogue as the ordinary
        # "nothing to identify this file with" case (MUTE_NO_CANDIDATES), not
        # a failure. Raising here, or handing back something a caller must
        # special-case, would turn every plain miss — the common case for an
        # obscure creator — into an error scan._examine has to catch instead
        # of a result it can act on directly.
        t = _transport([{"data": {"scrapeSingleScene": []}}])
        got = _read_stash(t).scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)
        self.assertEqual(got, [])

    def test_a_null_result_is_normalised_to_an_empty_list(self):
        # HARM: the schema this method is built against was read off the
        # upstream project's published files because the live instance
        # refused introspection — it is good but UNVERIFIED, and declares
        # scrapeSingleScene non-null. `examine` calls `list(search(...))`
        # unconditionally, so a server that answers `null` for "nothing
        # matched" (a schema mismatch, a version difference) must not reach
        # it as None: that is a TypeError on every miss instead of the empty
        # catalogue examine already knows how to handle.
        t = _transport([{"data": {"scrapeSingleScene": None}}])
        got = _read_stash(t).scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)
        self.assertEqual(got, [])
        self.assertIsInstance(got, list)


def _scrape_request(query=SCRAPE_QUERY_TEXT, scraper_id=SCRAPER_ID):
    t = _QueryRecorder({"data": {"scrapeSingleScene": []}})
    _binding_stash(t).scrape_scenes_by_query(scraper_id, query)
    return t.only()


class ScrapeScenesByQueryBindings(unittest.TestCase):
    def test_the_scraper_id_binds_to_source_and_the_text_binds_to_input(self):
        # HARM: `source` and `input` take opposite roles — WHICH scraper
        # answers versus WHAT it is asked — and swapping them is invisible to
        # a check that only inspects the variables dict, which holds the same
        # two values either way. Bound the wrong way round the server refuses
        # every call, so scrape_scenes_by_query — the one thing that can
        # execute a search this whole project has been waiting on — never
        # works at all.
        query, variables = _scrape_request()
        self.assertEqual(
            _bound_arguments(query, variables, "scrapeSingleScene"),
            {"source": {"scraper_id": SCRAPER_ID},
             "input": {"query": SCRAPE_QUERY_TEXT}})

    def test_each_argument_declares_the_type_the_schema_gives_it(self):
        # HARM: the bindings above pass even if the operation header declares
        # the wrong type for whichever variable they are bound to — a swap of
        # the two DECLARATIONS is a different mutation than a swap of the two
        # ARGUMENTS, and the server rejects it at parse time just the same.
        query, _ = _scrape_request()
        self.assertEqual(
            _declared_type_of(query, "scrapeSingleScene", "source"),
            "ScraperSourceInput!")
        self.assertEqual(
            _declared_type_of(query, "scrapeSingleScene", "input"),
            "ScrapeSingleSceneInput!")

    def test_the_selection_set_names_every_field_apply_scene_can_write(self):
        # HARM: what a query does not select, the server never sends, and a
        # dropped field is silent — nothing raises, the key is simply absent
        # forever. `title` is the one scan.examine cannot run without
        # (candidates are scored on it); the rest is exactly what
        # Stash.apply_scene can write onto a scene, read off its own body.
        # `image` is here too, and deliberately: see the docstring test below
        # for why dropping it is worse than dropping an ordinary field.
        selection = _selection_of(_scrape_request()[0], "scrapeSingleScene")
        for field in ("title", "code", "details", "director", "urls",
                     "date", "image", "studio", "tags", "performers"):
            with self.subTest(field=field):
                self.assertIn(field, selection,
                              "%s is writable by apply_scene and is not "
                              "being asked for" % (field,))

    def test_studio_performers_and_tags_carry_the_ids_apply_scene_resolves_by(self):
        # HARM: apply_scene's find_or_create takes a `stored_id` alongside
        # `name` for each of these three — the server's own "you already have
        # this one" signal, which is exactly the shape ScrapedStudio /
        # ScrapedPerformer / ScrapedTag take on the wire. Selecting `name`
        # alone would still let a scene apply, but every single one of these
        # would be re-resolved by name search on every scrape, even for an
        # entity the server itself already matched.
        selection = _selection_of(_scrape_request()[0], "scrapeSingleScene")
        for block in ("studio", "tags", "performers"):
            with self.subTest(block=block):
                self.assertEqual(set(selection[block]), {"stored_id", "name"})


class DeprecatedSingularUrlIsStillSelected(unittest.TestCase):
    """`ScrapedScene.url` (singular) is schema-deprecated in favour of `urls`
    — but `cronicled.adapters.declarative.DeclarativeAdapter.owner_of` is the
    one existing consumer of a scraped result's URL in this codebase, and it
    reads `result.get("url")`, singular, for every adapter configured with
    `owner_source: "url_segment"` (the shape `config/adapters.example.json`'s
    own example adapter uses). apply_scene needs no help here — its own
    `urls`/`url` fallback already prefers the plural field — so dropping
    `url` would not touch the apply path at all; it would silently leave
    every url_segment adapter's `owner_of` reading nothing, which resolves to
    an unresolved creator and mutes the file. Nothing raises anywhere in that
    chain, which is exactly the shape of failure this project's briefs keep
    finding: a field silently missing rather than an error."""

    def test_the_deprecated_singular_url_is_selected_alongside_urls(self):
        selection = _selection_of(_scrape_request()[0], "scrapeSingleScene")
        self.assertIn("url", selection)
        self.assertIn("urls", selection)


class CoverImageIsIrreversible(unittest.TestCase):
    """The one field here that is not like the others: `apply_scene` can
    write it, but its undo snapshot cannot represent a scene's CURRENT
    cover — only a URL is exposed for that, never the base64 payload an
    update accepts — so a cover applied from what this method returns can
    never be reverted. Selecting it anyway is a decision already taken with
    the project owner, not something this task may undo; what this task owns
    is making sure that is documented where a reader of this method will
    meet it."""

    def test_the_docstring_states_the_cover_cannot_be_undone(self):
        # HARM: the whole point of writing this down is that a person reads
        # it before wiring an auto-apply path to a result carrying `image`.
        # A docstring that selects the field without saying so is the silent
        # version of the exact harm the brief for this task exists to close.
        doc = Stash.scrape_scenes_by_query.__doc__
        self.assertIn("cannot be undone", doc)
        self.assertIn("image", doc)

    def test_nothing_here_claims_the_undo_is_complete(self):
        # HARM: the inverse overclaim — a comment or docstring that describes
        # apply_scene's undo as whole would be read as "safe to auto-apply
        # blindly", which is false for exactly the cover field this method
        # selects. Checked across both new methods' docs, not just the one
        # that mentions the cover, since the false claim would be just as
        # wrong sitting beside the other.
        for doc in (Stash.scrape_scenes_by_query.__doc__,
                   Stash.scrape_scene_url.__doc__,
                   Stash.scene_scrapers.__doc__):
            for overclaim in ("fully reversible", "can always be undone",
                             "undo is complete", "safe to undo"):
                self.assertNotIn(overclaim, doc.lower())


# -- scraping a clip page directly, by URL ---------------------------------- #
#
# `scrape_scene_url` is the enrichment call: given the winning candidate's
# own URL, it asks the media server to scrape that page directly rather than
# answer from a free-text search index, which is what lets a proposal carry
# a creator, a studio and a date instead of just a title and a link. Same
# discipline as `ScrapeScenesByQuery` above: every test that touches the
# selection set or the argument shape reads the ACTUAL request through
# `_QueryRecorder`, never a second copy of the query text.

SCRAPE_URL = "https://example.invalid/clip/copper-kettle"


def _scrape_url_request(url=SCRAPE_URL):
    t = _QueryRecorder({"data": {"scrapeSceneURL": None}})
    _binding_stash(t).scrape_scene_url(url)
    return t.only()


class ScrapeSceneURL(unittest.TestCase):
    def test_it_returns_the_scene_the_scraper_found(self):
        scene = _scraped_scene(title="Copper Kettle")
        t = _transport([{"data": {"scrapeSceneURL": scene}}])
        got = _read_stash(t).scrape_scene_url(SCRAPE_URL)
        self.assertEqual(got, scene)

    def test_no_match_is_none_not_an_error(self):
        # HARM: `scan.examine`'s enrichment step treats "the scraper had
        # nothing new for this URL" as an ordinary miss, keeping its thin
        # candidate rather than treating a plain `None` as a failure to
        # degrade from.
        t = _transport([{"data": {"scrapeSceneURL": None}}])
        got = _read_stash(t).scrape_scene_url(SCRAPE_URL)
        self.assertIsNone(got)

    def test_a_missing_field_in_the_payload_is_also_none(self):
        # HARM: the same schema-drift residual `scrape_scenes_by_query`
        # guards against — a server answering a payload with no
        # `scrapeSceneURL` key at all (rather than an explicit `null`) must
        # not raise a bare KeyError somewhere downstream instead of the
        # ordinary "nothing new" outcome.
        t = _transport([{"data": {}}])
        got = _read_stash(t).scrape_scene_url(SCRAPE_URL)
        self.assertIsNone(got)


class ScrapeSceneURLBindings(unittest.TestCase):
    def test_the_url_binds_to_the_url_argument(self):
        query, variables = _scrape_url_request()
        self.assertEqual(_bound_arguments(query, variables, "scrapeSceneURL"),
                         {"url": SCRAPE_URL})

    def test_the_url_travels_as_a_variable_not_baked_into_the_query(self):
        query, _ = _scrape_url_request()
        self.assertIn("$url", query)
        self.assertNotIn(SCRAPE_URL, query)

    def test_the_url_argument_declares_the_type_the_schema_gives_it(self):
        query, _ = _scrape_url_request()
        self.assertEqual(_declared_type_of(query, "scrapeSceneURL", "url"),
                         "String!")

    def test_the_selection_set_names_every_field_apply_scene_can_write(self):
        # The same guard `ScrapeScenesByQueryBindings` runs for the name
        # search, repeated here because this is a SECOND place a field can
        # go missing from the wire independently of the first.
        selection = _selection_of(_scrape_url_request()[0], "scrapeSceneURL")
        for field in ("title", "code", "details", "director", "urls",
                     "date", "image", "studio", "tags", "performers"):
            with self.subTest(field=field):
                self.assertIn(field, selection,
                              "%s is writable by apply_scene and is not "
                              "being asked for" % (field,))

    def test_studio_performers_and_tags_carry_the_ids_apply_scene_resolves_by(self):
        selection = _selection_of(_scrape_url_request()[0], "scrapeSceneURL")
        for block in ("studio", "tags", "performers"):
            with self.subTest(block=block):
                self.assertEqual(set(selection[block]), {"stored_id", "name"})

    def test_the_selection_set_is_identical_to_scrape_scenes_by_query(self):
        # HARM: the whole reason `examine`'s enrichment step may replace a
        # thin search result with what this method returns is that both
        # calls promise the SAME shape for a `ScrapedScene`. Compared as the
        # selection sets pulled from each ACTUAL request — not against a
        # hand-copied field list that could drift from either query on its
        # own — so a field dropped from either one alone, or added to only
        # one, fails here even though each method's own field-by-field test
        # above might still pass in isolation.
        by_query = _selection_of(_scrape_request()[0], "scrapeSingleScene")
        by_url = _selection_of(_scrape_url_request()[0], "scrapeSceneURL")
        self.assertEqual(by_query, by_url)


def _scrapers_request(**kwargs):
    t = _QueryRecorder({"data": {"listScrapers": []}})
    _binding_stash(t).scene_scrapers(**kwargs)
    return t.only()


class SceneScrapers(unittest.TestCase):
    def test_it_returns_what_the_server_configured(self):
        # HARM: a caller uses this to tell an operator which scraper ids are
        # actually usable, rather than failing later on a bare id nobody can
        # check. Losing a row here is the tool silently under-reporting what
        # is available.
        rows = [{"id": SCRAPER_ID, "name": "Velvet Index"},
               {"id": "scraper-other", "name": "Harbour Registry"}]
        t = _transport([{"data": {"listScrapers": rows}}])
        self.assertEqual(_read_stash(t).scene_scrapers(), rows)

    def test_nothing_configured_is_an_empty_list_not_an_error(self):
        t = _transport([{"data": {"listScrapers": []}}])
        self.assertEqual(_read_stash(t).scene_scrapers(), [])

    def test_a_null_result_is_normalised_to_an_empty_list(self):
        # HARM: same schema-drift residual as the scrape read above — a
        # server answering `null` where the published schema promises a
        # non-null list must not reach a caller doing `for s in
        # scene_scrapers()` as a crash.
        t = _transport([{"data": {"listScrapers": None}}])
        got = _read_stash(t).scene_scrapers()
        self.assertEqual(got, [])
        self.assertIsInstance(got, list)


class SceneScrapersBindings(unittest.TestCase):
    def test_the_types_argument_asks_for_scene_scrapers_specifically(self):
        # HARM: `listScrapers` takes `types: [ScrapeContentType!]!` — read
        # off the schema rather than guessed. Sending the wrong content type,
        # or none at all in a way the server accepts as "everything", answers
        # a different question than "what can scrape a SCENE": a gallery- or
        # movie-only scraper would be reported as available for a scene
        # search and fail the moment it is actually used.
        query, variables = _scrapers_request()
        self.assertEqual(
            _bound_arguments(query, variables, "listScrapers"),
            {"types": ["SCENE"]})
        self.assertEqual(
            _declared_type_of(query, "listScrapers", "types"),
            "[ScrapeContentType!]!")

    def test_the_selection_carries_a_name_a_caller_can_show_an_operator(self):
        # HARM: the whole reason this method exists rather than a caller
        # guessing a bare scraper id is so an operator can be told what is
        # available in words they can check. Selecting `id` alone defeats
        # that: a caller would still have nothing but an id to fail with.
        selection = _selection_of(_scrapers_request()[0], "listScrapers")
        self.assertIn("id", selection)
        self.assertIn("name", selection)


# -- which scenes a bulk run writes to ------------------------------------- #

class _ReadTransport:
    """Fake transport for the read paths that choose which scenes a bulk run
    writes to, in the style of _TagTransport: it recognizes the two find
    queries, records the variables each one was sent, and answers with a
    minimal payload shaped like the real server's. Opens no socket.

    Recording the variables verbatim is the point. These reads produce the
    worklist a later apply writes to, and the only description of "which
    scenes" is the filter dictionary in the request body — by the time a wrong
    filter is visible in the results, the scenes have already been written to.

    `tag_pages` is a list of findTags result pages, served by the requested
    page number, so paging can be observed rather than assumed. By default the
    reported `count` is the true total across those pages; `tag_count`
    overrides it, which is how a fixture makes the two arms of all_tags' stop
    condition DISAGREE (a real server's count can over-report — rows deleted
    between pages, a count computed before a filter). While they agree, either
    arm alone ends the read and neither is really pinned.

    Past the last page it serves empty pages, and refuses after
    `max_tag_pages` requests: a client that never stops would otherwise walk
    empty pages forever, and a test that hangs is not a test that fails.
    """

    def __init__(self, scenes=None, count=None, tag_pages=None, tag_count=None,
                 max_tag_pages=6):
        self.calls = []  # (query, variables) for every request sent, in order
        self.scenes = [] if scenes is None else scenes
        self.count = len(self.scenes) if count is None else count
        self.tag_pages = [[]] if tag_pages is None else tag_pages
        self.tag_count = tag_count
        self.max_tag_pages = max_tag_pages
        self.tag_requests = 0

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        if "findScenes(" in q:
            return {"data": {"findScenes": {"count": self.count,
                                            "scenes": self.scenes}}}
        if "findTags(" in q:
            self.tag_requests += 1
            if self.tag_requests > self.max_tag_pages:
                raise AssertionError(
                    "the client is still asking for tag pages after %d requests "
                    "— it never stopped" % self.max_tag_pages)
            page = (variables.get("f") or {}).get("page", 1)
            rows = self.tag_pages[page - 1] if page <= len(self.tag_pages) else []
            total = sum(len(p) for p in self.tag_pages)
            return {"data": {"findTags": {
                "count": total if self.tag_count is None else self.tag_count,
                "tags": rows}}}
        raise AssertionError("test transport does not recognize query: %s" % q)

    def only(self):
        """The variables of the single request sent — fails if it was not
        exactly one, so a test cannot pass by inspecting the wrong call."""
        if len(self.calls) != 1:
            raise AssertionError("expected exactly 1 request, got %d"
                                 % len(self.calls))
        return self.calls[0][1]

    def scene_filter(self):
        """The `scene_filter` of the single request — the WHOLE dictionary, so
        an assertion against it notices an added key as well as a changed one."""
        return self.only()["s"]

    def find_filter(self):
        """The paging/sort `filter` of the single request, whole."""
        return self.only()["f"]


# Invented cohort: one tag a scan is pointed at, and two scenes carrying it.
COHORT_TAG_ID = "tag-cohort"
COHORT_TAG_NAME = "Lantern Drift"
SCENE_ROWS = [{"id": "sc-1", "title": "Harbour Fog", "date": "2021-03-04",
               "files": [{"basename": "harbour-fog.mp4", "path": "/m/harbour-fog.mp4"}],
               "studio": None, "performers": [], "tags": []},
              {"id": "sc-2", "title": "Quiet Tide", "date": None,
               "files": [{"basename": "quiet-tide.mp4", "path": "/m/quiet-tide.mp4"}],
               "studio": None, "performers": [], "tags": []}]

# Mirrors the page size all_tags() requests; a page shorter than this is what
# tells it to stop.
TAG_PAGE_SIZE = 500


def _tag_rows(n, prefix):
    return [{"id": "%s-%d" % (prefix, i), "name": "%s %d" % (prefix, i),
             "aliases": [], "description": None, "scene_count": 0}
            for i in range(n)]


def _read_stash(transport):
    return Stash("http://example.test", "k", transport=transport)


class FindScenes(unittest.TestCase):
    def test_limit_none_asks_the_server_for_every_page_in_one_read(self):
        # HARM: per_page -1 is the server's "all of them"; any positive number
        # silently truncates the worklist, so a full-library scan quietly
        # covers only the first slice and the rest is reported as done.
        #
        # PINS CURRENT BEHAVIOUR, and the behaviour carries a known risk:
        # _find_scenes does NOT page. It sends ONE request with per_page -1 and
        # trusts the server to return everything, where all_tags() loops until
        # a page comes back short. If a server (or a proxy in front of it) caps
        # -1 at some maximum, this read is silently truncated and nothing in
        # the client can tell — unlike all_tags, which would at least keep
        # asking. A "full library" scan would then cover only the cap and
        # report itself complete. Flagged, not fixed: changing the read is a
        # behaviour change and this ticket only pins what is here today.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t)._find_scenes({"organized": False}, None)
        self.assertEqual(t.find_filter(),
                         {"per_page": -1, "page": 1, "sort": "id", "direction": "ASC"})

    def test_a_limit_is_passed_through_as_the_page_size(self):
        # HARM: ignoring the caller's limit turns a deliberately small trial
        # run ("do 5 and let me check them") into a library-wide one.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t)._find_scenes({"organized": False}, 5)
        self.assertEqual(t.find_filter(),
                         {"per_page": 5, "page": 1, "sort": "id", "direction": "ASC"})

    def test_the_scene_filter_is_forwarded_unchanged(self):
        # HARM: this is the seam every selector below relies on. If the filter
        # is edited on the way through, each caller's carefully-scoped cohort
        # becomes something else entirely.
        t = _ReadTransport(scenes=SCENE_ROWS)
        sent = {"tags": {"value": [COHORT_TAG_ID], "modifier": "INCLUDES"}}
        _read_stash(t)._find_scenes(sent, 10)
        self.assertEqual(t.scene_filter(), sent)

    def test_it_returns_the_count_and_the_rows(self):
        # HARM: returning the page length as the count hides from the caller
        # that a limited read left scenes behind.
        t = _ReadTransport(scenes=SCENE_ROWS, count=97)
        count, scenes = _read_stash(t)._find_scenes({"organized": False}, 2)
        self.assertEqual(count, 97)
        self.assertEqual(scenes, SCENE_ROWS)


class UnorganizedScenes(unittest.TestCase):
    def test_it_asks_only_for_scenes_the_user_has_not_organized(self):
        # HARM: this filter is the ONLY thing keeping a bulk apply off scenes
        # the user curated by hand. Inverting it aims the run squarely at
        # them; dropping it aims the run at the whole library. Either way
        # human-entered metadata is overwritten with scraped guesses, at
        # scale, and apply's undo is per-scene. Asserted as the whole
        # dictionary: an extra key here would narrow or widen the cohort just
        # as effectively as a changed one.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t).unorganized_scenes(None)
        self.assertEqual(t.scene_filter(), {"organized": False})
        # False, not 0/None/"" — the value is sent to a GraphQL Boolean
        self.assertIs(t.scene_filter()["organized"], False)

    def test_the_limit_reaches_the_server(self):
        # HARM: the same trial-run harm as above, via the public entry point
        # the CLI actually calls.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t).unorganized_scenes(3)
        self.assertEqual(t.find_filter()["per_page"], 3)


class TaggedScenes(unittest.TestCase):
    def test_it_selects_the_scenes_that_carry_the_tag(self):
        # HARM: INCLUDES -> EXCLUDES inverts the cohort — the run then applies
        # one cohort's metadata to every scene OUTSIDE it, which is the whole
        # library minus the handful that were meant to be touched. Asserted
        # whole, so a stray extra key cannot ride along unnoticed.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t).tagged_scenes(COHORT_TAG_ID, None)
        self.assertEqual(t.scene_filter(),
                         {"tags": {"value": [COHORT_TAG_ID], "modifier": "INCLUDES"}})

    def test_it_sends_no_organized_key_at_all(self):
        # HARM: the method exists to REVISIT a cohort, and a cohort worth
        # revisiting was usually marked organized by an earlier guessed-
        # metadata pass. Adding `organized: False` silently empties the run;
        # adding `organized: True` narrows it to the already-done ones. The
        # absence is load-bearing, so pin the absence, not just what is there.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t).tagged_scenes(COHORT_TAG_ID, 10)
        self.assertNotIn("organized", t.scene_filter())

    def test_the_limit_reaches_the_server(self):
        # HARM: as above — a capped trial run must stay capped.
        t = _ReadTransport(scenes=SCENE_ROWS)
        _read_stash(t).tagged_scenes(COHORT_TAG_ID, 7)
        self.assertEqual(t.find_filter()["per_page"], 7)


class SceneIds(unittest.TestCase):
    """The read that decides which stored subjects still exist.

    Every assertion here is about a decision that, made wrongly, silently
    discards a person's mutes, dismissals and refusals -- see
    `cronicled.scan.sweep_gone`, which is what interprets what this returns.
    """

    # Ids as the server sends them, and DELIBERATELY not the shape the store
    # keys by: a running server answers `id` as a GraphQL ID, which arrives as
    # a string, but a fixture that only ever used strings could not tell a
    # client that coerces from one that passes through -- and a subject id that
    # reached the store as an int matches nothing there, so every stored
    # decision reads as a deleted file.
    ROWS = [{"id": "4"}, {"id": 11}, {"id": "9"}]

    def test_it_asks_for_the_whole_library_in_one_request(self):
        # HARM: any positive per_page truncates the answer, and every id past
        # the cap is a present file this would report as deleted.
        t = _ReadTransport(scenes=self.ROWS)
        _read_stash(t).scene_ids()
        self.assertEqual(t.find_filter(),
                         {"per_page": -1, "page": 1, "sort": "id",
                          "direction": "ASC"})

    def test_it_narrows_the_library_by_nothing_at_all(self):
        # HARM: the ENTIRE point. A scene_filter here -- `organized: False`
        # most plausibly, copied from the pool read next door -- would answer
        # with the unorganized set, and every organized scene in the library
        # would then be absent from the answer and marked gone. That is
        # thousands of present files in one sweep. The absence is
        # load-bearing, so the absence is what is pinned.
        t = _ReadTransport(scenes=self.ROWS)
        _read_stash(t).scene_ids()
        self.assertNotIn("s", t.calls[0][1])
        self.assertNotIn("scene_filter", t.calls[0][0])

    def test_it_selects_ids_and_no_other_field(self):
        # HARM: selecting a scene's whole shape for every scene in a library
        # is thousands of rows of payload fetched to learn one bit each. The
        # fields are named individually rather than by comparing the query to
        # a constant the client also reads: a shared constant moves with the
        # code and can only ever confirm the code agrees with itself.
        t = _ReadTransport(scenes=self.ROWS)
        _read_stash(t).scene_ids()
        query = t.calls[0][0]
        self.assertIn("findScenes(", query)
        self.assertIn("count", query)
        self.assertIn("id", query)
        for extravagance in ("title", "files", "basename", "path", "studio",
                             "performers", "tags", "date"):
            self.assertNotIn(extravagance, query)

    def test_it_returns_the_servers_own_count_beside_the_ids(self):
        # HARM: the count is the ONLY thing a caller can check the id list
        # against. Returning `len(ids)` in its place would make the
        # completeness guard downstream compare a number with itself and agree
        # with every truncated read there is.
        t = _ReadTransport(scenes=self.ROWS, count=3)
        count, ids = _read_stash(t).scene_ids()
        self.assertEqual(count, 3)
        self.assertEqual(ids, ["4", "11", "9"])

    def test_a_count_that_disagrees_with_the_rows_is_reported_as_it_came(self):
        # HARM: repairing the disagreement here -- answering `len(ids)`, or
        # raising -- destroys the one signal that says the read was partial.
        # This method reports what the server said; deciding what a
        # disagreement means belongs to the caller that would do the marking.
        t = _ReadTransport(scenes=self.ROWS, count=6289)
        count, ids = _read_stash(t).scene_ids()
        self.assertEqual(count, 6289)
        self.assertEqual(len(ids), 3)

    def test_every_id_comes_back_as_a_string(self):
        # HARM: the store keys a mute and a refusal by the STRING form of a
        # subject id. An int here matches none of them, so every stored
        # decision about a present file reads as a decision about a deleted
        # one. `assertIs` on the type, because `11 == "11"` is False but
        # `["11"] == [11]` failing is the same assertion made weakly.
        t = _ReadTransport(scenes=self.ROWS)
        _, ids = _read_stash(t).scene_ids()
        self.assertEqual([type(one) for one in ids], [str, str, str])

    def test_an_empty_library_is_an_answer_not_an_error(self):
        # The client reports it; refusing to act on it is the caller's rule.
        t = _ReadTransport(scenes=[], count=0)
        self.assertEqual(_read_stash(t).scene_ids(), (0, []))

    def test_a_server_error_reaches_the_caller_as_a_StashError(self):
        # HARM: an error swallowed into an empty list here is a library that
        # reports every scene deleted at once.
        t = _transport([{"errors": [{"message": "no"}]}])
        with self.assertRaises(StashError):
            _read_stash(t).scene_ids()


class TagIdByName(unittest.TestCase):
    def test_the_name_is_matched_exactly(self):
        # HARM: EQUALS -> MATCHES (or INCLUDES) resolves a short tag name to a
        # longer one that merely contains it, and the whole cohort scan then
        # runs against the wrong tag — writing one cohort's metadata onto
        # another's scenes. Asserted whole: an extra key in the tag filter
        # would re-scope the lookup just as silently.
        t = _ReadTransport(tag_pages=[[{"id": COHORT_TAG_ID,
                                        "name": COHORT_TAG_NAME,
                                        "scene_count": 2}]])
        _read_stash(t).tag_id_by_name(COHORT_TAG_NAME)
        self.assertEqual(t.only()["f"],
                         {"name": {"value": COHORT_TAG_NAME, "modifier": "EQUALS"}})

    def test_it_returns_the_id_of_the_first_row(self):
        # HARM: returning the name (or the row) instead of the id sends a
        # string where the scene filter expects an id, and the cohort read
        # comes back empty — a scan that reports "nothing to do" rather than
        # failing. Picking a later row picks a different tag.
        #
        # PINS CURRENT BEHAVIOUR, and the behaviour carries a known risk: the
        # lookup asks for per_page 5 and then reads row 0 only, discarding the
        # other four without looking at them. With an EQUALS filter a second
        # row should not exist, so today the extra four are merely fetched and
        # thrown away — but if the server ever returns more than one row for an
        # exact name (case-differing duplicates, an alias hit), "first row
        # wins" silently picks one of them by search rank and the whole cohort
        # scan runs against whichever that was, with nothing logged. Flagged,
        # not fixed: the fixture below deliberately supplies a second row, and
        # the test pins that the FIRST is taken.
        t = _ReadTransport(tag_pages=[[
            {"id": COHORT_TAG_ID, "name": COHORT_TAG_NAME, "scene_count": 2},
            {"id": "tag-other", "name": "Lantern Drift II", "scene_count": 9}]])
        self.assertEqual(_read_stash(t).tag_id_by_name(COHORT_TAG_NAME),
                         COHORT_TAG_ID)

    def test_it_returns_none_when_the_server_has_no_such_tag(self):
        # HARM: an IndexError here would abort the run; a truthy stand-in
        # would point it at a tag that does not exist.
        t = _ReadTransport(tag_pages=[[]])
        self.assertIsNone(_read_stash(t).tag_id_by_name("No Such Tag"))


class AllTags(unittest.TestCase):
    def test_it_pages_past_the_first_page(self):
        # HARM: consolidation computes its merges from this list. Stopping at
        # the first page means it only ever sees the alphabetically-first 500
        # tags, so a duplicate whose twin sorts later looks unique — and the
        # merges it does compute are made against a partial view, then written
        # with tagsMerge, which deletes tags permanently and cannot be undone.
        pages = [_tag_rows(TAG_PAGE_SIZE, "alpha"), _tag_rows(3, "omega")]
        t = _ReadTransport(tag_pages=pages)
        got = _read_stash(t).all_tags()
        self.assertEqual(len(got), TAG_PAGE_SIZE + 3)
        self.assertEqual(got, pages[0] + pages[1])
        self.assertEqual([v["f"]["page"] for _, v in t.calls], [1, 2])

    def test_every_page_is_requested_with_the_same_size_and_ordering(self):
        # HARM: an unstable or differing sort between pages makes the server
        # return overlapping or skipped windows, so consolidation sees some
        # tags twice and others never. Asserted whole, per page.
        pages = [_tag_rows(TAG_PAGE_SIZE, "alpha"), _tag_rows(1, "omega")]
        t = _ReadTransport(tag_pages=pages)
        _read_stash(t).all_tags()
        self.assertEqual([v["f"] for _, v in t.calls],
                         [{"per_page": TAG_PAGE_SIZE, "page": 1,
                           "sort": "name", "direction": "ASC"},
                          {"per_page": TAG_PAGE_SIZE, "page": 2,
                           "sort": "name", "direction": "ASC"}])

    def test_a_short_first_page_ends_the_read(self):
        # HARM: the other half of the paging contract. Not stopping means an
        # endless walk of empty pages against a live server.
        #
        # NOTE: the stop condition has TWO arms (a short page, or the running
        # total reaching the server's `count`), and this fixture makes them
        # agree — 4 rows out of a reported 4 — so it does not distinguish
        # them: either arm alone passes this test. The two tests below drive
        # the arms apart so each is pinned on its own.
        t = _ReadTransport(tag_pages=[_tag_rows(4, "alpha")])
        got = _read_stash(t).all_tags()
        self.assertEqual(len(got), 4)
        self.assertEqual(len(t.calls), 1)

    def test_a_short_page_ends_the_read_even_when_the_count_over_reports(self):
        # HARM: pins the short-page arm ALONE, by making the count arm unable
        # to fire — the server claims far more tags than it hands back. A count
        # can over-report on a real server (rows deleted between pages, a total
        # computed before a filter), and then the short page is the only thing
        # that ends the walk: without that arm the client asks for page after
        # empty page forever, against a live server, and all_tags never
        # returns — the consolidation run hangs rather than failing.
        #
        # Driven at a plainly-short page AND at the boundary one (exactly one
        # row less than the page size), because "short" means short OF THE
        # REQUESTED PAGE SIZE: an arm testing some other threshold would still
        # end a 4-row read, and only the boundary case notices.
        for short in (4, TAG_PAGE_SIZE - 1):
            with self.subTest(rows=short):
                t = _ReadTransport(tag_pages=[_tag_rows(short, "alpha")],
                                   tag_count=TAG_PAGE_SIZE * 3)
                got = _read_stash(t).all_tags()
                self.assertEqual(len(got), short)
                self.assertEqual(len(t.calls), 1)

    def test_a_full_page_that_completes_the_count_ends_the_read(self):
        # HARM: pins the count arm ALONE, by making the short-page arm unable
        # to fire — exactly 500 rows, which is indistinguishable from "there
        # is more" by length. `count` is what stops it, and without that arm
        # every read spends an extra round trip past the end of the tag list;
        # against a server that answers an over-run page by repeating the last
        # one (rather than returning nothing), the walk never terminates and
        # the returned list grows duplicates of tags the merge planner would
        # then plan merges between.
        t = _ReadTransport(tag_pages=[_tag_rows(TAG_PAGE_SIZE, "alpha")])
        got = _read_stash(t).all_tags()
        self.assertEqual(len(got), TAG_PAGE_SIZE)
        self.assertEqual(len(t.calls), 1)


PERFORMER_PAGE_SIZE = 500


def _performer_rows(n, prefix, endpoint="https://box.example.test/graphql"):
    return [{"id": "%s-%d" % (prefix, i), "name": "%s %d" % (prefix, i),
             "stash_ids": [{"endpoint": endpoint, "stash_id": "%s-%d" % (prefix, i)}]}
            for i in range(n)]


class _PerformerReadTransport:
    """Fake transport for `performers_with_stash_ids`, in the style of
    `_ReadTransport`'s tag paging: `pages` is served by requested page
    number, `count` (default: the true total across `pages`) can be forced
    to disagree with what the pages actually hold, so the short-page and
    count-reached stop conditions can be driven apart the same way
    `AllTags` drives `all_tags`'s two arms apart."""

    def __init__(self, pages=None, count=None, max_pages=6):
        self.calls = []
        self.pages = [[]] if pages is None else pages
        self.count = count
        self.max_pages = max_pages
        self.requests = 0

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        if "findPerformers(" not in q:
            raise AssertionError("test transport does not recognize query: %s" % q)
        self.requests += 1
        if self.requests > self.max_pages:
            raise AssertionError(
                "the client is still asking for performer pages after %d "
                "requests — it never stopped" % self.max_pages)
        page = (variables.get("f") or {}).get("page", 1)
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        total = sum(len(p) for p in self.pages)
        return {"data": {"findPerformers": {
            "count": total if self.count is None else self.count,
            "performers": rows}}}


class PerformersWithStashIds(unittest.TestCase):
    def test_it_pages_past_the_first_page(self):
        # HARM: `cronicled.performer_ids.derive_performer_ids` builds its
        # name -> stash-box id mapping from this whole list. Stopping at the
        # first page means a performer whose name sorts past it is simply
        # never offered a stash-box id, and every file resolved to them is
        # silently skipped as though this library had no link for them at all.
        pages = [_performer_rows(PERFORMER_PAGE_SIZE, "alpha"),
                 _performer_rows(3, "omega")]
        t = _PerformerReadTransport(pages=pages)
        got = _read_stash(t).performers_with_stash_ids()
        self.assertEqual(len(got), PERFORMER_PAGE_SIZE + 3)
        self.assertEqual(got, pages[0] + pages[1])
        self.assertEqual([v["f"]["page"] for _, v in t.calls], [1, 2])

    def test_every_page_is_requested_with_the_same_size_and_ordering(self):
        pages = [_performer_rows(PERFORMER_PAGE_SIZE, "alpha"),
                 _performer_rows(1, "omega")]
        t = _PerformerReadTransport(pages=pages)
        _read_stash(t).performers_with_stash_ids()
        self.assertEqual([v["f"] for _, v in t.calls],
                         [{"per_page": PERFORMER_PAGE_SIZE, "page": 1,
                           "sort": "name", "direction": "ASC"},
                          {"per_page": PERFORMER_PAGE_SIZE, "page": 2,
                           "sort": "name", "direction": "ASC"}])

    def test_a_short_page_ends_the_read_even_when_the_count_over_reports(self):
        # HARM: pins the short-page arm alone, by making the count arm unable
        # to fire. Without it a server whose count over-reports (a performer
        # deleted between pages) is asked for page after empty page forever.
        for short in (4, PERFORMER_PAGE_SIZE - 1):
            with self.subTest(rows=short):
                t = _PerformerReadTransport(
                    pages=[_performer_rows(short, "alpha")],
                    count=PERFORMER_PAGE_SIZE * 3)
                got = _read_stash(t).performers_with_stash_ids()
                self.assertEqual(len(got), short)
                self.assertEqual(len(t.calls), 1)

    def test_a_full_page_that_completes_the_count_ends_the_read(self):
        # HARM: pins the count arm alone, by making the short-page arm unable
        # to fire — exactly PERFORMER_PAGE_SIZE rows, indistinguishable from
        # "there is more" by length alone.
        t = _PerformerReadTransport(pages=[_performer_rows(PERFORMER_PAGE_SIZE, "alpha")])
        got = _read_stash(t).performers_with_stash_ids()
        self.assertEqual(len(got), PERFORMER_PAGE_SIZE)
        self.assertEqual(len(t.calls), 1)

    def test_the_selection_set_carries_the_stash_id_object_fields(self):
        # HARM: `stash_ids` is `[StashID!]!`, an OBJECT list — written bare
        # in the query it is either rejected outright or (against a fake
        # transport that never parses the query it is handed, as every test
        # transport here does not) silently comes back without `endpoint`,
        # and `cronicled.performer_ids.derive_performer_ids` can then never
        # tell which stash-box a performer's id belongs to.
        t = _PerformerReadTransport(pages=[_performer_rows(1, "alpha")])
        _read_stash(t).performers_with_stash_ids()
        query = t.calls[0][0]
        self.assertIn("stash_ids{ endpoint stash_id }", query)
        self.assertIn("findPerformers(filter:$f)", query)


# -- the request path: what would actually go on the wire ----------------- #

# Invented server. `.invalid` is a reserved TLD that can never resolve, so if
# any test below ever escaped its substituted urlopen it would fail loudly
# rather than dial a real host. Neither the host nor the key belongs to any
# install, real or plausible.
SERVER_URL = "http://media-server.invalid:9999"
SERVER_GRAPHQL_URL = "http://media-server.invalid:9999/graphql"
API_KEY = "invented-api-key-0000"


class _CannedResponse:
    """What urlopen hands back: a context manager whose read() yields the raw
    body bytes. There is no socket behind it."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


class _UrlopenRecorder:
    """Stands in for urllib.request.urlopen. Captures the Request object and
    the timeout it was handed, and answers with a canned JSON body — so every
    assertion below is about what WOULD have gone on the wire, with nothing on
    the other end of it. Opens no socket.

    `raises` makes the call fail the way a real one does (an HTTPError for a
    non-200, a URLError for an unreachable host, a TimeoutError for a socket
    that gave up) instead of answering.
    """

    def __init__(self, payload=None, raises=None):
        self.payload = {"data": {"ok": True}} if payload is None else payload
        self.raises = raises
        self.calls = []  # (Request, timeout) per call, in order

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        if self.raises is not None:
            raise self.raises
        return _CannedResponse(json.dumps(self.payload).encode())

    def _only(self):
        """The single call made — fails if it was not exactly one, so a test
        cannot pass by inspecting the wrong request."""
        if len(self.calls) != 1:
            raise AssertionError("expected exactly 1 request, got %d"
                                 % len(self.calls))
        return self.calls[0]

    @property
    def request(self):
        return self._only()[0]

    @property
    def timeout(self):
        return self._only()[1]


def _http_error(code, detail=b"server said no"):
    """The exception a real urlopen raises for a non-200."""
    return urllib.error.HTTPError(SERVER_GRAPHQL_URL, code, "nope", {},
                                  io.BytesIO(detail))


class PerformRequest(unittest.TestCase):
    """`_perform` is the only code in this client that ever touches a real
    server, and the injected-transport seam that makes everything above it
    testable is exactly why nothing exercised it. These tests substitute
    urlopen itself, so the request is built for real and simply never sent."""

    BODY = {"query": "query{version{version}}", "variables": {}}

    def setUp(self):
        # Belt and braces for the whole class: if the request path ever stopped
        # going through the substituted urlopen, these tests must fail rather
        # than quietly dial a host from a unit test run.
        patcher = mock.patch(
            "socket.socket",
            side_effect=AssertionError("no test may open a socket"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, recorder, url=SERVER_URL, api_key=API_KEY, timeout=11,
              body=None):
        with mock.patch("urllib.request.urlopen", recorder):
            return Stash(url, api_key)._perform(
                self.BODY if body is None else body, timeout)

    def test_it_sends_a_POST(self):
        # HARM: GraphQL is POST-only here. A GET (urllib's default when no data
        # is set, and what a careless edit produces) is refused by the server or
        # answered from a cache, so every call fails — or worse, silently
        # returns a stale read that a write is then computed from.
        rec = _UrlopenRecorder()
        self._send(rec)
        self.assertEqual(rec.request.get_method(), "POST")

    def test_it_posts_to_the_graphql_endpoint_of_the_configured_base(self):
        # HARM: the base URL a user configures is the server's root, not its
        # API. Posting to the root (or to any other suffix) 404s every call on
        # every install — the whole tool stops working and no test notices.
        rec = _UrlopenRecorder()
        self._send(rec)
        self.assertEqual(rec.request.full_url, SERVER_GRAPHQL_URL)

    def test_a_trailing_slash_on_the_configured_base_is_not_doubled(self):
        # HARM: users paste the URL out of a browser bar, where it carries a
        # trailing slash. Appending blindly gives "//graphql", which some
        # servers 404 — a config that looks identical to a working one fails.
        rec = _UrlopenRecorder()
        self._send(rec, url=SERVER_URL + "/")
        self.assertEqual(rec.request.full_url, SERVER_GRAPHQL_URL)

    def test_the_api_key_travels_in_the_ApiKey_header(self):
        # HARM: this header IS the authentication. Rename it, misspell it, send
        # something other than the configured key, or drop it altogether, and
        # every call against a secured install comes back unauthorized — a
        # total outage that the injected-transport tests above cannot see.
        rec = _UrlopenRecorder()
        self._send(rec)
        # urllib title-cases stored header names; the wire name is
        # case-insensitive, so "Apikey" here is the "ApiKey" the code sets.
        self.assertEqual(rec.request.get_header("Apikey"), API_KEY)

    def test_the_body_is_declared_as_json(self):
        # HARM: the body is JSON. Without this content type the server parses it
        # as form data and rejects the query, so every call fails.
        rec = _UrlopenRecorder()
        self._send(rec)
        self.assertEqual(rec.request.get_header("Content-type"),
                         "application/json")

    def test_those_are_the_only_two_headers_set(self):
        # HARM: asserted whole so an ADDED header is caught too — a second
        # header carrying the key copies the credential somewhere it was never
        # meant to go, and a duplicate content type can override the real one.
        rec = _UrlopenRecorder()
        self._send(rec)
        self.assertEqual(dict(rec.request.header_items()),
                         {"Content-type": "application/json", "Apikey": API_KEY})

    def test_no_configured_key_means_no_auth_header_at_all(self):
        # HARM: an install with authentication switched off configures no key.
        # Sending the header anyway with an empty or literal-None value is a
        # credential the server may reject outright, locking out precisely the
        # setup that needs no credential.
        rec = _UrlopenRecorder()
        self._send(rec, api_key="")
        self.assertFalse(rec.request.has_header("Apikey"))
        self.assertEqual(dict(rec.request.header_items()),
                         {"Content-type": "application/json"})

    def test_the_body_is_the_json_encoded_query_and_variables(self):
        # HARM: the body is the request. Sent as text rather than bytes urlopen
        # refuses it; re-shaped or with either key missing, the server rejects
        # every query. Read back through json.loads because that is what the
        # server does with it.
        rec = _UrlopenRecorder()
        body = {"query": "query($a:ID){x}", "variables": {"a": "sc-1"}}
        self._send(rec, body=body)
        self.assertIsInstance(rec.request.data, bytes)
        self.assertEqual(json.loads(rec.request.data.decode()), body)

    def test_a_query_sent_through_gql_carries_both_keys(self):
        # HARM: the pair above is assembled by gql, and `variables` must be
        # present even when there are none — a body missing the key is a
        # protocol error on servers that require it. This is the one test that
        # runs the whole path, gql through to the built request.
        rec = _UrlopenRecorder(payload={"data": {"ok": True}})
        with mock.patch("urllib.request.urlopen", rec):
            Stash(SERVER_URL, API_KEY).gql("query{version{version}}")
        self.assertEqual(json.loads(rec.request.data.decode()),
                         {"query": "query{version{version}}", "variables": {}})

    def test_it_returns_the_parsed_payload_untouched(self):
        # HARM: interpreting the payload is gql's job, not the transport's — an
        # injected transport must be able to hand back exactly what this one
        # does. A transport that unwrapped "data" itself, or swallowed
        # "errors", would make every server rejection look like a success.
        payload = {"data": {"findScene": {"id": "sc-1"}},
                   "errors": [{"message": "partial"}]}
        rec = _UrlopenRecorder(payload=payload)
        self.assertEqual(self._send(rec), payload)

    def test_a_5xx_becomes_a_transient_StashError(self):
        # HARM: a server having a bad minute is worth retrying. Escaping as a
        # bare HTTPError skips every caller's error handling; marked permanent,
        # a whole run is condemned over a blip.
        rec = _UrlopenRecorder(raises=_http_error(503, b"overloaded"))
        with self.assertRaises(StashError) as ctx:
            self._send(rec)
        self.assertTrue(ctx.exception.transient)
        self.assertIn("503", str(ctx.exception))
        self.assertIn("overloaded", str(ctx.exception))

    def test_a_4xx_becomes_a_permanent_StashError(self):
        # HARM: a rejected request will be rejected again. Marked transient, the
        # caller retries a bad key or a bad query forever.
        rec = _UrlopenRecorder(raises=_http_error(401, b"bad api key"))
        with self.assertRaises(StashError) as ctx:
            self._send(rec)
        self.assertFalse(ctx.exception.transient)
        self.assertIn("401", str(ctx.exception))

    def test_an_unreachable_host_becomes_a_transient_StashError(self):
        # HARM: a server that is down or a name that will not resolve is the
        # single most common real failure. A bare URLError out of here is not
        # the StashError callers catch, so it aborts the run instead of failing
        # one retryable row.
        rec = _UrlopenRecorder(raises=urllib.error.URLError("no route to host"))
        with self.assertRaises(StashError) as ctx:
            self._send(rec)
        self.assertTrue(ctx.exception.transient)
        self.assertIn(SERVER_GRAPHQL_URL, str(ctx.exception))

    def test_a_socket_timeout_becomes_a_transient_StashError(self):
        # HARM: a socket timeout is a TimeoutError — an OSError, but NOT a
        # URLError. Without the OSError arm it escapes uncaught, so the very
        # failure the timeout exists to produce is the one callers cannot
        # handle.
        rec = _UrlopenRecorder(raises=TimeoutError("timed out"))
        with self.assertRaises(StashError) as ctx:
            self._send(rec)
        self.assertTrue(ctx.exception.transient)

    def test_the_per_call_timeout_reaches_urlopen(self):
        # HARM: a timeout that is computed and then not passed on leaves urlopen
        # on its default of "no timeout", so a half-open connection to a
        # rebooted server holds the caller forever. Nothing above this line can
        # observe it — the argument only exists at this boundary.
        rec = _UrlopenRecorder()
        self._send(rec, timeout=7)
        self.assertEqual(rec.timeout, 7)


# How long the wedged-call test waits for gql to come back before calling it a
# hang. Hundreds of times the 10ms deadline it is watching, so a healthy run
# never approaches it — it exists only so a regression that removes the
# deadline FAILS in a few seconds instead of hanging the suite forever.
WEDGED_CALL_WATCHDOG = 5


class _FakeThreading:
    """Stands in for the `threading` module as gql sees it, and only for what
    gql uses: the REAL Thread (so the worker still runs), plus an Event that
    records the deadline it was asked to wait for and always reports "not
    finished". That makes the deadline arithmetic readable directly, without
    any test having to wait out a real deadline. Thread's own internal Event
    use is untouched — it resolves inside the genuine threading module."""

    Thread = threading.Thread

    def __init__(self):
        self.waits = []
        recorder = self

        class _Event:
            def set(self):
                pass

            def wait(self, timeout=None):
                recorder.waits.append(timeout)
                return False

        self.Event = _Event


class Deadlines(unittest.TestCase):
    """How long a call is given, and what happens when it never comes back."""

    def test_the_default_timeout_is_generous_on_purpose(self):
        # HARM: this is not a latency budget, it is the point at which a read is
        # abandoned. Some calls here page through everything the server holds
        # (unorganized_scenes(limit=None), all_tags()) and a real library can
        # hold thousands of scenes under one tag. A short timeout abandons a
        # slow but perfectly healthy read of a large library and reports it as a
        # server fault, which it is not — and on a big install it does so every
        # single run. Pinned to the documented value so shrinking it is a
        # deliberate act with a test to change, not a tidy-up.
        self.assertEqual(DEFAULT_TIMEOUT, 180)

    def test_gql_hands_the_default_timeout_to_the_transport(self):
        # HARM: the transport cannot bound anything it is not told about. A
        # default that stops here means every call runs unbounded.
        t = _transport([{"data": {}}])
        Stash(SERVER_URL, API_KEY, transport=t).gql("query{x}")
        self.assertEqual(t.calls[0][1], DEFAULT_TIMEOUT)

    def test_gql_hands_an_explicit_per_call_timeout_to_the_transport(self):
        # HARM: callers that know a call is cheap (or unusually expensive) pass
        # their own bound. Ignoring it silently substitutes the default.
        t = _transport([{"data": {}}])
        Stash(SERVER_URL, API_KEY, transport=t).gql("query{x}", timeout=42)
        self.assertEqual(t.calls[0][1], 42)

    def test_the_slack_above_the_socket_timeout_is_the_documented_value(self):
        # HARM: the hard deadline must sit ABOVE the socket timeout so a normal
        # slow call ends via the clean urlopen timeout rather than an abandoned
        # thread. Zero or negative slack makes the deadline fire first on every
        # slow-but-healthy call, permanently leaking a wedged-looking worker
        # thread each time.
        self.assertEqual(HARD_DEADLINE_SLACK, 30)
        self.assertGreater(HARD_DEADLINE_SLACK, 0)

    def test_the_hard_deadline_is_the_call_timeout_plus_the_slack(self):
        # HARM: the arithmetic itself. Waiting only the slack abandons every
        # healthy call after 30s; waiting only the timeout races the socket
        # timeout it is supposed to sit above. Read off the wait() argument, so
        # no test has to wait for a deadline to observe its size.
        for asked, expected in ((None, DEFAULT_TIMEOUT + HARD_DEADLINE_SLACK),
                                (5, 5 + HARD_DEADLINE_SLACK)):
            fake = _FakeThreading()
            kwargs = {} if asked is None else {"timeout": asked}
            with mock.patch("cronicled.stash.threading", fake):
                with self.assertRaises(StashError) as ctx:
                    Stash(SERVER_URL, API_KEY,
                          transport=_transport([{"data": {}}])).gql(
                              "query{x}", **kwargs)
            self.assertEqual(fake.waits, [expected])
            self.assertTrue(ctx.exception.transient)
            self.assertIn("hard deadline", str(ctx.exception))

    def test_a_call_that_never_returns_is_abandoned_rather_than_hanging(self):
        # HARM: urlopen's timeout bounds socket operations but NOT name
        # resolution, so a wedged host can hold a request open forever. Without
        # this deadline the caller never comes back — and under any concurrency
        # a pool joining that stuck worker on shutdown hangs the whole process,
        # with no error, no output and nothing to retry.
        #
        # The transport blocks on an event this test never sets: no sleeping,
        # and the wait ends only because the deadline fires. The slack is
        # patched to 0 (its size is pinned above) so the deadline under test is
        # 10ms rather than 210s.
        #
        # The call itself is made on a watched thread, because the failure this
        # guards against is a hang: assert-and-wait would hang the suite instead
        # of reporting it, which is the same silence in a different place.
        never = threading.Event()
        self.addCleanup(never.set)  # release the abandoned worker on the way out

        def wedged(body, timeout):
            never.wait()  # never set while this test runs
            return {"data": {}}

        box = {}

        def call():
            try:
                with mock.patch("cronicled.stash.HARD_DEADLINE_SLACK", 0):
                    Stash(SERVER_URL, API_KEY, transport=wedged).gql(
                        "query{x}", timeout=0.01)
            except BaseException as e:  # noqa: BLE001 — relayed to the assertions
                box["exc"] = e

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        caller.join(WEDGED_CALL_WATCHDOG)
        self.assertFalse(caller.is_alive(),
                         "gql never came back from a wedged transport: the hard "
                         "deadline did not fire")
        self.assertIsInstance(box.get("exc"), StashError)
        self.assertTrue(box["exc"].transient)
        self.assertIn("hard deadline", str(box["exc"]))
        self.assertIn(SERVER_GRAPHQL_URL, str(box["exc"]))


# -- which entity a name resolves to --------------------------------------- #

class _ResolveTransport:
    """Fake transport for the entity-resolution tests, in the style of
    _ReadTransport: it recognizes one kind's find query and its create
    mutation, records every request, and answers with a minimal payload
    shaped like the real server's. Opens no socket.

    `results` maps a SEARCHED name to the list of result PAGES the server
    answers with (each page a list of `{"id", "name", "aliases"}` rows,
    `aliases` optional and renamed to whichever field this kind uses), so
    paging and result ordering can be observed rather than assumed. A name
    absent from `results` comes back with no rows at all.

    `create` is what the create mutation does: an id it returns, or an
    exception instance it raises (the server refusing the name). When
    `results_after_create` is given it REPLACES `results` the moment a create
    raises — that is how the server looks to the recovery search that runs
    after a clash, which is a different moment in time from the search before
    it.
    """

    _FIND = SERVER_FIND
    _CREATE_MUTATION = SERVER_CREATE_MUTATION

    def __init__(self, kind, results=None, create=None,
                 results_after_create=None):
        self.kind = kind
        self.results = results or {}
        self.create = create
        self.results_after_create = results_after_create
        self.calls = []      # (query, variables) for every request, in order
        self.searches = []   # the `q` of every find, in order
        self.creates = []    # the input of every create mutation, in order

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        fn, field, alias_field = self._FIND[self.kind]
        if fn + "(" in q:
            return self._find(fn, field, alias_field, variables)
        if self._CREATE_MUTATION[self.kind] in q:
            return self._create(variables)
        raise AssertionError("test transport does not recognize query: %s" % q)

    def _find(self, fn, field, alias_field, variables):
        f = variables["f"]
        name, page = f["q"], f["page"]
        self.searches.append(name)
        pages = self.results.get(name, [])
        rows = pages[page - 1] if page <= len(pages) else []
        count = sum(len(p) for p in pages)
        shaped = [{"id": r["id"], "name": r["name"],
                   alias_field: list(r.get("aliases") or [])} for r in rows]
        return {"data": {fn: {"count": count, field: shaped}}}

    def _create(self, variables):
        self.creates.append(variables["in"])
        if isinstance(self.create, Exception):
            if self.results_after_create is not None:
                self.results = self.results_after_create
            raise self.create
        mut = self._CREATE_MUTATION[self.kind]
        return {"data": {mut: {"id": self.create}}}


# Mirrors the page size _find_first requests; a page shorter than this is what
# tells it to stop, so a full page is what makes it ask for another.
RESOLVE_PAGE_SIZE = 100

# Invented entities. A studio whose name a scrape spells three ways, and a tag
# the server holds under one spelling with the other as its alias.
STUDIO_NAME = "Harbour Light Pictures"
STUDIO_ID = "studio-harbour-light"
CANONICAL_TAG_NAME = "Lantern Drift"
TYPO_TAG_NAME = "Lanterndrift"


def _rows(n, prefix):
    """Filler rows: the near-misses a server's fuzzy `q` search returns
    alongside (or instead of) the row actually wanted."""
    return [{"id": "%s-%d" % (prefix, i), "name": "%s %d" % (prefix, i)}
            for i in range(n)]


def _resolve_stash(transport):
    return Stash("http://example.test", "k", transport=transport)


class FindOrCreate(unittest.TestCase):
    def test_a_stored_id_is_returned_without_searching_at_all(self):
        # PINS CURRENT BEHAVIOUR — and the behaviour is a FLAGGED RISK, not a
        # rule this test endorses. A `stored_id` is returned as-is: no
        # existence check, no name cross-check, no fallback to a search if it
        # is stale or was never an id on this server. Entity ids here are
        # explicitly installation-specific (see tag_id_by_name's docstring), so
        # the moment an adapter populates stored_id from anything remote — a
        # StashBox id, a cached id from another install, a stale export — every
        # scene that match touches gets a WRONG performer/studio/tag attached,
        # silently, with no error and no search that could have caught it. The
        # write is a union, so the wrong entity is added rather than replacing
        # anything, and only the undo snapshot's performer_ids/tag_ids records
        # that it happened.
        #
        # Nothing in this repo sets stored_id today (it appears only inside
        # stash.py), so the hazard is latent, not live — which is why it is
        # pinned rather than changed by this tests-only ticket. If a caller
        # ever starts supplying it, this test is the one to revisit FIRST:
        # verifying the id would break it, and that break is the intended
        # signal, not a regression.
        #
        # What the current shortcut buys, and why it is not simply wrong: the
        # stored id names the entity the scrape actually identified, so
        # searching by name instead would let a fuzzy match override it.
        # Asserted as "no request was sent", because a search that happens is a
        # search whose result can win.
        t = _ResolveTransport("performer", results={
            "Velvet Crane": [[{"id": "performer-other", "name": "Velvet Crane"}]]})
        got = _resolve_stash(t).find_or_create("performer", "Velvet Crane",
                                               stored_id="performer-stored")
        self.assertEqual(got, "performer-stored")
        self.assertEqual(t.calls, [])

    def test_a_blank_name_resolves_to_nothing_and_creates_nothing(self):
        # HARM: a scrape with a missing field hands this an empty (or
        # whitespace-only) name. Falling through to create makes a blank-named
        # studio/performer/tag on the server — permanent library clutter that
        # then gets attached to scenes, and every later blank resolves to it.
        for name in (None, "", "   ", "\t\n"):
            with self.subTest(name=repr(name)):
                t = _ResolveTransport("tag", create="tag-blank")
                self.assertIsNone(_resolve_stash(t).find_or_create("tag", name))
                self.assertEqual(t.calls, [])

    def test_an_existing_name_is_reused_rather_than_created(self):
        # HARM: creating before searching duplicates an entity the library
        # already has, splitting one studio's scenes across two studio records.
        t = _ResolveTransport("studio",
                              results={STUDIO_NAME: [[{"id": STUDIO_ID,
                                                       "name": STUDIO_NAME}]]},
                              create="studio-duplicate")
        got = _resolve_stash(t).find_or_create("studio", STUDIO_NAME)
        self.assertEqual(got, STUDIO_ID)
        self.assertEqual(t.creates, [])

    def test_a_name_the_server_does_not_have_is_created_once_trimmed(self):
        # HARM: two harms in one. Not creating at all silently drops the
        # entity from the apply; creating with the untrimmed name puts a
        # padded duplicate (" Harbour Light Pictures ") in the library that no
        # later exact lookup ever matches, so every run creates another.
        t = _ResolveTransport("studio", create=STUDIO_ID)
        got = _resolve_stash(t).find_or_create("studio", "  %s  " % STUDIO_NAME)
        self.assertEqual(got, STUDIO_ID)
        self.assertEqual(t.creates, [{"name": STUDIO_NAME}])
        self.assertEqual(t.searches, [STUDIO_NAME])


class FindFirst(unittest.TestCase):
    def test_an_exact_name_match_beats_a_fuzzy_row_ranked_above_it(self):
        # HARM: the server's `q` search is fuzzy and ranks the rows itself, so
        # the first row is routinely NOT the name asked for. Taking it attaches
        # a neighbouring studio ("Harbour Light Pictures International") to
        # every scene of the one actually matched — and apply's undo is
        # per-scene.
        t = _ResolveTransport("studio", results={STUDIO_NAME: [[
            {"id": "studio-neighbour", "name": STUDIO_NAME + " International"},
            {"id": STUDIO_ID, "name": STUDIO_NAME}]]})
        self.assertEqual(_resolve_stash(t)._find_first("studio", STUDIO_NAME),
                         STUDIO_ID)

    def test_an_exact_name_match_beats_an_alias_hit_found_first(self):
        # HARM: an alias is a weaker claim on a name than the name itself. If
        # an alias hit short-circuits the walk, a tag that genuinely exists
        # under the searched spelling is passed over in favour of whichever
        # other tag merely lists it as an alias, and scenes are tagged with the
        # wrong one.
        t = _ResolveTransport("tag", results={TYPO_TAG_NAME: [[
            {"id": "tag-alias-owner", "name": CANONICAL_TAG_NAME,
             "aliases": [TYPO_TAG_NAME]},
            {"id": "tag-exact", "name": TYPO_TAG_NAME}]]})
        self.assertEqual(_resolve_stash(t)._find_first("tag", TYPO_TAG_NAME),
                         "tag-exact")

    def test_an_exact_match_on_a_later_page_beats_an_alias_hit_on_the_first(self):
        # HARM: the same precedence, across the page boundary — the case a
        # "return as soon as we have something" shortcut gets wrong. Returning
        # the page-1 alias owner means the exact tag on page 2 is never seen.
        page1 = _rows(RESOLVE_PAGE_SIZE - 1, "tag-near") + [
            {"id": "tag-alias-owner", "name": CANONICAL_TAG_NAME,
             "aliases": [TYPO_TAG_NAME]}]
        t = _ResolveTransport("tag", results={TYPO_TAG_NAME: [
            page1, [{"id": "tag-exact", "name": TYPO_TAG_NAME}]]})
        self.assertEqual(_resolve_stash(t)._find_first("tag", TYPO_TAG_NAME),
                         "tag-exact")

    def test_matching_ignores_case_and_surrounding_whitespace(self):
        # HARM: this is what stops one studio becoming several. Scrapes spell
        # the same name with different capitalisation and stray padding; an
        # exact-string comparison misses the existing record every time and
        # creates another, so the library ends up with "harbour light
        # pictures", "Harbour Light Pictures" and " Harbour Light Pictures"
        # as three separate studios, each holding a slice of the scenes.
        for spelling in (STUDIO_NAME.lower(), STUDIO_NAME.upper(),
                         "  %s  " % STUDIO_NAME):
            with self.subTest(spelling=spelling):
                t = _ResolveTransport("studio", results={spelling: [[
                    {"id": STUDIO_ID, "name": "  %s " % STUDIO_NAME}]]})
                self.assertEqual(
                    _resolve_stash(t)._find_first("studio", spelling), STUDIO_ID)

    def test_an_alias_match_ignores_case_and_whitespace_too(self):
        # HARM: same harm on the alias side — the alias list is exactly where
        # hand-entered spellings with odd casing and padding accumulate.
        t = _ResolveTransport("tag", results={TYPO_TAG_NAME: [[
            {"id": "tag-alias-owner", "name": CANONICAL_TAG_NAME,
             "aliases": ["  %s  " % TYPO_TAG_NAME.upper()]}]]})
        self.assertEqual(_resolve_stash(t)._find_first("tag", TYPO_TAG_NAME),
                         "tag-alias-owner")

    def test_a_name_held_only_as_an_alias_resolves_to_its_owner(self):
        # HARM: the server refuses to create a name that is already someone's
        # alias, so not resolving it here turns one alias into a hard failure
        # of the whole scene apply. Resolving it to the owner is also the point
        # of aliases: it is the server's own "same thing, different spelling".
        t = _ResolveTransport("tag", results={TYPO_TAG_NAME: [[
            {"id": "tag-alias-owner", "name": CANONICAL_TAG_NAME,
             "aliases": [TYPO_TAG_NAME]}]]})
        self.assertEqual(_resolve_stash(t)._find_first("tag", TYPO_TAG_NAME),
                         "tag-alias-owner")

    def test_the_search_continues_past_the_first_page(self):
        # HARM: a fuzzy search for a common word fills its first page with
        # near-misses. Stopping there reports "not found" for an entity the
        # library already has, and the caller then CREATES it — a duplicate
        # studio/performer/tag per run, each holding some of the scenes.
        t = _ResolveTransport("studio", results={STUDIO_NAME: [
            _rows(RESOLVE_PAGE_SIZE, "studio-near"),
            [{"id": STUDIO_ID, "name": STUDIO_NAME}]]})
        self.assertEqual(_resolve_stash(t)._find_first("studio", STUDIO_NAME),
                         STUDIO_ID)
        self.assertEqual([v["f"]["page"] for _, v in t.calls], [1, 2])

    def test_a_genuine_miss_still_resolves_to_nothing(self):
        # HARM: the other half of the walk. Returning some near-miss row when
        # nothing matched attaches an unrelated entity; never terminating walks
        # empty pages against a live server forever.
        t = _ResolveTransport("studio", results={
            STUDIO_NAME: [_rows(3, "studio-near")]})
        self.assertIsNone(_resolve_stash(t)._find_first("studio", STUDIO_NAME))


class CreateRecovery(unittest.TestCase):
    def test_a_refused_create_falls_back_to_the_entity_that_now_exists(self):
        # HARM: the server refuses a create whose name is already taken — a
        # concurrent run, or a row the earlier search did not rank highly
        # enough to reach. Letting that refusal escape fails the WHOLE scene
        # apply over one tag, costing the scene its title, date, cover and
        # every other performer, for a name that does exist.
        t = _ResolveTransport(
            "tag",
            results={},  # the search before the create finds nothing
            create=StashError("tag with name '%s' already exists" % CANONICAL_TAG_NAME),
            results_after_create={CANONICAL_TAG_NAME: [[
                {"id": "tag-canonical", "name": CANONICAL_TAG_NAME}]]})
        self.assertEqual(_resolve_stash(t)._create("tag", CANONICAL_TAG_NAME),
                         "tag-canonical")

    def test_a_name_refused_as_someone_elses_alias_resolves_to_that_owner(self):
        # HARM: the alias clash the module's own docstring quotes. The refusal
        # message is the only thing that names the owner, and the re-search for
        # the refused spelling does not find it (that is why the create was
        # attempted at all), so without reading the owner out of the message
        # this one tag fails the entire scene apply.
        owner_msg = ("name '%s' is used as alias for '%s'"
                     % (TYPO_TAG_NAME, CANONICAL_TAG_NAME))
        t = _ResolveTransport(
            "tag",
            results={CANONICAL_TAG_NAME: [[{"id": "tag-canonical",
                                            "name": CANONICAL_TAG_NAME}]]},
            create=StashError(owner_msg))
        self.assertEqual(_resolve_stash(t)._create("tag", TYPO_TAG_NAME),
                         "tag-canonical")
        # and it got there by looking up the owner the server named
        self.assertEqual(t.searches, [TYPO_TAG_NAME, CANONICAL_TAG_NAME])

    def test_a_refusal_that_resolves_to_nothing_is_re_raised(self):
        # HARM: a name the server permanently refuses for its own reasons must
        # reach the caller, which records it as skipped. Swallowing it and
        # returning None makes the entity vanish from the write with no report
        # — the scene looks applied and is quietly missing a performer.
        t = _ResolveTransport("tag", results={},
                              create=StashError("name is not allowed"))
        with self.assertRaises(StashError) as ctx:
            _resolve_stash(t)._create("tag", "Some Tag")
        self.assertIn("not allowed", str(ctx.exception))


# -- the exact shape of an apply's write ----------------------------------- #

# A scene the library already holds metadata for, and a match that supplies
# every field apply_scene can write. Invented throughout.
SHAPE_EXISTING = {"id": "sc-9", "studio": None,
                  "performers": [{"id": "performer-existing"}],
                  "tags": [{"id": "tag-existing"}]}
SHAPE_MATCH = {
    "title": "Harbour Fog",
    "details": "Filmed on the quay at dawn.",
    "date": "2021-03-04",
    "urls": ["http://scene-source.invalid/harbour-fog"],
    "stash_ids": [{"endpoint": "http://scene-source.invalid",
                   "stash_id": "invented-scene-id"}],
    "image": "data:image/jpeg;base64,aW52ZW50ZWQtY292ZXI=",
    "studio": {"name": STUDIO_NAME},
    "performers": [{"name": "Velvet Crane"}],
    "tags": [{"name": CANONICAL_TAG_NAME}],
}

# Exactly what a sceneUpdate for SHAPE_MATCH is allowed to contain. Every key
# here is one this module was written to send; anything else in the input is a
# field being written that nothing asked for.
SHAPE_KEYS = {"id", "organized", "title", "details", "date", "urls",
              "stash_ids", "studio_id", "performer_ids", "tag_ids",
              "cover_image"}


def _shape_transport(existing=None, **kwargs):
    found = {"studio": {STUDIO_NAME: STUDIO_ID},
             "performer": {"Velvet Crane": "performer-velvet"},
             "tag": {CANONICAL_TAG_NAME: "tag-canonical"}}
    return _SceneTransport(dict(SHAPE_EXISTING if existing is None else existing),
                           found=kwargs.pop("found", found), **kwargs)


def _shape_stash(transport):
    return Stash("http://example.test", "k", transport=transport)


class ApplyWriteShape(unittest.TestCase):
    def test_the_update_input_holds_exactly_these_keys_and_no_others(self):
        # HARM: this is the assertion that catches a field nobody listed. The
        # server's SceneUpdateInput accepts far more than this module writes,
        # and every extra key is a field REPLACED on a scene the user may have
        # curated by hand — a stray `rating100` (or `code`, or `director`)
        # blanks that field on every scene a bulk run touches, and it is not in
        # the undo snapshot's writable set either. Asserting individual fields
        # cannot see an added one; asserting the whole key set can.
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", SHAPE_MATCH)
        self.assertEqual(set(t.scene_update_input), SHAPE_KEYS)

    def test_an_empty_match_writes_only_the_id_and_the_organized_flag(self):
        # HARM: the other half of the same guard. A field written
        # unconditionally rather than only when supplied shows up here as an
        # empty/None value overwriting whatever the scene already had — a match
        # that knows nothing must not erase anything.
        t = _shape_transport(existing={"id": "sc-9", "studio": None,
                                       "performers": [], "tags": []})
        _shape_stash(t).apply_scene("sc-9", {})
        self.assertEqual(set(t.scene_update_input), {"id", "organized"})

    def test_it_writes_the_scalars_it_was_given(self):
        # HARM: these five are the payload of the whole apply. Today they are
        # killed only incidentally, by a snapshot-completeness test over in the
        # revert suite — refactor that and all five are unguarded. Dropping any
        # one makes the apply silently not apply it; the run reports success
        # and the field stays as it was, on every scene.
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", SHAPE_MATCH)
        inp = t.scene_update_input
        self.assertEqual(inp["id"], "sc-9")
        self.assertEqual(inp["title"], SHAPE_MATCH["title"])
        self.assertEqual(inp["details"], SHAPE_MATCH["details"])
        self.assertEqual(inp["date"], SHAPE_MATCH["date"])
        self.assertEqual(inp["urls"], SHAPE_MATCH["urls"])
        self.assertEqual(inp["stash_ids"], SHAPE_MATCH["stash_ids"])

    def test_it_marks_the_scene_organized(self):
        # HARM: `organized` is what takes a scene OUT of the unorganized
        # worklist. Without it every applied scene is picked up again by the
        # next run and rewritten, forever — and the flag is a GraphQL Boolean,
        # so a truthy stand-in is not the same value.
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", SHAPE_MATCH)
        self.assertIs(t.scene_update_input["organized"], True)

    def test_free_text_is_html_stripped_for_both_title_and_details(self):
        # HARM: scraped free text arrives with raw markup in it. Written
        # through, the tags land in the library and are displayed literally by
        # every client — and both fields go through the same sanitizer, so
        # sanitizing only one is the easy half-fix this catches.
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", {
            "title": "<b>Harbour Fog</b>",
            "details": "<p>Filmed on the quay.<br>At dawn.</p>"})
        self.assertEqual(t.scene_update_input["title"], "Harbour Fog")
        self.assertEqual(t.scene_update_input["details"],
                         "Filmed on the quay. At dawn.")

    def test_a_single_url_falls_back_into_urls(self):
        # HARM: adapters hand back either shape. Reading only `urls` drops the
        # source link of every match that supplies the singular `url` — the one
        # field that records WHERE this metadata came from, and so the only way
        # to check a scene's data later.
        t = _shape_transport()
        _shape_stash(t).apply_scene(
            "sc-9", {"url": "http://scene-source.invalid/harbour-fog"})
        self.assertEqual(t.scene_update_input["urls"],
                         ["http://scene-source.invalid/harbour-fog"])

    def test_the_plural_urls_wins_when_a_match_carries_both(self):
        # PINS CURRENT BEHAVIOUR: `urls` is used whole and the singular `url`
        # is ignored rather than appended. HARM of changing it silently: a
        # match carrying both would write a different set of links than the
        # adapter's list, without anything saying so.
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", {
            "urls": ["http://scene-source.invalid/a"],
            "url": "http://scene-source.invalid/b"})
        self.assertEqual(t.scene_update_input["urls"],
                         ["http://scene-source.invalid/a"])

    def test_the_cover_is_written_only_when_an_image_was_supplied(self):
        # HARM: the single irreversible field in this write. A scene's current
        # cover is exposed only as a URL, never as the base64 payload
        # `cover_image` takes, so there is nothing to snapshot it with and an
        # applied cover CANNOT be undone (apply_scene's own docstring says so).
        # Writing it unconditionally replaces the artwork of every scene a bulk
        # run touches — with None or "" when the match had no image, which is
        # the library's cover art permanently gone.
        for image in (None, "", {}):
            with self.subTest(image=repr(image)):
                t = _shape_transport()
                _shape_stash(t).apply_scene("sc-9", {"image": image})
                self.assertNotIn("cover_image", t.scene_update_input)
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", {"image": SHAPE_MATCH["image"]})
        self.assertEqual(t.scene_update_input["cover_image"],
                         SHAPE_MATCH["image"])

    def test_apply_writes_once(self):
        # HARM: mirrors test_revert_writes_once. Everything is resolved before
        # the single sceneUpdate so a mid-apply failure leaves the scene
        # untouched; splitting the write means a failure between the parts
        # leaves a scene half-applied, with a `prior` snapshot that no longer
        # describes it and an undo that restores the wrong thing.
        server = _mutable_scene(title="Old Title")
        stash = Stash("http://example.test", "k", transport=server.transport)
        stash.apply_scene("sc-9", RICH_MATCH, overwrite_studio=True)
        self.assertEqual(server.writes, 1)


# -- the write shape, across EVERY match shape ----------------------------- #

# The whole-key-set assertion above is the right assertion — it fails on an
# added key and on a removed one alike — but it only ever evaluates two match
# shapes: the full SHAPE_MATCH and {}. A rule about what an apply may write is
# a rule about ALL match shapes, and a write gated on a key neither of those
# two carries slips straight past it. This costs nothing on either shape:
#
#     if match.get("rating"):
#         inp["rating100"] = match["rating"]
#
# ...and blanks or overwrites rating100 on every scene an adapter that does
# carry `rating` touches — a field that is not in the undo snapshot's writable
# set, so the damage is not even reversible. `rating`, `code`, `director` and
# `studio_code` are all things a scraper plausibly hands back. One shape cannot
# pin a rule about all shapes, so the sweep below drives the same assertion
# over many, including a match carrying every plausible adapter key at once and
# each of those keys on its own.
#
# Every value here is invented; none of these keys is read by apply_scene
# today, which is exactly the property being pinned.
ADAPTER_NOISE = {
    "rating": 100,
    "rating100": 100,
    "code": "HRB-014",
    "director": "Wren Ashby",
    "studio_code": "HRB",
    "organized": False,
    "id": "sc-somewhere-else",
    "cover_image": "data:image/jpeg;base64,aW52ZW50ZWQtd3Jvbmc=",
    "o_counter": 3,
    "play_count": 9,
    "phash": "invented-phash-0000",
    "duration": 1234,
    "index": 2,
    "endpoint": "http://scene-source.invalid",
    "stash_id": "invented-scene-id",
    "source": "invented-adapter",
    "score": 0.97,
    "aliases": ["Harbour Fog (2021)"],
    "movies": [{"movie_id": "movie-1"}],
    "groups": [{"group_id": "group-1"}],
    "galleries": [{"id": "gallery-1"}],
    "files": [{"basename": "harbour-fog.mp4"}],
    "path": "/m/harbour-fog.mp4",
    "director_url": "http://scene-source.invalid/wren-ashby",
}

# Every sceneUpdate carries these two whatever the match holds: the scene it is
# about, and the flag that takes it out of the unorganized worklist.
BASE_WRITE_KEYS = {"id", "organized"}

# A match whose every field is present but empty. Nothing may be written from
# one: a match that knows nothing must not erase anything.
EMPTY_VALUED_MATCH = {"title": "", "details": None, "date": "", "urls": [],
                      "url": "", "stash_ids": [], "image": None, "studio": {},
                      "performers": [], "tags": []}


def _shape_sweep():
    """(label, match, the EXACT key set its sceneUpdate may hold).

    Exact rather than "a subset of SHAPE_KEYS", so each case fails on a
    dropped key as well as an added one — the same both-directions property
    the single-shape assertion has, now held across the range of shapes an
    adapter can actually produce.
    """
    m = SHAPE_MATCH
    cases = [
        ("everything", dict(m), SHAPE_KEYS),
        ("nothing", {}, BASE_WRITE_KEYS),
        ("every field present but empty", dict(EMPTY_VALUED_MATCH), BASE_WRITE_KEYS),
        ("title only", {"title": m["title"]}, BASE_WRITE_KEYS | {"title"}),
        ("details only", {"details": m["details"]}, BASE_WRITE_KEYS | {"details"}),
        ("date only", {"date": m["date"]}, BASE_WRITE_KEYS | {"date"}),
        ("urls only", {"urls": m["urls"]}, BASE_WRITE_KEYS | {"urls"}),
        ("singular url only", {"url": m["urls"][0]}, BASE_WRITE_KEYS | {"urls"}),
        ("stash_ids only", {"stash_ids": m["stash_ids"]},
         BASE_WRITE_KEYS | {"stash_ids"}),
        ("image only", {"image": m["image"]}, BASE_WRITE_KEYS | {"cover_image"}),
        ("studio only", {"studio": m["studio"]}, BASE_WRITE_KEYS | {"studio_id"}),
        ("performers only", {"performers": m["performers"]},
         BASE_WRITE_KEYS | {"performer_ids"}),
        ("tags only", {"tags": m["tags"]}, BASE_WRITE_KEYS | {"tag_ids"}),
        ("every plausible adapter key and nothing else",
         dict(ADAPTER_NOISE), BASE_WRITE_KEYS),
        ("everything plus every plausible adapter key",
         dict(ADAPTER_NOISE, **m), SHAPE_KEYS),
    ]
    # each adapter key on its own: a write gated on one key is not visible in
    # the combined shape above if some earlier key already added its own
    cases += [("only the adapter key %r" % k, {k: v}, BASE_WRITE_KEYS)
              for k, v in sorted(ADAPTER_NOISE.items())]
    return cases


class ApplyWriteShapeOverEveryMatchShape(unittest.TestCase):
    def test_no_match_shape_can_put_an_unlisted_key_in_the_update_input(self):
        # HARM: verbatim the harm this module's key-set assertion was written
        # for — a field written that nothing asked for, replacing whatever the
        # user had on every scene a bulk run touches — reached through the one
        # door a single-shape assertion leaves open: a match key no fixture
        # happens to carry.
        for label, match, expected in _shape_sweep():
            with self.subTest(match=label):
                t = _shape_transport()
                _shape_stash(t).apply_scene("sc-9", match)
                self.assertEqual(set(t.scene_update_input), expected)
                self.assertLessEqual(set(t.scene_update_input), SHAPE_KEYS)

    def test_the_sweep_reaches_every_key_this_module_is_allowed_to_write(self):
        # HARM: the sweep is only as good as its coverage. If SHAPE_KEYS ever
        # grows a key no shape above produces, that key is listed as allowed
        # but never actually exercised — the sweep would keep passing while
        # saying nothing about it. This fails when that happens.
        seen = set()
        for label, match, _ in _shape_sweep():
            t = _shape_transport()
            _shape_stash(t).apply_scene("sc-9", match)
            seen |= set(t.scene_update_input)
        self.assertEqual(seen, SHAPE_KEYS)


# -- the read that makes the merge and the undo possible ------------------- #

# Every scene fake above answers a findScene by keying on the string
# "findScene(" and handing back its canned row whole, so what the query
# actually SELECTED is invisible to them: drop `urls` from the selection set
# and the fake still returns urls. That blind spot is one layer above the write
# assertions — apply_scene's union and its entire `prior` snapshot are built
# out of this read, so a field the client stops asking for reads back as
# None/[] and the snapshot records THAT as the scene's real state. revert_scene
# then writes the empty value back, wiping the field it existed to restore.
# The helpers below read the selection set out of the query text so it can be
# asserted on directly, and answer by it so the harm is reproducible.


def _selection_block(query, field):
    """The text between `field`'s selection braces, braces balanced."""
    start = query.index("{", query.index(field))
    depth = 0
    for i in range(start, len(query)):
        if query[i] == "{":
            depth += 1
        elif query[i] == "}":
            depth -= 1
            if depth == 0:
                return query[start + 1:i]
    raise AssertionError("unbalanced selection set for %r in: %s" % (field, query))


def _selection_of(query, field):
    """What `field`'s selection set asks for: {name: None} for a scalar,
    {name: [nested names]} for a block. One level of nesting is enough — that
    is all this client's scene read uses."""
    fields, depth, last, owner = {}, 0, None, None
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[{}]",
                            _selection_block(query, field)):
        if token == "{":
            depth += 1
            if depth == 1:
                owner = last
                fields[owner] = []
        elif token == "}":
            depth -= 1
            if depth == 0:
                owner = None
        elif depth == 0:
            fields.setdefault(token, None)
            last = token
        elif depth == 1 and owner is not None:
            fields[owner].append(token)
    return fields


def _project(row, selection):
    """`row` reduced to exactly what `selection` asked for — what a real
    GraphQL server returns, and what none of the other fakes here do."""
    out = {}
    for field, nested in selection.items():
        if field not in row:
            raise AssertionError("the scene read selected %r, which this fake "
                                 "scene does not have" % field)
        value = row[field]
        if nested is None:
            out[field] = value
        elif isinstance(value, list):
            out[field] = [{k: r.get(k) for k in nested} for r in value]
        elif value is None:
            out[field] = None
        else:
            out[field] = {k: value.get(k) for k in nested}
    return out


class _SelectiveScene(_MutableScene):
    """_MutableScene, except its findScene answers with ONLY the fields the
    query actually selected. That one difference is what makes the selection
    set testable: against this server, a field the client stops asking for is
    a field it stops getting, exactly as on a real one."""

    def __init__(self, **fields):
        _MutableScene.__init__(self, **fields)
        self.scene_read_variables = []

    def _handle(self, body, timeout):
        if "findScene(" in body["query"]:
            self.scene_read_variables.append(body["variables"])
            return {"data": {"findScene": _project(
                self._read_scene(), _selection_of(body["query"], "findScene"))}}
        return _MutableScene._handle(self, body, timeout)


# Exactly what scene_existing must select. `studio`/`performers`/`tags` feed
# apply_scene's union (keep what the scene already has, add what was
# resolved); the rest are copied into the `prior` snapshot verbatim and are
# the whole of what an undo has to replay.
SCENE_READ_FIELDS = {"id", "title", "details", "date", "urls", "organized",
                     "rating100", "code", "director", "stash_ids",
                     "studio", "performers", "tags"}


def _read_one_scene(scene_id="sc-42"):
    """Run scene_existing and hand back the request it sent."""
    t = _transport([{"data": {"findScene": {"id": scene_id}}}])
    _read_stash(t).scene_existing(scene_id)
    body, _ = t.calls[0]
    return body


class SceneReadTarget(unittest.TestCase):
    def test_the_read_asks_about_the_scene_it_was_given(self):
        # HARM: reading a DIFFERENT scene than the one being written is
        # invisible to every assertion about the write — the update still goes
        # to the right scene, carrying the wrong scene's performers and tags
        # unioned in, and a `prior` snapshot describing a scene that was never
        # written. The undo then restores one scene's metadata onto another.
        # Two ids, because one fixture cannot tell "sends the id it was given"
        # apart from "always sends sc-42".
        for scene_id in ("sc-42", "sc-7"):
            with self.subTest(scene_id=scene_id):
                body = _read_one_scene(scene_id)
                self.assertEqual(body["variables"], {"id": scene_id})

    def test_the_id_travels_as_a_variable_not_baked_into_the_query(self):
        # HARM: an id interpolated into the query text instead of bound as a
        # variable is both unescaped and unpinnable — the assertion above
        # would pass while the server was sent something else entirely.
        body = _read_one_scene("sc-42")
        self.assertIn("$id", body["query"])
        self.assertNotIn("sc-42", body["query"])

    def test_the_apply_reads_the_scene_it_is_about_to_write(self):
        # HARM: the same wrong-scene harm, reached through the caller that
        # matters. The read and the write must name the same scene: this is
        # the seam where the merge's "existing" ids and the undo snapshot come
        # from, and nothing downstream can tell they came from elsewhere.
        t = _shape_transport()
        _shape_stash(t).apply_scene("sc-9", SHAPE_MATCH)
        self.assertEqual(t.scene_read_variables, [{"id": "sc-9"}])
        self.assertEqual(t.scene_update_input["id"], "sc-9")


class SceneReadSelection(unittest.TestCase):
    def test_the_selection_set_is_exactly_the_fields_the_snapshot_needs(self):
        # HARM: this selection set is the sole source of both the apply's
        # union and the undo snapshot. Dropping `urls` makes prior["urls"]
        # always [], and revert_scene writes that back — the scene's links,
        # gone. Dropping `stash_ids` loses the canonical external id the same
        # way. Asserted as the whole set, so a field going missing fails here
        # rather than several layers downstream, and every field is named once
        # in a place that says why it is needed.
        selection = _selection_of(_read_one_scene()["query"], "findScene")
        self.assertEqual(set(selection), SCENE_READ_FIELDS)

    def test_the_nested_selections_carry_the_ids_the_merge_needs(self):
        # HARM: studio/performers/tags come back as objects, and it is their
        # `id` the apply unions and the snapshot stores. A block that selects
        # only `name` reads back ids of None, so the merge appends nothing
        # sensible and the undo has nothing to restore.
        selection = _selection_of(_read_one_scene()["query"], "findScene")
        for block in ("studio", "performers", "tags"):
            with self.subTest(block=block):
                self.assertEqual(selection[block], ["id", "name"])

    def test_the_snapshot_holds_what_the_read_selected_and_nothing_more(self):
        # HARM: the same rule again, proved end to end instead of by reading
        # the query — against a server that answers ONLY what was selected,
        # every field the read stops asking for turns up in the snapshot as
        # None or [], claiming the scene had nothing there. Compared whole:
        # `prior` and this fake's state are deliberately the same shape.
        server = _SelectiveScene(**RICH_STATE)
        before = server.snapshot()
        stash = Stash("http://example.test", "k", transport=server.transport)
        prior = stash.apply_scene("s1", {})["prior"]
        self.assertEqual(prior, before)

    def test_the_round_trip_holds_only_while_the_read_selects_everything(self):
        # HARM: the full chain, against a server that honours the selection
        # set — apply, then undo, and the scene must come back exactly as it
        # was. A field missing from the read is snapshotted as empty and
        # WRITTEN BACK empty by the revert, which is how a dropped `urls`
        # turns into wiped urls on every reverted scene. This also covers
        # rating100/code/director, which the apply never writes and the other
        # round-trip test therefore cannot see (_NOT_WRITABLE_BY_APPLY_SCENE).
        server = _SelectiveScene(**RICH_STATE)
        before = server.snapshot()
        stash = Stash("http://example.test", "k", transport=server.transport)
        info = stash.apply_scene("s1", RICH_MATCH, overwrite_studio=True)
        self.assertNotEqual(server.snapshot(), before)  # the apply really changed it
        stash.revert_scene("s1", info["prior"])
        self.assertEqual(server.snapshot(), before)

    def test_the_existing_cast_survives_only_because_the_read_selects_it(self):
        # HARM: sceneUpdate REPLACES the performer and tag arrays, so the
        # union depends entirely on the read seeing what is already there.
        # Drop `performers` from the selection and a bulk run stops adding to
        # the cast and starts REPLACING it — every scene it touches loses the
        # performers a human attached, at scale, and the tag arm does the same
        # to tags.
        server = _SelectiveScene(performers=[{"id": "p1"}], tags=[{"id": "t1"}])
        stash = Stash("http://example.test", "k", transport=server.transport)
        stash.apply_scene("s1", {"performers": [{"name": "Velvet Crane"}],
                                 "tags": [{"name": CANONICAL_TAG_NAME}]})
        state = server.snapshot()
        self.assertEqual(len(state["performer_ids"]), 2)
        self.assertEqual(state["performer_ids"][0], "p1")
        self.assertEqual(len(state["tag_ids"]), 2)
        self.assertEqual(state["tag_ids"][0], "t1")


# -- the query text the variables are bound into --------------------------- #
#
# Everything above asserts the VARIABLES a request carries, thoroughly and
# verbatim. Almost none of it touches the query STRING those variables are
# bound into — and a variable is worth exactly what the query binds it to.
# Swap `findScenes(filter:$f, scene_filter:$s)` for `(filter:$s,
# scene_filter:$f)` and every assertion above still passes, while every scan
# request this client sends is malformed against a real server: a total
# outage with no test signal at all. The same holds for `findTags(tag_filter:
# $f)` becoming `(filter:$f)`, and for every `input:` on the write side.
#
# The pin is on STRUCTURE, not on wording. Comparing a query to a golden blob
# breaks on every reformat and says nothing about what broke, so the helpers
# below parse the query and the tests assert argument bindings BY NAME,
# resolved through to the value each argument actually receives. Re-indent a
# query, rename $f to $paging, reorder the arguments, add a field to a
# selection set: all still pass, because none of it changes what the server is
# sent. Swap two bindings and the failure names the argument that is wrong and
# prints the value that arrived on it.


def _argument_list(query, field):
    """The source text inside `field`'s argument parentheses, with brackets and
    braces balanced (an inline object argument contains commas of its own)."""
    opener = re.search(r"\b%s\s*\(" % re.escape(field), query)
    if opener is None:
        raise AssertionError("this query never calls %r:\n%s" % (field, query))
    depth = 0
    for i in range(opener.end() - 1, len(query)):
        if query[i] in "([{":
            depth += 1
        elif query[i] in ")]}":
            depth -= 1
            if depth == 0:
                return query[opener.end():i]
    raise AssertionError("unbalanced argument list for %r in:\n%s" % (field, query))


def _arguments_of(query, field):
    """{argument name: binding} for `field`'s call. A binding is "$name" for a
    variable, or the argument's literal source text with whitespace removed for
    an inline value. Order and formatting are discarded; names are not."""
    args, depth, current = [], 0, ""
    for ch in _argument_list(query, field):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(current)
            current = ""
        else:
            current += ch
    args.append(current)
    out = {}
    for arg in args:
        if not arg.strip():
            continue
        name, sep, binding = arg.partition(":")
        if not sep or not name.strip():
            raise AssertionError("%r is not a name:value argument of %r"
                                 % (arg, field))
        name = name.strip()
        if name in out:
            raise AssertionError("%r binds the argument %r twice" % (field, name))
        out[name] = re.sub(r"\s+", "", binding)
    return out


def _declared_variables(query):
    """{"$f": "FindFilterType"} — the operation header's variable declarations,
    so an argument can be checked against the TYPE it carries as well as
    against the value it receives."""
    return _arguments_of(
        query, "mutation" if query.lstrip().startswith("mutation") else "query")


def _bound_arguments(query, variables, field):
    """`field`'s arguments with every variable binding RESOLVED to the value
    the request actually sent for it — argument by argument, what the server
    receives.

    This is the assertion the variables-only tests above cannot make. Swapping
    a pair of bindings leaves the variables dict byte-identical and changes
    this one, which is precisely the gap: `{"f": paging, "s": cohort}` is
    correct whichever way round the query binds it, and only one of the two
    ways is a request a server will answer.
    """
    out = {}
    for name, binding in _arguments_of(query, field).items():
        if not binding.startswith("$"):
            out[name] = binding          # an inline literal, kept as source text
        elif binding[1:] in variables:
            out[name] = variables[binding[1:]]
        else:
            raise AssertionError(
                "%s's %r argument is bound to %s, which this request never sent "
                "— it sent %s" % (field, name, binding,
                                  sorted("$" + v for v in variables)))
    return out


def _declared_type_of(query, field, argument):
    """The GraphQL type declared for whatever variable `field`'s `argument` is
    bound to. Follows the binding rather than the variable's name, so renaming
    a variable does not fail this and rebinding one does."""
    binding = _arguments_of(query, field)[argument]
    if not binding.startswith("$"):
        raise AssertionError("%s's %r is an inline literal (%s), not a variable"
                             % (field, argument, binding))
    declared = _declared_variables(query)
    if binding not in declared:
        raise AssertionError(
            "%s's %r is bound to %s, which the operation never declares — it "
            "declares %s" % (field, argument, binding, sorted(declared)))
    return declared[binding]


class _QueryRecorder:
    """Records the (query, variables) of every request and answers with canned
    payloads in order, so any client method can be driven purely to see the
    request it builds. Opens no socket."""

    def __init__(self, *payloads):
        self.calls = []
        self.payloads = list(payloads) or [{"data": {}}]

    def __call__(self, body, timeout):
        self.calls.append((body["query"], body["variables"]))
        return self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]

    def only(self):
        """The single request sent — fails if it was not exactly one, so a test
        cannot pass by inspecting the wrong call."""
        if len(self.calls) != 1:
            raise AssertionError("expected exactly 1 request, got %d"
                                 % len(self.calls))
        return self.calls[0]


def _binding_stash(transport):
    return Stash(SERVER_URL, API_KEY, transport=transport)


# What _find_scenes is driven with below, and the paging block it builds for it.
BOUND_SCENE_FILTER = {"tags": {"value": [COHORT_TAG_ID], "modifier": "INCLUDES"}}
BOUND_SCENE_PAGING = {"per_page": 5, "page": 1, "sort": "id", "direction": "ASC"}


def _scene_search_request():
    t = _QueryRecorder({"data": {"findScenes": {"count": 0, "scenes": []}}})
    _binding_stash(t)._find_scenes(BOUND_SCENE_FILTER, 5)
    return t.only()


class SceneSearchBindings(unittest.TestCase):
    def test_paging_binds_to_filter_and_the_cohort_binds_to_scene_filter(self):
        # HARM: the headline case. `filter` is the server's PAGING argument and
        # `scene_filter` is its WHICH-SCENES argument; they take different input
        # types and mean opposite things. Bound the wrong way round, every scan
        # request — unorganized_scenes and tagged_scenes both come through here
        # — is rejected by the server, so the tool stops working entirely on
        # every install at once. Every existing assertion about this call is
        # about the variables, and the variables are identical either way.
        #
        # Asserted as the whole argument map, so an argument added or dropped
        # fails here too: a `filter` that quietly stopped being sent would send
        # a full-library read where a 5-scene trial run was asked for.
        query, variables = _scene_search_request()
        self.assertEqual(_bound_arguments(query, variables, "findScenes"),
                         {"filter": BOUND_SCENE_PAGING,
                          "scene_filter": BOUND_SCENE_FILTER})

    def test_each_argument_declares_the_type_the_server_expects_for_it(self):
        # HARM: the other half of the swap. The operation header declares the
        # types, and swapping THOSE — `$f: SceneFilterType, $s: FindFilterType`
        # — leaves the bindings correct and the declarations wrong, which the
        # server rejects at parse time just as flatly. Read through the
        # binding, so renaming a variable is not a failure and rebinding it is.
        query, _ = _scene_search_request()
        self.assertEqual(_declared_type_of(query, "findScenes", "filter"),
                         "FindFilterType")
        self.assertEqual(_declared_type_of(query, "findScenes", "scene_filter"),
                         "SceneFilterType")


def _tag_lookup_request():
    t = _QueryRecorder({"data": {"findTags": {"tags": []}}})
    _binding_stash(t).tag_id_by_name(COHORT_TAG_NAME)
    return t.only()


class TagLookupBindings(unittest.TestCase):
    def test_the_exact_name_binds_to_tag_filter_and_paging_stays_inline(self):
        # HARM: this lookup is how a cohort scan finds the tag it is pointed
        # at. `tag_filter` is where the EQUALS name match belongs; `filter`
        # takes the paging block, inline here because there is nothing to vary.
        # Bind the name to `filter` and the server is handed a name matcher
        # where it expects paging: every tag lookup fails, so every tag-driven
        # run reports "no such tag" and does nothing, quietly. The name match
        # itself is asserted verbatim above; what was never asserted is which
        # argument carries it.
        query, variables = _tag_lookup_request()
        self.assertEqual(
            _bound_arguments(query, variables, "findTags"),
            {"tag_filter": {"name": {"value": COHORT_TAG_NAME,
                                     "modifier": "EQUALS"}},
             "filter": "{per_page:5}"})

    def test_the_name_match_declares_the_tag_filter_type(self):
        # HARM: `TagFilterType` is what makes a `name`/`modifier` match legal
        # here. Declared as FindFilterType the server refuses the query, with
        # the same silent "no such tag" outcome.
        query, _ = _tag_lookup_request()
        self.assertEqual(_declared_type_of(query, "findTags", "tag_filter"),
                         "TagFilterType")


def _all_tags_request():
    t = _QueryRecorder({"data": {"findTags": {"count": 0, "tags": []}}})
    _binding_stash(t).all_tags()
    return t.only()


class AllTagsSelection(unittest.TestCase):
    def test_it_asks_for_every_field_a_caller_reads_off_a_tag(self):
        # Stated as a list of names rather than compared against the query
        # constant the client also sends: reading the expectation off the
        # query moves both sides at once.
        #
        # HARM differs per field and `description` is the sharp one: without
        # it, "this tag has no description" becomes unanswerable, and the
        # honest readings are both wrong -- treat every tag as described and
        # nothing is ever proposed, treat every tag as blank and a proposal
        # is offered to overwrite text somebody wrote.
        query, _ = _all_tags_request()
        for field in ("id", "name", "aliases", "description", "scene_count"):
            self.assertIn(field, query,
                          "a caller reads %s off a tag and it is not being "
                          "asked for" % (field,))


class AllTagsBindings(unittest.TestCase):
    def test_the_tag_page_walk_binds_its_paging_to_filter(self):
        # HARM: the mirror image of the lookup above — here `filter` is the
        # right argument, because this call pages and does not match a name.
        # Bound to `tag_filter` the walk fails outright, and consolidation
        # (which deletes tags permanently via tagsMerge) can never run at all.
        # Asserted whole: this call must send paging and NOTHING else, or the
        # merge planner computes its merges from a filtered subset of the tags
        # while believing it saw them all.
        query, variables = _all_tags_request()
        self.assertEqual(_bound_arguments(query, variables, "findTags"),
                         {"filter": {"per_page": 500, "page": 1,
                                     "sort": "name", "direction": "ASC"}})
        self.assertEqual(_declared_type_of(query, "findTags", "filter"),
                         "FindFilterType")


def _entity_search_request(kind, name=STUDIO_NAME):
    fn, field, _ = SERVER_FIND[kind]
    t = _QueryRecorder({"data": {fn: {"count": 0, field: []}}})
    _binding_stash(t)._find_first(kind, name)
    return t.only()


class EntitySearchBindings(unittest.TestCase):
    def test_every_kind_binds_its_search_to_filter(self):
        # HARM: this is the find half of find-or-create, for all three kinds.
        # A search the server refuses is indistinguishable here from a search
        # that found nothing — _find_first would raise, but the caller's own
        # recovery treats a refusal as "create it" — so a wrong argument name
        # turns every resolution into a CREATE: a duplicate studio, performer
        # and tag per name per run, permanently, in the user's library.
        #
        # Driven per kind because the field name is interpolated from the
        # client's own map: a kind whose search field is wrong never reaches
        # the assertion, it fails in _arguments_of naming the field the query
        # does not call.
        for kind in sorted(SERVER_FIND):
            with self.subTest(kind=kind):
                fn = SERVER_FIND[kind][0]
                query, variables = _entity_search_request(kind)
                self.assertEqual(
                    _bound_arguments(query, variables, fn),
                    {"filter": {"q": STUDIO_NAME, "per_page": 100, "page": 1}})
                self.assertEqual(_declared_type_of(query, fn, "filter"),
                                 "FindFilterType")


class SceneReadBindings(unittest.TestCase):
    def test_the_scene_id_binds_to_the_id_argument(self):
        # HARM: the read that supplies both the apply's union and its undo
        # snapshot. `$id` bound to anything but `id` is a query the server
        # refuses, so every apply fails — and the module already pins that the
        # id travels as a variable rather than baked into the text, which is
        # the assertion that looks like it covers this and does not.
        body = _read_one_scene("sc-42")
        self.assertEqual(
            _bound_arguments(body["query"], body["variables"], "findScene"),
            {"id": "sc-42"})

    def test_the_id_is_declared_as_a_required_ID(self):
        # HARM: `ID!` is what makes the server reject a null id rather than
        # answer with some arbitrary scene. Declared nullable, a caller that
        # loses the id somewhere upstream gets a query the server accepts.
        self.assertEqual(_declared_type_of(_read_one_scene()["query"],
                                           "findScene", "id"), "ID!")


def _create_request(kind, name=STUDIO_NAME):
    mut = SERVER_CREATE_MUTATION[kind]
    t = _QueryRecorder({"data": {mut: {"id": "created-entity"}}})
    _binding_stash(t)._create(kind, name)
    return t.only()


class CreateMutationBindings(unittest.TestCase):
    def test_every_kind_sends_its_new_entity_as_the_input_argument(self):
        # HARM: the create half of find-or-create. A rejected create is caught
        # by _create's own recovery, which re-searches and — finding nothing,
        # because nothing was created — re-raises. apply_scene then records the
        # name as permanently skipped and marks the scene organized, so the
        # scene looks done and is silently missing that studio/performer/tag
        # forever. A wrong argument name does that to EVERY name that is not
        # already in the library.
        for kind in sorted(SERVER_CREATE_MUTATION):
            with self.subTest(kind=kind):
                query, variables = _create_request(kind)
                self.assertEqual(
                    _bound_arguments(query, variables,
                                     SERVER_CREATE_MUTATION[kind]),
                    {"input": {"name": STUDIO_NAME}})

    def test_every_kind_declares_the_create_input_type_the_server_expects(self):
        # HARM: each kind's create takes its OWN input type, and the three are
        # not interchangeable. Declare a performer create as taking a
        # StudioCreateInput and the server refuses it at parse time — with the
        # same silent, permanent skip as above, on every performer the library
        # does not already hold.
        for kind, row in sorted(SERVER_ENTITY_API.items()):
            with self.subTest(kind=kind):
                mut, input_type = row[3], row[4]
                query, _ = _create_request(kind)
                self.assertEqual(_declared_type_of(query, mut, "input"),
                                 input_type + "!")


def _apply_update_request():
    t = _QueryRecorder({"data": {"findScene": {"id": "sc-9"}}},
                       {"data": {"sceneUpdate": {"id": "sc-9"}}})
    _binding_stash(t).apply_scene("sc-9", {"title": "Harbour Fog"})
    return t.calls[-1]


def _revert_update_request():
    t = _QueryRecorder({"data": {"sceneUpdate": {"id": "sc-9"}}})
    _binding_stash(t).revert_scene("sc-9", {"title": "Old Title"})
    return t.only()


class SceneWriteBindings(unittest.TestCase):
    """apply_scene and revert_scene each carry their OWN copy of the
    sceneUpdate mutation text, so each needs its own pin — fixing one would
    otherwise leave the other malformed."""

    def test_the_apply_sends_its_update_as_the_input_argument(self):
        # HARM: the write the whole module exists to make. A wrong argument
        # name means every apply is refused; the failure is classified
        # permanent (a GraphQL error, not a blip), so the run condemns every
        # scene it touches rather than retrying, and a full pass over a library
        # writes nothing while reporting each row as a permanent failure.
        query, variables = _apply_update_request()
        self.assertEqual(_bound_arguments(query, variables, "sceneUpdate"),
                         {"input": {"id": "sc-9", "organized": True,
                                    "title": "Harbour Fog"}})
        self.assertEqual(_declared_type_of(query, "sceneUpdate", "input"),
                         "SceneUpdateInput!")

    def test_the_revert_sends_its_restore_as_the_input_argument(self):
        # HARM: worse than the apply's, because undo is the recovery path. An
        # apply that fails leaves the scene as it was; a REVERT that fails
        # leaves the scene holding metadata the user asked to have taken back
        # off it, and the only tool for taking it back off is the one that
        # just failed.
        query, variables = _revert_update_request()
        self.assertEqual(_bound_arguments(query, variables, "sceneUpdate"),
                         {"input": {"id": "sc-9", "title": "Old Title"}})
        self.assertEqual(_declared_type_of(query, "sceneUpdate", "input"),
                         "SceneUpdateInput!")


class TagWriteBindings(unittest.TestCase):
    def test_the_merge_sends_its_input_as_the_input_argument(self):
        # HARM: tagsMerge deletes the source tags permanently and nothing in
        # this module can undo it. A refused merge is the SAFE failure here —
        # what this pins is that the call is well-formed, so the consolidation
        # a user reviewed and approved is the one that actually runs, rather
        # than every merge failing and the duplicates staying put run after run.
        t = _QueryRecorder({"data": {"tagsMerge": {"id": CANONICAL_TAG_ID}}})
        _binding_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID])
        query, variables = t.only()
        self.assertEqual(_bound_arguments(query, variables, "tagsMerge"),
                         {"input": {"source": [TYPO_TAG_ID],
                                    "destination": CANONICAL_TAG_ID}})
        self.assertEqual(_declared_type_of(query, "tagsMerge", "input"),
                         "TagsMergeInput!")

    def test_the_alias_write_sends_its_input_as_the_input_argument(self):
        # HARM: an alias top-up that never lands is invisible — the caller is
        # told it worked, so every later run recomputes the same "missing"
        # aliases, and the duplicate spellings this write exists to absorb keep
        # being recreated by every scrape.
        t = _QueryRecorder({"data": {"tagUpdate": {"id": CANONICAL_TAG_ID}}})
        _binding_stash(t).update_tag_aliases(CANONICAL_TAG_ID, MERGED_ALIASES)
        query, variables = t.only()
        self.assertEqual(_bound_arguments(query, variables, "tagUpdate"),
                         {"input": {"id": CANONICAL_TAG_ID,
                                    "aliases": MERGED_ALIASES}})
        self.assertEqual(_declared_type_of(query, "tagUpdate", "input"),
                         "TagUpdateInput!")


# -- the entity-kind maps -------------------------------------------------- #


class EntityKindMaps(unittest.TestCase):
    """The two maps every find-or-create query is built out of, checked against
    the server's own names as stated at the top of this file — not against a
    copy that travels with them."""

    def test_the_search_map_names_what_the_server_calls_these_things(self):
        # HARM: the alias field is the entry that matters and the entry that
        # differs. Point the performer row at `aliases` and performer alias
        # resolution silently returns nothing: every name that exists only as
        # an existing performer's alias falls through to a create, the server
        # refuses it ("used as alias for..."), and that refusal fails the whole
        # scene apply — costing the scene its title, date, cover and every
        # other performer. The search field and result block are here too: get
        # either wrong and the read raises on every call.
        self.assertEqual(Stash._FIELDS, SERVER_FIND)

    def test_the_create_map_names_what_the_server_calls_these_things(self):
        # HARM: as CreateMutationBindings above, stated as a map rather than
        # per query, so a wrong entry fails once and names the kind rather than
        # failing three tests obliquely.
        self.assertEqual(Stash._CREATE,
                         {kind: row[3:] for kind, row in SERVER_ENTITY_API.items()})

    def test_the_two_maps_cover_the_same_kinds(self):
        # HARM: find_or_create reads both for one kind. A kind present in one
        # and missing from the other raises KeyError mid-apply — after some of
        # the match has already resolved, which is the one moment apply_scene
        # is written to avoid failing at.
        self.assertEqual(set(Stash._FIELDS), set(Stash._CREATE))


# A name the server holds only as some other entity's alias, and the entity
# that owns it. Deliberately kind-neutral: the same pair is driven through all
# three kinds below, because the rule is the same for all three and only the
# field name differs.
ALIAS_SPELLING = "Harbourlight"
ALIAS_OWNER_NAME = "Harbour Light"
ALIAS_OWNER_ID = "entity-alias-owner"


class AliasFieldPerKind(unittest.TestCase):
    def test_a_name_held_only_as_an_alias_resolves_for_every_kind(self):
        # HARM: the behavioural half of the map test above, and the reason the
        # performer entry could be broken with a green suite. Each kind's alias
        # list arrives under a different field name — performers `alias_list`,
        # studios and tags `aliases` — so asking for the wrong one returns rows
        # with no alias list at all and the alias hit is simply never seen.
        # The existing alias tests all drive the TAG kind, whose field name is
        # shared with studios; the one entry that cannot be guessed from its
        # neighbours was the one entry nothing exercised.
        #
        # The fake shapes its rows from SERVER_FIND, which is this file's own
        # independent statement of the server's names — so a mutation of the
        # client's map is contradicted here rather than travelling with it.
        for kind in sorted(SERVER_FIND):
            with self.subTest(kind=kind):
                t = _ResolveTransport(kind, results={ALIAS_SPELLING: [[
                    {"id": ALIAS_OWNER_ID, "name": ALIAS_OWNER_NAME,
                     "aliases": [ALIAS_SPELLING]}]]})
                self.assertEqual(
                    _resolve_stash(t)._find_first(kind, ALIAS_SPELLING),
                    ALIAS_OWNER_ID)


# -- the alias-clash pattern ----------------------------------------------- #
#
# _ALIAS_CLASH reads the alias owner's name out of the server's refusal. The
# owner is quoted LAST, and an owner's name may itself contain an apostrophe,
# so the match runs greedily up to a closing quote at the END of the message.
# The only fixture that reached it used an owner name with no apostrophe in it,
# which is the one case the greedy match exists for.
APOSTROPHE_OWNER_NAME = "O'Hare's Lantern"
APOSTROPHE_OWNER_ID = "tag-apostrophe-owner"


class AliasClashPattern(unittest.TestCase):
    def test_an_owner_name_containing_an_apostrophe_is_read_whole(self):
        # HARM: the refusal message is the ONLY thing that names the owner —
        # the re-search for the refused spelling already failed, which is why
        # the create was attempted. Stop the match at the first apostrophe
        # inside the owner's name and the client looks up "O" instead: that
        # resolves to nothing (or, worse, to whatever a fuzzy search for a
        # single letter ranks first), so the tag fails the whole scene apply —
        # or attaches an unrelated entity to it.
        #
        # The searches are asserted, not just the result: a fixture that only
        # checked the returned id would also pass if the client had found the
        # owner some other way.
        t = _ResolveTransport(
            "tag",
            results={APOSTROPHE_OWNER_NAME: [[{"id": APOSTROPHE_OWNER_ID,
                                               "name": APOSTROPHE_OWNER_NAME}]]},
            create=StashError("name '%s' is used as alias for '%s'"
                              % (TYPO_TAG_NAME, APOSTROPHE_OWNER_NAME)))
        self.assertEqual(_resolve_stash(t)._create("tag", TYPO_TAG_NAME),
                         APOSTROPHE_OWNER_ID)
        self.assertEqual(t.searches, [TYPO_TAG_NAME, APOSTROPHE_OWNER_NAME])

    def test_a_refusal_that_does_not_end_at_the_owner_is_not_guessed_at(self):
        # HARM: the end anchor is what confines this recovery to the one
        # message shape the module documents. Without it the pattern pulls a
        # quoted fragment out of the MIDDLE of any refusal that happens to
        # contain the words, looks that fragment up, and attaches whatever it
        # resolves to — a wrong performer or tag on the scene, from a message
        # that was never the clash this code handles.
        #
        # Uncertainty may withhold evidence, never supply it: an unrecognised
        # refusal is re-raised, apply_scene records the name as skipped, and
        # the rest of the scene still lands. The fixture holds the quoted
        # entity in the fake server, so an unanchored pattern would find it and
        # return successfully — the raise is the assertion.
        t = _ResolveTransport(
            "tag",
            results={CANONICAL_TAG_NAME: [[{"id": "tag-canonical",
                                            "name": CANONICAL_TAG_NAME}]]},
            create=StashError("name '%s' is used as alias for '%s' (tag 41); "
                              "rename it first"
                              % (TYPO_TAG_NAME, CANONICAL_TAG_NAME)))
        with self.assertRaises(StashError) as ctx:
            _resolve_stash(t)._create("tag", TYPO_TAG_NAME)
        self.assertIn("rename it first", str(ctx.exception))  # the server's own


# -- de-duplication of the ids an apply resolves --------------------------- #

# The same performer and the same tag, each under TWO spellings the fake server
# resolves to ONE id — a display name and a spelling it also answers to. Two
# different names resolving to one entity is what makes the fixture pin
# de-duplication BY THE RESOLVED ID: de-duplicating the names instead would let
# both straight through.
DOUBLE_SPELLING_FOUND = {
    "studio": {STUDIO_NAME: STUDIO_ID},
    "performer": {"Velvet Crane": "performer-velvet",
                  "Crane, Velvet": "performer-velvet"},
    "tag": {CANONICAL_TAG_NAME: "tag-canonical",
            TYPO_TAG_NAME: "tag-canonical"},
}


class ResolvedIdDeduplication(unittest.TestCase):
    def test_two_names_resolving_to_one_entity_are_written_once(self):
        # HARM: sceneUpdate REPLACES the performer and tag arrays, so what goes
        # out is the scene's whole cast, and a repeated id makes that a
        # malformed set. The run's own report counts it twice (apply_scene
        # returns len() of the merged lists), the `prior` snapshot records the
        # doubled list as the state an undo should restore, and a server that
        # refuses a duplicated id in a replace-array fails the entire apply
        # over it — a scene that would otherwise have applied cleanly.
        #
        # A scrape naming one performer twice is ordinary: an adapter that
        # reads both a credits list and a title line, or a tag that appears
        # under its canonical name and its alias in the same match.
        t = _shape_transport(found=DOUBLE_SPELLING_FOUND)
        result = _shape_stash(t).apply_scene("sc-9", {
            "performers": [{"name": "Velvet Crane"}, {"name": "Crane, Velvet"}],
            "tags": [{"name": CANONICAL_TAG_NAME}, {"name": TYPO_TAG_NAME}]})
        self.assertEqual(t.scene_update_input["performer_ids"],
                         ["performer-existing", "performer-velvet"])
        self.assertEqual(t.scene_update_input["tag_ids"],
                         ["tag-existing", "tag-canonical"])
        # ...and what the run reports back is what it wrote
        self.assertEqual(result["performers"], 2)
        self.assertEqual(result["tags"], 2)


# -- the request worker thread --------------------------------------------- #


class _RecordingThreading:
    """The `threading` module as gql sees it, with Thread subclassed so the
    worker it constructs is visible to a test. The real Thread still runs the
    work and the real Event still ends the wait, so the call behaves exactly as
    it does unpatched — only the construction is observed."""

    Event = threading.Event

    def __init__(self):
        self.threads = []
        recorder = self

        class _Thread(threading.Thread):
            def __init__(self, *args, **kwargs):
                threading.Thread.__init__(self, *args, **kwargs)
                recorder.threads.append(self)

        self.Thread = _Thread


class RequestWorkerThread(unittest.TestCase):
    def test_the_request_worker_is_a_daemon(self):
        # HARM: the hard deadline ABANDONS a wedged request — it returns to the
        # caller and leaves the worker running, forever if the host never
        # answers. That is only survivable because the worker is a daemon. A
        # non-daemon thread is joined by the interpreter at exit, so a single
        # wedged host turns "one transient error, retry next run" into a
        # process that finishes all its work, prints its summary, and then
        # never exits: no error, no output, nothing to retry. It is precisely
        # the hang the deadline exists to prevent, relocated to shutdown where
        # it is harder to attribute. The deadline's arithmetic is pinned above;
        # the flag that makes abandoning safe was not.
        #
        # Asserted on the constructed thread's own `daemon`, not on the keyword
        # it was passed, because the attribute is what the interpreter reads.
        # Proving the exit behaviour itself would need a subprocess; this flag
        # is the discriminator, and it is the only thing that differs between a
        # worker the interpreter waits for and one it does not.
        fake = _RecordingThreading()
        with mock.patch("cronicled.stash.threading", fake):
            Stash(SERVER_URL, API_KEY,
                  transport=_transport([{"data": {}}])).gql("query{x}")
        self.assertEqual(len(fake.threads), 1)
        self.assertIs(fake.threads[0].daemon, True)


# -- what the two irreversible tag writes put on the wire ------------------ #
#
# Every id this module READS arrives from the server as a string, so the
# coercion below is only ever exercised by a caller that computed an id itself
# — a merge plan built from a store, a number typed on the command line.
NUMERIC_DESTINATION_ID = 41
NUMERIC_SOURCE_IDS = [42, 43]


class IrreversibleTagWriteTypes(unittest.TestCase):
    """merge_tags and update_tag_aliases have no read-back and no undo, so the
    request body is the only thing a test can check — and the only description
    of what the module claims to send."""

    def test_the_merge_sends_every_id_as_a_string(self):
        # PINS CURRENT BEHAVIOUR, with a stated residual: a spec-compliant
        # GraphQL server coerces an Int given for an `ID` argument to a String
        # itself, so sending 41 rather than "41" is not a proven outage. What
        # this pins is that the body is the shape the module says it sends, and
        # — the part that is load-bearing — that the destination id is sent
        # IDENTICALLY in the two places it appears. `destination` names the tag
        # that survives and `values.id` names the tag whose alias list is
        # replaced; the existing merge tests assert those two are equal, and
        # coercing only one of them would make them unequal in type while a
        # sloppier comparison still passed.
        #
        # Asserted as the whole input, so a key added to an irreversible
        # mutation is caught here as well.
        t = _TagTransport()
        _tag_stash(t).merge_tags(NUMERIC_DESTINATION_ID, NUMERIC_SOURCE_IDS,
                                 aliases=MERGED_ALIASES)
        self.assertEqual(t.only(),
                         {"source": ["42", "43"], "destination": "41",
                          "values": {"id": "41", "aliases": MERGED_ALIASES}})

    def test_the_alias_write_sends_its_id_as_a_string(self):
        # PINS CURRENT BEHAVIOUR, same residual as above.
        t = _TagTransport()
        _tag_stash(t).update_tag_aliases(NUMERIC_DESTINATION_ID, MERGED_ALIASES)
        self.assertEqual(t.only(), {"id": "41", "aliases": MERGED_ALIASES})

    def test_an_alias_collection_that_is_not_a_list_is_copied_into_one(self):
        # HARM: this one is not cosmetic. A caller computing an alias top-up
        # naturally produces a set or a generator — the spellings a merge
        # folded in, the ones an external source knows about — and json.dumps
        # refuses both outright. Without the copy the alias write does not go
        # out malformed, it does not go out AT ALL: the call raises from inside
        # the transport, or the body serialises as something the server cannot
        # read, while the caller has already recorded the top-up as done.
        #
        # json.dumps is called on the recorded input as well, because "it is a
        # list" and "it would survive the wire" are the same claim here and the
        # fake transport does not serialise anything itself.
        for label, supplied in (("generator", (a for a in MERGED_ALIASES)),
                                ("tuple", tuple(MERGED_ALIASES))):
            with self.subTest(supplied=label):
                t = _TagTransport()
                _tag_stash(t).update_tag_aliases(CANONICAL_TAG_ID, supplied)
                self.assertEqual(t.only(), {"id": CANONICAL_TAG_ID,
                                            "aliases": MERGED_ALIASES})
                json.dumps(t.only())

    def test_the_merge_copies_its_alias_collection_too(self):
        # HARM: the same harm through the other write, which carries its own
        # copy of the same coercion — fixing one would leave the other broken.
        # And this one is the more expensive: the aliases travel WITH a merge
        # that permanently deletes the source tags, so a body the server
        # refuses either loses the merge or loses the spellings the merge was
        # run to preserve.
        t = _TagTransport()
        _tag_stash(t).merge_tags(CANONICAL_TAG_ID, [TYPO_TAG_ID],
                                 aliases=(a for a in MERGED_ALIASES))
        self.assertEqual(t.only()["values"],
                         {"id": CANONICAL_TAG_ID, "aliases": MERGED_ALIASES})
        json.dumps(t.only())


class CompositeFieldsAreSelectedWithSubfields(unittest.TestCase):
    """A field whose type is an object needs a selection set. Written bare it
    is not a narrower read -- the server rejects the WHOLE query, so every
    call using it fails.

    This shipped. `stash_ids` (type `[StashID!]!`) was selected bare in the
    read that builds an apply's undo snapshot, so every apply against a real
    server failed with a validation error while the entire suite stayed
    green. Nothing here parses a query: the transport under test is a double
    that returns canned dictionaries for any string at all, so a query can be
    well-formed Python, invalid GraphQL, and fully "covered".

    Narrow by design -- it knows only the composite fields this client
    actually selects. The general fix is validating every query against the
    server's published schema offline, which is filed separately.
    """

    # Read off the upstream schema rather than guessed. `StashID` is
    # {endpoint, stash_id, updated_at}; only the first two are accepted by
    # `StashIDInput` on the way back, which is why the snapshot takes those.
    _COMPOSITE = ("stash_ids", "studio", "performers", "tags", "files",
                  "scene", "findScene")

    @staticmethod
    def _graphql_literals():
        """Every GraphQL document in the client, taken from the AST rather
        than by scanning the source -- a regex over Python text also finds
        dict keys and prose, which is how the first attempt at this test
        produced false failures."""
        import ast
        import inspect
        from cronicled import stash as stash_module
        tree = ast.parse(inspect.getsource(stash_module))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.strip()
                if text.startswith(("query", "mutation")):
                    out.append(node.value)
        return out

    def test_there_are_queries_to_check(self):
        # Without this the scan passes by finding nothing, which is how it
        # would read as green after a refactor moved the queries elsewhere.
        self.assertGreater(len(self._graphql_literals()), 3)

    def test_every_composite_field_selected_has_a_selection_set(self):
        import re
        for doc in self._graphql_literals():
            for field in self._COMPOSITE:
                for m in re.finditer(
                        r"(?<![A-Za-z_$])%s(?![A-Za-z_])" % re.escape(field),
                        doc):
                    tail = doc[m.end():].lstrip()
                    if tail[:1] in ("{", "("):
                        continue          # has a selection, or is a call
                    self.fail(
                        "%r is a composite field selected without a "
                        "selection set. The server rejects the whole query, "
                        "so every call using it fails -- and no fake "
                        "transport will ever notice.\n  in: %s"
                        % (field, " ".join(doc.split())[:160]))


# -- identifying a file by its own fingerprints ---------------------------- #
#
# The stash-box half of this client. Nothing here searches for text: the
# server computes a scene's hashes and asks a box which scene they belong to,
# and the box either recognises the file or does not.
#
# Every fixture below is invented. `example.invalid` is reserved by RFC 2606
# and can never resolve.

BOX_ONE = {"name": "north-box", "endpoint": "https://one.example.invalid/gql"}
BOX_TWO = {"name": "south-box", "endpoint": "https://two.example.invalid/gql"}


def _boxes_reply(boxes):
    return {"data": {"configuration": {"general": {"stashBoxes": boxes}}}}


def _match(title, remote_site_id):
    """One `ScrapedScene` as a box returns it, carrying the box's own id for
    the scene alongside the fields an apply would write."""
    return {"title": title, "code": None, "details": None, "director": None,
            "urls": [], "url": None, "date": None, "image": None,
            "studio": None, "tags": [], "performers": [],
            "remote_site_id": remote_site_id}


class ConfiguredStashBoxes(unittest.TestCase):
    def test_it_returns_every_configured_box_in_the_server_s_own_order(self):
        # The order is the operator's configured order and callers are told
        # to try boxes in it, so a client that re-ordered (or de-duplicated,
        # or sorted) would silently change which box's copy of a scene a
        # proposal carries.
        t = _transport([_boxes_reply([BOX_TWO, BOX_ONE])])
        got = Stash("http://example.test", "k", transport=t).stash_boxes()
        self.assertEqual(got, [BOX_TWO, BOX_ONE])

    def test_no_boxes_configured_is_an_empty_list_not_a_failure(self):
        # A fresh install has none. Fingerprint identification is an addition
        # to the text path, never a replacement, so having nobody to ask is an
        # ordinary state and must not raise.
        t = _transport([_boxes_reply(None)])
        self.assertEqual(
            Stash("http://example.test", "k", transport=t).stash_boxes(), [])

    def test_the_query_asks_the_server_s_configuration_for_its_boxes(self):
        # Asserted as PROPERTIES of the document, never against a constant the
        # client also builds the request from: a query swapped for a sibling
        # that returns a different thing would move both sides together.
        t = _transport([_boxes_reply([])])
        Stash("http://example.test", "k", transport=t).stash_boxes()
        query = t.calls[0][0]["query"]
        self.assertIn("configuration", query)
        self.assertIn("general", query)
        self.assertIn("stashBoxes", query)

    def test_it_selects_the_two_fields_a_caller_needs_to_ask_a_box(self):
        # A name to report and an endpoint to address. Selecting only one of
        # them leaves a caller unable either to name the box in a proposal or
        # to send it anything.
        t = _transport([_boxes_reply([])])
        Stash("http://example.test", "k", transport=t).stash_boxes()
        query = t.calls[0][0]["query"]
        self.assertIn("name", query)
        self.assertIn("endpoint", query)

    def test_the_box_api_key_is_never_asked_for(self):
        # HARM: the server holds each box's key and uses it on our behalf
        # when we name the endpoint, so this client has no use for one.
        # Selecting a secret in order to discard it puts it in a response
        # body, and in whatever ever logs one.
        t = _transport([_boxes_reply([])])
        Stash("http://example.test", "k", transport=t).stash_boxes()
        self.assertNotIn("api_key", t.calls[0][0]["query"])


KEYED_BOX_ONE = dict(BOX_ONE, api_key="key-one")
KEYED_BOX_TWO = dict(BOX_TWO, api_key="key-two")


class ConfiguredStashBoxCredentials(unittest.TestCase):
    """The sibling read, for the caller that talks to a box ITSELF."""

    def test_it_returns_every_configured_box_in_the_server_s_own_order(self):
        # The order is the operator's, and a caller treats it as the
        # preference between two sources that both answer -- so a client that
        # re-ordered, sorted or de-duplicated would silently change which
        # source's description a proposal carries.
        t = _transport([_boxes_reply([KEYED_BOX_TWO, KEYED_BOX_ONE])])
        got = Stash("http://example.test", "k",
                    transport=t).stash_box_credentials()
        self.assertEqual(got, [KEYED_BOX_TWO, KEYED_BOX_ONE])

    def test_the_answer_is_a_sequence_and_never_keyed_by_name(self):
        # HARM: the order IS the answer to "which source wins", so carrying
        # it in a container that could put it at the mercy of a key's hash --
        # or that would collapse two boxes an operator named the same -- is
        # the resolution-by-iteration-order this project has removed from
        # five places.
        t = _transport([_boxes_reply([KEYED_BOX_ONE, KEYED_BOX_TWO])])
        got = Stash("http://example.test", "k",
                    transport=t).stash_box_credentials()
        self.assertIsInstance(got, list)

    def test_no_boxes_configured_is_an_empty_list_not_a_failure(self):
        t = _transport([_boxes_reply(None)])
        self.assertEqual(Stash("http://example.test", "k",
                               transport=t).stash_box_credentials(), [])

    def test_it_selects_the_key_because_this_caller_actually_uses_it(self):
        # HARM: without it every request to a configured source is anonymous,
        # and a source that requires a key contributes nothing while
        # reporting itself as unreachable. This is the one difference from
        # `stash_boxes`, whose caller genuinely has no use for a secret.
        t = _transport([_boxes_reply([])])
        Stash("http://example.test", "k", transport=t).stash_box_credentials()
        query = t.calls[0][0]["query"]
        self.assertIn("api_key", query)
        self.assertIn("name", query)
        self.assertIn("endpoint", query)

    def test_the_query_asks_the_server_s_configuration_for_its_boxes(self):
        # Asserted as PROPERTIES of the document, never against a constant
        # the client also builds the request from: a query swapped for a
        # sibling would move both sides together.
        t = _transport([_boxes_reply([])])
        Stash("http://example.test", "k", transport=t).stash_box_credentials()
        query = t.calls[0][0]["query"]
        self.assertIn("configuration", query)
        self.assertIn("general", query)
        self.assertIn("stashBoxes", query)


class FingerprintLookup(unittest.TestCase):

    def _stash(self, responses):
        return Stash("http://example.test", "k", transport=_transport(responses))

    def _reply(self, per_scene):
        return {"data": {"scrapeMultiScenes": per_scene}}

    def test_the_answer_keeps_the_order_the_scenes_were_asked_about_in(self):
        # Only some scenes match, and the two that do match DIFFERENTLY, at
        # positions that are not mirror images of each other. A fixture where
        # every scene matched could not tell a preserved order from a
        # shuffled one -- and neither could a symmetric one: a single hit in
        # the middle of three survives being reversed unchanged, which is
        # exactly the mutation this test exists to catch.
        ledger = _match("Winter Ledger", "r-77")
        morning = _match("Morning Ritual", "r-12")
        t = _transport([self._reply([[], [ledger], [morning], []])])
        got = Stash("http://example.test", "k", transport=t
                    ).scrape_scenes_by_fingerprint(
                        BOX_ONE["endpoint"], ["11", "12", "13", "14"])
        self.assertEqual(got, [[], [ledger], [morning], []])

    def test_the_source_names_a_box_endpoint_and_the_input_the_scene_ids(self):
        # The whole variables shape, not a sampled key: supplying
        # `scraper_id` here instead would point the same query at a site
        # scraper, which answers about TEXT, and an added key would travel to
        # the server unnoticed.
        t = _transport([self._reply([[]])])
        Stash("http://example.test", "k", transport=t
              ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["4"])
        self.assertEqual(t.calls[0][0]["variables"], {
            "source": {"stash_box_endpoint": BOX_ONE["endpoint"]},
            "input": {"scene_ids": ["4"]},
        })

    def test_scene_ids_travel_as_strings_whatever_the_caller_held(self):
        t = _transport([self._reply([[], []])])
        Stash("http://example.test", "k", transport=t
              ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], [7, 8])
        self.assertEqual(t.calls[0][0]["variables"]["input"],
                         {"scene_ids": ["7", "8"]})

    def test_an_empty_batch_asks_the_box_nothing_at_all(self):
        t = _transport([self._reply([])])
        got = Stash("http://example.test", "k", transport=t
                    ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], [])
        self.assertEqual(got, [])
        self.assertEqual(t.calls, [])

    def test_a_short_reply_is_refused_rather_than_matched_up_by_position(self):
        # HARM: the reply carries no scene ids -- position is the ONLY thing
        # tying a match to its scene. Zipping a short reply against the
        # request would attribute one file's box metadata to a different
        # file, and nothing afterwards could detect it.
        t = _transport([self._reply([[_match("Winter Ledger", "r-1")]])])
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t
                  ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"],
                                                 ["1", "2", "3"])
        self.assertIn("3", str(ctx.exception))

    def test_a_long_reply_is_refused_too(self):
        # The other side of the same guard. A guard that only refuses one
        # direction is half a guard, and the surplus direction is just as
        # unalignable.
        t = _transport([self._reply([[], [], []])])
        with self.assertRaises(StashError):
            Stash("http://example.test", "k", transport=t
                  ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["1"])

    def test_a_reply_with_no_list_at_all_is_refused(self):
        t = _transport([self._reply(None)])
        with self.assertRaises(StashError):
            Stash("http://example.test", "k", transport=t
                  ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["1"])

    def test_a_reply_of_exactly_the_right_length_is_accepted(self):
        # The permissive side of the length guard, pinned so a mutation that
        # tightened it into refusing everything would fail something. A guard
        # that drifts too strict is as wrong as one too loose, and quieter.
        t = _transport([self._reply([[], []])])
        got = Stash("http://example.test", "k", transport=t
                    ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"],
                                                   ["1", "2"])
        self.assertEqual(got, [[], []])

    def test_the_query_asks_the_multi_scene_scrape_for_the_box_s_own_id(self):
        # Properties of the document. `remote_site_id` is the only thing that
        # makes two boxes' answers comparable, so a selection that dropped it
        # would leave a caller unable to tell agreement from conflict.
        t = _transport([self._reply([[]])])
        Stash("http://example.test", "k", transport=t
              ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["1"])
        query = t.calls[0][0]["query"]
        self.assertIn("scrapeMultiScenes", query)
        self.assertIn("remote_site_id", query)

    def test_the_query_asks_which_fingerprints_a_match_was_found_on(self):
        # `algorithm` is the only thing that tells a byte-identical match
        # from a perceptual one apart -- see `cronicled.scan._resolve_claims`
        # for the rule that reads it. A selection that dropped this would
        # leave every match looking exactly like the flat "identity" this
        # client's docstring used to (wrongly) promise.
        t = _transport([self._reply([[]])])
        Stash("http://example.test", "k", transport=t
              ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["1"])
        query = t.calls[0][0]["query"]
        self.assertIn("fingerprints", query)
        self.assertIn("algorithm", query)
        self.assertIn("hash", query)

    def test_it_selects_everything_an_apply_would_write_from_a_match(self):
        # A box match is carried into a proposal and applied by exactly the
        # same code a text-scraped candidate is, so a field missing here is a
        # field silently absent from the write. Named individually rather
        # than compared against the client's own selection constant, which
        # would move with any change to it.
        t = _transport([self._reply([[]])])
        Stash("http://example.test", "k", transport=t
              ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["1"])
        query = t.calls[0][0]["query"]
        for field in ("title", "details", "date", "urls", "code", "director",
                      "image", "studio", "performers", "tags"):
            self.assertIn(field, query)

    def test_the_ids_travel_as_variables_rather_than_baked_into_the_query(self):
        t = _transport([self._reply([[]])])
        Stash("http://example.test", "k", transport=t
              ).scrape_scenes_by_fingerprint(BOX_ONE["endpoint"], ["9"])
        body = t.calls[0][0]
        self.assertNotIn("9", body["query"])
        self.assertIn("ScrapeMultiScenesInput!", body["query"])
        self.assertIn("ScraperSourceInput!", body["query"])


# -- performer descriptions ------------------------------------------------ #


def _description_rows(n, prefix):
    return [{"id": "%s-%d" % (prefix, i), "name": "%s %d" % (prefix, i),
             "details": "a description for %s %d" % (prefix, i)}
            for i in range(n)]


class PerformersWithDescriptions(unittest.TestCase):
    """The read the description pass makes, and the field it depends on.

    `details` is the media server's own name for a performer's description --
    an EXTERNAL fact, restated here rather than imported from the client, so
    these assertions are a check rather than a mirror. A client that selected
    some other field would come back with `None` for every performer and the
    pass would report a library with nothing wrong with it, forever, with
    nothing anywhere saying otherwise.
    """

    def test_the_query_asks_for_the_field_the_description_lives_in(self):
        t = _PerformerReadTransport(pages=[_description_rows(2, "alpha")])
        _read_stash(t).performers_with_descriptions()
        query, _ = t.calls[0]
        self.assertIn("findPerformers(", query)
        self.assertIn("details", query)
        self.assertIn("name", query)

    def test_it_returns_every_row_the_server_gave_it_untouched(self):
        rows = _description_rows(3, "alpha")
        t = _PerformerReadTransport(pages=[rows])
        self.assertEqual(_read_stash(t).performers_with_descriptions(), rows)

    def test_it_pages_past_the_first_page(self):
        # HARM: a performer whose name sorts past the first page would never
        # be looked at, so a markup-ridden description on any but the first
        # 500 performers is invisible and stays that way.
        pages = [_description_rows(PERFORMER_PAGE_SIZE, "alpha"),
                 _description_rows(3, "omega")]
        t = _PerformerReadTransport(pages=pages)
        got = _read_stash(t).performers_with_descriptions()
        self.assertEqual(got, pages[0] + pages[1])
        self.assertEqual([v["f"]["page"] for _, v in t.calls], [1, 2])

    def test_a_performer_with_no_description_comes_back_as_it_is(self):
        # A normal answer, not a gap: most performers have none, and the
        # client must not invent a string for them.
        t = _PerformerReadTransport(
            pages=[[{"id": "1", "name": "Wren Alderly", "details": None}]])
        got = _read_stash(t).performers_with_descriptions()
        self.assertEqual(got, [{"id": "1", "name": "Wren Alderly",
                                "details": None}])


class _PerformerWriteTransport:
    """A media server holding one performer's description.

    The READ answers whatever the description currently is; the WRITE records
    the whole update input and then changes it, so a test can assert both what
    was sent and what the server was left holding. `writes` stays empty when
    the client refuses, which is how "nothing was written" is asserted rather
    than assumed.
    """

    def __init__(self, details, performer_id="7", missing=False):
        self.details = details
        self.performer_id = performer_id
        self.missing = missing
        self.writes = []
        self.reads = 0

    def __call__(self, body, timeout):
        query, variables = body["query"], body["variables"]
        if "findPerformer(" in query:
            self.reads += 1
            if self.missing:
                return {"data": {"findPerformer": None}}
            return {"data": {"findPerformer": {"id": self.performer_id,
                                               "details": self.details}}}
        if "performerUpdate(" in query:
            self.writes.append(variables["in"])
            self.details = variables["in"].get("details", self.details)
            return {"data": {"performerUpdate": {"id": self.performer_id}}}
        raise AssertionError("test transport does not recognize query: %s"
                             % query)


class ApplyPerformerDescription(unittest.TestCase):
    def test_it_writes_exactly_the_id_and_the_description_and_nothing_else(self):
        # The WHOLE input, not a check that `details` is in it. An unlisted
        # extra key on a performer update is how a field nobody meant to touch
        # gets blanked across a library, and a field-by-field assertion cannot
        # see one.
        t = _PerformerWriteTransport("<p>Before.</p>")
        Stash("http://example.test", "k", transport=t).\
            apply_performer_description("7", "Before.",
                                        expected="<p>Before.</p>")
        self.assertEqual(t.writes, [{"id": "7", "details": "Before."}])

    def test_it_returns_the_text_it_replaced_as_the_undo_snapshot(self):
        t = _PerformerWriteTransport("<p>Before.</p>")
        result = Stash("http://example.test", "k", transport=t).\
            apply_performer_description("7", "Before.",
                                        expected="<p>Before.</p>")
        self.assertEqual(result, {"prior": {"details": "<p>Before.</p>"}})

    def test_a_description_edited_since_the_scan_is_refused_and_not_written(self):
        # HARM: the cleaned text is a function of the exact description it was
        # computed from. Writing it over something somebody has since edited
        # replaces their edit with a tidied-up copy of what they edited away,
        # and the only record that it happened is an undo snapshot nobody is
        # looking at.
        t = _PerformerWriteTransport("Rewritten by hand since the scan.")
        with self.assertRaises(StashError) as ctx:
            Stash("http://example.test", "k", transport=t).\
                apply_performer_description("7", "Before.",
                                            expected="<p>Before.</p>")
        self.assertIn("changed since the scan", str(ctx.exception))
        self.assertEqual(t.writes, [])
        self.assertEqual(t.details, "Rewritten by hand since the scan.")

    def test_a_performer_the_server_does_not_know_is_refused(self):
        t = _PerformerWriteTransport(None, missing=True)
        with self.assertRaises(StashError):
            Stash("http://example.test", "k", transport=t).\
                apply_performer_description("7", "Before.",
                                            expected="<p>Before.</p>")
        self.assertEqual(t.writes, [])

    def test_the_check_reads_the_server_rather_than_trusting_the_caller(self):
        # The read has to actually happen: a client that compared `expected`
        # with itself would agree every time and write unconditionally.
        t = _PerformerWriteTransport("<p>Before.</p>")
        Stash("http://example.test", "k", transport=t).\
            apply_performer_description("7", "Before.",
                                        expected="<p>Before.</p>")
        self.assertEqual(t.reads, 1)


class RevertPerformerDescription(unittest.TestCase):
    def test_it_writes_back_the_snapshot_exactly_including_whitespace(self):
        # Every character, whitespace and all. A revert that tidied what it
        # restored would leave the field in a third state that is neither what
        # was there before nor what was applied.
        prior = {"details": "  <p>Before.</p>\n\n   trailing spaces   "}
        t = _PerformerWriteTransport("After.")
        Stash("http://example.test", "k", transport=t).\
            revert_performer_description("7", prior)
        self.assertEqual(
            t.writes,
            [{"id": "7",
              "details": "  <p>Before.</p>\n\n   trailing spaces   "}])

    def test_it_restores_an_empty_description_rather_than_refusing(self):
        # A performer who had no description before the apply. Restoring
        # exactly that is the job; treating it as a missing snapshot would
        # leave the applied text in place and report success.
        t = _PerformerWriteTransport("After.")
        Stash("http://example.test", "k", transport=t).\
            revert_performer_description("7", {"details": ""})
        self.assertEqual(t.writes, [{"id": "7", "details": ""}])

    def test_a_missing_or_empty_snapshot_is_refused_and_nothing_is_written(self):
        # A revert that no-ops is indistinguishable from one that worked.
        for prior in (None, {}, {"nothing": "useful"}):
            with self.subTest(prior=prior):
                t = _PerformerWriteTransport("After.")
                with self.assertRaises(ValueError):
                    Stash("http://example.test", "k", transport=t).\
                        revert_performer_description("7", prior)
                self.assertEqual(t.writes, [])


class PerformerDescription(unittest.TestCase):
    def test_it_reads_the_current_text(self):
        t = _PerformerWriteTransport("<p>Now.</p>")
        got = Stash("http://example.test", "k",
                    transport=t).performer_description("7")
        self.assertEqual(got, "<p>Now.</p>")

    def test_an_unknown_performer_reads_as_no_description(self):
        t = _PerformerWriteTransport(None, missing=True)
        got = Stash("http://example.test", "k",
                    transport=t).performer_description("7")
        self.assertIsNone(got)


# -- retrying a store fault that looks transient ---------------------------- #
#
# THE DEFECT: a store answered a name search with a GraphQL error twice, on
# different files, and nothing retried. The error was the media server
# reporting that its configured scraper had handed back a single object where
# the schema promises a list — a fault in the scraper, upstream of this
# project. The identical queries succeeded when they were repeated by hand
# (sixteen out of sixteen), so the failure is intermittent and one repeat
# recovers the file.
#
# Two rules are under test here and they are pinned separately, because each
# fails a different way round:
#
#   the CLASSIFICATION — which failures are worth repeating. Too narrow and
#   the fault above stays unretried; too wide and a request the server will
#   refuse verbatim is sent again, and a genuinely rejected input reads as a
#   flaky network.
#
#   the LOOP — how many times. Unbounded, a permanently-broken store spins
#   forever inside one file's examination and the run never terminates;
#   absent, the classification buys nothing at all.
#
# Every fixture below is invented. `_SCRAPER_FAULT` reproduces the SHAPE of
# the observed message — an upstream Go type name a reader of this project
# cannot act on — under an invented scraper name, because what the tests
# assert is that the recorded text is readable WITHOUT that tail.
#
# No test here sleeps: the delay between attempts is an injected collaborator,
# recorded rather than paid, on the same seam `transport` already uses.

_SCRAPER_FAULT = (
    "error while name scraping with scraper Bramblewick: could not unmarshal "
    "json from script output: json: cannot unmarshal object into Go value of "
    "type []models.ScrapedScene")


def _graphql_error(message):
    """A payload the media server answers with when the operation failed
    server-side: no `data`, an `errors` array. Returned by the transport, not
    raised by it — that is the real path, and `_attempt`'s errors branch is
    the code under test."""
    return {"errors": [{"message": message}]}


def _scripted_transport(script):
    """A fake transport that plays `script` IN ORDER — an exception instance
    is raised, a dict is returned — and refuses to run off the end.

    Deliberately not `_transport`'s "repeat the last entry forever" behaviour.
    A bound-under-test needs a script LONGER than the bound, so that a loop
    mutated to unlimited terminates with the wrong answer and fails an
    assertion instead of hanging the suite — a hang is not a failing test.
    Running off the end raising is the fake reporting the opposite mistake:
    more calls than the test was written to describe."""
    calls = []

    def send(body, timeout):
        calls.append((body, timeout))
        if len(calls) > len(script):
            raise AssertionError(
                "the client made call %d against a transport scripted for %d"
                % (len(calls), len(script)))
        item = script[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    send.calls = calls
    return send


def _retrying_stash(script):
    """(client, transport, sleeps) — a client whose retry delay is recorded on
    `sleeps` instead of waited out."""
    sleeps = []
    transport = _scripted_transport(script)
    return (Stash(SERVER_URL, API_KEY, transport=transport,
                  sleep=sleeps.append),
            transport, sleeps)


_SCRAPED = _scraped_scene(title="Copper Kettle")
_SCRAPE_ANSWER = {"data": {"scrapeSingleScene": [_SCRAPED]}}


class TheRetryBound(unittest.TestCase):
    """`RETRY_ATTEMPTS` / `RETRY_DELAY` — the two numbers that decide what a
    caller can be made to wait. Pinned as values because everything else in
    this section is measured against them."""

    def test_the_bound_is_a_total_of_two_attempts(self):
        # HARM in either direction: 1 is no retry at all, and the defect this
        # exists for stays unfixed silently — a store simply answers less
        # often than it could. A larger number multiplies the cost of the
        # evidence being wrong: intermittence rests on a failure to
        # reproduce, which is weaker than a reproduction, so the smallest
        # number that fixes the fault is the one the evidence supports.
        self.assertEqual(RETRY_ATTEMPTS, 2)

    def test_the_delay_between_attempts_is_positive(self):
        # HARM: a zero delay makes the retry actively harmful against the one
        # server that says so explicitly — 429 means "come back later", and
        # coming back immediately is what it asked the client not to do.
        self.assertGreater(RETRY_DELAY, 0)

    def test_the_delay_is_small_enough_to_sit_inside_a_callers_budget(self):
        # HARM: the delay is paid inside the caller's own wall clock, on top
        # of a hard deadline it already has to allow for. A delay of minutes
        # would make a failing store look like a wedged one to everything
        # upstream, which is the confusion the deadline machinery exists to
        # remove.
        self.assertLessEqual(RETRY_DELAY, 5)


class ATransientScrapeFailureIsRetried(unittest.TestCase):
    def test_a_scrape_that_fails_once_then_succeeds_returns_the_real_answer(self):
        # HARM: this is the defect. Treating the first failure as final loses
        # a file the store could identify — and loses it as a REFUSAL, so
        # nothing distinguishes it from a file the catalogue genuinely has
        # nothing for.
        stash, t, sleeps = _retrying_stash(
            [_graphql_error(_SCRAPER_FAULT), _SCRAPE_ANSWER])

        got = stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(got, [_SCRAPED])
        self.assertEqual(len(t.calls), 2, "the second attempt never happened")
        self.assertEqual(sleeps, [RETRY_DELAY],
                         "one gap, between the two attempts")

    def test_scraping_a_clip_page_by_url_is_retried_too(self):
        # HARM: the SAME scraper code runs behind this call, and its caller
        # (`scan.examine`'s enrichment) swallows the failure — so a blip here
        # never surfaces as an error at all. It surfaces as a proposal whose
        # payload silently lacks the creator, studio and date the fuller
        # record would have carried.
        stash, t, _ = _retrying_stash(
            [_graphql_error(_SCRAPER_FAULT),
             {"data": {"scrapeSceneURL": _SCRAPED}}])

        got = stash.scrape_scene_url(SCRAPE_URL)

        self.assertEqual(got, _SCRAPED)
        self.assertEqual(len(t.calls), 2)

    def test_no_delay_is_paid_when_the_first_attempt_answers(self):
        # HARM: a delay paid before the first attempt, or after a success,
        # taxes every healthy call in the run — thousands of them — for a
        # failure that did not happen.
        stash, t, sleeps = _retrying_stash([_SCRAPE_ANSWER])

        stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(sleeps, [])
        self.assertEqual(len(t.calls), 1)

    def test_the_default_delay_is_taken_from_the_clock_when_none_is_injected(self):
        # HARM: an injected `sleep` is what keeps the suite from paying the
        # delay, so a default that silently did nothing would look identical
        # in every test here and remove the pause in production — the one
        # place it matters. Patched rather than endured, so this test does
        # not sleep either.
        transport = _scripted_transport(
            [_graphql_error(_SCRAPER_FAULT), _SCRAPE_ANSWER])

        # Built inside the patch: the default is resolved once, at
        # construction, the same way the default transport is.
        with mock.patch("time.sleep") as slept:
            Stash(SERVER_URL, API_KEY,
                  transport=transport).scrape_scenes_by_query(
                      SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(slept.call_args_list, [mock.call(RETRY_DELAY)])


class TheRetryIsBounded(unittest.TestCase):
    """A store failing every attempt must end VISIBLY failing — bounded,
    recorded, and never as an answer."""

    # Longer than any bound under test, and ending in a success: a loop
    # mutated to unlimited would reach that success and return it, failing
    # the assertions below rather than hanging the suite.
    FIVE_FAILURES_THEN_AN_ANSWER = (
        [_graphql_error(_SCRAPER_FAULT)] * 5 + [_SCRAPE_ANSWER])

    def test_a_store_failing_every_attempt_stops_at_the_bound(self):
        # HARM: unbounded, one permanently-broken store spins inside one
        # file's examination forever and the run never terminates — no
        # proposals, no error, nothing to look at.
        stash, t, _ = _retrying_stash(self.FIVE_FAILURES_THEN_AN_ANSWER)

        with self.assertRaises(StashError):
            stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(len(t.calls), RETRY_ATTEMPTS)

    def test_a_store_failing_every_attempt_raises_rather_than_answering_empty(self):
        # HARM: the distinction the whole scan rests on. An empty list is
        # "this catalogue has nothing for this creator", which is what earns
        # a MUTE — a verdict that stops the file ever being looked at again.
        # An error is evidence about the round trip and must never be
        # convertible into one.
        stash, _, _ = _retrying_stash(self.FIVE_FAILURES_THEN_AN_ANSWER)

        with self.assertRaises(StashError) as ctx:
            stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertTrue(ctx.exception.transient,
                        "the fault was transient and stays so after the bound "
                        "— giving up is this loop's verdict, not a "
                        "reclassification of the failure")

    def test_the_exhausted_failure_says_how_many_attempts_it_took(self):
        # HARM: this is the whole message a person reads afterwards, via
        # `scan`'s `store_errors` line. Without the bound in it, a store that
        # failed twice is indistinguishable from one asked once, so "the
        # retry is not working" is not a conclusion anybody can reach — and
        # the upstream sentence alone is a Go type name, which says nothing
        # about this project at all. Asserted WHOLE: a test checking the
        # original text is merely present would pass on the behaviour that
        # shipped before this ticket.
        stash, _, _ = _retrying_stash(self.FIVE_FAILURES_THEN_AN_ANSWER)

        with self.assertRaises(StashError) as ctx:
            stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(str(ctx.exception),
                         _SCRAPER_FAULT + " — gave up after 2 attempts")

    def test_the_attempts_reported_are_counted_not_assumed(self):
        # HARM: a fixture of one repeat cannot tell a counter that
        # ACCUMULATES from a constant that happens to equal it, and a message
        # claiming two attempts after three would be a false record of what
        # the client actually spent. Three is the smallest bound that
        # separates the two.
        stash, t, sleeps = _retrying_stash(self.FIVE_FAILURES_THEN_AN_ANSWER)

        with mock.patch("cronicled.stash.RETRY_ATTEMPTS", 3):
            with self.assertRaises(StashError) as ctx:
                stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(len(t.calls), 3)
        self.assertEqual(len(sleeps), 2, "the gaps fall BETWEEN the attempts")
        self.assertEqual(str(ctx.exception),
                         _SCRAPER_FAULT + " — gave up after 3 attempts")

    def test_a_failure_that_turned_permanent_reports_only_what_was_spent(self):
        # The BOUND and what was SPENT are different numbers, and they coincide
        # everywhere else in this file — every other case exhausts the bound.
        # They part company only here: a transient failure followed by a
        # permanent one stops the loop early.
        #
        # HARM, both halves. A message built from the bound claims requests
        # that were never made, and it is the record a person uses to decide
        # whether the retry is working at all. And the failure that ENDED the
        # call was a refusal, so reporting it as transient would tell every
        # caller that reads the flag to try the same rejected input again —
        # which is the failure mode the classification exists to prevent.
        permanent = StashError("HTTP 400 from the media server: no",
                               transient=False)
        stash, t, sleeps = _retrying_stash(
            [_graphql_error(_SCRAPER_FAULT), permanent, _SCRAPE_ANSWER])

        with mock.patch("cronicled.stash.RETRY_ATTEMPTS", 3):
            with self.assertRaises(StashError) as ctx:
                stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(len(t.calls), 2,
                         "a refusal ends the loop; the third attempt the bound "
                         "allowed must not be spent")
        self.assertEqual(sleeps, [RETRY_DELAY])
        self.assertEqual(str(ctx.exception),
                         "HTTP 400 from the media server: no "
                         "— gave up after 2 attempts")
        self.assertFalse(ctx.exception.transient,
                         "the failure that ended the call was a refusal and "
                         "stays one")


class WhatIsNotRetried(unittest.TestCase):
    """The restrictive half. Pinned separately from the permissive half
    because a loop that retried everything passes every test above."""

    PERMANENT_HTTP = StashError("HTTP 400 from the media server: no",
                                transient=False)
    UNREACHABLE = StashError("cannot reach the media server: no route",
                             transient=True)

    def test_a_permanent_failure_of_a_retryable_call_is_not_retried(self):
        # HARM: the operation opting in is not the whole rule — the failure
        # still has to be one worth repeating. A scrape whose request the
        # server refused outright (a 4xx) would otherwise be sent again, and
        # a rule that retries a refusal is the rule that retries a rejected
        # input forever the moment the bound is ever raised.
        stash, t, sleeps = _retrying_stash([self.PERMANENT_HTTP, _SCRAPE_ANSWER])

        with self.assertRaises(StashError) as ctx:
            stash.scrape_scenes_by_query(SCRAPER_ID, SCRAPE_QUERY_TEXT)

        self.assertEqual(len(t.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(str(ctx.exception), str(self.PERMANENT_HTTP),
                         "a failure that was never repeated must not claim to "
                         "have been")

    def test_a_read_this_project_builds_itself_is_not_retried(self):
        # HARM: `findScenes` takes a filter this project constructs. An
        # `errors` array from it is the server rejecting THAT FILTER and it
        # will reject it identically next time — retrying buys nothing and
        # reclassifying it as transient would make a malformed query look
        # like a flaky network for as long as nobody fixes the query.
        stash, t, _ = _retrying_stash(
            [_graphql_error("unknown field organised on SceneFilterType"),
             {"data": {"findScenes": {"count": 0, "scenes": []}}}])

        with self.assertRaises(StashError) as ctx:
            stash.unorganized_scenes(None)

        self.assertEqual(len(t.calls), 1)
        self.assertFalse(ctx.exception.transient)
        self.assertEqual(str(ctx.exception),
                         "unknown field organised on SceneFilterType")

    def test_a_transient_failure_of_a_call_that_did_not_opt_in_is_not_retried(self):
        # HARM: the flag alone must not buy a retry, or every write in this
        # client gets one. A `performerCreate` whose transport failed may
        # have LANDED on the server anyway, so repeating it makes two
        # performers where the operator asked for one — a silent duplicate,
        # which is worse than the visible failure it replaces.
        stash, t, sleeps = _retrying_stash(
            [self.UNREACHABLE, {"data": {"findScenes": {"count": 0,
                                                        "scenes": []}}}])

        with self.assertRaises(StashError):
            stash.unorganized_scenes(None)

        self.assertEqual(len(t.calls), 1)
        self.assertEqual(sleeps, [])

    def test_the_fingerprint_batch_is_not_retried(self):
        # The deliberate edge of the carve-out, pinned so it cannot widen by
        # accident. HARM of widening it: this call asks a stash-box's own
        # database rather than running a scraper plugin, no fault of the
        # observed shape has come from it, and its misalignment guard raises
        # a PERMANENT error on purpose — repeating a reply that could not be
        # matched to the scenes it was asked about re-sends a request whose
        # answer was already unusable.
        stash, t, _ = _retrying_stash(
            [_graphql_error("box unavailable"),
             {"data": {"scrapeMultiScenes": [[]]}}])

        with self.assertRaises(StashError):
            stash.scrape_scenes_by_fingerprint("https://box.invalid/graphql",
                                               ["7"])

        self.assertEqual(len(t.calls), 1)


class TheRetryDoesNotMultiplyTheDeadline(unittest.TestCase):
    """What a caller can be made to wait, in total, for one `gql` call.

    The bound is

        RETRY_ATTEMPTS * (timeout + HARD_DEADLINE_SLACK)
            + (RETRY_ATTEMPTS - 1) * RETRY_DELAY

    and both factors are read off the client's own behaviour below rather
    than restated: the deadlines it actually asked to wait for, and the
    delays it actually asked to sleep for."""

    TIMEOUT = 7  # not the default, so a call ignoring the caller's bound shows

    class _BoundedDeadlines:
        """`_FakeThreading` narrowed by one thing: it reports "not finished"
        only a fixed number of times and then REFUSES.

        `_FakeThreading` reports it forever, which is what makes it readable —
        but every one of those waits ends in a transient failure, so a retry
        loop mutated to unlimited would spin here and HANG the suite. A hang is
        not a failing test. Refusing past the limit turns the same mutation
        into an exception the assertions below cannot absorb: it is not a
        `StashError`, so the loop cannot catch it and `assertRaises` cannot
        accept it."""

        Thread = threading.Thread

        def __init__(self, limit):
            self.waits = []
            recorder = self

            class _Event:
                def set(self):
                    pass

                def wait(self, timeout=None):
                    if len(recorder.waits) >= limit:
                        raise AssertionError(
                            "the client waited on a %d-th deadline; this test "
                            "describes at most %d" % (len(recorder.waits) + 1,
                                                      limit))
                    recorder.waits.append(timeout)
                    return False

            self.Event = _Event

    def test_each_attempt_hands_the_transport_the_timeout_the_caller_asked_for(self):
        # HARM: a per-attempt deadline scaled by the attempt number makes a
        # wedged host cost a multiple of the bound the caller asked for, and
        # the caller has no way to see it — the number it passed in is the
        # only one it knows.
        # Padded past the bound with an answer, for the reason
        # `TheRetryIsBounded`'s script is: a script that ran out would raise on
        # the worker thread, be reclassified as transient, and be retried
        # forever by an unbounded loop — hanging this test instead of failing
        # it.
        stash, t, _ = _retrying_stash(
            [_graphql_error(_SCRAPER_FAULT)] * 5 + [{"data": {}}])

        with self.assertRaises(StashError):
            stash.gql("query{x}", timeout=self.TIMEOUT, retryable=True)

        self.assertEqual([timeout for _body, timeout in t.calls],
                         [self.TIMEOUT, self.TIMEOUT])

    def test_the_total_wait_is_one_deadline_per_attempt_plus_the_gaps(self):
        # HARM: the retry wraps a whole attempt, deadline included, so the
        # worst case is additive. A retry INSIDE the deadline's own wait, or
        # a deadline recomputed to cover all attempts at once, would make a
        # wedged host hold the caller for a multiple of `timeout + slack`
        # with nothing observable saying so.
        #
        # Every wait is reported as unfinished, so each attempt ends by the
        # deadline firing — the most expensive attempt there is — and records
        # the deadline it was given. Nothing here waits.
        fake = self._BoundedDeadlines(RETRY_ATTEMPTS + 2)
        sleeps = []
        stash = Stash(SERVER_URL, API_KEY,
                      transport=_scripted_transport([{"data": {}}] * 4),
                      sleep=sleeps.append)

        with mock.patch("cronicled.stash.threading", fake):
            with self.assertRaises(StashError) as ctx:
                stash.gql("query{x}", timeout=self.TIMEOUT, retryable=True)

        one_deadline = self.TIMEOUT + HARD_DEADLINE_SLACK
        self.assertEqual(fake.waits, [one_deadline, one_deadline],
                         "one unmultiplied deadline per attempt, and exactly "
                         "as many attempts as the bound allows")
        self.assertEqual(sleeps, [RETRY_DELAY])
        self.assertEqual(sum(fake.waits) + sum(sleeps),
                         2 * one_deadline + RETRY_DELAY)
        self.assertIn("hard deadline", str(ctx.exception))
        self.assertIn("gave up after 2 attempts", str(ctx.exception))
