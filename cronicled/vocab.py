"""Shared vocabulary for the string primitives in `cronicled.text`."""

STOPWORDS = frozenset({"the", "a", "an", "and", "of", "to", "my", "you", "your", "in", "for", "with"})
# The single canonical junk-token set, shared by scoring (`tokens()`) and title
# prettification — keep it unified so the two never drift. Container/format
# tokens mirror SCAN_EXTS (incl. 'mov') so a stray container word left mid-name
# is dropped just like an extension.
#
# Deliberately NOT widened alongside CONTAINER_EXTS below. This set is applied
# to every token of a name, not to the trailing one, so an entry here removes a
# word from the MIDDLE of a filename — which shrinks both readings of the
# evidence at once, including the one containment is judged on. The trailing
# token is a separable, and safer, question; this one is not, and widening it
# needs its own reasoning rather than riding along with a list that answers
# something else.
JUNK_TOKENS = frozenset({
    "1080p", "2160p", "720p", "480p", "4k", "8k", "uhd", "hd", "sd", "xxx",
    "hevc", "x264", "x265", "h264", "h265", "mp4", "wmv", "mkv", "avi", "mov",
    "full", "clip", "video", "part", "pt", "feat", "ft",
})

# Two questions, two lists, because they want opposite things.
#
# SCAN_EXTS answers "which files should a library walker look at at all". It
# wants to be CONSERVATIVE: every entry is work a run has to do, and a wrong
# one is a run spent opening files nobody wanted opened.
#
# CONTAINER_EXTS answers "which trailing token is definitely not part of a
# title". It wants to be EXHAUSTIVE, because every container it misses becomes
# a token the matcher weighs as though it were title text. That miss is
# measurable: the same file scored 0.933 and contained under `.mp4`, and 0.686
# and not contained under `.mpeg` — correct metadata held back for review over
# nothing but the container it happened to be muxed into.
#
# One list cannot be both, which is why widening the second used to be
# unavailable: it would have widened the first with it.
SCAN_EXTS = frozenset({".mp4", ".mkv", ".wmv", ".avi", ".mov", ".m4v", ".flv",
                       ".ts", ".webm"})

# Every scanned extension is certainly a container, so it is certainly not
# title text — that direction is sound and is why this is built FROM
# SCAN_EXTS. The reverse must never be done: a container being recognisable is
# no argument for spending a walk on it.
#
# What is added here is video containers and nothing else. Not audio, not
# images, not subtitles: each of those is a further question about which files
# are even in scope, and answering it inside this list is how one list came to
# answer two in the first place.
#
# This list is not complete and cannot be — new containers keep appearing, and
# a library holds whatever it holds. That is precisely why `scoring` keeps its
# separate rule for an extension-SHAPED suffix it does not recognise: this list
# is the confident half, that rule is the uncertain half, and the two are
# allowed to do different things with what they find. Extending this set
# strengthens a claim ("definitely not a title"), so an entry belongs here only
# when a trailing word of that spelling would be a container and not a word.
CONTAINER_EXTS = SCAN_EXTS | frozenset({
    ".mpeg", ".mpg", ".mpe", ".m1v", ".m2v", ".mpv", ".mp2",
    ".m2ts", ".mts", ".vob", ".ogv", ".ogm", ".asf",
    ".divx", ".xvid", ".rm", ".rmvb",
    ".3gp", ".3g2", ".f4v", ".qt", ".dv", ".mxf", ".wtv", ".amv",
})
