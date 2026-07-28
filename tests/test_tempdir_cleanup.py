"""Ticket #29: a full suite run used to leave dozens of throwaway directories
behind (a git repo per guard test, a config directory per adapter test), none
of which anyone noticed until `TMPDIR` was pointed at an empty directory and
counted before and after a run. `tests.fixtures.tempdirs` is the fix; this
file is the test FOR the fix, as opposed to the tests that merely use it (see
tests/test_guard.py, tests/test_commit_msg_hook.py, tests/test_adapters.py).

The property that actually matters is not "the directory eventually goes
away" -- a `tearDown` that never fired would say so at process exit too, via
the OS reclaiming `/tmp`, and that is precisely the invisible failure mode
the ticket describes. It is "cleanup runs even when the test that created the
directory did not finish cleanly", which is why the case below is a fixture
that DELIBERATELY FAILS, driven through unittest's own machinery so this file
can inspect the directory afterwards without that failure also failing this
suite (or, worse, being picked up by `discover` as a permanently-red test of
its own -- which is why the fixture class is built locally, inside the test
method, rather than as a module-level class `discover` would find on its
own)."""
import os
import unittest

from tests.fixtures.tempdirs import TempDirCleanup, mkdtemp


class CleansUpEvenWhenTheTestFails(unittest.TestCase):
    def test_a_directory_created_before_a_failure_is_still_removed(self):
        created = {}

        class _CreatesADirectoryThenRaises(TempDirCleanup, unittest.TestCase):
            def test_it(self):
                created["path"] = mkdtemp()
                raise RuntimeError("simulated failure partway through the fixture")

        result = unittest.TestResult()
        _CreatesADirectoryThenRaises("test_it").run(result)

        # Confirm the fixture really did fail before trusting what it implies
        # about cleanup -- a baseline that was secretly green would make the
        # assertion below meaningless.
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("simulated failure", result.errors[0][1])
        self.assertIn("path", created, "the fixture never even created one")
        self.assertFalse(
            os.path.exists(created["path"]),
            "a directory created before a mid-test failure must still be "
            "removed by tearDown, not left behind because the test never "
            "reached its own cleanup code")


if __name__ == "__main__":
    unittest.main()
