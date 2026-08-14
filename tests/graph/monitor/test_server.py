"""The monitor's loopback server, driven over real HTTP.

Half these tests are attacks. A local UI server is still a network service, and the failure that
matters is not "a button does not work" — it is a page on another origin, or a crafted URL,
reaching into someone's project.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest

from bounded_loops.graph.monitor import api
from bounded_loops.graph.monitor.server import _ASSETS, MonitorServer

REPO_ROOT_GRAPHS = Path(__file__).resolve().parents[3] / "graphs"


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MonitorServer]:
    """A running server scoped to an empty temporary workspace."""
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))
    server = MonitorServer(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _base(server: MonitorServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _get(server: MonitorServer, path: str, *, token: str | None = None,
         origin: str | None = None) -> int:
    tok = server.token if token is None else token
    joiner = "&" if "?" in path else "?"
    request = urllib.request.Request(f"{_base(server)}{path}{joiner}token={tok}")
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _post(server: MonitorServer, route: str, payload: dict[str, Any] | None = None, *,
          token: str | None = None, origin: str | None = "self",
          method: str = "POST") -> tuple[int, Any]:
    tok = server.token if token is None else token
    body = json.dumps({"token": tok, **(payload or {})}).encode("utf-8")
    request = urllib.request.Request(
        f"{_base(server)}/api/{route}", data=body, method=method,
    )
    request.add_header("Content-Type", "application/json")
    if origin is not None:
        request.add_header("Origin", _base(server) if origin == "self" else origin)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


# ── it binds loopback only ───────────────────────────────────────────────────


def test_the_server_binds_only_the_loopback_interface(served: MonitorServer) -> None:
    assert served.server_address[0] == "127.0.0.1"
    assert _base(served).startswith("http://127.0.0.1:")


def test_the_url_carries_a_fresh_high_entropy_token_each_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "p"))
    first, second = MonitorServer(port=0), MonitorServer(port=0)
    try:
        assert first.token != second.token
        assert len(first.token) >= 40
        assert f"token={first.token}" in first.app_url or "token=" in first.app_url
    finally:
        first.server_close()
        second.server_close()


# ── the happy path ───────────────────────────────────────────────────────────


def test_the_page_and_every_listed_asset_are_served(served: MonitorServer) -> None:
    assert _get(served, "/") == 200
    for path in _ASSETS:
        assert _get(served, path) == 200, path


def test_the_page_carries_the_token_so_the_app_never_parses_the_url(
    served: MonitorServer,
) -> None:
    request = urllib.request.Request(f"{_base(served)}/?token={served.token}")
    with urllib.request.urlopen(request, timeout=5) as response:
        document = response.read().decode("utf-8")
    assert "__BL_TOKEN__" in document
    assert json.dumps(served.token) in document


@pytest.mark.parametrize("route", ["workspace", "forms", "capabilities", "runs"])
def test_the_read_routes_answer_ok(served: MonitorServer, route: str) -> None:
    status, body = _post(served, route)
    assert status == 200
    assert body["ok"] is True


def test_every_api_route_is_reachable_over_http(served: MonitorServer) -> None:
    """A route in the dispatch table that the server cannot reach is a dead feature."""
    for route in api.routes():
        status, body = _post(served, route, {})
        assert status == 200, route
        # Some routes legitimately refuse an empty payload; none may 404 or crash.
        assert isinstance(body, dict) and "ok" in body, route


# ── what it must refuse ──────────────────────────────────────────────────────


@pytest.mark.parametrize("token", ["", "deadbeef", "x" * 43])
def test_a_missing_or_wrong_token_is_refused_on_the_page(
    served: MonitorServer, token: str,
) -> None:
    assert _get(served, "/", token=token) == 403


@pytest.mark.parametrize(
    "path",
    [
        "/../../../etc/passwd",
        "/vendor/../../../etc/passwd",
        "/vendor/../server.py",
        "/server.py",
        "/api.py",
        "/assets/index.html",
        "/index.html",
    ],
)
def test_no_request_can_name_a_file_outside_the_allowlist(
    served: MonitorServer, path: str,
) -> None:
    """Assets resolve through a fixed allowlist, so there is no path construction to attack."""
    assert _get(served, path) == 404


def test_a_cross_origin_page_cannot_call_the_api_even_with_the_token(
    served: MonitorServer,
) -> None:
    """The token can leak — into a shell history, a screenshot, a shared URL. Origin is the
    second lock, and it is the one a hostile page in another tab cannot pick."""
    status, _body = _post(served, "workspace", origin="http://evil.example")
    assert status == 403


def test_the_api_refuses_a_request_with_NO_origin_at_all(served: MonitorServer) -> None:
    status, _body = _post(served, "workspace", origin=None)
    assert status == 403


def test_the_api_refuses_a_bad_token_even_from_the_right_origin(served: MonitorServer) -> None:
    status, _body = _post(served, "workspace", token="nope")
    assert status == 403


def test_an_unknown_route_is_an_error_document_not_a_crash(served: MonitorServer) -> None:
    status, body = _post(served, "no-such-route")
    assert status == 200
    assert body["ok"] is False
    assert "no such route" in body["error"]


def test_a_body_that_is_not_a_json_object_is_refused(served: MonitorServer) -> None:
    request = urllib.request.Request(
        f"{_base(served)}/api/workspace", data=b"[1,2,3]", method="POST",
    )
    request.add_header("Origin", _base(served))
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 400


def test_an_oversized_body_is_refused_before_it_is_read(served: MonitorServer) -> None:
    request = urllib.request.Request(
        f"{_base(served)}/api/lint", data=b"{}", method="POST",
    )
    request.add_header("Origin", _base(served))
    request.add_header("Content-Length", "999999999")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 413


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_a_writing_http_method_is_refused(served: MonitorServer, method: str) -> None:
    status, _body = _post(served, "workspace", method=method)
    assert status == 405


# ── the event stream ─────────────────────────────────────────────────────────


def test_the_stream_needs_a_run_name(served: MonitorServer) -> None:
    assert _get(served, "/events", origin=_base(served)) == 400


@pytest.mark.parametrize("hostile", ["../../etc", "..", "/etc/passwd", "a/../b"])
def test_the_stream_cannot_be_pointed_outside_the_workspace(
    served: MonitorServer, hostile: str,
) -> None:
    """A run is addressed by name through the one run-id validator, never by path."""
    assert _get(served, f"/events?run={hostile}", origin=_base(served)) == 400


def test_the_stream_refuses_a_cross_origin_reader(served: MonitorServer) -> None:
    assert _get(served, "/events?run=whatever", origin="http://evil.example") == 403


def test_the_stream_refuses_a_missing_token_before_looking_at_the_run(
    served: MonitorServer,
) -> None:
    assert _get(served, "/events?run=whatever", token="", origin=_base(served)) == 403


# ── exactly how far the unauthenticated surface goes ─────────────────────────


@pytest.mark.parametrize("path", sorted(_ASSETS))
def test_a_static_asset_is_served_WITHOUT_a_token(served: MonitorServer, path: str) -> None:
    """Deliberate, and the boundary of the narrowing.

    A browser fetches these from relative URLs in the document, which carry no query string.
    Gating them 403'd the stylesheet and React itself, and the page rendered as bare HTML — found
    by loading it in a browser, which no unit test had done.

    What is exposed: the vendored React build, this app's script, its stylesheet. Inert, public,
    zero project data.
    """
    assert _get(served, path, token="") == 200


def test_the_page_itself_STILL_requires_a_token(served: MonitorServer) -> None:
    """The shell is not an asset. Serving it unauthenticated would invite an unaware click."""
    assert _get(served, "/", token="") == 403


def test_no_route_carrying_PROJECT_DATA_was_opened_up(served: MonitorServer) -> None:
    """The narrowing must not have leaked past the asset allowlist.

    Every route that can return the contents of someone's workspace — the API and the event
    stream — must still refuse a request with no token.
    """
    for route in api.routes():
        status, _body = _post(served, route, token="")
        assert status == 403, f"/api/{route} answered without a token"
    assert _get(served, "/events?run=whatever", token="", origin=_base(served)) == 403


def test_an_asset_response_still_carries_the_hardening_headers(served: MonitorServer) -> None:
    """Unauthenticated does not mean unhardened."""
    request = urllib.request.Request(f"{_base(served)}/style.css")
    with urllib.request.urlopen(request, timeout=5) as response:
        headers = {key.lower(): value for key, value in response.getheaders()}
    assert headers["content-type"].startswith("text/css")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


def test_the_plan_route_returns_EDGES_so_a_dag_can_be_drawn(served: MonitorServer) -> None:
    """A node list with no edges is a bag of boxes. The UI found this by trying to draw one."""
    manifest = (
        REPO_ROOT_GRAPHS / "customer-data-request" / "graph.yaml"
    ).read_text(encoding="utf-8")

    status, body = _post(served, "plan", {"manifest": manifest})

    assert status == 200 and body["ok"] is True
    assert len(body["edges"]) >= 5
    assert {"from_node", "from_port", "to_node", "to_port", "when"} <= set(body["edges"][0])
    assert any(edge["when"] for edge in body["edges"]), "no edge guard survived the projection"
