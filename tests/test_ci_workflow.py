"""CI's guard job (`.github/workflows/ci.yml`) used to run the leak guard
in reduced (--file-types-only) mode whenever the LEAK_PATTERNS secret came
through empty, on the theory that this only happens on a fork pull request
(which cannot read repository secrets at all, by GitHub's own design).
That reasoning conflated two different things: "secret is empty" is not
the same fact as "this is a fork PR" - the secret can also be empty because
it was deleted, renamed, expired, or scoped to the wrong environment, and
none of those should ever run in reduced mode. Reproduced directly: commit
a forbidden string in a tracked file, then run the workflow's own logic
with the secret absent - it prints "expected on a fork PR" regardless of
whether it is one, runs --file-types-only, and the job goes green.

`github.event.pull_request.head.repo.fork` is the actual discriminator:
GitHub sets it (true/false) for every pull_request event, and it simply
does not exist on a push event, so the guard step's own condition
evaluates to false there rather than erroring - exactly right, since a
fork PR is impossible by construction on a push to main.

This has no code to import and run (it is CI configuration, not Python),
so this test reads the workflow file as text and asserts the shape of its
guard logic. It exists specifically to fail if someone reverts the gate
to the old "secret is empty" check - a plausible-looking simplification
that reintroduces the exact regression above.
"""
import re
import unittest

CI_YML = ".github/workflows/ci.yml"


class CiWorkflowForkGating(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CI_YML) as fh:
            cls.text = fh.read()
        # The "Leak guard" step's own block: from its `name:` line up to
        # (not including) the next step at the same indentation, or the
        # end of the file.
        m = re.search(
            r"- name:\s*Leak guard\n(.*?)(?=\n\s{6}- name:|\Z)",
            cls.text, re.S)
        assert m, "could not find the 'Leak guard' step in %s" % CI_YML
        cls.step = m.group(1)
        run_m = re.search(r"run:\s*\|(.*)", cls.step, re.S)
        assert run_m, "the 'Leak guard' step has no `run: |` script"
        cls.run_script = run_m.group(1)

    def test_the_fork_discriminator_is_present(self):
        # The one thing in this workflow that can actually tell a fork PR
        # apart from a same-repo run with a missing secret.
        self.assertIn(
            "github.event.pull_request.head.repo.fork", self.step,
            "the guard step must consult the real fork/non-fork "
            "discriminator (github.event.pull_request.head.repo.fork), "
            "not infer fork-ness from whether the secret came through empty")

    def _fork_env_var(self):
        m = re.search(
            r"(\w+):\s*\$\{\{[^}]*github\.event\.pull_request\.head\.repo\.fork[^}]*\}\}",
            self.step)
        self.assertIsNotNone(
            m, "expected an env var in the step assigned from the fork "
               "expression, e.g. SOMETHING: ${{ github.event.pull_request"
               ".head.repo.fork == true }}")
        return m.group(1)

    def test_reduced_mode_is_gated_on_the_fork_variable_not_the_secret(self):
        fork_var = self._fork_env_var()
        idx = self.run_script.find("--file-types-only")
        self.assertNotEqual(idx, -1,
                             "expected a --file-types-only invocation in "
                             "the guard step's script")
        preceding = self.run_script[:idx]
        last_if = preceding.rfind("if ")
        self.assertNotEqual(
            last_if, -1,
            "--file-types-only must be reached only through a conditional")
        condition = preceding[last_if:]
        self.assertIn(
            fork_var, condition,
            "the branch that runs --file-types-only must test the fork "
            "discriminator variable (%r), not something else" % fork_var)
        # The exact regression this guards against: gating reduced mode on
        # nothing but the secret being empty, which cannot distinguish a
        # fork PR from a deleted/renamed/expired/mis-scoped secret.
        self.assertNotRegex(
            condition, r"-z\s+\"?\$\{?LEAK_PATTERNS\}?\"?\s*\]",
            "reduced mode must not be reachable merely because "
            "LEAK_PATTERNS is empty - that was the exact bug: it cannot "
            "tell a fork PR apart from a missing/renamed/expired secret")

    def test_a_non_fork_run_with_an_empty_secret_fails_the_job(self):
        # The other half of the fix: when this is NOT a fork PR (a push to
        # main, or a same-repo PR) and the secret is still empty, the job
        # must fail loudly rather than quietly downgrade. Isolate the
        # branch that handles "secret empty" outside of the fork branch:
        # it must not itself invoke --file-types-only, and it must abort.
        m = re.search(r"elif(.*?)else", self.run_script, re.S)
        self.assertIsNotNone(
            m, "expected a second branch (elif) distinguishing 'secret "
               "empty but not a fork PR' from both the fork-PR case and "
               "the normal full-scan case")
        empty_secret_branch = m.group(1)
        self.assertIn("LEAK_PATTERNS", empty_secret_branch)
        self.assertNotIn(
            "--file-types-only", empty_secret_branch,
            "a non-fork run with an empty secret must not fall back to "
            "reduced mode")
        self.assertRegex(
            empty_secret_branch, r"exit\s+1",
            "a non-fork run with an empty secret must fail the job")

    def test_full_scan_runs_when_neither_branch_applies(self):
        self.assertIn("./scripts/check_leaks\n", self.run_script)
        # The unqualified invocation (full mode) must appear in the final
        # `else` branch, i.e. after the fork check and the empty-secret
        # check have both been ruled out.
        idx = self.run_script.rfind("./scripts/check_leaks")
        preceding = self.run_script[:idx]
        self.assertNotIn("--file-types-only", self.run_script[idx:])
        self.assertIn("else", preceding[preceding.rfind("elif"):])


class CiWorkflowRunsOnEveryBranch(unittest.TestCase):
    """A feature branch pushed to the public remote is public the moment it
    lands there. The guard job used to trigger only on a push to the
    default branch (plus on any pull request), so a branch with no pull
    request open yet was never scanned at all. The `push:` trigger's
    `branches:` filter must match every branch, not name the default one."""

    @classmethod
    def setUpClass(cls):
        with open(CI_YML) as fh:
            cls.text = fh.read()
        # The top-level `on:` block: from `on:` up to the `jobs:` key that
        # starts the next top-level section.
        on_m = re.search(r"^on:\n(.*?)^jobs:", cls.text, re.S | re.M)
        assert on_m, "could not find the top-level `on:` block in %s" % CI_YML
        on_block = on_m.group(1)
        # `push:`'s own children: consecutive lines indented at least four
        # spaces (or blank), i.e. everything nested under it before the
        # next two-space-indented key (`pull_request:`).
        push_m = re.search(r"push:\n((?:[ ]{4}.*\n|\n)*)", on_block)
        assert push_m, "could not find the `push:` trigger block in %s" % CI_YML
        cls.push_block = push_m.group(1)

    def test_the_push_trigger_is_not_restricted_to_the_default_branch(self):
        self.assertNotRegex(
            self.push_block, r"branches:\s*\[\s*[\"']?main[\"']?\s*\]",
            "the push trigger still names only the default branch - a "
            "feature branch pushed to the public remote is public before "
            "any pull request exists to bring it through the guard")

    def test_the_push_trigger_matches_every_branch(self):
        self.assertRegex(
            self.push_block, r"branches:\s*\[\s*[\"']\*\*[\"']\s*\]",
            "expected the push trigger's branches filter to match every "
            "branch (e.g. branches: [\"**\"])")

    def test_release_tags_still_trigger_the_workflow_too(self):
        # The branch-matching fix must not have dropped the separate tag
        # trigger the publish job depends on.
        self.assertRegex(self.push_block, r"tags:\s*\[\s*[\"']v\*[\"']\s*\]")
