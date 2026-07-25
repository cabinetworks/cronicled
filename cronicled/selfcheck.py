"""Runtime self-check: proves the pinned interpreter can actually import and
run this project's code, rather than merely that a directory was copied into
an image.

Run as `python -m cronicled.selfcheck`. It imports every module under the
`cronicled` package — a syntax error or a broken top-level statement in any
of them surfaces right here as an ImportError — and then exercises a handful
of pure functions end to end, checking their output against a known-correct
value rather than merely calling them. Exits non-zero with a clear message on
the first failure it hits; prints a single ready line and exits 0 only once
everything above has passed.
"""
import importlib
import pkgutil
import sys

import cronicled


class SelfCheckError(Exception):
    """A self-check assertion failed: an exercised function returned something
    other than the known-correct value."""


def _import_every_module():
    """Import every module under the `cronicled` package (not just the
    top-level one), returning the full list of module names imported.
    Discovered via pkgutil rather than a hand-written list, so a new module
    is covered automatically instead of the list quietly going stale."""
    names = [cronicled.__name__]
    for info in pkgutil.walk_packages(cronicled.__path__, prefix=cronicled.__name__ + "."):
        names.append(info.name)
    for name in names:
        importlib.import_module(name)
    return names


def _check(label, actual, expected):
    if actual != expected:
        raise SelfCheckError("%s: expected %r, got %r" % (label, expected, actual))


def run():
    """Import every module and exercise a few pure functions end to end.
    Returns the list of imported module names on success; raises
    SelfCheckError or ImportError on the first failure."""
    modules = _import_every_module()

    from cronicled.text import normalize
    _check("text.normalize folds accents and case",
           normalize("Café Übermensch!"), "cafe ubermensch")

    from cronicled.dates import parse_date
    _check("dates.parse_date reads an ISO date",
           parse_date("2023-09-11"), "2023-09-11")

    from cronicled.censorship import decensor, search_variants
    subs = {"kestrel": ["k3strel"]}
    _check("censorship.search_variants expands a censored variant",
           search_variants("kestrel clip", subs), ["kestrel clip", "k3strel clip"])
    _check("censorship.decensor round-trips the censored form back",
           decensor("k3strel clip", subs), "kestrel clip")

    from cronicled.adapters.declarative import DeclarativeAdapter
    adapter = DeclarativeAdapter({
        "name": "selfcheck", "owner_source": "url_segment", "owner_segment": 2,
    })
    result = {"url": "https://example.test/store/velvetcrane/copper-kettle"}
    _check("adapter built from a spec resolves an owner from a URL segment",
           adapter.owner_of(result), "velvetcrane")

    return modules


def main():
    try:
        modules = run()
    except Exception as e:
        print("cronicled selfcheck FAILED: %s" % e, file=sys.stderr)
        return 1
    print("cronicled selfcheck ready (%d modules imported)" % len(modules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
