"""Hermetic tests for the RC-LOCKDOWN loopback CONNECT proxy.

All traffic is loopback: a stand-in echo server is the "upstream", an injected resolver maps the
admitted hostname to 127.0.0.1, and the allow-path test relaxes the pin policy so the loopback
stand-in is reachable. The deny/SSRF paths use the REAL public-unicast guard. No real egress, no
network, no subscription.
"""

from __future__ import annotations

import socket
import threading

from bounded_loops.graph.adapters.enforcement.egress_proxy import LoopbackEgressProxy
from bounded_loops.graph.application.execution_policy import NetworkDestination


# ── loopback echo "upstream" ─────────────────────────────────────────────────

def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_echo_conn, args=(conn,), daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()
    return srv, port


def _echo_conn(conn: socket.socket) -> None:
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                return
            conn.sendall(data)
    except OSError:
        return
    finally:
        conn.close()


class _FakeResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._mapping = mapping

    def resolve(self, host: str, port: int | None) -> tuple[str, ...]:
        return self._mapping[host]  # KeyError → proxy denies fail-closed


def _connect_through(proxy_port: int, target: str) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
    return client


# ── tests ─────────────────────────────────────────────────────────────────────

def test_allowed_destination_tunnels_bytes():
    srv, echo_port = _echo_server()
    logs: list[dict] = []
    try:
        proxy = LoopbackEgressProxy(
            allowed=(NetworkDestination("example.test", echo_port),),
            resolver=_FakeResolver({"example.test": ("127.0.0.1",)}),
            pin_policy=lambda ip: True,  # relax the SSRF guard so the loopback stand-in is reachable
            log=lambda **kw: logs.append(kw),
        )
        port = proxy.start()
        assert port > 0
        try:
            client = _connect_through(port, f"example.test:{echo_port}")
            assert client.recv(1024).startswith(b"HTTP/1.1 200"), "tunnel must be established"
            client.sendall(b"hello-cage")
            assert client.recv(1024) == b"hello-cage", "bytes must round-trip through the tunnel"
            client.close()
        finally:
            proxy.stop()
    finally:
        srv.close()
    assert any(entry["allowed"] for entry in logs), "an allowed tunnel must be logged"


def test_unlisted_destination_is_forbidden():
    logs: list[dict] = []
    proxy = LoopbackEgressProxy(
        allowed=(NetworkDestination("ok.test", 443),),
        resolver=_FakeResolver({}),
        log=lambda **kw: logs.append(kw),
    )
    port = proxy.start()
    try:
        client = _connect_through(port, "evil.test:443")
        assert client.recv(1024).startswith(b"HTTP/1.1 403"), "an unlisted destination must be refused"
        client.close()
    finally:
        proxy.stop()
    assert logs and not logs[-1]["allowed"]
    assert "allowlist" in logs[-1]["reason"]


def test_allowed_host_resolving_to_private_ip_is_ssrf_denied():
    # Real public-unicast guard (default pin_policy): an admitted host that resolves to a private
    # address must be refused — a rebind/misconfig cannot punch the cage inward.
    proxy = LoopbackEgressProxy(
        allowed=(NetworkDestination("private.test", 8080),),
        resolver=_FakeResolver({"private.test": ("10.0.0.5",)}),
    )
    port = proxy.start()
    try:
        client = _connect_through(port, "private.test:8080")
        assert client.recv(1024).startswith(b"HTTP/1.1 403"), "SSRF to a private address must be denied"
        client.close()
    finally:
        proxy.stop()


def test_mixed_public_and_private_resolution_denied():
    proxy = LoopbackEgressProxy(
        allowed=(NetworkDestination("mixed.test", 443),),
        resolver=_FakeResolver({"mixed.test": ("93.184.216.34", "127.0.0.1")}),  # one public, one loopback
    )
    port = proxy.start()
    try:
        client = _connect_through(port, "mixed.test:443")
        assert client.recv(1024).startswith(b"HTTP/1.1 403"), "any non-public address in the set denies"
        client.close()
    finally:
        proxy.stop()


def test_non_connect_request_is_rejected():
    proxy = LoopbackEgressProxy(allowed=(), resolver=_FakeResolver({}))
    port = proxy.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")
        assert client.recv(1024).startswith(b"HTTP/1.1 400"), "only CONNECT is honored"
        client.close()
    finally:
        proxy.stop()


def test_connect_without_port_is_rejected():
    proxy = LoopbackEgressProxy(allowed=(), resolver=_FakeResolver({}))
    port = proxy.start()
    try:
        client = _connect_through(port, "example.test")  # no :port
        assert client.recv(1024).startswith(b"HTTP/1.1 400")
        client.close()
    finally:
        proxy.stop()


# ── proxy dual-audit hardening (M1 capacity, M2 restart, M3 IP-literal, m5 oversized) ──

def test_ip_literal_connect_is_denied_not_crashed():
    # An IP-literal target makes NetworkDestination raise; it must become a clean 403 + log, never
    # an unhandled crash that drops the client with no decision (dual-audit MAJOR-3).
    logs: list[dict] = []
    proxy = LoopbackEgressProxy(allowed=(), resolver=_FakeResolver({}), log=lambda **kw: logs.append(kw))
    port = proxy.start()
    try:
        client = _connect_through(port, "127.0.0.1:443")
        assert client.recv(1024).startswith(b"HTTP/1.1 403"), "an IP-literal must be cleanly refused"
        client.close()
    finally:
        proxy.stop()
    assert logs and not logs[-1]["allowed"]


def test_restart_after_stop_accepts_again():
    # stop() must not leave the proxy dead-on-restart (dual-audit MAJOR-2: the stop event is reset).
    srv, echo_port = _echo_server()
    try:
        proxy = LoopbackEgressProxy(
            allowed=(NetworkDestination("example.test", echo_port),),
            resolver=_FakeResolver({"example.test": ("127.0.0.1",)}),
            pin_policy=lambda ip: True,
        )
        proxy.start()
        proxy.stop()
        port = proxy.start()  # restart
        try:
            client = _connect_through(port, f"example.test:{echo_port}")
            assert client.recv(1024).startswith(b"HTTP/1.1 200"), "proxy must accept after restart"
            client.close()
        finally:
            proxy.stop()
    finally:
        srv.close()


def test_capacity_cap_returns_503():
    # A connection beyond the cap is refused 503, so a flood cannot exhaust threads (dual-audit MAJOR-1).
    srv, echo_port = _echo_server()
    try:
        proxy = LoopbackEgressProxy(
            allowed=(NetworkDestination("example.test", echo_port),),
            resolver=_FakeResolver({"example.test": ("127.0.0.1",)}),
            pin_policy=lambda ip: True,
            max_connections=1,
        )
        port = proxy.start()
        held = None
        try:
            held = _connect_through(port, f"example.test:{echo_port}")
            assert held.recv(1024).startswith(b"HTTP/1.1 200")  # holds the only slot
            overflow = socket.create_connection(("127.0.0.1", port), timeout=5)
            assert overflow.recv(1024).startswith(b"HTTP/1.1 503"), "over-capacity must be refused 503"
            overflow.close()
        finally:
            if held is not None:
                held.close()
            proxy.stop()
    finally:
        srv.close()


def test_oversized_request_head_is_rejected():
    proxy = LoopbackEgressProxy(allowed=(), resolver=_FakeResolver({}))
    port = proxy.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"CONNECT " + b"A" * 9000)  # >8192 and no terminating CRLFCRLF
        assert client.recv(1024).startswith(b"HTTP/1.1 400"), "an oversized head must be refused"
        client.close()
    finally:
        proxy.stop()


def test_pipelined_bytes_after_connect_are_forwarded():
    # Bytes sent in the SAME segment after the CONNECT head (a pipelined TLS ClientHello) must reach
    # upstream, not be dropped — else real HTTPS clients stall (dual-audit MAJOR M3).
    srv, echo_port = _echo_server()
    try:
        proxy = LoopbackEgressProxy(
            allowed=(NetworkDestination("example.test", echo_port),),
            resolver=_FakeResolver({"example.test": ("127.0.0.1",)}),
            pin_policy=lambda ip: True,
        )
        port = proxy.start()
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            client.sendall(
                f"CONNECT example.test:{echo_port} HTTP/1.1\r\nHost: example.test\r\n\r\n".encode("ascii")
                + b"PIPELINED-HELLO"
            )
            resp = client.recv(1024)
            assert resp.startswith(b"HTTP/1.1 200")
            data = resp.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in resp else b""
            client.settimeout(5)
            while b"PIPELINED-HELLO" not in data:
                more = client.recv(1024)
                if not more:
                    break
                data += more
            assert b"PIPELINED-HELLO" in data, "pipelined bytes must be forwarded to upstream and echoed back"
            client.close()
        finally:
            proxy.stop()
    finally:
        srv.close()


def test_double_start_raises():
    proxy = LoopbackEgressProxy(allowed=(), resolver=_FakeResolver({}))
    proxy.start()
    try:
        import pytest
        with pytest.raises(RuntimeError):
            proxy.start()
    finally:
        proxy.stop()
