"""The runtime version is declared once. This asserts nothing has grown a second
copy — a lesson from the ignore rules and the guard's extension list, two lists
that had to agree, drifted, and silently dropped CI."""
import re
import unittest


def _declared():
    with open(".python-version") as fh:
        return fh.read().strip()


class RuntimePin(unittest.TestCase):
    def test_version_file_holds_a_bare_version(self):
        self.assertRegex(_declared(), r"^\d+\.\d+$")

    def test_dockerfile_default_matches_the_declared_version(self):
        with open("Dockerfile") as fh:
            body = fh.read()
        m = re.search(r"^ARG PYTHON_VERSION=(\S+)", body, re.M)
        self.assertIsNotNone(m, "Dockerfile must default PYTHON_VERSION")
        self.assertEqual(m.group(1), _declared())

    def test_dockerfile_base_image_uses_the_arg(self):
        with open("Dockerfile") as fh:
            body = fh.read()
        # assertRegex's third positional parameter is `msg`, not flags (unlike
        # re.search, whose third parameter genuinely is flags) -- so the flag
        # must be compiled into the pattern to actually take effect.
        self.assertRegex(body, re.compile(r"^FROM python:\$\{PYTHON_VERSION\}", re.M))

    def test_ci_reads_the_version_file_rather_than_repeating_it(self):
        # The test job now hands the interpreter choice to `uv run`, which
        # reads .python-version itself with nothing spelled out in the
        # workflow - so the literal filename no longer has to appear there
        # for that job to stay honest. The container job still names it
        # explicitly (`cat .python-version`), which is the surviving proof
        # that CI derives the version from the one declared file rather
        # than repeating it.
        with open(".github/workflows/ci.yml") as fh:
            body = fh.read()
        self.assertIn(".python-version", body)
        # Catch a hardcoded version regardless of quote style, or none at all
        # (e.g. python-version: "3.11", python-version: '3.11', python-version: 3.11).
        self.assertNotRegex(body, r'python-version:\s*[\'"]?\d')

    def test_readme_does_not_repeat_the_version(self):
        # prose is the easiest place for a second copy to hide, because it
        # looks like documentation rather than configuration
        with open("README.md") as fh:
            body = fh.read()
        self.assertNotRegex(body, r"\bPython\s+3\.\d+\b")


# Exactly one runtime dependency is permitted, and it is here for one reason:
# the inbox renders scraped text, and Jinja2's autoescaping makes escaping
# structural instead of remembered. Anything else is an architectural change
# that should be argued for, not slipped in.
_ALLOWED_DEPENDENCIES = ("jinja2",)


class ProjectMetadata(unittest.TestCase):
    """pyproject.toml necessarily restates the Python version, so it joins the set
    of copies that must be kept honest by a test rather than by memory."""

    def _pyproject(self):
        with open("pyproject.toml") as fh:
            return fh.read()

    def test_requires_python_agrees_with_the_version_file(self):
        m = re.search(r'requires-python\s*=\s*"[>=~^]*([\d.]+)"', self._pyproject())
        self.assertIsNotNone(m, "pyproject.toml must declare requires-python")
        self.assertEqual(m.group(1), _declared())

    def test_declares_only_the_permitted_runtime_dependencies(self):
        m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", self._pyproject(),
                      re.S | re.M)
        self.assertIsNotNone(m, "pyproject.toml must declare dependencies")
        declared = re.findall(r'"([^"]+)"', m.group(1))
        names = [re.split(r"[<>=!~\[ ]", d)[0].strip().lower() for d in declared]
        # Asserted as a whole list, not with assertIn: sampling the names would
        # let an added dependency through, which is precisely what this guards.
        self.assertEqual(names, list(_ALLOWED_DEPENDENCIES),
                         "only %s may be a runtime dependency"
                         % ", ".join(_ALLOWED_DEPENDENCIES))

    def test_the_permitted_dependency_is_pinned_to_a_floor(self):
        # An unbounded requirement makes the image a function of the day it was
        # built rather than of its inputs.
        m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", self._pyproject(),
                      re.S | re.M)
        for dep in re.findall(r'"([^"]+)"', m.group(1)):
            self.assertRegex(dep, r"[><=~]=?\s*\d",
                             "%r must carry a version constraint" % dep)

    def test_the_image_does_not_install_uv(self):
        # uv is a development and CI convenience, not something the service
        # needs to run. The image installs exactly one thing (see the pip line
        # above `COPY cronicled/`), and this keeps that list from growing a
        # second entry by way of the build tooling rather than a decision.
        with open("Dockerfile") as fh:
            body = fh.read()
        self.assertNotIn("uv", body.lower())
