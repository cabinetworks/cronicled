"""CI publishes the container image to a public registry. Four things about
that job are each one line away from being wrong, and none of them fails
visibly:

- a `latest` tag would be published, and `latest` is read as "the one you
  want" — this image's default command serves an unauthenticated page whose
  buttons write to a library, and nobody wants that pulled without knowing
  which build it is;
- a dropped `needs: guard` publishes an image the leak guard never scanned;
- a widened `if:` lets a pull request push into the project's own namespace;
- a dropped `--build-arg` publishes an image built on whatever interpreter the
  Dockerfile's ARG default happens to say, rather than the declared one.

So this file does not describe the workflow, it exercises it. The tag logic is
a shell script in the workflow, and the tests below extract that exact script
out of the YAML and run it under `sh` with the environment GitHub would supply
— a reimplementation of the logic in Python would prove only that the logic can
be written twice. The `if:` expression is likewise taken as text out of the
workflow and evaluated against real event contexts, including the two that
would otherwise be holes (`pull_request_target`, which runs with the base
repository's permissions and whose `github.ref` is `refs/heads/main`, and a
push to a branch that is not the default one).
"""
import os
import re
import subprocess
import tempfile
import unittest

from cronicled.web.app import DEFAULT_HOST, DEFAULT_PORT

CI_YML = ".github/workflows/ci.yml"
REGISTRY_IMAGE = "ghcr.io/cabinetworks/cronicled"
TAG_STEP = "Work out the image tags and the pinned interpreter"


def _read(path):
    with open(path) as fh:
        return fh.read()


def _job(name):
    """The text of one job block: from `  <name>:` at two-space indentation up
    to the next job at the same indentation, or the end of the file."""
    text = _read(CI_YML)
    m = re.search(r"^  %s:\n(.*?)(?=\n  \w+:\n|\Z)" % re.escape(name),
                  text, re.S | re.M)
    assert m, "no %r job in %s" % (name, CI_YML)
    return m.group(1)


def _step_script(job_text, step_name):
    """The `run: |` script of a named step, dedented back to column zero.

    Only lines indented to the block scalar's own depth are taken, so a
    comment sitting between this step and the next one cannot be swept in and
    silently change what gets executed."""
    m = re.search(r"- name:\s*%s\b.*?\n\s*run: \|\n" % re.escape(step_name),
                  job_text, re.S)
    assert m, "no %r step with a `run: |` script" % step_name
    rest = job_text[m.end():].split("\n")
    indent = len(rest[0]) - len(rest[0].lstrip())
    assert indent > 0, "block scalar body is not indented"
    out = []
    for line in rest:
        if not line.strip():
            out.append("")
        elif line.startswith(" " * indent):
            out.append(line[indent:])
        else:
            break
    return "\n".join(out)


class TagScript(unittest.TestCase):
    """Run the workflow's own tag script, not a copy of it."""

    @classmethod
    def setUpClass(cls):
        cls.script = _step_script(_job("publish"), TAG_STEP)
        cls.declared_version = re.search(
            r'^version = "([^"]*)"', _read("pyproject.toml"), re.M).group(1)

    def _run(self, ref, sha="0" * 40, cwd=None):
        env = dict(os.environ, GITHUB_REF=ref, GITHUB_SHA=sha)
        with tempfile.NamedTemporaryFile("w+", suffix=".env") as out:
            env["GITHUB_OUTPUT"] = out.name
            proc = subprocess.run(["sh", "-c", self.script], env=env, cwd=cwd,
                                  capture_output=True, text=True)
            out.seek(0)
            written = out.read()
        outputs = dict(
            line.split("=", 1) for line in written.splitlines() if "=" in line)
        return proc, outputs

    def test_a_push_to_the_default_branch_tags_the_commit_and_latest(self):
        # Asserted as the WHOLE tag list rather than by checking `latest` is
        # somewhere in it: this output is the exact argument the push step
        # takes, so a tag gained or lost is the defect worth catching, and a
        # containment check cannot see either.
        proc, outputs = self._run("refs/heads/main", sha="a" * 40)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            outputs["tags"],
            "%s:%s,%s:latest" % (REGISTRY_IMAGE, "a" * 40, REGISTRY_IMAGE))

    def test_a_release_tag_adds_the_declared_version(self):
        proc, outputs = self._run("refs/tags/v" + self.declared_version,
                                  sha="b" * 40)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            outputs["tags"],
            "%s:%s,%s:%s" % (REGISTRY_IMAGE, "b" * 40,
                             REGISTRY_IMAGE, self.declared_version))

    def test_a_release_tag_does_not_move_latest(self):
        # The one way this tag can mislead beyond its ordinary imprecision.
        # Releases are not necessarily cut in order: tagging an older but
        # still-supported version would drag `latest` backwards onto it, so
        # someone pulling `latest` would silently DOWNGRADE. `latest` follows
        # the default branch and nothing else.
        _, outputs = self._run("refs/tags/v" + self.declared_version)
        produced = outputs["tags"].split(",")
        self.assertTrue(produced)
        for tag in produced:
            self.assertNotEqual(tag.rsplit(":", 1)[1], "latest")

    def test_a_push_to_a_non_default_branch_does_not_move_latest(self):
        # The publish job's `if:` gate should already stop this job running at
        # all off the default branch; this pins the tag script's own half of
        # that, so `latest` cannot follow a branch even if the gate widens.
        _, outputs = self._run("refs/heads/some-feature-branch")
        for tag in outputs["tags"].split(","):
            self.assertNotEqual(tag.rsplit(":", 1)[1], "latest")

    def test_a_tag_that_disagrees_with_the_declared_version_publishes_nothing(self):
        # Not a tie to break by preferring one of the two: an image labelled
        # with a version it was not built from is worse than no image, and it
        # cannot be taken back off a public registry.
        proc, outputs = self._run("refs/tags/v0.0.0-not-the-declared-version")
        self.assertNotEqual(proc.returncode, 0,
                            "a mismatched release tag must fail the job")
        self.assertEqual(outputs, {},
                         "nothing may be emitted for a mismatched tag")

    def test_the_pinned_interpreter_is_the_declared_one(self):
        _, outputs = self._run("refs/heads/main")
        self.assertEqual(outputs["interpreter"], _read(".python-version").strip())

    def test_the_interpreter_is_read_from_the_file_and_not_pasted_in(self):
        # Run the script somewhere declaring a version this repo does not.
        # Comparing the output against `.python-version` alone cannot fail
        # for a literal pasted into the workflow, because the literal anyone
        # would paste is today's value - and a second copy of the pin is
        # exactly what the rest of this project's tests exist to prevent.
        with tempfile.TemporaryDirectory() as elsewhere:
            with open(os.path.join(elsewhere, ".python-version"), "w") as fh:
                fh.write("9.99\n")
            proc, outputs = self._run("refs/heads/main", cwd=elsewhere)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["interpreter"], "9.99")

    def test_the_script_names_the_public_registry(self):
        self.assertIn(REGISTRY_IMAGE, self.script)


# --- the `if:` gate -------------------------------------------------------
#
# Evaluating GitHub's expression syntax rather than pattern-matching it, so
# the test asks the question that matters ("would this job run on a pull
# request?") instead of the question that is easy ("does this string appear?").
# The subset understood here is exactly what the workflow uses; anything else
# raises, so an `if:` rewritten in unfamiliar syntax fails loudly rather than
# quietly evaluating to something convenient.

_TOKEN = re.compile(r"""
      (?P<space>\s+)
    | (?P<string>'(?:[^']|'')*')
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    | (?P<op>==|!=|&&|\|\||[()!,])
""", re.X)

_OPS = {"&&": " and ", "||": " or ", "!": " not ", "==": "==", "!=": "!=",
        "(": "(", ")": ")", ",": ","}

_FUNCS = {
    "startsWith": lambda a, b: a.startswith(b),
    "endsWith": lambda a, b: a.endswith(b),
    "contains": lambda a, b: b in a,
}


def _lookup(path, context):
    node = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(
                "the `if:` reads %r, which this test's event context does not "
                "define - either the expression consults a new property or it "
                "has a typo, and both need looking at" % path)
        node = node[part]
    return node


def _evaluate(expr, context):
    py, names, pos = [], {}, 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError("unsupported syntax in the `if:` expression at "
                             "%r" % expr[pos:pos + 30])
        pos = m.end()
        kind = m.lastgroup
        tok = m.group()
        if kind == "space":
            py.append(" ")
        elif kind == "string":
            py.append(repr(tok[1:-1].replace("''", "'")))
        elif kind == "op":
            py.append(_OPS[tok])
        elif tok in _FUNCS:
            py.append("_f[%r]" % tok)
        else:
            key = "_v%d" % len(names)
            names[key] = _lookup(tok, context)
            py.append(key)
    scope = dict(names, _f=_FUNCS)
    return bool(eval("".join(py), {"__builtins__": {}}, scope))


def _ctx(event_name, ref):
    return {"github": {"event_name": event_name, "ref": ref}}


class PublishGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        job = _job("publish")
        m = re.search(r"^    if:\s*(.+)$", job, re.M)
        assert m, "the publish job has no `if:` gate at all"
        cls.gate = m.group(1).strip()

    def _assert_runs(self, expected, event_name, ref):
        self.assertEqual(
            _evaluate(self.gate, _ctx(event_name, ref)), expected,
            "publish gate %r gave the wrong answer for %s on %s"
            % (self.gate, event_name, ref))

    def test_it_publishes_from_the_default_branch(self):
        # The permissive side matters too: a gate that has drifted shut
        # publishes nothing and says nothing about it.
        self._assert_runs(True, "push", "refs/heads/main")

    def test_it_publishes_from_a_release_tag(self):
        self._assert_runs(True, "push", "refs/tags/v0.1.0")

    def test_a_pull_request_cannot_publish(self):
        self._assert_runs(False, "pull_request", "refs/pull/7/merge")

    def test_a_pull_request_target_cannot_publish(self):
        # The trap: `pull_request_target` runs with the base repository's
        # permissions, and its `github.ref` is `refs/heads/main`. A gate that
        # checked only the ref would hand a fork's pull request the ability to
        # push an image into this project's own namespace.
        self._assert_runs(False, "pull_request_target", "refs/heads/main")

    def test_a_push_to_any_other_branch_cannot_publish(self):
        self._assert_runs(False, "push", "refs/heads/some-feature-branch")

    def test_a_push_to_a_branch_merely_starting_with_main_cannot_publish(self):
        self._assert_runs(False, "push", "refs/heads/maintenance")

    def test_an_unrecognised_expression_is_an_error_not_a_pass(self):
        # The evaluator above must never quietly return False for syntax it
        # does not understand, or a rewritten gate would look watertight.
        with self.assertRaises(ValueError):
            _evaluate("github.event_name =~ 'push'", _ctx("push", "x"))
        with self.assertRaises(KeyError):
            _evaluate("github.actor == 'x'", _ctx("push", "x"))


class PublishJobShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.job = _job("publish")

    def test_it_waits_for_the_leak_guard(self):
        # Without this, a published image is one the guard never scanned - and
        # what is on a public registry cannot be recalled.
        m = re.search(r"^    needs:\s*(.+)$", self.job, re.M)
        self.assertIsNotNone(m, "the publish job must declare `needs`")
        needs = re.findall(r"[\w-]+", m.group(1))
        self.assertIn("guard", needs)

    def test_it_asks_for_no_more_than_package_write(self):
        m = re.search(r"^    permissions:\n((?:^      .*\n)+)", self.job, re.M)
        self.assertIsNotNone(m, "the publish job must narrow its permissions")
        granted = dict(re.findall(r"^\s*([\w-]+):\s*(\S+)\s*$", m.group(1), re.M))
        self.assertEqual(granted, {"contents": "read", "packages": "write"})

    def test_the_pinned_interpreter_is_passed_to_the_build(self):
        m = re.search(r"^\s*build-args: \|\n((?:^\s{12}.*\n)+)", self.job, re.M)
        self.assertIsNotNone(m, "the build must be given build-args")
        args = dict(re.findall(r"^\s*(\w+)=(.*)$", m.group(1), re.M))
        # It must come from the step that read `.python-version`, not from a
        # literal pasted here and not from the Dockerfile's ARG default.
        self.assertIn("PYTHON_VERSION", args)
        self.assertRegex(args["PYTHON_VERSION"],
                         r"\$\{\{\s*steps\.\w+\.outputs\.\w+\s*\}\}")
        self.assertNotRegex(args["PYTHON_VERSION"], r"\d")

    def test_the_tags_come_from_the_tag_script(self):
        m = re.search(r"^\s*tags:\s*(.+)$", self.job, re.M)
        self.assertIsNotNone(m, "the build must be given tags")
        self.assertRegex(m.group(1).strip(),
                         r"^\$\{\{\s*steps\.\w+\.outputs\.tags\s*\}\}$")

    def test_it_builds_for_both_published_architectures(self):
        m = re.search(r"^\s*platforms:\s*(.+)$", self.job, re.M)
        self.assertIsNotNone(
            m, "a single-architecture image excludes the machines this is "
               "most likely to run on")
        platforms = {p.strip() for p in m.group(1).split(",")}
        self.assertEqual(platforms, {"linux/amd64", "linux/arm64"})

    def test_it_pushes_to_the_project_registry(self):
        # assertRegex's third positional parameter is `msg`, not flags, so the
        # multiline flag has to be compiled into the pattern to take effect.
        self.assertRegex(self.job, re.compile(r"^\s*registry:\s*ghcr\.io\s*$", re.M))
        self.assertRegex(self.job, re.compile(r"^\s*push:\s*true\s*$", re.M))


class ImageMetadata(unittest.TestCase):
    """The registry listing is built from the image, not typed into a form, so
    the sentence that says what this image is not travels with it."""

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = _read("Dockerfile")
        cls.labels = dict(re.findall(
            r"(org\.opencontainers\.image\.[\w.]+)=\"([^\"]*)\"", cls.dockerfile))

    def test_the_source_label_points_at_the_repository(self):
        self.assertEqual(self.labels.get("org.opencontainers.image.source"),
                         "https://github.com/cabinetworks/cronicled")

    def test_the_licence_label_agrees_with_the_project_metadata(self):
        declared = re.search(r'license\s*=\s*\{\s*text\s*=\s*"([^"]*)"',
                             _read("pyproject.toml")).group(1)
        self.assertEqual(self.labels.get("org.opencontainers.image.licenses"),
                         declared)

    def test_the_description_says_the_default_command_now_serves_something(self):
        # This is the substance of publishing at all: on the registry there is
        # no README next to the image, and a published container reads as a
        # runnable thing. The image used to falsify that by exiting at once;
        # now that it does not, the description has to say so, or a stranger
        # who finds it here still has no idea it starts anything.
        description = self.labels.get("org.opencontainers.image.description", "")
        self.assertIn("inbox", description.lower())
        self.assertNotIn("not a way to run the tool", description.lower())
        self.assertNotIn("there is no service", description.lower())

    def test_the_image_does_not_buffer_away_its_startup_warnings(self):
        # Found by running the built image, not by reading it: `docker logs`
        # returned NOTHING. Python block-buffers stdout when it is not a
        # terminal, and a container's stdout is a pipe, so a long-running
        # server never fills or flushes the buffer and every startup line sits
        # in it invisibly.
        #
        # That matters here more than it usually would. One of those lines is
        # the binding warning, which tells an operator that what protects this
        # unauthenticated page is their `-p` flag rather than the bind address
        # -- and container.md states the warning prints on every container
        # start. A security warning nobody can see is not a warning, and the
        # documentation would have been describing output that never appeared.
        # Matched per line (re.M). `assertRegex` uses a bare `re.search`, where
        # `^` anchors to the start of the whole file rather than of a line --
        # written that way first, this could never pass, and it failed
        # identically with and without the directive present. A mutation
        # "caught" by a test that was already failing is not caught at all.
        directive = re.compile(r"^ENV\s+PYTHONUNBUFFERED=1\s*$", re.M)
        self.assertIsNotNone(
            directive.search(self.dockerfile),
            "the image must not buffer away its startup warnings")

    def test_the_description_warns_the_page_has_no_authentication(self):
        # The one fact a stranger pulling this image most needs before they
        # publish the port to more than loopback.
        description = self.labels.get("org.opencontainers.image.description", "")
        self.assertIn("no authentication", description.lower())

    def test_the_description_still_says_what_does_not_work_yet(self):
        # Not overclaiming in the other direction: this does not scan, and
        # nothing populates the store on its own. A stranger starting it will
        # notice an empty inbox immediately; the description should have
        # already told them why.
        description = self.labels.get("org.opencontainers.image.description", "")
        self.assertIn("scan", description.lower())

    def test_the_description_is_not_left_to_the_registry_default(self):
        self.assertGreater(len(self.labels.get(
            "org.opencontainers.image.description", "")), 80)


class DockerContext(unittest.TestCase):
    """`.dockerignore` is matched with Go's filepath.Match, not gitignore
    semantics: a bare `__pycache__` entry matches the context root only, so
    `cronicled/__pycache__` was copied into the image verbatim by anyone who
    had run the suite locally. CI never saw it, because CI builds from a clean
    checkout - which is why it went unnoticed and why it matters now."""

    @classmethod
    def setUpClass(cls):
        cls.patterns = [
            line.strip() for line in _read(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]

    def test_byte_code_is_excluded_at_every_depth(self):
        self.assertIn("**/__pycache__", self.patterns)

    def test_the_top_level_only_pattern_is_gone(self):
        self.assertNotIn("__pycache__", self.patterns,
                         "a bare entry matches the context root only")

    def test_the_directories_the_image_must_not_contain_are_listed(self):
        # The property that makes publishing from CI safe: everything in the
        # image is tracked source the guard has already scanned.
        for excluded in (".git", ".github", "tests", "scripts", "config",
                         ".leak-patterns"):
            self.assertIn(excluded, self.patterns)

    def test_the_dockerfile_copies_only_the_package(self):
        copies = re.findall(r"^COPY\s+(.*)$", _read("Dockerfile"), re.M)
        self.assertEqual(copies, ["cronicled/ ./cronicled/"],
                         "a new COPY line widens what reaches a public "
                         "registry and has to be checked against .dockerignore")


class PublishedImageDocumented(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = _read("README.md")

    def test_the_readme_names_the_published_image(self):
        self.assertIn(REGISTRY_IMAGE, self.readme)

    def test_the_readme_says_what_latest_actually_means(self):
        # `latest` is published now, so the claim this guarded is gone. What
        # replaces it is narrower and worth pinning for the same reason the
        # absence was: a moving tag on an image whose default command serves
        # an unauthenticated page that writes to a library is a fact someone
        # deserves before they pull it, and prose that quietly loses it reads
        # exactly like prose that never had it.
        self.assertRegex(self.readme, r"`latest`.{0,80}default branch",
                         re.S)
        self.assertRegex(self.readme, r"moves under anyone who\s+pulls it",
                         re.S)
        # And still points at the pinnable alternatives, so the moving tag is
        # never the only option a reader is shown.
        self.assertRegex(self.readme, r"commit SHA or a released\s+version",
                         re.S)


if __name__ == "__main__":
    unittest.main()
