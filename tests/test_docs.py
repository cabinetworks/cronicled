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
* the site's deploy step is inert until two Cloudflare secrets are added to
  the repository. "Inert" has to mean the job still passes: a red main for a
  secret nobody has added yet teaches everyone to ignore a red main. That
  behaviour is tested by running the workflow's own shell, not by reading it.

None of this needs a YAML parser or a Markdown parser, and deliberately so -
the suite runs on a bare interpreter with nothing installed.
"""
import os
import re
import subprocess
import tempfile
import unittest

from cronicled import selfcheck

README = "README.md"
INDEX = os.path.join("docs", "index.md")
CI_YML = os.path.join(".github", "workflows", "ci.yml")

_FENCE_RE = re.compile(r"^```mermaid\n(.*?)^```$", re.S | re.M)
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
            "project's runtime dependencies stay empty) and with --strict "
            "(so a dead internal link fails here rather than shipping)")

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


class TheDeployIsInertNotBroken(unittest.TestCase):
    """Neither Cloudflare secret exists on the repository yet. Until they do,
    the deploy must skip with a visible notice and a passing job.

    This runs the gate step's own shell rather than reading its text: the
    thing that matters is what it decides for a given set of inputs, and a
    condition can be reworded a dozen ways that all read fine and one of
    which is wrong."""

    @classmethod
    def setUpClass(cls):
        ci = _read(CI_YML)
        # Deliberately not re.DOTALL across the whole pattern: `.` staying
        # line-bound is what keeps this a linear scan instead of a
        # backtracking one.
        m = re.search(
            r"^      - name: Decide whether this run can publish\n"
            r"((?:        .*\n|\n)*)",
            ci, re.M)
        assert m, "no 'Decide whether this run can publish' step in %s" % CI_YML
        body = m.group(1)
        run = re.search(r"^        run: \|\n((?:          .*\n|\n)*)", body, re.M)
        assert run, "that step has no `run: |` script"
        cls.script = run.group(1)
        cls.ci = ci

    def _decide(self, is_fork_pr, token, account_id):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "gh_output")
            open(out_path, "w").close()
            env = dict(os.environ)
            env.update({
                "GITHUB_OUTPUT": out_path,
                "IS_FORK_PR": is_fork_pr,
                "CLOUDFLARE_API_TOKEN": token,
                "CLOUDFLARE_ACCOUNT_ID": account_id,
            })
            proc = subprocess.run(
                ["sh", "-c", self.script], env=env,
                capture_output=True, text=True)
            with open(out_path) as fh:
                written = fh.read()
        return proc, written

    def test_a_missing_token_skips_the_deploy_without_failing_the_job(self):
        proc, written = self._decide("false", "", "an-account-id")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("publish=no", written)
        self.assertIn("::notice::", proc.stdout)

    def test_a_missing_account_id_skips_it_too(self):
        # Deploying with one of the two present is not a partial success -
        # wrangler would fail mid-publish instead of never starting.
        proc, written = self._decide("false", "a-token", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("publish=no", written)

    def test_a_fork_pull_request_skips_it_and_says_why(self):
        proc, written = self._decide("true", "a-token", "an-account-id")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("publish=no", written)
        self.assertIn("fork", proc.stdout.lower())

    def test_a_run_with_both_secrets_does_publish(self):
        # The other half, and the one a "skip cleanly" change would quietly
        # break: a gate that never says yes is not cautious, it is broken,
        # and nothing else in this file would notice.
        proc, written = self._decide("false", "a-token", "an-account-id")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("publish=yes", written)
        self.assertNotIn("publish=no", written)

    def test_nothing_publishes_except_through_that_decision(self):
        m = re.search(r"^  docs-deploy:\n((?:    .*\n|\n)*)", self.ci, re.M)
        self.assertIsNotNone(m)
        job = m.group(1)
        # Every step that could touch Cloudflare, or fetch what would be
        # sent there, has to be gated on the decision above. An ungated
        # wrangler step would fail the job with a missing token - the exact
        # outcome the gate exists to avoid.
        steps = re.split(r"^      - ", job, flags=re.M)[1:]
        publishing = [s for s in steps if "wrangler" in s or "download-artifact" in s]
        self.assertGreaterEqual(
            len(publishing), 2,
            "expected the artifact download and the wrangler publish step")
        for step in publishing:
            self.assertRegex(
                step, r"if:\s*steps\.gate\.outputs\.publish == 'yes'",
                "this step runs regardless of whether the run can publish:\n%s"
                % step)


if __name__ == "__main__":
    unittest.main()
