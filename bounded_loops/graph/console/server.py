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

import threading
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote

from bounded_loops.graph.application.arena_projection import ArenaProjection, ArenaReadRequest
from bounded_loops.graph.graph_run_report import _awaiting_approval_nodes
from bounded_loops.graph.graph_runtime_facade import LocalGraphRuntimeFacade
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests
from bounded_loops.graph.console.rendering import render_console_page
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.live.posture import (
    LoopbackHandler,
    LoopbackServer,
    _LOOPBACK_HOST,
    _MAX_BODY_BYTES,
    _FORM_CONTENT_TYPE,
)


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
        _plan, identity, _meta = load_plan_from_run_dir(
            run_dir.resolve(), package_digests=admitted_loop_package_digests(),
        )
    except (FileNotFoundError, ValueError, GraphValidationError) as exc:
        raise ConsoleOpenError(str(exc)) from exc
    return identity, facade


class ConsoleServer(LoopbackServer):
    """A short-lived, loopback-only HTTP server scoped to exactly one run directory.

    Inherits the loopback bind, per-invocation token, and connection semaphore
    from ``LoopbackServer`` (``live/posture.py``).  This class adds only the
    console-specific domain state: ``identity``, ``facade``, ``decisions_made``.

    There is deliberately NO ``host`` parameter — inherited from
    ``LoopbackServer``, which binds ``_LOOPBACK_HOST`` literally.
    """

    def __init__(
        self,
        *,
        identity: GraphRunIdentity,
        facade: LocalGraphRuntimeFacade,
        port: int = 0,
    ) -> None:
        super().__init__(ConsoleRequestHandler, port=port)
        self.identity = identity
        self.facade = facade
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


class ConsoleRequestHandler(LoopbackHandler):
    """Fixed-route handler: `GET /`, `POST /approve`, `POST /reject` — nothing else.

    Inherits the loopback security posture (token check, origin check, hardening
    headers, strict method allowlist, logging suppression) from ``LoopbackHandler``
    (``live/posture.py``).  This class adds only the console-specific routes and
    the form-body reader.

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
        if path not in ("/approve", "/reject", "/continue"):
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

        if path == "/continue":
            # Same guard order as a decision — route, Origin, bounded body, constant-time
            # token — before anything reaches the facade. Continuing a run spends money, so it
            # gets exactly the protection approving one does, no more and no less.
            self._continue_with_new_ceiling(token=token, form=form)
            return

        node_id = _first(form, "node_id").strip()
        if not node_id:
            self._send_plain(HTTPStatus.BAD_REQUEST, "node_id is required")
            return

        decision = "approved" if path == "/approve" else "rejected"
        self._decide(token=token, node_id=node_id, decision=decision)

    def _continue_with_new_ceiling(self, *, token: str, form: dict[str, list[str]]) -> None:
        """Continue a budget-paused run under a ceiling the operator just typed.

        The number is never persisted as an authorisation. It applies to THIS continuation
        only, exactly like the CLI flag — so there is no stored grant in the log that could be
        replayed to buy the same spend twice.
        """
        from bounded_loops.graph.application.budget_config import usd_to_microunits
        from bounded_loops.graph.application.node_spend import RunBudget

        raw_tokens = _first(form, "max_tokens").strip()
        raw_cost = _first(form, "max_cost_usd").strip()
        if not raw_tokens and not raw_cost:
            self._send_plain(
                HTTPStatus.BAD_REQUEST,
                "give a new token ceiling or a new cost ceiling in USD",
            )
            return
        try:
            budget = RunBudget(
                max_tokens=int(raw_tokens) if raw_tokens else None,
                max_cost_microunits=usd_to_microunits(raw_cost) if raw_cost else None,
            )
        except (ValueError, GraphIntegrityError) as exc:
            self._send_plain(HTTPStatus.BAD_REQUEST, f"that is not a usable limit — {exc}")
            return

        try:
            self.server.facade.resume(self.server.request_ctx, run_budget=budget)
        except (GraphIntegrityError, GraphValidationError) as exc:
            self._send_plain(HTTPStatus.CONFLICT, f"could not continue the run — {exc}")
            return
        # Deliberately NOT record_decision(): that counter drives the console's auto-stop for
        # an approval queue that has been worked through. Continuing a run under a new ceiling
        # is not a decision on a node, and it may well pause again — shutting the console down
        # here would take away the surface the operator needs next.
        location = f"/?token={quote(token, safe='')}&continued=1"
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._send_hardening_headers()
        self.end_headers()

    def _decide(self, *, token: str, node_id: str, decision: str) -> None:
        try:
            # No ceiling is typed on an approve, so the controller carries the pause's own
            # ceilings forward (effective_run_budget) — which is why approving a run that paused
            # on budget is not refused here. Adding a second number to this form would ask the
            # operator to re-authorise spend they already authorised.
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

    # ── request parsing / validation ─────────────────────────────────────────────
    # _split_path, _token_ok, _origin_ok are inherited from LoopbackHandler.

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

    # _send_hardening_headers, _send_plain, _send_html are inherited from LoopbackHandler.


def _first(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key)
    return values[0] if values else ""


def _notice_from_query(query: dict[str, list[str]]) -> str | None:
    resolved = _first(query, "resolved")
    decision = _first(query, "decision")
    if not resolved or not decision:
        return None
    return f"node {resolved!r} decision: {decision}"
