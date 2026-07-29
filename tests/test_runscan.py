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

from cronicled.adapters.base import SiteAdapter
from cronicled.jobs import COST_CLASS_LIMITS, JobRunner
from cronicled.scan import IDENTIFIED_BY_FINGERPRINT
from cronicled.runscan import (EVERY_FILE, build_producer,
                               build_scheduled_producer, configured_adapters,
                               main)
from cronicled.store import Store
from tests.fixtures.cast import CENSORSHIP

WAIT = 10
HOUR = 3600


class _Adapter(SiteAdapter):
    """Subclasses the real interface for `search_query` alone — the phrasing
    the per-title fallback goes through. A double with its own copy of it
    could agree with this file's assertions while disagreeing with every
    configured adapter in production."""

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

    def __init__(self, scenes, script=None, by_url=None, boxes=None,
                 by_fingerprint=None):
        self._scenes = list(scenes)
        self._script = dict(script or {})
        self._by_url = dict(by_url or {})
        # No box configured is the default and the ordinary state of a fresh
        # install: `Stash.stash_boxes` answers `[]` for one, and a scan then
        # asks nobody. The fake matches that limitation rather than
        # inventing a box, so every test in this file that is not about
        # fingerprints exercises exactly the path it did before.
        self._boxes = list(boxes or [])
        self._by_fingerprint = dict(by_fingerprint or {})
        self.calls = []

    def stash_boxes(self):
        self.calls.append(("stash_boxes",))
        return [dict(box) for box in self._boxes]

    def scrape_scenes_by_fingerprint(self, endpoint, scene_ids):
        self.calls.append(("scrape_scenes_by_fingerprint", endpoint,
                           list(scene_ids)))
        answers = self._by_fingerprint.get(endpoint, {})
        # One match list per requested scene, in the order requested -- the
        # real method REFUSES a reply of any other length, so a fake that
        # could return a shorter one would be offering a shape production
        # never passes on.
        return [list(answers.get(str(scene_id), [])) for scene_id in scene_ids]

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
            build_producer(_FakeStash([]), {"store": _Adapter()}, self.store,
                           limit=None)

    def test_omitting_the_argument_entirely_is_refused_too(self):
        # HARM: a signature that DEFAULTED `limit` to `None` would make this
        # call succeed silently, reachable by a caller who simply forgot the
        # flag — which is the shape of failure this whole guard exists for.
        with self.assertRaises(TypeError):
            build_producer(_FakeStash([]), {"store": _Adapter()}, self.store)

    def test_limit_zero_is_accepted_as_a_deliberate_instruction(self):
        # The permissive-looking value that is nonetheless a real, honoured
        # instruction — see `scan.select`'s own docstring for `limit=0`.
        producer = build_producer(_FakeStash([]), {"store": _Adapter()},
                                  self.store, limit=0)
        self.assertEqual(producer._limit, 0)

    def test_every_file_is_the_one_way_to_ask_for_no_limit(self):
        # The deliberate unbounded caller — an unattended pass over the whole
        # unorganized set. It reaches `select` as the same `None` the check
        # above refuses, and that is the point: the value is not the guard,
        # having to name it at the call site is. A caller who simply forgot
        # the argument still cannot get here.
        producer = build_producer(_FakeStash([]), {"store": _Adapter()},
                                  self.store, limit=EVERY_FILE)
        self.assertIsNone(producer._limit)


class TheScheduledScanIsNotTheManualOne(unittest.TestCase):
    """Two scans, and every difference between them is a way this wiring
    would otherwise fail without saying anything.

    `web.actions.Actions.scan` builds a producer per click and `reregister`s
    it, because a scan's limit can only be fixed at construction. `reregister`
    REPLACES whatever holds the name. So a scheduled scan that shared the
    name would be silently reconfigured by somebody typing 25 into the box —
    and the next unattended run, hours later with nobody watching, would scan
    25 files instead of the library.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.stash = _FakeStash([])
        self.adapters = {"store": _Adapter()}

    def _both(self, **kwargs):
        manual = build_producer(self.stash, self.adapters, self.store,
                                limit=25)
        scheduled = build_scheduled_producer(self.stash, self.adapters,
                                             self.store, **kwargs)
        return manual, scheduled

    def test_the_two_are_registered_under_different_names(self):
        # Asserted as a DIFFERENCE rather than against the name constant the
        # code itself uses: what matters is that one cannot replace the
        # other, and a test comparing the constant to itself would hold for
        # any rename including one back onto the manual scan's name.
        manual, scheduled = self._both()
        self.assertNotEqual(scheduled.name, manual.name)

    def test_the_scheduled_scan_is_built_with_no_file_limit(self):
        # The limit the producer was BUILT with, not "a scan ran": a
        # scheduled run that quietly inherited the manual scan's 25 would
        # start, finish, and record a run exactly as a whole-library pass
        # does.
        manual, scheduled = self._both()
        self.assertIsNone(scheduled._limit)
        self.assertEqual(manual._limit, 25)

    def test_the_scheduled_scan_declares_a_daily_cadence(self):
        # 86400 seconds spelled out, not read back from the constant the
        # producer was built from — that comparison holds whatever the
        # constant becomes, including a cadence of one second against a
        # rate-limited scraper.
        _manual, scheduled = self._both()
        self.assertEqual(scheduled.every, 86400)

    def test_the_manual_scan_declares_no_cadence_at_all(self):
        # The other side, and the reason the two are separate registrations:
        # `schedule.resolve` refuses an enabled producer with no cadence, so
        # a manual scan that declared one would be schedulable by accident
        # and one that is in the registry at start-up would take the whole
        # start-up down.
        manual, _scheduled = self._both()
        self.assertIsNone(manual.every)

    def test_the_cadence_is_overridable_but_has_no_permissive_default(self):
        _manual, scheduled = self._both(every=HOUR)
        self.assertEqual(scheduled.every, HOUR)

    def test_it_shares_the_scraping_cost_class_so_the_two_serialise(self):
        # Not a detail: an unattended scan and a manual one both drive the
        # media server's headless browser, and two at once thrash it. The
        # runner rations that class to one job, so sharing it is what makes
        # a nightly run and somebody's click take turns instead of colliding.
        manual, scheduled = self._both()
        self.assertEqual(scheduled.cost, manual.cost)
        self.assertEqual(COST_CLASS_LIMITS[scheduled.cost], 1)

    def test_it_is_wired_through_build_producer_not_around_it(self):
        # A scheduled scan assembled separately would be a second copy of the
        # wiring, free to lose the enrichment pass or a store, and every test
        # of `build_producer` above would go on passing while the unattended
        # run — the one nobody watches — searched fewer places.
        manual, scheduled = self._both()
        self.assertEqual([source.name for source in scheduled._sources],
                         [source.name for source in manual._sources])
        self.assertEqual(scheduled._enrich, manual._enrich)
        self.assertIs(scheduled._store, self.store)
        self.assertIsNotNone(scheduled._identify)


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

        producer = build_producer(stash, {"store": adapter}, self.store, limit=10)
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
            build_producer(stash, {"store": wrong_adapter}, self.store, limit=10))

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
            build_producer(stash, {"store": adapter}, self.store, limit=10))

        self.assertEqual(finished.recorded, 1)

    def test_a_clip_the_creators_page_missed_is_found_through_the_wiring(self):
        # The per-title fallback, end to end: the scraper answers the
        # creator with a page that does not carry this file — the shape a
        # store with more clips than fit one response returns — and answers
        # the file itself only when asked for it by title. Nothing but the
        # wiring supplies `title_query`, so a `build_producer` that stopped
        # binding it would refuse this file while every test that injects
        # its own `Source` stayed green.
        path = "/library/Velvet Crane/Morning Ritual.mp4"
        other = row("Harbour Lights", "https://example.invalid/clip/w")
        wanted = row("Morning Ritual", "https://example.invalid/clip/x")
        stash = _FakeStash(
            [scene(1, path)],
            script={("scraper-alpha", "Velvet Crane"): [other],
                    ("scraper-alpha", "Velvet Crane Morning Ritual"): [wanted]})
        adapter = _Adapter(scraper_id="scraper-alpha")

        finished = self._run_to_completion(
            build_producer(stash, {"store": adapter}, self.store, limit=10))

        self.assertEqual(finished.recorded, 1)
        self.assertIn(
            ("scrape_scenes_by_query", "scraper-alpha",
             "Velvet Crane Morning Ritual"),
            stash.calls)

    def test_the_adapters_own_query_phrasing_is_what_travels(self):
        # Bound off the adapter rather than rebuilt here: a store whose spec
        # says narrowing by the creator costs recall phrases its own query,
        # and a copy of the base phrasing made at this call site could not
        # honour that.
        adapter = _Adapter()
        producer = build_producer(_FakeStash([]), {"store": adapter},
                                  self.store, limit=10)
        self.assertEqual(producer._sources[0].title_query,
                         adapter.search_query)

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
            build_producer(stash, {"store": adapter}, self.store, limit=10))

        self.assertEqual(finished.state, "done")
        for call in stash.calls:
            self.assertIn(call[0], ("unorganized_scenes",
                                    "scrape_scenes_by_query",
                                    "scrape_scene_url",
                                    "stash_boxes",
                                    "scrape_scenes_by_fingerprint"))


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
        producer = build_producer(_FakeStash([]), {"store": adapter},
                                  self.store, limit=10)
        self.assertEqual(producer._sources[0].owner_of, adapter.owner_of)

    def test_a_non_catalog_resolvable_adapter_passes_none(self):
        adapter = _Adapter(catalog_resolvable=False)
        producer = build_producer(_FakeStash([]), {"store": adapter},
                                  self.store, limit=10)
        self.assertIsNone(producer._sources[0].owner_of)


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
        producer = build_producer(stash, {"store": _Adapter()}, self.store,
                                  limit=10)
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
            build_producer(stash, {"store": adapter}, self.store, limit=10))

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
            build_producer(stash, {"store": adapter}, self.store, limit=10))

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
            Stash=mock.DEFAULT,
            Store=mock.DEFAULT,
            JobRunner=mock.DEFAULT,
            build_producer=mock.DEFAULT,
        )

    def test_it_wires_server_adapters_producer_and_runner_together(self):
        with self._patched() as mocks:
            mocks["load_server"].return_value = {
                "url": "http://server.example.test", "api_key": "K"}
            configured = {"only": _Adapter(), "second": _Adapter(name="second")}
            mocks["configured_adapters"].return_value = configured
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

            rc = main(["--limit", "5", "--db", "irrelevant.sqlite3"])

            self.assertEqual(rc, 0)
            mocks["Stash"].assert_called_once_with(
                "http://server.example.test", "K")
            # Every configured adapter reaches `build_producer` -- there is
            # no `--adapter` flag left to single one out, and no `get_adapter`
            # call standing between `configured_adapters` and the producer.
            args, kwargs = mocks["build_producer"].call_args
            self.assertEqual(args[1], configured)
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


# `example.invalid` is reserved by RFC 2606 and can never resolve.
BOX = {"name": "north-box", "endpoint": "https://one.example.invalid/gql"}


def box_match(title, remote_site_id):
    return {"title": title, "code": None, "details": None, "director": None,
            "urls": [], "url": None, "date": None, "image": None,
            "studio": None, "tags": [], "performers": [],
            "remote_site_id": remote_site_id}


class BuildProducerFingerprintWiring(unittest.TestCase):
    """The stash-box half of the client reaches the `ScanProducer` it builds,
    as one run-wide collaborator -- no adapter-level gate and no per-store
    copy, because a box identifies a file by the file's own fingerprints and
    has nothing to do with which stores are configured.
    """

    PATH = "/library/Velvet Crane/Morning Ritual.mp4"

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _run_to_completion(self, producer):
        runner = JobRunner(self.store)
        runner.register(producer)
        job = runner.start(producer.name)
        self.assertTrue(runner.wait(job.id, WAIT))
        return runner.job(job.id)

    def test_an_install_with_no_box_asks_nobody_and_scans_exactly_as_before(self):
        candidate = row("Morning Ritual", "https://example.invalid/clip/x")
        stash = _FakeStash(
            [scene(1, self.PATH)],
            script={("scraper-alpha", "Velvet Crane"): [candidate]})

        self._run_to_completion(build_producer(
            stash, {"store": _Adapter(scraper_id="scraper-alpha")},
            self.store, limit=10))

        self.assertNotIn("scrape_scenes_by_fingerprint",
                         [call[0] for call in stash.calls])
        item = self.store.items(folder="library")[0]
        self.assertEqual(item["payload"]["score"], 1.0)

    def test_a_box_identified_file_is_recorded_and_never_searched_for(self):
        # The end-to-end property, run through the whole wiring path: a
        # composition that asked the boxes but then searched anyway -- or
        # that never asked at all -- would be caught here and nowhere else.
        match = box_match("Morning Ritual", "r-77")
        stash = _FakeStash(
            [scene(1, self.PATH)], boxes=[BOX],
            by_fingerprint={BOX["endpoint"]: {"1": [match]}})

        finished = self._run_to_completion(build_producer(
            stash, {"store": _Adapter(scraper_id="scraper-alpha")},
            self.store, limit=10))

        self.assertEqual(finished.recorded, 1)
        item = self.store.items(folder="library")[0]
        self.assertEqual(item["payload"]["identified_by"],
                         IDENTIFIED_BY_FINGERPRINT)
        self.assertEqual(item["payload"]["candidate"], match)
        self.assertIsNone(item["confidence"])
        self.assertNotIn("scrape_scenes_by_query",
                         [call[0] for call in stash.calls])

    def test_the_batch_reaches_the_box_as_the_scene_ids_that_were_selected(self):
        stash = _FakeStash([scene(1, self.PATH), scene(2, self.PATH)],
                           boxes=[BOX])

        self._run_to_completion(build_producer(
            stash, {"store": _Adapter(scraper_id="scraper-alpha")},
            self.store, limit=10))

        self.assertIn(("scrape_scenes_by_fingerprint", BOX["endpoint"],
                       ["1", "2"]), stash.calls)

    def test_the_boxes_are_read_at_scan_time_not_at_build_time(self):
        # A producer built once and run twice must not hold a stale list, and
        # a box added to the server between the two must be asked.
        stash = _FakeStash([], boxes=[BOX])
        build_producer(stash, {"store": _Adapter()}, self.store, limit=10)

        self.assertEqual(stash.calls, [])
