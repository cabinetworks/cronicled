"""The guard is the project's load-bearing safety mechanism and has failed three
times in ways code review missed. These tests exercise it as a black box."""
import os
import subprocess
import tempfile
import unittest

GUARD = os.path.abspath("scripts/check_leaks.sh")


def _repo(patterns, files, subdir=None):
    """A throwaway git repo with `files` committed and `patterns` configured."""
    d = tempfile.mkdtemp()
    subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=d)
    subprocess.check_call(["git", "config", "user.email", "t@example.test"], cwd=d)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=d)
    # Mirror this project's own .gitignore rule: .leak-patterns must never be
    # committed. Without this, the fixture would commit the pattern list
    # alongside the test's "clean" file and the guard would (correctly) flag
    # its own tracked content as a leak of the pattern list itself.
    with open(os.path.join(d, ".gitignore"), "w") as fh:
        fh.write(".leak-patterns\n")
    if patterns is not None:
        with open(os.path.join(d, ".leak-patterns"), "w") as fh:
            fh.write("\n".join(patterns) + "\n")
    for name, body in files.items():
        path = os.path.join(d, name)
        if os.path.dirname(name):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
    subprocess.check_call(["git", "add", "-A"], cwd=d)
    subprocess.check_call(["git", "commit", "-q", "-m", "seed"], cwd=d)
    if subdir:
        os.makedirs(os.path.join(d, subdir), exist_ok=True)
    return d


def _run(cwd, env=None, args=None):
    e = dict(os.environ)
    e.pop("LEAK_PATTERNS", None)
    e.update(env or {})
    p = subprocess.run([GUARD] + (args or []), cwd=cwd, env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


class OutputDiscipline(unittest.TestCase):
    def test_a_matching_filename_does_not_print_the_pattern(self):
        # CI logs are public: reporting the offending name would republish the
        # very string the guard exists to keep private
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "clean body\n"})
        code, out = _run(d)
        self.assertNotEqual(code, 0)
        self.assertNotIn("zzsecretzz", out)

    def test_it_still_says_how_many_filenames_matched(self):
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "clean body\n"})
        _, out = _run(d)
        self.assertIn("1", out)


class WorkingDirectory(unittest.TestCase):
    def test_finds_the_pattern_file_from_a_subdirectory(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"}, subdir="nested/deep")
        code, out = _run(os.path.join(d, "nested", "deep"))
        self.assertEqual(code, 0, out)
        self.assertIn("clean", out)

    def test_still_catches_a_leak_from_a_subdirectory(self):
        d = _repo(["zzsecretzz"], {"a.md": "has zzsecretzz inside\n"},
                  subdir="nested")
        code, _ = _run(os.path.join(d, "nested"))
        self.assertEqual(code, 1)


class FailClosed(unittest.TestCase):
    def test_no_patterns_anywhere_is_a_failure(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        os.remove(os.path.join(d, ".leak-patterns"))
        code, out = _run(d)
        self.assertEqual(code, 2)
        self.assertIn("no patterns", out.lower())


class FileTypesOnly(unittest.TestCase):
    def test_no_patterns_does_not_fail_in_file_types_only_mode(self):
        # A fork PR has no access to the LEAK_PATTERNS secret. This mode must
        # not demand patterns that cannot exist in that context.
        d = _repo(None, {"a.md": "harmless\n"})
        code, out = _run(d, args=["--file-types-only"])
        self.assertEqual(code, 0, out)

    def test_a_tracked_data_file_still_fails_in_file_types_only_mode(self):
        d = _repo(None, {"a.md": "harmless\n"})
        with open(os.path.join(d, "secret.env"), "w") as fh:
            fh.write("X=1\n")
        subprocess.check_call(["git", "add", "-f", "secret.env"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, args=["--file-types-only"])
        self.assertEqual(code, 1, out)

    def test_pattern_legs_are_skipped_even_when_a_pattern_would_match(self):
        # A fork PR's contents are not scanned against secret patterns at all
        # in this mode: there is no pattern list to scan against.
        d = _repo(None, {"zzsecretzz-notes.md": "has zzsecretzz inside\n"})
        code, out = _run(d, args=["--file-types-only"])
        self.assertEqual(code, 0, out)


class ExtensionListsAgree(unittest.TestCase):
    def test_every_guarded_extension_is_also_git_ignored(self):
        with open("scripts/data-extensions.txt") as fh:
            guarded = {l.strip() for l in fh if l.strip() and not l.startswith("#")}
        with open(".gitignore") as fh:
            ignored = {l.strip().lstrip("*") for l in fh if l.strip().startswith("*.")}
        missing = sorted(e.lstrip("*") for e in guarded if e.lstrip("*") not in ignored)
        self.assertEqual(missing, [],
                         "guarded but not git-ignored: %s" % missing)
