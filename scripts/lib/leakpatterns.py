"""Shared pattern-list and glob-list loading for the leak guard and the
commit-msg hook.

This is a straight port of scripts/lib/patterns.sh plus the load_ext_list
helper that used to live inside scripts/check_leaks.sh. Both existed because
the pattern list and the two extension lists have each, independently, been
silently corrupted in ways that let a real leak through while the tool
reported "clean":

  - a leading byte-order mark glued itself to the front of the first entry,
    turning a real pattern/glob into a look-alike that could never match
  - a whitespace-only line was neither blank (by string equality) nor a
    comment, so it survived an untrimmed blank/comment test and became a
    literal, never-matching entry - while the file still "loaded" with no
    error
  - a missing trailing newline made a line-by-line shell read silently drop
    the file's last entry
  - an inline comment glued onto a real entry ("*.env # note") passed a
    naive shape test while being stored as a string that could never match
    anything

pathlib.Path.read_text()/splitlines() remove the missing-trailing-newline
class entirely: Python's own line splitting counts an unterminated final
line as a line, the same way a well-formed file's last line would be
counted. That bug class needed a whole paragraph of shell commentary
(patterns.sh's original header) precisely because `while read` line loops
and `$(cat ...)` command substitution disagreed with each other about it;
nothing here needs to reconcile that disagreement.

What is NOT removed by moving to Python: the possibility that whatever
computes "how many entries did we just load" and whatever computes "how
many candidate lines were there to begin with" share a bug and agree with
each other while both being wrong. That is exactly the failure mode the
count cross-check exists to catch, and it is only meaningful if the two
counts come from genuinely independent mechanisms. For that reason the
"candidate line" count below is still produced by shelling out to the real
`grep`, deliberately not by counting the same Python-parsed line list a
second time - a bug in this module's own line-splitting could not then
produce a self-consistent wrong answer on both sides of the check.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

BOM = "﻿"


class GuardError(Exception):
    """Raised on any failure to load a trustworthy pattern or glob list.
    The caller (scripts/check_leaks) treats every GuardError, wherever it is
    raised, as fail-closed: print the message, exit 2. Never exit 0 after
    one of these."""


def _strip_bom(text: str) -> str:
    if text.startswith(BOM):
        return text[1:]
    return text


def _run_grep(args: list[str], data: bytes, what: str) -> int:
    """Run `grep <args>` with `data` on stdin and return the count it
    reports. Exit 1 ("no match") is a legitimate zero count, not a failure -
    grep uses it for a genuinely empty result as well as a real non-match.
    Only an exit status greater than 1 is an actual grep error, and that
    must abort rather than be read as "zero"."""
    try:
        proc = subprocess.run(
            ["grep", *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        raise GuardError(f"could not run grep while {what}: {exc}") from exc
    if proc.returncode > 1:
        raise GuardError(
            f"counting {what} failed (grep exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    if proc.returncode == 1:
        return 0
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        return int(out)
    except ValueError as exc:
        raise GuardError(f"grep produced a non-numeric count while {what}: {out!r}") from exc


def _clean_pattern_lines(raw: str) -> list[str]:
    """Per-line cleaning for the pattern list: strip an inline comment,
    strip surrounding whitespace, drop the line if it is now empty.

    Deliberately does NOT reject an entry containing embedded whitespace,
    unlike load_globs' entry-building loop below. A glob must be rejected
    for embedded whitespace because a space can never be part of a real
    filename glob shape (*.ext) - but a *pattern* is matched as a literal
    fixed string (`git grep -F`), and a legitimate forbidden string is
    routinely multiple words (a person's full name, a phrase). Rejecting
    those would not catch a corrupted entry; it would refuse to load a
    real, correctly-configured multi-word pattern - confirmed directly
    against this project's own .leak-patterns file, which relies on
    exactly that."""
    entries: list[str] = []
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        entries.append(entry)
    return entries


def load_patterns(env, repo_root) -> tuple[list[str], str]:
    """Read LEAK_PATTERNS from `env` if set and non-empty, else
    `<repo_root>/.leak-patterns`. Returns (patterns, source) where source is
    "env" or "file". Raises GuardError when neither is available, when the
    file exists but is unreadable, or when the resulting list is empty
    after cleaning - "no patterns configured" is a failure here, not a
    condition the caller has to notice on its own, exactly as
    "no patterns configured is a FAILURE, not a pass" in check_leaks.sh's
    own header: a missing or renamed secret must break the build rather
    than silently disable the guard.
    """
    env_value = env.get("LEAK_PATTERNS")
    if env_value:
        raw = env_value
        source = "env"
    else:
        pattern_path = Path(repo_root) / ".leak-patterns"
        if not pattern_path.exists():
            raise GuardError(
                "no patterns configured. "
                "CI: set the LEAK_PATTERNS repository secret. "
                "Local: create .leak-patterns (git-ignored, one pattern per line)."
            )
        try:
            raw_bytes = pattern_path.read_bytes()
        except OSError as exc:
            raise GuardError(
                f"pattern file exists but could not be read: {pattern_path} ({exc})"
            ) from exc
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GuardError(f"pattern file is not valid UTF-8: {pattern_path} ({exc})") from exc
        source = "file"

    raw = _strip_bom(raw)

    # Independent candidate count: lines that are neither blank nor a pure
    # comment, counted by a mechanism (external grep) wholly separate from
    # the Python parsing below. Two grep invocations, matching the shape of
    # the two that used to live in patterns.sh: one filters out blank and
    # comment-only lines, the other counts what is left.
    filtered = subprocess.run(
        ["grep", "-a", "-v", "-e", r"^[[:space:]]*#", "-e", r"^[[:space:]]*$"],
        input=raw.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if filtered.returncode > 1:
        raise GuardError(
            "counting candidate pattern lines failed "
            f"(grep exit {filtered.returncode}): "
            f"{filtered.stderr.decode('utf-8', 'replace').strip()}"
        )
    candidate_count = _run_grep(["-a", "-c", "^"], filtered.stdout, "candidate pattern lines")

    entries = _clean_pattern_lines(raw)

    if len(entries) != candidate_count:
        raise GuardError(
            "pattern preprocessing dropped or duplicated a line: "
            f"{candidate_count} candidate line(s) but {len(entries)} pattern(s) produced."
        )

    if not entries:
        raise GuardError("no patterns configured: the pattern list is empty after cleaning.")

    return entries, source


def load_globs(path) -> list[str]:
    """Read a one-glob-per-line file (scripts/data-extensions.txt or
    scripts/media-extensions.txt), applying the same cleaning as
    load_patterns, plus a glob-shape check: every surviving entry must look
    like `*.something`, since anything else can never match a real filename
    via the glob matching check_leaks does with these lists. Raises
    GuardError if the file is missing, unreadable, contains an entry that is
    not glob-shaped, or yields no entries."""
    path = Path(path)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise GuardError(f"extension list is missing or unreadable: {path}")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise GuardError(f"extension list could not be read: {path} ({exc})") from exc
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuardError(f"extension list is not valid UTF-8: {path} ({exc})") from exc

    raw = _strip_bom(raw)

    candidate_count = _run_grep(
        ["-a", "-c", "-v", "-e", r"^[[:space:]]*#", "-e", r"^[[:space:]]*$"],
        raw.encode("utf-8"),
        f"candidate lines in extension list {path}",
    )

    entries: list[str] = []
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if any(ch.isspace() for ch in entry):
            raise GuardError(
                f"extension list has an entry containing embedded whitespace: {entry!r} ({path})"
            )
        # Glob-shape check: must start with a literal "*" and contain a "."
        # somewhere after that - the same invariant scripts/check_leaks.sh's
        # `case "$ext" in \**.*)` tested. A corrupted survivor (e.g. a stray
        # non-glob line) can never match a real filename via the fnmatch
        # matching the caller does with these entries.
        if not (entry.startswith("*") and "." in entry[1:]):
            raise GuardError(
                f"extension list has an entry that is not a glob: {entry!r} ({path})"
            )
        entries.append(entry)

    if len(entries) != candidate_count:
        raise GuardError(
            f"extension list {path}: {candidate_count} candidate line(s) "
            f"but {len(entries)} usable entry(ies) were loaded."
        )

    if not entries:
        raise GuardError(f"extension list has no usable entries: {path}")

    return entries
