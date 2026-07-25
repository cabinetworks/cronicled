"""Find the creator's folder: the ancestor directory a file's attribution
scores against.

A library layout often inserts one or more generic directories between the
creator's own folder and the file itself -- "Clips", "Downloads",
"Videos/Uploads" and the like. `creator_folder` walks up past those to find
the folder that actually names the creator, so scoring compares a filename
against *that creator's* catalogue rather than a container's name.

``CONTAINER_NAMES`` is a small, closed, English-only set of generic
directory names. It recognises common single-word containers, in their
usual English spelling, and nothing more: it will not recognise a numbered
set ("Set 03"), a non-English equivalent, or a season/date directory. When
a container is missing from the set, the failure is to attribute the file
to that folder as if it were the creator -- a visible, checkable mistake (a
"creator" that turns out to be "Downloads"), not a silent one.

The set grew from real filing conventions rather than being assembled up
front: every entry below except `misc` is carried over from a legacy
implementation's list, grown over time from libraries actually seen in the
wild ("Vids"/"Vid" as shorthand for "Videos"/"Video" among them). `misc` is
this rewrite's own addition -- a reasonable guess, not observed evidence,
and worth distinguishing from the rest for that reason. Keep extending the
set the same way it was built: from a real container name a test or a
report turns up, not from speculating about what else might exist.
"""
import posixpath

from cronicled.text import clean_folder

CONTAINER_NAMES = frozenset({
    "video", "videos",
    "clip", "clips",
    "vid", "vids",
    "movie", "movies",
    "media",
    "download", "downloads",
    "upload", "uploads",
    "content",
    "files",
    "misc",  # judgment call, not carried over from the legacy list
})


def _container_name(name):
    """The lowercased, qualifier-stripped form of `name`, for comparison
    against CONTAINER_NAMES."""
    return clean_folder(name).lower()


def creator_folder(path, max_up=4):
    """The nearest ancestor directory of `path` that is not a generic
    container, with any qualifier (an encode tag, a bracketed year, ...)
    stripped.

    Walks up at most `max_up` ancestor directories from the file, skipping
    any whose `clean_folder`-ed, lowercased name is in CONTAINER_NAMES, and
    returns the first that is not. If every ancestor checked is a
    container -- or there simply is no such ancestor -- falls back to the
    immediate parent, also stripped, rather than continuing to walk toward
    the filesystem root.

    `path` is treated purely as a string, split with `posixpath`. This
    never touches the filesystem: no ``os.path.exists``, no stat, no
    resolving of ``..`` or symlinks.
    """
    directory = posixpath.dirname(path)
    if not directory or directory == "/":
        return ""

    immediate_parent = None
    ancestor = directory
    for _ in range(max_up):
        if not ancestor or ancestor == "/":
            break
        name = posixpath.basename(ancestor)
        if immediate_parent is None:
            immediate_parent = name
        if _container_name(name) not in CONTAINER_NAMES:
            return clean_folder(name)
        ancestor = posixpath.dirname(ancestor)

    return clean_folder(immediate_parent) if immediate_parent is not None else ""
