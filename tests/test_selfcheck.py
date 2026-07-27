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
    """The self-check is what proves the pinned interpreter actually RUNS this
    project's code rather than merely having a directory copied into it -- the
    smoke test it replaced was a bare `import cronicled`, which ran none of it.

    It used to be the image's default command and is not any more: the image
    now serves the inbox, which is the point of having an image. The claim
    narrowed rather than disappeared -- what this class protects now is that
    the self-check still runs somewhere automatic, and that the image does not
    quietly go back to running it instead of the tool."""

    def test_the_image_does_not_start_the_selfcheck_by_default(self):
        # Its own assertion rather than left implicit in a "serves the inbox"
        # check: if the default command reverted to the self-check, the image
        # would start, print one line, exit 0 and serve nothing -- and a test
        # that only looked for the inbox command appearing SOMEWHERE in the
        # file would not notice, because the self-check line would be the CMD
        # and the inbox line would be a comment.
        with open("Dockerfile") as fh:
            body = fh.read()
        self.assertNotIn('CMD ["python", "-m", "cronicled.selfcheck"]', body)

    def test_ci_container_job_runs_the_selfcheck(self):
        # Now the only place the self-check runs automatically. It used to have
        # the image's default command as a second, independent trigger; that
        # backstop is gone, so this assertion carries it alone.
        with open(".github/workflows/ci.yml") as fh:
            body = fh.read()
        self.assertRegex(body, r"python -m cronicled\.selfcheck")


if __name__ == "__main__":
    unittest.main()
