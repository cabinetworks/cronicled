import io
import json
import socket
import threading
import unittest
import urllib.error
from unittest import mock

from cronicled.stash import Stash, StashError


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

    def test_no_test_opens_a_socket(self):
        # the transport seam exists so the suite never needs the network
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

    _FIND = {"studio": ("findStudios", "studios", "aliases"),
             "performer": ("findPerformers", "performers", "alias_list"),
             "tag": ("findTags", "tags", "aliases")}
    _CREATE_MUTATION = {"studio": "studioCreate", "performer": "performerCreate",
                        "tag": "tagCreate"}

    def __init__(self, existing, found=None, create=None):
        self.existing = existing
        self.found = found or {}
        self.create = create or {}
        self.scene_update_input = None

    def __call__(self, body, timeout):
        q = body["query"]
        if "findScene(" in q:
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

    _FIND = {"studio": ("findStudios", "studios", "aliases"),
             "performer": ("findPerformers", "performers", "alias_list"),
             "tag": ("findTags", "tags", "aliases")}
    _CREATE_MUTATION = {"studio": "studioCreate", "performer": "performerCreate",
                        "tag": "tagCreate"}

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
