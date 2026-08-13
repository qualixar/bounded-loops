"""Pure deterministic readiness and dispatch-state transitions."""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from bounded_loops.graph.application.edge_guards import DEFAULT_GUARD, EdgeGuard
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


class Admission(str, Enum):
    """What a node's incoming edges say about whether it may leave PENDING.

    Three-valued because an edge guard introduces an outcome the old boolean could not express: a
    node every one of whose incoming edges was explicitly guarded AND excluded is not "waiting" —
    its branch was not taken, and it must be SKIPPED rather than left PENDING forever.
    """

    #: Predecessors permit dispatch.
    ADMIT = "ADMIT"
    #: Stay PENDING — either a predecessor has not reached a terminal state yet, or an UNGUARDED
    #: dependency failed. The second case is the pre-guard behaviour and must be preserved: a hard
    #: dependency failure stops the run, it does not skip past it.
    BLOCK = "BLOCK"
    #: Every incoming edge was explicitly guarded and excluded. This branch was not taken.
    SKIP = "SKIP"


#: One incoming edge as the scheduler sees it: the source node's state, and the edge's authored
#: guard (``None`` for an unguarded edge).
Parent = tuple[NodeState, str | None]


def derive_ready_nodes(plan: ExecutionPlan, states: Mapping[str, NodeState]) -> tuple[str, ...]:
    """Return pending nodes whose declared predecessor semantics are decidable."""
    return _pending_with_admission(plan, states, Admission.ADMIT)


def derive_skipped_nodes(plan: ExecutionPlan, states: Mapping[str, NodeState]) -> tuple[str, ...]:
    """Return pending nodes whose every incoming edge was explicitly guarded and excluded.

    Separate from ``derive_ready_nodes`` because these nodes are not dispatched — they transition
    straight to SKIPPED. Without this the first conditional edge would leave its untaken branch
    PENDING forever and the run could never reach a terminal projection.
    """
    return _pending_with_admission(plan, states, Admission.SKIP)


def _pending_with_admission(
    plan: ExecutionPlan, states: Mapping[str, NodeState], wanted: Admission,
) -> tuple[str, ...]:
    plan_ids = {node.node_id for node in plan.nodes}
    if set(states) != plan_ids:
        raise GraphValidationError("scheduler_state", "/states", "state map must contain exactly the plan node IDs")
    predecessors: dict[str, list[tuple[str, str | None]]] = {node_id: [] for node_id in plan_ids}
    for edge in plan.edges:
        # The edge's guard travels WITH its source. Discarding it here is what made ``when`` a
        # silent no-op: the scheduler only ever saw a list of parent ids.
        predecessors[edge.to_node].append((edge.from_node, edge.when))
    selected: list[str] = []
    for node in sorted(plan.nodes, key=lambda value: value.node_id):
        if states[node.node_id] is not NodeState.PENDING:
            continue
        parents = tuple(
            (states[parent], guard) for parent, guard in predecessors[node.node_id]
        )
        if predecessors_admission(node.kind, node.approval_policy, parents) is wanted:
            selected.append(node.node_id)
    return tuple(selected)


def dispatch_node(states: Mapping[str, NodeState], node_id: str) -> dict[str, NodeState]:
    """Move exactly one READY node to STARTING and reject duplicate dispatch."""
    if states.get(node_id) is not NodeState.READY:
        raise GraphValidationError("dispatch_state", f"/states/{node_id}", "node must be READY before dispatch")
    next_states = dict(states)
    next_states[node_id] = NodeState.STARTING
    return next_states


def guard_satisfied(guard: str | None, state: NodeState) -> bool | None:
    """Whether one edge's guard is met by its source's state. ``None`` means not yet decidable.

    A guard can only be evaluated once the source is TERMINAL — a running node's outcome is not yet
    knowable, and guessing would admit a node before its dependency resolved.

    ``failed`` matches FAILED and nothing else. HALTED, CANCELLED and EXPIRED are run-level stops
    (a fail-closed halt, an operator cancel, a deadline), and a repair or recovery path must not fire
    on those — so they leave both ``succeeded`` and ``failed`` unsatisfied and the halt propagates.
    ``terminal`` is the guard for "whatever happened".
    """
    if state not in _TERMINAL:
        return None
    resolved = DEFAULT_GUARD if guard is None else EdgeGuard(guard)
    if resolved is EdgeGuard.TERMINAL:
        return True
    if resolved is EdgeGuard.SUCCEEDED:
        return state is NodeState.SUCCEEDED
    if resolved is EdgeGuard.FAILED:
        return state is NodeState.FAILED
    return state is NodeState.SKIPPED


def predecessors_admit(kind: str, policy: Mapping[str, object], parents: tuple[Parent, ...]) -> bool:
    """Whether a node's predecessors permit it to leave PENDING *for dispatch*.

    Boolean face of ``predecessors_admission`` for callers that only ask "is it ready".
    """
    return predecessors_admission(kind, policy, parents) is Admission.ADMIT


def predecessors_admission(
    kind: str, policy: Mapping[str, object], parents: tuple[Parent, ...],
) -> Admission:
    """The single source of truth for cross-node DAG causality.

    ``derive_ready_nodes`` uses it at run time to admit a node; the receipt read model
    (``arena_projection.latest_node_states``) reuses it to verify, at replay time, that a rebuilt
    receipt stream never admitted a node before its predecessors did — so the scheduler and the
    verifier can never disagree about causality.

    **A guard FILTERS which edges participate; it never overrides the decision below.** Only an
    edge carrying an EXPLICIT guard may be excluded. An unguarded edge always participates and is
    judged exactly as it was before guards existed.

    That asymmetry is load-bearing, and two regressions proved it. Treating an unsatisfied *unguarded*
    edge as excluded turns a hard dependency failure into a green light — an unguarded node with one
    SUCCEEDED and one FAILED parent would keep the survivor and dispatch. Treating it as a BLOCK
    instead pre-empts the join modes, which is just as wrong: ``all_selected`` exists precisely to
    tolerate a failed parent, and ``any_successful`` is meant to admit as soon as one parent succeeds
    without waiting for the rest to finish. Both of those are the join's call, not the guard's.

    So an unguarded edge is handed to the join logic untouched, and only an explicit guard can remove
    an edge from consideration or hold the decision open.
    """
    if not parents:
        return Admission.ADMIT
    live: list[Parent] = []
    for state, guard in parents:
        if guard is None:
            # Participates unconditionally — the join mode (or the all-successful rule below)
            # decides what its state means, exactly as before guards existed.
            live.append((state, None))
            continue
        satisfied = guard_satisfied(guard, state)
        if satisfied is None:
            return Admission.BLOCK  # explicit guard on an unfinished source: not yet decidable
        if satisfied:
            live.append((state, guard))
        # else: explicitly guarded and excluded — this path was not taken, so it does not block.
    if not live:
        # Every incoming edge was explicitly guarded and excluded.
        return Admission.SKIP
    if kind != "join":
        # An explicitly-guarded live edge already asserted the outcome it required, so re-checking it
        # for SUCCEEDED would contradict a ``failed`` guard — the entire point of failure routing.
        # An unguarded live edge still has to have succeeded.
        return (
            Admission.ADMIT
            if all(guard is not None or state is NodeState.SUCCEEDED for state, guard in live)
            else Admission.BLOCK
        )
    mode = policy.get("join_mode")
    states = tuple(state for state, _ in live)
    if mode == "all_selected":
        return Admission.ADMIT if all(state in _TERMINAL for state in states) else Admission.BLOCK
    if mode == "all_successful":
        return (
            Admission.ADMIT if all(state is NodeState.SUCCEEDED for state in states)
            else Admission.BLOCK
        )
    if mode == "any_successful":
        return (
            Admission.ADMIT if any(state is NodeState.SUCCEEDED for state in states)
            else Admission.BLOCK
        )
    raise GraphValidationError("join_mode", "/approval_policy/join_mode", "compiled join mode is missing")
