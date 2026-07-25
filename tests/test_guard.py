"""The guard is the project's load-bearing safety mechanism and has failed three
times in ways code review missed. These tests exercise it as a black box."""
import os
import shutil
import subprocess
import tempfile
import unittest

GUARD = os.path.abspath("scripts/check_leaks.sh")
EXT_LIST_NAMES = ("data-extensions.txt", "media-extensions.txt")


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


def _run(cwd, env=None, args=None, guard=GUARD):
    e = dict(os.environ)
    e.pop("LEAK_PATTERNS", None)
    e.update(env or {})
    p = subprocess.run([guard] + (args or []), cwd=cwd, env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _fake_git_dir(fail_match):
    """A directory containing a `git` shim, meant to be put first on PATH.
    It fails (non-zero exit, a message on stderr) only invocations whose
    full argument string contains `fail_match`; every other invocation is
    passed straight through to the real git. This proves a *specific* git
    call's failure is handled correctly without needing to actually corrupt
    a repository to make that call fail for real."""
    real_git = shutil.which("git")
    d = tempfile.mkdtemp()
    script = os.path.join(d, "git")
    with open(script, "w") as fh:
        fh.write("#!/usr/bin/env bash\n")
        fh.write('case "$*" in\n')
        fh.write('*"%s"*)\n' % fail_match)
        fh.write('  echo "fake-git: simulated failure for: $*" >&2\n')
        fh.write("  exit 128 ;;\n")
        fh.write("esac\n")
        fh.write('exec "%s" "$@"\n' % real_git)
    os.chmod(script, 0o755)
    return d


def _guard_with_ext_lists(missing=(), empty=(), whitespace_only=()):
    """Copy the real guard script and its two extension lists into a fresh
    directory, then delete or empty specific list(s), and return the path to
    the copy. The guard resolves scripts/*-extensions.txt relative to its
    OWN location (not the repo it is scanning), so a test that wants to
    exercise a missing or empty list must not touch the real checkout - it
    must run a copy of the script from a directory it controls instead."""
    d = tempfile.mkdtemp()
    shutil.copy(GUARD, os.path.join(d, "check_leaks.sh"))
    src_dir = os.path.dirname(GUARD)
    for name in EXT_LIST_NAMES:
        if name in missing:
            continue  # do not copy: simulates a deleted/renamed list
        dst = os.path.join(d, name)
        if name in empty:
            with open(dst, "w") as fh:
                fh.write("# only a comment\n\n")
            continue
        if name in whitespace_only:
            # Not blank by string comparison, not a comment either - the
            # exact case that dodged the un-trimmed blank/comment test.
            with open(dst, "w") as fh:
                fh.write("# only a comment\n   \n")
            continue
        shutil.copy(os.path.join(src_dir, name), dst)
    return os.path.join(d, "check_leaks.sh")


class OutputDiscipline(unittest.TestCase):
    # These use LEAK_PATTERNS (the CI secret path), not the local file: CI
    # logs are public, so this is where redaction is guaranteed by default.
    # See RedactionBySource below for the file-mode and --redact behaviour.
    def test_a_matching_filename_does_not_print_the_pattern(self):
        # CI logs are public: reporting the offending name would republish the
        # very string the guard exists to keep private
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "clean body\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertNotEqual(code, 0)
        self.assertNotIn("zzsecretzz", out)

    def test_it_still_says_how_many_filenames_matched(self):
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "clean body\n"})
        _, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
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


class RedactionBySource(unittest.TestCase):
    """Round 1 redacted the filename legs but left the content legs printing
    paths verbatim - and a path can coincidentally contain the pattern even
    when a *content* leg is what matched, whenever a file's name and body
    both mention it. The brief's Step 1 fixture used a clean body precisely
    so name-match and content-match stayed disjoint, which is why nothing in
    that suite caught it. These fixtures deliberately let both match the
    same file."""

    def test_env_sourced_patterns_redact_a_combined_filename_and_content_match(self):
        # The exact regression: one file whose NAME and BODY both match.
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "belongs to zzsecretzz too\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertEqual(code, 1, out)
        self.assertNotIn("zzsecretzz", out)

    def test_env_sourced_patterns_redact_an_untracked_match(self):
        d = _repo(["zzsecretzz"], {"a.md": "clean\n"})
        with open(os.path.join(d, "zzsecretzz-scratch.md"), "w") as fh:
            fh.write("body also has zzsecretzz\n")
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertEqual(code, 1, out)
        self.assertNotIn("zzsecretzz", out)

    def test_env_sourced_patterns_redact_a_history_match(self):
        d = _repo(["zzsecretzz"], {"a.md": "clean\n"})
        with open(os.path.join(d, "a.md"), "w") as fh:
            fh.write("temporarily contains zzsecretzz\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "temp leak"], cwd=d)
        with open(os.path.join(d, "a.md"), "w") as fh:
            fh.write("clean again\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "remove leak"], cwd=d)
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertEqual(code, 1, out)
        self.assertNotIn("zzsecretzz", out)

    def test_file_sourced_patterns_show_the_matched_path(self):
        # A developer's own terminal is not a public log: a guard that will
        # not say what it found is not usable for local runs.
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "belongs to zzsecretzz too\n"})
        code, out = _run(d)  # no LEAK_PATTERNS -> falls back to .leak-patterns
        self.assertEqual(code, 1, out)
        self.assertIn("zzsecretzz-notes.md", out)

    def test_redact_flag_forces_redaction_even_in_file_mode(self):
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "belongs to zzsecretzz too\n"})
        code, out = _run(d, args=["--redact"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("zzsecretzz", out)


class UnreadableFileFailsClosed(unittest.TestCase):
    """`git grep` and plain `grep` both report "no match" (their normal exit
    code for a genuinely clean result) for a file they cannot open at all -
    the same shape of bug as the extension list, one layer down: a read
    that failed must abort, not read as clean. `chmod 000` reproduces this
    directly, no simulation needed."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits do not apply to root")

    def test_an_unreadable_tracked_file_with_an_uncommitted_leak_aborts(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        path = os.path.join(d, "a.md")
        with open(path, "w") as fh:
            fh.write("contains zzsecretzz now\n")
        os.chmod(path, 0o000)
        try:
            code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        finally:
            os.chmod(path, 0o644)
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)

    def test_an_unreadable_untracked_file_containing_the_pattern_aborts(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        path = os.path.join(d, "scratch.md")
        with open(path, "w") as fh:
            fh.write("contains zzsecretzz\n")
        os.chmod(path, 0o000)
        try:
            code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        finally:
            os.chmod(path, 0o644)
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)


class GitFailureFailsClosed(unittest.TestCase):
    """Re-review confirmed clean-exit-on-failure for `git ls-files` (tracked
    and untracked filename legs, and the file-type section) and for
    `git log -1 --format=%B` (the commit-message leg), and rejected the
    reasoning that these need actual repository corruption to matter. A
    fake `git` on PATH forces one specific invocation to fail without
    needing real corruption."""

    def test_tracked_ls_files_failure_aborts(self):
        d = _repo(["zzsecretzz"], {"zzsecretzz-notes.md": "clean body\n"})
        fake_dir = _fake_git_dir("ls-files -z")
        code, out = _run(d, env={
            "LEAK_PATTERNS": "zzsecretzz",
            "PATH": fake_dir + os.pathsep + os.environ["PATH"],
        })
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)

    def test_untracked_ls_files_failure_aborts(self):
        d = _repo(["zzsecretzz"], {"a.md": "clean\n"})
        with open(os.path.join(d, "zzsecretzz-scratch.md"), "w") as fh:
            fh.write("contains zzsecretzz\n")
        fake_dir = _fake_git_dir("--others --exclude-standard -z")
        code, out = _run(d, env={
            "LEAK_PATTERNS": "zzsecretzz",
            "PATH": fake_dir + os.pathsep + os.environ["PATH"],
        })
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)

    def test_git_log_failure_during_commit_message_scan_aborts(self):
        # No other leg matches here: only the commit message does, so if the
        # failed `git log` call were silently read as "message not found",
        # this scenario alone must not exit 0.
        d = _repo(["zzsecretzz"], {"a.md": "clean\n"})
        subprocess.check_call(["git", "commit", "--allow-empty", "-q",
                                "-m", "mentions zzsecretzz in the message"], cwd=d)
        fake_dir = _fake_git_dir("log -1 --format=%B")
        code, out = _run(d, env={
            "LEAK_PATTERNS": "zzsecretzz",
            "PATH": fake_dir + os.pathsep + os.environ["PATH"],
        })
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)


class ExtensionListFailClosed(unittest.TestCase):
    """Extracting the extension lists to scripts/*.txt (round 2) introduced a
    new instance of this file's recurring defect class: a failure to load
    external configuration must abort, not continue. A missing or unreadable
    list previously printed a shell error to stderr and execution continued
    straight to `check_leaks: clean`, exit 0 - with a tracked .env file
    sitting right there. Loading these lists is now held to the same
    standard as loading the pattern list."""

    def test_missing_data_extensions_file_is_a_failure(self):
        guard = _guard_with_ext_lists(missing=("data-extensions.txt",))
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)

    def test_missing_media_extensions_file_is_a_failure(self):
        guard = _guard_with_ext_lists(missing=("media-extensions.txt",))
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)

    def test_a_comments_and_blanks_only_list_is_a_failure(self):
        # Present and readable is not enough: zero usable entries is not a
        # legitimately empty ruleset, it is a sign the file lost its content.
        guard = _guard_with_ext_lists(empty=("data-extensions.txt",))
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)

    def test_a_whitespace_only_line_does_not_count_as_a_usable_entry(self):
        # The exact miss from the re-review: a line of only spaces is not
        # "" by string comparison and is not a comment, so untrimmed it
        # entered the array as a literal glob that can never match a real
        # filename - making the array non-empty and defeating this abort.
        guard = _guard_with_ext_lists(whitespace_only=("data-extensions.txt",))
        d = _repo(["zzpatternmatchesnothingzz"], {"a.md": "harmless\n"})
        with open(os.path.join(d, "secrets.env"), "w") as fh:
            fh.write("X=1\n")
        subprocess.check_call(["git", "add", "-f", "secrets.env"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, env={"LEAK_PATTERNS": "zzpatternmatchesnothingzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)

    def test_file_types_only_mode_fails_closed_on_a_missing_list_too(self):
        # This mode exists because patterns are legitimately absent on a
        # fork PR; the extension lists are never legitimately absent, and
        # the extension check is the ONLY thing this mode runs. If its one
        # check cannot load, exiting 0 would silence the entire run.
        guard = _guard_with_ext_lists(missing=("data-extensions.txt",))
        d = _repo(None, {"a.md": "harmless\n"})
        code, out = _run(d, args=["--file-types-only"], guard=guard)
        self.assertNotEqual(code, 0, out)

    def test_the_reviewers_exact_repro_now_fails(self):
        # Patterns that match nothing, a tracked .env planted, and the data
        # extension list deleted. Before this fix: "check_leaks: clean", 0.
        guard = _guard_with_ext_lists(missing=("data-extensions.txt",))
        d = _repo(["zzpatternmatchesnothingzz"], {"a.md": "harmless\n"})
        with open(os.path.join(d, "secrets.env"), "w") as fh:
            fh.write("X=1\n")
        subprocess.check_call(["git", "add", "-f", "secrets.env"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, env={"LEAK_PATTERNS": "zzpatternmatchesnothingzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)

    def test_both_lists_present_and_a_clean_repo_still_exits_zero(self):
        # Regression guard on the copy-and-mutate machinery itself: nothing
        # missing must not itself become a false failure.
        guard = _guard_with_ext_lists()
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertEqual(code, 0, out)


class ExtensionListsAgree(unittest.TestCase):
    # Extensions permitted to exist in only one list, with the reason.
    ALLOWLIST_GUARDED_ONLY = frozenset()
    # .pyc and .egg-info/ are Python build hygiene (bytecode caches, packaging
    # metadata), ignored so a fresh venv/build doesn't dirty `git status` -
    # not data that could ever be confidential, so the leak guard's file-type
    # check has no reason to know about them.
    ALLOWLIST_IGNORED_ONLY = frozenset({".pyc", ".egg-info/"})

    @staticmethod
    def _bare_extensions(path):
        with open(path) as fh:
            return {l.strip().lstrip("*") for l in fh if l.strip() and not l.startswith("#")}

    @classmethod
    def _guarded(cls):
        return (cls._bare_extensions("scripts/data-extensions.txt")
                | cls._bare_extensions("scripts/media-extensions.txt"))

    @staticmethod
    def _ignored():
        with open(".gitignore") as fh:
            return {l.strip().lstrip("*") for l in fh if l.strip().startswith("*.")}

    def test_every_guarded_extension_is_also_git_ignored(self):
        missing = sorted(self._guarded() - self._ignored() - self.ALLOWLIST_GUARDED_ONLY)
        self.assertEqual(missing, [],
                         "guarded but not git-ignored: %s" % missing)

    def test_every_git_ignored_extension_is_also_guarded(self):
        # The reverse direction matters just as much: an extension .gitignore
        # excludes but the guard's file-type check does not know about is
        # invisible to that check, so a `git add -f` of such a file slips
        # past it entirely.
        missing = sorted(self._ignored() - self._guarded() - self.ALLOWLIST_IGNORED_ONLY)
        self.assertEqual(missing, [],
                         "git-ignored but not guarded: %s" % missing)
