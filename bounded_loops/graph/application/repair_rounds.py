"""Repair rounds: the bounded outer loop that lets a downstream failure re-run an upstream node.

A ``failed``-guarded edge points FORWARD. A **repair** points BACKWARD: node C fails, the fault was
in A, so A runs again and everything reachable from A runs again after it. The data graph stays
acyclic; the repair relation does not, and that is the whole difficulty.

**Repair is modelled as a bounded outer loop, never as a cycle in the graph.** Nothing is revived.
When a node with ``on_failure: repair`` exhausts its budget, a new ROUND opens: the target and its
descendants go back to PENDING and run again. Within a round the state machine is exactly what it was
— terminal states absorb, predecessor states stay monotonic, and the single causality check at
admission is still sufficient. Across a round boundary state resets, but the boundary is an explicit
hash-chained receipt, so a verifier sees precisely where and why monotonicity restarted.

So the lifecycle is per **(node, round)**, not per node. Every rule below follows from that.

**Termination.** Total node executions are bounded by ``(1 + R) * Σ_v a_v``, where ``R`` is the
graph's GLOBAL repair budget and ``a_v`` is node ``v``'s ``max_attempts`` — the TOTAL attempts it may
make, so ``max_attempts: 1`` contributes 1, not 2.

Stated in the retry-budget notation the scheduling literature uses, where ``b_v`` counts RETRIES, that
is ``(1 + R) * Σ_v (b_v + 1)``. The two are the same quantity: ``a_v = b_v + 1``. Writing
``Σ (max_attempts + 1)`` — as this module's own docs briefly did — overstates the bound by one per
node per round, which is exactly the sort of off-by-|V| that must not reach a paper.

The bound holds given three conditions:

1. *suffix locality* — a round re-executes only the target's descendants, never the whole graph;
2. *per-round reset* — each node's retry budget resets at a boundary, or a repair accomplishes
   nothing because the node it re-runs has no attempts left;
3. *global round bound* — one budget bounds the TOTAL rounds, decremented globally and never reset.

Condition 3 carries the proof. Bound repairs per node instead and two nodes can repair each other
indefinitely, each seeing its own counter as unspent — the reset-arc construction whose soundness is
undecidable in general. A single global counter is what makes a restricted answer possible, and the
restricted answer is the contribution precisely because the general case is not decidable.
"""

from __future__ import annotations

from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import StoredGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan

#: Receipt written at a round boundary. Additive: it transitions no node, so a reader that ignores it
#: still sees a consistent stream, and the run stays RUNNING and therefore resumable mid-repair.
REPAIR_ROUND_EVENT = "run.repair.round"
#: One per node the boundary resets, recording the terminal state it is leaving so the audit trail
#: shows a terminal state was deliberately abandoned rather than corrupted.
NODE_REPAIRED_EVENT = "node.repaired"


def descendants(plan: ExecutionPlan, target: str) -> frozenset[str]:
    """``target`` and everything reachable from it — the suffix a repair round re-executes.

    Includes the target itself: repairing a node means running it again. Nodes outside this set keep
    their state, which is condition 1 of the bound — a repair must not silently redo unrelated work.
    """
    forward: dict[str, set[str]] = {}
    for edge in plan.edges:
        forward.setdefault(edge.from_node, set()).add(edge.to_node)
    seen = {target}
    frontier = [target]
    while frontier:
        for child in forward.get(frontier.pop(), ()):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return frozenset(seen)


def repair_targets(plan: ExecutionPlan) -> Mapping[str, str]:
    """Map each node that declares a repair to the ancestor it repairs.

    Read from the plan's node budgets rather than the authoring spec, because the controller and the
    replay verifier both need it and neither holds the manifest.
    """
    targets: dict[str, str] = {}
    for node in plan.nodes:
        declared = node.approval_policy.get("repair_target")
        if isinstance(declared, str) and declared:
            targets[node.node_id] = declared
    return targets


def repair_budget(plan: ExecutionPlan) -> int:
    """The graph's GLOBAL repair-round budget, 0 when repair is off."""
    for node in plan.nodes:
        declared = node.approval_policy.get("repair_budget")
        if isinstance(declared, int) and not isinstance(declared, bool):
            return declared
    return 0


def total_execution_bound(plan: ExecutionPlan) -> int:
    """``(1 + R) * Σ_v (b_v + 1)`` — the tight bound on node executions for the whole run.

    Sums ``max_attempts`` directly, which is what the controller actually spends: ``run_graph``
    iterates ``range(1, max_attempts + 1)``. See the module docstring on why ``Σ (max_attempts + 1)``
    would be wrong.

    This is the quantity arXiv:2604.11378 Theorem 6.2 gets wrong under repair. Its bound is
    ``Σ_v τ_v·(b_v + 1)`` — the same per-round total as ours — but its proof depends on terminal
    states being absorbing. A repair breaks that premise, so the true bound carries the ``(1 + R)``
    factor; with ``R`` unbounded their bound does not exist at all. The disagreement is about the
    FACTOR, not the per-round sum, and the paper must say so precisely or it is attacking a straw man.
    """
    per_round = 0
    for node in plan.nodes:
        attempts = node.budgets.get("max_attempts")
        per_round += (attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 1)
    return (1 + repair_budget(plan)) * per_round


def assert_boundary_is_legal(
    plan: ExecutionPlan,
    *,
    round_index: int,
    trigger_node: str,
    target_node: str,
    trigger_state: str,
) -> None:
    """Refuse a repair-round boundary a run could not legitimately have opened.

    This is the whole reason a boundary may reset state at all. Without it, a forged but correctly
    re-hash-chained log could write one boundary and erase any inconvenient failure — the terminal
    states it resets are exactly the evidence a reader relies on.

    Four checks, mirroring the authoring rules so the runtime cannot be laxer than the validator:
    the trigger must have FAILED, it must actually declare a repair, the target must be the one it
    declared, and the round must be inside the global budget.
    """
    if trigger_state != "FAILED":
        raise GraphIntegrityError(
            f"repair round {round_index} claims node {trigger_node!r} triggered it, but that node "
            f"is {trigger_state}, not FAILED"
        )
    declared = repair_targets(plan).get(trigger_node)
    if declared is None:
        raise GraphIntegrityError(
            f"repair round {round_index} was triggered by node {trigger_node!r}, which declares no "
            "repair policy"
        )
    if declared != target_node:
        raise GraphIntegrityError(
            f"repair round {round_index} repairs {target_node!r} but node {trigger_node!r} declares "
            f"{declared!r}"
        )
    budget = repair_budget(plan)
    if not 1 <= round_index <= budget:
        raise GraphIntegrityError(
            f"repair round {round_index} exceeds the graph's global repair budget of {budget}"
        )


def rounds_spent(receipts: tuple[StoredGraphEvent, ...]) -> int:
    """How many repair rounds this run has already opened, counted from the RECEIPTS.

    Derived from the log rather than held in memory so a resumed run cannot start its budget over.
    That is the whole point of the budget being global: an in-memory counter would reset on every
    process restart, and a crash-loop could then repair without limit.
    """
    return sum(1 for stored in receipts if stored.event.event_type == REPAIR_ROUND_EVENT)


def next_repair_round(
    plan: ExecutionPlan,
    states: Mapping[str, str],
    receipts: tuple[StoredGraphEvent, ...],
) -> tuple[str, str, int] | None:
    """``(trigger, target, round_index)`` for the round to open now, or ``None``.

    Pure, so the decision can be tested without a run directory, an event log or a clock. The caller
    writes the receipts and applies the reset.

    Rounds are counted from the RECEIPTS, never from memory: an in-memory counter resets on every
    process restart, so a crash-loop could repair without limit. Nodes are considered in a stable
    order so two runs of the same failed graph open the same round.
    """
    budget = repair_budget(plan)
    if not budget:
        return None
    targets = repair_targets(plan)
    if not targets:
        return None
    spent = rounds_spent(receipts)
    if spent >= budget:
        return None
    for node in sorted(plan.nodes, key=lambda value: value.node_id):
        target = targets.get(node.node_id)
        if target is not None and states.get(node.node_id) == "FAILED":
            return node.node_id, target, spent + 1
    return None
