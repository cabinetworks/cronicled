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
from .render import render
from .rows import to_rows, to_summary_view

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8571

# The three subject types `rows.to_rows` cannot build a row for. It dispatches
# only on `rows.DESCRIPTION_SUBJECT`/`rows.TAG_DESCRIPTION_SUBJECT` and treats
# everything else as scene-shaped (see `to_rows`), so a tag-cluster,
# tag-performer or tag-unused item forced through it KeyErrors on
# `payload["path"]` — the same reason `cronicled.__main__`'s own
# `_OWN_SECTION_SUBJECTS` excludes these three before calling `to_rows` for
# the combined inbox. Named again here, independently, because a per-inbox
# page filters its OWN group down to what it can safely show rather than
# filtering one shared list down to what is left over.
#
# THE RESIDUAL, NAMED RATHER THAN HIDDEN: a working merge, reconciliation or
# low-count-tag proposal still reaches the combined `/inbox` page exactly as
# it does today (`cronicled.__main__` still wires it there); it simply does
# not yet appear on the narrower `/tags` page, which does not yet carry the
# three other row shapes (`to_merge_rows`, `to_reconcile_rows`,
# `to_unused_groups`) these subject types need. That is a real gap in what
# `/tags` shows today, not a hidden one.
_NO_ROW_BUILDER = (_MERGE_SUBJECT, _RECONCILE_SUBJECT, _UNUSED_TAG_SUBJECT)

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

    THE COUNT IS DELIBERATELY NARROWED to `INBOXES[name]` minus
    `_NO_ROW_BUILDER`, exactly the same subject types `_inbox_page` asks the
    store for. A tag inbox has four subject types but `/tags` can only ever
    render one of them (see `_NO_ROW_BUILDER`'s own comment for why the other
    three still take no row on that page); counting the full four here would
    put a bigger number on this link than the page it points at can ever
    show, with nothing on either page saying why. This is the cheaper of the
    two honest fixes named for that gap -- render only what can be rendered,
    and count only that -- rather than the more thorough one (composing the
    merge/reconcile/hygiene sections onto every per-inbox page too), because
    the combined `/inbox` page already carries those three sections in full;
    nothing here makes them harder to reach, only absent from a NUMBER this
    narrower page did not previously have at all.

    One `store.counts()` call answers "how many, in every non-hidden state"
    for the inbox as a whole -- summed here excluding `applied`, the same way
    `_inbox_page` itself drops an applied row from the working-queue view.
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
        types = tuple(t for t in INBOXES[name] if t not in _NO_ROW_BUILDER)
        counts = store.counts(subject_types=types)
        waiting = sum(n for state, n in counts.items() if state != "applied")
        states = []
        for state in _INBOX_STATES:
            if state == "applied":
                present = counts.get("applied", 0) > 0
            else:
                present = bool(store.items(subject_types=types, state=state,
                                           limit=1))
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
           "unmute", "undismiss", "refresh")

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


def build_handler(rows, actions, scan_status=None, muted=None, dismissed=None,
                  refused=None, superseded=None, applied=None,
                  schedule_status=None, merges=None, reconciles=None,
                  unused=None, gone=None, summary=None, store=None,
                  base_url=None):
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
                self._serve_inbox_route(path)
                return
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
            just_applied = (urllib.parse.parse_qs(parsed.query)
                            .get("applied") or [None])[0]
            # `opened` names the COLLAPSED section a reversal (`/unmute`,
            # `/undismiss`) just redirected here from, so that section
            # re-opens instead of every write closing it again -- see
            # `do_POST`'s own `_REOPEN_SECTION` use below. Unlike `applied`,
            # this never names a specific row, so there is no stale-row check
            # to make; it is still checked against the fixed set this server
            # actually knows how to reopen, so a stray or foreign query value
            # opens nothing rather than being handed to the template as a
            # section name it has never heard of.
            opened = (urllib.parse.parse_qs(parsed.query)
                     .get("opened") or [None])[0]
            if opened not in _OPENABLE_SECTIONS:
                opened = None
            body = render("inbox.html", rows=rows(), counts={},
                         muted=_muted(), dismissed=_dismissed(),
                         refused=_refused(),
                         superseded=_superseded(),
                         applied=_applied(),
                         merges=_merges(),
                         reconciles=_reconciles(),
                         unused=_unused(),
                         gone=_gone(),
                         # Read off the module that owns the claim rather than
                         # typed into the template, for the reason
                         # `MergeRow.warning` reads `tags
                         # .MERGE_IS_IRREVERSIBLE`: a second copy of a sentence
                         # this important is a second copy free to drift.
                         low_count_is_not_proof=LOW_COUNT_IS_NOT_PROOF,
                         schedule=_schedule_status(),
                         just_applied=just_applied,
                         sidebar=_sidebar_context(_store),
                         opened=opened).encode()
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

        def _serve_inbox_route(self, path):
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
            body = self._inbox_page(segments[0], state).encode()
            self._send(200, body, [("Content-Type", "text/html; charset=utf-8")])

        def _inbox_page(self, name, state):
            """The body of `/{name}` (`state` is `None`) or `/{name}/{state}`.

            Narrowed to `name`'s own subject types via `Store.items
            (subject_types=)` -- see `inboxes.INBOXES` -- and, for a
            terminal-state page, further to `state`. `_NO_ROW_BUILDER`'s
            three subject types are excluded from what is asked for at
            all: see its own comment for why forcing one of them through
            `to_rows` is not survivable, and for what is NOT yet shown here
            because of it.
            """
            types = tuple(t for t in INBOXES[name] if t not in _NO_ROW_BUILDER)
            items = _store.items(subject_types=types, state=state)
            if state is None:
                # `items()`'s own default excludes `dismissed`/`muted`/
                # `superseded`/`gone` (see `Store._HIDDEN_STATES`) but NOT
                # `applied` -- an applied proposal is a decision already
                # made, and the working queue is not where it belongs.
                # `cronicled.__main__._inbox_rows` filters the same way,
                # for the same reason, against the combined inbox.
                items = [item for item in items if item["state"] != "applied"]
            built = to_rows(items, base_url=_base_url)
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
            # `merges`/`reconciles`/`unused` are left OUT of this call
            # entirely, rather than passed as `[]` -- inbox.html tells the
            # two states apart (`is defined`) and renders none of those three
            # sections at all here, rather than rendering each as a false
            # "nothing found" for subject types this page never asked the
            # store about. See `_sidebar_context` for the other half of this
            # same decision: what a per-inbox page cannot show, it does not
            # count either.
            return render(
                "inbox.html", title=TITLES[name],
                rows=context["rows"], applied=context["applied"],
                dismissed=context["dismissed"], muted=context["muted"],
                refused=[], superseded=[], gone=[], counts={},
                low_count_is_not_proof=LOW_COUNT_IS_NOT_PROOF,
                schedule=None, just_applied=None,
                sidebar=_sidebar_context(_store))

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
            if not (0 <= length <= _MAX_BODY_BYTES):
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
         store=None, base_url=None, host=DEFAULT_HOST, port=DEFAULT_PORT):
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
        store=store, base_url=base_url))
    # Names what is actually at that address. It said "inbox" when the inbox
    # was the landing page; a start-up line that keeps naming the page that
    # used to be there sends the one person reading it to the wrong place.
    print("cronicled on http://%s:%d%s (the inbox is at %s)"
          % (host, port, SUMMARY_PATH, INBOX_PATH))
    httpd.serve_forever()
