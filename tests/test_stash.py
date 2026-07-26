import io
import json
import threading
import unittest
import urllib.error
from unittest import mock

from cronicled.stash import (DEFAULT_TIMEOUT, HARD_DEADLINE_SLACK, Stash,
                             StashError)


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

    def __init__(self, merged=None):
        self.calls = []  # (query, input) for every mutation sent, in order
        self.merged = merged or {"id": "tag-canonical", "name": "Lantern Drift",
                                 "aliases": []}

    def __call__(self, body, timeout):
        q = body["query"]
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
    page number, so paging can be observed rather than assumed.
    """

    def __init__(self, scenes=None, count=None, tag_pages=None):
        self.calls = []  # (query, variables) for every request sent, in order
        self.scenes = [] if scenes is None else scenes
        self.count = len(self.scenes) if count is None else count
        self.tag_pages = [[]] if tag_pages is None else tag_pages

    def __call__(self, body, timeout):
        q, variables = body["query"], body["variables"]
        self.calls.append((q, variables))
        if "findScenes(" in q:
            return {"data": {"findScenes": {"count": self.count,
                                            "scenes": self.scenes}}}
        if "findTags(" in q:
            page = (variables.get("f") or {}).get("page", 1)
            rows = self.tag_pages[page - 1] if page <= len(self.tag_pages) else []
            total = sum(len(p) for p in self.tag_pages)
            return {"data": {"findTags": {"count": total, "tags": rows}}}
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
             "aliases": [], "scene_count": 0} for i in range(n)]


def _read_stash(transport):
    return Stash("http://example.test", "k", transport=transport)


class FindScenes(unittest.TestCase):
    def test_limit_none_asks_the_server_for_every_page_in_one_read(self):
        # HARM: per_page -1 is the server's "all of them"; any positive number
        # silently truncates the worklist, so a full-library scan quietly
        # covers only the first slice and the rest is reported as done.
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
        t = _ReadTransport(tag_pages=[_tag_rows(4, "alpha")])
        got = _read_stash(t).all_tags()
        self.assertEqual(len(got), 4)
        self.assertEqual(len(t.calls), 1)


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
