"""WatchServer — live SSE event stream for `bl graph watch` (U3).

Security model (inherits LOCAL posture from ``live/posture.py``):

* Binds ``127.0.0.1`` only.  No host parameter anywhere in this module.
* Per-invocation token, constant-time comparison, gates BOTH the page
  endpoint (``GET /``) and the stream endpoint (``GET /events``).
* ``GET /events`` additionally requires a same-origin ``Origin`` header —
  CSRF defense in depth.  A browser tab on a hostile page would send the
  hostile page's Origin, which is refused.  A correctly connected browser
  tab sends the server's own origin.
* Path validation: the run directory path is resolved and locked at server
  construction.  The only file this server ever opens is
  ``controller-events.jsonl`` inside that directory.  No request parameter
  ever names a file path; the only routes are ``GET /`` and ``GET /events``.
* Connection semaphore: ``_MAX_CONCURRENT_CONNECTIONS`` (from posture) caps
  in-flight threads — inherited from ``LoopbackServer``.

Dual-mode Arena template:
  Served page: the Arena HTML with a ``<script>window.__BL_STREAM_URL__
  = …</script>`` injected, so the in-page JS connects to SSE.
  Static page: the Arena HTML as produced by ``bl graph arena`` — no
  ``window.__BL_STREAM_URL__``, no ``EventSource``.  A saved ``arena.html``
  opened from disk years later renders exactly as it does today.

Spend/budget panel:
  The SSE payload is a full ``ArenaProjection`` snapshot, so the page has
  ``spend_tokens``, ``spend_cost_microunits``, ``spend_complete`` on the run
  and on each node.  The JS reads them from the same data block as the rest.
  We show what the log has; we invent nothing.
"""

from __future__ import annotations

import dataclasses
import json
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bounded_loops.graph.application.arena_projection import (
    ArenaProjection,
    ArenaReadRequest,
)
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.arena.render import render_arena_html
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.graph_runtime_facade import LocalGraphRuntimeFacade
from bounded_loops.graph.live.posture import (
    LoopbackHandler,
    LoopbackServer,
    _LOOPBACK_HOST,
)

# SSE poll interval when no new events have arrived (seconds).
_POLL_INTERVAL_S: float = 0.25
# How long to keep a stream alive after the run reaches a terminal state.
_TERMINAL_LINGER_S: float = 5.0
# Maximum events to push before considering the stream stale.
_MAX_EVENTS: int = 50_000
# SSE keepalive comment interval (seconds).
_KEEPALIVE_INTERVAL_S: float = 15.0


class WatchOpenError(Exception):
    """Raised when ``--run`` cannot be opened as a safe, valid run directory."""


def open_watch_run(
    run_dir: Path,
) -> tuple[GraphRunIdentity, LocalGraphRuntimeFacade]:
    """Validate *run_dir* and build its identity + facade, before any socket binds.

    Uses the same ``LocalGraphRuntimeFacade.for_run_dir`` and
    ``load_plan_from_run_dir`` calls that ``bl graph approve`` and
    ``bl graph console`` already trust — no new traversal logic here.
    """
    try:
        facade = LocalGraphRuntimeFacade.for_run_dir(run_dir)
    except (GraphIntegrityError, GraphValidationError) as exc:
        raise WatchOpenError(str(exc)) from exc
    try:
        _plan, identity, _meta = load_plan_from_run_dir(run_dir.resolve())
    except (FileNotFoundError, ValueError, GraphValidationError) as exc:
        raise WatchOpenError(str(exc)) from exc
    return identity, facade


def _projection_json(facade: LocalGraphRuntimeFacade, request_ctx: ArenaReadRequest) -> str:
    """Serialize the current ArenaProjection to a compact JSON string.

    Returns an empty string if the projection cannot be read (e.g. the run
    directory has been removed), so the caller can skip sending and try again.
    """
    try:
        projection: ArenaProjection = facade.status(request_ctx)
    except (GraphIntegrityError, GraphValidationError, OSError):
        return ""
    data: Any = dataclasses.asdict(projection)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _escape_js_string(s: str) -> str:
    """Escape ``s`` for safe embedding inside a JS string literal in a <script> tag.

    Only escapes characters that can break out of a string or a <script> block;
    JSON-encodes the whole value so no quoting ambiguity remains.
    """
    return (
        json.dumps(s)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


class WatchServer(LoopbackServer):
    """Short-lived loopback SSE server for one run directory.

    Inherits loopback bind, per-invocation token, and connection semaphore
    from ``LoopbackServer`` (``live/posture.py``).  Adds only watch-specific
    state: a locked run directory, an identity, and a facade reference.

    The run directory is resolved and stored once.  Neither the ``GET /``
    nor the ``GET /events`` handler ever opens any file named in the request
    URL — the only file path used is the fixed, pre-validated event log path.
    """

    def __init__(
        self,
        *,
        identity: GraphRunIdentity,
        facade: LocalGraphRuntimeFacade,
        run_dir: Path,
        port: int = 0,
    ) -> None:
        super().__init__(WatchRequestHandler, port=port)
        self.identity = identity
        self.facade = facade
        # Resolve once at construction; never re-derive from request parameters.
        self.run_dir: Path = run_dir.resolve()
        self.request_ctx = ArenaReadRequest(
            subject_id=identity.organization_id,
            organization_id=identity.organization_id,
            project_id=identity.project_id,
            run_id=identity.run_id,
        )
        # The event log path is derived from run_dir here — not from any request.
        self._event_log_path: Path = self.run_dir / "controller-events.jsonl"

    @property
    def watch_url(self) -> str:
        """The Arena page URL — including the token — printed for the operator."""
        port = self.server_address[1]
        return f"http://{_LOOPBACK_HOST}:{port}/?token={self.token}"

    @property
    def events_url(self) -> str:
        """The SSE stream URL — token included — injected into the served Arena page."""
        port = self.server_address[1]
        return f"http://{_LOOPBACK_HOST}:{port}/events?token={quote(self.token, safe='')}"


class WatchRequestHandler(LoopbackHandler):
    """Two-route handler: ``GET /`` (live Arena page) and ``GET /events`` (SSE stream).

    Both routes require the per-invocation token.  The stream additionally
    requires a same-origin ``Origin`` header, which a browser sends automatically
    when the Arena page (served by this same server) opens the ``EventSource``.

    A hostile page cannot satisfy the ``Origin`` check because it carries its
    own origin, not ``http://127.0.0.1:<port>``.
    """

    server: WatchServer  # type: ignore[assignment]

    def do_GET(self) -> None:
        path, query = self._split_path()
        token = self._first(query, "token")
        if not self._token_ok(token):
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid token")
            return
        if path == "/":
            self._render_arena(token)
        elif path == "/events":
            self._stream_events()
        else:
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")

    def _render_arena(self, token: str) -> None:
        """Serve the Arena HTML with the SSE stream URL injected."""
        try:
            projection: ArenaProjection = self.server.facade.status(self.server.request_ctx)
        except (GraphIntegrityError, GraphValidationError) as exc:
            self._send_plain(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"cannot read run status — {exc}",
            )
            return
        html = render_arena_html(projection)
        # Inject the stream URL and the EventSource connector just before </head>.
        # EventSource lives here ONLY — never in the static template — so that a
        # saved arena.html opened from disk has no network dependency.
        stream_url = self.server.events_url
        escaped_url = _escape_js_string(stream_url)
        # The connector:
        #  1. Sets window.__BL_STREAM_URL__ so the page can introspect the URL.
        #  2. Shows the live indicator badge.
        #  3. Opens an EventSource to /events and calls window.refresh(data) on each
        #     message — the function is defined in the template but never called
        #     when there is no EventSource (i.e., when the page is opened from disk).
        #  4. Closes the EventSource on error (server closed, tab navigated away).
        injection = (
            "<script>"
            f"window.__BL_STREAM_URL__={escaped_url};"
            "(function(){"
            "var li=document.getElementById('live-indicator');"
            "if(li){li.style.display='flex';}"
            "var ld=document.getElementById('live-div');"
            "if(ld){ld.style.display='';}"
            "var es=new EventSource(window.__BL_STREAM_URL__);"
            "es.onmessage=function(ev){"
            "try{var d=JSON.parse(ev.data);"
            "if(typeof window.refresh==='function')window.refresh(d);"
            "}catch(e){}"
            "};"
            "es.onerror=function(){es.close();"
            "var li2=document.getElementById('live-indicator');"
            "if(li2)li2.style.display='none';"
            "};"
            "})();"
            "</script>"
        )
        html = html.replace("</head>", injection + "\n</head>", 1)
        self._send_html(HTTPStatus.OK, html)

    def _stream_events(self) -> None:
        """Tail ``controller-events.jsonl`` and push ArenaProjection snapshots as SSE.

        Origin check: required before streaming.  A browser tab that the Arena page
        opened sends ``Origin: http://127.0.0.1:<port>``, which matches.  A tab on
        any other page sends a different origin and is refused.
        """
        if not self._origin_ok():
            self._send_plain(
                HTTPStatus.FORBIDDEN,
                "forbidden — missing or invalid Origin/Referer",
            )
            return
        self._run_sse_loop()

    def _run_sse_loop(self) -> None:
        """Open the SSE connection and stream projection snapshots until done.

        Sends one snapshot immediately (before any new events arrive) so the
        page does not wait for the next poll tick to show its first live state.
        """
        # SSE response headers.  ``Cache-Control: no-store`` still applies (hardening);
        # ``X-Accel-Buffering: no`` tells any intervening nginx/proxy to not buffer the stream.
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # HTTP/1.0: no Content-Length — we stream until the connection drops.
        self.end_headers()

        event_log_path = self.server._event_log_path
        last_size = 0
        last_keepalive = time.monotonic()
        events_sent = 0
        facade = self.server.facade
        request_ctx = self.server.request_ctx

        # Send the initial snapshot immediately.
        payload = _projection_json(facade, request_ctx)
        if payload:
            self._send_sse_data(payload)
            events_sent += 1

        while events_sent < _MAX_EVENTS:
            try:
                current_size = event_log_path.stat().st_size if event_log_path.exists() else 0
            except OSError:
                break  # run directory removed

            if current_size > last_size:
                last_size = current_size
                new_payload = _projection_json(facade, request_ctx)
                if new_payload:
                    if not self._send_sse_data(new_payload):
                        break  # client disconnected
                    events_sent += 1
                    last_keepalive = time.monotonic()
                    # Check if run is terminal — linger briefly then close.
                    try:
                        proj_state = facade.status(request_ctx).run_state
                    except (GraphIntegrityError, GraphValidationError, OSError):
                        break
                    if proj_state in ("SUCCEEDED", "FAILED"):
                        time.sleep(_TERMINAL_LINGER_S)
                        break
            else:
                # No new events — send a keepalive comment if needed.
                now = time.monotonic()
                if now - last_keepalive >= _KEEPALIVE_INTERVAL_S:
                    if not self._send_sse_comment("keepalive"):
                        break
                    last_keepalive = now
                time.sleep(_POLL_INTERVAL_S)

    def _send_sse_data(self, json_payload: str) -> bool:
        """Write one SSE event.  Returns False if the connection is broken."""
        try:
            self.wfile.write(f"data: {json_payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _send_sse_comment(self, text: str) -> bool:
        """Write an SSE keepalive comment.  Returns False if the connection is broken."""
        try:
            self.wfile.write(f": {text}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except OSError:
            return False
