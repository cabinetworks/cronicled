"""The documentation makes claims that go stale silently.

Three of them are pinned here, because each has already drifted or is one
edit away from drifting, and none of them fails anything when it does:

* the module map is drawn twice - once in the README as the overview's one
  diagram, once on the architecture page that owns the reference material.
  Two hand-maintained copies of the same picture is exactly the duplication
  the restructure was meant to remove, so they are held byte-identical here
  instead of by good intentions.
* the self-check transcript quotes a module count. It said 12 while the
  program printed 16.
* the site publishes from the default branch and nowhere else, after the
  leak guard has run. That used to be a gate deciding whether a Cloudflare
  deploy could go ahead without its secrets; GitHub Pages needs no secret, so
  what is left to protect is that a pull request cannot publish and that
  nothing reaches the internet ahead of the guard.

None of this needs a YAML parser or a Markdown parser, and deliberately so -
the suite runs on a bare interpreter with nothing installed.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest

from cronicled import selfcheck
from cronicled.adapters.registry import load_adapters

README = "README.md"
INDEX = os.path.join("docs", "index.md")
ADAPTERS_DOC = os.path.join("docs", "adapters.md")
ADAPTERS_EXAMPLE = os.path.join("config", "adapters.example.json")
CI_YML = os.path.join(".github", "workflows", "ci.yml")

_FENCE_RE = re.compile(r"^```mermaid\n(.*?)^```$", re.S | re.M)
# Adapter examples sit inside a markdown bullet list, so their fences are
# indented a couple of spaces rather than starting at column 0 - unlike the
# mermaid fences above, which are always top-level.
_JSON_FENCE_RE = re.compile(r"^[ \t]*```json\n(.*?)^[ \t]*```$", re.S | re.M)
_COUNT_RE = re.compile(r"selfcheck ready \((\d+) modules imported\)")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fences(path):
    return _FENCE_RE.findall(_read(path))


def _doc_paths():
    paths = [README]
    for name in sorted(os.listdir("docs")):
        if name.endswith(".md"):
            paths.append(os.path.join("docs", name))
    return paths


class TheModuleMapIsDrawnOnce(unittest.TestCase):
    def test_the_readme_carries_exactly_one_diagram(self):
        # The README is the overview: the other three diagrams belong to the
        # architecture page, and moving a second one up here would rebuild
        # the duplication this split exists to end.
        self.assertEqual(
            len(_fences(README)), 1,
            "the README should carry the module map and nothing else; the "
            "matching path, the job lifecycle and the planned service live "
            "on docs/index.md")

    def test_the_architecture_page_carries_all_four(self):
        self.assertEqual(
            len(_fences(INDEX)), 4,
            "docs/index.md is where all four diagrams live: the module map, "
            "the matching path, the job lifecycle, and the planned service")

    def test_both_copies_of_the_module_map_are_identical(self):
        readme_map = _fences(README)[0]
        index_fences = _fences(INDEX)
        self.assertIn(
            readme_map, index_fences,
            "the module map in the README and the one on docs/index.md have "
            "drifted apart. They are the same picture; edit one and copy it "
            "to the other, or the two documents start describing different "
            "packages.")


class ThePlannedServiceIsMarkedAsPlanned(unittest.TestCase):
    """A reader believes a picture before they believe a paragraph. The
    prose is careful that no scheduler, inbox or entry point exists; a
    diagram that quietly drew them as ordinary boxes would undo that in one
    image. The marking is in the node TEXT, not only in a stroke style, so
    it survives a reader who cannot see the dashes."""

    def _planned_diagram(self):
        for fence in _fences(INDEX):
            if "PLANNED" in fence:
                return fence
        self.fail("no diagram on docs/index.md marks any node PLANNED")

    def test_the_planned_service_has_its_own_diagram(self):
        planned = [f for f in _fences(INDEX) if "PLANNED" in f]
        self.assertEqual(
            len(planned), 1,
            "the unbuilt service belongs in exactly one diagram of its own: "
            "spreading planned nodes across the diagrams of what is built is "
            "how a picture starts claiming more than the prose does")

    def test_every_node_in_it_is_labelled(self):
        fence = self._planned_diagram()
        # Every node declaration in that diagram - `id["label"]` or
        # `id{"label"}` - must say PLANNED or BUILT in its own label. A node
        # whose only marking is a classDef stroke is not marked at all to
        # anyone reading a stripped-down render.
        labels = re.findall(r'^\s*\w+[\[{]"(.*?)"[\]}]', fence, re.M)
        self.assertGreater(len(labels), 1, "expected several labelled nodes")
        for label in labels:
            self.assertRegex(
                label, r"^(PLANNED|BUILT):",
                "every node in the planned-service diagram must open with "
                "PLANNED: or BUILT:, so which is which cannot be mistaken")

    def test_the_diagrams_of_what_exists_claim_nothing_planned(self):
        # A diagram is believed before a paragraph is, so a picture of the
        # whole intended system would undo what the prose is careful about.
        # These are the things that genuinely do not exist.
        #
        # "scheduler" used to be on this list and is not any more, because
        # `cronicled/schedule.py` now exists. That move is the maintenance
        # this test is for: a planned thing becoming built should require
        # someone to change an assertion on purpose, rather than a diagram
        # drifting into claiming it. Note what stayed true through that
        # change - nothing constructs a Scheduler, so the entry point is
        # still drawn as planned even though the scheduler beside it is not.
        built_diagrams = [f for f in _fences(INDEX) if "PLANNED" not in f]
        self.assertEqual(len(built_diagrams), 3)
        for fence in built_diagrams:
            self.assertNotIn("inbox", fence.lower())
            self.assertNotIn("approval", fence.lower())


class NothingCallsTheSchedulerUnattended(unittest.TestCase):
    """The claim this class protects narrowed rather than disappeared.

    `cronicled/__main__.py` now exists — `python -m cronicled` serves the
    inbox — so "no entry point that starts any of this" is no longer true,
    and the fourth diagram now draws the entry point, the inbox and the
    approval gate as BUILT rather than PLANNED. What survives, and what this
    class now exists to protect, is the narrower half: nothing constructs a
    `Scheduler`, so a scan is never run without a person asking for one.

    The honesty test on `ThePlannedServiceIsMarkedAsPlanned` above covers the
    diagram's labelling mechanism. This is the entry point's counterpart: a
    claim about what the CODE does, which no diagram assertion can make.
    """

    def test_no_module_in_the_package_constructs_a_scheduler(self):
        offenders = []
        for root, _dirs, names in os.walk("cronicled"):
            if "__pycache__" in root:
                continue
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                if "Scheduler(" in _read(path):
                    offenders.append(path)
        self.assertEqual(
            offenders, [],
            "%s constructs a Scheduler. If this project now runs producers "
            "unattended, the README's 'nothing constructs a scheduler' claim "
            "and the fourth diagram's one remaining PLANNED node are both "
            "wrong and need changing with it." % ", ".join(offenders))

    def test_the_entry_point_exists_and_declares_no_console_script(self):
        # `cronicled/__main__.py` is what makes `python -m cronicled`
        # runnable; a `[project.scripts]` entry would be a SECOND, different
        # way to start it, and this project has only ever documented the one.
        self.assertNotIn(
            "[project.scripts]", _read("pyproject.toml"),
            "pyproject declares a console script - a second, undocumented "
            "way to start this project")
        self.assertTrue(
            os.path.exists(os.path.join("cronicled", "__main__.py")),
            "cronicled/__main__.py is missing - the README documents "
            "`python -m cronicled` as this project's entry point")

    def test_the_readme_states_the_narrowed_claim(self):
        # Both halves asserted, and separately, because only one of them is
        # still true. Collapsed to whitespace so a wrapped line cannot decide
        # the result.
        prose = " ".join(_read(README).split())
        self.assertIn("nothing constructs a scheduler", prose,
                      "the narrower claim that survives must still be made")
        self.assertIn("python -m cronicled", prose,
                      "the README must document the entry point that now "
                      "exists, not continue to claim nothing starts this "
                      "project")


class TheSelfCheckTranscriptIsCurrent(unittest.TestCase):
    """The README quoted `12 modules imported` while the program printed 16.

    Keeping the number literal is the choice here, rather than eliding it to
    a placeholder: the count is the one part of that line worth reading, and
    a number a test pins cannot go stale the way an unwatched one does. The
    cost is that adding a module now also edits a doc page - which is the
    point, not a side effect."""

    def test_every_quoted_count_matches_what_the_program_prints(self):
        actual = len(selfcheck.run())
        found = 0
        for path in _doc_paths():
            for quoted in _COUNT_RE.findall(_read(path)):
                found += 1
                self.assertEqual(
                    int(quoted), actual,
                    "%s quotes '%s modules imported' but the self-check "
                    "prints %d. A module was added or removed; update the "
                    "transcript." % (path, quoted, actual))
        self.assertGreater(
            found, 0,
            "no page quotes the self-check's output any more. If that line "
            "was removed on purpose, remove this test with it - leaving it "
            "passing vacuously is worse than either.")


class TheDocsBuildRunsOnEveryCommit(unittest.TestCase):
    def setUp(self):
        self.ci = _read(CI_YML)

    def test_the_site_is_built_from_the_docs_dependency_group(self):
        self.assertRegex(
            self.ci, r"uv run --frozen --group docs mkdocs build --strict",
            "the site must build from the `docs` dependency group (so the "
            "project's runtime dependencies stay exactly Jinja2, nothing "
            "more) and with --strict (so a dead internal link fails here "
            "rather than shipping)")

    def test_both_site_jobs_run_only_after_the_guard(self):
        # Nothing reaches the open internet without ./scripts/check_leaks
        # having passed on the same commit. This is the reason a Cloudflare
        # Pages Git integration is not used: it would build outside this
        # workflow entirely.
        for job in ("docs", "docs-deploy"):
            m = re.search(r"^  %s:\n((?:    .*\n|\n)*)" % job, self.ci, re.M)
            self.assertIsNotNone(m, "no `%s` job in %s" % (job, CI_YML))
            needs = re.search(r"^    needs:\s*(.+)$", m.group(1), re.M)
            self.assertIsNotNone(needs, "`%s` declares no `needs`" % job)
            self.assertIn(
                "guard", needs.group(1),
                "`%s` must declare needs: guard" % job)


class TheDeployGoesOnlyToTheDefaultBranch(unittest.TestCase):
    """The site publishes from `main` and from nowhere else, after the guard.

    This replaced a gate that decided whether a Cloudflare deploy could run,
    given two repository secrets that might be absent. GitHub Pages needs no
    secret - it authenticates with the workflow's own OIDC token - so there is
    nothing to be absent, and the skip-with-a-notice mechanism went with it.
    What is left to protect is narrower and more important: that nothing
    reaches the open internet without the leak guard having run, and that a
    pull request cannot publish.

    Note what a fork pull request gets now. Previously it was skipped with an
    explaining notice, because it could not read a secret. Now it simply does
    not match the `if`, like any other pull request. The site is still BUILT
    for it by the `docs` job, which is the check that catches a broken page;
    what it does not get is a preview URL, because GitHub Pages has one site
    per repository and no concept of one."""

    @classmethod
    def setUpClass(cls):
        cls.ci = _read(CI_YML)
        m = re.search(r"^  docs-deploy:\n((?:    .*\n|\n)*)", cls.ci, re.M)
        assert m, "no docs-deploy job in %s" % CI_YML
        cls.job = m.group(1)

    def test_it_waits_for_the_guard(self):
        # The whole reason this is a workflow job rather than a Pages branch
        # setting: a build on GitHub's side would publish without the guard
        # ever running against the commit that produced the pages.
        self.assertRegex(self.job, r"needs:\s*\[[^]]*\bguard\b")

    def test_it_runs_only_on_a_push_to_the_default_branch(self):
        # Both halves are load-bearing. Without the event check, a
        # `pull_request_target` run - which carries the base repository's
        # permissions - reports `refs/heads/main` and would let a fork's pull
        # request publish. Without the ref check, any pushed branch would
        # overwrite the published site.
        m = re.search(r"^    if: (.*)$", self.job, re.M)
        self.assertIsNotNone(m, "docs-deploy has no `if:` gate at all")
        gate = m.group(1)
        self.assertIn("github.event_name == 'push'", gate)
        self.assertIn("refs/heads/main", gate)

    def test_it_asks_for_no_more_permission_than_publishing_needs(self):
        m = re.search(r"^    permissions:\n((?:      .*\n)*)", self.job, re.M)
        self.assertIsNotNone(m, "docs-deploy declares no permissions block")
        granted = dict(re.findall(r"^      (\S+):\s*(\S+)$", m.group(1), re.M))
        self.assertEqual(
            granted, {"pages": "write", "id-token": "write"},
            "publishing to Pages needs exactly these two: `pages` to write "
            "the site and `id-token` for the OIDC exchange. Anything else "
            "here is scope this job does not use")

    def test_no_trace_of_the_previous_host_survives_anywhere(self):
        # The switch is only finished when nothing still reaches for the old
        # provider - not just its secrets. A leftover mention reads as
        # configuration the repository is missing rather than one it
        # deliberately dropped, and the worst case is a reader following stale
        # prose into setting up the build-from-a-branch integration that very
        # prose argues against, which is the un-guarded publish path.
        #
        # An earlier version of this test read only ci.yml, and only for the
        # uppercase secret name. It passed with four live mentions of the
        # provider in that same file and three more on a published docs page.
        # The name promised "no leftover reference" and the assertion checked
        # for a secret - so it is swept case-insensitively, across every
        # document as well as the workflow.
        stale = ("cloudflare", "wrangler", "pages.dev")
        for path in _doc_paths() + [CI_YML]:
            haystack = _read(path).lower()
            for needle in stale:
                self.assertNotIn(
                    needle, haystack,
                    "%s still mentions %r. The site is published by GitHub "
                    "Pages now; a stale reference sends a reader looking for "
                    "configuration that does not exist." % (path, needle))


class TheAdaptersDocAgreesWithTheExampleItDescribes(unittest.TestCase):
    """docs/adapters.md walks through `owner_segment` with a worked example -
    a URL, an index, and the owner that index resolves to - because getting
    that index wrong (it counts from after the scheme and INCLUDES the host)
    has already caused a real misconfiguration. That worked example and
    config/adapters.example.json's own `owner_segment_example` are two
    hand-written copies of the same fact; this fails if they drift apart,
    the same shape as the module-map duplication pinned above."""

    @classmethod
    def setUpClass(cls):
        cls.doc = _read(ADAPTERS_DOC)
        cls.loaded = load_adapters(ADAPTERS_EXAMPLE)

    def _url_segment_fence(self):
        for fence in _JSON_FENCE_RE.findall(self.doc):
            payload = json.loads(fence)
            if payload.get("owner_source") == "url_segment":
                return payload
        self.fail("no ```json fence in docs/adapters.md configures "
                  "owner_source: url_segment")

    def test_the_docs_url_segment_example_is_itself_valid_json(self):
        self._url_segment_fence()   # raises via self.fail if absent/invalid

    def test_the_docs_example_and_the_shipped_config_pick_the_same_segment(self):
        doc_spec = self._url_segment_fence()
        config_adapter = self.loaded["examplestore"]
        self.assertEqual(doc_spec["owner_segment"], 2)
        self.assertEqual(config_adapter.owner_source, "url_segment")
        # The doc's own worked example must resolve exactly as documented -
        # this is the guard against the doc's prose and its own code fence
        # disagreeing about what index 2 picks out.
        example = doc_spec["owner_segment_example"]
        self.assertEqual(config_adapter.artist_from_url(example["url"]),
                         example["owner"])

    def test_every_owner_source_mechanism_documented_is_also_in_the_example(self):
        documented = {json.loads(f)["owner_source"]
                     for f in _JSON_FENCE_RE.findall(self.doc)
                     if "owner_source" in json.loads(f)}
        configured = {a.owner_source for a in self.loaded.values()}
        self.assertEqual(
            documented, configured,
            "docs/adapters.md documents owner_source mechanisms %s but "
            "config/adapters.example.json only configures %s - a reader "
            "working from the example alone should be able to find every "
            "mechanism the prose describes" % (sorted(documented), sorted(configured)))


class ThereIsAStandingRuleForProvingANewTestCanFail(unittest.TestCase):
    """Several tests in this repository have been found to pass for a reason
    other than the one they name — one passed before the code it tested even
    existed. The fix for that is a habit, not a line of code, so what a test
    can pin here is only that the habit is written down where a contributor
    will actually see it — not that anyone follows it. If this ever starts
    failing because CONTRIBUTING.md was softened or removed, that is the
    thing to look at, not this test."""

    def test_contributing_md_exists(self):
        self.assertTrue(
            os.path.exists("CONTRIBUTING.md"),
            "no CONTRIBUTING.md at the repository root")

    def test_it_states_the_standing_rule(self):
        prose = " ".join(_read("CONTRIBUTING.md").split()).lower()
        self.assertIn(
            "watched failing", prose,
            "CONTRIBUTING.md should say a test that has never been watched "
            "failing does not hold")
        self.assertIn(
            "break the", prose,
            "CONTRIBUTING.md should instruct breaking the specific "
            "behaviour a new test names")
        self.assertIn(
            "restore the code", prose,
            "CONTRIBUTING.md should instruct restoring the code once the "
            "test has been confirmed to fail for the right reason")

    def test_the_readme_links_it(self):
        self.assertIn(
            "CONTRIBUTING.md", _read(README),
            "the README's Documentation section should link CONTRIBUTING.md, "
            "the same way it links every other doc page")


if __name__ == "__main__":
    unittest.main()
