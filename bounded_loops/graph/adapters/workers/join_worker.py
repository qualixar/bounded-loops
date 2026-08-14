"""In-process join worker and its independent receipt gate.

A join node records causality — the declared mode and, for each incoming edge, the LIVE state of
the source node and that edge's guard — into a content-addressed receipt. The gate re-reads the
receipt and replays the real admission predicate over it, refusing the node if the recorded facts
would not have admitted it under the recorded mode.

**Why live states rather than the plan's edge list.** The first version recorded
``sorted(edge.from_node for edge in plan.edges if edge.to_node == node)`` — every incoming edge
source, straight from the plan. Both the worker and the gate then read the compiler, so the "gate
verifies causality" claim compared the plan to itself and could not fail. Worse, it named
predecessors that did not participate: with ``a --when:succeeded--> join`` and
``b --when:failed--> join`` and ``a`` succeeding, the scheduler EXCLUDES ``b`` (its failed-guard is
unsatisfied) and admits — while the receipt still listed ``b`` as a participant. And had the
scheduler admitted under ``all_selected`` while the plan said ``all_successful``, the receipt would
still have said ``all_successful`` and the gate would still have passed: a silent wrong number in a
hash-chained receipt. Found by the P4.5 audit (Grok finding 5).

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO

from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.graph_ports import ArtifactReaderPort, ArtifactStorePort
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.application.schedule_ready import (
    Admission,
    NodeState,
    predecessors_admission,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


@dataclass(frozen=True)
class JoinNodeWorker:
    """In-process join worker: records causality as a content-addressed receipt.

    The receipt encodes the mode and the live ``(node_id, state, guard)`` of every incoming edge,
    so the gate can replay the real admission predicate rather than re-reading the plan.
    """

    store: ArtifactStorePort
    organization_id: str
    project_id: str
    #: Reads the CURRENT node states from the receipt stream. Optional so fixture graphs and unit
    #: tests that wire no event log keep working; when absent the receipt records
    #: ``parents_observed: false`` and the gate falls back to a plan-shape check, which it says
    #: openly rather than pretending it verified causality.
    node_states_fn: Callable[[ExecutionPlan], Mapping[str, str]] | None = None

    def execute(
        self,
        *,
        plan: ExecutionPlan,
        node: PlannedNode,
        envelope: ExecutionEnvelope,
        attempt: int, repair_round: int,
    ) -> WorkerResult:
        incoming = [edge for edge in plan.edges if edge.to_node == node.node_id]
        states = self.node_states_fn(plan) if self.node_states_fn is not None else {}
        receipt: dict[str, object] = {
            "node_id": node.node_id,
            "join_mode": node.approval_policy.get("join_mode"),
            "predecessor_node_ids": sorted(edge.from_node for edge in incoming),
            # The load-bearing part: what each parent ACTUALLY was when this join was admitted.
            "parents": sorted(
                (
                    [edge.from_node, states.get(edge.from_node), edge.when]
                    for edge in incoming
                ),
                key=lambda entry: str(entry[0]),
            ),
            "parents_observed": self.node_states_fn is not None,
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
        attempt: int,
        repair_round: int,
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

        # THE CHECK THAT MAKES THIS A CAUSALITY GATE. Everything above compares the receipt to the
        # plan, which is the compiler checking itself. This replays the REAL admission predicate --
        # the same function the scheduler used -- over the parent states the worker observed, and
        # refuses the node if those facts would not have admitted it under the recorded mode.
        parents_raw = receipt.get("parents")
        if not receipt.get("parents_observed"):
            # Said openly rather than passed silently: no event log was wired, so no causality claim
            # is being made for this receipt beyond its plan shape.
            return GateVerdict(
                True,
                f"join node {node.node_id!r}: plan shape matches, but parent states were NOT "
                f"observed — causality is unverified for this receipt",
                evidence_digest=digests[0],
            )
        if not isinstance(parents_raw, list):
            return GateVerdict(
                False, f"join node {node.node_id!r}: receipt records no parent states",
                evidence_digest=digests[0],
            )
        try:
            observed = tuple(
                (NodeState(str(entry[1])), (None if entry[2] is None else str(entry[2])))
                for entry in parents_raw
            )
        except (ValueError, IndexError, TypeError) as exc:
            return GateVerdict(
                False,
                f"join node {node.node_id!r}: receipt parent states are malformed ({exc})",
                evidence_digest=digests[0],
            )
        admission = predecessors_admission(node.kind, node.approval_policy, observed)
        if admission is not Admission.ADMIT:
            return GateVerdict(
                False,
                f"join node {node.node_id!r}: the recorded parent states would have produced "
                f"{admission.value}, not ADMIT, under mode {plan_mode!r} — the receipt claims a "
                "causality the scheduler's own predicate does not support",
                evidence_digest=digests[0],
            )

        return GateVerdict(
            True,
            f"join causality verified: mode={plan_mode!r}, "
            f"predecessors={sorted(plan_predecessors)!r}",
            evidence_digest=digests[0],
        )
