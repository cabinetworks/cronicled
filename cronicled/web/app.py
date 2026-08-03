"""The HTTP surface: routing, and nothing else.

Every write is a POST followed by a redirect. A GET to an action path is
refused rather than tolerated — browsers prefetch, and a link that applies a
proposal will eventually be followed by something that is not a person.
"""

import urllib.parse
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from cronicled.performer_tags import SUBJECT_TYPE as _RECONCILE_SUBJECT
from cronicled.tag_hygiene import LOW_COUNT_IS_NOT_PROOF
from cronicled.tag_hygiene import SUBJECT_TYPE as _UNUSED_TAG_SUBJECT
from cronicled.tags import SUBJECT_TYPE as _MERGE_SUBJECT

from .inboxes import INBOXES, TITLES
from .pagination import PAGE_SIZE, offset_for, page_number, total_pages, window
from .render import render
from .rows import to_rows, to_summary_view, windowed_unused_groups

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8571

# The three subject types `rows.to_rows` cannot build a row for -- it
# dispatches only on `rows.DESCRIPTION_SUBJECT`/`rows.TAG_DESCRIPTION_SUBJECT`
# and treats everything else as scene-shaped (see `to_rows`), so a
# tag-cluster, tag-performer or tag-unused item forced through it KeyErrors on
# `payload["path"]`. That used to mean a per-inbox page simply left these
# three out of what it asked the store for at all (see the ticket that added
# `/tags`, `/scenes`, `/performers`) -- a real, named gap in what `/tags`
# could show, since none of the three has a home on the generic per-item row
# list `to_rows` builds.
#
# They are not homeless, though: each already has its OWN section on the
# combined `/inbox` page (`to_merge_rows`, `to_reconcile_rows`,
# `to_unused_groups`, wired in `cronicled.__main__` as `merges=`/
# `reconciles=`/`unused=`) -- a cluster, a reconciliation and a
# nothing-scenes-classify tag are not one-row-per-proposal the way a scene or
# a description is; a merge is a decision about several spellings, and an
# unused-tag group is many proposals collapsed into one expandable row. This
# maps each of the three to the KEYWORD `_inbox_page` composes it under, so a
# per-inbox page builds exactly the section the combined page builds for a
# type its own inbox owns, through the SAME already-existing closure
# (`_merges`/`_reconciles`/`_unused` below) -- never a second copy of
# `to_merge_rows` et al., and never forced through `to_rows`.
#
# Also doubles as what the GENERIC row list (built through `to_rows`, for
# every subject type that is neither of these three nor a description) has to
# exclude when asking the store — see `_scene_subject_types` below, the one
# place that exclusion happens.
_SECTION_SUBJECTS = {
    _MERGE_SUBJECT: "merges",
    _RECONCILE_SUBJECT: "reconciles",
    _UNUSED_TAG_SUBJECT: "unused",
}


def _scene_subject_types(types):
    """`types`, narrowed to what `to_rows` can build a row for.

    The one place `_SECTION_SUBJECTS`'s keys are subtracted out, so
    `_sidebar_context` and `_inbox_page` cannot narrow by two separately
    written tuples that could drift apart.
    """
    return tuple(t for t in types if t not in _SECTION_SUBJECTS)

# The terminal states a per-inbox page can be asked for at `/{inbox}/{state}`.
#
# The states named for this stage were `applied, refused, dismissed, muted`,
# and `refused` is left out on purpose: it is not a state an `item` row ever
# carries (see `Store._HIDDEN_STATES` and `Store.items()`'s own docstring) --
# a refusal is recorded in a wholly separate `refusal` table, keyed by
# SUBJECT rather than fingerprint, and `Store.refusals()` takes no
# `subject_types` argument to narrow it by. There is no way to serve a
# per-inbox refused page through `Store.items(subject_types=)`, which is the
# one interface this stage consumes -- so `/{inbox}/refused` 404s rather than
# silently serving an empty page that reads as "nothing refused" for an
# inbox that may have plenty.
_INBOX_STATES = ("applied", "dismissed", "muted")


def _sidebar_context(store):
    """The persistent navigation: one entry per inbox (see `INBOXES`), each
    carrying the count that `/{name}` itself will show and, only for a
    terminal state that inbox actually has something in, a link to
    `/{name}/{state}`.

    Returns `None` when there is no store wired -- the same condition
    `_serve_inbox_route` itself 404s on (see its own comment) -- because a
    link into a page that would 404 is worse than no navigation there at all.

    THE COUNT SPANS EVERY SUBJECT TYPE `INBOXES[name]` LISTS, not just the
    ones `to_rows` can build a row for. `_inbox_page` now composes a section
    for each of `_SECTION_SUBJECTS` too (see its own comment), so a tag
    inbox's count has to add in every tag-cluster, tag-performer and
    tag-unused proposal for the number here to agree with what `/tags`
    itself now shows, and with `cronicled.__main__.waiting_counts`'s own total
    for the same heading on the summary page -- the two used to disagree
    (one counted four subject types, the other one), and that gap is the
    defect this composition closes.

    One `store.counts()` call over the FULL set answers "how many, in every
    non-hidden state" for the inbox as a whole -- summed here excluding
    `applied`, the same way `_inbox_page` itself drops an applied row from
    the working-queue view.

    THE NESTED STATE LINKS ('applied'/'dismissed'/'muted') stay narrowed to
    `_scene_subject_types`, deliberately NOT widened alongside the count
    above. A dismissed or muted tag-cluster, tag/performer match or
    low-count-tag proposal is shown -- with its own Undismiss/Unmute control
    -- INLINE in its own section on `/{name}` itself (see `_inbox_page`),
    the same way the combined page has always shown it; there is no separate
    `/{name}/dismissed` or `/{name}/muted` ROUTE for any of the three, because
    the generic terminal-state route only ever draws `to_rows`-built rows.
    Widening this check the same way as the count would light up a nested
    link promising a dismissed proposal that the page behind it has no way to
    draw.

    `counts()` cannot report `dismissed`/`muted` at all -- both are in
    `Store._HIDDEN_STATES` and excluded from its query by design, the same
    design that makes `items()`'s own default view hide them -- so whether
    each of those two nested links is worth showing is answered with
    `items(state=..., limit=1)` instead: a real, unpaginated existence check
    against the store, not a second count invented from the first.
    """
    if store is None:
        return None
    entries = []
    for name in INBOXES:
        types = INBOXES[name]
        counts = store.counts(subject_types=types)
        waiting = sum(n for state, n in counts.items() if state != "applied")
        scene_types = _scene_subject_types(types)
        scene_counts = (counts if scene_types == types
                       else store.counts(subject_types=scene_types))
        states = []
        for state in _INBOX_STATES:
            if state == "applied":
                present = scene_counts.get("applied", 0) > 0
            else:
                present = bool(store.items(subject_types=scene_types,
                                           state=state, limit=1))
            if present:
                states.append(state)
        entries.append({"name": name, "title": TITLES[name],
                        "count": waiting, "states": states})
    return entries

# The two pages. `/` is the summary -- "did the passes run, and what did they
# find" -- and the inbox, which used to be here, is one click away at `/inbox`.
#
# Every write redirects to `INBOX_PATH` except the one that belongs to the
# summary (see `do_POST`). A write that redirected to `/` would answer a person
# who has just judged one of forty proposals by taking them off the list of the
# other thirty-nine.
SUMMARY_PATH = "/"
INBOX_PATH = "/inbox"

_ACTIONS = ("approve", "dismiss", "mute", "undo", "scan",
           "unmute", "undismiss", "refresh", "bulk_apply_tag_descriptions")

# A REVERSAL undoes an earlier verdict recorded against a collapsed section --
# `/unmute` reverses a mute (the "Muted" section), `/undismiss` a dismissal
# (the "Dismissed" section) -- and both sections start collapsed, the same as
# every section but Applied. Before this, every reversal redirected to the
# plain inbox, which closes the section again: reversing seventeen mutes in
# one sitting meant opening "Muted" seventeen times, and every open lost the
# scroll position too. The redirect now names which section it acted in (see
# `do_POST` below), and `do_GET` reopens exactly that one -- never every
# section (that would bury whatever the inbox exists to surface under a pile
# of reversed rows) and never a bulk reversal (approving, muting and now
# reversing all stay one row at a time).
_REOPEN_SECTION = {"unmute": "muted", "undismiss": "dismissed"}
_OPENABLE_SECTIONS = tuple(_REOPEN_SECTION.values())

# Pre-filled into the number input so a person is not left guessing a value
# from nothing -- never read on the code path itself. A request that omits
# `limit` is refused there (see `do_POST`), not silently given this number.
DEFAULT_SCAN_LIMIT = 25

# Every write here posts exactly one hidden field: a fingerprint. Nothing
# genuine comes anywhere near this many bytes -- it exists to bound
# `rfile.read(length)` below, not to accommodate a real form.
#
# `int(Content-Length)` alone only catches a non-numeric header (a
# `ValueError`); a numeric-but-dishonest one still parses. `rfile.read(n)`
# blocks until `n` bytes arrive or the connection closes, so a NEGATIVE
# length (`read(-1)` means read-until-EOF, and this client has no reason to
# close) or an absurdly large one (far more than this client will ever send)
# both wedge the handler waiting on bytes that are never coming. Both are
# refused here, before `rfile.read` is ever called -- this server is
# single-threaded (see `serve()`), so a wedge on any one connection stalls
# every other request until it clears.
_MAX_BODY_BYTES = 4096

# `bulk_apply_tag_descriptions` is the one form on this whole site that
# legitimately posts more than a handful of bytes: it carries one hidden
# `fp` field per row the page showed, not one. The measured population
# (ticket bulk179) is 1456 waiting tag-description proposals, and each
# fingerprint is a 64-character sha256 hex digest (see
# `cronicled.store.fingerprint`), so one field costs `len("fp=") + 64 +
# len("&")` = 68 bytes: 1456 * 68 = 98,988 bytes for that whole measured
# population in one submission. This bound is set well above that -- room
# for roughly 3,800 fingerprints -- so the mechanism does not need
# revisiting the next time the population grows a little, while still
# being a BOUND rather than `_MAX_BODY_BYTES` simply widened for every
# action: `rfile.read` here blocks on a fixed, generous ceiling, never an
# attacker-chosen one, and every other action keeps the tight bound above.
_MAX_BULK_BODY_BYTES = 262144

# Sec-Fetch-Site values a legitimate write can carry. `same-origin` is a
# request from a page this server served; `none` is the address bar, a
# bookmark, or curl deliberately setting the header. Everything else
# (`cross-site`, `same-site`) means some OTHER page caused the browser to
# send this request.
_ACCEPTABLE_SEC_FETCH_SITE = ("same-origin", "none")


def _origin_matches_host(origin, host_header):
    """Does `Origin` (`scheme://host[:port]`) name the same host:port this
    request's own `Host` header carries? This server is plain HTTP only, so
    only the part after `//` is compared."""
    marker = "//"
    idx = origin.find(marker)
    if idx == -1:
        return False
    return origin[idx + len(marker):] == host_header


# Query-string keys naming which page of a bounded section to show. One
# name per section the combined `/inbox` page composes; a per-inbox page
# reuses `_ROWS_PAGE_KEY` for its one generic list and, only when it owns
# the corresponding subject type, the three special-section keys -- see
# `Handler._inbox_page`.
_ROWS_PAGE_KEY = "page"
_SECTION_PAGE_KEYS = {
    "applied": "applied_page", "dismissed": "dismissed_page",
    "muted": "muted_page", "superseded": "superseded_page",
    "gone": "gone_page", "refused": "refused_page",
    "merges": "merges_page", "reconciles": "reconciles_page",
    "unused": "unused_page",
}
_ALL_PAGE_KEYS = (_ROWS_PAGE_KEY,) + tuple(_SECTION_PAGE_KEYS.values())


def _current_pages(qs):
    """Every pagination query key this module knows, read from `qs`
    (`urllib.parse.parse_qs`'s own dict shape) as a 1-based page number --
    `pagination.page_number` is what turns a missing or malformed value into
    page 1, so a link built from this dict never carries a foreign or
    negative page forward.
    """
    return {key: page_number((qs.get(key) or [None])[0])
            for key in _ALL_PAGE_KEYS}


def _pager(path, current_pages, key, total, page_size=PAGE_SIZE):
    """The pagination context one bounded section hands its template: the
    TRUE total this section has (see `cronicled.store.Store.item_count`'s
    own docstring for why that must never be `len` of whatever was
    rendered), its current page, how many pages that total makes, and
    Prev/Next hrefs.

    `total` is carried through into the returned dict UNCHANGED, so a
    template reads a section's stated count (`pagination.<name>.total`)
    from the exact same place it reads the pager -- one context value per
    section, rather than a second `<name>_total` beside it that a future
    edit could update without noticing this one.

    The hrefs carry every OTHER pagination key in `current_pages` forward
    UNCHANGED, so paging through one section never silently resets where a
    person was in another -- the two are independent, and a link that reset
    the rest to page 1 would make one click undo several others.

    `prev_href`/`next_href` are `None` when there is nowhere to go in that
    direction, which is what the template reads to decide whether to draw
    the control at all -- never a link back to the same page.
    """
    page = current_pages[key]
    pages = total_pages(total, page_size)

    def href(target):
        params = dict(current_pages)
        params[key] = target
        # A key sitting at its default (page 1) is dropped, so a page with
        # nothing else paginated keeps a plain, bookmarkable URL instead of
        # carrying every section's `_page=1` on every link.
        kept = {k: v for k, v in params.items() if v != 1}
        query = urllib.parse.urlencode(kept)
        return path + ("?" + query if query else "")

    return {
        "total": total, "page": page, "total_pages": pages,
        "prev_href": href(page - 1) if page > 1 else None,
        "next_href": href(page + 1) if page < pages else None,
    }


def build_handler(rows, actions, scan_status=None, muted=None, dismissed=None,
                  refused=None, superseded=None, applied=None,
                  schedule_status=None, merges=None, reconciles=None,
                  unused=None, gone=None, summary=None, store=None,
                  base_url=None, rows_count=None):
    # `rows` is called as `rows(limit=, offset=)`, never bare -- it answers
    # for exactly ONE bounded window of the generic row list, the same shape
    # `Store.items(limit=, offset=)` already takes (see
    # `cronicled.__main__._inbox_rows`, the real caller). `rows_count`
    # answers the total that window is drawn from -- the same total/window
    # split `Store.item_count`'s own docstring explains, kept here rather
    # than derived by calling `rows` with no bound and taking `len`, which
    # is exactly the whole-page fetch a bound exists to avoid. Defaulted to
    # reporting zero for the same reason every callable below defaults to an
    # empty answer: an existing test's double for `rows` has no opinion
    # about a total, and a page with nothing wired renders as genuinely
    # empty rather than raising.
    _rows_count = rows_count or (lambda: 0)
    # A separate callable rather than always reaching through `actions`:
    # every existing action-path test builds its own recording double for
    # `actions` and none of them implement `scan_status`, so defaulting it
    # here keeps GET / renderable for a double that only knows the four
    # original writes.
    _scan_status = scan_status or (lambda: None)
    # Same reasoning, extended to the five new sections: an existing test's
    # double for `rows`/`actions` knows nothing about muted, dismissed,
    # refused, superseded or applied subjects, so each defaults to reporting
    # none rather than making every such test wire up five more callables it
    # has no opinion about.
    _muted = muted or (lambda: [])
    _dismissed = dismissed or (lambda: [])
    _refused = refused or (lambda: [])
    _superseded = superseded or (lambda: [])
    _applied = applied or (lambda: [])
    # Same reasoning again for the tag-merge section: an existing test's
    # double knows nothing about tag clusters, and a page with no merges to
    # show is the ordinary state of a library with no duplicate spellings.
    _merges = merges or (lambda: [])
    # And again for the tag/performer section: a library where no tag shares a
    # name with a performer has none of these, which is the ordinary state, and
    # an existing test's double has no opinion about them.
    _reconciles = reconciles or (lambda: [])
    # And again for the low-count tag section. A library where every tag is on
    # two scenes or more has none of these, and so does one whose configured
    # source could not be read (see `cronicled.tag_hygiene`) -- both are states
    # the page has to be able to draw as an empty section rather than as an
    # error, and an existing test's double has no opinion about either.
    _unused = unused or (lambda: [])
    # And again for the subjects the media server no longer holds. A library
    # nothing has been deleted from has none, which is the ordinary state, and
    # an existing test's double has no opinion about them.
    _gone = gone or (lambda: [])
    # `None` here is not "no information": it is the answer for an install
    # where nothing is scheduled at all, which is a state the page has to be
    # able to say out loud rather than render as an empty section that looks
    # like a healthy idle one. Ordinarily `cronicled.schedule.Scheduler.status`
    # itself -- see `cronicled.__main__`.
    _schedule_status = schedule_status or (lambda: None)
    # Defaulted through the REAL builder rather than a hand-written dict of
    # the keys it currently emits. A literal stand-in would go stale the day
    # `to_summary_view` grows a key -- and the symptom would be summary.html
    # rendering that key as empty text for every caller that did not wire this
    # up, which is the exact failure mode this layer is arranged to avoid. An
    # install with no runs, nothing waiting and no loop is also a real state
    # (a fresh one), so this is the honest empty page rather than a fixture.
    _summary = summary or (lambda: to_summary_view([], {}, None,
                                                   zone=timezone.utc))
    # Neither defaults to a callable the way every section above does: there
    # is no honest empty stand-in for a store, and a per-inbox route with
    # none wired 404s rather than rendering an inbox that looks empty because
    # nothing can be asked. See `_serve_inbox_route`.
    _store = store
    _base_url = base_url

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body=b"", headers=()):
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path.lstrip("/") in _ACTIONS:
                self._send(405, b"use POST",
                           [("Allow", "POST"),
                            ("Content-Type", "text/plain; charset=utf-8")])
                return
            if path == SUMMARY_PATH:
                self._send(200, self._summary_page(),
                           [("Content-Type", "text/html; charset=utf-8")])
                return
            if path != INBOX_PATH:
                self._serve_inbox_route(path, parsed.query)
                return
            qs = urllib.parse.parse_qs(parsed.query)
            # `applied` in the query string names the fingerprint a
            # successful /approve just redirected here with (see
            # `do_POST`'s own `applied` branch below) -- read-only, and
            # never trusted as anything more than "show a one-row
            # confirmation, above the fold, for the person who is looking at
            # the page right after clicking it". It does NOT open the
            # Applied section -- that drawer is the operator's own and
            # nothing here overrides it; see inbox.html's confirmation
            # banner and its own `just_applied_rows` guard, which is also
            # what keeps a stale or foreign value from producing a
            # confirmation for a row that is not (or no longer) there.
            just_applied = (qs.get("applied") or [None])[0]
            # `opened` names the COLLAPSED section a reversal (`/unmute`,
            # `/undismiss`) just redirected here from, so that section
            # re-opens instead of every write closing it again -- see
            # `do_POST`'s own `_REOPEN_SECTION` use below. Unlike `applied`,
            # this never names a specific row, so there is no stale-row check
            # to make; it is still checked against the fixed set this server
            # actually knows how to reopen, so a stray or foreign query value
            # opens nothing rather than being handed to the template as a
            # section name it has never heard of.
            opened = (qs.get("opened") or [None])[0]
            if opened not in _OPENABLE_SECTIONS:
                opened = None
            # `bulk_requested`/`bulk_applied` name the two counts
            # `/bulk_apply_tag_descriptions` just redirected here with (see
            # `do_POST`'s own branch for that action) -- read-only, exactly
            # like `applied` above, and shown as a banner stating both
            # numbers rather than a single "succeeded" flag, so a partial
            # batch cannot be collapsed into looking like a complete one.
            # Either missing, or either not a plain non-negative integer,
            # means no banner at all -- a stray or foreign query value must
            # not invent a count that was never reported.
            bulk_result = None
            raw_requested = (qs.get("bulk_requested") or [None])[0]
            raw_applied = (qs.get("bulk_applied") or [None])[0]
            if raw_requested is not None and raw_applied is not None:
                try:
                    requested_n = int(raw_requested)
                    applied_n = int(raw_applied)
                    if requested_n < 0 or applied_n < 0:
                        raise ValueError("negative count")
                    bulk_result = {"requested": requested_n,
                                  "applied": applied_n}
                except ValueError:
                    bulk_result = None

            # Every pagination query key this page knows, read once so a
            # link built for one section can carry every other section's
            # own current page forward unchanged -- see `_pager`.
            current_pages = _current_pages(qs)

            # THE GENERIC ROW LIST. `rows(limit=, offset=)` fetches exactly
            # this page's own window -- never the whole waiting queue just
            # to slice it in Python -- and `_rows_count()` answers the total
            # it is a window OF, which is a separate question from what got
            # rendered (see `build_handler`'s own comment on `rows_count`).
            # THIS WINDOW, in this exact order, is "the rows this page
            # rendered": every row object it contains keeps its own
            # `fingerprint` (see `cronicled.web.pagination`'s module
            # docstring), so nothing downstream has to re-derive that set by
            # asking the store again.
            rows_page = current_pages[_ROWS_PAGE_KEY]
            rows_window = rows(limit=PAGE_SIZE, offset=offset_for(rows_page))
            rows_pager = _pager(INBOX_PATH, current_pages, _ROWS_PAGE_KEY,
                               _rows_count())

            # Every other section is fetched WHOLE (as it always was -- see
            # `cronicled.web.pagination`'s module docstring for why these,
            # unlike the generic list, are windowed here rather than at the
            # store) and then bounded to its own page's window; `_pager`
            # carries the section's TRUE total through for its count badge,
            # never `len(<the window>)`.
            sections = {}
            pagers = {}
            for name, fetch in (("applied", _applied),
                                ("dismissed", _dismissed),
                                ("muted", _muted),
                                ("superseded", _superseded),
                                ("gone", _gone),
                                ("refused", _refused),
                                ("merges", _merges),
                                ("reconciles", _reconciles)):
                full = fetch()
                key = _SECTION_PAGE_KEYS[name]
                sections[name] = window(full, current_pages[key])
                pagers[name] = _pager(INBOX_PATH, current_pages, key,
                                      len(full))

            unused_key = _SECTION_PAGE_KEYS["unused"]
            unused_windowed, unused_total = windowed_unused_groups(
                _unused(), current_pages[unused_key])
            pagers["unused"] = _pager(INBOX_PATH, current_pages, unused_key,
                                      unused_total)

            body = render("inbox.html", rows=rows_window, counts={},
                         bulk_result=bulk_result,
                         muted=sections["muted"],
                         dismissed=sections["dismissed"],
                         refused=sections["refused"],
                         superseded=sections["superseded"],
                         applied=sections["applied"],
                         merges=sections["merges"],
                         reconciles=sections["reconciles"],
                         unused=unused_windowed,
                         gone=sections["gone"],
                         # Read off the module that owns the claim rather than
                         # typed into the template, for the reason
                         # `MergeRow.warning` reads `tags
                         # .MERGE_IS_IRREVERSIBLE`: a second copy of a sentence
                         # this important is a second copy free to drift.
                         low_count_is_not_proof=LOW_COUNT_IS_NOT_PROOF,
                         schedule=_schedule_status(),
                         just_applied=just_applied,
                         sidebar=_sidebar_context(_store),
                         opened=opened,
                         pagination=dict(rows=rows_pager, **pagers)).encode()
            self._send(200, body,
                       [("Content-Type", "text/html; charset=utf-8")])

        def _summary_page(self):
            """The landing page's body.

            The Scan control lives here, so this is the page that carries the
            running scan's status and the limit its form is pre-filled with.
            The schedule panel reaches the template inside the summary view
            (see `rows.to_summary_view`), not as a second variable, so this
            page cannot draw one status beside a differently-converted copy of
            the other.
            """
            return render("summary.html", summary=_summary(),
                          scan=_scan_status(),
                          scan_default_limit=DEFAULT_SCAN_LIMIT,
                          sidebar=_sidebar_context(_store)).encode()

        def _serve_inbox_route(self, path, query):
            """`/{inbox}` and `/{inbox}/{state}` -- everything that is
            neither the summary nor the combined inbox.

            Parsed into AT MOST two segments and resolved against `INBOXES`
            and `_INBOX_STATES` before anything is queried: an inbox name
            this map does not know, or a second segment that is not one of
            the three terminal states served here, is a 404 -- never a fall
            through to the combined inbox, which would make a typo read as
            "nothing to review" rather than as the wrong address it is.
            """
            segments = [s for s in path.split("/") if s]
            if not (1 <= len(segments) <= 2) or segments[0] not in INBOXES:
                self._send(404, b"not found")
                return
            state = None
            if len(segments) == 2:
                if segments[1] not in _INBOX_STATES:
                    self._send(404, b"not found")
                    return
                state = segments[1]
            if _store is None:
                self._send(404, b"not found")
                return
            body = self._inbox_page(segments[0], state, query).encode()
            self._send(200, body, [("Content-Type", "text/html; charset=utf-8")])

        def _inbox_page(self, name, state, query):
            """The body of `/{name}` (`state` is `None`) or `/{name}/{state}`.

            The GENERIC row list -- everything `to_rows` can build a row
            for -- is narrowed to `name`'s own subject types minus
            `_SECTION_SUBJECTS` (see `_scene_subject_types`) via `Store.items
            (subject_types=)`, and, for a terminal-state page, further to
            `state`. It is fetched a PAGE at a time straight from the store
            (`limit`/`offset`, and `Store.item_count` for the total this
            window is drawn from) -- the same `?page=` key the combined
            `/inbox` page's own generic list uses, reused rather than a
            second name, because on this page there is exactly one list to
            paginate and no ambiguity about which section it means.

            The three subject types `_SECTION_SUBJECTS` names are composed
            SEPARATELY, as their own sections, and only on the working-queue
            view (`state is None`): each already has a dedicated builder
            (`to_merge_rows`/`to_reconcile_rows`/`to_unused_groups`) reached
            through the SAME `_merges`/`_reconciles`/`_unused` closures the
            combined `/inbox` page composes from (see `build_handler`'s own
            parameters) -- reused here rather than re-read from `_store`, so
            there is exactly one place that ever builds a "Tag merges" (etc.)
            section, for either page. Every one of those closures already
            reads across every state a cluster/reconciliation/low-count tag
            can carry a control in (`new`/`seen`/`failed` via `items()`'s own
            default, plus `dismissed` and `muted` explicitly -- see
            `cronicled.__main__._merge_rows`'s own docstring for why that is
            three store reads, not one), so a dismissed or muted one reaches
            this page, with its Undismiss/Unmute control, exactly as it does
            on the combined page -- INLINE in its own section, never via the
            terminal `/{name}/dismissed` or `/{name}/muted` route, which only
            ever draws the generic list above. Each of the three is bounded
            the same way the combined page bounds it -- windowed in Python
            over the list those closures already build whole -- under its
            own `?{name}_page=` key.

            Only included when `name`'s OWN inbox owns that subject type
            (checked against the full, unnarrowed `INBOXES[name]`) -- `/tags`
            gets all three, `/scenes` and `/performers` get none, because
            `inboxes.INBOXES` maps each of the three to `tags` alone.
            """
            current_pages = _current_pages(urllib.parse.parse_qs(query))
            path = "/" + "/".join(s for s in (name, state) if s)
            types = INBOXES[name]
            scene_types = _scene_subject_types(types)
            # `state is None` (the working queue) also excludes `applied` --
            # a decision already made does not belong there -- the same
            # exclusion the combined page's own generic list makes; see
            # `Store.items`'s own `exclude_states` docstring for why this is
            # a store-level argument now rather than a Python filter applied
            # AFTER a page's already-bounded fetch, which could leave a page
            # short of its own bound for no reason a reader would see.
            exclude = ("applied",) if state is None else ()
            rows_page = current_pages[_ROWS_PAGE_KEY]
            total = _store.item_count(subject_types=scene_types, state=state,
                                      exclude_states=exclude)
            items = _store.items(subject_types=scene_types, state=state,
                                 exclude_states=exclude, limit=PAGE_SIZE,
                                 offset=offset_for(rows_page))
            built = to_rows(items, base_url=_base_url)
            pagers = {"rows": _pager(path, current_pages, _ROWS_PAGE_KEY,
                                    total)}
            context = {"rows": [], "applied": [], "dismissed": [], "muted": []}
            if state is None:
                context["rows"] = built
            elif state == "muted":
                # `Row`/`DescriptionRow`/`TagDescriptionRow` carry no
                # `subject_type`/`subject_id` of their own -- `to_mute_row`'s
                # dict shape carries both alongside the row precisely so
                # Unmute has something to post, and that shape needs
                # `Store.mutes()`, not `Store.items()`. Rebuilt here from the
                # raw item instead, which is where `subject_type`/
                # `subject_id` still are at this point. `reason` and `at`
                # are NOT rebuilt -- that provenance lives only in the
                # `mute` table `Store.mutes()` reads, so this page shows the
                # identity and the Unmute control and leaves those two
                # blank rather than inventing them.
                context["muted"] = [
                    {"subject_type": item["subject_type"],
                     "subject_id": item["subject_id"], "row": row}
                    for item, row in zip(items, built)]
            else:
                context[state] = built
            # `merges`/`reconciles`/`unused` are left OUT of the render call
            # entirely -- never passed as `[]` -- for any inbox that does not
            # own the corresponding subject type, and for any state other
            # than the working queue: inbox.html tells the two apart (`is
            # defined`) and renders none of those three sections at all when
            # the keyword is simply absent, rather than rendering each as a
            # false "nothing found" for a section this page never asked the
            # store about.
            extra = {}
            if state is None:
                if _MERGE_SUBJECT in types:
                    full = _merges()
                    key = _SECTION_PAGE_KEYS["merges"]
                    extra["merges"] = window(full, current_pages[key])
                    pagers["merges"] = _pager(path, current_pages, key,
                                              len(full))
                if _RECONCILE_SUBJECT in types:
                    full = _reconciles()
                    key = _SECTION_PAGE_KEYS["reconciles"]
                    extra["reconciles"] = window(full, current_pages[key])
                    pagers["reconciles"] = _pager(path, current_pages, key,
                                                  len(full))
                if _UNUSED_TAG_SUBJECT in types:
                    key = _SECTION_PAGE_KEYS["unused"]
                    windowed, utotal = windowed_unused_groups(
                        _unused(), current_pages[key])
                    extra["unused"] = windowed
                    pagers["unused"] = _pager(path, current_pages, key,
                                              utotal)
            return render(
                "inbox.html", title=TITLES[name],
                rows=context["rows"], applied=context["applied"],
                dismissed=context["dismissed"], muted=context["muted"],
                refused=[], superseded=[], gone=[], counts={},
                low_count_is_not_proof=LOW_COUNT_IS_NOT_PROOF,
                schedule=None, just_applied=None,
                sidebar=_sidebar_context(_store),
                pagination=pagers,
                **extra)

        def _cross_origin_write(self):
            """Refuse a write whose own headers say it did not originate
            from a page this server served.

            The binding stops other HOSTS from reaching this port; it does
            nothing about the user's OWN browser, which can reach
            127.0.0.1 from any tab regardless of which page is open. That
            is the gap this closes.

            `Sec-Fetch-Site` is sent by every current mainstream browser
            (Chrome, Firefox, Safari) on every request, including a plain
            form POST navigation, not just `fetch`/XHR -- so it is checked
            first and is the primary defence. `Origin` is checked too, when
            present, as a second and independently-sent signal: belt and
            braces, not a fallback for browsers that lack the first.

            Neither header is sent by a plain HTTP client (curl, a script)
            that talks to this server directly on purpose. Such a client
            is not a browser tricked by an unrelated page into carrying a
            request -- it IS the person operating this tool, so a request
            with NEITHER header present is let through.

            The honest cost of that choice: a browser too old to send
            Fetch Metadata headers would also pass uncontested and would
            rely on the binding alone, exactly as before this change. That
            gap is accepted and named here rather than hidden -- it does
            not include any browser in current mainstream use.
            """
            sec_fetch_site = self.headers.get("Sec-Fetch-Site")
            if (sec_fetch_site is not None
                    and sec_fetch_site not in _ACCEPTABLE_SEC_FETCH_SITE):
                return True
            origin = self.headers.get("Origin")
            if (origin is not None
                    and not _origin_matches_host(
                        origin, self.headers.get("Host", ""))):
                return True
            return False

        def do_POST(self):
            name = urllib.parse.urlparse(self.path).path.lstrip("/")
            if name not in _ACTIONS:
                self._send(404, b"not found")
                return
            if self._cross_origin_write():
                self._send(403, b"refused: this request declares itself "
                                 b"cross-origin",
                           [("Content-Type", "text/plain; charset=utf-8")])
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, b"malformed content-length",
                           [("Content-Type", "text/plain; charset=utf-8")])
                return
            # `bulk_apply_tag_descriptions` alone gets the larger, still
            # bounded ceiling -- see `_MAX_BULK_BODY_BYTES`'s own comment.
            # Every other action keeps the tight one nothing genuine ever
            # approaches.
            max_body = (_MAX_BULK_BODY_BYTES
                       if name == "bulk_apply_tag_descriptions"
                       else _MAX_BODY_BYTES)
            if not (0 <= length <= max_body):
                self._send(400, b"malformed content-length",
                           [("Content-Type", "text/plain; charset=utf-8")])
                return
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8"))
            # Overridden below only for a successful "approve" -- the one
            # write that moves a row into the Applied section (ticket 98).
            # Carrying its fingerprint on the redirect is what lets the very
            # next GET show a one-row confirmation for exactly that row
            # (see inbox.html) without opening the Applied drawer itself --
            # the section's own open/closed state is the operator's, the
            # same as every other section, and a row moving into it is not
            # a reason to override that choice.
            #
            # Back to the INBOX, not to the landing page: every one of these
            # writes is a judgement made on a row while looking at the other
            # rows, and a redirect to the summary would answer each decision by
            # closing the list it was made from. `scan` is the exception and
            # overrides this below -- its control is on the summary page, so
            # that is the page it returns to.
            location = INBOX_PATH
            if name == "scan":
                location = SUMMARY_PATH
                # `limit` is required here on the same terms
                # `cronicled.runscan.build_producer` requires it of the CLI:
                # no permissive default, so a request that omits it (or
                # sends something that is not a number) is refused before
                # anything is registered or started -- never silently
                # treated as "no limit", which `scan.select` reads as
                # "scan everything".
                raw_limit = (form.get("limit") or [""])[0]
                if raw_limit == "":
                    self._send(400, b"missing limit")
                    return
                try:
                    limit = int(raw_limit)
                except ValueError:
                    self._send(400, b"malformed limit",
                               [("Content-Type", "text/plain; charset=utf-8")])
                    return
                try:
                    actions.scan(limit)
                except Exception as exc:
                    self._send(400, str(exc).encode("utf-8"),
                               [("Content-Type", "text/plain; charset=utf-8")])
                    return
            elif name == "unmute":
                # Shaped differently from every other action: a mute is keyed
                # by (subject_type, subject_id), not by a proposal's
                # fingerprint (see `Store.mute`), so there is no single `fp`
                # this control can carry -- it posts the pair instead.
                subject_type = (form.get("subject_type") or [""])[0]
                subject_id = (form.get("subject_id") or [""])[0]
                if not subject_type or not subject_id:
                    self._send(400, b"missing subject")
                    return
                try:
                    actions.unmute(subject_type, subject_id)
                except Exception as exc:
                    self._send(400, str(exc).encode("utf-8"),
                               [("Content-Type", "text/plain; charset=utf-8")])
                    return
                location = "%s?opened=%s" % (INBOX_PATH, _REOPEN_SECTION[name])
            elif name == "bulk_apply_tag_descriptions":
                # Every hidden `fp` field the bulk form on the page posted --
                # `parse_qs` already collects repeated keys into a list, so
                # this IS the exact, ordered set the page rendered. Nothing
                # here re-derives a set from a filter; see
                # `Actions.bulk_apply_tag_descriptions`'s own docstring for
                # why that distinction is the whole point of this action.
                fps = form.get("fp") or []
                if not fps:
                    self._send(400, b"missing fingerprint")
                    return
                try:
                    result = actions.bulk_apply_tag_descriptions(fps)
                except Exception as exc:
                    self._send(400, str(exc).encode("utf-8"),
                               [("Content-Type", "text/plain; charset=utf-8")])
                    return
                # The two counts travel on the redirect, not a single
                # "succeeded" flag: the page states "N of M applied" from
                # both, so a partial batch cannot render as a plain success
                # by there being nowhere on the page to say otherwise.
                location = "%s?bulk_requested=%d&bulk_applied=%d" % (
                    INBOX_PATH, len(result.requested), len(result.applied))
            else:
                fp = (form.get("fp") or [""])[0]
                if not fp:
                    self._send(400, b"missing fingerprint")
                    return
                try:
                    getattr(actions, name)(fp)
                except Exception as exc:
                    self._send(400, str(exc).encode("utf-8"),
                               [("Content-Type", "text/plain; charset=utf-8")])
                    return
                if name == "approve":
                    location = "%s?applied=%s" % (
                        INBOX_PATH, urllib.parse.quote(fp, safe=""))
                elif name in _REOPEN_SECTION:
                    location = "%s?opened=%s" % (INBOX_PATH, _REOPEN_SECTION[name])
            # 303 so a refresh redraws the page rather than repeating the write.
            self._send(303, b"", [("Location", location)])

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(rows, actions, scan_status=None, muted=None, dismissed=None,
         refused=None, superseded=None, applied=None, schedule_status=None,
         merges=None, reconciles=None, unused=None, gone=None, summary=None,
         store=None, base_url=None, host=DEFAULT_HOST, port=DEFAULT_PORT,
         rows_count=None):
    # `HTTPServer` is single-threaded: one connection wedged on a slow read
    # or a slow downstream call (a media server taking its whole configured
    # timeout to answer an Approve, say) stalls every other request -- an
    # unrelated GET for the page itself included -- until it clears.
    #
    # `ThreadingHTTPServer` (stdlib, same module) would remove that freeze
    # for free and was evaluated as a straight swap here. Rejected: it would
    # let two Approve/Undo requests run concurrently against the SAME
    # `Actions`/`Stash`, and while `Store`'s own lock serializes its SQL
    # (safe on its own), `Stash.apply_scene` is a multi-step check-then-act
    # sequence against the REMOTE server -- find-or-create for each studio/
    # performer/tag, then one write -- with no lock of its own. Two
    # concurrent approvals (a double-submitted click, or two proposals that
    # happen to name the same new performer) could each find nothing, each
    # create, and leave the library with two entities for one name. That is
    # a silent, hard-to-notice corruption of the thing this tool exists to
    # curate, traded for a freeze that is at least visible and bounded (the
    # client's own configured timeout, and `Stash`'s HARD_DEADLINE_SLACK on
    # top of it). Making concurrent Approves safe would need its own guard
    # (serializing writes per subject, at least) and is out of scope here --
    # this single-threaded server, and the freeze it implies, stays as a
    # documented limitation rather than a silently traded one.
    if host != DEFAULT_HOST:
        # Loud, because there is no authentication: the binding is the only
        # thing standing between this page and anyone who can reach the
        # host -- and inside a container this fires on EVERY start, not just
        # a mistake, because 127.0.0.1 in there answers nothing `docker run
        # -p` forwards to it. Repeating a true warning every time is the
        # accepted cost: what it says stays correct no matter how often it
        # prints, and silencing it on the container path would hide the one
        # thing an operator most needs to get right -- which this message
        # names directly, rather than just naming the bind host that no
        # longer decides it.
        print("WARNING: binding to %s, not %s. This page has NO "
              "authentication and its buttons write to your library. If "
              "this is a container, the bind host above is not what "
              "protects you -- 0.0.0.0 is required in there just to be "
              "reachable at all. What protects you is how `docker run` "
              "published the port: `-p 127.0.0.1:%d:%d` keeps it reachable "
              "only from this machine; `-p %d:%d` (or -P) publishes this "
              "same unauthenticated page to every network this host is on."
              % (host, DEFAULT_HOST, port, port, port, port))
    httpd = HTTPServer((host, port), build_handler(
        rows, actions, scan_status, muted=muted, dismissed=dismissed,
        refused=refused, superseded=superseded, applied=applied,
        schedule_status=schedule_status, merges=merges,
        reconciles=reconciles, unused=unused, gone=gone, summary=summary,
        store=store, base_url=base_url, rows_count=rows_count))
    # Names what is actually at that address. It said "inbox" when the inbox
    # was the landing page; a start-up line that keeps naming the page that
    # used to be there sends the one person reading it to the wrong place.
    print("cronicled on http://%s:%d%s (the inbox is at %s)"
          % (host, port, SUMMARY_PATH, INBOX_PATH))
    httpd.serve_forever()
