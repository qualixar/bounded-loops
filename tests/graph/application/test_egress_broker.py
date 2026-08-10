"""C1 slice 1 — the no-secret egress broker authorizes fail-closed.

The broker is pure policy: it never performs egress. Every deny path is proven
with an injected resolver so the SSRF / rebind guard is deterministic on any host.
"""

from __future__ import annotations

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


def _lease(*, destination="api.example.com:443", effects=(Effect.EXTERNAL_WRITE,), expires=None, lease_id="lease:abc"):
    return CredentialLease(
        lease_id=lease_id,
        grant_id="grant:1",
        run_id="run-1",
        node_id="n1",
        attempt=1,
        connection_id="conn-1",
        binding_id="bind-1",
        effects=frozenset(effects),
        destination=destination,
        expires_at=(expires or (_NOW + timedelta(minutes=5))).isoformat(),
    )


def _request(*, destination="api.example.com:443", host="api.example.com", port=443,
             method="POST", effect=Effect.EXTERNAL_WRITE, declared_bytes=10):
    return EgressRequest(
        destination=destination, host=host, port=port, method=method,
        effect=effect, declared_bytes=declared_bytes,
    )


class _StaticResolver:
    def __init__(self, ips):
        self._ips = tuple(ips)

    def resolve(self, host, port):
        return self._ips


class _RaisingResolver:
    def resolve(self, host, port):
        raise OSError("resolver blew up")


# ── IP classification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_ips_pass(ip):
    assert _is_public_unicast(ip)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",  # cloud metadata
    "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "::ffff:10.0.0.1",  # ipv4-mapped private
    "not-an-ip",
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


# ── fail-closed deny paths ──────────────────────────────────────────────────

def test_expired_lease_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    lease = _lease(expires=_NOW - timedelta(seconds=1))
    assert not broker.authorize(lease=lease, request=_request(), now=_NOW).allowed


def test_unparseable_expiry_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    lease = _lease()
    object.__setattr__(lease, "expires_at", "not-a-time")
    assert not broker.authorize(lease=lease, request=_request(), now=_NOW).allowed


def test_destination_mismatch_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    req = _request(destination="evil.example.com:443")
    out = broker.authorize(lease=_lease(), request=req, now=_NOW)
    assert not out.allowed and "destination" in out.reason


def test_effect_not_in_lease_denied():
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    req = _request(effect=Effect.FINANCIAL)
    out = broker.authorize(lease=_lease(effects=(Effect.EXTERNAL_WRITE,)), request=req, now=_NOW)
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
    broker = EgressBroker(resolver=_RaisingResolver())
    out = broker.authorize(lease=_lease(), request=_request(), now=_NOW)
    assert not out.allowed and "resolve" in out.reason


def test_empty_resolution_denied():
    broker = EgressBroker(resolver=_StaticResolver([]))
    assert not broker.authorize(lease=_lease(), request=_request(), now=_NOW).allowed


def test_private_resolution_denied():
    broker = EgressBroker(resolver=_StaticResolver(["10.0.0.5"]))
    out = broker.authorize(lease=_lease(), request=_request(), now=_NOW)
    assert not out.allowed and "SSRF" in out.reason


def test_metadata_ip_denied():
    """The classic SSRF target — cloud metadata at 169.254.169.254 — is refused."""
    broker = EgressBroker(resolver=_StaticResolver(["169.254.169.254"]))
    assert not broker.authorize(lease=_lease(), request=_request(), now=_NOW).allowed


def test_split_public_and_private_denied():
    """DNS-rebind split: if ANY resolved address is non-public, the whole request is
    refused (a rebind cannot smuggle an internal address alongside a public one)."""
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34", "10.0.0.5"]))
    assert not broker.authorize(lease=_lease(), request=_request(), now=_NOW).allowed


def test_denied_request_does_not_consume_lease():
    """A denied request must not burn the single-use lease (same broker instance)."""
    broker = EgressBroker(resolver=_StaticResolver(["93.184.216.34"]))
    lease = _lease()
    denied = broker.authorize(lease=lease, request=_request(method="TRACE"), now=_NOW)  # method deny
    assert not denied.allowed
    allowed = broker.authorize(lease=lease, request=_request(), now=_NOW)  # same lease, now valid
    assert allowed.allowed  # the earlier deny did not consume the single-use lease


def test_request_validation():
    with pytest.raises(ValueError):
        _request(port=0)
    with pytest.raises(ValueError):
        _request(method="")
    with pytest.raises(ValueError):
        _request(declared_bytes=-1)
