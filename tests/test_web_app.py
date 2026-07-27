import http.client
import io
import threading
import unittest
from contextlib import redirect_stdout
from http.server import HTTPServer
from unittest.mock import patch

from cronicled.jobs import JobRejected
from cronicled.web.actions import ApplyFailed, UnknownProposal
from cronicled.web.app import build_handler, serve, DEFAULT_HOST


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

    def scan(self, limit):
        return self._do("scan", limit, "started")


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


class Posts(unittest.TestCase):
    def test_each_action_path_reaches_its_action(self):
        for path, name in (("/approve", "approve"), ("/dismiss", "dismiss"),
                           ("/mute", "mute"), ("/undo", "undo")):
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


if __name__ == "__main__":
    unittest.main()
