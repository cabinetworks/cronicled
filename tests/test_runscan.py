"""`cronicled.runscan` is the first thing that can actually run a library
scan: it wires a `Stash`, a configured `SiteAdapter` and a `Store` into a
`ScanProducer`, registers it with a `JobRunner`, and gives that a command
line.

No test here opens a socket or touches the real environment or filesystem
outside a temporary directory this file creates and cleans up itself. The
media client is a fake holding the same discipline `tests/test_scan.py`'s
`FakeStash` and `tests/test_search.py`'s `_SpyStash` do: one read to
enumerate the library, one read per query to the scraper, one read per
proposal to enrich the winning candidate's URL, and anything else raises —
this file is the first place a write introduced by the WIRING itself (as
opposed to `ScanProducer` or `catalog_search` alone) would show up, since it
is the first place all three are run together.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from cronicled.jobs import JobRunner
from cronicled.runscan import build_producer, configured_adapters, main
from cronicled.store import Store
from tests.fixtures.cast import CENSORSHIP

WAIT = 10


class _Adapter:
    def __init__(self, scraper_id="scraper-alpha", censorship=None, name="store",
                 catalog_resolvable=True):
        self.name = name
        self.scraper_id = scraper_id
        self.censorship = censorship or {}
        self.catalog_resolvable = catalog_resolvable

    def owner_of(self, result):
        # None of this file's scenes are ambiguous enough to trigger a
        # candidate check, so this is never actually called; it exists only
        # so `build_producer` can read `adapter.owner_of` off this fake the
        # same way it reads it off a real `SiteAdapter`.
        return (result or {}).get("owner", "")


def scene(scene_id, path):
    return {"id": str(scene_id), "title": None, "date": None,
            "files": [{"basename": path.rsplit("/", 1)[-1], "path": path}],
            "studio": None, "performers": [], "tags": []}


def row(title, url):
    return {"title": title, "url": url, "urls": [url], "code": None,
            "details": None, "director": None, "date": None, "image": None,
            "studio": None, "tags": [], "performers": []}


class _FakeStash:
    """Everything a scan wired through `build_producer` may touch: one read
    to enumerate the library (`unorganized_scenes`), one read per query to
    the configured scraper (`scrape_scenes_by_query`), and one read per
    proposal to enrich the winning candidate's own URL (`scrape_scene_url`).
    Anything else raises — the property this whole module exists to
    preserve.

    `by_url` scripts `scrape_scene_url`, keyed by the exact URL asked for;
    an unscripted URL answers `None` — an ordinary "nothing new for this
    one" miss, exactly `Stash.scrape_scene_url`'s own contract for a scraper
    that has nothing to add, so a test that never exercises enrichment does
    not have to script it to stay green.
    """

    def __init__(self, scenes, script=None, by_url=None):
        self._scenes = list(scenes)
        self._script = dict(script or {})
        self._by_url = dict(by_url or {})
        self.calls = []

    def unorganized_scenes(self, limit):
        self.calls.append(("unorganized_scenes", limit))
        scenes = self._scenes if limit is None else self._scenes[:limit]
        return len(self._scenes), list(scenes)

    def scrape_scenes_by_query(self, scraper_id, query):
        self.calls.append(("scrape_scenes_by_query", scraper_id, query))
        return list(self._script.get((scraper_id, query), []))

    def scrape_scene_url(self, url):
        self.calls.append(("scrape_scene_url", url))
        return self._by_url.get(url)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "a scan wired through cronicled.runscan called %r on the "
                "media server; it reads and looks things up, it never "
                "writes" % (name,))
        return refuse


class BuildProducerRequiresALimit(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_limit_none_is_refused(self):
        # HARM: `scan.select` treats `limit=None` as "take every survivor of
        # narrowing" — a first run against a whole library, reachable simply
        # by passing the same `None` a caller might use elsewhere to mean
        # "no filter".
        with self.assertRaises(ValueError):
            build_producer(_FakeStash([]), _Adapter(), self.store, limit=None)

    def test_omitting_the_argument_entirely_is_refused_too(self):
        # HARM: a signature that DEFAULTED `limit` to `None` would make this
        # call succeed silently, reachable by a caller who simply forgot the
        # flag — which is the shape of failure this whole guard exists for.
        with self.assertRaises(TypeError):
            build_producer(_FakeStash([]), _Adapter(), self.store)

    def test_limit_zero_is_accepted_as_a_deliberate_instruction(self):
        # The permissive-looking value that is nonetheless a real, honoured
        # instruction — see `scan.select`'s own docstring for `limit=0`.
        producer = build_producer(_FakeStash([]), _Adapter(), self.store, limit=0)
        self.assertEqual(producer._limit, 0)


class BuildProducerWiring(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _run_to_completion(self, producer):
        runner = JobRunner(self.store)
        runner.register(producer)
        job = runner.start(producer.name)
        self.assertTrue(runner.wait(job.id, WAIT))
        return runner.job(job.id)

    def test_it_asks_the_configured_scraper_and_records_a_match(self):
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        candidate = row("Morning Ritual", "https://example.invalid/clip/x")
        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "Velvet Crane"): [candidate]})
        adapter = _Adapter(scraper_id="scraper-alpha")

        producer = build_producer(stash, adapter, self.store, limit=10)
        finished = self._run_to_completion(producer)

        self.assertEqual(finished.state, "done")
        self.assertEqual(finished.recorded, 1)
        self.assertIn(
            ("scrape_scenes_by_query", "scraper-alpha", "Velvet Crane"),
            stash.calls)

    def test_the_wrong_scraper_id_would_have_found_nothing(self):
        # HARM: this is `catalog_search`'s own bindings test re-run through
        # the whole wiring path, because the wrong adapter's scraper_id could
        # in principle be introduced here instead — a mismatch between what
        # `build_producer` reads off `adapter` and what it hands to
        # `catalog_search`.
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        candidate = row("Morning Ritual", "https://example.invalid/clip/x")
        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "Velvet Crane"): [candidate]})
        wrong_adapter = _Adapter(scraper_id="scraper-beta")  # never scripted

        finished = self._run_to_completion(
            build_producer(stash, wrong_adapter, self.store, limit=10))

        self.assertEqual(finished.recorded, 0)
        self.assertIn(
            ("scrape_scenes_by_query", "scraper-beta", "Velvet Crane"),
            stash.calls)

    def test_the_censorship_map_reaches_both_the_query_and_the_scoring(self):
        # The creator's own folder name carries the censored canonical, so
        # this exercises both halves at once: the QUERY must be expanded
        # (search_variants) to ever reach the scripted candidate at all, and
        # the candidate's TITLE must be decensored (decensor) to score high
        # enough to be chosen.
        path = "/library/Kestrel Hollow/Kestrel Nightfall.mp4"
        censored = row("K3strel Nightfall", "https://example.invalid/clip/y")
        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "k3strel hollow"): [censored]})
        adapter = _Adapter(scraper_id="scraper-alpha", censorship=CENSORSHIP)

        finished = self._run_to_completion(
            build_producer(stash, adapter, self.store, limit=10))

        self.assertEqual(finished.recorded, 1)

    def test_a_scan_built_this_way_never_writes_to_the_media_server(self):
        # HARM: `ScanProducer` on its own already holds this property (see
        # `tests/test_scan.py`), but this is the first path that also runs
        # `catalog_search` against the same fake, so it is the first place a
        # write introduced by the composition itself — rather than by either
        # piece alone — would be caught.
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        candidate = row("Morning Ritual", "https://example.invalid/clip/x")
        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "Velvet Crane"): [candidate]})
        adapter = _Adapter(scraper_id="scraper-alpha")

        finished = self._run_to_completion(
            build_producer(stash, adapter, self.store, limit=10))

        self.assertEqual(finished.state, "done")
        for call in stash.calls:
            self.assertIn(call[0], ("unorganized_scenes",
                                    "scrape_scenes_by_query",
                                    "scrape_scene_url"))


class BuildProducerOwnerOfWiring(unittest.TestCase):
    """`adapter.owner_of` reaches the `ScanProducer` it builds, but only when
    `adapter.catalog_resolvable` says a name search can identify a creator on
    this store at all -- see `build_producer`'s docstring for the regression
    this guards: an adapter with no owner signal (`owner_source: "none"`)
    would otherwise find zero support for every candidate on every ambiguous
    file, unresolving what the old folder-wins default used to handle.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_a_catalog_resolvable_adapter_passes_its_owner_of_through(self):
        adapter = _Adapter(catalog_resolvable=True)
        producer = build_producer(_FakeStash([]), adapter, self.store, limit=10)
        self.assertEqual(producer._owner_of, adapter.owner_of)

    def test_a_non_catalog_resolvable_adapter_passes_none(self):
        adapter = _Adapter(catalog_resolvable=False)
        producer = build_producer(_FakeStash([]), adapter, self.store, limit=10)
        self.assertIsNone(producer._owner_of)


class BuildProducerEnrichmentWiring(unittest.TestCase):
    """`stash.scrape_scene_url` reaches the `ScanProducer` as `enrich`,
    unconditionally -- unlike `owner_of`, this needs no adapter-level gate,
    since it scrapes a URL directly rather than asking a per-adapter name
    search to identify anything.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _run_to_completion(self, producer):
        runner = JobRunner(self.store)
        runner.register(producer)
        job = runner.start(producer.name)
        self.assertTrue(runner.wait(job.id, WAIT))
        return runner.job(job.id)

    def test_the_stashs_own_scrape_by_url_method_is_wired_through(self):
        stash = _FakeStash([])
        producer = build_producer(stash, _Adapter(), self.store, limit=10)
        self.assertEqual(producer._enrich, stash.scrape_scene_url)

    def test_a_proposal_carries_the_fuller_scrape_of_its_own_url(self):
        # The end-to-end property `scan.ExamineEnrichmentTest` already pins
        # against `examine` directly: run here through the whole wiring
        # path, so a mismatch introduced by the COMPOSITION itself -- rather
        # than by `ScanProducer` or `Stash` alone -- would be caught.
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        url = "https://example.invalid/clip/morning-ritual"
        thin = row("Morning Ritual", url)
        fuller = dict(thin, date="2025-11-02",
                     studio={"stored_id": None, "name": "Amber Vale"},
                     performers=[{"stored_id": None, "name": "Wren Ashcombe"}])
        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "Velvet Crane"): [thin]},
            by_url={url: fuller})
        adapter = _Adapter(scraper_id="scraper-alpha")

        finished = self._run_to_completion(
            build_producer(stash, adapter, self.store, limit=10))

        self.assertEqual(finished.recorded, 1)
        item = self.store.items(folder="library")[0]
        self.assertEqual(item["payload"]["candidate"], fuller)
        self.assertIn(("scrape_scene_url", url), stash.calls)

    def test_a_failed_enrichment_still_records_the_thin_proposal(self):
        # HARM: refusing the row over a scrape failure would throw away a
        # title and a URL that are worth writing on their own, exactly as
        # `scan.ExamineEnrichmentTest` pins directly against `examine`.
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        url = "https://example.invalid/clip/morning-ritual"
        thin = row("Morning Ritual", url)

        def raising_scrape(_url):
            raise RuntimeError("connection reset")

        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "Velvet Crane"): [thin]})
        stash.scrape_scene_url = raising_scrape
        adapter = _Adapter(scraper_id="scraper-alpha")

        finished = self._run_to_completion(
            build_producer(stash, adapter, self.store, limit=10))

        self.assertEqual(finished.recorded, 1)
        item = self.store.items(folder="library")[0]
        self.assertEqual(item["payload"]["candidate"], thin)


class ConfiguredAdapters(unittest.TestCase):
    def test_raises_a_clear_error_when_none_are_configured(self):
        # HARM: `adapters.registry.load_adapters` returning `{}` is meant to
        # let the APP start with no config; letting that same empty mapping
        # reach `get_adapter`/`catalog_search` unchecked fails obscurely deep
        # inside a scan instead of at the one call site that actually needs
        # an adapter to exist.
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError) as ctx:
                configured_adapters(env={"CRONICLED_CONFIG_DIR": d})
        self.assertIn("no adapters are configured", str(ctx.exception))

    def test_returns_what_is_configured_when_something_is(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "adapters.json")
            with open(path, "w") as fh:
                json.dump({"adapters": [
                    {"name": "onlystore", "owner_source": "none",
                     "title_match_counts_as_ownership": True}]}, fh)
            adapters = configured_adapters(env={"CRONICLED_CONFIG_DIR": d})
        self.assertIn("onlystore", adapters)


class MainRequiresALimitFlag(unittest.TestCase):
    def test_omitting_limit_exits_before_touching_any_config(self):
        # argparse's own required-ness, so this fires before `load_server`
        # or `configured_adapters` are even called — no config, no server,
        # no adapter needs to exist for this refusal to happen.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--db", ":memory:"])


class MainOrchestration(unittest.TestCase):
    """`main` itself is exercised with everything it constructs replaced by
    a double: the config loaders, `Stash`, `Store`, `JobRunner` and
    `build_producer`. What is being pinned here is the ORDER and the
    ARGUMENTS `main` wires together, not the scan mechanics underneath —
    those belong to `BuildProducerWiring` above and to `tests/test_scan.py`.
    """

    def _patched(self):
        return mock.patch.multiple(
            "cronicled.runscan",
            load_server=mock.DEFAULT,
            configured_adapters=mock.DEFAULT,
            get_adapter=mock.DEFAULT,
            Stash=mock.DEFAULT,
            Store=mock.DEFAULT,
            JobRunner=mock.DEFAULT,
            build_producer=mock.DEFAULT,
        )

    def test_it_wires_server_adapter_producer_and_runner_together(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["configured_adapters"].return_value = {"only": _Adapter()}
            chosen_adapter = _Adapter(name="chosen")
            mocks["get_adapter"].return_value = chosen_adapter
            producer = mock.Mock()
            producer.name = "library-scan"
            mocks["build_producer"].return_value = producer
            runner = mocks["JobRunner"].return_value
            job = mock.Mock(id="job-1")
            runner.start.return_value = job
            runner.job.return_value = mock.Mock(
                id="job-1", state="done", message="finished: 1 proposed",
                error=None)
            store_instance = mocks["Store"].return_value

            rc = main(["--limit", "5", "--db", "irrelevant.sqlite3",
                      "--adapter", "chosen"])

            self.assertEqual(rc, 0)
            mocks["Stash"].assert_called_once_with(
                "http://server.example.test", "K")
            mocks["get_adapter"].assert_called_once_with(
                "chosen", {"only": mocks["configured_adapters"].return_value["only"]})
            _, kwargs = mocks["build_producer"].call_args
            self.assertEqual(kwargs["limit"], 5)
            runner.register.assert_called_once_with(producer)
            runner.start.assert_called_once_with("library-scan")
            runner.wait.assert_called_once_with("job-1")
            store_instance.close.assert_called_once()

    def test_a_failed_job_is_reported_and_exits_nonzero(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["configured_adapters"].return_value = {"only": _Adapter()}
            mocks["get_adapter"].return_value = _Adapter()
            producer = mock.Mock()
            producer.name = "library-scan"
            mocks["build_producer"].return_value = producer
            runner = mocks["JobRunner"].return_value
            runner.start.return_value = mock.Mock(id="job-1")
            runner.job.return_value = mock.Mock(
                id="job-1", state="failed", message="finished: 0 proposed",
                error="StashError: unreachable")

            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)

    def test_an_unconfigured_server_is_refused_before_anything_is_built(self):
        with self._patched() as mocks:
            mocks["load_server"].side_effect = ValueError(
                "missing media-server config: url, api_key")

            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)
            mocks["Stash"].assert_not_called()
            mocks["Store"].assert_not_called()
            mocks["build_producer"].assert_not_called()

    def test_no_adapters_configured_is_refused_before_anything_is_built(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            mocks["configured_adapters"].side_effect = RuntimeError(
                "no adapters are configured")

            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["--limit", "5"])

            self.assertEqual(rc, 1)
            mocks["Stash"].assert_not_called()
            mocks["Store"].assert_not_called()
            mocks["build_producer"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
