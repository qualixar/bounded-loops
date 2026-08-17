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

from typing import Mapping, Protocol

from bounded_loops.graph.application.failure_policy import MAY_CONTINUE_AFTER
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import NodeFailureCause, StoredGraphEvent
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


def gated_effects_for_approval(plan: ExecutionPlan, approval_node_id: str) -> frozenset[Effect]:
    """Union of effects of all nodes reachable from *approval_node_id*, excluding itself.

    Uses REACHABILITY — a safe over-approximation: the human authorising this gate is informed
    about every effect downstream, never fewer. For a two-approval chain (A→X→B→Y), A's set
    includes X, B and Y effects; B's includes only Y effects.
    """
    reachable = descendants(plan, approval_node_id)
    nodes_by_id = {n.node_id: n for n in plan.nodes}
    result: frozenset[Effect] = frozenset()
    for node_id in reachable:
        if node_id != approval_node_id:
            node = nodes_by_id.get(node_id)
            if node is not None:
                result = result | node.required_effects
    return result


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
    """The graph's GLOBAL repair-round budget, 0 when repair is off.

    Stored per node — the compiler copies `graph.policies.repair_budget` onto every
    node — but read as a single global number, because condition 3 above is what
    carries the termination proof. ENG-05: this used to `return` on the first node
    carrying a value, so if the compiler ever stopped distributing uniformly the run
    would silently adopt one node's budget as the graph's. That is precisely the
    per-node bound the proof forbids, arrived at by accident rather than by design, and
    it would not have shown up as an error anywhere — the run would simply have a
    different bound than the one `bl graph plan` printed.

    So disagreement is refused rather than resolved. There is no correct way to pick
    among conflicting global budgets, and picking the first is only defensible while
    nobody looks.
    """
    # Bound once per node rather than tested through `.get` and read through `[]`. Those were
    # two expressions for the same key, so the isinstance guard narrowed one and the element
    # kept the declared `object` type — correct at runtime, unprovable to a reader or a checker.
    declared: set[int] = set()
    for node in plan.nodes:
        budget = node.approval_policy.get("repair_budget")
        # `bool` is a subclass of `int`, and `repair_budget: true` must not read as a budget of 1.
        if isinstance(budget, int) and not isinstance(budget, bool):
            declared.add(budget)
    if not declared:
        return 0
    if len(declared) > 1:
        raise GraphIntegrityError(
            "nodes declare different repair budgets "
            f"({sorted(declared)}); the repair budget is GLOBAL — one counter bounds the "
            "total rounds for the whole graph. A per-node budget lets two nodes repair "
            "each other indefinitely with both counters showing credit, which is the "
            "construction the termination bound excludes. Recompile the plan."
        )
    return declared.pop()


def total_execution_bound(plan: ExecutionPlan) -> int:
    """``(1 + R) * Σ_v (b_v + 1)`` — the tight bound on node executions for the whole run.

    Sums ``max_attempts`` directly, which is what the controller actually spends: ``run_graph``
    iterates ``range(1, max_attempts + 1)``. See the module docstring on why ``Σ (max_attempts + 1)``
    would be wrong.

    Relation to arXiv:2604.11378 Theorem 6.2, stated precisely because the paper will be read by a
    referee. Their bound is ``Σ_v τ_v·(b_v + 1)`` — the same per-round total as ours — and it is
    CORRECT on its own premise, which they state explicitly: terminal states are absorbing, and
    parent-chain rollback is excluded from their design. So their theorem is not mistaken. What is
    true is narrower and must be written that way: **once repair is added their premise no longer
    holds, so their bound no longer applies to the run** — and under our repair semantics the tight
    bound carries the ``(1 + R)`` factor. Saying they "got it wrong" would imply they claimed to cover
    repair, which they did not; that is a straw man and a referee would say so.
    Flagged by the P4.25 dual audit (Muse finding 5).
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
    trigger_cause: object = None,
) -> None:
    """Refuse a repair-round boundary a run could not legitimately have opened.

    This is the whole reason a boundary may reset state at all. Without it, a forged but correctly
    re-hash-chained log could write one boundary and erase any inconvenient failure — the terminal
    states it resets are exactly the evidence a reader relies on.

    Five checks, mirroring the authoring and continuation rules so the runtime cannot be laxer than
    the validator: the trigger must have FAILED, its failure must have been CONTINUE-ELIGIBLE, it must
    actually declare a repair, the target must be the one it declared, and the round must be inside the
    global budget.

    The cause check closes a real hole. Checking only the STATE let a hand-chained log repair past a
    HALT-class failure — a broken gate, a denied policy or isolation refusal, a rejected approval, an
    exhausted spend cap — and so rewind a run the live controller would have sealed. The live path
    could not do it, because a halting cause never reaches a repair; replay accepted it. Found by the
    P4.25 dual audit (Muse finding 3).

    A missing cause is REFUSED, not waved through: pre-0.5 logs have no repair boundaries at all, so
    an absent cause on one can only mean a forgery or a corruption.
    """
    if trigger_state != "FAILED":
        raise GraphIntegrityError(
            f"repair round {round_index} claims node {trigger_node!r} triggered it, but that node "
            f"is {trigger_state}, not FAILED"
        )
    if not isinstance(trigger_cause, str) or not trigger_cause:
        raise GraphIntegrityError(
            f"repair round {round_index} names trigger {trigger_node!r} with no recorded failure "
            "cause; a repair may only follow a failure whose cause is known"
        )
    try:
        cause = NodeFailureCause(trigger_cause)
    except ValueError:
        raise GraphIntegrityError(
            f"repair round {round_index} names an unknown failure cause {trigger_cause!r}"
        ) from None
    if cause not in MAY_CONTINUE_AFTER:
        raise GraphIntegrityError(
            f"repair round {round_index} was triggered by a {trigger_cause!r} failure, which stops "
            "the run whatever the fail mode; only a node's own bounded-loop outcome may be repaired"
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


class RoundWriter(Protocol):
    """The receipt-writing surface a boundary needs. Keeps this module free of the writer class."""

    def open_repair_round(
        self, *, round_index: int, target_node: str, trigger_node: str, reason: str,
    ) -> None: ...

    def append_node_repaired(self, node_id: str, from_state: str) -> None: ...


def write_repair_boundary(
    plan: ExecutionPlan,
    receipts: RoundWriter,
    states: dict[str, str],
    *,
    trigger: str,
    target: str,
    round_index: int,
) -> tuple[str, ...]:
    """Record the boundary and return the nodes to reset, in a stable order.

    The caller applies the reset to its own state map. Suffix locality lives here: only
    ``descendants(target)`` is recorded and returned, so a boundary can never quietly redo unrelated
    work — condition 1 of the termination bound.
    """
    receipts.open_repair_round(
        round_index=round_index, target_node=target, trigger_node=trigger,
        reason=f"node {trigger!r} failed; repairing {target!r}",
    )
    reset = tuple(sorted(descendants(plan, target)))
    for node_id in reset:
        receipts.append_node_repaired(node_id, states[node_id])
    return reset
