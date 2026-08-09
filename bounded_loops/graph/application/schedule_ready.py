"""Pure deterministic readiness and dispatch-state transitions."""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan


class NodeState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    GATING = "GATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    HALTED = "HALTED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"


_TERMINAL = frozenset({
    NodeState.SUCCEEDED, NodeState.FAILED, NodeState.HALTED, NodeState.CANCELLED,
    NodeState.SKIPPED, NodeState.EXPIRED,
})


def derive_ready_nodes(plan: ExecutionPlan, states: Mapping[str, NodeState]) -> tuple[str, ...]:
    """Return pending nodes whose declared predecessor semantics are decidable."""
    plan_ids = {node.node_id for node in plan.nodes}
    if set(states) != plan_ids:
        raise GraphValidationError("scheduler_state", "/states", "state map must contain exactly the plan node IDs")
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in plan_ids}
    for edge in plan.edges:
        predecessors[edge.to_node].append(edge.from_node)
    ready: list[str] = []
    for node in sorted(plan.nodes, key=lambda value: value.node_id):
        if states[node.node_id] is not NodeState.PENDING:
            continue
        parents = tuple(states[parent] for parent in predecessors[node.node_id])
        if _ready_for(node.kind, node.approval_policy, parents):
            ready.append(node.node_id)
    return tuple(ready)


def dispatch_node(states: Mapping[str, NodeState], node_id: str) -> dict[str, NodeState]:
    """Move exactly one READY node to STARTING and reject duplicate dispatch."""
    if states.get(node_id) is not NodeState.READY:
        raise GraphValidationError("dispatch_state", f"/states/{node_id}", "node must be READY before dispatch")
    next_states = dict(states)
    next_states[node_id] = NodeState.STARTING
    return next_states


def _ready_for(kind: str, policy: Mapping[str, object], parents: tuple[NodeState, ...]) -> bool:
    if not parents:
        return True
    if kind != "join":
        return all(parent is NodeState.SUCCEEDED for parent in parents)
    mode = policy.get("join_mode")
    if mode == "all_selected":
        return all(parent in _TERMINAL for parent in parents)
    if mode == "all_successful":
        return all(parent is NodeState.SUCCEEDED for parent in parents)
    if mode == "any_successful":
        return any(parent is NodeState.SUCCEEDED for parent in parents)
    raise GraphValidationError("join_mode", "/approval_policy/join_mode", "compiled join mode is missing")
