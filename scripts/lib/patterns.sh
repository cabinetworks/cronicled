#!/usr/bin/env bash
# Shared pattern-list loading for the leak guard and the commit-msg hook.
#
# This logic used to be duplicated: ~60 lines of pattern loading,
# preprocessing, and cross-checking copied into both scripts/check_leaks.sh
# and scripts/hooks/commit-msg. The duplication was the actual defect - it
# drifted twice, silently, in two different ways (a BOM strip added to one
# copy and never ported to the other; a count cross-check's own exit-status
# check added to one copy and never ported to the other). Fixing the drift a
# third time would only set up a fourth. There is now exactly one copy of
# this mechanism; both callers source this file and call load_leak_patterns.
#
# This file is a library, not an executable: sourced only, never run
# directly. It must not `exit` on failure - each caller has its own exit
# code and its own message vocabulary ("check_leaks: FAILED", exit 2, vs
# "commit-msg: BLOCKED", exit 1), and a library that unilaterally ends its
# caller's process is a worse coupling than the duplication it replaces.
# Instead, load_leak_patterns returns non-zero and prints a message (via
# $PATTERNS_LIB_PREFIX, so each caller's own wording survives) to stderr;
# the caller is responsible for treating ANY non-zero return as fail-closed.
#
# Governing rule (same one scripts/check_leaks.sh's header spells out): every
# read of external state must either succeed or abort. This file must not be
# the one place that rule is relaxed just because the logic is now shared.

# load_leak_patterns
#
# Reads LEAK_PATTERNS (env) if set and non-empty, else .leak-patterns (a
# file, read relative to the CALLER's current directory - both callers rely
# on running with the repository root as cwd: scripts/check_leaks.sh cd's
# there explicitly before sourcing this file, and a real git hook is invoked
# by git with the top of the working tree as cwd already).
#
# Strips a leading byte-order mark (not whitespace, so it would otherwise
# survive the trim below and glue itself to the front of the first pattern,
# silently weakening that one pattern to something that can never match),
# then preprocesses with sed: comments stripped, leading/trailing whitespace
# trimmed, blank lines dropped.
#
# Two independent checks guard the preprocessing step, and either one
# failing returns non-zero rather than continuing with a possibly-truncated
# list:
#   1. sed's own exit status, captured immediately - BSD sed (macOS, and
#      therefore any contributor's laptop) aborts mid-stream on a byte its
#      locale cannot parse, but has already written every line before the
#      bad byte to stdout, so a naive capture would silently keep a
#      truncated list with no hint anything was dropped.
#   2. A line-count cross-check: the number of non-empty, non-comment lines
#      going in must equal the number of patterns coming out. Both counts'
#      own exit status is checked immediately too - these `grep -c` calls
#      are themselves a read of external state, and this whole mechanism
#      exists to catch corruption, so it must not be exempt from its own
#      rule. A failed count previously yielded an empty string, the numeric
#      comparison threw a non-fatal "integer expression expected" error to
#      stderr, and the mismatch-abort was silently skipped - execution
#      continuing past the very check meant to enforce this. Exit 1 ("no
#      lines matched") is a legitimate zero count, not a failure; only >1 is
#      an actual grep error.
#
# A missing trailing newline on the pattern source needs no separate
# handling here: raw_patterns is read via `$(cat ...)` (or the LEAK_PATTERNS
# variable directly), and command substitution captures a file's last line
# whether or not it ends in a newline - unlike a `while read` loop reading a
# file directly, which would drop an unterminated final line silently.
#
# On success: sets `patterns` (cleaned, one per line) and `patterns_source`
# ("env" or "file"), and returns 0. Deliberately does NOT check whether the
# resulting pattern list is empty - "no patterns configured" is a condition
# both callers already treat as a failure, but they disagree on the wording,
# so each checks `[ -z "$patterns" ]` itself after calling this.
load_leak_patterns() {
	local prefix="${PATTERNS_LIB_PREFIX:-patterns.sh}"

	patterns=""
	patterns_source=none
	if [ -n "${LEAK_PATTERNS:-}" ]; then
		patterns=$LEAK_PATTERNS
		patterns_source=env
	elif [ -f .leak-patterns ]; then
		patterns=$(cat .leak-patterns)
		patterns_source=file
	fi
	raw_patterns=$patterns

	local bom
	bom=$'\xef\xbb\xbf'
	raw_patterns="${raw_patterns#"$bom"}"

	local input_count input_count_rc
	input_count=$(printf '%s\n' "$raw_patterns" | grep -a -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' | grep -a -c '^')
	input_count_rc=$?
	if [ "$input_count_rc" -gt 1 ]; then
		echo "$prefix counting candidate pattern lines failed (exit $input_count_rc)." >&2
		echo "  Refusing to proceed without a trustworthy count to cross-check preprocessing against." >&2
		return 1
	fi

	patterns=$(printf '%s\n' "$raw_patterns" | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d')
	local sed_status=$?
	if [ "$sed_status" -ne 0 ]; then
		echo "$prefix pattern preprocessing exited $sed_status." >&2
		echo "  A byte in the pattern source could not be parsed by this platform's sed," >&2
		echo "  which aborts mid-stream and silently truncates the pattern list." >&2
		echo "  Refusing to proceed with a possibly-truncated pattern list." >&2
		return 1
	fi

	local survived_count survived_count_rc
	survived_count=$(printf '%s\n' "$patterns" | grep -a -c '^.')
	survived_count_rc=$?
	if [ "$survived_count_rc" -gt 1 ]; then
		echo "$prefix counting surviving patterns failed (exit $survived_count_rc)." >&2
		echo "  Refusing to proceed without a trustworthy count to cross-check preprocessing against." >&2
		return 1
	fi
	if [ "$survived_count" -ne "$input_count" ]; then
		echo "$prefix pattern preprocessing dropped lines silently." >&2
		echo "  $survived_count pattern(s) survived out of $input_count supplied." >&2
		echo "  Refusing to proceed with a possibly-truncated pattern list." >&2
		return 1
	fi

	return 0
}
