import email.message
import http.client
import io
import threading
import unittest
from contextlib import redirect_stdout
from http.server import HTTPServer
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cronicled.jobs import JobRejected
from cronicled.schedule import LoopStatus, TickResult
from cronicled.web.actions import ApplyFailed, UnknownProposal
from cronicled.web.app import build_handler, serve, DEFAULT_HOST
from cronicled.web.rows import to_summary_view


class _RecordingActions:
    """Records each call before answering, but can be told to raise for a
    given action instead of recording it -- mirroring the two ways the real
    Actions is known to fail a caller rather than answer: `UnknownProposal`
    for a fingerprint it no longer has, and `ApplyFailed` for an apply the
    media server refused. A double that never raises is more forgiving than
    the thing it stands in for, and that gap has cost this project a shipped
    bug before."""

    def __init__(self, fail=None):
        self.calls = []
        self._fail = fail or {}

    def _do(self, name, fp, ok):
        if name in self._fail:
            raise self._fail[name]
        self.calls.append((name, fp))
        return ok

    def approve(self, fp):
        return self._do("approve", fp, "applied")

    def dismiss(self, fp):
        return self._do("dismiss", fp, "dismissed")

    def mute(self, fp):
        return self._do("mute", fp, "muted")

    def undo(self, fp):
        return self._do("undo", fp, "reverted")

    def undismiss(self, fp):
        return self._do("undismiss", fp, "undismissed")

    def refresh(self, fp):
        return self._do("refresh", fp, "refreshed")

    def scan(self, limit):
        return self._do("scan", limit, "started")

    def unmute(self, subject_type, subject_id):
        if "unmute" in self._fail:
            raise self._fail["unmute"]
        self.calls.append(("unmute", subject_type, subject_id))
        return "unmuted"


class _Server:
    def __init__(self, fail=None):
        self._fail = fail

    def __enter__(self):
        self.actions = _RecordingActions(fail=self._fail)
        handler = build_handler(rows=lambda: [], actions=self.actions)
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address)
        all_headers = ({"Content-Type": "application/x-www-form-urlencoded"}
                       if body else {})
        if headers:
            all_headers.update(headers)
        conn.request(method, path, body, all_headers)
        return conn.getresponse()


class GetNeverWrites(unittest.TestCase):
    def test_a_get_to_an_action_path_does_not_perform_it(self):
        # Browsers prefetch. A link that applies a proposal will eventually be
        # followed by something that is not a person.
        with _Server() as s:
            r = s.request("GET", "/approve?fp=fp-1")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])

    def test_the_index_renders_without_writing(self):
        with _Server() as s:
            r = s.request("GET", "/")
            self.assertEqual(r.status, 200)
            self.assertEqual(s.actions.calls, [])

    def test_a_get_to_scan_does_not_start_one(self):
        # `/scan` starts a job that spends a rate-limited third party's
        # budget -- exactly the control a prefetching browser, or a link
        # followed by something that is not a person, must never trigger.
        with _Server() as s:
            r = s.request("GET", "/scan")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])

    def test_a_get_to_unmute_does_not_perform_it(self):
        # Acceptance for ticket 75: a GET must never unmute anything.
        with _Server() as s:
            r = s.request(
                "GET", "/unmute?subject_type=scene&subject_id=42")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])

    def test_a_get_to_undismiss_does_not_perform_it(self):
        with _Server() as s:
            r = s.request("GET", "/undismiss?fp=fp-1")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])

    def test_a_get_to_refresh_does_not_perform_it(self):
        # Acceptance for ticket 86: a GET must never supersede anything.
        with _Server() as s:
            r = s.request("GET", "/refresh?fp=fp-1")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])


class Posts(unittest.TestCase):
    def test_each_action_path_reaches_its_action(self):
        for path, name in (("/approve", "approve"), ("/dismiss", "dismiss"),
                           ("/mute", "mute"), ("/undo", "undo"),
                           ("/undismiss", "undismiss"),
                           ("/refresh", "refresh")):
            with _Server() as s:
                r = s.request("POST", path, "fp=fp-1")
                self.assertEqual(s.actions.calls, [(name, "fp-1")])
                # Redirect, so a refresh does not repeat the write.
                self.assertEqual(r.status, 303)

    def test_a_post_without_a_fingerprint_is_rejected(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])


class ApproveRedirectCarriesTheFingerprint(unittest.TestCase):
    """Ticket 98: undo has to stay reachable for the approve just made and
    immediately regretted. Carrying the fingerprint on `/approve`'s own
    redirect is what lets the very next GET open the Applied section on
    exactly that row -- see inbox.html's `just_applied_rows` guard.
    """

    def test_a_successful_approve_redirects_with_its_own_fingerprint(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-7")
            self.assertEqual(r.status, 303)
            self.assertEqual(r.getheader("Location"), "/inbox?applied=fp-7")

    def test_other_actions_redirect_to_the_plain_index(self):
        # Only approve moves a row into Applied -- every other action must
        # not carry this query param at all.
        for path, name in (("/dismiss", "dismiss"), ("/mute", "mute"),
                           ("/undo", "undo"), ("/undismiss", "undismiss"),
                           ("/refresh", "refresh")):
            with _Server() as s:
                r = s.request("POST", path, "fp=fp-7")
                self.assertEqual(r.getheader("Location"), "/inbox", name)

    def test_a_failed_approve_does_not_redirect_at_all(self):
        # `ApplyFailed` is reported as an error (see `ExceptionBranch`), not
        # a redirect -- nothing here to carry a fingerprint on.
        with _Server(fail={"approve":
                           ApplyFailed("could not apply: a fixture failure")}
                     ) as s:
            r = s.request("POST", "/approve", "fp=fp-7")
            self.assertEqual(r.status, 400)

    def test_the_fingerprint_is_url_encoded(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp%2Fwith%2Fslashes")
            self.assertEqual(s.actions.calls, [("approve", "fp/with/slashes")])
            self.assertEqual(r.getheader("Location"),
                             "/inbox?applied=fp%2Fwith%2Fslashes")


class UnmutePost(unittest.TestCase):
    # `/unmute` carries a different shape from the other five actions: a
    # mute is keyed by (subject_type, subject_id), not by a proposal's
    # fingerprint, so there is no single `fp` to post.

    def test_posting_the_subject_reaches_unmute_and_redirects(self):
        with _Server() as s:
            r = s.request("POST", "/unmute",
                          "subject_type=scene&subject_id=42")
            self.assertEqual(s.actions.calls, [("unmute", "scene", "42")])
            self.assertEqual(r.status, 303)

    def test_a_post_missing_the_subject_id_is_rejected(self):
        with _Server() as s:
            r = s.request("POST", "/unmute", "subject_type=scene")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_post_missing_the_subject_type_is_rejected(self):
        with _Server() as s:
            r = s.request("POST", "/unmute", "subject_id=42")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_cross_site_post_is_refused_and_does_not_fire(self):
        with _Server() as s:
            r = s.request("POST", "/unmute",
                          "subject_type=scene&subject_id=42",
                          headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])

    def test_an_unmute_failure_is_reported_as_an_error_not_a_redirect(self):
        with _Server(fail={"unmute": UnknownProposal(("scene", "42"))}) as s:
            r = s.request("POST", "/unmute",
                          "subject_type=scene&subject_id=42")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])


class MutedDismissedRefusedRendering(unittest.TestCase):
    """`build_handler`'s five new callables reach the page a GET renders --
    the plumbing between a store and the template, as opposed to how the
    template itself renders a given row (covered in
    tests/test_web_render.py)."""

    def _get(self, path="/inbox", **callables):
        actions = _RecordingActions()
        handler = build_handler(rows=lambda: [], actions=actions, **callables)
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(*httpd.server_address)
            conn.request("GET", path)
            return conn.getresponse().read().decode()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_the_muted_count_reaches_the_page(self):
        html = self._get(muted=lambda: [
            {"subject_type": "scene", "subject_id": "1",
             "reason": "never identifiable", "at": "2026-07-01T00:00:00"}])
        self.assertIn("Muted (1)", html)

    def test_the_superseded_count_reaches_the_page(self):
        html = self._get(superseded=lambda: [
            {"fingerprint": "fp-1", "state": "superseded",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        self.assertIn("Superseded (1)", html)

    def test_the_applied_count_reaches_the_page(self):
        html = self._get(applied=lambda: [
            {"fingerprint": "fp-1", "state": "applied",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        self.assertIn("Applied (1)", html)

    def test_the_gone_count_reaches_the_page(self):
        # HARM: a state with nowhere to render is a state that vanished. The
        # decision is "mark, do not remove", and a marked row that dropped out
        # of every list on the page was removed as far as anybody reading the
        # page can tell.
        html = self._get(gone=lambda: [
            {"fingerprint": "fp-1", "state": "gone",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        self.assertIn("Gone (1)", html)

    def test_the_gone_section_offers_no_control_at_all(self):
        # HARM: every control the other sections draw -- Approve, Undo,
        # Undismiss, Unmute -- would write to an id the server does not have.
        # A button that cannot work is worse than no button.
        html = self._get(gone=lambda: [
            {"fingerprint": "fp-1", "state": "gone",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        section = html.split("Gone (1)", 1)[1].split("</details>", 1)[0]
        self.assertIn("clip.mp4", section)
        self.assertNotIn("<form", section)

    def test_omitted_sections_default_to_empty_rather_than_erroring(self):
        # Every existing action-path test's double for `rows`/`actions`
        # supplies none of these -- the page must still render, with every
        # count at zero, not raise.
        html = self._get()
        self.assertIn("Muted (0)", html)
        self.assertIn("Dismissed (0)", html)
        self.assertIn("Refused (0)", html)
        self.assertIn("Superseded (0)", html)
        self.assertIn("Applied (0)", html)
        self.assertIn("Gone (0)", html)

    def test_the_applied_query_param_opens_that_rows_section(self):
        # The wiring half of ticket 98's "keep undo reachable" story: a GET
        # carrying `?applied=fp-1` -- exactly the shape `/approve`'s own
        # redirect now produces -- must reach the template as
        # `just_applied`, not be dropped on the floor between the query
        # string and `render()`.
        html = self._get(path="/inbox?applied=fp-1", applied=lambda: [
            {"fingerprint": "fp-1", "state": "applied",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        self.assertIn('<details class="section" open', html)

    def test_the_schedule_status_callable_reaches_the_page(self):
        # The seam between a running `Scheduler` and the panel that reports
        # it. A `build_handler` that accepted the callable and never called
        # it would render the "nothing is scheduled" branch on an install
        # that HAS a schedule -- indistinguishable, to someone reading the
        # page, from one where nothing was ever wired up.
        html = self._get(schedule_status=lambda: LoopStatus(
            running=True, closed=False, ticks=4, failures=0,
            consecutive_failures=0, last_tick_at="2026-07-27T03:00:00+00:00",
            last_error=None, last_error_at=None, last_traceback=None,
            failing_to_start={},
            last_result=TickResult(at="2026-07-27T03:00:00+00:00",
                                   due=["nightly-library-scan"], started={},
                                   skipped={"nightly-library-scan":
                                            "disabled by override"},
                                   failed_to_start={})))
        self.assertIn("did not run: disabled by override", html)
        self.assertNotIn("Nothing is scheduled", html)

    def test_omitting_it_says_nothing_is_scheduled(self):
        # The other side, and the default every existing double here relies
        # on: no schedule wired up must read as "no schedule", never as a
        # healthy loop that has simply not been due yet.
        self.assertIn("Nothing is scheduled", self._get())

    def test_no_applied_query_param_leaves_every_section_collapsed(self):
        html = self._get(applied=lambda: [
            {"fingerprint": "fp-1", "state": "applied",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        self.assertNotIn('<details class="section" open', html)


class ScanControl(unittest.TestCase):
    # `/scan` is shaped differently from the other four actions: it carries
    # a `limit`, not a fingerprint, and a "busy" refusal from the runner
    # must reach the person as clearly as an unknown fingerprint does for
    # the other four (see `ExceptionBranch`).

    def test_a_scan_with_a_limit_starts_a_job_and_redirects(self):
        with _Server() as s:
            r = s.request("POST", "/scan", "limit=25")
            # An int, not the string the form actually posts: a mutation
            # that forwarded the raw form string straight through instead
            # of parsing it would still satisfy an assertion written
            # against "25", so this is asserted as the type the rest of
            # the wiring (`build_producer`, then `scan.select`) requires.
            self.assertEqual(s.actions.calls, [("scan", 25)])
            self.assertEqual(r.status, 303)

    def test_a_scan_with_no_limit_is_refused_not_defaulted_to_unlimited(self):
        # HARM: `scan.select` reads a missing limit as "take every
        # survivor" -- the harm this whole control exists to bound.
        with _Server() as s:
            r = s.request("POST", "/scan", "")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_malformed_limit_is_refused_not_a_crash(self):
        with _Server() as s:
            r = s.request("POST", "/scan", "limit=not-a-number")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_busy_runner_refusal_is_reported_not_redirected(self):
        # The runner already raises `JobRejected` for a second concurrent
        # scan of the same cost class; the handler must let that reach the
        # person as an error, not swallow it and redirect as though this
        # scan, too, had started -- indistinguishable, to someone watching
        # the page, from a control that silently does nothing.
        with _Server(fail={"scan": JobRejected(
                "cost class 'scraping' is already running library-scan-x")
                }) as s:
            r = s.request("POST", "/scan", "limit=25")
            self.assertEqual(r.status, 400)
            self.assertIn(b"already running", r.read())
            self.assertEqual(s.actions.calls, [])


class CrossOriginWrites(unittest.TestCase):
    # A POST to a write path is only safe from a page this server served.
    # The loopback binding stops other HOSTS reaching this port; it does
    # nothing about the user's own browser, which can reach 127.0.0.1 from
    # any tab regardless of which page is open there.

    def test_a_cross_site_post_is_refused_and_does_not_fire(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403)
            # The status alone is not enough: a refusal that still
            # performed the write would satisfy a status-only assertion.
            self.assertEqual(s.actions.calls, [])

    def test_a_same_site_post_is_also_refused(self):
        # `same-site` still means the request was caused by a DIFFERENT
        # page (a sibling subdomain, say), just not a fully unrelated host.
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Sec-Fetch-Site": "same-site"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])

    def test_a_same_origin_post_still_works(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(r.status, 303)
            self.assertEqual(s.actions.calls, [("approve", "fp-1")])

    def test_sec_fetch_site_none_still_works(self):
        # The address bar, a bookmark, or a client that sets the header
        # itself without being a cross-site navigation.
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Sec-Fetch-Site": "none"})
            self.assertEqual(r.status, 303)
            self.assertEqual(s.actions.calls, [("approve", "fp-1")])

    def test_a_mismatched_origin_is_refused_and_does_not_fire(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Origin": "https://unrelated.example"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])

    def test_a_matching_origin_still_works(self):
        with _Server() as s:
            host, port = s.httpd.server_address
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Origin": "http://%s:%d" % (host, port)})
            self.assertEqual(r.status, 303)
            self.assertEqual(s.actions.calls, [("approve", "fp-1")])

    def test_a_request_with_neither_header_still_works(self):
        # A non-browser client (curl, a script) sends neither header. See
        # the comment on `_cross_origin_write` for the reasoning behind
        # accepting this case, and what it does not stop.
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1")
            self.assertEqual(r.status, 303)
            self.assertEqual(s.actions.calls, [("approve", "fp-1")])

    def test_a_cross_site_refresh_is_refused_and_does_not_fire(self):
        # Refresh writes to the store (see `Store.supersede`) exactly like
        # the other fingerprint-keyed actions above, and must refuse a
        # cross-origin request on the same terms.
        with _Server() as s:
            r = s.request("POST", "/refresh", "fp=fp-1",
                          headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])


class ExceptionBranch(unittest.TestCase):
    # `Actions.dismiss`/`mute`/`undo` raise `UnknownProposal` for a
    # fingerprint that is no longer present -- a double-submitted form, or a
    # row already dismissed from another tab. Ordinary, not an edge case.
    # `Actions.approve` raises `ApplyFailed` when the write to the media
    # server itself failed. Both must reach the person as an error, never
    # as the same 303 a success gets.

    def test_an_unknown_fingerprint_is_reported_as_an_error_not_a_redirect(self):
        with _Server(fail={"dismiss": UnknownProposal("fp-1")}) as s:
            r = s.request("POST", "/dismiss", "fp=fp-1")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_failed_approve_is_reported_as_an_error_not_a_redirect(self):
        with _Server(fail={"approve":
                           ApplyFailed("could not apply: a fixture failure")}
                     ) as s:
            r = s.request("POST", "/approve", "fp=fp-1")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_an_unknown_fingerprint_refresh_is_reported_as_an_error_not_a_redirect(self):
        with _Server(fail={"refresh": UnknownProposal("fp-1")}) as s:
            r = s.request("POST", "/refresh", "fp=fp-1")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])


class MalformedContentLength(unittest.TestCase):
    def test_a_malformed_content_length_is_rejected_not_a_crash(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Content-Length": "not-a-number"})
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_negative_content_length_is_refused_not_read(self):
        # `rfile.read(-1)` means "read until the connection closes" -- a
        # client that lies with a negative Content-Length and never closes
        # would otherwise wedge this single-threaded handler indefinitely.
        # If this assertion regresses to a hang, that IS the bug it pins.
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Content-Length": "-1"})
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_an_absurdly_large_content_length_is_refused_not_read(self):
        # A numeric Content-Length far beyond anything this form ever sends
        # still parses with plain `int()` -- it takes an explicit bound to
        # catch it before `rfile.read` blocks waiting for bytes the client
        # never sends.
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-1",
                          headers={"Content-Length": "999999999"})
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])


class Binding(unittest.TestCase):
    def test_the_default_host_is_loopback(self):
        # There is no authentication. A listener on all interfaces would be a
        # stranger able to rewrite a library.
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")


class _NoopServer:
    """Stands in for `HTTPServer` so `serve()` can be exercised without
    binding a socket or looping forever."""

    def __init__(self, address, handler):
        self.address = address

    def serve_forever(self):
        return


class ServeWarnsOnNonDefaultHost(unittest.TestCase):
    def test_binding_to_a_non_default_host_prints_a_warning(self):
        out = io.StringIO()
        with patch("cronicled.web.app.HTTPServer", _NoopServer):
            with redirect_stdout(out):
                serve(rows=lambda: [], actions=_RecordingActions(),
                      host="0.0.0.0", port=0)
        self.assertIn("WARNING", out.getvalue())

    def test_binding_to_the_default_host_prints_no_warning(self):
        out = io.StringIO()
        with patch("cronicled.web.app.HTTPServer", _NoopServer):
            with redirect_stdout(out):
                serve(rows=lambda: [], actions=_RecordingActions(),
                      host=DEFAULT_HOST, port=0)
        self.assertNotIn("WARNING", out.getvalue())


def _drive(method, path, body=b"", headers=None, **callables):
    """One request through the real handler with no socket underneath it.

    `do_GET`/`do_POST` read `self.path`, `self.headers` and `self.rfile` and
    answer through `self._send`; all four are supplied here, so the routing and
    redirect decisions are exercised exactly as they are in production while
    the transport is injected. Same shape as the driver in
    `tests/test_tag_hygiene.py`.
    """
    actions = callables.pop("actions", None) or _RecordingActions()
    handler = build_handler(rows=lambda: [], actions=actions, **callables)
    instance = object.__new__(handler)
    instance.path = path
    instance.headers = email.message.Message()
    for key, value in (headers or {}).items():
        instance.headers[key] = value
    if body:
        instance.headers["Content-Length"] = str(len(body))
    instance.rfile = io.BytesIO(body)
    sent = {}
    instance._send = lambda status, body=b"", headers=(): sent.update(
        status=status, body=body, headers=dict(headers))
    getattr(instance, "do_" + method)()
    sent["actions"] = actions
    return sent


class TheTwoPages(unittest.TestCase):
    """`/` is the summary and `/inbox` is the inbox.

    The inbox used to be `/`. Moving it is the whole of this change from a
    reader's point of view, and both halves have to be pinned: a router that
    served the inbox at both would look correct on the landing page and be
    missing the summary entirely.
    """

    def test_the_landing_page_is_the_summary(self):
        sent = _drive("GET", "/")
        self.assertEqual(sent["status"], 200)
        body = sent["body"].decode()
        self.assertIn("<h1>Summary</h1>", body)
        self.assertNotIn("<h1>Inbox</h1>", body)

    def test_the_inbox_is_one_click_away(self):
        sent = _drive("GET", "/inbox")
        self.assertEqual(sent["status"], 200)
        body = sent["body"].decode()
        self.assertIn("<h1>Inbox</h1>", body)
        self.assertNotIn("<h1>Summary</h1>", body)

    def test_a_path_that_is_neither_is_still_a_404(self):
        # A misspelt address must not fall through to whichever page the
        # router happens to reach last.
        sent = _drive("GET", "/inbx")
        self.assertEqual(sent["status"], 404)

    def test_the_summary_callable_is_called_rather_than_merely_accepted(self):
        # A handler that took the callable and never invoked it would draw the
        # empty page on an install with a full run log -- indistinguishable,
        # to a reader, from one where nothing has ever run.
        view = to_summary_view(
            [{"id": "r-1", "job": "scene-scan", "trigger": "scheduled",
              "started": "2026-07-15T00:30:00+00:00",
              "finished": "2026-07-15T00:34:00+00:00", "outcome": "completed",
              "counts": {"recorded": 4}, "error": None}],
            {"scenes": 12}, None, zone=ZoneInfo("Europe/Madrid"))
        body = _drive("GET", "/", summary=lambda: view)["body"].decode()
        self.assertIn("scene-scan", body)
        self.assertIn("2026-07-15 02:30", body)
        self.assertIn("12", body)

    def test_a_handler_with_no_summary_wired_still_draws_the_page(self):
        # Every existing test's double supplies none of this. The default has
        # to be a real empty view, not a missing one -- a page that raises is
        # a page with no way back to the inbox either.
        body = _drive("GET", "/")["body"].decode()
        self.assertIn("No pass has run yet.", body)
        self.assertIn("Nothing waiting.", body)


class WhereAWriteReturnsTo(unittest.TestCase):
    """A redirect answers the question "what was this person doing".

    Judging a proposal is done while looking at the other proposals, so those
    writes return to the inbox. Starting a scan is done from the summary, and
    it returns there -- to the page that reports what the scan then does.
    """

    def test_a_judgement_returns_to_the_inbox(self):
        for name in ("dismiss", "mute", "undo", "undismiss", "refresh"):
            with self.subTest(name):
                sent = _drive("POST", "/" + name, b"fp=fp-7")
                self.assertEqual(sent["status"], 303)
                self.assertEqual(sent["headers"]["Location"], "/inbox")

    def test_an_approve_returns_to_the_inbox_carrying_its_fingerprint(self):
        sent = _drive("POST", "/approve", b"fp=fp-7")
        self.assertEqual(sent["headers"]["Location"], "/inbox?applied=fp-7")

    def test_an_unmute_returns_to_the_inbox_too(self):
        sent = _drive("POST", "/unmute", b"subject_type=scene&subject_id=42")
        self.assertEqual(sent["headers"]["Location"], "/inbox")

    def test_a_scan_returns_to_the_page_its_button_is_on(self):
        sent = _drive("POST", "/scan", b"limit=25")
        self.assertEqual(sent["status"], 303)
        self.assertEqual(sent["headers"]["Location"], "/")
        self.assertEqual(sent["actions"].calls, [("scan", 25)])


class TheScanControlOnItsNewPageStillRefusesCrossOrigin(unittest.TestCase):
    """The control moved pages; the refusal moves with it.

    `/scan` starts a job that spends a rate-limited third party's budget. Any
    tab in the operator's own browser can reach 127.0.0.1, so a write whose own
    headers say it came from some other page is refused -- and a control that
    lost that on its way to a new page is a live vulnerability, not a missing
    feature.
    """

    def test_a_cross_site_scan_is_refused_and_does_not_fire(self):
        sent = _drive("POST", "/scan", b"limit=25",
                      headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(sent["status"], 403)
        self.assertEqual(sent["actions"].calls, [])

    def test_a_same_site_scan_is_refused_too(self):
        # `same-site` is a DIFFERENT origin that happens to share a registrable
        # domain, which is not the same page.
        sent = _drive("POST", "/scan", b"limit=25",
                      headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual(sent["status"], 403)
        self.assertEqual(sent["actions"].calls, [])

    def test_a_scan_from_a_foreign_origin_is_refused_and_does_not_fire(self):
        sent = _drive("POST", "/scan", b"limit=25",
                      headers={"Origin": "http://elsewhere.invalid",
                               "Host": "127.0.0.1:8571"})
        self.assertEqual(sent["status"], 403)
        self.assertEqual(sent["actions"].calls, [])

    def test_a_scan_from_the_page_this_server_served_still_works(self):
        # The permissive side. A refusal that fired on the legitimate request
        # would take the control out of service while looking like security.
        sent = _drive("POST", "/scan", b"limit=25",
                      headers={"Sec-Fetch-Site": "same-origin",
                               "Origin": "http://127.0.0.1:8571",
                               "Host": "127.0.0.1:8571"})
        self.assertEqual(sent["status"], 303)
        self.assertEqual(sent["actions"].calls, [("scan", 25)])


class _FakeStore:
    """A minimal stand-in for `Store.items`, filtering an in-memory list the
    same way the real one filters SQL rows: `subject_types` is checked with
    `is not None`, never truthiness (an empty tuple is a real "select
    nothing", not "no filter given"), and a `state=None` call excludes the
    same four states `Store._HIDDEN_STATES` names -- everything else asks
    for exactly one state.
    """

    _HIDDEN_STATES = ("dismissed", "muted", "superseded", "gone")

    def __init__(self, items):
        self._items = items

    def items(self, folder=None, state=None, limit=None, offset=0,
             subject_types=None):
        result = self._items
        if subject_types is not None:
            result = [i for i in result if i["subject_type"] in subject_types]
        if state is not None:
            result = [i for i in result if i["state"] == state]
        else:
            result = [i for i in result
                     if i["state"] not in self._HIDDEN_STATES]
        return result


# Three invented fixtures, one per inbox, each carrying its own fingerprint
# inside a field its own row builder actually renders -- a scene's filename,
# a tag description's name, a performer description's name -- so a test can
# tell whether ITS row reached the page without depending on the fingerprint
# appearing anywhere in the markup (it does not, in the Muted section: see
# `web/templates/inbox.html`'s Unmute form, which posts `subject_type`/
# `subject_id` and never `fp`).

def _scene_item(fp, state="new"):
    return {
        "fingerprint": fp, "state": state, "subject_type": "scene",
        "subject_id": "scene-%s" % fp,
        "payload": {
            "path": "/invented/library/%s.mp4" % fp,
            "identified_by": "invented-box", "box": "invented-box",
            "candidate": {"title": "An Invented Title", "image": None,
                          "performers": [], "studio": None},
        },
    }


def _tag_item(fp, state="new"):
    return {
        "fingerprint": fp, "state": state, "subject_type": "tag",
        "subject_id": "tag-%s" % fp,
        "payload": {
            "name": "%s-invented-tag" % fp, "source_box": "invented-box",
            "original": "", "description": "an invented description",
        },
    }


def _performer_item(fp, state="new"):
    return {
        "fingerprint": fp, "state": state, "subject_type": "performer",
        "subject_id": "performer-%s" % fp,
        "payload": {
            "name": "%s Invented Performer" % fp, "faults": ("markup",),
            "original": "<b>before</b>", "cleaned": "before",
        },
    }


class EachInboxRouteServesOnlyItsOwnSubjectTypes(unittest.TestCase):
    """`/scenes`, `/tags` and `/performers` -- narrowed by `inboxes.INBOXES`
    through `Store.items(subject_types=)`, reusing `to_rows` unchanged."""

    def test_each_inbox_route_serves_only_its_own_subject_types(self):
        store = _FakeStore([_scene_item("s1"), _tag_item("t1")])
        scenes = _drive("GET", "/scenes", store=store)
        tags = _drive("GET", "/tags", store=store)
        self.assertEqual(scenes["status"], 200)
        self.assertIn(b"s1", scenes["body"])
        self.assertNotIn(b"t1", scenes["body"])
        self.assertEqual(tags["status"], 200)
        self.assertIn(b"t1", tags["body"])
        self.assertNotIn(b"s1", tags["body"])

    def test_the_performers_inbox_is_served_too(self):
        store = _FakeStore([_performer_item("p1"), _scene_item("s1")])
        sent = _drive("GET", "/performers", store=store)
        self.assertEqual(sent["status"], 200)
        self.assertIn(b"p1", sent["body"])
        self.assertNotIn(b"s1", sent["body"])

    def test_each_inbox_shows_its_own_title(self):
        store = _FakeStore([])
        for name, title in (("scenes", "Scenes"), ("tags", "Tags"),
                            ("performers", "Performers")):
            with self.subTest(name):
                sent = _drive("GET", "/" + name, store=store)
                self.assertIn(("<h1>%s</h1>" % title).encode(), sent["body"])

    def test_a_tag_cluster_candidate_does_not_reach_the_tags_page_yet(self):
        # `to_rows` cannot build a row for a merge candidate (it dispatches
        # only on the description/tag-description subject types and treats
        # everything else as scene-shaped -- see `web.app._NO_ROW_BUILDER`),
        # so it is excluded from what `/tags` asks the store for at all,
        # rather than reaching `to_rows` and raising `KeyError` on
        # `payload["path"]`.
        store = _FakeStore([_tag_item("t1"),
                            {"fingerprint": "cluster-1", "state": "new",
                             "subject_type": "tag-cluster",
                             "subject_id": "1", "payload": {}}])
        sent = _drive("GET", "/tags", store=store)
        self.assertEqual(sent["status"], 200)
        self.assertIn(b"t1", sent["body"])
        self.assertNotIn(b"cluster-1", sent["body"])


class UnknownInboxesAndStatesAre404(unittest.TestCase):
    def test_an_unknown_inbox_is_404_not_an_empty_page(self):
        # A typo that rendered an empty page would read as "nothing to
        # review", the same silent-wrong-answer failure this project refuses
        # elsewhere.
        sent = _drive("GET", "/scene", store=_FakeStore([]))  # singular typo
        self.assertEqual(sent["status"], 404)

    def test_an_unknown_state_is_404_not_an_empty_page(self):
        sent = _drive("GET", "/scenes/pending", store=_FakeStore([]))
        self.assertEqual(sent["status"], 404)

    def test_refused_is_404_not_an_empty_page(self):
        # `refused` is not a state an `item` row ever carries (see
        # `Store._HIDDEN_STATES` and `items()`'s own docstring) -- it lives in
        # a wholly separate `refusal` table, keyed by subject, and
        # `Store.refusals()` takes no `subject_types` argument to narrow it
        # by. There is no honest way to serve it through
        # `Store.items(subject_types=)`, the one interface these routes
        # consume, so it 404s rather than rendering an empty page that would
        # read as "nothing refused" for an inbox that may have plenty.
        sent = _drive("GET", "/scenes/refused", store=_FakeStore([]))
        self.assertEqual(sent["status"], 404)

    def test_a_third_path_segment_is_404(self):
        sent = _drive("GET", "/scenes/applied/extra", store=_FakeStore([]))
        self.assertEqual(sent["status"], 404)

    def test_with_no_store_wired_a_per_inbox_route_is_404_not_a_crash(self):
        # Every existing test's double supplies no store at all -- a
        # per-inbox route with nothing to ask 404s rather than raising
        # `AttributeError` on `None.items`.
        sent = _drive("GET", "/scenes")
        self.assertEqual(sent["status"], 404)


class TheNewRoutesDoNotAcceptPosts(unittest.TestCase):
    def test_the_new_routes_do_not_accept_posts(self):
        sent = _drive("POST", "/scenes", b"", store=_FakeStore([]))
        self.assertEqual(sent["status"], 404)

    def test_a_terminal_state_route_does_not_accept_posts_either(self):
        sent = _drive("POST", "/scenes/applied", b"", store=_FakeStore([]))
        self.assertEqual(sent["status"], 404)


class TerminalStatePagesShowOnlyTheirOwnState(unittest.TestCase):
    """`/{inbox}/{state}` for `applied`, `dismissed` and `muted`."""

    def test_an_applied_row_does_not_reach_the_working_queue_page(self):
        # `items()`'s own default excludes dismissed/muted/superseded/gone
        # but NOT applied (see `Store._HIDDEN_STATES`) -- an applied
        # proposal is a decision already made, and the working queue must
        # still filter it out itself, the same way
        # `cronicled.__main__._inbox_rows` does for the combined inbox.
        store = _FakeStore([_scene_item("open-1", state="new"),
                            _scene_item("closed-1", state="applied")])
        sent = _drive("GET", "/scenes", store=store)
        self.assertIn(b"open-1", sent["body"])
        self.assertNotIn(b"closed-1", sent["body"])

    def test_the_applied_state_page_shows_only_applied_rows(self):
        store = _FakeStore([_scene_item("open-1", state="new"),
                            _scene_item("closed-1", state="applied")])
        sent = _drive("GET", "/scenes/applied", store=store)
        self.assertIn(b"closed-1", sent["body"])
        self.assertNotIn(b"open-1", sent["body"])

    def test_the_dismissed_state_page_shows_only_dismissed_rows(self):
        store = _FakeStore([_scene_item("open-1", state="new"),
                            _scene_item("hidden-1", state="dismissed")])
        sent = _drive("GET", "/scenes/dismissed", store=store)
        self.assertIn(b"hidden-1", sent["body"])
        self.assertNotIn(b"open-1", sent["body"])

    def test_the_muted_state_page_shows_only_muted_rows_and_offers_unmute(self):
        store = _FakeStore([_scene_item("open-1", state="new"),
                            _scene_item("hidden-1", state="muted")])
        sent = _drive("GET", "/scenes/muted", store=store)
        body = sent["body"].decode()
        self.assertIn("hidden-1", body)
        self.assertNotIn("open-1", body)
        self.assertIn('action="/unmute"', body)
        # Not just THAT an Unmute control is offered -- it has to post the
        # RIGHT subject. `Row` carries no `subject_type`/`subject_id` of its
        # own (see `_inbox_page`'s own comment on the `muted` branch), so
        # this is the one guard against those two being read from the wrong
        # place, or not at all.
        self.assertIn('name="subject_type" value="scene"', body)
        self.assertIn('name="subject_id" value="scene-hidden-1"', body)


if __name__ == "__main__":
    unittest.main()
