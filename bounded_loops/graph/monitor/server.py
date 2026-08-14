"""The monitor's loopback server: static assets, a JSON API, and one SSE stream.

A transport with no behaviour of its own. Every POST goes to `api.handle`, every stream goes to
`live.sse_server.stream_projection_snapshots`, and the security posture comes from
`live.posture.LoopbackServer` / `LoopbackHandler` — the same one the approval console and
`bl graph watch` use. Three surfaces, one posture, because a second answer to "who may talk to
this port" is how one of them ends up weaker.

Two containment rules that matter more than anything else here:

1. **No request ever names a file.** Static assets resolve through a fixed allowlist, so
   `GET /../../etc/passwd` cannot become a path — there is no path construction to attack.
2. **No request ever names a directory.** A run is addressed by name and resolved through
   `Workspace.run_dir`, which validates it, so the SSE stream cannot be pointed outside the
   workspace.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from bounded_loops.domain.errors import ManifestError
from bounded_loops.graph.domain.errors import GraphError
from bounded_loops.graph.monitor import api
from bounded_loops.graph.live.posture import _LOOPBACK_HOST, LoopbackHandler, LoopbackServer
from bounded_loops.graph.live.sse_server import stream_projection_snapshots
from bounded_loops.workspace import discover

#: The only files this server will ever serve, and the content type for each. A URL path is
#: matched against these keys — it is never joined onto a directory, so there is no traversal to
#: defend against rather than a traversal check to get right.
_ASSETS: dict[str, str] = {
    "/app.js": "text/javascript; charset=utf-8",
    "/dag.js": "text/javascript; charset=utf-8",
    "/forms.js": "text/javascript; charset=utf-8",
    "/palette.js": "text/javascript; charset=utf-8",
    "/columns.js": "text/javascript; charset=utf-8",
    "/style.css": "text/css; charset=utf-8",
    "/vendor/react.production.min.js": "text/javascript; charset=utf-8",
    "/vendor/react-dom.production.min.js": "text/javascript; charset=utf-8",
    "/vendor/htm.module.js": "text/javascript; charset=utf-8",
}

_MAX_API_BODY_BYTES = 512 * 1024 + 8 * 1024  # a manifest, plus room for the rest of the envelope


def _asset_bytes(relative: str) -> bytes:
    """Read one packaged asset. `relative` comes from `_ASSETS`, never from a request."""
    # The allowlist one frame up is what makes this safe, and that is the problem with leaving
    # it implicit: this function will happily join `..` if a later edit ever calls it with a
    # computed name. Cheap to make the invariant enforce itself rather than depend on every
    # future caller remembering it.
    segments = relative.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"refusing to read a traversing asset path: {relative!r}")
    resource = files("bounded_loops.graph.monitor").joinpath("assets", *segments)
    return resource.read_bytes()


class MonitorServer(LoopbackServer):
    """Loopback server for one monitor session.

    Inherits the loopback bind, the per-invocation token, and the connection semaphore. Adds
    nothing but the workspace it is scoped to — which is resolved once, here, so no request can
    change which project's receipts are visible.
    """

    def __init__(self, *, port: int = 0) -> None:
        super().__init__(MonitorHandler, port=port)
        self.workspace = discover()

    @property
    def app_url(self) -> str:
        """The URL to open, token included. Printed once for the person who ran the command."""
        return f"http://{_LOOPBACK_HOST}:{self.server_address[1]}/?token={quote(self.token, safe='')}"


class MonitorHandler(LoopbackHandler):
    """GET for the page and its assets, POST for the API, GET /events for the live stream."""

    server: MonitorServer  # type: ignore[assignment]

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path, query = self._split_path()

        # Static assets are served WITHOUT a token, deliberately. A browser requests them from
        # relative URLs in the document (`href="style.css"`), which carry no query string, so
        # gating them 403'd the stylesheet and React itself and the page rendered as bare HTML —
        # found by loading it, not by reading it.
        #
        # This is a considered narrowing, not a weakening. The allowlisted files are the vendored
        # React build, this app's own script, and its stylesheet: inert, public, and containing
        # zero project data. The security boundary is the API and the event stream, which return
        # the contents of someone's workspace, and both still require the token AND a same-origin
        # header. Serving a copy of React to whoever guessed the port discloses nothing.
        if path in _ASSETS:
            self._send_asset(path)
            return

        if not self._token_ok(self._first(query, "token")):
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid token")
            return

        if path == "/":
            self._send_page()
        elif path == "/events":
            self._send_stream(self._first(query, "run"))
        else:
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")

    def _send_page(self) -> None:
        """Serve index.html with the token injected as a global the app reads once.

        The token is already in the URL the person opened, so putting it in the document adds no
        exposure — and it means the app never has to parse `location.search`, which is the one
        place a token tends to get logged or copied into a link by accident.
        """
        try:
            document = _asset_bytes("index.html").decode("utf-8")
        except (OSError, FileNotFoundError):
            self._send_plain(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "the monitor assets are missing from this installation",
            )
            return
        injected = document.replace(
            "</head>",
            f"<script>window.__BL_TOKEN__={json.dumps(self.server.token)};</script></head>",
            1,
        )
        self._send_html(HTTPStatus.OK, injected)

    def _send_asset(self, path: str) -> None:
        try:
            payload = _asset_bytes(path.lstrip("/"))
        except (OSError, FileNotFoundError):
            self._send_plain(HTTPStatus.NOT_FOUND, "asset not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _ASSETS[path])
        self.send_header("Content-Length", str(len(payload)))
        self._send_hardening_headers()
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass

    def _send_stream(self, run_name: str) -> None:
        """Tail one run's receipts as SSE. The run is named, never pathed.

        Requires a same-origin header as well as the token: the page this server served sends one
        automatically, and a hostile page in another tab cannot forge it.
        """
        if not self._origin_ok():
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid Origin/Referer")
            return
        if not run_name:
            self._send_plain(HTTPStatus.BAD_REQUEST, "the events stream needs a ?run=<name>")
            return

        from bounded_loops import mcp_authoring

        try:
            _workspace, run_dir = mcp_authoring._resolve_run(run_name)
            facade, payload = mcp_authoring._facade_and_payload(run_dir)
        except (ManifestError, GraphError, OSError, ValueError) as exc:
            # GraphError belongs here because a corrupt run is exactly the case a watcher hits.
            # Without it, GraphIntegrityError — raised when a run's controller-events.jsonl is
            # a symlink, say — escaped this handler: the browser got a closed socket with no
            # status at all and the operator got a traceback on the terminal running `bl
            # monitor`. A refusal the UI cannot render is indistinguishable from a crash.
            self._send_plain(HTTPStatus_NOT_FOUND_OR_BAD(exc), f"cannot watch that run — {exc}")
            return

        from bounded_loops.graph.application.arena_projection import ArenaReadRequest

        self._send_sse_headers()
        stream_projection_snapshots(
            event_log_path=run_dir / "controller-events.jsonl",
            facade=facade,
            request_ctx=ArenaReadRequest(
                subject_id=payload["subject_id"],
                organization_id=payload["organization_id"],
                project_id=payload["project_id"],
                run_id=payload["run_id"],
            ),
            send_data=self._send_sse_data,
            send_comment=self._send_sse_comment,
        )

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        """Every API call. Token AND same-origin are both required, on every route.

        The origin check applies to reads as well as writes here, unlike a classic CSRF rule that
        only guards mutations: these reads return the contents of someone's project, and a
        cross-origin page should not be able to enumerate it just because it guessed the port.
        """
        path, _query = self._split_path()
        if not path.startswith("/api/"):
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._origin_ok():
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid Origin/Referer")
            return

        body = self._read_json_body()
        if body is None:
            return
        if not self._token_ok(str(body.get("token", ""))):
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden — missing or invalid token")
            return

        route = path[len("/api/"):]
        payload = {key: value for key, value in body.items() if key != "token"}
        self._send_json(api.handle(route, payload))

    def _read_json_body(self) -> dict | None:
        """Read and parse the request body, or send an error and return None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self._send_plain(HTTPStatus.BAD_REQUEST, "a Content-Length is required")
            return None
        if length <= 0:
            self._send_plain(HTTPStatus.BAD_REQUEST, "an empty body is not a request")
            return None
        if length > _MAX_API_BODY_BYTES:
            self._send_plain(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            return None
        try:
            raw = self.rfile.read(length)
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_plain(HTTPStatus.BAD_REQUEST, "the body must be UTF-8 JSON")
            return None
        if not isinstance(parsed, dict):
            self._send_plain(HTTPStatus.BAD_REQUEST, "the body must be a JSON object")
            return None
        return parsed

    def _send_json(self, document: dict) -> None:
        payload = json.dumps(document).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_hardening_headers()
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass


def HTTPStatus_NOT_FOUND_OR_BAD(exc: Exception) -> HTTPStatus:  # noqa: N802
    """404 for a run that is not there, 400 for a name that could never be one.

    Distinguished because they mean different things to whoever is looking: one is "you picked a
    run that has been deleted", the other is "that is not a run id at all".
    """
    return (
        HTTPStatus.BAD_REQUEST
        if "run_id must be" in str(exc)
        else HTTPStatus.NOT_FOUND
    )


def assets_dir() -> Path:
    """The packaged assets directory, for the tests that assert what ships."""
    return Path(str(files("bounded_loops.graph.monitor").joinpath("assets")))
