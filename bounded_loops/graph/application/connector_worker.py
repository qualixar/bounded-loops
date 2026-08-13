"""Execute a connector node by invoking its connector over the no-secret path (C1 slice 3).

A connector node's "work" is not a sandboxed subprocess — it is an AUTHORIZED EGRESS
call to an admitted connection (a frontier model API, or an on-box local model over
the same seam). This worker is the ``NodeWorkerPort`` adapter that runs such a node
through the fail-closed, no-secret ``ConnectorInvoker``: it asks a deployment-owned
``ConnectorRequestPort`` to assemble the node's audience-bound grant + content-addressed
request, invokes, and maps a successful result to a receipt-bound ``WorkerResult`` (the
response digest is the node's output, bound to the node's admitted route). A failed
invocation raises, so the controller records a closed node failure.

This worker holds no secret and performs no egress itself; request assembly and grant
issuance are deployment-owned (they need connector-adapter and authority specifics) and
live behind the port. Routing (which nodes are connector nodes) and the connector-node
policy/enforce semantics are the controller's concern and a follow-up — this is the
worker adapter only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bounded_loops.graph.application.connector_forward import ConnectorInvocation, ConnectorInvoker
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.domain.connections import ExecutionGrant, ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


@dataclass(frozen=True)
class ConnectorCall:
    """The grant + content-addressed request a connector node should make, assembled
    by a deployment-owned request port from the node's inputs and authority."""

    grant: ExecutionGrant
    invocation: ConnectorInvocation


class ConnectorRequestPort(Protocol):
    """Deployment-owned: assemble a node's connector call (its audience-bound grant
    and content-addressed request) from the immutable plan/node and the accepted
    envelope. No secret is produced here — only a grant reference and a request."""

    def build(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> ConnectorCall: ...


class ConnectorNodeWorker:
    """A ``NodeWorkerPort`` that executes a connector node via the no-secret invoker.

    It never runs a subprocess and never touches a credential: it assembles the call
    through the injected request port, invokes fail-closed, and returns the response
    digest as the node's output artifact bound to the node's admitted route. A failed
    invocation raises so the controller records a closed node failure.
    """

    def __init__(
        self, *, run_id: str, invoker: ConnectorInvoker, request_port: ConnectorRequestPort,
    ) -> None:
        self._run_id = run_id
        self._invoker = invoker
        self._request_port = request_port

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        if node.binding_id is None:
            raise GraphIntegrityError(
                f"connector node {node.node_id!r} must be bound to an admitted connection"
            )
        call = self._request_port.build(
            plan=plan, node=node, envelope=envelope, attempt=attempt,
        )
        if not isinstance(call, ConnectorCall):
            raise GraphIntegrityError("connector request port must return a ConnectorCall")
        result = self._invoker.invoke(
            grant=call.grant,
            invocation=call.invocation,
            run_id=self._run_id,
            node_id=node.node_id,
            attempt=attempt,
        )
        if not result.ok:
            raise GraphIntegrityError(f"connector node {node.node_id!r} failed: {result.reason}")
        digests = (result.response_digest,) if result.response_digest is not None else ()
        route, transport = self._route_for(plan, node)
        # usage keyword, not positional: WorkerResult's isolation fields sit between here and
        # transport, and a positional argument would silently land in one of them.
        return WorkerResult(digests, route, transport, usage=result.usage)

    @staticmethod
    def _route_for(plan: ExecutionPlan, node: PlannedNode) -> tuple[ResolvedRoute | None, str | None]:
        binding = next((b for b in plan.connection_bindings if b.binding_id == node.binding_id), None)
        if binding is None:
            return (None, None)
        route = ResolvedRoute(
            binding.provider_id,
            binding.model_target,
            binding.region,
            binding.fallback,
            binding.route_policy_digest,
        )
        return (route, binding.transport)
