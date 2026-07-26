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
