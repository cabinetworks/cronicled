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
