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
# Output discipline: CI logs are public. For CONTENT matches this reports the
# file path or commit SHA where the pattern was found - never the matched
# line or the pattern itself. For FILENAME matches, even the path cannot be
# shown: a filename that matches a pattern CONTAINS that pattern, so those
# legs report a count only. Run it locally, where .leak-patterns exists, to
# see which file.
#
# --file-types-only skips every pattern-based leg (checks 1-4 below) and
# does not fail when no patterns are configured, because none are expected in
# that mode. It exists for fork pull requests, which cannot read the
# LEAK_PATTERNS secret. Every other invocation keeps failing closed.
set -u

# The extension list ships alongside this script, not inside whatever repo is
# being scanned - resolve it from $0's own location before anything below
# changes directory.
script_dir=$(cd "$(dirname "$0")" && pwd)

# Resolve everything else from the repository root: the pattern file and the
# scan scope must not depend on where the script was invoked from.
root=$(git rev-parse --show-toplevel 2>/dev/null) || {
	echo "check_leaks: not inside a git repository" >&2
	exit 2
}
cd "$root" || exit 2

mode=full
if [ "${1:-}" = "--file-types-only" ]; then
	mode=file-types-only
fi

status=0
fail() { echo "LEAK: $1" >&2; status=1; }

if [ "$mode" = full ]; then

patterns=""
if [ -n "${LEAK_PATTERNS:-}" ]; then
	patterns=$LEAK_PATTERNS
elif [ -f .leak-patterns ]; then
	patterns=$(cat .leak-patterns)
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

# --- History setup -----------------------------------------------------------
# Every commit reachable from any ref, not just the current branch. A repo
# with no commits yet has an empty list here; history scanning is then
# skipped rather than letting a revision-less `git grep` fall back to the
# working tree and double up (or misreport) step (a).
all_revs=$(git rev-list --all 2>/dev/null || true)
if [ -n "$all_revs" ]; then
	rev_count=$(printf '%s\n' "$all_revs" | grep -a -c '^')
	echo "check_leaks: scanning full history ($rev_count commit(s)) - this can be slow on a large repo." >&2
fi

commit_shas=$(git log --all --format=%H 2>/dev/null || true)

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
	#    against.)
	hits=$(git grep -I -i -F -l -e "$pat" -- . 2>&1)
	rc=$?
	if [ "$rc" -eq 0 ]; then
		echo "$hits" >&2
		fail "forbidden string in the contents of the file(s) above (working tree)"
	elif [ "$rc" -gt 1 ]; then
		# >1 is a grep ERROR, not "no match" - never read that as clean
		fail "git grep failed (exit $rc); a pattern was not evaluated (working tree)"
	fi

	names=$(git ls-files | grep -i -F -e "$pat" || true)
	if [ -n "$names" ]; then
		# never echo the names: a filename that matches a pattern CONTAINS the
		# pattern, and CI logs are public. Run locally for detail.
		fail "$(printf '%s\n' "$names" | wc -l | tr -d ' ') tracked filename(s) match a configured pattern - run the guard locally to see which"
	fi

	# 2. Untracked-but-not-ignored files. These are invisible to `git grep`
	#    (it only ever sees tracked content) and to `git ls-files` without
	#    --others, so a leak sitting in a new file that was never `git add`ed
	#    would otherwise pass clean while still resting on disk in a clone of
	#    this working copy. --exclude-standard keeps .gitignore'd files out
	#    of scope, matching what would actually ship if committed as-is.
	untracked_names=$(git ls-files --others --exclude-standard | grep -i -F -e "$pat" || true)
	if [ -n "$untracked_names" ]; then
		# never echo the names: a filename that matches a pattern CONTAINS the
		# pattern, and CI logs are public. Run locally for detail.
		fail "$(printf '%s\n' "$untracked_names" | wc -l | tr -d ' ') untracked filename(s) match a configured pattern - run the guard locally to see which"
	fi

	# git grep cannot see untracked content at all, so untracked files are
	# checked with plain grep instead, one file at a time. NUL-delimited:
	# filenames containing spaces or brackets (the normal convention for
	# scraped media) must be handled literally.
	while IFS= read -r -d '' f; do
		if grep -I -i -q -a -F -e "$pat" -- "$f" 2>/dev/null; then
			echo "$f" >&2
			fail "forbidden string in the contents of the untracked file above"
		fi
	done < <(git ls-files --others --exclude-standard -z)

	# 3. Tracked file CONTENTS at every commit in history, even if the
	#    offending line was removed by a later commit. A leak that was
	#    committed and then deleted is still permanently public the moment
	#    it is pushed; scanning only the working tree (as before) reports
	#    that case as clean, which is exactly how the leak that prompted
	#    this fix survived undetected.
	if [ -n "$all_revs" ]; then
		hist_hits=$(git grep -I -i -F -l -e "$pat" $all_revs -- . 2>&1)
		rc=$?
		if [ "$rc" -eq 0 ]; then
			echo "$hist_hits" >&2
			fail "forbidden string in tracked history above (commit:path)"
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
#    The data extensions live in scripts/data-extensions.txt, not here: this
#    list and .gitignore's deny-by-default block are two copies of the same
#    rule that have already drifted once (a re-initialised repo silently
#    shipped with no CI at all), so the extensions themselves now have exactly
#    one source of truth. A test asserts the two stay in agreement.
data_exts=()
while IFS= read -r ext; do
	[ -n "$ext" ] || continue
	case "$ext" in \#*) continue ;; esac
	data_exts+=("$ext")
done < "$script_dir/data-extensions.txt"

while IFS= read -r -d '' f; do
	case "$f" in
	*.example.json | .github/workflows/*.yml | .github/workflows/*.yaml) continue ;;
	esac
	is_data=0
	# Guard the expansion itself, not just the loop body: under `set -u`,
	# bash 3.2 (the version macOS ships) treats "${arr[@]}" on a genuinely
	# empty array as an unbound-variable error rather than zero words.
	if [ "${#data_exts[@]}" -gt 0 ]; then
		for ext in "${data_exts[@]}"; do
			case "$f" in
			$ext) is_data=1; break ;;
			esac
		done
	fi
	if [ "$is_data" -eq 1 ]; then
		fail "tracked data file: $f"
		continue
	fi
	case "$f" in
	*.mp4 | *.m4v | *.mkv | *.avi | *.wmv | *.mov | *.jpg | *.jpeg | *.png \
	  | *.gif | *.webp | *.webm | *.srt | *.vtt | *.ass | *.m3u | *.mp3 \
	  | *.wav | *.flac | *.bmp | *.tiff | *.heic)
		fail "tracked media file: $f" ;;
	esac
done < <(git ls-files -z)

[ "$status" -eq 0 ] && echo "check_leaks: clean"
exit "$status"
