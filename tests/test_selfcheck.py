"""The self-check proves the pinned runtime can actually run this project's
code, not merely that a directory was copied into an image (see
cronicled/selfcheck.py for the full rationale). These tests exercise it
in-process; the proof that it also catches a broken module or a broken
exercised function lives in a scratch-copy transcript, not here, since that
requires mutating source files out of process."""
import re
import unittest

from cronicled import selfcheck


class SelfCheck(unittest.TestCase):
    def test_run_imports_every_module_and_returns_their_names(self):
        modules = selfcheck.run()
        self.assertIn("cronicled", modules)
        self.assertIn("cronicled.stash", modules)
        self.assertIn("cronicled.adapters.declarative", modules)

    def test_main_prints_a_ready_line_and_exits_zero_on_success(self):
        self.assertEqual(selfcheck.main(), 0)

    def test_check_raises_on_a_mismatch(self):
        with self.assertRaises(selfcheck.SelfCheckError):
            selfcheck._check("label", "actual", "expected")

    def test_check_is_silent_on_a_match(self):
        selfcheck._check("label", "same", "same")  # must not raise


class Wiring(unittest.TestCase):
    """The old smoke test (a bare `import cronicled`) never ran a line of this
    project's code; both places that invoke the container's default command
    must point at the self-check instead, or this drifts right back."""

    def test_dockerfile_cmd_runs_the_selfcheck(self):
        with open("Dockerfile") as fh:
            body = fh.read()
        self.assertIn('CMD ["python", "-m", "cronicled.selfcheck"]', body)

    def test_ci_container_job_runs_the_selfcheck(self):
        with open(".github/workflows/ci.yml") as fh:
            body = fh.read()
        self.assertRegex(body, r"python -m cronicled\.selfcheck")


if __name__ == "__main__":
    unittest.main()
