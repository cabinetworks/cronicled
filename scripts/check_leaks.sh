#!/usr/bin/env bash
# Fail the build if anything that must never be public has entered the repo.
#
# Scans, for every configured pattern:
#   (a) the working tree: tracked file CONTENTS and tracked FILENAMES
#   (b) every commit reachable in history: tracked file CONTENTS as they
#       existed at that commit, even if later removed
#   (c) untracked-but-not-ignored files: FILENAMES and CONTENTS
#   (d) every commit message in history
#
# The pattern list is deliberately NOT stored here. A committed list would
# enumerate exactly what must stay private — a worse disclosure than any single
# string it guards against. Supply it out of band:
#
#   LEAK_PATTERNS    newline-separated, from a CI secret
#   .leak-patterns   one per line, git-ignored, for local runs
#
# No patterns configured is a FAILURE, not a pass: a missing or renamed secret
# must break the build rather than silently disable the guard.
#
# --- The governing rule for every read below --------------------------------
# Every read of external state - a file, an environment variable, a command
# substitution, a pipeline - must either succeed or abort. No code path may
# exit 0 after a read that failed. "Could not determine" is not "clean".
# This file has needed that rule spelled out explicitly because it has been
# violated three different ways by three different fixes: pattern
# preprocessing silently truncated by a byte BSD sed's locale rejected; an
# extracted extension list that was simply absent; and, most subtly, `grep`
# and `git grep` themselves reporting "no match" (their normal exit code for
# a clean result) for a file they could not even open - which reads
# identically to a clean result unless the underlying read is checked
# directly rather than inferred from grep's exit code alone. Every git/grep
# invocation below either distinguishes "ran successfully and found nothing"
# from "could not run", or is preceded by an explicit check that lets it.
#
# Output discipline: paths and matched names are shown or redacted depending
# on where the patterns came from, never on which leg matched.
#
#   LEAK_PATTERNS (CI secret) -> redact by default: every leg reports a
#   count, plus commit SHAs where relevant (a SHA is not sensitive), and
#   NEVER a path or a matched name. CI logs are public, and a path can
#   coincidentally contain the pattern even when a *content* leg is what
#   matched it (e.g. a file whose name and body both mention it) - so every
#   leg redacts, not just the filename legs.
#
#   .leak-patterns (local file) -> show paths and names by default: a
#   developer's own terminal is not a public log, and a guard that will not
#   say what it found is not usable.
#
#   --redact forces the CI behaviour even when reading the local file, so
#   this can be exercised directly rather than only implied by which source
#   happened to supply the patterns.
#
# --file-types-only skips every pattern-based leg (checks 1-4 below) and
# does not fail when no patterns are configured, because none are expected in
# that mode. It exists for fork pull requests, which cannot read the
# LEAK_PATTERNS secret. Every other invocation keeps failing closed.
set -u

# The extension lists ship alongside this script, not inside whatever repo is
# being scanned - resolve it from $0's own location before anything below
# changes directory. Checked, not assumed: if this fails (the script's own
# directory vanished, a permission error, ...), continuing would send later
# reads of $script_dir/*.txt somewhere unintended rather than aborting.
script_dir=$(cd "$(dirname "$0")" && pwd) || {
	echo "check_leaks: could not resolve this script's own directory from '$0'" >&2
	exit 2
}
if [ -z "$script_dir" ]; then
	echo "check_leaks: this script's own directory resolved to an empty path" >&2
	exit 2
fi

# Resolve everything else from the repository root: the pattern file and the
# scan scope must not depend on where the script was invoked from.
root=$(git rev-parse --show-toplevel 2>/dev/null) || {
	echo "check_leaks: not inside a git repository" >&2
	exit 2
}
cd "$root" || exit 2

mode=full
force_redact=0
for arg in "$@"; do
	case "$arg" in
	--file-types-only) mode=file-types-only ;;
	--redact) force_redact=1 ;;
	*)
		echo "check_leaks: unknown argument: $arg" >&2
		exit 2
		;;
	esac
done

status=0
fail() { echo "LEAK: $1" >&2; status=1; }

# --- Enumerate tracked files once, for every mode ---------------------------
# Section 5 (the data/media extension check) needs this list regardless of
# mode, and full mode's content and filename legs need it too. `git
# ls-files` is run alone here, never piped directly into something else, so
# its own exit status is captured directly rather than being hidden behind
# a later pipeline stage's. NUL-delimited, to a scratch file rather than a
# shell variable (which cannot hold embedded NULs) or a process substitution
# (whose own exit status bash cannot easily surface) - filenames containing
# spaces or brackets are the normal convention for scraped media.
tmp_tracked=$(mktemp) || {
	echo "check_leaks: FAILED - could not create a scratch file for tracked-file enumeration" >&2
	exit 2
}
trap 'rm -f "$tmp_tracked" "${tmp_untracked:-}"' EXIT

git ls-files -z > "$tmp_tracked"
rc=$?
if [ "$rc" -ne 0 ]; then
	echo "check_leaks: FAILED - git ls-files failed (exit $rc)" >&2
	echo "  Refusing to report clean without having enumerated tracked files." >&2
	exit 2
fi

if [ "$mode" = full ]; then

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

# --- Pattern preprocessing, and proof that it did not lose anything ----------
#
# The pattern list is cleaned with sed: comments stripped, leading/trailing
# whitespace trimmed, blank lines dropped. Leading whitespace must be
# stripped too: an indented pattern (e.g. pasted into the LEAK_PATTERNS
# secret with a leading space) would otherwise never match anything and the
# guard would silently stop checking for it.
#
# sed's exit status used to be discarded entirely. On BSD sed (macOS, and
# therefore any contributor's laptop) a single byte sed's locale cannot
# parse — a stray Latin-1 accented character or smart quote pasted into the
# CI secret is enough — aborts sed partway through. sed exits non-zero, but
# critically it has already written its partial output (every line before
# the bad byte) to stdout, so a naive `patterns=$(... | sed ...)` silently
# keeps only a truncated list, and the rest of this script would then run
# "clean" against fewer patterns than were configured, with no hint that
# anything was dropped.
#
# Two independent checks guard against that now, and either one failing
# aborts the whole run rather than continuing with a possibly-truncated
# list:
#
#   1. sed's own exit status, captured immediately.
#   2. A line-count cross-check: the number of non-empty, non-comment lines
#      going in must equal the number of patterns coming out. This catches
#      truncation even in a case where sed's exit status alone were not
#      trustworthy.
#
# Counting uses `grep -a` (never plain grep) on both sides: -a forces grep to
# treat its input as text no matter whether it looks like valid UTF-8.
# Without it, grep can itself refuse to report lines around the same invalid
# byte, which would silently defeat the very cross-check meant to catch
# this.
input_count=$(printf '%s\n' "$raw_patterns" | grep -a -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' | grep -a -c '^')

patterns=$(printf '%s\n' "$raw_patterns" | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d')
sed_status=$?

if [ "$sed_status" -ne 0 ]; then
	echo "check_leaks: FAILED - pattern preprocessing exited $sed_status." >&2
	echo "  A byte in the pattern source could not be parsed by this platform's sed," >&2
	echo "  which aborts mid-stream and silently truncates the pattern list." >&2
	echo "  Refusing to run with a possibly-truncated pattern list." >&2
	exit 2
fi

survived_count=$(printf '%s\n' "$patterns" | grep -a -c '^.')
if [ "$survived_count" -ne "$input_count" ]; then
	echo "check_leaks: FAILED - pattern preprocessing dropped lines silently." >&2
	echo "  $survived_count pattern(s) survived out of $input_count supplied." >&2
	echo "  Refusing to run with a possibly-truncated pattern list." >&2
	exit 2
fi

if [ -z "$patterns" ]; then
	echo "check_leaks: FAILED - no patterns configured." >&2
	echo "  CI: set the LEAK_PATTERNS repository secret." >&2
	echo "  Local: create .leak-patterns (git-ignored, one pattern per line)." >&2
	exit 2
fi

# See the header for the full rationale: env-sourced patterns imply CI (public
# logs, redact), file-sourced patterns imply a local terminal (show detail),
# and --redact overrides either way.
redact=1
if [ "$patterns_source" = file ]; then
	redact=0
fi
if [ "$force_redact" -eq 1 ]; then
	redact=1
fi

# --- History setup -----------------------------------------------------------
# Every commit reachable from any ref, not just the current branch. A repo
# with no commits yet has an empty list here; history scanning is then
# skipped rather than letting a revision-less `git grep` fall back to the
# working tree and double up (or misreport) step (a).
#
# git's own exit status is checked explicitly rather than folded into a
# blanket `|| true`: a genuinely commit-less repo exits 0 with empty output
# (verified), so that case is still handled below, but a real git failure
# (a corrupt object database, for instance) must abort rather than be read
# as "no history to scan" and silently skip checks 3 and 4 entirely.
all_revs=$(git rev-list --all 2>&1)
rev_rc=$?
if [ "$rev_rc" -ne 0 ]; then
	echo "check_leaks: FAILED - git rev-list --all failed (exit $rev_rc): $all_revs" >&2
	echo "  Refusing to run without knowing whether history scanning is possible." >&2
	exit 2
fi
if [ -n "$all_revs" ]; then
	rev_count=$(printf '%s\n' "$all_revs" | grep -a -c '^')
	echo "check_leaks: scanning full history ($rev_count commit(s)) - this can be slow on a large repo." >&2
fi

commit_shas=$(git log --all --format=%H 2>&1)
log_rc=$?
if [ "$log_rc" -ne 0 ]; then
	echo "check_leaks: FAILED - git log --all failed (exit $log_rc): $commit_shas" >&2
	echo "  Refusing to run without knowing whether commit messages can be scanned." >&2
	exit 2
fi

# --- Untracked-file enumeration, and readability preflight for both lists ----
# `git ls-files --others` is captured the same way as the tracked list above:
# alone, to a scratch file, with its own exit status checked directly.
#
# Then, before any pattern is tested against any file: every tracked and
# untracked file must be readable, checked once, up front. `git grep`
# reports rc=1 ("no match") both when nothing matches and when it silently
# skips a file it cannot open - its warning goes to a stream this script
# does not treat as a result either way - and a plain per-file `grep`
# behaves the same way. Without this preflight, a single `chmod 000` on a
# tracked file holding an uncommitted leak, or on an untracked file holding
# one outright, would report clean.
tmp_untracked=$(mktemp) || {
	echo "check_leaks: FAILED - could not create a scratch file for untracked-file enumeration" >&2
	exit 2
}

git ls-files --others --exclude-standard -z > "$tmp_untracked"
rc=$?
if [ "$rc" -ne 0 ]; then
	echo "check_leaks: FAILED - git ls-files --others failed (exit $rc)" >&2
	echo "  Refusing to run without having enumerated untracked files." >&2
	exit 2
fi

while IFS= read -r -d '' f; do
	if [ ! -r "$f" ]; then
		echo "check_leaks: FAILED - tracked file is not readable: $f" >&2
		echo "  Refusing to report clean without having been able to scan its contents." >&2
		exit 2
	fi
done < "$tmp_tracked"

while IFS= read -r -d '' f; do
	if [ ! -r "$f" ]; then
		echo "check_leaks: FAILED - untracked file is not readable: $f" >&2
		echo "  Refusing to report clean without having been able to scan its contents." >&2
		exit 2
	fi
done < "$tmp_untracked"

# Newline-separated views of the same two lists, for the filename-matching
# legs below (which substring-match a name; they never open the file, so
# they do not need the NUL-delimited form).
tracked_files=$(tr '\0' '\n' < "$tmp_tracked")
untracked_files=$(tr '\0' '\n' < "$tmp_untracked")

while IFS= read -r pat; do
	[ -n "$pat" ] || continue

	# 1. Forbidden strings, in tracked file CONTENTS (working tree) and in
	#    tracked PATHS. A file *named* for a private term leaks exactly as
	#    surely as one containing it.
	#    -F: patterns are literal strings, not regex. Without it, a pattern
	#    such as an email domain would also match with any single character
	#    standing in for its "." (which is a regex metacharacter), and any
	#    pattern containing a regex metacharacter would need escaping it
	#    doesn't otherwise need. (Deliberately not spelling out a real
	#    pattern here - this script must not become the thing it guards
	#    against.) Every tracked file was already confirmed readable above,
	#    so `git grep`'s rc=1 here means only "no match", never "skipped".
	hits=$(git grep -I -i -F -l -e "$pat" -- . 2>&1)
	rc=$?
	if [ "$rc" -eq 0 ]; then
		if [ "$redact" -eq 1 ]; then
			fail "$(printf '%s\n' "$hits" | grep -a -c '^') tracked file(s) contain a configured pattern (working tree) - run the guard locally to see which"
		else
			echo "$hits" >&2
			fail "forbidden string in the contents of the file(s) above (working tree)"
		fi
	elif [ "$rc" -gt 1 ]; then
		# >1 is a grep ERROR, not "no match" - never read that as clean
		fail "git grep failed (exit $rc); a pattern was not evaluated (working tree)"
	fi

	names=$(printf '%s\n' "$tracked_files" | grep -i -F -e "$pat")
	grep_rc=$?
	if [ "$grep_rc" -gt 1 ]; then
		fail "grep failed while matching tracked filenames against a pattern (exit $grep_rc); a pattern was not evaluated"
	elif [ -n "$names" ]; then
		if [ "$redact" -eq 1 ]; then
			fail "$(printf '%s\n' "$names" | grep -a -c '^') tracked filename(s) match a configured pattern - run the guard locally to see which"
		else
			echo "$names" >&2
			fail "forbidden string in the tracked filename(s) above"
		fi
	fi

	# 2. Untracked-but-not-ignored files. These are invisible to `git grep`
	#    (it only ever sees tracked content) and to `git ls-files` without
	#    --others, so a leak sitting in a new file that was never `git add`ed
	#    would otherwise pass clean while still resting on disk in a clone of
	#    this working copy. --exclude-standard keeps .gitignore'd files out
	#    of scope, matching what would actually ship if committed as-is.
	untracked_names=$(printf '%s\n' "$untracked_files" | grep -i -F -e "$pat")
	grep_rc=$?
	if [ "$grep_rc" -gt 1 ]; then
		fail "grep failed while matching untracked filenames against a pattern (exit $grep_rc); a pattern was not evaluated"
	elif [ -n "$untracked_names" ]; then
		if [ "$redact" -eq 1 ]; then
			fail "$(printf '%s\n' "$untracked_names" | grep -a -c '^') untracked filename(s) match a configured pattern - run the guard locally to see which"
		else
			echo "$untracked_names" >&2
			fail "forbidden string in the untracked filename(s) above"
		fi
	fi

	# git grep cannot see untracked content at all, so untracked files are
	# checked with plain grep instead, one file at a time. NUL-delimited:
	# filenames containing spaces or brackets (the normal convention for
	# scraped media) must be handled literally. Matches are buffered rather
	# than reported inline so a redacted run can report a count instead of
	# a path. Every untracked file was already confirmed readable above.
	untracked_content_hits=""
	untracked_content_count=0
	while IFS= read -r -d '' f; do
		if grep -I -i -q -a -F -e "$pat" -- "$f" 2>/dev/null; then
			untracked_content_count=$((untracked_content_count + 1))
			untracked_content_hits="$untracked_content_hits$f
"
		fi
	done < "$tmp_untracked"
	if [ "$untracked_content_count" -gt 0 ]; then
		if [ "$redact" -eq 1 ]; then
			fail "$untracked_content_count untracked file(s) contain a configured pattern - run the guard locally to see which"
		else
			printf '%s' "$untracked_content_hits" >&2
			fail "forbidden string in the contents of the untracked file(s) above"
		fi
	fi

	# 3. Tracked file CONTENTS at every commit in history, even if the
	#    offending line was removed by a later commit. A leak that was
	#    committed and then deleted is still permanently public the moment
	#    it is pushed; scanning only the working tree (as before) reports
	#    that case as clean, which is exactly how the leak that prompted
	#    this fix survived undetected. Historical blobs live in the object
	#    database, not the working tree, so the readability preflight above
	#    does not apply here - a filesystem permission bit on a checked-out
	#    file cannot make an already-committed blob unreadable to git.
	if [ -n "$all_revs" ]; then
		hist_hits=$(git grep -I -i -F -l -e "$pat" $all_revs -- . 2>&1)
		rc=$?
		if [ "$rc" -eq 0 ]; then
			if [ "$redact" -eq 1 ]; then
				# hist_hits lines are "sha:path" - the SHA (up to the first
				# colon) is not sensitive and safe to show; the path is not.
				hist_shas=$(printf '%s\n' "$hist_hits" | cut -d: -f1 | sort -u | tr '\n' ' ')
				hist_count=$(printf '%s\n' "$hist_hits" | grep -a -c '^')
				fail "$hist_count occurrence(s) of a configured pattern in tracked history, commit(s): ${hist_shas}- run the guard locally to see which path(s)"
			else
				echo "$hist_hits" >&2
				fail "forbidden string in tracked history above (commit:path)"
			fi
		elif [ "$rc" -gt 1 ]; then
			fail "git grep over history failed (exit $rc); a pattern was not evaluated"
		fi
	fi
done <<EOF
$patterns
EOF

# 4. Commit messages, across all of history. Commit messages are permanent
#    and public the moment a commit is pushed, and nothing before this
#    checked them: a forbidden string typed into a commit message (even one
#    describing a redaction, rather than the file diff itself) would ship
#    regardless of what the tracked file contents say.
if [ -n "$commit_shas" ]; then
	while IFS= read -r sha; do
		[ -n "$sha" ] || continue
		msg=$(git log -1 --format=%B "$sha" 2>&1)
		msg_rc=$?
		if [ "$msg_rc" -ne 0 ]; then
			echo "check_leaks: FAILED - git log failed for commit $sha (exit $msg_rc): $msg" >&2
			echo "  Refusing to report clean without having read that commit's message." >&2
			exit 2
		fi
		while IFS= read -r pat; do
			[ -n "$pat" ] || continue
			if printf '%s\n' "$msg" | grep -q -a -i -F -e "$pat"; then
				fail "forbidden string in the commit message of $sha"
			fi
		done <<EOF
$patterns
EOF
	done <<EOF
$commit_shas
EOF
fi

fi # mode = full

# 5. Data and media files. The ignore rules should prevent these; this catches
#    a `git add -f`. NUL-delimited: filenames containing spaces, brackets or
#    glob characters must be handled literally, and bracketed names are the
#    normal convention for scraped media.
#    The yaml/yml rule is scoped to exclude the checked-in CI workflow(s) under
#    .github/workflows/ — those are legitimate, non-secret config and must
#    stay trackable; a config/archive/data file with the same extension
#    anywhere else is still caught.
#
#    The data and media extensions live in scripts/data-extensions.txt and
#    scripts/media-extensions.txt, not here: those lists and .gitignore's
#    deny-by-default block are two copies of the same rule that have already
#    drifted once (a re-initialised repo silently shipped with no CI at
#    all), so the extensions themselves now have exactly one source of
#    truth each. A test asserts the two stay in agreement in both
#    directions - an extension .gitignore excludes but this list does not
#    know about would be invisible to this check, so a `git add -f` of that
#    extension would slip past entirely.
#
#    Loading either list is held to the same standard as loading the pattern
#    list: a missing file, an unreadable one, or one that yields zero usable
#    entries after comment/blank stripping must abort the run rather than
#    silently checking nothing. This was previously unchecked - a deleted or
#    typo'd path here printed a "No such file or directory" error but let the
#    script continue past it and report clean, exactly the failure-to-load
#    silently disables a check defect this file has now had three times.
#    This also runs unconditionally (not only in `mode = full`): on a fork
#    PR, --file-types-only's extension check is the *only* thing that runs,
#    so if it cannot load, the run must not exit 0 either.
#
#    load_ext_list is called directly, never through a pipe or $(...): a
#    subshell would let its `exit` end only the subshell, silently leaving
#    the caller's array empty and letting the script continue regardless.
load_ext_list() {
	# $1: path to read  $2: human name for messages. Populates the global
	# array `loaded_exts` (bash 3.2 has no namerefs to return one directly).
	loaded_exts=()
	if [ ! -f "$1" ] || [ ! -r "$1" ]; then
		echo "check_leaks: FAILED - $2 extension list is missing or unreadable: $1" >&2
		echo "  Refusing to run with an unknown set of guarded extensions." >&2
		exit 2
	fi
	while IFS= read -r ext; do
		# Trim whitespace BEFORE testing blankness/comments: a whitespace-
		# only line is not "" by string comparison and does not start with
		# "#", so untrimmed it would dodge both checks and enter the array
		# as a literal glob that can never match a real filename - making
		# the array non-empty without adding anything that actually guards
		# anything, and silently defeating the "zero usable entries" check
		# just below. The pattern list learned this exact lesson via sed
		# (see the preprocessing block above); this list did not repeat it
		# until now.
		ext="${ext#"${ext%%[![:space:]]*}"}"
		ext="${ext%"${ext##*[![:space:]]}"}"
		[ -n "$ext" ] || continue
		case "$ext" in \#*) continue ;; esac
		loaded_exts+=("$ext")
	done < "$1"
	if [ "${#loaded_exts[@]}" -eq 0 ]; then
		echo "check_leaks: FAILED - $2 extension list has no usable entries: $1" >&2
		echo "  Refusing to run with an unknown set of guarded extensions." >&2
		exit 2
	fi
}

load_ext_list "$script_dir/data-extensions.txt" "data"
data_exts=("${loaded_exts[@]}")

load_ext_list "$script_dir/media-extensions.txt" "media"
media_exts=("${loaded_exts[@]}")

while IFS= read -r -d '' f; do
	case "$f" in
	*.example.json | .github/workflows/*.yml | .github/workflows/*.yaml) continue ;;
	esac
	is_data=0
	# data_exts is guaranteed non-empty here (load_ext_list aborts otherwise),
	# so this expansion is safe even under bash 3.2's `set -u` handling of it.
	for ext in "${data_exts[@]}"; do
		case "$f" in
		$ext) is_data=1; break ;;
		esac
	done
	if [ "$is_data" -eq 1 ]; then
		fail "tracked data file: $f"
		continue
	fi
	is_media=0
	# media_exts is likewise guaranteed non-empty.
	for ext in "${media_exts[@]}"; do
		case "$f" in
		$ext) is_media=1; break ;;
		esac
	done
	if [ "$is_media" -eq 1 ]; then
		fail "tracked media file: $f"
	fi
done < "$tmp_tracked"

[ "$status" -eq 0 ] && echo "check_leaks: clean"
exit "$status"
