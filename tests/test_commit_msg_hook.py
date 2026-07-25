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

from tests.test_guard import GUARD, _repo

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


class BomFailClosed(unittest.TestCase):
    """This hook used to carry its own ~60-line copy of check_leaks.sh's
    pattern loading, and that copy never received the BOM strip: a
    byte-order mark glued to the front of the first pattern (some editors,
    and the Windows clipboard, prepend one to UTF-8 text) survived the
    hook's whitespace trim and corrupted the pattern into something that
    could never match. Reproduced directly: a commit message containing
    the (BOM-corrupted) first pattern was ALLOWED here, rc=0, while
    scripts/check_leaks.sh caught the identical content correctly (see
    tests/test_guard.py's PatternListCorruptionVectors). Fixed by sourcing
    the same scripts/lib/patterns.sh both scripts now share, rather than
    porting the missing strip across a third time."""

    def test_a_leading_bom_on_the_pattern_source_does_not_corrupt_the_first_pattern(self):
        f = _msg_file("contains zzsecretzz\n")
        code, out = _run(f, env={"LEAK_PATTERNS": "﻿zzsecretzz"})
        self.assertEqual(code, 1, out)
        self.assertIn("BLOCKED", out)


def _run_guard(cwd, env=None):
    e = dict(os.environ)
    e.pop("LEAK_PATTERNS", None)
    e.update(env or {})
    p = subprocess.run([GUARD], cwd=cwd, env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


class HookAndGuardAgree(unittest.TestCase):
    """The hook and the guard now source the exact same
    scripts/lib/patterns.sh, rather than two independently-maintained
    copies of the same ~60 lines. Across any shape the pattern list can
    arrive corrupted in - each of these four has been a real vector at
    some point - they must reach the same verdict on the same content:
    both catch a genuine match, and neither catches a non-match."""

    SHAPES = {
        "bom": "﻿zzsecretzz\n",
        "whitespace_only_line": "   \nzzsecretzz\n",
        "missing_trailing_newline": "zzsecretzz",
        "inline_comment": "zzsecretzz   # trailing note\n",
    }

    def test_both_catch_a_matching_body_across_pattern_shapes(self):
        for shape, raw in self.SHAPES.items():
            with self.subTest(shape=shape):
                d = _repo(None, {"a.md": "contains zzsecretzz\n"})
                guard_code, guard_out = _run_guard(d, env={"LEAK_PATTERNS": raw})
                msg_code, msg_out = _run(
                    _msg_file("contains zzsecretzz\n"),
                    env={"LEAK_PATTERNS": raw})
                self.assertNotEqual(guard_code, 0,
                                    "guard did not catch shape %r: %s" % (shape, guard_out))
                self.assertNotEqual(msg_code, 0,
                                    "hook did not catch shape %r: %s" % (shape, msg_out))

    def test_both_stay_clean_on_a_non_matching_body_across_pattern_shapes(self):
        for shape, raw in self.SHAPES.items():
            with self.subTest(shape=shape):
                d = _repo(None, {"a.md": "unrelated harmless text\n"})
                guard_code, guard_out = _run_guard(d, env={"LEAK_PATTERNS": raw})
                msg_code, msg_out = _run(
                    _msg_file("an unrelated harmless commit message\n"),
                    env={"LEAK_PATTERNS": raw})
                self.assertEqual(guard_code, 0,
                                 "guard wrongly flagged shape %r: %s" % (shape, guard_out))
                self.assertEqual(msg_code, 0,
                                 "hook wrongly flagged shape %r: %s" % (shape, msg_out))
