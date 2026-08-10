"""Receipt-derived, read-only data for a future Graph Arena client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.schedule_ready import NodeState, predecessors_admit
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, StoredGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding


_ALLOWED = {
    "PENDING": frozenset({"READY"}),
    "READY": frozenset({"STARTING"}),
    "STARTING": frozenset({"RUNNING"}),
    "RUNNING": frozenset({"GATING", "FAILED"}),
    "GATING": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
}


@dataclass(frozen=True)
class ArenaReadRequest:
    subject_id: str
    organization_id: str
    project_id: str
    run_id: str


class ArenaAuthorizationPort(Protocol):
    def authorize(self, request: ArenaReadRequest) -> bool: ...


class ArenaReceiptVerifierPort(Protocol):
    """Verifies receipt trust independently of hash-chain integrity."""

    def verify(self, identity: GraphRunIdentity, receipts: tuple[StoredGraphEvent, ...]) -> None: ...


@dataclass(frozen=True)
class ArenaNodeProjection:
    node_id: str
    kind: str
    state: str
    attempt: int
    required_effects: tuple[str, ...]
    isolation: str
    hard_deadline_ms: int
    artifact_digests: tuple[str, ...]
    route: tuple[str, str, str, bool, str] | None
    transport: str | None


@dataclass(frozen=True)
class ArenaProjection:
    organization_id: str
    project_id: str
    run_id: str
    graph_digest: str
    plan_digest: str
    policy_digest: str
    run_state: str
    receipt_sequence: int
    receipt_head_hash: str
    nodes: tuple[ArenaNodeProjection, ...]
    edges: tuple[tuple[str, str], ...]
    levels: tuple[tuple[str, ...], ...]


def read_arena_projection(
    plan: ExecutionPlan,
    event_log: GraphEventLog,
    request: ArenaReadRequest,
    authorizer: ArenaAuthorizationPort,
    receipt_verifier: ArenaReceiptVerifierPort,
) -> ArenaProjection:
    """Build display data from one verified, authorized receipt snapshot."""
    identity = event_log.identity
    _authorize(request, identity, authorizer)
    _match_plan(identity, plan)
    snapshot = event_log.verified_snapshot()
    receipt_verifier.verify(identity, snapshot.receipts)
    latest = latest_node_states(plan, snapshot.receipts)
    if snapshot.projection.state == "SUCCEEDED" and any(value["state"] != "SUCCEEDED" for value in latest.values()):
        raise GraphIntegrityError("Arena succeeded receipt has a planned node that is not succeeded")
    bindings = {binding.binding_id: binding for binding in plan.connection_bindings}
    return ArenaProjection(
        organization_id=identity.organization_id,
        project_id=identity.project_id,
        run_id=identity.run_id,
        graph_digest=identity.graph_digest,
        plan_digest=identity.plan_digest,
        policy_digest=identity.policy_digest,
        run_state=snapshot.projection.state,
        receipt_sequence=snapshot.projection.sequence,
        receipt_head_hash=snapshot.projection.head_hash,
        nodes=tuple(_node_projection(node, latest[node.node_id], bindings) for node in plan.nodes),
        edges=tuple((edge.from_node, edge.to_node) for edge in plan.edges),
        levels=tuple(tuple(level) for level in plan.levels),
    )


def _authorize(request: ArenaReadRequest, identity: GraphRunIdentity, authorizer: ArenaAuthorizationPort) -> None:
    if not all(isinstance(value, str) and value for value in (request.subject_id, request.organization_id, request.project_id, request.run_id)):
        raise GraphIntegrityError("Arena read request is invalid")
    if (request.organization_id, request.project_id, request.run_id) != (identity.organization_id, identity.project_id, identity.run_id):
        raise GraphIntegrityError("Arena reader does not match receipt tenant")
    if not authorizer.authorize(request):
        raise GraphIntegrityError("Arena reader is unauthorized")


def _match_plan(identity: GraphRunIdentity, plan: ExecutionPlan) -> None:
    if (
        identity.graph_digest != plan.source_graph_digest
        or identity.plan_digest != plan.plan_id
        or identity.policy_digest != plan.policy_digest
    ):
        raise GraphIntegrityError("Arena receipt stream does not match immutable plan")


def latest_node_states(plan: ExecutionPlan, receipts: tuple[StoredGraphEvent, ...]) -> dict[str, dict[str, object]]:
    """Rebuild each planned node's latest receipt state, validating BOTH the per-node
    lifecycle strictly (``_ALLOWED``) AND cross-node DAG causality (a node never leaves
    PENDING before its ``plan.edges`` predecessors admit it). Shared by the Arena read
    model and the controller's resume path — so both fail closed on a tampered, fully
    re-hash-chained log that inverts node order (finding H4a)."""
    values = {node.node_id: {"state": "PENDING", "attempt": 0} for node in plan.nodes}
    nodes_by_id = {node.node_id: node for node in plan.nodes}
    predecessors = _predecessors(plan)
    for stored in receipts:
        event = stored.event
        if not event.event_type.startswith("node."):
            continue
        node_id = event.payload["node_id"]
        if node_id not in values:
            raise GraphIntegrityError("Arena receipt references a node outside the immutable plan")
        next_state = event.payload["state"]
        attempt = event.payload["attempt"]
        current = values[node_id]
        current_state = current["state"]
        current_attempt = current["attempt"]
        if (
            not isinstance(next_state, str)
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not isinstance(current_state, str)
            or isinstance(current_attempt, bool)
            or not isinstance(current_attempt, int)
            or attempt != current_attempt + 1 and current_state == "PENDING"
            or next_state not in _ALLOWED[current_state]
        ):
            raise GraphIntegrityError("Arena receipt node lifecycle is invalid")
        if attempt != current_attempt and current_state != "PENDING":
            raise GraphIntegrityError("Arena receipt node attempt sequence is invalid")
        # A node may only ever leave PENDING via READY (`_ALLOWED`); that single
        # admission edge is where cross-node causality is decided, and predecessor
        # states are monotonic thereafter, so one check here is sufficient and sound.
        if current_state == "PENDING" and next_state == "READY":
            _assert_causal_admission(nodes_by_id[node_id], predecessors[node_id], values)
        values[node_id] = dict(event.payload)
    return values


def _predecessors(plan: ExecutionPlan) -> dict[str, tuple[str, ...]]:
    """Map each planned node to its DAG predecessors (edge sources), mirroring the
    scheduler's construction in ``derive_ready_nodes``."""
    sources: dict[str, list[str]] = {node.node_id: [] for node in plan.nodes}
    for edge in plan.edges:
        sources[edge.to_node].append(edge.from_node)
    return {node_id: tuple(parents) for node_id, parents in sources.items()}


def _assert_causal_admission(
    node: PlannedNode, predecessors: tuple[str, ...], values: dict[str, dict[str, object]],
) -> None:
    """Fail closed if a node left PENDING before its DAG predecessors admitted it.

    This is the receipt-time dual of the scheduler's admission rule
    (``predecessors_admit``): the SAME predicate that lets ``derive_ready_nodes``
    dispatch a node must hold over the predecessor states rebuilt from the receipt
    sequence so far. A tampered, fully re-hash-chained log that inverts DAG order — a
    child reaching READY (and thus SUCCEEDED) before its parents — is rejected here even
    though every per-node ``_ALLOWED`` lifecycle is individually legal. Join semantics
    are honored exactly, because the check and the scheduler share one predicate."""
    parents: list[NodeState] = []
    for source in predecessors:
        state = values[source]["state"]
        if not isinstance(state, str) or state not in NodeState.__members__:
            raise GraphIntegrityError("Arena receipt node state is invalid")
        parents.append(NodeState(state))
    if not predecessors_admit(node.kind, node.approval_policy, tuple(parents)):
        raise GraphIntegrityError(
            f"Arena receipt violates DAG causality: node {node.node_id!r} left PENDING "
            "before its plan predecessors admitted it"
        )


def _node_projection(
    node: PlannedNode,
    receipt: dict[str, object],
    bindings: dict[str, ResolvedBinding],
) -> ArenaNodeProjection:
    route = _route(receipt.get("route"))
    transport = receipt.get("transport")
    if transport is not None and (not isinstance(transport, str) or not transport):
        raise GraphIntegrityError("Arena receipt transport is invalid")
    binding = bindings.get(node.binding_id) if node.binding_id else None
    _match_binding(node, binding, route, transport)
    artifacts = receipt.get("artifact_digests", ())
    state = receipt["state"]
    attempt = receipt["attempt"]
    if not isinstance(artifacts, (tuple, list)) or not all(isinstance(value, str) for value in artifacts):
        raise GraphIntegrityError("Arena receipt artifacts are invalid")
    if not isinstance(state, str) or isinstance(attempt, bool) or not isinstance(attempt, int):
        raise GraphIntegrityError("Arena receipt node state is invalid")
    return ArenaNodeProjection(
        node_id=node.node_id, kind=node.kind, state=state, attempt=attempt,
        required_effects=tuple(sorted(effect.value for effect in node.required_effects)),
        isolation=node.isolation.value, hard_deadline_ms=node.hard_deadline_ms,
        artifact_digests=tuple(artifacts), route=route, transport=transport,
    )


def _route(value: object) -> tuple[str, str, str, bool, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GraphIntegrityError("Arena receipt route is invalid")
    provider, model, region = value.get("provider_id"), value.get("model_id"), value.get("region")
    fallback, policy = value.get("fallback"), value.get("policy_digest")
    if (
        not isinstance(provider, str) or not provider
        or not isinstance(model, str) or not model
        or not isinstance(region, str) or not region
        or not isinstance(policy, str) or not policy
        or not isinstance(fallback, bool)
    ):
        raise GraphIntegrityError("Arena receipt route is invalid")
    return provider, model, region, fallback, policy


def _match_binding(
    node: PlannedNode,
    binding: ResolvedBinding | None,
    route: tuple[str, str, str, bool, str] | None,
    transport: str | None,
) -> None:
    if node.binding_id is None:
        if route is not None or transport is not None:
            raise GraphIntegrityError("Arena receipt has route or transport for an unbound node")
        return
    if binding is None:
        raise GraphIntegrityError("Arena node binding is absent from immutable plan")
    expected = (binding.provider_id, binding.model_target, binding.region, binding.fallback, binding.route_policy_digest)
    if route != expected or transport != binding.transport:
        raise GraphIntegrityError("Arena receipt route or transport does not match immutable binding")
