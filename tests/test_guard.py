"""The guard is the project's load-bearing safety mechanism and has failed three
times in ways code review missed. These tests exercise it as a black box."""
import os
import shutil
import subprocess
import tempfile
import unittest

GUARD = os.path.abspath("scripts/check_leaks")
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


def _fake_grep_dir(fail_exact_args):
    """A directory containing a `grep` shim, meant to be put first on PATH.
    grep is used pervasively throughout the guard (pattern matching,
    filename matching, every count in this file), so this matches an EXACT
    argument string, not a substring, to fail only one specific invocation;
    everything else passes straight through to the real grep. Resolved via
    shutil.which rather than a bare "grep" exec, so this cannot recurse into
    itself even if something in the environment shadows the name."""
    real_grep = shutil.which("grep")
    d = tempfile.mkdtemp()
    script = os.path.join(d, "grep")
    with open(script, "w") as fh:
        fh.write("#!/usr/bin/env bash\n")
        fh.write('if [ "$*" = "%s" ]; then\n' % fail_exact_args)
        fh.write('  echo "fake-grep: simulated failure for: $*" >&2\n')
        fh.write("  exit 2\n")
        fh.write("fi\n")
        fh.write('exec "%s" "$@"\n' % real_grep)
    os.chmod(script, 0o755)
    return d


def _fake_git_dir_with_commit_side_effect(repo_dir, trigger_args, new_filename, new_file_content):
    """A `git` shim that, the FIRST time it is invoked with exactly
    `trigger_args`, commits a new tracked file into `repo_dir` (using the
    real git directly, to avoid recursing into this shim) as a side effect,
    before passing that same invocation through to the real git normally.
    Every other invocation, and every later one matching `trigger_args`
    again, passes straight through with no side effect (guarded by a marker
    file so it fires exactly once).

    This proves whether a consumer *later* in the script's execution sees a
    file added *during* the run: no timing or threading needed; the side
    effect is synchronous, fired by whichever git call the test picks as the
    "during the scan" moment."""
    real_git = shutil.which("git")
    d = tempfile.mkdtemp()
    script = os.path.join(d, "git")
    marker = os.path.join(d, ".fired")
    with open(script, "w") as fh:
        fh.write("#!/usr/bin/env bash\n")
        fh.write('if [ "$*" = "%s" ] && [ ! -f "%s" ]; then\n' % (trigger_args, marker))
        fh.write('  touch "%s"\n' % marker)
        fh.write('  printf %%s "%s" > "%s"\n' % (new_file_content, os.path.join(repo_dir, new_filename)))
        fh.write('  "%s" -C "%s" add -f "%s" >&2\n' % (real_git, repo_dir, new_filename))
        fh.write('  "%s" -C "%s" commit -q -m "added mid-scan" >&2\n' % (real_git, repo_dir))
        fh.write("fi\n")
        fh.write('exec "%s" "$@"\n' % real_git)
    os.chmod(script, 0o755)
    return d


def _guard_with_ext_lists(missing=(), empty=(), whitespace_only=(), raw_content=None,
                           include_lib=True):
    """Copy the real guard script and its two extension lists into a fresh
    directory, then delete, empty, or overwrite specific list(s) with exact
    content, and return the path to the copy. The guard resolves
    scripts/*-extensions.txt (and scripts/lib/leakpatterns.py) relative to
    its OWN location (not the repo it is scanning), so a test that wants to
    exercise a missing, empty, or corrupted list must not touch the real
    checkout - it must run a copy of the script from a directory it
    controls instead.

    `raw_content`, if given, maps a list filename to exact text to write
    verbatim (UTF-8 encoded) - used for byte-level corruption cases (a
    missing trailing newline, a leading byte-order mark) that `empty` and
    `whitespace_only` cannot express.

    `include_lib=False` omits scripts/lib/leakpatterns.py from the copy, for
    exercising the guard's own fail-closed behaviour when its shared
    pattern-loading dependency is missing (see PatternsLibFailClosed)."""
    d = tempfile.mkdtemp()
    shutil.copy(GUARD, os.path.join(d, "check_leaks"))
    src_dir = os.path.dirname(GUARD)
    raw_content = raw_content or {}
    if include_lib:
        os.makedirs(os.path.join(d, "lib"), exist_ok=True)
        shutil.copy(os.path.join(src_dir, "lib", "leakpatterns.py"),
                    os.path.join(d, "lib", "leakpatterns.py"))
    for name in EXT_LIST_NAMES:
        if name in missing:
            continue  # do not copy: simulates a deleted/renamed list
        dst = os.path.join(d, name)
        if name in raw_content:
            with open(dst, "wb") as fh:
                fh.write(raw_content[name].encode("utf-8"))
            continue
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
    return os.path.join(d, "check_leaks")


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


class AllowedTrackedFiles(unittest.TestCase):
    """`*.yml` is a data file type, deny-by-default, because a .yml is a
    perfectly good place for library data to hide. A handful of specific
    configuration files the project cannot work without are named back in
    (ALLOWED_TRACKED_PATTERNS). Both halves are pinned here: an allow-list
    that stops allowing wedges the build, and one that quietly widens to
    every .yml is the guard failing open."""

    def test_the_named_config_files_are_allowed(self):
        d = _repo(None, {
            "mkdocs.yml": "site_name: x\n",
            "pyproject.toml": "[project]\nname = 'x'\n",
            "uv.lock": "version = 1\n",
            ".github/workflows/ci.yml": "name: ci\n",
            "config/adapters.example.json": "{}\n",
        })
        code, out = _run(d, args=["--file-types-only"])
        self.assertEqual(code, 0, out)

    def test_any_other_yml_is_still_a_tracked_data_file(self):
        # The widening is one filename, not the extension. A .yml that is not
        # on the list must still fail, or the entry above has quietly turned
        # off the rule for every YAML file in the repo.
        d = _repo(None, {"a.md": "harmless\n"})
        with open(os.path.join(d, "library.yml"), "w") as fh:
            fh.write("scenes:\n  - one\n")
        subprocess.check_call(["git", "add", "-f", "library.yml"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, args=["--file-types-only"])
        self.assertEqual(code, 1, out)
        self.assertIn("tracked data file: library.yml", out)

    def test_the_allowance_is_anchored_to_the_repository_root(self):
        # fnmatchcase on the full path, not on the basename: a data file
        # parked in a subdirectory must not inherit the root allowance by
        # borrowing its name.
        d = _repo(None, {"a.md": "harmless\n"})
        os.makedirs(os.path.join(d, "elsewhere"), exist_ok=True)
        with open(os.path.join(d, "elsewhere", "mkdocs.yml"), "w") as fh:
            fh.write("scenes:\n  - one\n")
        subprocess.check_call(["git", "add", "-f", "elsewhere/mkdocs.yml"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, args=["--file-types-only"])
        self.assertEqual(code, 1, out)
        self.assertIn("tracked data file: elsewhere/mkdocs.yml", out)


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


class DeletedTrackedFileIsReportedClearly(unittest.TestCase):
    """A plain `rm` of a tracked file - the deletion neither staged nor
    committed - is an ordinary working-tree state. It used to fail closed
    with the same message as a genuine permission problem ("file is not
    readable"), which misdescribes what happened and trains people to
    distrust or skip the guard. This still fails closed (the working tree
    cannot be scanned for a file that is not there), but with a message
    that names the actual cause."""

    def test_a_deleted_tracked_file_gets_its_own_message_not_unreadable(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n", "b.md": "also harmless\n"})
        os.remove(os.path.join(d, "b.md"))
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        self.assertNotEqual(code, 0, out)
        self.assertIn("no longer exists on disk", out)
        self.assertIn("b.md", out)
        self.assertNotIn("file is not readable", out)

    def test_a_genuinely_unreadable_file_still_gets_the_permission_message(self):
        # The two causes must stay distinguishable: this fixture leaves the
        # file in place (chmod 000, not deleted), so the new "no longer
        # exists" branch must not swallow this case too.
        if os.geteuid() == 0:
            self.skipTest("permission bits do not apply to root")
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        path = os.path.join(d, "a.md")
        os.chmod(path, 0o000)
        try:
            code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"})
        finally:
            os.chmod(path, 0o644)
        self.assertNotEqual(code, 0, out)
        self.assertIn("file is not readable", out)
        self.assertNotIn("no longer exists on disk", out)


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


class PatternListCorruptionVectors(unittest.TestCase):
    """The re-review that fixed load_ext_list's whitespace-only-line vector
    asked whether the pattern list has equivalent coverage for the SAME two
    vectors that then hit load_ext_list a second time (a missing trailing
    newline, a leading BOM), since the sed cross-check might not catch a
    missing trailing newline. It already handled one and needed a fix for
    the other:

    - Missing trailing newline: NOT a bug. `raw_patterns` is read via
      `$(cat ...)` (or the LEAK_PATTERNS variable directly), and command
      substitution captures a file's last line whether or not it ends in a
      newline - unlike a `while read` loop reading a file directly, which
      is what actually dropped the last line in load_ext_list. The first
      test below is a regression proof of this, not a fix.
    - Leading BOM: a real, unfixed vector until this round - a BOM is not
      whitespace, so it survived the sed trim and glued itself to the front
      of the first pattern, silently corrupting it into something that
      could never match. Now stripped the same way as the extension lists."""

    def test_a_missing_trailing_newline_on_the_pattern_file_does_not_drop_the_last_pattern(self):
        d = _repo(["zzfirstpattern"], {"b.md": "contains zzsecretzz\n"})
        with open(os.path.join(d, ".leak-patterns"), "wb") as fh:
            fh.write(b"zzfirstpattern\nzzsecretzz")  # no trailing newline
        code, out = _run(d)
        self.assertEqual(code, 1, out)

    def test_a_leading_bom_on_the_pattern_file_does_not_corrupt_the_first_pattern(self):
        d = _repo(["placeholder"], {"b.md": "contains zzsecretzz\n"})
        with open(os.path.join(d, ".leak-patterns"), "wb") as fh:
            fh.write("﻿zzsecretzz\n".encode("utf-8"))
        code, out = _run(d)
        self.assertEqual(code, 1, out)


class CountCrossCheckFailsClosed(unittest.TestCase):
    """The `grep -c` calls that produce the independent candidate count (in
    load_ext_list) and the input/survived counts (in pattern-list
    preprocessing) were not exit-status-checked. If one failed, it yielded
    an empty string, the numeric `[ -ne ]` comparison threw a non-fatal
    bash "integer expression expected" error to stderr, and the
    mismatch-abort branch was silently skipped - execution continuing past
    the very check meant to catch corruption. Reproduced directly with a
    stub `grep` that fails one exact invocation and passes everything else
    through untouched (grep is used pervasively elsewhere in the guard, so
    an exact-argument match is required, not a substring one)."""

    def test_a_failing_grep_during_the_extension_list_candidate_count_aborts(self):
        fake_grep_dir = _fake_grep_dir(
            "-a -c -v -e ^[[:space:]]*# -e ^[[:space:]]*$")
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={
            "LEAK_PATTERNS": "zzsecretzz",
            "PATH": fake_grep_dir + os.pathsep + os.environ["PATH"],
        })
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)

    def test_a_failing_grep_during_the_pattern_list_input_count_aborts(self):
        fake_grep_dir = _fake_grep_dir("-a -c ^")
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={
            "LEAK_PATTERNS": "zzsecretzz",
            "PATH": fake_grep_dir + os.pathsep + os.environ["PATH"],
        })
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)


class Section5SeesFreshState(unittest.TestCase):
    """Section 5 (the data/media extension check) runs last, after every
    pattern-based leg. An earlier version of this script took one
    tracked-file snapshot near the top and reused it everywhere, including
    in section 5 - so a file added to the index partway through a run was
    invisible to section 5's check even though it was genuinely tracked by
    the time section 5 actually ran. `_fake_git_dir_with_commit_side_effect`
    forces that addition to happen synchronously, during an early git call,
    so this is deterministic and needs no timing or threading."""

    def test_a_data_file_added_during_the_scan_is_still_caught_by_section_5(self):
        d = _repo(["zznomatchzz"], {"a.md": "harmless\n"})
        fake_dir = _fake_git_dir_with_commit_side_effect(
            d, "rev-list --all", "secrets.env", "X=1\n")
        code, out = _run(d, env={
            "LEAK_PATTERNS": "zznomatchzz",
            "PATH": fake_dir + os.pathsep + os.environ["PATH"],
        })
        self.assertEqual(code, 1, out)
        self.assertIn("tracked data file: secrets.env", out)


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

    def test_a_missing_trailing_newline_does_not_drop_the_last_entry(self):
        # `while read` silently skips a final line with no trailing
        # newline. This is the invariant check working, not a guess about
        # this one vector: the independent candidate-line count catches
        # ANY line that fails to become a usable entry, not just this one.
        guard = _guard_with_ext_lists(raw_content={"data-extensions.txt": "*.json\n*.env"})
        d = _repo(["zzpatternmatchesnothingzz"], {"a.md": "harmless\n"})
        with open(os.path.join(d, "secrets.env"), "w") as fh:
            fh.write("X=1\n")
        subprocess.check_call(["git", "add", "-f", "secrets.env"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, env={"LEAK_PATTERNS": "zzpatternmatchesnothingzz"}, guard=guard)
        # The fix repairs this vector entirely (the entry loads correctly),
        # rather than merely detecting corruption and aborting.
        self.assertEqual(code, 1, out)
        self.assertIn("tracked data file: secrets.env", out)

    def test_a_leading_bom_does_not_corrupt_the_first_entry(self):
        # A byte-order mark is not whitespace, so it survives trimming and
        # would otherwise glue itself to the front of the first real glob,
        # silently corrupting it into something that can never match.
        guard = _guard_with_ext_lists(raw_content={
            "data-extensions.txt": "﻿*.env\n*.json\n",
        })
        d = _repo(["zzpatternmatchesnothingzz"], {"a.md": "harmless\n"})
        with open(os.path.join(d, "secrets.env"), "w") as fh:
            fh.write("X=1\n")
        subprocess.check_call(["git", "add", "-f", "secrets.env"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, env={"LEAK_PATTERNS": "zzpatternmatchesnothingzz"}, guard=guard)
        self.assertEqual(code, 1, out)
        self.assertIn("tracked data file: secrets.env", out)

    def test_an_entry_that_is_not_a_glob_is_a_failure(self):
        # A corrupted entry that survives comment/blank stripping but does
        # not look like *.something can never match a real filename via the
        # case-statement matching in section 5 - silent, not loud, unless
        # this is checked explicitly.
        guard = _guard_with_ext_lists(raw_content={
            "data-extensions.txt": "*.json\nnotaglob\n*.env\n",
        })
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)
        self.assertIn("not a glob", out)

    def test_an_inline_comment_loads_correctly_rather_than_corrupting_the_entry(self):
        # A line like "*.env # note" passed the glob-shape check as-is (it
        # starts with * and contains a .) while being stored as the
        # literal, never-matching string "*.env # note" - the independent
        # count and the loaded-entry count then agreed with each other
        # while both were wrong, since this one line still became exactly
        # one "entry" either way. Reproduced directly: a tracked
        # secrets.env went uncaught with this line present, "check_leaks:
        # clean", exit 0. Chosen fix: strip the inline comment (the same
        # way the pattern list already does) and load the entry correctly,
        # rather than merely detecting the corruption and aborting -
        # comments are a legitimate, documented feature of these files.
        guard = _guard_with_ext_lists(raw_content={
            "data-extensions.txt": "*.env # note\n*.json\n",
        })
        d = _repo(["zzpatternmatchesnothingzz"], {"a.md": "harmless\n"})
        with open(os.path.join(d, "secrets.env"), "w") as fh:
            fh.write("X=1\n")
        subprocess.check_call(["git", "add", "-f", "secrets.env"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add data file"], cwd=d)
        code, out = _run(d, env={"LEAK_PATTERNS": "zzpatternmatchesnothingzz"}, guard=guard)
        self.assertEqual(code, 1, out)
        self.assertIn("tracked data file: secrets.env", out)

    def test_an_entry_with_embedded_whitespace_is_a_failure(self):
        # No comment marker this time, so nothing strips the extra text -
        # a glob with a space in it can never match a real filename, and
        # this must fail loudly rather than silently store it.
        guard = _guard_with_ext_lists(raw_content={
            "data-extensions.txt": "*.env extra\n*.json\n",
        })
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)
        self.assertIn("embedded whitespace", out)

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


class BinaryDetectedContentIsScanned(unittest.TestCase):
    """`git grep -I` (the flag both content legs used) silently excludes
    anything git treats as binary: a plain-ASCII file `.gitattributes`
    marks `-diff` or `binary`, and any file containing a NUL byte anywhere
    in it (git's own heuristic for "is this binary"). Reproduced directly:
    committing a `.gitattributes`-marked file containing a forbidden string
    reported "check_leaks: clean", exit 0 - in the working tree AND forever
    in history, since the history leg used the same flag. The untracked
    leg already used `grep -a` (plain grep, not `git grep`), so it was never
    affected - proof this was drift, not a considered decision. Fixed by
    using `git grep -a` on both content legs, matching the untracked leg."""

    def test_a_tracked_file_marked_diff_is_still_scanned_in_the_working_tree(self):
        d = _repo(["zzsecretzz"], {
            ".gitattributes": "doc.txt -diff\n",
            "doc.txt": "harmless intro\ncontains zzsecretzz here\n",
        })
        code, out = _run(d)
        self.assertEqual(code, 1, out)
        self.assertIn("doc.txt", out)

    def test_a_tracked_file_marked_diff_is_still_scanned_in_history(self):
        d = _repo(["zzsecretzz"], {
            ".gitattributes": "doc.txt -diff\n",
            "doc.txt": "harmless intro\n",
        })
        with open(os.path.join(d, "doc.txt"), "w") as fh:
            fh.write("temporarily contains zzsecretzz\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "temp leak"], cwd=d)
        with open(os.path.join(d, "doc.txt"), "w") as fh:
            fh.write("clean again\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "remove leak"], cwd=d)
        code, out = _run(d)
        self.assertEqual(code, 1, out)
        self.assertIn("doc.txt", out)

    def test_a_tracked_file_with_a_nul_byte_is_still_scanned_in_the_working_tree(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        path = os.path.join(d, "binlike.txt")
        with open(path, "wb") as fh:
            fh.write(b"otherwise ascii \x00 contains zzsecretzz too\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "add nul-containing file"], cwd=d)
        code, out = _run(d)
        self.assertEqual(code, 1, out)
        self.assertIn("binlike.txt", out)

    def test_a_tracked_file_with_a_nul_byte_is_still_scanned_in_history(self):
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        path = os.path.join(d, "binlike.txt")
        with open(path, "wb") as fh:
            fh.write(b"otherwise ascii \x00 contains zzsecretzz too\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "temp leak with nul byte"], cwd=d)
        with open(path, "wb") as fh:
            fh.write(b"clean now, still has a \x00 byte\n")
        subprocess.check_call(["git", "add", "-A"], cwd=d)
        subprocess.check_call(["git", "commit", "-q", "-m", "remove leak"], cwd=d)
        code, out = _run(d)
        self.assertEqual(code, 1, out)
        self.assertIn("binlike.txt", out)


class PatternsLibFailClosed(unittest.TestCase):
    """scripts/check_leaks imports scripts/lib/leakpatterns.py (shared with
    scripts/hooks/commit-msg) rather than carrying its own copy of
    pattern-loading logic. A missing or unreadable shared library must
    break the build, the same as a missing pattern list or a missing
    extension list - not be silently skipped."""

    def test_a_missing_shared_library_is_a_failure(self):
        guard = _guard_with_ext_lists(include_lib=False)
        d = _repo(["zzsecretzz"], {"a.md": "harmless\n"})
        code, out = _run(d, env={"LEAK_PATTERNS": "zzsecretzz"}, guard=guard)
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("check_leaks: clean", out)
