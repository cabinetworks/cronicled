import json
import unittest

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
