"""Adapters are data, not code: no store is special-cased in this repo. These
specs are invented, and exercise each owner-source mechanism."""
import json
import os
import shutil
import tempfile
import unittest

from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.adapters.registry import load_adapters

# owner_segment counts from the host: for
#   https://example.test/store/velvetcrane/copper-kettle
# the segments are ["example.test", "store", "velvetcrane", "copper-kettle"],
# so the creator sits at index 2.
URL_SPEC = {"name": "urlsite", "display": "URL Site", "scraper_id": "UrlSite",
            "owner_source": "url_segment", "owner_segment": 2,
            "catalog_resolvable": True,
            "title_match_counts_as_ownership": True}
FIELD_SPEC = {"name": "fieldsite", "display": "Field Site", "scraper_id": "FieldSite",
              "owner_source": "result_field", "owner_field": ["studio", "name"],
              "catalog_resolvable": True,
              "title_match_counts_as_ownership": True}
NONE_SPEC = {"name": "nosite", "display": "No Site", "scraper_id": "NoSite",
             "owner_source": "none", "catalog_resolvable": False,
             "title_match_counts_as_ownership": True}
# The store the bug was found on: no trustworthy owner signal, and a title or
# URL-slug mention of a creator is not evidence they own the clip -- it is
# just as likely to be a fan edit or a collaboration clip sold by somebody
# else. `owner_source` is still "url_segment" here (rather than "none") on
# purpose, to prove the two checks are independent: the owner-segment match
# must keep working while the weaker title/slug inference is switched off.
NO_TITLE_EVIDENCE_SPEC = {
    "name": "noevidencesite", "display": "No Evidence Site",
    "scraper_id": "NoEvidenceSite",
    "owner_source": "url_segment", "owner_segment": 2,
    "catalog_resolvable": True,
    "title_match_counts_as_ownership": False}


class OwnerFromUrlSegment(unittest.TestCase):
    def setUp(self):
        self.a = DeclarativeAdapter(URL_SPEC)

    def test_reads_the_owner_from_the_configured_segment(self):
        r = {"url": "https://example.test/store/velvetcrane/copper-kettle"}
        self.assertEqual(self.a.owner_of(r), "velvetcrane")

    def test_title_slug_is_the_last_path_segment(self):
        self.assertEqual(
            self.a.url_title_slug("https://example.test/store/velvetcrane/copper-kettle/"),
            "copper-kettle")

    def test_title_slug_strips_a_query_string(self):
        self.assertEqual(
            self.a.url_title_slug("https://example.test/store/vc/copper-kettle?ref=1"),
            "copper-kettle")

    def test_title_slug_strips_a_fragment(self):
        self.assertEqual(
            self.a.url_title_slug("https://example.test/store/vc/copper-kettle#part2"),
            "copper-kettle")

    def test_own_store_clip_features_the_artist(self):
        r = {"url": "https://example.test/store/velvetcrane/copper-kettle",
             "title": "Copper Kettle"}
        self.assertTrue(self.a.clip_features_artist(r, "velvetcrane"))

    def test_owner_leg_is_a_prefix_match_not_equality(self):
        # a store's own account handle is often the performer slug plus a
        # house suffix ("velvetcraneofficial"); title/url-slug carry no
        # mention of the artist, so this can only pass via the owner leg
        r = {"url": "https://example.test/store/velvetcraneofficial/some-clip",
             "title": "Some Clip"}
        self.assertTrue(self.a.clip_features_artist(r, "velvetcrane"))

    def test_another_store_clip_naming_the_artist_also_counts(self):
        r = {"url": "https://example.test/store/marbleaux/velvet-crane-guest",
             "title": "Velvet Crane guests"}
        self.assertTrue(self.a.clip_features_artist(r, "velvetcrane"))

    def test_unrelated_clip_does_not(self):
        r = {"url": "https://example.test/store/marbleaux/harbour-lights",
             "title": "Harbour Lights"}
        self.assertFalse(self.a.clip_features_artist(r, "velvetcrane"))


class OwnerFromResultField(unittest.TestCase):
    def setUp(self):
        self.a = DeclarativeAdapter(FIELD_SPEC)

    def test_reads_the_owner_from_the_nested_field(self):
        r = {"studio": {"name": "Velvet Crane"}, "url": "https://example.test/1/2/x"}
        self.assertEqual(self.a.owner_of(r), "Velvet Crane")

    def test_strips_html_leaked_into_the_field(self):
        r = {"studio": {"name": "<em>Velvet</em> Crane"}}
        self.assertEqual(self.a.owner_of(r), "Velvet Crane")

    def test_url_carries_no_owner(self):
        self.assertEqual(self.a.artist_from_url("https://example.test/1/2/x"), "")


class TitleMatchDoesNotCountAsOwnership(unittest.TestCase):
    """The bug this ticket fixes: a store whose spec says a title or
    URL-slug mention proves nothing must not admit a cross-store clip on
    that mention alone -- while the store's own (stronger) owner-segment
    attribution must keep working exactly as it does when the flag is on."""

    def setUp(self):
        self.a = DeclarativeAdapter(NO_TITLE_EVIDENCE_SPEC)

    def test_the_stores_own_owner_segment_match_still_counts(self):
        r = {"url": "https://example.test/store/velvetcrane/copper-kettle",
             "title": "Copper Kettle"}
        self.assertTrue(self.a.clip_features_artist(r, "velvetcrane"))

    def test_the_owner_prefix_leg_still_counts(self):
        r = {"url": "https://example.test/store/velvetcraneofficial/some-clip",
             "title": "Some Clip"}
        self.assertTrue(self.a.clip_features_artist(r, "velvetcrane"))

    def test_a_bare_title_mention_from_another_store_does_not_count(self):
        # same fixture as OwnerFromUrlSegment's
        # test_another_store_clip_naming_the_artist_also_counts, which
        # proves the opposite answer when title_match_counts_as_ownership
        # is True -- the flag, not the fixture, is what is under test
        r = {"url": "https://example.test/store/marbleaux/velvet-crane-guest",
             "title": "Velvet Crane guests"}
        self.assertFalse(self.a.clip_features_artist(r, "velvetcrane"))

    def test_a_bare_url_slug_mention_does_not_count_either(self):
        r = {"url": "https://example.test/store/marbleaux/velvetcrane-guest",
             "title": "unrelated title"}
        self.assertFalse(self.a.clip_features_artist(r, "velvetcrane"))


class SearchOmitsSeed(unittest.TestCase):
    def test_default_search_query_includes_the_seed(self):
        a = DeclarativeAdapter(NONE_SPEC)
        self.assertEqual(a.search_query("velvetcrane", "copper kettle"),
                         "velvetcrane copper kettle")

    def test_search_omits_seed_drops_it(self):
        spec = dict(NONE_SPEC, name="omitseedsite", search_omits_seed=True)
        a = DeclarativeAdapter(spec)
        self.assertEqual(a.search_query("velvetcrane", "copper kettle"),
                         "copper kettle")


class SearchQueryCaseFolding(unittest.TestCase):
    """The outbound query used to be plain concatenation of whatever case the
    seed and the filename-as-title happened to carry, while the scorer's own
    equality test (`cronicled.text.normalize`) folds case before judging two
    strings the same -- so a query and the comparison it will eventually be
    judged against could disagree on nothing but case. Folding case here
    costs nothing (search is case-insensitive essentially everywhere) and
    closes that one gap.

    Punctuation stripping and letter-folding are a DIFFERENT question -- see
    `SiteAdapter.search_query`'s docstring -- and the tests below pin that
    boundary too, so a later change that reaches for `normalize` wholesale
    has to touch a documented decision rather than slide past a silent one.
    """

    def test_the_whole_query_is_case_folded(self):
        a = DeclarativeAdapter(NONE_SPEC)
        self.assertEqual(a.search_query("VelvetCrane", "Copper KETTLE"),
                         "velvetcrane copper kettle")

    def test_case_folding_survives_search_omits_seed(self):
        spec = dict(NONE_SPEC, name="omitseedsite", search_omits_seed=True)
        a = DeclarativeAdapter(spec)
        self.assertEqual(a.search_query("VelvetCrane", "Copper KETTLE"),
                         "copper kettle")

    def test_punctuation_is_left_exactly_alone(self):
        # Deliberately NOT stripped: a punctuation mark may be the one token
        # distinguishing this title from a wrong one, and this project does
        # not guess that unmeasured -- see the module docstring.
        a = DeclarativeAdapter(NONE_SPEC)
        self.assertEqual(a.search_query("velvetcrane", "Copper Kettle: Part Two!"),
                         "velvetcrane copper kettle: part two!")

    def test_accents_are_left_exactly_alone(self):
        # Deliberately NOT folded: a store that does not fold accents itself
        # would be sent a query it cannot match if this folded them away.
        a = DeclarativeAdapter(NONE_SPEC)
        self.assertEqual(a.search_query("velvetcrane", "Séance"),
                         "velvetcrane séance")


class NoOwnerAnywhere(unittest.TestCase):
    def setUp(self):
        self.a = DeclarativeAdapter(NONE_SPEC)

    def test_owner_is_empty(self):
        self.assertEqual(self.a.owner_of({"url": "https://example.test/x",
                                          "studio": {"name": "Velvet Crane"}}), "")

    def test_catalog_is_not_resolvable(self):
        self.assertFalse(self.a.catalog_resolvable)

    def test_search_query_defaults_to_seed_plus_title(self):
        self.assertEqual(self.a.search_query("velvetcrane", "copper kettle"),
                         "velvetcrane copper kettle")

    def test_clip_features_artist_is_false_when_the_slug_is_empty(self):
        r = {"url": "https://example.test/x", "title": "anything"}
        self.assertFalse(self.a.clip_features_artist(r, ""))


class InvalidSpec(unittest.TestCase):
    """A mistyped config must fail loudly: an empty owner falls through to
    title-substring matching in clip_features_artist, so a bad owner_source or
    owner_segment would otherwise produce plausible wrong matches instead of an
    error."""

    def test_unrecognised_owner_source_raises(self):
        spec = dict(NONE_SPEC, name="badsource", owner_source="nonsense")
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("badsource", str(ctx.exception))

    def test_url_segment_without_a_segment_raises(self):
        spec = dict(URL_SPEC, name="nosegment", owner_source="url_segment")
        del spec["owner_segment"]
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("nosegment", str(ctx.exception))

    def test_negative_owner_segment_raises(self):
        spec = dict(URL_SPEC, name="negsegment", owner_segment=-1)
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("negsegment", str(ctx.exception))

    def test_result_field_without_an_owner_field_raises(self):
        spec = dict(FIELD_SPEC, name="nofield")
        del spec["owner_field"]
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("nofield", str(ctx.exception))

    def test_missing_title_match_counts_as_ownership_raises(self):
        # the safe default for "an adapter that cannot state its ownership
        # rule" is not knowing, not the permissive reading -- see
        # DeclarativeAdapter's module docstring
        spec = dict(NONE_SPEC, name="notitleflag")
        del spec["title_match_counts_as_ownership"]
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("notitleflag", str(ctx.exception))
        self.assertIn("title_match_counts_as_ownership", str(ctx.exception))


class OwnerSegmentExampleValidatesAtLoadTime(unittest.TestCase):
    """`owner_segment` counts from after the scheme and includes the host --
    one off from what a reader used to URL *paths* would assume, and that
    exact mismatch has already caused a real misconfiguration. An optional
    `owner_segment_example` lets that be caught at load time instead of only
    documented and hoped for."""

    URL = "https://example.test/store/velvetcrane/copper-kettle"

    def test_a_matching_example_loads_without_complaint(self):
        spec = dict(URL_SPEC, name="matches",
                    owner_segment_example={"url": self.URL, "owner": "velvetcrane"})
        DeclarativeAdapter(spec)   # must not raise

    def test_an_off_by_one_segment_is_refused_rather_than_silently_wrong(self):
        # segment 1 on this URL is "store", not the creator -- the exact
        # shape of the off-by-one this field exists to catch.
        spec = dict(URL_SPEC, name="offbyone", owner_segment=1,
                    owner_segment_example={"url": self.URL, "owner": "velvetcrane"})
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        message = str(ctx.exception)
        self.assertIn("offbyone", message)
        self.assertIn("velvetcrane", message)
        self.assertIn("store", message)

    def test_no_example_given_loads_exactly_as_before(self):
        spec = dict(URL_SPEC, name="noexample")
        DeclarativeAdapter(spec)   # must not raise; the field is optional

    def test_the_example_is_ignored_for_a_non_url_segment_adapter(self):
        # owner_segment_example only means anything for url_segment; giving
        # one alongside a different owner_source is not a contradiction this
        # class is responsible for catching.
        spec = dict(FIELD_SPEC, name="fieldwithexample",
                    owner_segment_example={"url": self.URL, "owner": "wrong"})
        DeclarativeAdapter(spec)   # must not raise


class Defaults(unittest.TestCase):
    def test_censorship_and_aliases_default_to_empty(self):
        a = DeclarativeAdapter(NONE_SPEC)
        self.assertEqual(a.censorship, {})
        self.assertEqual(a.aliases, {})

    def test_censorship_comes_from_the_spec_when_given(self):
        spec = dict(NONE_SPEC, censorship={"kestrel": ["k3strel"]})
        self.assertEqual(DeclarativeAdapter(spec).censorship,
                         {"kestrel": ["k3strel"]})

    def test_aliases_come_from_the_spec_when_given(self):
        # The WHOLE map, not a sampled key: an entry silently dropped on the
        # way in is an alias an operator wrote and no scan will ever apply.
        spec = dict(NONE_SPEC,
                    aliases={"vcrane": "Velvet Crane",
                             "i m k": "Ivy May Kingsley"})
        self.assertEqual(DeclarativeAdapter(spec).aliases,
                         {"vcrane": "Velvet Crane",
                          "i m k": "Ivy May Kingsley"})


class AnAliasMapIsCheckedWhereItWasWritten(unittest.TestCase):
    """`adapters.json` is where an alias is typed, so it is where a mistake in
    one is reported.

    Every refusal here already existed in `cronicled.artist.Aliases`, which a
    scan builds when it starts. What moves is WHEN: the entry point names the
    config file it could not load (see `cronicled/__main__.py`), so an
    operator who mistypes an alias is told about the file they just edited
    rather than about the scan they started afterwards.
    """

    def test_a_censorship_entry_filed_under_aliases_is_refused_by_name(self):
        # HARM: the two maps do different jobs and their names do not say
        # which is which. An operator whose file says one thing and whose
        # store says another reaches for "aliases" -- observed -- and a title
        # substitution left there does nothing at all, forever, while reading
        # as configured. A list value can only be a censorship entry, so this
        # one is diagnosable rather than guessed at.
        spec = dict(NONE_SPEC, aliases={"kettle": ["k3ttle", "k-ettle"]})
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        message = str(ctx.exception)
        self.assertIn("nosite", message)        # which adapter
        self.assertIn("kettle", message)        # which entry
        self.assertIn("censorship", message)    # and the map it belongs in

    def test_a_string_value_is_taken_at_its_word(self):
        # The other side of that guard, and the reason it is drawn on the
        # value's SHAPE. A short or unusual name is the operator's to
        # declare, and nothing here can tell one from a title word without
        # guessing -- a guard that refused it would refuse somebody's name.
        spec = dict(NONE_SPEC, aliases={"kettle": "Copper"})
        self.assertEqual(DeclarativeAdapter(spec).aliases, {"kettle": "Copper"})

    def test_two_keys_that_normalise_alike_are_refused_at_load(self):
        # HARM: "vcrane" and "v crane" are one lookup, and whichever the dict
        # yielded first would decide who a file is attributed to.
        spec = dict(NONE_SPEC, aliases={"vcrane": "Velvet Crane",
                                        "v crane": "Vera Crane"})
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("nosite", str(ctx.exception))

    def test_a_value_that_is_not_a_name_is_refused_at_load(self):
        spec = dict(NONE_SPEC, aliases={"vcrane": ""})
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("nosite", str(ctx.exception))

    def test_a_key_that_normalises_to_nothing_is_refused_at_load(self):
        spec = dict(NONE_SPEC, aliases={"  ": "Velvet Crane"})
        with self.assertRaises(ValueError) as ctx:
            DeclarativeAdapter(spec)
        self.assertIn("nosite", str(ctx.exception))

    def test_a_malformed_map_stops_the_whole_config_loading(self):
        # Through the loader, not the adapter class: that is the path the
        # entry point takes, and the one that reports the file's name.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "adapters.json")
        spec = dict(NONE_SPEC, aliases={"vcrane": "Velvet Crane",
                                        "V Crane": "Velvet Crane"})
        with open(path, "w") as fh:
            json.dump({"adapters": [spec]}, fh)
        with self.assertRaises(ValueError):
            load_adapters(path)


class ExampleConfig(unittest.TestCase):
    """The shipped example config must itself pass the spec validation added
    alongside it - rejecting the project's own example would be worse than
    not validating at all.

    It also has to keep covering every `owner_source` mechanism: the written
    documentation (docs/adapters.md) describes all three, and a reader working
    from the example file alone should be able to find every one of them
    there too, not just the one the config happened to ship with first.
    """

    @classmethod
    def setUpClass(cls):
        cls.loaded = load_adapters(os.path.join("config", "adapters.example.json"))

    def test_example_config_loads_a_working_url_segment_adapter(self):
        adapter = self.loaded["examplestore"]
        self.assertEqual(adapter.name, "examplestore")
        r = {"url": "https://example.test/store/velvetcrane/copper-kettle",
             "title": "Copper Kettle"}
        self.assertEqual(adapter.owner_of(r), "velvetcrane")

    def test_every_owner_source_mechanism_is_represented(self):
        sources = {a.owner_source for a in self.loaded.values()}
        self.assertEqual(sources, {"url_segment", "result_field", "none"},
                         "config/adapters.example.json should carry one "
                         "adapter per owner_source mechanism, so a reader "
                         "working from the example alone can find all three "
                         "documented in docs/adapters.md")

    def test_the_result_field_example_reads_a_nested_studio_name(self):
        by_source = {a.owner_source: a for a in self.loaded.values()}
        adapter = by_source["result_field"]
        r = {"studio": {"name": "Velvet Crane"}}
        self.assertEqual(adapter.owner_of(r), "Velvet Crane")

    def test_the_none_example_is_marked_not_catalog_resolvable(self):
        by_source = {a.owner_source: a for a in self.loaded.values()}
        adapter = by_source["none"]
        self.assertFalse(adapter.catalog_resolvable)


class Registry(unittest.TestCase):
    def _write(self, payload):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "adapters.json")
        with open(p, "w") as fh:
            json.dump(payload, fh)
        return p

    def test_loads_every_adapter_in_the_config(self):
        p = self._write({"adapters": [URL_SPEC, FIELD_SPEC]})
        loaded = load_adapters(p)
        self.assertEqual(sorted(loaded), ["fieldsite", "urlsite"])

    def test_missing_config_yields_no_adapters_rather_than_an_error(self):
        # a fresh install has no config yet; the app must still start
        self.assertEqual(load_adapters("/nonexistent/adapters.json"), {})

    def test_malformed_config_raises_rather_than_loading_silently(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "adapters.json")
        with open(p, "w") as fh:
            fh.write("{not valid json")
        with self.assertRaises(ValueError):
            load_adapters(p)

    def test_a_default_key_is_refused_at_load_time(self):
        """`--adapter` is gone and every configured adapter is searched on
        every scan, so `"default"` has nothing left to mean. A config
        written before this ticket that still sets it must fail loudly, at
        load time, naming the key -- not be silently ignored, which is
        exactly the "key that stops mattering but stays accepted" drift
        this refusal exists to prevent."""
        p = self._write({"default": "fieldsite", "adapters": [URL_SPEC, FIELD_SPEC]})
        with self.assertRaises(ValueError) as ctx:
            load_adapters(p)
        self.assertIn("default", str(ctx.exception))

    def test_a_config_with_no_default_key_at_all_loads_normally(self):
        """The permissive side of the same guard: a config that never
        mentioned `"default"` must not be caught by a check aimed only at
        one that does."""
        p = self._write({"adapters": [URL_SPEC, FIELD_SPEC]})
        loaded = load_adapters(p)
        self.assertEqual(sorted(loaded), ["fieldsite", "urlsite"])


if __name__ == "__main__":
    unittest.main()
