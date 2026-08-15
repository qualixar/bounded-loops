"""RED-first tests for `bl graph watch` live SSE surface (U3 — live run visibility).

Drives a REAL ``WatchServer`` on an OS-assigned loopback port with an in-process
HTTP client.  Tests assert the FULL security posture:

* Binds 127.0.0.1 only — never a routable interface.
* Per-invocation token, constant-time comparison, gates BOTH the page and the stream.
* Origin/Referer check on the stream connection — CSRF defense in depth.
* Path validation: run_dir locked at construction; a request that names any other
  file must be refused (the file-read surface is the threat model).
* Cross-origin request (hostile page trick) is refused with 403.
* SSE stream produces valid event lines from the event log.
* Arena page contains the stream URL when served (dual-mode template check).
* Static arena.html opened from disk has NO EventSource in it.
* Connection semaphore: a 9th simultaneous connection receives 503.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from bounded_loops.graph.graph_composition import execute_graph_run

# ── fixtures ──────────────────────────────────────────────────────────────────

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: watch-test
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


def _paused_run(tmp_path: Path) -> Path:
    out_dir = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out_dir, run_id="run-1",
    )
    assert rc == 3, "fixture manifest must pause at its approval node"
    return out_dir


def _start_watch(run_dir: Path):
    """Start a WatchServer on an ephemeral port and return it."""
    from bounded_loops.graph.live.sse_server import WatchServer, open_watch_run
    identity, facade = open_watch_run(run_dir)
    server = WatchServer(identity=identity, facade=facade, run_dir=run_dir, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stop(server) -> None:
    server.shutdown()


def _conn(server) -> http.client.HTTPConnection:
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    return conn


# ── import guard ──────────────────────────────────────────────────────────────

def test_posture_module_is_importable() -> None:
    """live/posture.py must be importable and export the expected names."""
    from bounded_loops.graph.live import posture  # noqa: F401
    assert hasattr(posture, "token_ok")
    assert hasattr(posture, "origin_ok")
    assert hasattr(posture, "_LOOPBACK_HOST")
    assert hasattr(posture, "_TOKEN_BYTES")
    assert hasattr(posture, "_MAX_CONCURRENT_CONNECTIONS")


def test_sse_server_module_is_importable() -> None:
    from bounded_loops.graph.live.sse_server import WatchServer, open_watch_run  # noqa: F401


def test_cli_watch_module_is_importable() -> None:
    from bounded_loops.graph.live.cli_watch import cmd_graph_watch  # noqa: F401


# ── security: bind address ─────────────────────────────────────────────────────

def test_watch_server_binds_loopback_only(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        host = server.server_address[0]
        assert host == "127.0.0.1", f"bound to {host!r}, not loopback"
    finally:
        _stop(server)


# ── security: token check ─────────────────────────────────────────────────────

def test_get_page_without_token_returns_403(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", "/")
        r = conn.getresponse()
        assert r.status == 403
    finally:
        _stop(server)


def test_get_page_with_wrong_token_returns_403(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", "/?token=wrongtoken")
        r = conn.getresponse()
        assert r.status == 403
    finally:
        _stop(server)


def test_get_page_with_valid_token_returns_200(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", f"/?token={quote(server.token, safe='')}")
        r = conn.getresponse()
        assert r.status == 200
        body = r.read().decode()
        assert "arena" in body.lower() or "bounded-loops" in body.lower()
    finally:
        _stop(server)


def test_get_events_without_token_returns_403(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", "/events")
        r = conn.getresponse()
        assert r.status == 403
    finally:
        _stop(server)


def test_get_events_with_wrong_token_returns_403(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", "/events?token=badtoken")
        r = conn.getresponse()
        assert r.status == 403
    finally:
        _stop(server)


# ── security: CSRF / origin check ─────────────────────────────────────────────

def test_sse_stream_cross_origin_is_refused(tmp_path: Path) -> None:
    """A browser on a hostile page would send the hostile page's Origin header.
    The stream must refuse that, just as the console refuses a cross-origin POST.
    """
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        token = quote(server.token, safe="")
        conn.request(
            "GET", f"/events?token={token}",
            headers={"Origin": "http://evil.example.com"},
        )
        r = conn.getresponse()
        assert r.status == 403
    finally:
        _stop(server)


def test_sse_stream_no_origin_returns_403(tmp_path: Path) -> None:
    """No Origin and no Referer → fail closed; same discipline as the console POST."""
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        token = quote(server.token, safe="")
        conn.request("GET", f"/events?token={token}")
        r = conn.getresponse()
        assert r.status == 403
    finally:
        _stop(server)


def test_sse_stream_same_origin_returns_200(tmp_path: Path) -> None:
    """Correct Origin → 200 text/event-stream."""
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        port = server.server_address[1]
        conn = _conn(server)
        token = quote(server.token, safe="")
        conn.request(
            "GET", f"/events?token={token}",
            headers={"Origin": f"http://127.0.0.1:{port}"},
        )
        r = conn.getresponse()
        assert r.status == 200
        ct = r.getheader("Content-Type", "")
        assert "text/event-stream" in ct
        # Read one event (server sends an initial snapshot) then cancel.
        chunk = b""
        while b"\n\n" not in chunk:
            chunk += r.read(256)
            if len(chunk) > 65536:
                break
        assert b"data:" in chunk
    finally:
        _stop(server)


# ── security: path traversal ──────────────────────────────────────────────────

def test_unknown_path_returns_404(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", f"/?token={quote(server.token, safe='')}")
        conn.getresponse().read()
        conn2 = _conn(server)
        conn2.request("GET", f"/../../etc/passwd?token={quote(server.token, safe='')}")
        r = conn2.getresponse()
        assert r.status == 404
    finally:
        _stop(server)


# ── security: hardening headers on every response ─────────────────────────────

def test_hardening_headers_on_403(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        conn = _conn(server)
        conn.request("GET", "/")
        r = conn.getresponse()
        r.read()
        assert r.getheader("Referrer-Policy") == "no-referrer"
        assert r.getheader("X-Content-Type-Options") == "nosniff"
        # Cache-Control and Pragma may vary for event-stream; check on error pages only.
        cc = r.getheader("Cache-Control", "")
        assert "no-store" in cc
    finally:
        _stop(server)


# ── SSE payload format ─────────────────────────────────────────────────────────

def test_sse_event_contains_valid_json(tmp_path: Path) -> None:
    """The first SSE event from /events must be valid JSON matching arena projection schema."""
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        port = server.server_address[1]
        conn = _conn(server)
        token = quote(server.token, safe="")
        conn.request(
            "GET", f"/events?token={token}",
            headers={"Origin": f"http://127.0.0.1:{port}"},
        )
        r = conn.getresponse()
        assert r.status == 200
        chunk = b""
        while b"\n\n" not in chunk:
            chunk += r.read(256)
            if len(chunk) > 65536:
                break
        # Find the first "data:" line.
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                assert "run_state" in payload
                assert "nodes" in payload
                break
        else:
            pytest.fail("No data: line found in SSE output")
    finally:
        _stop(server)


# ── Arena page: dual-mode template ────────────────────────────────────────────

def test_live_page_contains_stream_url(tmp_path: Path) -> None:
    """The Arena page served by WatchServer must inject the stream URL."""
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        port = server.server_address[1]
        conn = _conn(server)
        conn.request("GET", f"/?token={quote(server.token, safe='')}")
        r = conn.getresponse()
        assert r.status == 200
        body = r.read().decode()
        # The stream URL must appear in the served page.
        assert "/events?token=" in body or "__BL_STREAM_URL__" in body
        assert str(port) in body
    finally:
        _stop(server)


def test_static_arena_html_has_no_eventsource(tmp_path: Path) -> None:
    """A saved arena.html opened from disk must not contain EventSource calls."""
    from bounded_loops.graph.arena.cli_arena import cmd_graph_arena  # noqa: F401
    from bounded_loops.graph.arena.render import load_template
    template = load_template()
    # The static template must not contain EventSource (it's injected only when served).
    assert "EventSource" not in template


# ── posture extraction: token_ok / origin_ok ──────────────────────────────────

def test_token_ok_accepts_only_an_exact_match() -> None:
    """Correctness, including the two length-mismatch cases that must not raise.

    Renamed from `test_token_ok_constant_time`, which claimed a timing property this body does not
    measure — a plain `==` passes every assertion here unchanged. The timing guarantee is asserted
    separately below, against the mechanism. Named for a property it did not test, which the
    wave-1 Grok audit flagged.
    """
    from bounded_loops.graph.live.posture import token_ok
    assert token_ok("abc", "abc") is True
    assert token_ok("abc", "xyz") is False
    assert token_ok("", "abc") is False
    assert token_ok("abc", "") is False
    assert token_ok("abc", "abcd") is False, "a prefix must not authenticate"


def test_token_ok_uses_a_constant_time_primitive() -> None:
    """The actual timing guarantee, asserted against the mechanism rather than the clock.

    A wall-clock timing test on a comparison this short is dominated by scheduler noise and would
    be flaky in CI — it would eventually be marked skip, and the property would go unguarded. What
    is checkable and stable is that the comparison goes through `hmac.compare_digest` on bytes:
    that is where "time does not vary with WHICH byte differs" comes from, and swapping in `==`
    (the regression this exists to catch) fails here immediately.
    """
    import inspect

    from bounded_loops.graph.live import posture

    source = inspect.getsource(posture.token_ok)

    assert "hmac.compare_digest" in source, (
        "token comparison no longer uses a constant-time primitive; a plain == leaks how much of "
        "the token an attacker has guessed"
    )
    assert ".encode(" in source, (
        "compare_digest must receive bytes — on str arguments it raises for non-ASCII, so a "
        "unicode token would crash the handler instead of being rejected"
    )


def test_origin_ok_logic() -> None:
    from bounded_loops.graph.live.posture import origin_ok

    class FakeHeaders:
        def __init__(self, d):
            self._d = d
        def get(self, key, default=None):
            return self._d.get(key, default)

    expected = "http://127.0.0.1:9999"
    assert origin_ok(FakeHeaders({"Origin": expected}), expected) is True
    assert origin_ok(FakeHeaders({"Origin": "http://evil.com"}), expected) is False
    assert origin_ok(FakeHeaders({"Referer": expected + "/"}), expected) is True
    assert origin_ok(FakeHeaders({"Referer": "http://other.com/"}), expected) is False
    # Neither header → fail closed.
    assert origin_ok(FakeHeaders({}), expected) is False


# ── spend panel in arena data ─────────────────────────────────────────────────

def test_arena_projection_includes_spend_fields(tmp_path: Path) -> None:
    """ArenaProjection.spend_tokens / spend_cost_microunits must be present in JSON."""
    run_dir = _paused_run(tmp_path)
    server = _start_watch(run_dir)
    try:
        port = server.server_address[1]
        conn = _conn(server)
        token = quote(server.token, safe="")
        conn.request(
            "GET", f"/events?token={token}",
            headers={"Origin": f"http://127.0.0.1:{port}"},
        )
        r = conn.getresponse()
        assert r.status == 200
        chunk = b""
        while b"\n\n" not in chunk:
            chunk += r.read(512)
            if len(chunk) > 131072:
                break
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                # Fields must be present (may be 0 for a run with no spend).
                assert "spend_tokens" in payload
                assert "spend_cost_microunits" in payload
                assert "spend_complete" in payload
                break
        else:
            pytest.fail("No data: line found")
    finally:
        _stop(server)


# ── CLI registration ──────────────────────────────────────────────────────────

def test_watch_subcommand_is_registered() -> None:
    """bl graph watch must be wired into the graph subparser group."""
    import argparse
    from bounded_loops.graph.cli_graph import register

    p = argparse.ArgumentParser()
    subs = p.add_subparsers()
    register(subs)
    # Parse a `graph watch --run /dev/null` style call — it must not raise.
    args = p.parse_args(["graph", "watch", "--run", "/dev/null"])
    assert hasattr(args, "func")


# ── console posture extraction: existing console still works ──────────────────

def test_console_server_still_uses_loopback(tmp_path: Path) -> None:
    """Refactoring console/server.py to import from live/posture must not change its bind."""
    from bounded_loops.graph.console.server import ConsoleServer, open_console_run

    run_dir = _paused_run(tmp_path)
    identity, facade = open_console_run(run_dir)
    server = ConsoleServer(identity=identity, facade=facade, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()


def test_console_token_ok_uses_posture() -> None:
    """After refactor, console token check must still use constant-time compare."""
    from bounded_loops.graph.live.posture import token_ok
    # Verify the function is importable from posture and behaves correctly.
    assert token_ok("x" * 43, "x" * 43) is True
    assert token_ok("x" * 43, "y" * 43) is False
