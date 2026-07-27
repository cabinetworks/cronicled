import http.client
import threading
import unittest
from http.server import HTTPServer

from cronicled.web.app import build_handler, DEFAULT_HOST


class _RecordingActions:
    def __init__(self):
        self.calls = []

    def approve(self, fp):
        self.calls.append(("approve", fp)); return "applied"

    def dismiss(self, fp):
        self.calls.append(("dismiss", fp)); return "dismissed"

    def mute(self, fp):
        self.calls.append(("mute", fp)); return "muted"

    def undo(self, fp):
        self.calls.append(("undo", fp)); return "reverted"


class _Server:
    def __enter__(self):
        self.actions = _RecordingActions()
        handler = build_handler(rows=lambda: [], actions=self.actions)
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address)
        headers = ({"Content-Type": "application/x-www-form-urlencoded"}
                   if body else {})
        conn.request(method, path, body, headers)
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


class Binding(unittest.TestCase):
    def test_the_default_host_is_loopback(self):
        # There is no authentication. A listener on all interfaces would be a
        # stranger able to rewrite a library.
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
