"""scripts/hooks/commit-msg was never covered by tests, and a re-review found
it had the same defect the guard itself had just been fixed for: `grep`
reports "no match" (its normal exit code, treated as "allow the commit")
both for a clean message and for one it cannot open. `chmod 000` on a
commit message file reproduced this directly - the hook exited 0 and
allowed the commit. These tests exercise the hook as a black box, the same
way tests/test_guard.py exercises the guard."""
import os
import subprocess
import tempfile
import unittest

HOOK = os.path.abspath("scripts/hooks/commit-msg")


def _msg_file(body):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "COMMIT_EDITMSG")
    with open(path, "w") as fh:
        fh.write(body)
    return path


def _run(msg_file, env=None):
    e = dict(os.environ)
    e.pop("LEAK_PATTERNS", None)
    e.update(env or {})
    p = subprocess.run([HOOK, msg_file], env=e, cwd=os.path.dirname(msg_file),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


class BasicBehaviour(unittest.TestCase):
    def test_blocks_a_message_matching_a_forbidden_pattern(self):
        f = _msg_file("contains zzsecretzz\n")
        code, out = _run(f, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertEqual(code, 1, out)
        self.assertIn("BLOCKED", out)

    def test_allows_a_clean_message(self):
        f = _msg_file("a perfectly normal commit message\n")
        code, out = _run(f, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertEqual(code, 0, out)

    def test_no_patterns_anywhere_is_a_rejection(self):
        f = _msg_file("a perfectly normal commit message\n")
        code, out = _run(f)
        self.assertEqual(code, 1, out)
        self.assertIn("no patterns", out.lower())


class UnreadableMessageFailsClosed(unittest.TestCase):
    """The exact defect the re-review found: an unreadable message file
    must not be treated the same as a clean one."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits do not apply to root")

    def test_an_unreadable_message_file_is_blocked_not_allowed(self):
        f = _msg_file("contains zzsecretzz\n")
        os.chmod(f, 0o000)
        try:
            code, out = _run(f, env={"LEAK_PATTERNS": "zzsecretzz"})
        finally:
            os.chmod(f, 0o644)
        self.assertEqual(code, 1, out)
        self.assertIn("BLOCKED", out)
