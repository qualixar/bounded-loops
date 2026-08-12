"""C1 slice 1 — the no-secret egress broker authorizes fail-closed.

The broker is pure policy: it never performs egress and resolves the LEASE's own
destination, so a caller cannot supply a divergent host. Every deny path is proven
with an injected resolver so the SSRF / rebind guard is deterministic on any host.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from bounded_loops.graph.application.egress_broker import (
    EgressBroker,
    EgressRequest,
    _is_public_unicast,
)
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialLease

_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
_DEST = "api.example.com:443"


def _lease(*, destination=_DEST, effects=(Effect.EXTERNAL_WRITE,), expires=None, lease_id="lease:abc"):
    return CredentialLease(
        lease_id=lease_id, grant_id="grant:1", run_id="run-1", node_id="n1", attempt=1,
        connection_id="conn-1", binding_id="bind-1", effects=frozenset(effects),
        destination=destination, expires_at=(expires or (_NOW + timedelta(minutes=5))).isoformat(),
    )


def _request(*, destination=_DEST, method="POST", effect=Effect.EXTERNAL_WRITE, declared_bytes=10):
    return EgressRequest(destination=destination, method=method, effect=effect, declared_bytes=declared_bytes)


class _StaticResolver:
    def __init__(self, ips):
        self._ips = tuple(ips)

    def resolve(self, host, port):
        return self._ips


class _RaisingResolver:
    def resolve(self, host, port):
        raise OSError("resolver blew up")


# ── IP classification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "100.128.0.1"])
def test_public_ips_pass(ip):
    assert _is_public_unicast(ip)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",  # metadata
    "100.64.0.1",  # CGNAT (RFC 6598) — not is_private, but not is_global
    "192.88.99.1",  # 6to4 relay anycast
    "0.0.0.0", "255.255.255.255", "224.0.0.1",
    "::1", "fe80::1", "fc00::1", "fec0::1",  # site-local (deprecated)
    "2001:20::1",  # ORCHIDv2 (non-routable experimental)
    "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:100.64.0.1",  # v4-mapped internal
    "not-an-ip", "",
])
def test_non_public_ips_fail(ip):
    assert not _is_public_unicast(ip)


# ── happy path ────────────────────────────────────────────────────────────────

def test_authorizes_and_pins_public_destination():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    decision = broker.authorize(lease=_lease(), request=_request(), now=_NOW)
    assert decision.allowed and decision.pinned_ips == ("93.184.216.34",)


def test_single_use_consumes_the_lease():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    first = broker.authorize(lease=_lease(), request=_request(), now=_NOW)
    second = broker.authorize(lease=_lease(), request=_request(), now=_NOW)
    assert first.allowed
    assert not second.allowed and "single-use" in second.reason


def test_naive_now_is_handled_as_utc():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    out = broker.authorize(lease=_lease(), request=_request(), now=datetime(2026, 8, 10, 12, 0, 0))
    assert out.allowed  # normalized to UTC, within TTL — never a TypeError


def test_concurrent_single_use_is_exactly_once():
    class _SlowResolver:
        def resolve(self, host, port):
            time.sleep(0.02)  # widen the race window the lock must close
            return ("93.184.216.34",)

    broker = EgressBroker(resolver=_SlowResolver())
    lease = _lease()
    results: list[bool] = []
    barrier = threading.Barrier(5)

    def run():
        barrier.wait()
        results.append(broker.authorize(lease=lease, request=_request(), now=_NOW).allowed)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1 and results.count(False) == 4  # exactly-once under concurrency


def test_future_now_cannot_prune_valid_consumed_lease():
    """A caller-supplied FUTURE now must not evict a still-valid consumed lease —
    otherwise clock manipulation would silently defeat single-use."""
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    victim = _lease(lease_id="lease:victim", expires=_NOW + timedelta(days=3650))
    assert broker.authorize(lease=victim, request=_request(), now=_NOW).allowed  # consume it
    # An unrelated call with a far-future now must not prune the victim's record.
    attacker = _lease(lease_id="lease:attacker", expires=_NOW + timedelta(days=4000))
    broker.authorize(lease=attacker, request=_request(), now=_NOW + timedelta(days=3999))
    replay = broker.authorize(lease=victim, request=_request(), now=_NOW)  # victim still within TTL
    assert not replay.allowed and "single-use" in replay.reason


# ── fail-closed deny paths ──────────────────────────────────────────────────

def test_expired_lease_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    assert not broker.authorize(lease=_lease(expires=_NOW - timedelta(seconds=1)), request=_request(), now=_NOW).allowed


def test_unparseable_expiry_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    lease = _lease()
    object.__setattr__(lease, "expires_at", "not-a-time")
    assert not broker.authorize(lease=lease, request=_request(), now=_NOW).allowed


def test_destination_mismatch_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    out = broker.authorize(lease=_lease(), request=_request(destination="evil.example.com:443"), now=_NOW)
    assert not out.allowed and "destination" in out.reason


def test_malformed_lease_destination_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    out = broker.authorize(lease=_lease(destination="host:notaport"), request=_request(destination="host:notaport"), now=_NOW)
    assert not out.allowed and "malformed" in out.reason


def test_effect_not_in_lease_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    out = broker.authorize(lease=_lease(effects=(Effect.EXTERNAL_WRITE,)), request=_request(effect=Effect.FINANCIAL), now=_NOW)
    assert not out.allowed and "effect" in out.reason


def test_method_not_allowed_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]), allowed_methods=frozenset({"GET"}))
    out = broker.authorize(lease=_lease(), request=_request(method="POST"), now=_NOW)
    assert not out.allowed and "method" in out.reason


def test_byte_cap_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]), max_bytes=100)
    out = broker.authorize(lease=_lease(), request=_request(declared_bytes=101), now=_NOW)
    assert not out.allowed and "byte cap" in out.reason


def test_resolver_failure_denied():
    out = EgressBroker(resolver=_RaisingResolver()).authorize(lease=_lease(), request=_request(), now=_NOW)
    assert not out.allowed and "resolve" in out.reason


def test_empty_resolution_denied():
    assert not EgressBroker(resolver=_StaticResolver([])).authorize(lease=_lease(), request=_request(), now=_NOW).allowed


@pytest.mark.parametrize("ip", ["10.0.0.5", "169.254.169.254", "100.64.0.1", "fec0::1"])
def test_non_public_resolution_denied(ip):
    out = EgressBroker(resolver=_StaticResolver([ip])).authorize(lease=_lease(), request=_request(), now=_NOW)
    assert not out.allowed and "SSRF" in out.reason


def test_split_public_and_private_denied():
    """DNS-rebind split: if ANY resolved address is non-public, the whole request is
    refused (a rebind cannot smuggle an internal address alongside a public one)."""
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34", "10.0.0.5"]))
    assert not broker.authorize(lease=_lease(), request=_request(), now=_NOW).allowed


def test_denied_request_does_not_consume_lease():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    lease = _lease()
    assert not broker.authorize(lease=lease, request=_request(method="TRACE"), now=_NOW).allowed  # method deny
    assert broker.authorize(lease=lease, request=_request(), now=_NOW).allowed  # earlier deny did not consume


def test_request_validation():
    with pytest.raises(ValueError):
        _request(destination="")
    with pytest.raises(ValueError):
        _request(method="")
    with pytest.raises(ValueError):
        _request(declared_bytes=-1)
