"""RED-first tests for the `bl graph console` HTTP surface (Slice 3).

Drives a REAL `ConsoleServer` on an OS-assigned loopback port with an in-process
HTTP client (`http.client.HTTPConnection`) — never a subprocess, never a browser.
Every mutation goes through `LocalGraphRuntimeFacade.approve()` (Slice 1's durable
machinery, unchanged); these tests assert the HTTP surface wraps it correctly and
refuses everything the LLD calls out: non-loopback binds, missing/wrong tokens,
disallowed methods, unknown/traversal paths, missing CSRF Origin, and symlinked
run directories.
"""

from __future__ import annotations

import http.client
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from bounded_loops.graph.application.execute_graph import execute_graph_run
from bounded_loops.graph.console.server import (
    ConsoleOpenError,
    ConsoleServer,
    open_console_run,
)

# ── fixtures: build a paused run directly via execute_graph_run (no CLI, no subprocess) ──

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: console-one-gate
version: "1.0.0"
nodes:
  - id: checkpoint
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""

_TWO_GATE_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: console-two-gate
version: "1.0.0"
nodes:
  - id: gate1
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
  - id: gate2
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def _paused_run(
    tmp_path: Path, *, name: str = "run", manifest: str = _APPROVAL_MANIFEST, run_id: str = "run-1",
) -> Path:
    out_dir = tmp_path / name
    rc = execute_graph_run(
        manifest_text=manifest, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out_dir, run_id=run_id,
    )
    assert rc == 3, "fixture manifest must pause at its approval node"
    return out_dir


def _start(run_dir: Path) -> ConsoleServer:
    identity, facade = open_console_run(run_dir)
    server = ConsoleServer(identity=identity, facade=facade, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._test_thread = thread  # type: ignore[attr-defined]
    return server


def _stop(server: ConsoleServer) -> None:
    server.shutdown()
    server.server_close()
    thread = getattr(server, "_test_thread", None)
    if thread is not None:
        thread.join(timeout=5)


@pytest.fixture()
def console(tmp_path: Path):
    run_dir = _paused_run(tmp_path)
    server = _start(run_dir)
    try:
        yield server, run_dir
    finally:
        _stop(server)


def _origin(server: ConsoleServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _get(server: ConsoleServer, target: str, *, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request("GET", target, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _post(server: ConsoleServer, path: str, fields: dict[str, str], *, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        body = urlencode(fields).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
        hdrs.update(headers or {})
        conn.request("POST", path, body=body, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        return resp.status, dict(resp.getheaders()), text
    finally:
        conn.close()


def _method(server: ConsoleServer, verb: str, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request(verb, path)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


# ── 1: loopback-only bind ─────────────────────────────────────────────────────

def test_server_binds_loopback_only_never_0_0_0_0(console) -> None:
    server, _run_dir = console
    assert server.server_address[0] == "127.0.0.1"
    assert server.server_address[1] > 0, "port=0 must resolve to a real OS-assigned port"


def test_console_server_constructor_has_no_host_parameter() -> None:
    """There must be no way to ask the console to bind anywhere but loopback."""
    import inspect

    params = inspect.signature(ConsoleServer.__init__).parameters
    assert "host" not in params, "a host= parameter would let a caller bind off-loopback"


# ── 2/3: token enforcement on GET ─────────────────────────────────────────────

def test_get_root_without_token_is_forbidden(console) -> None:
    server, _ = console
    status, _headers, _body = _get(server, "/")
    assert status == 403


def test_get_root_with_wrong_token_is_forbidden(console) -> None:
    server, _ = console
    status, _headers, _body = _get(server, "/?token=not-the-real-token")
    assert status == 403


def test_get_root_with_correct_token_renders_page(console) -> None:
    server, _ = console
    status, headers, body = _get(server, f"/?token={server.token}")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert "checkpoint" in body
    assert "Approve" in body
    assert "Reject" in body


def test_every_response_carries_hardening_headers(console) -> None:
    """CRIT fix: Referrer-Policy defends the token-in-URL design even if a future
    edit adds an external resource to the page; asserted on both a success (200)
    and an error (403) response, since `_send_hardening_headers` runs on every
    reply path."""
    server, _ = console
    ok_status, ok_headers, _ = _get(server, f"/?token={server.token}")
    assert ok_status == 200
    assert ok_headers.get("Referrer-Policy") == "no-referrer"
    assert ok_headers.get("X-Content-Type-Options") == "nosniff"

    err_status, err_headers, _ = _get(server, "/?token=wrong")
    assert err_status == 403
    assert err_headers.get("Referrer-Policy") == "no-referrer"


# ── 4: unknown / traversal paths never leak, always 404 ──────────────────────

@pytest.mark.parametrize("path", ["/nope", "/../../etc/passwd", "/approve/../../x", "/%2e%2e/etc/passwd"])
def test_get_unknown_or_traversal_paths_are_not_found(console, path: str) -> None:
    server, _ = console
    status, _headers, _body = _get(server, f"{path}?token={server.token}")
    assert status == 404


# ── 5: strict method allowlist — anything but GET/POST is 405 ───────────────

@pytest.mark.parametrize("verb", ["PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"])
def test_disallowed_http_methods_return_405(console, verb: str) -> None:
    server, _ = console
    status = _method(server, verb, "/")
    assert status == 405


# ── 6/7: CSRF — POST without a valid Origin/Referer is refused ───────────────

def test_post_approve_without_origin_or_referer_is_forbidden(console) -> None:
    server, run_dir = console
    status, _headers, _body = _post(
        server, "/approve", {"token": server.token, "node_id": "checkpoint"},
    )
    assert status == 403
    # The refused POST must never have reached facade.approve() — node is still paused.
    _status, _headers2, body = _get(server, f"/?token={server.token}")
    assert "checkpoint" in body


def test_post_approve_with_wrong_origin_is_forbidden(console) -> None:
    server, _run_dir = console
    status, _headers, _body = _post(
        server, "/approve", {"token": server.token, "node_id": "checkpoint"},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403


def test_post_approve_with_correct_referer_and_no_origin_is_accepted(console) -> None:
    server, _run_dir = console
    origin = _origin(server)
    status, headers, _body = _post(
        server, "/approve", {"token": server.token, "node_id": "checkpoint"},
        headers={"Referer": origin + "/?token=" + server.token},
    )
    assert status == 303
    assert headers["Location"].startswith("/?token=")


# ── 8: happy path — POST approve drives facade.approve() and resolves ────────

def test_post_approve_with_correct_origin_and_token_resolves_the_run(console) -> None:
    server, _run_dir = console
    origin = _origin(server)
    status, headers, _body = _post(
        server, "/approve", {"token": server.token, "node_id": "checkpoint"},
        headers={"Origin": origin},
    )
    assert status == 303
    location = headers["Location"]
    assert location.startswith("/?token=")

    # A POST never auto-stops the server itself (that would race its own redirect —
    # see ConsoleServer.maybe_auto_stop); the flag flips only once the follow-up GET
    # has fully rendered the resolved page.
    assert server.resolved_and_idle is False

    get_status, _headers2, body = _get(server, location)
    assert get_status == 200
    assert "Nothing is currently awaiting approval" in body
    assert "SUCCEEDED" in body

    # The single-gate manifest has nothing left pending — the server marks itself
    # resolved-and-idle so it does not linger forever after the only decision is made.
    assert server.resolved_and_idle is True


def test_post_reject_fails_the_run_closed(console) -> None:
    server, _run_dir = console
    origin = _origin(server)
    status, headers, _body = _post(
        server, "/reject", {"token": server.token, "node_id": "checkpoint"},
        headers={"Origin": origin},
    )
    assert status == 303

    get_status, _headers2, body = _get(server, headers["Location"])
    assert get_status == 200
    assert "FAILED" in body
    assert server.resolved_and_idle is True


# ── 9: input validation on the POST body ──────────────────────────────────────

def test_post_approve_missing_token_field_is_forbidden(console) -> None:
    server, _run_dir = console
    status, _headers, _body = _post(
        server, "/approve", {"node_id": "checkpoint"},
        headers={"Origin": _origin(server)},
    )
    assert status == 403


def test_post_approve_missing_node_id_is_bad_request(console) -> None:
    server, _run_dir = console
    status, _headers, _body = _post(
        server, "/approve", {"token": server.token},
        headers={"Origin": _origin(server)},
    )
    assert status == 400


def test_post_approve_blank_node_id_is_bad_request(console) -> None:
    server, _run_dir = console
    status, _headers, _body = _post(
        server, "/approve", {"token": server.token, "node_id": "   "},
        headers={"Origin": _origin(server)},
    )
    assert status == 400


def test_post_with_wrong_content_type_is_bad_request(console) -> None:
    server, _run_dir = console
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        raw = b'{"token": "x", "node_id": "checkpoint"}'
        conn.request(
            "POST", "/approve", body=raw,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "Origin": _origin(server),
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400
    finally:
        conn.close()


def test_post_with_oversized_body_is_bad_request(console) -> None:
    server, _run_dir = console
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        huge_node_id = "x" * (64 * 1024)
        raw = urlencode({"token": server.token, "node_id": huge_node_id}).encode("utf-8")
        conn.request(
            "POST", "/approve", body=raw,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(raw)),
                "Origin": _origin(server),
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400
    finally:
        conn.close()


def test_post_with_chunked_transfer_encoding_is_bad_request(console) -> None:
    """Never attempt to decode a chunked body — refuse it outright (400)."""
    server, _run_dir = console
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        raw = urlencode({"token": server.token, "node_id": "checkpoint"}).encode("utf-8")
        conn.request(
            "POST", "/approve", body=raw,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Transfer-Encoding": "chunked",
                "Origin": _origin(server),
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400
    finally:
        conn.close()


def test_post_with_malformed_content_length_is_bad_request(console) -> None:
    server, _run_dir = console
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        raw = urlencode({"token": server.token, "node_id": "checkpoint"}).encode("utf-8")
        conn.request(
            "POST", "/approve", body=raw,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": "not-a-number",
                "Origin": _origin(server),
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400
    finally:
        conn.close()


def test_post_with_invalid_utf8_body_is_bad_request(console) -> None:
    server, _run_dir = console
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        raw = b"token=abc&node_id=" + b"\xff\xfe"
        conn.request(
            "POST", "/approve", body=raw,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(raw)),
                "Origin": _origin(server),
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400
    finally:
        conn.close()


# ── 10: unknown POST path is not found ────────────────────────────────────────

def test_post_unknown_path_is_not_found(console) -> None:
    server, _run_dir = console
    status, _headers, _body = _post(
        server, "/whatever", {"token": server.token, "node_id": "checkpoint"},
        headers={"Origin": _origin(server)},
    )
    assert status == 404


# ── 11: a decision the facade refuses is a clean 409, never a crash ──────────

def test_post_approve_unknown_node_id_is_conflict_not_a_crash(console) -> None:
    server, _run_dir = console
    status, _headers, body = _post(
        server, "/approve", {"token": server.token, "node_id": "ghost-node"},
        headers={"Origin": _origin(server)},
    )
    assert status == 409
    assert "ghost-node" in body
    # The real node must be untouched — a bogus decision must never poison the ledger.
    get_status, _headers2, get_body = _get(server, f"/?token={server.token}")
    assert get_status == 200
    assert "checkpoint" in get_body


# ── 12: multi-gate — server keeps serving until every gate is decided ────────

def test_two_gate_manifest_stays_up_until_both_gates_decided(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path, name="two-gate", manifest=_TWO_GATE_MANIFEST, run_id="run-2")
    server = _start(run_dir)
    try:
        origin = _origin(server)
        status, headers, _body = _post(
            server, "/approve", {"token": server.token, "node_id": "gate1"},
            headers={"Origin": origin},
        )
        assert status == 303
        assert server.resolved_and_idle is False, "gate2 is still pending — must not auto-stop yet"

        get_status, _headers2, body = _get(server, headers["Location"])
        assert get_status == 200
        assert "gate2" in body

        status2, headers2, _body2 = _post(
            server, "/approve", {"token": server.token, "node_id": "gate2"},
            headers={"Origin": origin},
        )
        assert status2 == 303
        assert server.resolved_and_idle is False, "flag only flips once the follow-up GET renders"

        get_status2, _headers4, body2 = _get(server, headers2["Location"])
        assert get_status2 == 200
        assert "Nothing is currently awaiting approval" in body2
        assert server.resolved_and_idle is True
    finally:
        _stop(server)


# ── 13: open_console_run refuses symlinked / non-run directories ─────────────

def test_open_console_run_refuses_a_symlinked_run_dir(tmp_path: Path) -> None:
    real = _paused_run(tmp_path, name="real-run")
    link = tmp_path / "link-run"
    link.symlink_to(real)

    with pytest.raises(ConsoleOpenError, match="symlink"):
        open_console_run(link)


def test_open_console_run_refuses_a_directory_that_is_not_a_real_run(tmp_path: Path) -> None:
    fake = tmp_path / "not-a-run"
    fake.mkdir()
    (fake / "notes.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(ConsoleOpenError):
        open_console_run(fake)


def test_open_console_run_succeeds_on_a_valid_paused_run(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    identity, facade = open_console_run(run_dir)
    assert identity.run_id == "run-1"
    assert identity.organization_id == "local-org"
    assert facade is not None


def test_open_console_run_fails_closed_if_identity_reload_fails_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: `for_run_dir` already validates the run directory
    internally (via its OWN pre-bound reference to `_load_plan_from_run_dir`,
    untouched by this patch); `open_console_run` independently reloads identity a
    second time to build the ArenaReadRequest. If THAT second, independent call
    fails — e.g. a TOCTOU where the directory changed between the two calls — it
    must fail closed as `ConsoleOpenError`, never leak a raw exception."""
    run_dir = _paused_run(tmp_path)

    import bounded_loops.graph.cli_graph as cli_graph_module

    def _always_fails(path: Path) -> None:
        raise ValueError("simulated post-open identity reload failure")

    monkeypatch.setattr(cli_graph_module, "_load_plan_from_run_dir", _always_fails)

    with pytest.raises(ConsoleOpenError, match="simulated post-open identity reload failure"):
        open_console_run(run_dir)


# ── 14: a GET whose status read fails closed is a clean 500, never a crash ───

def test_get_root_when_run_directory_is_corrupted_after_open_returns_500(console) -> None:
    server, run_dir = console
    (run_dir / "manifest.yaml").write_text("not: [valid, yaml, at, all: :::", encoding="utf-8")
    status, _headers, _body = _get(server, f"/?token={server.token}")
    assert status == 500


# ── 15: token sanity ──────────────────────────────────────────────────────────

def test_each_server_gets_a_distinct_sufficiently_long_token(tmp_path: Path) -> None:
    run_a = _paused_run(tmp_path, name="a")
    run_b = _paused_run(tmp_path, name="b", run_id="run-b")
    server_a = _start(run_a)
    server_b = _start(run_b)
    try:
        assert server_a.token != server_b.token
        assert len(server_a.token) >= 32
    finally:
        _stop(server_a)
        _stop(server_b)


def test_console_url_embeds_host_port_and_token(console) -> None:
    server, _run_dir = console
    url = server.console_url
    assert url.startswith("http://127.0.0.1:")
    assert f":{server.server_address[1]}/" in url
    assert f"token={server.token}" in url
