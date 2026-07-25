"""Adapters are data, not code: no store is special-cased in this repo. These
specs are invented, and exercise each owner-source mechanism."""
import json
import os
import tempfile
import unittest

from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.adapters.registry import load_adapters, get_adapter

# owner_segment counts from the host: for
#   https://example.test/store/velvetcrane/copper-kettle
# the segments are ["example.test", "store", "velvetcrane", "copper-kettle"],
# so the creator sits at index 2.
URL_SPEC = {"name": "urlsite", "display": "URL Site", "scraper_id": "UrlSite",
            "owner_source": "url_segment", "owner_segment": 2,
            "catalog_resolvable": True}
FIELD_SPEC = {"name": "fieldsite", "display": "Field Site", "scraper_id": "FieldSite",
              "owner_source": "result_field", "owner_field": ["studio", "name"],
              "catalog_resolvable": True}
NONE_SPEC = {"name": "nosite", "display": "No Site", "scraper_id": "NoSite",
             "owner_source": "none", "catalog_resolvable": False}


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


class Defaults(unittest.TestCase):
    def test_censorship_and_aliases_default_to_empty(self):
        a = DeclarativeAdapter(NONE_SPEC)
        self.assertEqual(a.censorship, {})
        self.assertEqual(a.aliases, {})

    def test_censorship_comes_from_the_spec_when_given(self):
        spec = dict(NONE_SPEC, censorship={"kestrel": ["k3strel"]})
        self.assertEqual(DeclarativeAdapter(spec).censorship,
                         {"kestrel": ["k3strel"]})


class ExampleConfig(unittest.TestCase):
    """The shipped example config must itself pass the spec validation added
    alongside it - rejecting the project's own example would be worse than
    not validating at all."""

    def test_example_config_loads_a_working_adapter(self):
        loaded = load_adapters(os.path.join("config", "adapters.example.json"))
        self.assertEqual(sorted(loaded), ["examplestore"])
        adapter = get_adapter(None, loaded)
        self.assertEqual(adapter.name, "examplestore")
        r = {"url": "https://example.test/store/velvetcrane/copper-kettle",
             "title": "Copper Kettle"}
        self.assertEqual(adapter.owner_of(r), "velvetcrane")


class Registry(unittest.TestCase):
    def _write(self, payload):
        d = tempfile.mkdtemp()
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

    def test_unknown_adapter_name_raises(self):
        p = self._write({"adapters": [URL_SPEC]})
        loaded = load_adapters(p)
        with self.assertRaises(KeyError):
            get_adapter("nosuchsite", loaded)

    def test_default_is_the_one_marked_default(self):
        p = self._write({"default": "fieldsite", "adapters": [URL_SPEC, FIELD_SPEC]})
        loaded = load_adapters(p)
        self.assertEqual(get_adapter(None, loaded).name, "fieldsite")

    def test_unknown_adapter_message_names_the_bad_name_and_whats_available(self):
        p = self._write({"adapters": [URL_SPEC, FIELD_SPEC]})
        loaded = load_adapters(p)
        try:
            get_adapter("nosuchsite", loaded)
            self.fail("expected KeyError")
        except KeyError as exc:
            msg = str(exc)
            self.assertIn("nosuchsite", msg)
            self.assertIn("fieldsite", msg)
            self.assertIn("urlsite", msg)

    def test_malformed_config_raises_rather_than_loading_silently(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "adapters.json")
        with open(p, "w") as fh:
            fh.write("{not valid json")
        with self.assertRaises(ValueError):
            load_adapters(p)

    def test_two_configs_with_overlapping_names_keep_their_own_default(self):
        # Regression: a module-level "current default" would let loading config B
        # silently change what get_adapter(None, ...) returns for config A's
        # mapping, even though the two configs share no state of their own.
        p_a = self._write({"default": "urlsite", "adapters": [URL_SPEC, FIELD_SPEC]})
        p_b = self._write({"default": "fieldsite", "adapters": [URL_SPEC, FIELD_SPEC]})
        loaded_a = load_adapters(p_a)
        loaded_b = load_adapters(p_b)
        self.assertEqual(get_adapter(None, loaded_a).name, "urlsite")
        self.assertEqual(get_adapter(None, loaded_b).name, "fieldsite")


if __name__ == "__main__":
    unittest.main()
