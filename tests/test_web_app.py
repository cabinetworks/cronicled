import email.message
import http.client
import io
import re
import threading
import unittest
from contextlib import redirect_stdout
from datetime import time, timezone
from http.server import HTTPServer
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cronicled.__main__ import waiting_counts
from cronicled.jobs import JobRejected
from cronicled.performer_tags import index_performers, match_tag
from cronicled.performer_tags import proposal as reconcile_proposal
from cronicled.schedule import LoopStatus, TickResult, resolve
from cronicled.store import Store
from cronicled.tag_hygiene import proposal as unused_proposal
from cronicled.tags import cluster_tags
from cronicled.tags import proposal as tag_merge_proposal
from cronicled.web.actions import (ApplyFailed, BatchResult, BulkApplyResult,
                                   UnknownProposal)
from cronicled.web.app import (PAGE_SIZE, _current_pages, _pager,
                                build_handler, serve, DEFAULT_HOST)
from cronicled.web.rows import (to_merge_rows, to_reconcile_rows,
                                to_rows, to_schedule_view, to_summary_view,
                                to_unused_groups)


class _ScheduledProducer:
    """A producer as `resolve` reads one, so a `LoopStatus` built here
    carries a schedule the loop could really be holding."""

    def __init__(self, name, at=None, zone=None):
        self.name = name
        self.every = None
        self.at = at
        self.zone = zone


class _RecordingActions:
    """Records each call before answering, but can be told to raise for a
    given action instead of recording it -- mirroring the two ways the real
    Actions is known to fail a caller rather than answer: `UnknownProposal`
    for a fingerprint it no longer has, and `ApplyFailed` for an apply the
    media server refused. A double that never raises is more forgiving than
    the thing it stands in for, and that gap has cost this project a shipped
    bug before."""

    def __init__(self, fail=None, bulk_result=None, batch_result=None):
        self.calls = []
        self._fail = fail or {}
        # `None` means "report every submitted fingerprint applied" -- the
        # ordinary case a test not exercising partial failure wants. A test
        # that DOES want to render a partial outcome passes its own
        # `BulkApplyResult` instead, exactly as `_fail` lets a test choose
        # which single-row action raises.
        self._bulk_result = bulk_result
        # Same idea, for `batch_apply` -- a test wanting a partial outcome
        # passes its own `BatchResult`.
        self._batch_result = batch_result

    def _do(self, name, fp, ok):
        if name in self._fail:
            raise self._fail[name]
        self.calls.append((name, fp))
        return ok

    def bulk_apply_tag_descriptions(self, fingerprints):
        if "bulk_apply_tag_descriptions" in self._fail:
            raise self._fail["bulk_apply_tag_descriptions"]
        fps = tuple(fingerprints)
        self.calls.append(("bulk_apply_tag_descriptions", fps))
        if self._bulk_result is not None:
            return self._bulk_result
        return BulkApplyResult(requested=fps, applied=fps, failed=())

    def batch_apply(self, verdict, fingerprints):
        if "batch_apply" in self._fail:
            raise self._fail["batch_apply"]
        fps = tuple(fingerprints)
        self.calls.append(("batch_apply", verdict, fps))
        if self._batch_result is not None:
            return self._batch_result
        return BatchResult(verdict=verdict, requested=fps, applied=fps,
                           failed=())

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
    def __init__(self, fail=None, bulk_result=None, batch_result=None):
        self._fail = fail
        self._bulk_result = bulk_result
        self._batch_result = batch_result

    def __enter__(self):
        self.actions = _RecordingActions(fail=self._fail,
                                         bulk_result=self._bulk_result,
                                         batch_result=self._batch_result)
        handler = build_handler(rows=lambda **_: [], actions=self.actions)
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
    redirect is what lets the very next GET show a one-row confirmation,
    above the fold, for exactly that row -- without opening the Applied
    section itself, which is the operator's own to open or leave closed.
    See inbox.html's `just_applied_rows` guard and its confirmation banner.
    """

    def test_a_successful_approve_redirects_with_its_own_fingerprint(self):
        with _Server() as s:
            r = s.request("POST", "/approve", "fp=fp-7")
            self.assertEqual(r.status, 303)
            self.assertEqual(r.getheader("Location"), "/inbox?applied=fp-7")

    def test_other_actions_redirect_to_the_plain_index(self):
        # Only approve moves a row into Applied -- every other action must
        # not carry this query param at all. `undismiss` is excluded here --
        # see `ReversalRedirectReopensItsSection` -- it carries `opened=`
        # instead, for the same reopening reason `approve` carries `applied=`.
        for path, name in (("/dismiss", "dismiss"), ("/mute", "mute"),
                           ("/undo", "undo"), ("/refresh", "refresh")):
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


class BulkApplyTagDescriptionsPost(unittest.TestCase):
    # Shaped differently from every other action for the same reason
    # `/unmute` is: it carries a SET, not a single fingerprint, so the
    # request posts one repeated `fp` field per row rather than one.

    def test_the_whole_submitted_set_reaches_the_action_in_order(self):
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions",
                          "fp=fp-a&fp=fp-b&fp=fp-c")
            self.assertEqual(
                s.actions.calls,
                [("bulk_apply_tag_descriptions", ("fp-a", "fp-b", "fp-c"))])
            self.assertEqual(r.status, 303)

    def test_a_post_without_any_fingerprint_is_rejected(self):
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions", "")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_get_does_not_perform_it(self):
        # Ticket bulk179's own acceptance: browsers prefetch, so this GET
        # rule (see `GetNeverWrites` above) must cover the new action too.
        with _Server() as s:
            r = s.request("GET", "/bulk_apply_tag_descriptions")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])

    def test_a_cross_site_post_is_refused_and_does_not_fire(self):
        # Every POST keeps the existing cross-origin refusal -- a new
        # action that forgot it would be a live vulnerability, not a
        # missing feature.
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions", "fp=fp-a",
                          headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])

    def test_a_same_origin_post_still_works(self):
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions", "fp=fp-a",
                          headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(r.status, 303)
            self.assertEqual(
                s.actions.calls,
                [("bulk_apply_tag_descriptions", ("fp-a",))])

    def test_the_redirect_carries_both_counts_on_a_complete_batch(self):
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions",
                          "fp=fp-a&fp=fp-b")
            self.assertEqual(r.getheader("Location"),
                            "/inbox?bulk_requested=2&bulk_applied=2")

    def test_the_redirect_states_a_partial_outcome_as_partial(self):
        # HARM: collapsing "2 requested, 1 applied" into the same redirect a
        # complete batch gets is exactly how a partial failure would read
        # as a success.
        partial = BulkApplyResult(requested=("fp-a", "fp-b"),
                                  applied=("fp-a",),
                                  failed=({"fingerprint": "fp-b",
                                          "reason": "a fixture failure"},))
        with _Server(bulk_result=partial) as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions",
                          "fp=fp-a&fp=fp-b")
            self.assertEqual(r.getheader("Location"),
                            "/inbox?bulk_requested=2&bulk_applied=1")

    def test_a_bulk_apply_failure_is_reported_as_an_error_not_a_redirect(self):
        with _Server(fail={"bulk_apply_tag_descriptions":
                           RuntimeError("the store is unavailable")}) as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions", "fp=fp-a")
            self.assertEqual(r.status, 400)

    def test_a_batch_at_the_measured_population_size_still_works(self):
        # The measured population (ticket bulk179) is 1456 waiting
        # tag-description proposals, each fingerprint a 64-character sha256
        # hex digest -- see `_MAX_BULK_BODY_BYTES`'s own comment in
        # `cronicled.web.app`. The ordinary 4096-byte body cap every other
        # action keeps (see `MalformedContentLength` below) would refuse
        # this outright.
        fps = ["%064d" % i for i in range(1456)]
        body = "&".join("fp=%s" % fp for fp in fps)
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions", body)
            self.assertEqual(r.status, 303)
            self.assertEqual(len(s.actions.calls[0][1]), 1456)

    def test_an_absurdly_large_bulk_body_is_still_refused(self):
        # Bounded, not unlimited: a numeric-but-dishonest Content-Length far
        # beyond even the bulk ceiling is still caught before `rfile.read`
        # blocks waiting for bytes that are never coming.
        with _Server() as s:
            r = s.request("POST", "/bulk_apply_tag_descriptions", "fp=x",
                          headers={"Content-Length": "999999999"})
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_an_ordinary_action_keeps_the_small_body_cap(self):
        # The bulk action's larger ceiling must not have leaked into every
        # other action -- `/approve` still refuses a body only the bulk
        # action is allowed to send.
        fps = ["%064d" % i for i in range(200)]
        body = "fp=" + "&fp=".join(fps)
        with _Server() as s:
            r = s.request("POST", "/approve", body)
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])


_CONFIRM_HIDDEN_FP_RE = re.compile(
    r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">')


class BatchPost(unittest.TestCase):
    """`/batch` -- one verdict, applied to a ticked (or "select all on this
    page") set of fingerprints. `dismiss`/`mute` act immediately, exactly
    like a single row's own click; `approve`/`refresh` stop at a
    confirmation page first (see `batch_confirm.html`) and only act once
    that page's own form re-posts here with `confirmed=1`.
    """

    def test_dismiss_reaches_the_action_with_the_whole_set_in_order(self):
        with _Server() as s:
            r = s.request("POST", "/batch",
                          "verdict=dismiss&fp=fp-a&fp=fp-b&fp=fp-c")
            self.assertEqual(
                s.actions.calls,
                [("batch_apply", "dismiss", ("fp-a", "fp-b", "fp-c"))])
            self.assertEqual(r.status, 303)

    def test_mute_also_acts_immediately_with_no_confirmation_step(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=mute&fp=fp-a")
            self.assertEqual(
                s.actions.calls, [("batch_apply", "mute", ("fp-a",))])
            self.assertEqual(r.status, 303)

    def test_a_post_without_any_fingerprint_is_rejected(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=dismiss")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_post_without_a_verdict_is_rejected(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "fp=fp-a")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_an_unrecognised_verdict_is_rejected_before_it_reaches_the_action(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=delete&fp=fp-a")
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])

    def test_a_get_does_not_perform_it(self):
        with _Server() as s:
            r = s.request("GET", "/batch")
            self.assertEqual(r.status, 405)
            self.assertEqual(s.actions.calls, [])

    def test_a_cross_site_post_is_refused_and_does_not_fire(self):
        # Every POST keeps the existing cross-origin refusal -- a new
        # action that forgot it would be a live vulnerability, not a
        # missing feature.
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=dismiss&fp=fp-a",
                          headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])

    def test_the_redirect_carries_the_verdict_and_both_counts(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=mute&fp=fp-a&fp=fp-b")
            self.assertEqual(
                r.getheader("Location"),
                "/inbox?batch_verdict=mute&batch_requested=2&batch_applied=2")

    def test_a_partial_batch_states_a_partial_outcome_not_a_success(self):
        partial = BatchResult(
            verdict="mute", requested=("fp-a", "fp-b"), applied=("fp-a",),
            failed=({"fingerprint": "fp-b", "reason": "a fixture failure"},))
        with _Server(batch_result=partial) as s:
            r = s.request("POST", "/batch", "verdict=mute&fp=fp-a&fp=fp-b")
            self.assertEqual(
                r.getheader("Location"),
                "/inbox?batch_verdict=mute&batch_requested=2&batch_applied=1")

    def test_a_batch_apply_failure_is_reported_as_an_error_not_a_redirect(self):
        with _Server(fail={"batch_apply":
                           RuntimeError("the store is unavailable")}) as s:
            r = s.request("POST", "/batch", "verdict=mute&fp=fp-a")
            self.assertEqual(r.status, 400)

    def test_a_batch_at_the_page_size_still_works(self):
        # `PAGE_SIZE` (200) fingerprints, each a real 64-character sha256
        # digest -- the largest a selection scoped to one page can ever
        # legitimately carry, well inside the bulk ceiling this action
        # shares with `bulk_apply_tag_descriptions`.
        fps = ["%064d" % i for i in range(PAGE_SIZE)]
        body = "verdict=mute&" + "&".join("fp=%s" % fp for fp in fps)
        with _Server() as s:
            r = s.request("POST", "/batch", body)
            self.assertEqual(r.status, 303)
            self.assertEqual(len(s.actions.calls[0][2]), PAGE_SIZE)

    def test_an_absurdly_large_batch_body_is_still_refused(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=mute&fp=x",
                          headers={"Content-Length": "999999999"})
            self.assertEqual(r.status, 400)
            self.assertEqual(s.actions.calls, [])


class BatchApproveAndRefreshRequireConfirmation(unittest.TestCase):
    """`approve` and `refresh` never fire on the first POST -- the response
    is a 200 confirmation page stating the exact set and count, and only a
    second POST carrying `confirmed=1` (that page's own form) actually
    calls `batch_apply`. `dismiss`/`mute` never reach this page at all --
    see `BatchPost` above, where both act on the first POST."""

    def test_approve_without_confirmation_renders_a_confirmation_page_and_does_not_fire(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=approve&fp=fp-a&fp=fp-b")
            self.assertEqual(r.status, 200)
            self.assertEqual(s.actions.calls, [])
            body = r.read().decode()
            self.assertIn("2", body)
            self.assertEqual(
                _CONFIRM_HIDDEN_FP_RE.findall(body), ["fp-a", "fp-b"])

    def test_refresh_without_confirmation_also_stops_at_the_confirmation_page(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=refresh&fp=fp-a")
            self.assertEqual(r.status, 200)
            self.assertEqual(s.actions.calls, [])

    def test_confirming_approve_reaches_the_action_with_the_same_set(self):
        with _Server() as s:
            r = s.request(
                "POST", "/batch",
                "verdict=approve&fp=fp-a&fp=fp-b&confirmed=1")
            self.assertEqual(r.status, 303)
            self.assertEqual(
                s.actions.calls,
                [("batch_apply", "approve", ("fp-a", "fp-b"))])

    def test_confirming_refresh_reaches_the_action_with_the_same_set(self):
        with _Server() as s:
            r = s.request(
                "POST", "/batch", "verdict=refresh&fp=fp-a&confirmed=1")
            self.assertEqual(r.status, 303)
            self.assertEqual(
                s.actions.calls, [("batch_apply", "refresh", ("fp-a",))])

    def test_a_stray_confirmed_value_on_dismiss_changes_nothing(self):
        # `dismiss`/`mute` already fire on the first POST -- an incidental
        # `confirmed=1` alongside one (a browser resubmitting a form, say)
        # must not change what fires or skip anything.
        with _Server() as s:
            r = s.request(
                "POST", "/batch", "verdict=dismiss&fp=fp-a&confirmed=1")
            self.assertEqual(r.status, 303)
            self.assertEqual(
                s.actions.calls, [("batch_apply", "dismiss", ("fp-a",))])

    def test_the_cross_origin_refusal_still_applies_to_the_confirmation_step(self):
        with _Server() as s:
            r = s.request("POST", "/batch", "verdict=approve&fp=fp-a",
                          headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status, 403)
            self.assertEqual(s.actions.calls, [])


class MutedDismissedRefusedRendering(unittest.TestCase):
    """`build_handler`'s five new callables reach the page a GET renders --
    the plumbing between a store and the template, as opposed to how the
    template itself renders a given row (covered in
    tests/test_web_render.py)."""

    def _get(self, path="/inbox", **callables):
        actions = _RecordingActions()
        handler = build_handler(rows=lambda **_: [], actions=actions, **callables)
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

    def test_the_applied_query_param_reaches_the_page_without_opening_anything(self):
        # The wiring half of "keep undo reachable" (see
        # tests/test_web_render.py's `ApplifiedSectionRendering` for the
        # rendering rules themselves): a GET carrying `?applied=fp-1` --
        # exactly the shape `/approve`'s own redirect now produces -- must
        # reach the template as `just_applied`, not be dropped on the floor
        # between the query string and `render()`. It must show the
        # confirmation, and it must NOT force the Applied section open --
        # that section's open state is the operator's, whether or not a row
        # just landed in it.
        #
        # The highlighted row also renders a second time INSIDE the (closed)
        # Applied section regardless of this query param -- that is
        # unaffected, existing behaviour -- so the confirmation is proved by
        # its position, not merely its presence: it must appear before the
        # Applied `<details>` tag even opens, not only somewhere in the page.
        html = self._get(path="/inbox?applied=fp-1", applied=lambda: [
            {"fingerprint": "fp-1", "state": "applied",
             "filename": "clip.mp4", "proposed_title": "T", "creator": "N",
             "creator_source": "folder", "score_text": "0.900"}])
        self.assertNotIn('<details class="section" open', html)
        applied_tag_at = html.index(
            '<details class="section"',
            max(0, html.index("Applied (") - 200))
        banner_at = html.index('class="proposal just-applied"')
        self.assertLess(banner_at, applied_tag_at,
                        "the confirmation must render above the fold, "
                        "before the Applied section's own tag")

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
            appointments=resolve([_ScheduledProducer(
                "nightly-library-scan", at=time(3, 0),
                zone=ZoneInfo("Europe/Madrid"))]),
            failing_to_start={},
            last_result=TickResult(at="2026-07-27T03:00:00+00:00",
                                   due=["nightly-library-scan"], started={},
                                   skipped={"nightly-library-scan":
                                            "disabled by override"},
                                   failed_to_start={})))
        self.assertIn("did not run: disabled by override", html)

    def test_a_converted_status_puts_the_appointment_on_the_page(self):
        # THE SHAPE PRODUCTION ACTUALLY SERVES. `cronicled.__main__` hands
        # this callable the output of `to_schedule_view`, never a raw
        # `LoopStatus`, and the template reads the appointments off the
        # converted form -- so a seam that stopped converting would draw a
        # panel with no appointment lines and no error, which reads as a
        # deployment where nothing is scheduled overnight.
        status = LoopStatus(
            running=True, closed=False, ticks=4, failures=0,
            consecutive_failures=0, last_tick_at="2026-07-27T03:00:00+00:00",
            last_error=None, last_error_at=None, last_traceback=None,
            appointments=resolve([_ScheduledProducer(
                "nightly-library-scan", at=time(3, 0),
                zone=ZoneInfo("Europe/Madrid"))]),
            failing_to_start={}, last_result=None)
        html = self._get(schedule_status=lambda: to_schedule_view(
            status, zone=ZoneInfo("Europe/Madrid")))
        self.assertIn("nightly-library-scan &mdash; 03:00", html)
        self.assertIn("Stated times are in Europe/Madrid.", html)
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

    def test_the_opened_query_param_opens_the_muted_section(self):
        # The GET half of a reversal's redirect (see
        # `ReversalRedirectReopensItsSection` in this same module for the
        # POST half): `?opened=muted`, exactly what a successful `/unmute`
        # now redirects with, must reach the template and reopen the
        # section it names.
        html = self._get(path="/inbox?opened=muted", muted=lambda: [
            {"subject_type": "scene", "subject_id": "1",
             "reason": "never identifiable", "at": "t"}])
        self.assertIn('<details class="section" open', html)

    def test_an_unrecognised_opened_value_is_dropped_before_it_reaches_the_page(self):
        # `do_GET`'s own whitelist (`_OPENABLE_SECTIONS`) is the first guard;
        # the template's `open=(opened == ...)` comparison would already
        # neutralise this too (see `tests/test_web_render.py`), but this
        # pins the whitelist itself, not only its backstop.
        html = self._get(path="/inbox?opened=applied", muted=lambda: [
            {"subject_type": "scene", "subject_id": "1",
             "reason": "never identifiable", "at": "t"}])
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
                serve(rows=lambda **_: [], actions=_RecordingActions(),
                      host="0.0.0.0", port=0)
        self.assertIn("WARNING", out.getvalue())

    def test_binding_to_the_default_host_prints_no_warning(self):
        out = io.StringIO()
        with patch("cronicled.web.app.HTTPServer", _NoopServer):
            with redirect_stdout(out):
                serve(rows=lambda **_: [], actions=_RecordingActions(),
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
    handler = build_handler(rows=lambda **_: [], actions=actions, **callables)
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
        # `undismiss` is covered separately in `ReversalRedirectReopensItsSection`
        # -- it is a judgement too, but its redirect carries `opened=` so its
        # own collapsed section reopens rather than closing again.
        for name in ("dismiss", "mute", "undo", "refresh"):
            with self.subTest(name):
                sent = _drive("POST", "/" + name, b"fp=fp-7")
                self.assertEqual(sent["status"], 303)
                self.assertEqual(sent["headers"]["Location"], "/inbox")

    def test_an_approve_returns_to_the_inbox_carrying_its_fingerprint(self):
        sent = _drive("POST", "/approve", b"fp=fp-7")
        self.assertEqual(sent["headers"]["Location"], "/inbox?applied=fp-7")

    def test_a_scan_returns_to_the_page_its_button_is_on(self):
        sent = _drive("POST", "/scan", b"limit=25")
        self.assertEqual(sent["status"], 303)
        self.assertEqual(sent["headers"]["Location"], "/")
        self.assertEqual(sent["actions"].calls, [("scan", 25)])


class ReversalRedirectReopensItsSection(unittest.TestCase):
    """A reversal (`/unmute`, `/undismiss`) undoes a verdict recorded against
    a collapsed section, and every write redirects to the plain inbox --
    which renders every `<details>` closed again. Reversing several mutes in
    one sitting used to mean opening "Muted" after every single one, and
    losing the scroll position each time.

    The redirect now names which section it acted in, so `do_GET` can
    reopen exactly that one -- see `cronicled.web.app._REOPEN_SECTION` and
    `inbox.html`'s own `open=(opened == ...)` on the Muted and Dismissed
    sections.
    """

    def test_an_unmute_redirects_naming_the_muted_section(self):
        sent = _drive("POST", "/unmute", b"subject_type=scene&subject_id=42")
        self.assertEqual(sent["status"], 303)
        self.assertEqual(sent["headers"]["Location"], "/inbox?opened=muted")

    def test_an_undismiss_redirects_naming_the_dismissed_section(self):
        sent = _drive("POST", "/undismiss", b"fp=fp-7")
        self.assertEqual(sent["status"], 303)
        self.assertEqual(sent["headers"]["Location"],
                         "/inbox?opened=dismissed")

    def test_a_failed_unmute_does_not_redirect_at_all(self):
        # Nothing to reopen a section over -- see `ExceptionBranch`'s own
        # reasoning for every other action's failure.
        sent = _drive("POST", "/unmute", b"subject_type=scene&subject_id=42",
                      actions=_RecordingActions(
                          fail={"unmute": UnknownProposal(("scene", "42"))}))
        self.assertEqual(sent["status"], 400)

    def test_a_failed_undismiss_does_not_redirect_at_all(self):
        sent = _drive("POST", "/undismiss", b"fp=fp-7",
                      actions=_RecordingActions(
                          fail={"undismiss": UnknownProposal("fp-7")}))
        self.assertEqual(sent["status"], 400)


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
    """A minimal stand-in for `Store.items`/`Store.item_count`, filtering an
    in-memory list the same way the real one filters SQL rows:
    `subject_types` is checked with `is not None`, never truthiness (an
    empty tuple is a real "select nothing", not "no filter given"), and a
    `state=None` call excludes the same four states `Store._HIDDEN_STATES`
    names, widened by `exclude_states` -- everything else asks for exactly
    one state.
    """

    _HIDDEN_STATES = ("dismissed", "muted", "superseded", "gone")

    def __init__(self, items):
        self._items = items

    def _filtered(self, folder, state, subject_types, exclude_states):
        result = self._items
        if subject_types is not None:
            result = [i for i in result if i["subject_type"] in subject_types]
        if state is not None:
            result = [i for i in result if i["state"] == state]
        else:
            hidden = self._HIDDEN_STATES + tuple(exclude_states)
            result = [i for i in result if i["state"] not in hidden]
        return result

    def items(self, folder=None, state=None, limit=None, offset=0,
             subject_types=None, exclude_states=()):
        result = self._filtered(folder, state, subject_types, exclude_states)
        if limit is not None:
            result = result[offset:offset + limit]
        return result

    def item_count(self, folder=None, state=None, subject_types=None,
                  exclude_states=()):
        return len(self._filtered(folder, state, subject_types,
                                  exclude_states))

    def counts(self, folder=None, subject_types=None):
        """Mirrors `Store.counts`: grouped by state, the same
        `_HIDDEN_STATES` excluded regardless of what is asked for -- there is
        no `state=` argument here to ask for one of those explicitly, unlike
        `items()` above, because the real method has none either."""
        result = self._items
        if subject_types is not None:
            result = [i for i in result if i["subject_type"] in subject_types]
        result = [i for i in result if i["state"] not in self._HIDDEN_STATES]
        counts = {}
        for i in result:
            counts[i["state"]] = counts.get(i["state"], 0) + 1
        return counts

    def counts_by_subject_type(self, folder=None):
        """Mirrors `Store.counts_by_subject_type`: the same `_HIDDEN_STATES`
        excluded, `applied` excluded too, grouped by subject type instead of
        by state."""
        hidden = self._HIDDEN_STATES + ("applied",)
        result = [i for i in self._items if i["state"] not in hidden]
        counts = {}
        for i in result:
            counts[i["subject_type"]] = counts.get(i["subject_type"], 0) + 1
        return counts


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

    def test_a_tag_cluster_candidate_never_reaches_the_generic_row_list(self):
        # `to_rows` cannot build a row for a merge candidate (it dispatches
        # only on the description/tag-description subject types and treats
        # everything else as scene-shaped -- see `web.app._SECTION_SUBJECTS`),
        # so it is excluded from what the GENERIC row list asks the store
        # for at all, rather than reaching `to_rows` and raising `KeyError`
        # on `payload["path"]`. It has its own section instead (see
        # `TheTagsPageComposesEverySectionTheCombinedPageDoes` below) -- this
        # test is only about the raw store item never being read through
        # `to_rows`, which is why no `merges=` callable is wired here at all:
        # nothing would surface its fingerprint even if the fixture's own
        # payload were real, because `to_rows` is never handed this item.
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


def _cluster_item(fp, state="new"):
    """A bare tag-cluster (merge) candidate -- one of the three subject types
    `web.app._SECTION_SUBJECTS` names. `to_rows` cannot build a row for it
    (see that mapping's own comment), so a `Store` holding one of these is
    used here only to prove what the GENERIC row list counts and excludes --
    its `payload` is `{}` throughout because nothing here ever hands it to
    `to_merge_row`, which would need a real one. A test that wants an actual
    rendered "Tag merges" row uses `_merge_item` and the real `merges=`
    callable instead (see `TheTagsPageComposesEverySectionTheCombinedPageDoes`
    below).
    """
    return {"fingerprint": fp, "state": state, "subject_type": "tag-cluster",
            "subject_id": "cluster-%s" % fp, "payload": {}}


def _merge_item(fp="fp-merge", state="new"):
    """One real tag-merge proposal, built through `cronicled.tags.proposal`
    on an actual cluster rather than hand-assembled -- a hand-written payload
    is exactly how a row builder comes to be handed a shape the producer
    never emits, and `to_merge_row` indexes almost every field it reads.
    """
    tags = [
        {"id": "1", "name": "Velvet Crane", "aliases": [], "description": None,
         "scene_count": 12},
        {"id": "9", "name": "VelvetCrane", "aliases": [], "description": None,
         "scene_count": 4},
    ]
    built = tag_merge_proposal(cluster_tags(tags)[0], "library", [])
    return {"fingerprint": fp, "state": state,
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "payload": built["payload"]}


def _reconcile_item(fp="fp-reconcile", state="new"):
    """One real tag/performer reconciliation, built the same way -- a tag
    whose name matches one invented performer's, on two invented scenes."""
    tag = {"id": "20", "name": "Marlowe Quill", "aliases": [],
          "description": None, "scene_count": 3}
    performer = {"id": "77", "name": "Marlowe Quill", "alias_list": []}
    matches = match_tag(tag, index_performers([performer]))
    built = reconcile_proposal(tag, matches, ["sc-1", "sc-2"], folder="library")
    return {"fingerprint": fp, "state": state,
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "payload": built["payload"]}


def _unused_item(fp="fp-unused", state="new"):
    """One real low-count-tag proposal, built the same way -- an invented tag
    on no scenes at all."""
    tag = {"id": "55", "name": "Faded Ledger", "scene_count": 0}
    built = unused_proposal(tag, folder="library")
    return {"fingerprint": fp, "state": state,
            "subject_type": built["subject_type"],
            "subject_id": built["subject_id"], "payload": built["payload"]}


class _StoreWithDetachedCounts:
    """A store double whose `.counts()` answers from a number handed to it
    directly, wholly independent of what `.items()`/`.item_count()` would
    return for the same request -- so a sidebar built from `store.counts()`
    and one built from a paginated page's own total give two DIFFERENT,
    distinguishable answers. `.items()` still filters for real, the same way
    `_FakeStore` does, so the page beneath the sidebar renders its own true
    (small) set of rows while the sidebar is handed a larger, invented total.
    """

    _HIDDEN_STATES = ("dismissed", "muted", "superseded", "gone")

    def __init__(self, items, waiting_count):
        self._items = items
        self._waiting_count = waiting_count

    def _filtered(self, folder, state, subject_types, exclude_states):
        result = self._items
        if subject_types is not None:
            result = [i for i in result if i["subject_type"] in subject_types]
        if state is not None:
            result = [i for i in result if i["state"] == state]
        else:
            hidden = self._HIDDEN_STATES + tuple(exclude_states)
            result = [i for i in result if i["state"] not in hidden]
        return result

    def items(self, folder=None, state=None, limit=None, offset=0,
             subject_types=None, exclude_states=()):
        result = self._filtered(folder, state, subject_types, exclude_states)
        if limit is not None:
            result = result[offset:offset + limit]
        return result

    def item_count(self, folder=None, state=None, subject_types=None,
                  exclude_states=()):
        return len(self._filtered(folder, state, subject_types,
                                  exclude_states))

    def counts(self, folder=None, subject_types=None):
        return {"new": self._waiting_count}


class TheSidebarCountsComeFromTheStore(unittest.TestCase):
    """The number beside each inbox's link is `store.counts()`, not a count
    of whatever `to_rows` happened to build for the page underneath it --
    the gap between the two is invisible until a page is paginated, which is
    exactly why it has to be pinned here rather than trusted by inspection.
    """

    def test_the_count_is_read_from_the_store_not_from_rendered_rows(self):
        # One actual row on the page; the store reports three waiting. A
        # sidebar built from `len(rendered rows)` would say 1.
        store = _StoreWithDetachedCounts([_tag_item("t1")], waiting_count=3)
        body = _drive("GET", "/tags", store=store)["body"].decode()
        self.assertIn(">3<", body)
        self.assertNotIn(">1<", body)

    def test_the_same_store_derived_count_appears_on_the_summary_page(self):
        # The sidebar is the same partial everywhere -- the summary page's
        # copy must not silently fall back to a different (rendering-based)
        # source just because there is no page of rows underneath it there.
        store = _StoreWithDetachedCounts([_tag_item("t1")], waiting_count=3)
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertIn(">3<", body)


class TheTagsCountNowSpansEverySubjectTypeTheInboxOwns(unittest.TestCase):
    """The defect this ticket closes: a tag inbox has four subject types, and
    the sidebar number by the Tags link used to add up only the one `to_rows`
    can build a generic row for -- on a real library, 1456 rather than the
    2606 the summary's own Waiting total reported for the same heading.
    `_inbox_page` now composes a section for the other three (see
    `web.app._SECTION_SUBJECTS`), and the count widens with it. The stronger,
    one-render version of this same guarantee is
    `TheSidebarTotalAgreesWithTheSummarysWaitingTotal` below.
    """

    def test_a_tag_cluster_candidate_now_counts_toward_the_tags_total(self):
        store = _FakeStore([_tag_item("t1"), _tag_item("t2"),
                            _cluster_item("c1"), _cluster_item("c2"),
                            _cluster_item("c3")])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertIn(">5<", body)
        self.assertNotIn(">2<", body)

    def test_a_dismissed_or_muted_one_of_the_three_is_still_excluded(self):
        # Widening the TYPES a count spans must not accidentally widen which
        # STATES count as waiting -- `store.counts()` excludes
        # `Store._HIDDEN_STATES` regardless of how many subject types it is
        # asked about.
        store = _FakeStore([_tag_item("t1"),
                            _cluster_item("c1", state="dismissed"),
                            _cluster_item("c2", state="muted")])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertIn(">1<", body)
        self.assertNotIn(">3<", body)


class TheTagsPageComposesEverySectionTheCombinedPageDoes(unittest.TestCase):
    """`/tags` now builds the merge, tag/performer and low-count-tag sections
    the same way the combined `/inbox` page does -- through the SAME
    `merges=`/`reconciles=`/`unused=` closures (see `web.app._inbox_page`),
    never a second copy of `to_merge_rows` et al. Three SEPARATE tests,
    each pinning one kind, because a single test asserting "some section
    appeared" cannot tell which of the three is missing if only one is.
    """

    def test_a_merge_reaches_the_tags_page(self):
        sent = _drive("GET", "/tags", store=_FakeStore([]),
                      merges=lambda: to_merge_rows([_merge_item()]))
        body = sent["body"].decode()
        self.assertIn("Merge into <b>Velvet Crane</b>", body)

    def test_a_tag_performer_match_reaches_the_tags_page(self):
        sent = _drive("GET", "/tags", store=_FakeStore([]),
                      reconciles=lambda: to_reconcile_rows(
                          [_reconcile_item()]))
        body = sent["body"].decode()
        self.assertIn("Marlowe Quill", body)
        self.assertIn("matched on their name", body)

    def test_an_unused_tag_reaches_the_tags_page(self):
        sent = _drive("GET", "/tags", store=_FakeStore([]),
                      unused=lambda: to_unused_groups([_unused_item()]))
        body = sent["body"].decode()
        self.assertIn("Faded Ledger", body)

    def test_scenes_gains_none_of_the_three_even_when_they_are_wired(self):
        # The quieter failure this project has recorded before: widening a
        # selection too far. Wiring all three callables and pointing every
        # assertion at `/scenes` proves the inbox itself decides what is
        # composed, not merely that nobody happened to pass the keyword.
        sent = _drive("GET", "/scenes", store=_FakeStore([]),
                      merges=lambda: to_merge_rows([_merge_item()]),
                      reconciles=lambda: to_reconcile_rows(
                          [_reconcile_item()]),
                      unused=lambda: to_unused_groups([_unused_item()]))
        body = sent["body"].decode()
        self.assertNotIn("Velvet Crane", body)
        self.assertNotIn("Marlowe Quill", body)
        self.assertNotIn("Faded Ledger", body)
        self.assertNotIn("Tag merges (", body)
        self.assertNotIn("Tags that are a performer (", body)
        self.assertNotIn("Tags that do almost no work (", body)

    def test_performers_gains_none_of_the_three_either(self):
        sent = _drive("GET", "/performers", store=_FakeStore([]),
                      merges=lambda: to_merge_rows([_merge_item()]),
                      reconciles=lambda: to_reconcile_rows(
                          [_reconcile_item()]),
                      unused=lambda: to_unused_groups([_unused_item()]))
        body = sent["body"].decode()
        self.assertNotIn("Velvet Crane", body)
        self.assertNotIn("Marlowe Quill", body)
        self.assertNotIn("Faded Ledger", body)

    def test_the_combined_inbox_page_still_renders_exactly_as_before(self):
        # What must not change: the one page that has always shown all six
        # subject types keeps showing them, through the same closures.
        sent = _drive("GET", "/inbox", store=_FakeStore([]),
                      merges=lambda: to_merge_rows([_merge_item()]),
                      reconciles=lambda: to_reconcile_rows(
                          [_reconcile_item()]),
                      unused=lambda: to_unused_groups([_unused_item()]))
        body = sent["body"].decode()
        self.assertIn("Merge into <b>Velvet Crane</b>", body)
        self.assertIn("Marlowe Quill", body)
        self.assertIn("Faded Ledger", body)

    def test_a_dismissed_merge_still_carries_its_undismiss_on_the_tags_page(self):
        # The whole reason `_merge_rows` reads the store three times: a
        # dismissed cluster needs its Undismiss control, and it has to reach
        # this page exactly as it reaches the combined one -- not only the
        # waiting rows, which a narrower test could not tell apart from a
        # section that dropped every closed state.
        sent = _drive("GET", "/tags", store=_FakeStore([]),
                      merges=lambda: to_merge_rows(
                          [_merge_item(state="dismissed")]))
        body = sent["body"].decode()
        # Widened by the return-address ticket: a per-inbox page's own forms
        # now carry where they were shown (see web/app.py's `_return_pages`
        # and inbox.html's `return_fields`), so the control is pinned as
        # this whole, closed shape rather than the narrower one before it.
        self.assertIn('<form method="post" action="/undismiss">'
                      '<input type="hidden" name="fp" value="fp-merge">'
                      '<input type="hidden" name="return_inbox" '
                      'value="tags">'
                      '<input type="hidden" name="return_state" value="">'
                      '<input type="hidden" name="return_page_key" '
                      'value="merges_page">'
                      '<input type="hidden" name="return_page" value="1">'
                      '<button>Undismiss</button></form>', body)

    def test_a_muted_unused_tag_still_carries_its_unmute_on_the_tags_page(self):
        sent = _drive("GET", "/tags", store=_FakeStore([]),
                      unused=lambda: to_unused_groups(
                          [_unused_item(state="muted")]))
        body = sent["body"].decode()
        # Same widening as above, applied to Unmute's own subject pair.
        self.assertIn('<form method="post" action="/unmute">'
                      '<input type="hidden" name="subject_type" '
                      'value="tag-unused">'
                      '<input type="hidden" name="subject_id" value="55">'
                      '<input type="hidden" name="return_inbox" '
                      'value="tags">'
                      '<input type="hidden" name="return_state" value="">'
                      '<input type="hidden" name="return_page_key" '
                      'value="unused_page">'
                      '<input type="hidden" name="return_page" value="1">'
                      '<button>Stop keeping</button></form>', body)


class TheSidebarTotalAgreesWithTheSummarysWaitingTotal(unittest.TestCase):
    """The two-number contradiction this ticket closes: the sidebar's Tags
    count and the summary's own Waiting total for the same heading used to
    read differently on a real library (1456 against 2606) because they were
    built from two different subject-type lists. Both numbers are pulled off
    ONE render of `/` -- asserting each separately against its own fixture
    could not tell "both correct" from "both wrong the same way", which is
    exactly the shape of this bug.
    """

    _SIDEBAR_RE = re.compile(
        r'<a href="/tags">Tags</a>\s*<span class="count">(\d+)</span>')
    _WAITING_RE = re.compile(r'<a href="/inbox">tags</a> &mdash; (\d+)')

    def test_the_tags_sidebar_count_matches_the_waiting_total_for_tags(self):
        items = [
            _tag_item("t1"), _tag_item("t2"),
            _tag_item("t-applied", state="applied"),
            _cluster_item("c1"), _cluster_item("c2"), _cluster_item("c3"),
            _cluster_item("c-dismissed", state="dismissed"),
            {"fingerprint": "r1", "state": "new",
             "subject_type": "tag-performer", "subject_id": "r1",
             "payload": {}},
            {"fingerprint": "r2", "state": "new",
             "subject_type": "tag-performer", "subject_id": "r2",
             "payload": {}},
            {"fingerprint": "r-muted", "state": "muted",
             "subject_type": "tag-performer", "subject_id": "r-muted",
             "payload": {}},
            {"fingerprint": "u1", "state": "new",
             "subject_type": "tag-unused", "subject_id": "u1",
             "payload": {}},
        ]
        store = _FakeStore(items)
        body = _drive(
            "GET", "/", store=store,
            summary=lambda: to_summary_view(
                [], waiting_counts(store), None, zone=timezone.utc),
        )["body"].decode()

        sidebar_match = self._SIDEBAR_RE.search(body)
        waiting_match = self._WAITING_RE.search(body)
        self.assertIsNotNone(sidebar_match, body)
        self.assertIsNotNone(waiting_match, body)
        # Both numbers, from the SAME render: 2 tag + 3 tag-cluster +
        # 2 tag-performer + 1 tag-unused = 8, with the applied tag, the
        # dismissed cluster and the muted reconciliation all excluded from
        # both counts on their own terms.
        self.assertEqual(sidebar_match.group(1), "8")
        self.assertEqual(sidebar_match.group(1), waiting_match.group(1))


class TheSidebarCountExcludesApplied(unittest.TestCase):
    """An applied proposal is a decision already made -- `/tags` itself drops
    it from the working queue (see `_inbox_page`), and the count beside the
    link has to agree with that, not with every non-hidden row regardless of
    state."""

    def test_an_applied_row_is_not_counted_as_waiting(self):
        store = _FakeStore([_tag_item("t1", state="new"),
                            _tag_item("t2", state="applied")])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertIn(">1<", body)
        self.assertNotIn(">2<", body)


class ASidebarNestedStateAppearsOnlyWhenThatInboxHasIt(unittest.TestCase):
    """`Applied`, `Dismissed` and `Muted` are each a real link only for an
    inbox that actually has something in that state -- a fixture where every
    inbox carries the same states could not tell "shown because present"
    from "shown regardless", so each inbox below is given a DIFFERENT one.
    """

    def _store(self):
        return _FakeStore([
            _scene_item("s-open", state="new"),
            _scene_item("s-applied", state="applied"),
            _tag_item("t-open", state="new"),
            _tag_item("t-dismissed", state="dismissed"),
            _performer_item("p-open", state="new"),
            _performer_item("p-muted", state="muted"),
        ])

    def test_the_inbox_with_an_applied_row_links_to_it(self):
        body = _drive("GET", "/", store=self._store())["body"].decode()
        self.assertIn('href="/scenes/applied"', body)

    def test_an_inbox_with_no_applied_row_does_not_link_to_it(self):
        body = _drive("GET", "/", store=self._store())["body"].decode()
        self.assertNotIn('href="/tags/applied"', body)
        self.assertNotIn('href="/performers/applied"', body)

    def test_the_inbox_with_a_dismissed_row_links_to_it(self):
        body = _drive("GET", "/", store=self._store())["body"].decode()
        self.assertIn('href="/tags/dismissed"', body)

    def test_an_inbox_with_no_dismissed_row_does_not_link_to_it(self):
        body = _drive("GET", "/", store=self._store())["body"].decode()
        self.assertNotIn('href="/scenes/dismissed"', body)
        self.assertNotIn('href="/performers/dismissed"', body)

    def test_the_inbox_with_a_muted_row_links_to_it(self):
        body = _drive("GET", "/", store=self._store())["body"].decode()
        self.assertIn('href="/performers/muted"', body)

    def test_an_inbox_with_no_muted_row_does_not_link_to_it(self):
        body = _drive("GET", "/", store=self._store())["body"].decode()
        self.assertNotIn('href="/scenes/muted"', body)
        self.assertNotIn('href="/tags/muted"', body)


class TheNestedStateLinksStayNarrowedEvenAsTheCountWidens(unittest.TestCase):
    """`_sidebar_context` widens the COUNT to every subject type an inbox
    owns, but the nested `Applied`/`Dismissed`/`Muted` links stay narrowed to
    what the GENERIC `/{name}/{state}` route can actually draw -- see that
    function's own docstring for why. A dismissed or applied tag-cluster,
    tag/performer match or low-count tag is real work, but it is shown
    inline in its own section on `/tags` itself, never via the terminal
    route these links point at, so the count and the nested links have to
    stay on two different subject-type lists rather than the same one.
    """

    def test_an_applied_cluster_with_no_applied_tag_does_not_link_applied(self):
        # If this used the SAME widened type list as the count, the applied
        # tag-cluster below would make the link appear -- pointing at
        # `/tags/applied`, which never draws a cluster row at all.
        store = _FakeStore([
            _tag_item("t-open", state="new"),
            _cluster_item("c-applied", state="applied"),
        ])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertNotIn('href="/tags/applied"', body)

    def test_a_dismissed_cluster_with_no_dismissed_tag_does_not_link_dismissed(self):
        store = _FakeStore([
            _tag_item("t-open", state="new"),
            _cluster_item("c-dismissed", state="dismissed"),
        ])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertNotIn('href="/tags/dismissed"', body)

    def test_an_applied_tag_still_links_applied_regardless(self):
        # The narrowing above must not swallow the ordinary case: an applied
        # row of a type the generic route DOES draw still lights up the link.
        store = _FakeStore([
            _tag_item("t-applied", state="applied"),
            _cluster_item("c-open", state="new"),
        ])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertIn('href="/tags/applied"', body)


class TheThreeSpecialSectionsAppearOnlyOnTheWorkingQueueView(unittest.TestCase):
    """`merges`/`reconciles`/`unused` are composed only for `/{name}` itself
    (`state is None`), never for the terminal `/{name}/{state}` routes --
    each of the three already shows every state a control belongs to, inline,
    on the working-queue page, so a second copy of the section on
    `/tags/dismissed` or `/tags/muted` would either repeat it or, worse,
    silently show a "nothing found" a mutation could make disagree with the
    real one.
    """

    def test_the_dismissed_state_route_carries_no_merge_section_at_all(self):
        sent = _drive("GET", "/tags/dismissed", store=_FakeStore([]),
                      merges=lambda: to_merge_rows(
                          [_merge_item(state="dismissed")]))
        body = sent["body"].decode()
        self.assertNotIn("Tag merges (", body)
        self.assertNotIn("Velvet Crane", body)

    def test_the_muted_state_route_carries_no_unused_section_at_all(self):
        sent = _drive("GET", "/tags/muted", store=_FakeStore([]),
                      unused=lambda: to_unused_groups(
                          [_unused_item(state="muted")]))
        body = sent["body"].decode()
        self.assertNotIn("Tags that do almost no work (", body)
        self.assertNotIn("Faded Ledger", body)


class TheSidebarAppearsOnEveryPage(unittest.TestCase):
    """Cross-navigation is the point: a per-inbox page has to lead somewhere
    other than itself, and so does the summary."""

    def test_the_summary_page_links_to_each_inbox(self):
        store = _FakeStore([_tag_item("t1")])
        body = _drive("GET", "/", store=store)["body"].decode()
        self.assertIn('href="/scenes"', body)
        self.assertIn('href="/tags"', body)
        self.assertIn('href="/performers"', body)

    def test_the_combined_inbox_page_links_to_each_inbox(self):
        store = _FakeStore([_tag_item("t1")])
        body = _drive("GET", "/inbox", store=store)["body"].decode()
        self.assertIn('href="/scenes"', body)
        self.assertIn('href="/tags"', body)
        self.assertIn('href="/performers"', body)

    def test_a_per_inbox_page_links_back_to_the_other_inboxes(self):
        store = _FakeStore([_tag_item("t1")])
        body = _drive("GET", "/tags", store=store)["body"].decode()
        self.assertIn('href="/scenes"', body)
        self.assertIn('href="/performers"', body)


class TheSidebarIsAbsentWithoutAStore(unittest.TestCase):
    """No store means `/{inbox}` itself 404s (see
    `test_with_no_store_wired_a_per_inbox_route_is_404_not_a_crash`) -- a
    sidebar offering a link into a page that cannot answer would be worse
    than no navigation there, so it is not offered at all."""

    def test_the_summary_page_offers_no_inbox_links_without_a_store(self):
        body = _drive("GET", "/")["body"].decode()
        self.assertNotIn('href="/tags"', body)
        self.assertNotIn('href="/scenes"', body)
        self.assertNotIn('href="/performers"', body)

    def test_the_combined_inbox_page_offers_no_inbox_links_without_a_store(self):
        body = _drive("GET", "/inbox")["body"].decode()
        self.assertNotIn('href="/tags"', body)


# -- pagination, against a real Store ------------------------------------- #
#
# Every test below uses a population LARGER than one page (`PAGE_SIZE`
# scene proposals plus a remainder), on purpose, in every fixture -- a
# fixture the size of one page cannot tell "the true total" apart from
# "what fit on a page", which is exactly the gap this ticket exists to keep
# visible. A real `Store` (SQLite, `:memory:`) is used rather than
# `_FakeStore` here specifically to prove the wiring this ticket adds --
# `Store.items(limit=, offset=)`, `Store.item_count`, `exclude_states` --
# actually reaches a rendered page, not only a Python-level fixture that
# happens to agree with it.

def _record_scene(store, i, now=None):
    """One fingerprint-identified scene proposal -- the shape with the
    fewest required payload fields (see `to_row`'s `identified_by` branch),
    which is all these tests need: a distinct, orderable row.
    """
    return store.record(
        folder="library", subject_type="scene", subject_id="scene-%d" % i,
        summary="a proposal",
        payload={"path": "/invented/library/%03d.mp4" % i,
                "identified_by": "invented-box", "box": "invented-box",
                "candidate": {"title": "Invented Title %03d" % i,
                             "image": None, "performers": [],
                             "studio": None}},
        producer="test-producer", now=now)


def _scene_rows_callables(store):
    """The same shape `cronicled.__main__._inbox_rows`/`_inbox_rows_count`
    wire in production, narrowed to just `scene` (nothing here needs the
    tag/performer special sections) -- built here rather than imported,
    because those two are closures private to `main()`.
    """
    def rows(limit=None, offset=0):
        items = store.items(subject_types=("scene",),
                            exclude_states=("applied",),
                            limit=limit, offset=offset)
        return to_rows(items)

    def rows_count():
        return store.item_count(subject_types=("scene",),
                                exclude_states=("applied",))

    return rows, rows_count


def _drive_paginated(method, path, store, body=b""):
    """Like `_drive`, but lets `rows`/`rows_count` be the REAL,
    `store`-backed callables above -- `_drive` itself hardcodes a zero-row
    `rows`, which is right for every test elsewhere in this module (none of
    them exercise real pagination) and wrong for the ones below.
    """
    rows, rows_count = _scene_rows_callables(store)
    handler = build_handler(rows=rows, rows_count=rows_count,
                            actions=_RecordingActions(), store=store)
    instance = object.__new__(handler)
    instance.path = path
    instance.headers = email.message.Message()
    if body:
        instance.headers["Content-Length"] = str(len(body))
    instance.rfile = io.BytesIO(body)
    sent = {}
    instance._send = lambda status, body=b"", headers=(): sent.update(
        status=status, body=body, headers=dict(headers))
    getattr(instance, "do_" + method)()
    return sent


_FINGERPRINT_IN_A_FORM_RE = re.compile(
    r'<input type="hidden" name="fp" value="(?P<fp>[^"]*)">')


class TheGenericListIsBounded(unittest.TestCase):
    """Acceptance: "a page must render at most a bounded number of rows;
    a test must fail if the bound is exceeded, asserted on the rendered
    output rather than on a parameter." 250 > `PAGE_SIZE` (200).
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        for i in range(250):
            _record_scene(self.store, i,
                         now="2026-07-01T00:00:00.%06d" % i)

    def test_the_first_page_renders_no_more_than_the_page_size(self):
        body = _drive_paginated("GET", "/inbox", self.store)["body"].decode()
        self.assertLessEqual(body.count('class="proposal"'), PAGE_SIZE)
        # Not merely "fewer than 250" -- exactly the bound, since 250 rows
        # over a 200-row page leaves a full first page.
        self.assertEqual(body.count('class="proposal"'), PAGE_SIZE)

    def test_the_second_page_renders_the_remainder(self):
        body = _drive_paginated(
            "GET", "/inbox?page=2", self.store)["body"].decode()
        self.assertEqual(body.count('class="proposal"'), 50)


class CountsComeFromTheStoreNotFromThePage(unittest.TestCase):
    """Acceptance: counts must keep coming from the store, never from what
    was rendered -- pinned on a fixture (250 rows) strictly larger than one
    page (200), which a same-or-smaller fixture cannot distinguish this
    from.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        for i in range(250):
            _record_scene(self.store, i,
                         now="2026-07-01T00:00:00.%06d" % i)

    def test_the_sidebar_states_the_true_total_not_the_page_size(self):
        body = _drive_paginated("GET", "/inbox", self.store)["body"].decode()
        self.assertIn(">250<", body)
        self.assertNotIn(">%d<" % PAGE_SIZE, body)

    def test_the_summary_pages_own_sidebar_states_the_true_total_too(self):
        # The sidebar is the same partial on every page (see
        # `_sidebar_context`); this is not a claim about the summary's own
        # waiting-total block, which needs its own `summary=` callable that
        # nothing here wires -- see `TheSidebarTotalAgreesWithTheSummarysWaitingTotal`
        # for that guarantee, which this ticket does not touch.
        body = _drive_paginated("GET", "/", self.store)["body"].decode()
        self.assertIn(">250<", body)

    def test_the_second_pages_own_sidebar_still_states_the_true_total(self):
        # The count is a property of the STORE, not of which page is open --
        # a sidebar that only got this right on page 1 would still be
        # reading it from what got rendered, just less obviously.
        body = _drive_paginated(
            "GET", "/inbox?page=2", self.store)["body"].decode()
        self.assertIn(">250<", body)


class EveryWaitingProposalIsReachable(unittest.TestCase):
    """Acceptance: every waiting proposal must be reachable -- a test must
    fail if a row exists that no page reaches. Walks every page a
    `Prev`/`Next` control could actually lead to and checks the union
    against the full set the store holds.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.fingerprints = {
            _record_scene(self.store, i, now="2026-07-01T00:00:00.%06d" % i)
            for i in range(250)
        }

    def _fingerprints_on(self, page):
        body = _drive_paginated(
            "GET", "/inbox?page=%d" % page, self.store)["body"].decode()
        return set(_FINGERPRINT_IN_A_FORM_RE.findall(body))

    def test_every_recorded_fingerprint_appears_on_some_page(self):
        seen = self._fingerprints_on(1) | self._fingerprints_on(2)
        self.assertEqual(seen, self.fingerprints)

    def test_the_two_pages_do_not_overlap(self):
        # Each row reachable from exactly one page, not duplicated onto a
        # neighbour and not left out of both.
        page_one = self._fingerprints_on(1)
        page_two = self._fingerprints_on(2)
        self.assertEqual(page_one & page_two, set())
        self.assertEqual(len(page_one), PAGE_SIZE)
        self.assertEqual(len(page_two), 50)

    def test_a_page_past_the_end_is_simply_empty_not_an_error(self):
        body = _drive_paginated(
            "GET", "/inbox?page=99", self.store)
        self.assertEqual(body["status"], 200)


class TheOrderIsStableAcrossRenders(unittest.TestCase):
    """Acceptance: the order must be stable across renders; a test must
    fail if acting on one row changes which rows appear on a later page.

    The store's order (`created_at`, then the fingerprint as a tiebreak --
    see `Store.items`'s own docstring) is a fixed total order for a row's
    whole life: nothing about rendering, or about a DIFFERENT row being
    acted on, may reshuffle it. What acting on a row legitimately changes is
    the SIZE of the waiting set (removing that one row) -- distinguished
    here from a row going silently missing, which is what an unstable order
    would produce.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.fingerprints = [
            _record_scene(self.store, i, now="2026-07-01T00:00:00.%06d" % i)
            for i in range(250)
        ]

    def test_rendering_the_same_page_twice_gives_byte_identical_output(self):
        first = _drive_paginated("GET", "/inbox", self.store)["body"]
        second = _drive_paginated("GET", "/inbox", self.store)["body"]
        self.assertEqual(first, second)

    def test_approving_a_row_on_page_one_does_not_disturb_page_ones_rest(self):
        before = re.findall(_FINGERPRINT_IN_A_FORM_RE,
                            _drive_paginated("GET", "/inbox",
                                            self.store)["body"].decode())
        # The oldest waiting row -- first in the store's own order -- is
        # removed from the waiting set entirely.
        self.store.mark_applied(self.fingerprints[0])
        after = re.findall(_FINGERPRINT_IN_A_FORM_RE,
                           _drive_paginated("GET", "/inbox",
                                           self.store)["body"].decode())
        # Deduplicate (each row posts its own fingerprint on more than one
        # form) while keeping the ORDER the page draws them in -- a plain
        # `set` would prove membership but not this test's actual claim.
        def ordered_unique(fps):
            seen = []
            for fp in fps:
                if fp not in seen:
                    seen.append(fp)
            return seen

        before_unique = ordered_unique(before)
        after_unique = ordered_unique(after)
        # Every row that was on page one and was NOT the one just approved
        # is still there, in the SAME relative order -- nothing shuffled.
        # The list is still exactly `PAGE_SIZE` long (249 rows remain, still
        # more than one page), so the LAST slot is now filled by whatever
        # row immediately followed page one before -- refilling from behind
        # is the correctly-working version of "the set got one row
        # smaller", not a bug; what this pins is that it is filled by that
        # SPECIFIC row and not some other one shuffled forward out of order.
        self.assertEqual(after_unique[:-1], before_unique[1:])
        self.assertEqual(len(after_unique), PAGE_SIZE)

    def test_no_other_row_is_skipped_by_the_removal(self):
        self.store.mark_applied(self.fingerprints[0])
        page_one = set(re.findall(_FINGERPRINT_IN_A_FORM_RE,
                                  _drive_paginated(
                                      "GET", "/inbox",
                                      self.store)["body"].decode()))
        page_two = set(re.findall(_FINGERPRINT_IN_A_FORM_RE,
                                  _drive_paginated(
                                      "GET", "/inbox?page=2",
                                      self.store)["body"].decode()))
        self.assertEqual(page_one | page_two,
                         set(self.fingerprints[1:]))
        self.assertEqual(page_one & page_two, set())


class ThePagerHelper(unittest.TestCase):
    """`web.app._pager`/`_current_pages` -- the Python side of the pager
    context a template reads (see `tests.test_web_render.ThePagerControl`
    for the template side). Exercised directly so a mistake in the href
    arithmetic is caught here rather than only by a rendered-page assertion
    that happens to include the right substring for the wrong reason.
    """

    def test_current_pages_reads_every_known_key(self):
        qs = {"page": ["3"], "applied_page": ["2"]}
        current = _current_pages(qs)
        self.assertEqual(current["page"], 3)
        self.assertEqual(current["applied_page"], 2)
        # Every OTHER key defaults to page 1, not merely the ones present.
        self.assertEqual(current["dismissed_page"], 1)
        self.assertEqual(current["muted_page"], 1)

    def test_a_malformed_value_reads_as_page_one(self):
        current = _current_pages({"page": ["not-a-number"]})
        self.assertEqual(current["page"], 1)

    def test_the_next_href_advances_only_the_named_key(self):
        current = _current_pages({"page": ["1"], "applied_page": ["4"]})
        pager = _pager("/inbox", current, "page", total=450)
        self.assertEqual(pager["next_href"], "/inbox?page=2&applied_page=4")

    def test_the_prev_href_goes_back_only_the_named_key(self):
        current = _current_pages({"page": ["2"]})
        pager = _pager("/inbox", current, "page", total=450)
        self.assertEqual(pager["prev_href"], "/inbox")

    def test_a_key_at_its_default_is_dropped_from_the_href(self):
        # `applied_page` sits at 1 (its default), so it is left out of the
        # query string entirely -- a page with nothing else paginated keeps
        # a plain, bookmarkable URL.
        current = _current_pages({"page": ["1"], "applied_page": ["1"]})
        pager = _pager("/inbox", current, "page", total=450)
        self.assertEqual(pager["next_href"], "/inbox?page=2")

    def test_no_prev_on_page_one(self):
        current = _current_pages({"page": ["1"]})
        pager = _pager("/inbox", current, "page", total=450)
        self.assertIsNone(pager["prev_href"])

    def test_no_next_on_the_last_page(self):
        current = _current_pages({"page": ["3"]})
        # 450 rows at the default PAGE_SIZE (200) is exactly 3 pages.
        pager = _pager("/inbox", current, "page", total=450)
        self.assertEqual(pager["total_pages"], 3)
        self.assertIsNone(pager["next_href"])

    def test_the_total_is_carried_through_unchanged(self):
        current = _current_pages({})
        pager = _pager("/inbox", current, "page", total=450)
        self.assertEqual(pager["total"], 450)

    def test_two_different_sections_pagers_do_not_interfere(self):
        current = _current_pages({"page": ["5"], "applied_page": ["2"]})
        # 1200, not 1000: page 5 of 1000-at-200/page is the LAST page (5),
        # which would have no `next_href` at all and defeat this test's own
        # assertion below for a reason unrelated to what it is pinning.
        rows_pager = _pager("/inbox", current, "page", total=1200)
        # Also more than one page past `applied_page`'s own current value
        # (2), for the same reason.
        applied_pager = _pager("/inbox", current, "applied_page", total=800)
        self.assertEqual(rows_pager["page"], 5)
        self.assertEqual(applied_pager["page"], 2)
        # Paging `rows` forward carries `applied_page` along unchanged, and
        # vice versa.
        self.assertIn("applied_page=2", rows_pager["next_href"])
        self.assertIn("page=5", applied_pager["next_href"])


class BulkApplySubmitsExactlyThePagesOwnFingerprints(unittest.TestCase):
    """Acceptance: bulk apply must write exactly the rows submitted from
    the page that showed them. Pagination makes the SHOWN set smaller --
    which is safer -- but only if the bulk form's hidden fields stay that
    page's own rows and never the whole waiting population, which is what
    ticket bulk179's own population (1456) would otherwise put in one form.
    """

    def _tag_items(self, n):
        return [_tag_item(str(i)) for i in range(n)]

    def _bulk_form_fingerprints(self, body):
        match = re.search(
            r'<form method="post" action="/bulk_apply_tag_descriptions">'
            r'(?P<body>.*?)</form>', body, re.DOTALL)
        return [] if match is None else _FINGERPRINT_IN_A_FORM_RE.findall(
            match.group("body"))

    def test_the_bulk_form_on_page_one_carries_only_page_ones_rows(self):
        store = _FakeStore(self._tag_items(250))
        body = _drive("GET", "/tags", store=store)["body"].decode()
        fps = self._bulk_form_fingerprints(body)
        self.assertEqual(len(fps), PAGE_SIZE)
        self.assertEqual(fps, [str(i) for i in range(PAGE_SIZE)])

    def test_the_bulk_form_on_page_two_carries_only_page_twos_rows(self):
        store = _FakeStore(self._tag_items(250))
        body = _drive("GET", "/tags?page=2", store=store)["body"].decode()
        fps = self._bulk_form_fingerprints(body)
        self.assertEqual(fps, [str(i) for i in range(PAGE_SIZE, 250)])

    def test_submitting_page_ones_form_applies_only_page_ones_rows(self):
        store = _FakeStore(self._tag_items(250))
        actions = _RecordingActions()
        body = "&".join("fp=%d" % i for i in range(PAGE_SIZE))
        sent = _drive("POST", "/bulk_apply_tag_descriptions", body.encode(),
                      store=store, actions=actions)
        self.assertEqual(sent["status"], 303)
        self.assertEqual(actions.calls,
                         [("bulk_apply_tag_descriptions",
                           tuple(str(i) for i in range(PAGE_SIZE)))])


_CHECKBOX_FP_RE = re.compile(
    r'<input type="checkbox" name="fp" value="(?P<fp>[^"]*)"')

# The "select all on this page" form's own action tag, deliberately WITHOUT
# an `id` attribute -- `#batch-form` (the ticked-selection form) carries one
# and so never matches this, letting this regex isolate the other form's
# hidden fields specifically. See inbox.html.
_SELECT_ALL_FORM_RE = re.compile(
    r'<form method="post" action="/batch">(?P<body>.*?)</form>', re.DOTALL)


class TheBatchCheckboxSelectionIsBoundToThePage(unittest.TestCase):
    """Acceptance: "select all" (and every individual checkbox) must not
    reach beyond the rendered page -- a test must fail if either carries a
    fingerprint the page did not actually draw. Pinned against a REAL
    `Store` and 250 scene rows (more than one `PAGE_SIZE` page), the same
    fixture `EveryWaitingProposalIsReachable`/`TheOrderIsStableAcrossRenders`
    use above for the identical claim about the existing per-row forms.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.fingerprints = [
            _record_scene(self.store, i, now="2026-07-01T00:00:00.%06d" % i)
            for i in range(250)
        ]

    def _page_body(self, page=None):
        path = "/inbox" if page is None else "/inbox?page=%d" % page
        return _drive_paginated("GET", path, self.store)["body"].decode()

    def test_page_ones_checkboxes_are_exactly_page_ones_own_rows(self):
        body = self._page_body()
        checked = _CHECKBOX_FP_RE.findall(body)
        self.assertEqual(len(checked), PAGE_SIZE)
        self.assertEqual(set(checked), set(self.fingerprints[:PAGE_SIZE]))

    def test_page_twos_checkboxes_never_include_a_page_one_fingerprint(self):
        body = self._page_body(2)
        checked = set(_CHECKBOX_FP_RE.findall(body))
        self.assertEqual(checked, set(self.fingerprints[PAGE_SIZE:]))
        self.assertEqual(checked & set(self.fingerprints[:PAGE_SIZE]), set())

    def test_the_select_all_form_on_page_one_carries_only_page_ones_rows(self):
        body = self._page_body()
        match = _SELECT_ALL_FORM_RE.search(body)
        self.assertIsNotNone(match)
        fps = _FINGERPRINT_IN_A_FORM_RE.findall(match.group("body"))
        self.assertEqual(len(fps), PAGE_SIZE)
        self.assertEqual(set(fps), set(self.fingerprints[:PAGE_SIZE]))

    def test_the_select_all_form_on_page_two_carries_only_page_twos_rows(self):
        body = self._page_body(2)
        match = _SELECT_ALL_FORM_RE.search(body)
        self.assertIsNotNone(match)
        fps = _FINGERPRINT_IN_A_FORM_RE.findall(match.group("body"))
        self.assertEqual(set(fps), set(self.fingerprints[PAGE_SIZE:]))


if __name__ == "__main__":
    unittest.main()
