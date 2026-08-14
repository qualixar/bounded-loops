"""In-process join worker and its independent receipt gate.

A join node records causality — the declared mode and the set of predecessors whose
states admitted it — into a content-addressed receipt. The gate re-reads that receipt
and refuses it if the mode or predecessor set disagrees with the plan.

**Why in-process (no sandboxed subprocess)?**
A join computes nothing from untrusted input. Its inputs are the plan's declared edges
and the mode the author wrote; both arrive through the controller's own data structures,
not from an external process. An in-process worker writing directly through the artifact
store is therefore both sufficient and correct.

If a future join variant consumed upstream node OUTPUT (e.g., to merge data from
predecessor artifacts), a sandboxed worker would be required to contain that output
from the rest of the process — the sandbox decision must be revisited at that point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO

from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.graph_ports import ArtifactReaderPort, ArtifactStorePort
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


@dataclass(frozen=True)
class JoinNodeWorker:
    """In-process join worker: records causality as a content-addressed receipt.

    The receipt encodes which mode was enforced and which predecessor node IDs
    the scheduler admitted. The gate verifies both match the plan, so a stale or
    mismatched receipt cannot satisfy this node.
    """

    store: ArtifactStorePort
    organization_id: str
    project_id: str

    def execute(
        self,
        *,
        plan: ExecutionPlan,
        node: PlannedNode,
        envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        predecessor_node_ids = sorted(
            edge.from_node for edge in plan.edges if edge.to_node == node.node_id
        )
        receipt: dict[str, object] = {
            "node_id": node.node_id,
            "join_mode": node.approval_policy.get("join_mode"),
            "predecessor_node_ids": predecessor_node_ids,
        }
        payload = json.dumps(receipt, sort_keys=True).encode("utf-8")
        policy = ArtifactPolicy(
            organization_id=self.organization_id,
            project_id=self.project_id,
            producer_attempt=str(attempt),
            media_type="application/json",
            sensitivity="internal",
            retention_class="standard",
        )
        record = self.store.put(BytesIO(payload), policy)
        return WorkerResult((record.digest,))


class JoinReceiptGate:
    """Independent gate for a join node: verifies the causality receipt against the plan.

    Provenance (node identity) is checked BEFORE mode and predecessor set, so a
    receipt from a different node cannot pass by coincidentally recording the right mode.
    """

    def __init__(
        self,
        store: ArtifactReaderPort,
        *,
        organization_id: str,
        project_id: str,
    ) -> None:
        self._store = store
        self._organization_id = organization_id
        self._project_id = project_id

    def evaluate(
        self,
        *,
        plan: ExecutionPlan,
        node: PlannedNode,
        result: WorkerResult,
    ) -> GateVerdict:
        digests = result.output_artifact_digests
        if not digests:
            return GateVerdict(False, f"join node {node.node_id!r} produced no causality receipt")
        ref = ArtifactRef(digests[0], self._organization_id, self._project_id)
        access = ArtifactAccess(self._organization_id, self._project_id)
        try:
            with self._store.open(ref, access) as handle:
                payload = handle.read()
        except Exception as exc:  # noqa: BLE001 — an unreadable receipt is a closed gate
            return GateVerdict(False, f"join node {node.node_id!r} receipt unreadable: {exc}")
        try:
            receipt = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return GateVerdict(
                False, f"join node {node.node_id!r} receipt is not valid JSON: {exc}",
            )
        if not isinstance(receipt, dict):
            return GateVerdict(
                False, f"join node {node.node_id!r} receipt is not a JSON object",
            )

        # Verify node identity BEFORE mode — a receipt from a different node must never
        # pass merely because it happens to record the same join mode.
        if receipt.get("node_id") != node.node_id:
            return GateVerdict(
                False,
                f"join receipt names node {receipt.get('node_id')!r}, not {node.node_id!r}",
            )

        # Mode must match the plan's declared join mode.
        plan_mode = node.approval_policy.get("join_mode")
        receipt_mode = receipt.get("join_mode")
        if receipt_mode != plan_mode:
            return GateVerdict(
                False,
                f"join node {node.node_id!r}: receipt records mode {receipt_mode!r} "
                f"but plan declares {plan_mode!r}",
                evidence_digest=digests[0],
            )

        # Predecessor set must exactly match the plan's incoming edges for this node.
        plan_predecessors = frozenset(
            edge.from_node for edge in plan.edges if edge.to_node == node.node_id
        )
        receipt_predecessors = frozenset(receipt.get("predecessor_node_ids") or [])
        if receipt_predecessors != plan_predecessors:
            return GateVerdict(
                False,
                f"join node {node.node_id!r}: receipt records predecessors "
                f"{sorted(receipt_predecessors)!r} but plan has {sorted(plan_predecessors)!r}",
                evidence_digest=digests[0],
            )

        return GateVerdict(
            True,
            f"join causality verified: mode={plan_mode!r}, "
            f"predecessors={sorted(plan_predecessors)!r}",
            evidence_digest=digests[0],
        )
