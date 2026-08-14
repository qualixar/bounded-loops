"""Rebuild controller state for a RESUME from the receipts a previous run left behind.

Extracted from the controller for the 800-line cap. It earns its own module: this is the one place
that decides what a resumed run re-drives and what it accepts as already settled, and getting it
wrong either repeats an irreversible effect or silently re-opens a decision the log already recorded.
"""

from __future__ import annotations

from bounded_loops.graph.application.node_spend import EFFECTFUL_EFFECTS
from bounded_loops.graph.application.schedule_ready import NodeState
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import ExecutionPlan

#: States a resume treats as SETTLED — carried across unchanged rather than re-driven.
#: SUCCEEDED and SKIPPED always; FAILED only when the run continues past node failures.
_SETTLED = {"SUCCEEDED": NodeState.SUCCEEDED, "SKIPPED": NodeState.SKIPPED}


def states_from_receipts(
    plan: ExecutionPlan,
    latest: dict[str, dict[str, object]],
    *,
    continue_on_failure: bool,
) -> dict[str, NodeState]:
    """Map rebuilt receipt states to controller states.

    A settled node is carried across; every other node re-drives from PENDING. An EFFECTFUL node
    interrupted mid-execution (STARTING/RUNNING/GATING) cannot be re-driven safely without a resume
    idempotency key (ADR-12 D7), so it fails closed rather than risk a double external or
    irreversible effect.

    ``SKIPPED`` is settled for the same reason ``SUCCEEDED`` is: its branch was not taken, and
    resetting it to PENDING re-opens a decision the log already recorded.

    ``FAILED`` depends on the fail mode, and the distinction is load-bearing:

    * halting mode — a FAILED node means the run is over, and ``resume`` finalizes it before ever
      reaching here. The raise below is a defensive guard for that unreachable case.
    * continuing mode — a FAILED node is a settled OUTCOME, not the end of the run: a
      failure-conditioned branch or an independent branch may still have work. Raising here would
      make ``continue_declared`` work on a fresh run and then break the moment the run was resumed.
      Found while fixing the resume seal the P4.25a dual audit (Grok) reported — one bug's fix
      exposed the next.
    """
    settled = dict(_SETTLED)
    if continue_on_failure:
        settled["FAILED"] = NodeState.FAILED
    states: dict[str, NodeState] = {}
    for node in plan.nodes:
        observed = latest[node.node_id]["state"]
        carried = settled.get(str(observed))
        if carried is not None:
            states[node.node_id] = carried
            continue
        if observed == "FAILED":
            raise GraphIntegrityError(
                f"cannot resume: node {node.node_id!r} has already failed"
            )
        if observed == "AWAITING_APPROVAL":
            # Paused for a human decision: no worker ran and no effect fired (approval GATES the
            # effect), so re-driving only re-consults the decision. Safe even for an effectful
            # approval node.
            states[node.node_id] = NodeState.PENDING
            continue
        if observed in ("STARTING", "RUNNING", "GATING") and (
            node.required_effects & EFFECTFUL_EFFECTS
        ):
            raise GraphIntegrityError(
                f"cannot safely resume: node {node.node_id!r} carries an external / irreversible "
                "effect and was interrupted mid-execution; a resume idempotency key (D7) is "
                "required before re-driving it"
            )
        states[node.node_id] = NodeState.PENDING
    return states
