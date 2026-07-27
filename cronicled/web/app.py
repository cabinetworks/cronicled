"""The HTTP surface: routing, and nothing else.

Every write is a POST followed by a redirect. A GET to an action path is
refused rather than tolerated — browsers prefetch, and a link that applies a
proposal will eventually be followed by something that is not a person.
"""

import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from .render import render

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8571

_ACTIONS = ("approve", "dismiss", "mute", "undo")


def build_handler(rows, actions):
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
            path = urllib.parse.urlparse(self.path).path
            if path.lstrip("/") in _ACTIONS:
                self._send(405, b"use POST",
                           [("Allow", "POST"),
                            ("Content-Type", "text/plain; charset=utf-8")])
                return
            if path != "/":
                self._send(404, b"not found")
                return
            body = render("inbox.html", rows=rows(), counts={}).encode()
            self._send(200, body,
                       [("Content-Type", "text/html; charset=utf-8")])

        def do_POST(self):
            name = urllib.parse.urlparse(self.path).path.lstrip("/")
            if name not in _ACTIONS:
                self._send(404, b"not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8"))
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
            # 303 so a refresh redraws the page rather than repeating the write.
            self._send(303, b"", [("Location", "/")])

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(rows, actions, host=DEFAULT_HOST, port=DEFAULT_PORT):
    if host != DEFAULT_HOST:
        # Loud, because there is no authentication: the binding is the only
        # thing standing between this page and anyone who can reach the host.
        print("WARNING: binding to %s, not %s. This page has NO "
              "authentication and its buttons write to your library."
              % (host, DEFAULT_HOST))
    httpd = HTTPServer((host, port), build_handler(rows, actions))
    print("inbox on http://%s:%d/" % (host, port))
    httpd.serve_forever()
