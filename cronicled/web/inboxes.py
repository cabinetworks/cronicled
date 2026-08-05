"""Which inbox owns a subject type.

An inbox is a named group of subject types, not a single one: four of the six
kinds are tag work and belong on one page. The grouping is presentation, so it
lives here rather than in the store.

The map must be exhaustive. A subject type with no inbox appears on no page,
and nothing says so -- a row invisible everywhere is worse than a row on the
wrong page, so an unmapped type is a startup failure.

See `tests/test_inboxes.py::test_every_declared_subject_type_has_an_inbox`
for how that is enforced: it discovers every subject type the package
declares by walking `cronicled`'s own modules rather than from a list copied
here, so a seventh subject type added later fails the suite instead of
shipping with no inbox.
"""

INBOXES = {
    "scenes": ("scene",),
    "tags": ("tag", "tag-cluster", "tag-performer", "tag-unused"),
    # Two producers, one page: `cronicled.descriptions` proposes a cleaned
    # description, `cronicled.enrichment` proposes values for a performer's
    # OTHER blank fields. Both are decisions about a performer, so both
    # belong where a reviewer already looks for one -- unlike their subject
    # TYPES, which stay deliberately separate (see
    # `cronicled.enrichment.SUBJECT_TYPE`'s own docstring for why sharing one
    # would let a mute or a refusal from one producer silently reach the
    # other's proposals).
    "performers": ("performer", "performer-enrichment"),
}

ALL_SUBJECT_TYPES = tuple(t for types in INBOXES.values() for t in types)

TITLES = {"scenes": "Scenes", "tags": "Tags", "performers": "Performers"}


def inbox_for(subject_type):
    """The inbox name that owns `subject_type`, or raise `KeyError`."""
    for name, types in INBOXES.items():
        if subject_type in types:
            return name
    raise KeyError(subject_type)


def check_total(subject_types):
    """Raise naming every subject type in `subject_types` that no inbox
    claims. Silent about the rest -- this is a totality check, not a report
    of what IS mapped.
    """
    unmapped = sorted(set(subject_types) - set(ALL_SUBJECT_TYPES))
    if unmapped:
        raise ValueError(
            "these subject types have no inbox, so their proposals would "
            "appear on no page: %s" % ", ".join(unmapped))
