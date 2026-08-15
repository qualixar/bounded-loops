"""Shared security posture for loopback-only HTTP servers in bounded-loops.

These primitives are extracted from ``console/server.py`` so that the SSE
watch server (``sse_server.py``) does not re-implement them. Any future
loopback surface should import from here, not copy.

Security model (LOCAL posture — same as the console, by construction):

* Loopback bind: ``_LOOPBACK_HOST = "127.0.0.1"`` is a module constant, not a
  parameter, so no caller can point a server at a routable interface without
  editing this file.
* Token: ``secrets.token_urlsafe(_TOKEN_BYTES)`` per invocation, compared with
  ``hmac.compare_digest`` (constant-time, bytes vs bytes, never fails on length
  mismatch).
* CSRF defense: ``origin_ok`` requires a same-origin ``Origin`` header, or
  failing that a same-origin ``Referer``.  Absent both → fails closed.
* Hardening headers: ``Referrer-Policy``, ``X-Content-Type-Options``,
  ``Cache-Control``, ``Pragma`` on every response.
* Connection semaphore: ``_MAX_CONCURRENT_CONNECTIONS`` caps in-flight threads.

A HOSTED / multi-tenant deployment must not reuse this module as-is: it needs
real authentication, TLS, and a role-checking authorizer.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_LOOPBACK_HOST: str = "127.0.0.1"
_TOKEN_BYTES: int = 32   # secrets.token_urlsafe(32) → 43-char URL-safe token, ~256 bits
_MAX_CONCURRENT_CONNECTIONS: int = 8
_MAX_BODY_BYTES: int = 8 * 1024
_FORM_CONTENT_TYPE: str = "application/x-www-form-urlencoded"


# ── pure security functions ────────────────────────────────────────────────────

def token_ok(provided: str, expected: str) -> bool:
    """Constant-time token comparison.

    Compares bytes so a mismatched length never raises, and comparison time
    never varies with WHICH byte differs — both properties of
    ``hmac.compare_digest`` on bytes arguments.
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def origin_ok(headers: object, expected_origin: str) -> bool:
    """CSRF defense in depth: require a same-origin Origin, or failing that Referer.

    ``headers`` must support ``.get(key, default=None)`` — any
    ``http.server.BaseHTTPRequestHandler.headers`` or compatible mapping.
    Fails closed (returns False) when neither header is present.
    """
    origin = headers.get("Origin")  # type: ignore[attr-defined]
    if origin is not None:
        return origin == expected_origin
    referer = headers.get("Referer")  # type: ignore[attr-defined]
    if referer is not None:
        return referer == expected_origin or referer.startswith(expected_origin + "/")
    return False  # neither header present → fail closed, never assume same-origin


# ── server base class ──────────────────────────────────────────────────────────

class LoopbackServer(ThreadingHTTPServer):
    """Base class: loopback-only, semaphore-bounded, per-invocation token.

    Subclasses pass ``handler_class`` as the ``RequestHandlerClass`` and
    add any domain-specific attributes before calling ``super().__init__``.

    There is deliberately NO ``host`` parameter: the bind address is the
    literal ``_LOOPBACK_HOST`` below, not a variable, so no caller can point
    this at a routable interface without editing this file.
    """

    daemon_threads = True
    request_queue_size = _MAX_CONCURRENT_CONNECTIONS

    def __init__(self, handler_class: type, *, port: int = 0) -> None:
        super().__init__((_LOOPBACK_HOST, port), handler_class)
        self.token: str = secrets.token_urlsafe(_TOKEN_BYTES)
        self._connection_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_CONNECTIONS)

    @property
    def server_url(self) -> str:
        """Base URL for this server (no path, no token)."""
        port = self.server_address[1]
        return f"http://{_LOOPBACK_HOST}:{port}"

    def process_request(self, request: object, client_address: object) -> None:
        """Refuse immediately with 503 when the connection semaphore is exhausted."""
        if not self._connection_semaphore.acquire(blocking=False):
            try:
                request.sendall(  # type: ignore[attr-defined]
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)  # type: ignore[arg-type]
            return
        super().process_request(request, client_address)  # type: ignore[arg-type]

    def process_request_thread(self, request: object, client_address: object) -> None:
        """Like the base-class implementation but releases the connection semaphore."""
        try:
            super().process_request_thread(request, client_address)  # type: ignore[arg-type]
        finally:
            self._connection_semaphore.release()


# ── request handler base class ─────────────────────────────────────────────────

class LoopbackHandler(BaseHTTPRequestHandler):
    """Base class: logging suppressed, hardening headers, plain/html send helpers.

    Subclasses annotate ``server`` with their own concrete server type.
    ``protocol_version = "HTTP/1.0"`` (inherited): every response closes the
    connection — no persistent-connection desync risk.
    """

    server: LoopbackServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.0"
    timeout = 30

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # suppress access log — printed URL is the CLI's only output contract

    # ── shared validation helpers ──────────────────────────────────────────────

    def _split_path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query, keep_blank_values=True)

    def _first(self, fields: dict[str, list[str]], key: str) -> str:
        values = fields.get(key)
        return values[0] if values else ""

    def _token_ok(self, provided: str) -> bool:
        return token_ok(provided, self.server.token)

    def _origin_ok(self) -> bool:
        expected = self.server.server_url
        return origin_ok(self.headers, expected)

    # ── response helpers ───────────────────────────────────────────────────────

    def _send_hardening_headers(self) -> None:
        """Sent on EVERY response — success or error.

        ``Referrer-Policy: no-referrer`` prevents the token (which lives in
        the URL query string by design) from leaking to any future external
        resource via the Referer header.
        ``Cache-Control: no-store`` + ``Pragma: no-cache``: the token-bearing
        URL/page must never be written to disk cache past this process's
        lifetime.
        """
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        # The page carried neither of these while the SSE response carried X-Frame-Options,
        # which is backwards: the page is the surface holding the token and the controls.
        # Framing it is how a local page in another tab gets a clickjacked Approve.
        self.send_header("X-Frame-Options", "DENY")
        # Everything this UI loads is packaged and same-origin — React and htm are vendored,
        # there are no outbound links and no CDN. So the policy can be closed rather than
        # merely tidy. 'unsafe-inline' for style is the one concession: the app sets inline
        # style attributes for the DAG layout, which are computed, not author-controlled text.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )

    def _send_sse_data(self, json_payload: str) -> bool:
        """Write one SSE event. Returns False when the connection is already broken.

        Lives on the shared handler because every loopback surface that streams needs exactly
        this, and a second copy would be a second place for the framing to go subtly wrong.
        """
        try:
            self.wfile.write(f"data: {json_payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _send_sse_comment(self, text: str) -> bool:
        """Write an SSE keepalive comment. Returns False when the connection is broken."""
        try:
            self.wfile.write(f": {text}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _send_sse_headers(self) -> None:
        """The response headers for a stream: the shared hardening set, plus streaming specifics.

        An audit noted the stream was the one token-bearing response that skipped the shared
        hardening headers. The first fix re-listed them here instead of calling the shared helper,
        which left two copies that promptly disagreed — this one omitted `Pragma: no-cache` and the
        entire `Content-Security-Policy`, while its docstring claimed no streaming surface could
        forget them. Copying a security header list is how half of it goes missing.

        `X-Accel-Buffering: no` is the only genuinely stream-specific header: it stops a reverse
        proxy buffering an event stream into uselessness.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self._send_hardening_headers()
        self.end_headers()

    def _send_plain(self, status: HTTPStatus, message: str) -> None:
        encoded = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._send_hardening_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status: HTTPStatus, document: str) -> None:
        encoded = document.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._send_hardening_headers()
        self.end_headers()
        self.wfile.write(encoded)

    # ── strict method allowlist ────────────────────────────────────────────────

    def do_HEAD(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_PUT(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_DELETE(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_PATCH(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_OPTIONS(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_TRACE(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def do_CONNECT(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def __getattr__(self, name: str):
        """Any OTHER verb — `FROB`, `PROPFIND`, anything — also gets 405.

        The explicit handlers above cover the standard verbs. `BaseHTTPRequestHandler` dispatches
        on `getattr(self, "do_" + command)`, so an unlisted verb fell through to its default 501
        "Unsupported method". That is a response shape distinguishable from 405, produced BEFORE
        any token check, so an unauthenticated caller could tell this server apart from one that
        answers uniformly — a small fingerprinting seam, and a needless one.

        Handled here rather than by adding more `do_*` methods because the set of verbs someone
        can send is not enumerable. Only `do_*` is intercepted; every other missing attribute
        raises as normal, so this cannot mask a genuine typo elsewhere in the handler.
        """
        if not name.startswith("do_"):
            raise AttributeError(name)

        def _refuse() -> None:
            self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

        return _refuse
