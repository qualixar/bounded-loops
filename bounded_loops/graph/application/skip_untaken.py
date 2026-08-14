"""Which branches were not taken — computed purely, before any receipt is written.

A node whose every incoming edge was explicitly guarded and EXCLUDED must be retired, or it stays
PENDING for ever: it is never ready, so the controller's loop would report a run FAILED with a node
still pending. Skipping is therefore not cosmetic — it is what lets a conditional graph terminate.

The cascade is computed here in one pure pass so the controller only applies effects. Keeping the
whole cascade pure also makes it testable without a run directory, an event log, or a clock.
"""

from __future__ import annotations

from typing import Mapping

from bounded_loops.graph.application.schedule_ready import NodeState, derive_skipped_nodes
from bounded_loops.graph.domain.plan import ExecutionPlan


def untaken_branches(
    plan: ExecutionPlan, states: Mapping[str, NodeState],
) -> tuple[tuple[str, str], ...]:
    """Every node whose branch was not taken, in cascade order, each with its reason.

    Driven to a FIXPOINT because a skip propagates: skipping A leaves a ``succeeded``-guarded edge
    out of A unsatisfied, which can skip B, and so on to the end of the branch. A single pass would
    strand the tail of a multi-node branch in PENDING — the exact hang this function prevents.

    Computed over a private copy, so a caller's state map is never mutated behind its back.
    """
    working = dict(states)
    cascade: list[tuple[str, str]] = []
    while True:
        batch = derive_skipped_nodes(plan, working)
        if not batch:
            return tuple(cascade)
        for node_id in batch:
            cascade.append((node_id, untaken_reason(plan, node_id, working)))
            working[node_id] = NodeState.SKIPPED


def untaken_reason(
    plan: ExecutionPlan, node_id: str, states: Mapping[str, NodeState],
) -> str:
    """Name the guarded predecessors that excluded this node, so a skip is explainable.

    An unexplained skip is indistinguishable from a scheduler bug when the run is read back weeks
    later, which is why ``node.skipped`` requires a reason rather than accepting one.
    """
    excluded = sorted(
        f"{edge.from_node} is {states[edge.from_node].value} (guard {edge.when!r})"
        for edge in plan.edges
        if edge.to_node == node_id and edge.when is not None
    )
    return "branch not taken: " + "; ".join(excluded)
