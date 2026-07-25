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
        with open(".github/workflows/ci.yml") as fh:
            body = fh.read()
        self.assertIn("python-version-file", body)
        self.assertNotRegex(body, r'python-version:\s*"\d')
