"""The HTTP surface: routing, and nothing else.

Every write is a POST followed by a redirect. A GET to an action path is
refused rather than tolerated — browsers prefetch, and a link that applies a
proposal will eventually be followed by something that is not a person.
"""

import urllib.parse
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from cronicled.tag_hygiene import LOW_COUNT_IS_NOT_PROOF

from .render import render
from .rows import to_summary_view

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8571

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
                  unused=None, gone=None, summary=None):
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
                self._send(404, b"not found")
                return
            # `applied` in the query string names the fingerprint a
            # successful /approve just redirected here with (see
            # `do_POST`'s own `applied` branch below) -- read-only, and
            # never trusted as anything more than "open this one row's
            # section for the person who is looking at the page right
            # after clicking it". A stale or foreign value simply fails to
            # match any row the template actually has (see inbox.html's own
            # `just_applied_rows` guard), so there is nothing here to
            # validate beyond parsing it out of the query string.
            just_applied = (urllib.parse.parse_qs(parsed.query)
                            .get("applied") or [None])[0]
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
                         just_applied=just_applied).encode()
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
                          scan_default_limit=DEFAULT_SCAN_LIMIT).encode()

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
            # write that moves a row into the new Applied section (ticket
            # 98). Carrying its fingerprint on the redirect is what lets the
            # very next GET open that section and mark that one row, rather
            # than requiring the person to go find it themselves among
            # everything else ever applied.
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
            # 303 so a refresh redraws the page rather than repeating the write.
            self._send(303, b"", [("Location", location)])

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(rows, actions, scan_status=None, muted=None, dismissed=None,
         refused=None, superseded=None, applied=None, schedule_status=None,
         merges=None, reconciles=None, unused=None, gone=None, summary=None,
         host=DEFAULT_HOST, port=DEFAULT_PORT):
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
        reconciles=reconciles, unused=unused, gone=gone, summary=summary))
    # Names what is actually at that address. It said "inbox" when the inbox
    # was the landing page; a start-up line that keeps naming the page that
    # used to be there sends the one person reading it to the wrong place.
    print("cronicled on http://%s:%d%s (the inbox is at %s)"
          % (host, port, SUMMARY_PATH, INBOX_PATH))
    httpd.serve_forever()
