"""Shared vocabulary for the string primitives in `cronicled.text`."""

STOPWORDS = frozenset({"the", "a", "an", "and", "of", "to", "my", "you", "your", "in", "for", "with"})
# The single canonical junk-token set, shared by scoring (`tokens()`) and title
# prettification — keep it unified so the two never drift. Container/format
# tokens mirror VIDEO_EXTS (incl. 'mov') so a stray container word left mid-name
# is dropped just like an extension.
JUNK_TOKENS = frozenset({
    "1080p", "2160p", "720p", "480p", "4k", "8k", "uhd", "hd", "sd", "xxx",
    "hevc", "x264", "x265", "h264", "h265", "mp4", "wmv", "mkv", "avi", "mov",
    "full", "clip", "video", "part", "pt", "feat", "ft",
})
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".wmv", ".avi", ".mov", ".m4v", ".flv", ".ts", ".webm"})
