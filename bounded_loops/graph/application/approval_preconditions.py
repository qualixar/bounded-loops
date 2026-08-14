"""What must be true before a human decision is written to the ledger.

Both checks answer the same question from different sides — "is this decision one the run can
actually hold?" — and both must run before any durable write, because the failure they prevent
is a ledger that cannot be replayed rather than a request that gets refused.

Extracted from `LocalGraphRuntimeFacade`, where they were methods that never touched `self`.
Nothing about them needs a facade: they read a plan and a directory and raise. Keeping them
there was costing the facade lines against its cap while hiding that they are testable alone.
"""

from __future__ import annotations

from pathlib import Path

from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.application.approval_ledger import _load_approvals
from bounded_loops.graph.domain.authoring import NodeKind


def require_approval_node(plan: ExecutionPlan, node_id: str) -> PlannedNode:
    """The APPROVAL node named `node_id`, or fail closed.

    A bogus or non-approval node_id must never reach a durable write: the receipt would name a
    node the plan cannot resolve, and every future resume would wedge on replaying it.
    """
    node = next((candidate for candidate in plan.nodes if candidate.node_id == node_id), None)
    if node is None:
        raise GraphIntegrityError(f"approval node {node_id!r} not found in plan")
    if node.kind != NodeKind.APPROVAL.value:
        raise GraphValidationError(
            "approval_node", "/node_id", f"node {node_id!r} is not an approval node"
        )
    return node


def guard_decision_conflict(run_dir: Path, node_id: str, decision: str) -> None:
    """Refuse a decision that contradicts one already durably recorded for this node.

    The ledger must never hold both an approval and a rejection for the same node: a reader
    cannot tell which one governed, and the whole value of the record is that it can.
    """
    record = _load_approvals(run_dir / "approvals.json")
    has_approval = any(
        commit.get("node_id") == node_id for commit in record.get("commits", [])
    )
    has_rejection = any(
        rejection.get("node_id") == node_id for rejection in record.get("rejections", [])
    )
    if decision == "approved" and has_rejection:
        raise GraphIntegrityError(
            f"cannot approve node {node_id!r}: a durable rejection already exists"
        )
    if decision == "rejected" and has_approval:
        raise GraphIntegrityError(
            f"cannot reject node {node_id!r}: a durable approval already exists"
        )
