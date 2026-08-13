"""C1 slice 3 — a connector node executes over the no-secret path via NodeWorkerPort.

ConnectorNodeWorker runs a connector node by invoking its connector through the
fail-closed ConnectorInvoker (no subprocess, no credential, and no real egress here —
a fake forwarder + injected resolver stand in), then maps the response digest to a
receipt-bound WorkerResult bound to the node's admitted route. A denied egress, a
failed forward, an unbound node, or a bad request port fails the node closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from bounded_loops.graph.application.connector_forward import (
    ConnectorInvocation,
    ConnectorInvoker,
    ConnectorResult,
)
from bounded_loops.graph.adapters.workers.connector_worker import ConnectorCall, ConnectorNodeWorker
from bounded_loops.graph.application.credential_broker import OpaqueCredentialBroker
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkMode
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.connections import CredentialBinding, CredentialKind, ExecutionGrant
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding

_DEST = "api.example.com:443"
_PUBLIC = "93.184.216.34"
_BODY = "sha256:" + "a" * 64
_RESP = "sha256:" + "d" * 64
_FAR_FUTURE = "2099-12-31T23:59:59Z"  # the worker uses the wall clock, so keep the grant valid


def _plan():
    binding = ResolvedBinding(
        binding_id="binding-1", slot_id="model", connector_id="openai-cli",
        connector_version="1.0.0", connection_id="conn-1",
        admission_digest="sha256:" + "b" * 64, route_policy_digest="sha256:" + "c" * 64,
        provider_id="openai", model_target="gpt", region="us", fallback=False, transport="https_api",
    )
    node = PlannedNode(
        node_id="call", kind="tool", package_digest=None, binding_id="binding-1",
        required_effects=frozenset({Effect.EXTERNAL_WRITE}), isolation=IsolationLevel.CONTAINER_RESTRICTED,
        hard_deadline_ms=5000, budgets={"max_attempts": 1}, approval_policy={},
    )
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "e" * 64,
        source_graph_digest="sha256:" + "f" * 64, policy_digest="sha256:" + "a" * 64,
        compiler_version="bounded-loops.graph-compiler/v1",
        nodes=(node,), edges=(), levels=(("call",),), package_digests=(),
        connection_bindings=(binding,), canonical_json=b'{"plan":"connector-fixture"}',
    )


def _node(plan):
    return plan.nodes[0]


def _envelope():
    return ExecutionEnvelope(
        IsolationLevel.CONTAINER_RESTRICTED, "https_api",
        frozenset({Effect.EXTERNAL_WRITE}), NetworkMode.DENY, (),
    )


def _grant():
    return ExecutionGrant(
        grant_id="grant:1", run_id="run-1", node_id="call", attempt=1, connection_id="conn-1",
        effects=frozenset({Effect.EXTERNAL_WRITE}), destinations=frozenset({_DEST}), expires_at=_FAR_FUTURE,
    )


def _invocation():
    return ConnectorInvocation(
        destination=_DEST, method="POST", effect=Effect.EXTERNAL_WRITE, payload_digest=_BODY, declared_bytes=10,
    )


class _StaticResolver:
    def __init__(self, ips):
        self._ips = tuple(ips)

    def resolve(self, host, port):
        return self._ips


@dataclass
class _Forwarder:
    result: ConnectorResult
    calls: list = field(default_factory=list)

    def forward(self, *, lease, invocation, pinned_ips) -> ConnectorResult:
        self.calls.append((lease, invocation, pinned_ips))
        return self.result


@dataclass
class _RequestPort:
    call: object

    def build(self, *, plan, node, envelope, attempt=1):
        return self.call


def _invoker(forwarder, *, ips=(_PUBLIC,)):
    return ConnectorInvoker(
        credential_broker=OpaqueCredentialBroker([
            CredentialBinding(binding_id="bind-1", connection_id="conn-1", kind=CredentialKind.VAULT_REFERENCE),
        ]),
        egress_broker=EgressBroker(resolver=_StaticResolver(ips)),
        forwarder=forwarder,
    )


def _worker(forwarder, *, ips=(_PUBLIC,), call=None):
    return ConnectorNodeWorker(
        run_id="run-1",
        invoker=_invoker(forwarder, ips=ips),
        request_port=_RequestPort(call if call is not None else ConnectorCall(grant=_grant(), invocation=_invocation())),
    )


def test_connector_node_returns_the_response_digest_bound_to_its_route():
    plan = _plan()
    forwarder = _Forwarder(ConnectorResult(True, "ok", response_digest=_RESP, provider_status=200))
    result = _worker(forwarder).execute(plan=plan, node=_node(plan), envelope=_envelope(), attempt=1)

    assert result.output_artifact_digests == (_RESP,)
    # Bound to the node's admitted route/transport so the controller's route-match holds.
    assert result.observed_route is not None and result.observed_route.provider_id == "openai"
    assert result.observed_transport == "https_api"
    # A connector node runs no process, so it publishes no process-isolation receipt.
    assert result.isolation_provider_id is None and result.enforced_controls is None
    # The forwarder got the opaque lease + pinned IPs + content-addressed request.
    lease, invocation, pinned = forwarder.calls[0]
    assert lease.binding_id == "bind-1" and pinned == (_PUBLIC,) and invocation.payload_digest == _BODY


def test_a_denied_egress_fails_the_node_closed():
    plan = _plan()
    forwarder = _Forwarder(ConnectorResult(True, "ok", response_digest=_RESP))
    worker = _worker(forwarder, ips=("10.0.0.5",))  # private → SSRF deny

    with pytest.raises(GraphIntegrityError, match="failed"):
        worker.execute(plan=plan, node=_node(plan), envelope=_envelope(), attempt=1)
    assert forwarder.calls == []  # never forwarded


def test_an_unbound_node_is_refused():
    plan = _plan()
    unbound = replace(_node(plan), binding_id=None)
    forwarder = _Forwarder(ConnectorResult(True, "ok", response_digest=_RESP))

    with pytest.raises(GraphIntegrityError, match="must be bound"):
        _worker(forwarder).execute(plan=plan, node=unbound, envelope=_envelope(), attempt=1)
    assert forwarder.calls == []


def test_a_request_port_that_returns_a_non_call_is_refused():
    plan = _plan()
    forwarder = _Forwarder(ConnectorResult(True, "ok", response_digest=_RESP))
    worker = _worker(forwarder, call="not a ConnectorCall")

    with pytest.raises(GraphIntegrityError, match="ConnectorCall"):
        worker.execute(plan=plan, node=_node(plan), envelope=_envelope(), attempt=1)
    assert forwarder.calls == []
