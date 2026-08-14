"""C1 slice 2 — the frontier-connector invocation path is fail-closed and no-secret.

The invoker never touches a credential: it mints a single-use lease from the grant,
authorizes egress through the broker, and only then hands the OPAQUE lease + the
broker's PINNED addresses to a deployment-owned forwarder. A refused lease, a denied
egress, or a broken forwarder is a closed failure — never a silent call. A fake
forwarder stands in for the real KMS-backed one, so no real egress or secret is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bounded_loops.graph.application.connector_forward import (
    ConnectorInvocation,
    ConnectorInvoker,
    ConnectorResult,
)
from bounded_loops.graph.application.credential_broker import OpaqueCredentialBroker
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialBinding, CredentialKind, ExecutionGrant

_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
_DEST = "api.example.com:443"
_PUBLIC = "93.184.216.34"
_BODY = "sha256:" + "a" * 64
_RESP = "sha256:" + "b" * 64


class _StaticResolver:
    def __init__(self, ips):
        self._ips = tuple(ips)

    def resolve(self, host, port):
        return self._ips


def _grant(*, destinations=(_DEST,), effects=(Effect.EXTERNAL_WRITE,), expires=None):
    return ExecutionGrant(
        grant_id="grant:1", run_id="run-1", node_id="n1", attempt=1, connection_id="conn-1",
        effects=frozenset(effects), destinations=frozenset(destinations),
        expires_at=(expires or (_NOW + timedelta(minutes=5))).isoformat(),
    )


def _credential_broker():
    return OpaqueCredentialBroker([
        CredentialBinding(binding_id="bind-1", connection_id="conn-1", kind=CredentialKind.VAULT_REFERENCE),
    ])


def _invocation(*, destination=_DEST, method="POST", effect=Effect.EXTERNAL_WRITE, declared_bytes=10, payload_digest=_BODY):
    return ConnectorInvocation(
        destination=destination, method=method, effect=effect,
        payload_digest=payload_digest, declared_bytes=declared_bytes,
    )


@dataclass
class _RecordingForwarder:
    """A fake, no-egress, no-secret forwarder: records what it was handed and returns
    a canned content-addressed result. Proves the invoker passes only the opaque
    lease + pinned IPs — never a credential or inline bytes."""

    result: ConnectorResult
    calls: list = field(default_factory=list)

    def forward(self, *, lease, invocation, pinned_ips) -> ConnectorResult:
        self.calls.append((lease, invocation, pinned_ips))
        return self.result


class _RaisingForwarder:
    def forward(self, *, lease, invocation, pinned_ips) -> ConnectorResult:
        raise RuntimeError("forwarder blew up")


class _BadResultForwarder:
    def forward(self, *, lease, invocation, pinned_ips):
        return "not a ConnectorResult"  # type: ignore[return-value]


def _invoker(forwarder, *, resolver_ips=(_PUBLIC,)):
    return ConnectorInvoker(
        credential_broker=_credential_broker(),
        egress_broker=EgressBroker(resolver=_StaticResolver(resolver_ips)),
        forwarder=forwarder,
    )


def _invoke(invoker, *, grant=None, invocation=None, attempt=1):
    return invoker.invoke(
        grant=grant or _grant(), invocation=invocation or _invocation(),
        run_id="run-1", node_id="n1", attempt=attempt, now=_NOW,
    )


def test_happy_path_forwards_after_authorization_with_the_opaque_lease_and_pinned_ips():
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok", response_digest=_RESP, provider_status=200))
    result = _invoke(_invoker(forwarder))

    assert result.ok and result.response_digest == _RESP and result.provider_status == 200
    assert len(forwarder.calls) == 1
    lease, invocation, pinned = forwarder.calls[0]
    # The forwarder got the OPAQUE lease (a binding reference, not a secret), the
    # broker's PINNED address, and a content-addressed request (no inline bytes).
    assert lease.binding_id == "bind-1" and lease.destination == _DEST
    assert pinned == (_PUBLIC,)
    assert invocation.payload_digest == _BODY


def test_a_denied_egress_never_forwards():
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok"))
    result = _invoke(_invoker(forwarder, resolver_ips=("10.0.0.5",)))  # private → SSRF deny

    assert not result.ok and "egress denied" in result.reason
    assert forwarder.calls == []


def test_two_bindings_on_one_connection_fail_before_any_egress():
    """An ambiguous connection binding is refused at the MINT — nothing leaves the process.

    A grant carries the ``connection_id`` but not the ``binding_id``, so the broker recovers the
    binding by scanning for the one whose connection matches. Two bindings on one connection make
    that ambiguous and the mint refuses.

    This pins WHERE it refuses, which is the whole severity question for an ``external_write``
    node: the controller reports the resulting worker failure as ``cause=worker_fault`` with an
    empty ``node.spend``, and that classification cannot distinguish "refused before the request"
    from "the provider was already paid". It is the former — asserted here by the forwarder never
    being called, and egress never being authorized.
    """
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok"))
    invoker = ConnectorInvoker(
        credential_broker=OpaqueCredentialBroker([
            CredentialBinding(binding_id="bind-1", connection_id="conn-1", kind=CredentialKind.VAULT_REFERENCE),
            CredentialBinding(binding_id="bind-2", connection_id="conn-1", kind=CredentialKind.VAULT_REFERENCE),
        ]),
        egress_broker=EgressBroker(resolver=_StaticResolver((_PUBLIC,))),
        forwarder=forwarder,
    )

    result = _invoke(invoker)

    assert not result.ok
    assert "credential lease refused" in result.reason
    assert "exactly one broker binding" in result.reason
    assert forwarder.calls == [], "the request must never be forwarded — no provider was paid"


def test_a_refused_lease_never_forwards():
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok"))
    # The grant does not authorize this destination, so mint_lease refuses.
    result = _invoke(_invoker(forwarder), grant=_grant(destinations=("other.example.com:443",)))

    assert not result.ok and "credential lease refused" in result.reason
    assert forwarder.calls == []


def test_a_grant_for_a_different_attempt_is_refused():
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok"))
    result = _invoke(_invoker(forwarder), attempt=2)  # grant is audience-bound to attempt 1

    assert not result.ok and "credential lease refused" in result.reason
    assert forwarder.calls == []


def test_an_expired_grant_is_refused():
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok"))
    result = _invoke(_invoker(forwarder), grant=_grant(expires=_NOW - timedelta(minutes=1)))

    assert not result.ok and "credential lease refused" in result.reason
    assert forwarder.calls == []


def test_a_broken_forwarder_fails_closed():
    result = _invoke(_invoker(_RaisingForwarder()))
    assert not result.ok and result.reason == "connector forward failed"


def test_an_invalid_forwarder_result_fails_closed():
    result = _invoke(_invoker(_BadResultForwarder()))
    assert not result.ok and result.reason == "connector forwarder returned an invalid result"


def test_the_single_use_lease_blocks_a_second_identical_invocation():
    forwarder = _RecordingForwarder(ConnectorResult(True, "ok", response_digest=_RESP))
    invoker = _invoker(forwarder)

    first = _invoke(invoker)
    assert first.ok

    # mint_lease is deterministic (same grant/audience/destination/effect → same
    # lease_id), so a second identical invoke re-mints the SAME lease, which the
    # broker's single-use guard refuses — the double-spend is blocked at the broker.
    second = _invoke(invoker)
    assert not second.ok and "single-use" in second.reason
    assert len(forwarder.calls) == 1  # only the first ever forwarded
