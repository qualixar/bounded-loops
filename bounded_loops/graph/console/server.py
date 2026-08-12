"""Loopback-only HTTP console for `bl graph console` (Slice 3 — click-to-approve).

Security model (LOCAL posture — see the honesty banner in console_template.html
for the same statement rendered to the operator):

* Binds ``127.0.0.1`` ONLY. There is no ``host=`` parameter anywhere in this
  module — the only way to change the bind address is to edit this file.
* Every request — GET and POST alike — must carry the per-invocation
  ``secrets.token_urlsafe`` token generated in ``ConsoleServer.__init__``,
  compared with ``hmac.compare_digest`` (constant-time). The token is the
  capability: anyone on this machine who has it can decide approvals for this
  one run until the console exits.
* POST additionally requires a same-origin ``Origin`` (or, failing that,
  ``Referer``) header — a CSRF defense in depth on top of the token, in case
  the token leaks by some channel other than reading this page (a proxy log,
  shell history, a screen share).
* Fixed routes only: ``GET /`` and ``POST /approve`` / ``POST /reject``. No
  request path is ever used to open a file, so there is no path-traversal
  surface to defend — an unknown or `../`-laden path simply does not match
  any route and 404s like any other unknown path.
* Every decision is made by calling ``LocalGraphRuntimeFacade.approve()``
  UNCHANGED (Slice 1's durable machinery: authority checks, idempotent
  commit, atomic file-locked persistence). This module adds no new approval
  path, no new persistence, and never mutates run state directly — a worker
  can never self-approve because a worker has no code path into this server
  at all; only ``LocalGraphRuntimeFacade.approve()`` writes a decision.

A HOSTED / multi-tenant deployment must NOT reuse this module as-is: it needs
real authentication, TLS, and a role-checking authorizer. This is a
single-operator, single-run, local development console — nothing more.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from bounded_loops.graph.application.arena_projection import ArenaProjection, ArenaReadRequest
from bounded_loops.graph.application.execute_graph import _awaiting_approval_nodes
from bounded_loops.graph.application.graph_runtime_facade import LocalGraphRuntimeFacade
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.console.rendering import render_console_page
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity

_LOOPBACK_HOST = "127.0.0.1"
_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> a 43-char URL-safe token, ~256 bits.
_MAX_BODY_BYTES = 8 * 1024  # generous for a token + node_id; refuses anything larger.
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
# Maximum concurrent in-flight connections (L-1 / security hardening).  A local-DoS
# attack from another unprivileged process that opens connections faster than this
# console can close them is bounded here: once the semaphore is exhausted, additional
# connections receive a minimal HTTP 503 and are refused before any thread is spawned.
# Sized to match `request_queue_size` so OS-level accept backlog and in-flight thread
# cap are both intentional, reviewable numbers.
_MAX_CONCURRENT_CONNECTIONS = 8


class ConsoleOpenError(Exception):
    """Raised when ``--run`` cannot be opened as a safe, valid run directory."""


def open_console_run(run_dir: Path) -> tuple[GraphRunIdentity, LocalGraphRuntimeFacade]:
    """Validate *run_dir* and build its facade + identity ONCE, before any socket binds.

    Reuses ``LocalGraphRuntimeFacade.for_run_dir`` (symlink guard + run-shape
    validation) and ``plan_persistence.load_plan_from_run_dir`` (identity
    reconstruction) UNCHANGED — the exact two calls ``cmd_graph_approve`` already
    trusts. Neither check is re-implemented here; a failure in either raises
    ``ConsoleOpenError`` with the original message, so the CLI layer has one
    exception type to catch.

    Both imports are now module-level (ARCH-01 fix): the import cycle that forced
    deferred imports here was broken when ``graph_runtime_facade.py`` stopped
    importing from ``cli_graph.py``.
    """
    try:
        facade = LocalGraphRuntimeFacade.for_run_dir(run_dir)
    except (GraphIntegrityError, GraphValidationError) as exc:
        raise ConsoleOpenError(str(exc)) from exc

    # `for_run_dir` already refused a symlinked/invalid run_dir, so resolving here is
    # safe — mirrors cli_graph_approve._load_identity_and_facade's own resolve-after-
    # validate discipline (never re-derive identity from the un-resolved caller path).
    try:
        _plan, identity, _meta = load_plan_from_run_dir(run_dir.resolve())
    except (FileNotFoundError, ValueError, GraphValidationError) as exc:
        raise ConsoleOpenError(str(exc)) from exc
    return identity, facade


class ConsoleServer(ThreadingHTTPServer):
    """A short-lived, loopback-only HTTP server scoped to exactly one run directory.

    There is deliberately NO ``host`` parameter: the bind address is the literal
    string ``127.0.0.1`` below, not a variable, so no caller — test, CLI flag, or
    future edit — can point this at a routable interface without editing this file.

    Connection limit (L-1 fix): ``_connection_semaphore`` caps in-flight threads at
    ``_MAX_CONCURRENT_CONNECTIONS``.  A connection that cannot acquire the semaphore
    is refused immediately with a minimal HTTP 503 before any thread is spawned, so
    an unprivileged local process cannot exhaust threads or file descriptors by opening
    connections faster than the console can close them.  ``request_queue_size`` is set
    explicitly (rather than left at the stdlib default) so the OS-level accept backlog
    and the in-flight thread cap are both intentional, reviewable numbers.
    """

    daemon_threads = True
    request_queue_size = _MAX_CONCURRENT_CONNECTIONS

    def __init__(
        self,
        *,
        identity: GraphRunIdentity,
        facade: LocalGraphRuntimeFacade,
        port: int = 0,
    ) -> None:
        super().__init__((_LOOPBACK_HOST, port), ConsoleRequestHandler)
        self.identity = identity
        self.facade = facade
        self.token = secrets.token_urlsafe(_TOKEN_BYTES)
        self.request_ctx = ArenaReadRequest(
            subject_id=identity.organization_id,
            organization_id=identity.organization_id,
            project_id=identity.project_id,
            run_id=identity.run_id,
        )
        # `decisions_made` counts successful POSTs (see `record_decision`); until at
        # least one decision has been made, `maybe_auto_stop` never fires — a run that
        # already has nothing pending when the console starts is left running for the
        # operator to look at, not auto-closed out from under them.
        self.decisions_made = 0
        # Set once a shutdown has actually been scheduled. See `maybe_auto_stop`.
        self.resolved_and_idle = False
        # Guards `decisions_made` and `resolved_and_idle` — both are read/written from
        # DIFFERENT request-handler threads (one per accepted connection under
        # ThreadingHTTPServer): a POST thread calls `record_decision`, a GET thread
        # calls `maybe_auto_stop`, and two concurrent GETs can call `maybe_auto_stop`
        # at once. Without this lock, two concurrent GETs could both observe
        # `resolved_and_idle is False` and each spawn their own shutdown thread. See
        # `maybe_auto_stop` for why this lock is NEVER held across `self.shutdown()`.
        self._decision_lock = threading.Lock()
        # Per-server connection semaphore — see class docstring and _MAX_CONCURRENT_CONNECTIONS.
        self._connection_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_CONNECTIONS)

    def process_request(self, request: object, client_address: object) -> None:
        """Refuse immediately with 503 when the connection semaphore is exhausted."""
        if not self._connection_semaphore.acquire(blocking=False):
            # Semaphore full — send a minimal HTTP 503 and drop the connection before
            # spawning any thread.  This is the DoS-mitigation (L-1 fix).
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
        # Semaphore acquired — delegate to ThreadingHTTPServer which spawns a new thread
        # running `process_request_thread`.  The semaphore is released in that thread.
        super().process_request(request, client_address)  # type: ignore[arg-type]

    def process_request_thread(self, request: object, client_address: object) -> None:
        """Like the base-class implementation but releases the connection semaphore."""
        try:
            super().process_request_thread(request, client_address)  # type: ignore[arg-type]
        finally:
            self._connection_semaphore.release()

    @property
    def console_url(self) -> str:
        """The exact URL — including the token — the operator opens in a browser."""
        # Always `_LOOPBACK_HOST` literally (never read back from `server_address[0]`,
        # whose stdlib type is the ambiguous `str | bytes` socket address union) — this
        # server binds nowhere else, so the constant IS the host, by construction.
        port = self.server_address[1]
        return f"http://{_LOOPBACK_HOST}:{port}/?token={self.token}"

    def record_decision(self) -> None:
        """Called by `ConsoleRequestHandler._decide` after a POST successfully commits."""
        with self._decision_lock:
            self.decisions_made += 1

    def maybe_auto_stop(self, projection: ArenaProjection) -> None:
        """Schedule a shutdown once a decision has been made and none is left pending.

        The LLD calls this console "short-lived: serve until the approval is
        resolved or the operator quits with Ctrl-C" — this is the "resolved" half.
        Deliberately called from `ConsoleRequestHandler._render_index` — i.e. from a
        GET — and NEVER from the POST handler itself: a POST's own 303 redirect sends
        the browser straight into a brand-new GET connection, and if the listening
        socket had already stopped accepting by then, that follow-up GET would hang
        instead of showing the resolved status. Triggering the shutdown only after a
        GET has ALREADY been fully written back removes that race entirely — the
        operator always sees the final page; the console simply does not outlive it.

        The check-and-set of `decisions_made`/`resolved_and_idle` is guarded by
        `_decision_lock` so two GETs arriving concurrently can never both observe
        "not yet idle" and each spawn their own shutdown thread — only ONE ever
        wins the flip to `resolved_and_idle = True`, single-flighting the shutdown.
        `self.shutdown()` itself blocks until `serve_forever()`'s loop notices and
        exits, so it is started on its OWN thread strictly AFTER the `with` block
        has already released the lock — holding the lock across `shutdown()` would
        let a concurrent `record_decision`/`maybe_auto_stop` call block forever on
        a lock this thread is meanwhile blocked waiting to release: a deadlock.
        """
        with self._decision_lock:
            if self.decisions_made == 0 or self.resolved_and_idle:
                return
            if _awaiting_approval_nodes(projection):
                return
            self.resolved_and_idle = True
        threading.Thread(target=self.shutdown, daemon=True).start()


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    """Fixed-route handler: `GET /`, `POST /approve`, `POST /reject` — nothing else.

    HTTP/1.0 semantics deliberately: every response closes the connection, so
    there is no persistent-connection body-framing state to get out of sync with
    a client (this also sidesteps chunked-transfer-encoding request smuggling —
    `_read_form` refuses `Transfer-Encoding` outright regardless, but not having
    keep-alive at all removes an entire class of cross-request desync risk on a
    server this small).

    CRIT finding (documented, deliberately NOT "fixed"): every STANDARD HTTP verb
    is covered — GET/POST are handled; HEAD/PUT/DELETE/PATCH/OPTIONS/TRACE/CONNECT
    each get an explicit `do_*` override below that returns 405. A client that
    sends a genuinely non-standard verb string, though, falls through to
    `BaseHTTPRequestHandler`'s own dynamic `do_<VERB>` lookup and gets the
    stdlib's default 501 Not Implemented instead of this console's 405. That is
    NOT a security bypass — 501 still refuses the request; nothing is dispatched
    and no facade method is ever reachable — only a cosmetic status-code
    mismatch for a request no real HTTP client sends. Closing it would mean
    overriding `handle_one_request` and re-implementing request-line parsing by
    hand, trading the stdlib's well-tested parser for a bespoke one — a strictly
    WORSE security trade for a purely cosmetic gain, so it is left as-is.

    `timeout = 30` (cross-audit FIX 5, Grok M3): a client that sends a header
    claiming `Content-Length: 8192` and then never finishes the body would
    otherwise pin this connection's handler thread in `_read_form`'s
    `self.rfile.read(length)` forever. `socketserver.StreamRequestHandler.setup`
    applies this class attribute as a socket timeout on every accepted
    connection, so a stalled read raises and the connection is torn down
    instead of hanging. This closes the SLOW-READ variant specifically; the
    broader "another local process opens unbounded connections" surface stays
    the accepted, documented residual on `ConsoleServer` above — both cross
    auditors agreed that one is non-blocking for this LOCAL posture.
    """

    server: ConsoleServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.0"
    timeout = 30

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        # Keep stdout clean for the operator — the printed console URL is the only
        # line `bl graph console` promises. Nothing here is ever a secret to hide;
        # it is simply not part of this CLI's output contract.
        pass

    # ── strict method allowlist: everything but GET/POST is refused up front ──

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_TRACE(self) -> None:
        self._method_not_allowed()

    def do_CONNECT(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_plain(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed — this console serves GET and POST only")

    # ── GET / ──────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        """Token is checked BEFORE path routing (cross-audit FIX 4).

        Checking the path first would let an unauthenticated request tell
        `GET /` (403, a live route) apart from `GET /nope` (404, no such
        route) — a listener-liveness oracle for anyone probing this port
        without the token. The token lives in the query string for GET, so it
        costs nothing to check it first: ANY GET lacking a valid token gets
        403 regardless of path; only once the token is valid does an unknown
        path get its own 404. The one exception is `_split_path` itself, which
        only parses the URL and never touches the facade or the filesystem.
        """
        path, query = self._split_path()
        token = _first(query, "token")
        if not self._token_ok(token):
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid token")
            return
        if path != "/":
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")
            return
        self._render_index(token=token, notice=_notice_from_query(query))

    def _render_index(self, *, token: str, notice: str | None) -> None:
        try:
            projection = self.server.facade.status(self.server.request_ctx)
        except (GraphIntegrityError, GraphValidationError) as exc:
            self._send_plain(HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error reading run status — {exc}")
            return
        html = render_console_page(
            identity=self.server.identity, projection=projection, token=token, notice=notice,
        )
        self._send_html(HTTPStatus.OK, html)
        # Only AFTER the page is fully written: see `ConsoleServer.maybe_auto_stop` for
        # why this lives here and not in `_decide` — a POST's own redirect must never
        # race the shutdown of the very socket it is about to reconnect to.
        self.server.maybe_auto_stop(projection)

    # ── POST /approve, POST /reject ─────────────────────────────────────────────

    def do_POST(self) -> None:
        """Route BEFORE the token check — unlike `do_GET` — because for POST the
        token lives in the form BODY, not the query string, so it cannot be
        checked before `_split_path` without reading (and bounding) the body
        first regardless of path. This does leave a route oracle: an
        unauthenticated `POST /approve` (404 or 403 depending on Origin) is
        distinguishable from `POST /whatever` (always 404) — but that only
        confirms this listener exists and speaks this console's protocol,
        which any port scan already reveals for free. It is NOT an approval
        oracle: no path/method combination here reaches `facade.approve()`
        without ALSO passing the Origin check and the constant-time token
        check that follow, in that order, below.
        """
        path, _query = self._split_path()
        if path not in ("/approve", "/reject"):
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._origin_ok():
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid Origin/Referer")
            return

        form = self._read_form()
        if form is None:
            return  # `_read_form` already sent the error response.

        token = _first(form, "token")
        if not self._token_ok(token):
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid token")
            return

        node_id = _first(form, "node_id").strip()
        if not node_id:
            self._send_plain(HTTPStatus.BAD_REQUEST, "node_id is required")
            return

        decision = "approved" if path == "/approve" else "rejected"
        self._decide(token=token, node_id=node_id, decision=decision)

    def _decide(self, *, token: str, node_id: str, decision: str) -> None:
        try:
            self.server.facade.approve(
                self.server.request_ctx, node_id=node_id, decision=decision,
            )
        except (GraphIntegrityError, GraphValidationError) as exc:
            self._send_plain(HTTPStatus.CONFLICT, f"could not record decision for {node_id!r} — {exc}")
            return

        # Record the decision, then ALWAYS redirect (never auto-stop from here — see
        # `ConsoleServer.maybe_auto_stop`). The browser's own follow-up GET is what
        # decides whether the console shuts down, only once that page has been served.
        self.server.record_decision()
        # `safe=""` on BOTH token and node_id (never the stdlib `quote` default of
        # `safe='/'`): a node_id containing a literal `/` must be percent-encoded
        # too, so it can never leave an unencoded slash sitting in the query string
        # of a `Location` header. No header-injection risk exists today (CRLF is
        # already percent-encoded by `quote`'s default safe set) — this is pure
        # defense in depth, not a fix for an exploitable bug.
        location = (
            f"/?token={quote(token, safe='')}"
            f"&resolved={quote(node_id, safe='')}"
            f"&decision={quote(decision)}"
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._send_hardening_headers()
        self.end_headers()

    # ── shared request parsing / validation ─────────────────────────────────────

    def _split_path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query, keep_blank_values=True)

    def _token_ok(self, provided: str) -> bool:
        # Constant-time comparison against bytes (never str-vs-str) so a mismatched
        # length can never raise, and so the comparison time never varies with WHICH
        # byte differs.
        return hmac.compare_digest(provided.encode("utf-8"), self.server.token.encode("utf-8"))

    def _origin_ok(self) -> bool:
        """CSRF defense in depth: require a same-origin Origin, or failing that Referer."""
        expected = f"http://{_LOOPBACK_HOST}:{self.server.server_address[1]}"
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin == expected
        referer = self.headers.get("Referer")
        if referer is not None:
            return referer == expected or referer.startswith(expected + "/")
        return False  # neither header present -> fail closed, never assume same-origin

    def _read_form(self) -> dict[str, list[str]] | None:
        """Read and parse a bounded `application/x-www-form-urlencoded` POST body.

        Sends its own error response and returns ``None`` on any failure — refuses
        chunked transfer-encoding outright (never attempts to decode it), an
        oversized or malformed Content-Length, a non-form content type, and
        undecodable bytes. Every branch fails closed to 400; none silently guesses.
        """
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_plain(HTTPStatus.BAD_REQUEST, "chunked transfer-encoding is not supported")
            return None

        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._send_plain(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            self._send_plain(HTTPStatus.BAD_REQUEST, "request body missing or too large")
            return None

        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != _FORM_CONTENT_TYPE:
            self._send_plain(HTTPStatus.BAD_REQUEST, "unsupported content type")
            return None

        raw_body = self.rfile.read(length)
        try:
            text = raw_body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self._send_plain(HTTPStatus.BAD_REQUEST, "request body is not valid UTF-8")
            return None
        return parse_qs(text, keep_blank_values=True, strict_parsing=False)

    # ── response helpers ────────────────────────────────────────────────────────

    def _send_hardening_headers(self) -> None:
        """Headers sent on EVERY response, success or error alike.

        CRIT finding (fixed, not just noted): the token lives in the URL query
        string (by this LLD's own explicit design — printed for the operator to
        open in a browser). The page today has zero external resources, so there
        is no CURRENT third-party leak — but that is a fragile, implicit
        guarantee: one future edit that adds so much as a favicon fetch would
        start leaking the token to that third party via `Referer`, with nothing
        here to stop it. `Referrer-Policy: no-referrer` closes that off
        explicitly rather than relying on "the page happens not to link out
        today." `X-Content-Type-Options: nosniff` is a free, standard hardening
        header with no functional cost for a console this small.

        `Cache-Control: no-store` + `Pragma: no-cache` (cross-audit FIX 3):
        the token-bearing URL/page must never be written to disk cache or kept
        in the browser's back/forward cache past this process's lifetime — a
        cached copy would keep the capability usable (or at least visible)
        after the console has already exited.
        """
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")

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


def _first(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key)
    return values[0] if values else ""


def _notice_from_query(query: dict[str, list[str]]) -> str | None:
    resolved = _first(query, "resolved")
    decision = _first(query, "decision")
    if not resolved or not decision:
        return None
    return f"node {resolved!r} decision: {decision}"
