"""The human checkpoint: apply a recorded approval decision to an approval node.

Extracted from the controller for the 800-line cap, and it earns its own module: an approval node
runs no worker and never retries, so it shares none of the bounded-loop attempt machinery around it.
The human decision IS the independent gate for this node kind.

Collaborators are passed explicitly rather than reaching into a controller, so the failure-sealing
policy stays owned by the controller — this module decides what the decision MEANS, not what a run
does about it.
"""

from __future__ import annotations

from typing import Callable, Protocol

from bounded_loops.graph.application.node_contracts import (
    ApprovalOutcome,
    ApprovalResolverPort,
)
from bounded_loops.graph.application.schedule_ready import NodeState
from bounded_loops.graph.domain.events import (
    GraphRunIdentity,
    GraphRunProjection,
    NodeFailureCause,
)
from bounded_loops.graph.domain.plan import PlannedNode


class _Receipts(Protocol):
    def append_node(
        self, node_id: str, event_type: str, state: str, *, attempt: int = 1, **extra: object,
    ) -> None: ...


class _Fail(Protocol):
    def __call__(
        self, states: dict[str, NodeState], node_id: str, reason: str,
        *, cause: NodeFailureCause,
    ) -> GraphRunProjection: ...


def resolve_approval(
    states: dict[str, NodeState],
    node_id: str,
    node: PlannedNode,
    *,
    resolver: ApprovalResolverPort | None,
    receipts: _Receipts,
    identity: GraphRunIdentity,
    fail: _Fail,
    projection: Callable[[], GraphRunProjection],
) -> GraphRunProjection | None:
    """Apply the recorded human decision for an approval node.

    Returns a projection when the run must STOP — a fail-closed failure (no resolver, a rejection,
    or a malformed outcome) or a durable pause (no decision yet, so the node is left
    AWAITING_APPROVAL and the run stays resumable). Returns ``None`` when the node was approved: its
    ``node.succeeded`` receipt is written (the human decision is the independent gate) and the caller
    drives the remaining nodes.

    Every failure here is a HALT cause — a rejection or broken approval machinery is never something
    a graph may route around, or the approval gate would mean nothing.
    """
    # An approval node ALWAYS records that it reached the human gate first, so every terminal
    # transition is AWAITING_APPROVAL -> {SUCCEEDED, FAILED} — the node never fails or succeeds
    # straight from READY, which would be an unprojectable receipt (READY has no terminal edge). It
    # also keeps the honest story: the receipt shows the node required human approval before its
    # outcome.
    states[node_id] = NodeState.AWAITING_APPROVAL
    receipts.append_node(node_id, "node.awaiting_approval", NodeState.AWAITING_APPROVAL.value)
    if resolver is None:
        return fail(
            states, node_id, "approval node reached without an approval resolver",
            cause=NodeFailureCause.APPROVAL_UNRESOLVED,
        )
    try:
        outcome = resolver.resolve(
            identity=identity, node=node, attempt=1,
            # attempt=1: approval nodes do not retry (ARCH-09); only the approval decision itself
            # is durable. Multi-attempt retry tracking would require a separate event kind and is
            # not supported at this layer.
        )
    except Exception:
        # Consistent with worker/gate/policy failures: a resolver error fails the run closed
        # (durable FAILED) rather than escaping as an uncaught exception.
        return fail(
            states, node_id, "approval resolver evaluation failed",
            cause=NodeFailureCause.APPROVAL_UNRESOLVED,
        )
    if outcome is ApprovalOutcome.APPROVED:
        states[node_id] = NodeState.SUCCEEDED
        receipts.append_node(
            node_id, "node.succeeded", NodeState.SUCCEEDED.value, artifact_digests=[],
        )
        return None
    if outcome is ApprovalOutcome.REJECTED:
        return fail(
            states, node_id, "human approval was rejected",
            cause=NodeFailureCause.APPROVAL_REJECTED,
        )
    if outcome is not ApprovalOutcome.PENDING:
        return fail(
            states, node_id, "approval resolver returned an invalid outcome",
            cause=NodeFailureCause.APPROVAL_UNRESOLVED,
        )
    # PENDING: no decision yet — stay paused; the hold receipt is already durable.
    return projection()
