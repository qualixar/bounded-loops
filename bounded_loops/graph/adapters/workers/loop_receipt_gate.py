"""The independent gate for a ``kind: loop`` node: verify the receipt, never re-run the loop.

The obvious design — bridge the loop's own declared gate (``pytest``, ``jsonschema``, ``osv`` …)
onto ``IndependentGatePort`` — is wrong, and the mistake is worth stating because it looks like
reuse. A graph attempt of a loop node runs the WHOLE loop, and a bounded loop already contains its
own independent gate. So the graph's worker has, by construction, produced both the work and a
verdict on the work. If the graph's gate then re-runs that same ``pytest``, it is either doing the
producer's own check a second time or standing in for it — and either way the ``worker is not gate``
invariant has become a formality. ``LegacyLoopWorker``'s docstring said this before the code
existed: "the graph controller still invokes a separate outer gate after this worker returns its
receipt artifact."

What an outer gate can honestly add is verification of EVIDENCE it did not produce:

* the promoted outcome artifact exists, parses, and is the shape this engine writes;
* the package digest in it matches the digest the PLAN admitted, so the receipt describes the
  package the node was compiled against and not some other package on the host;
* the node and round in it match the node and round being gated, so a stale artifact from another
  attempt cannot satisfy this one;
* the loop's own terminal status is ``DONE``.

That is strictly stronger than re-running the gate, because a re-run proves only that a check passes
now, while this proves the recorded work is the work that was admitted.
"""

from __future__ import annotations

import json

from bounded_loops.graph.application.graph_ports import ArtifactReaderPort
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

#: The loop-engine terminal status that means the loop's own independent gate passed.
_ACCEPTED_STATUS = "DONE"
#: Loop-engine statuses that mean it ran and did not converge. These are GATE REJECTIONS, not
#: worker faults: the loop exhausted its laps or its no-progress window with the gate still
#: failing. Calling them faults would let ``continue_declared`` treat an honest "did not converge"
#: as a transient crash, and would have the controller retry a loop that already spent its bound.
_REJECTED_STATUSES = frozenset({"HALT", "KILLED"})


class LoopReceiptGate:
    """Independent gate for a loop node: re-reads the promoted outcome, never the producer."""

    def __init__(
        self, store: ArtifactReaderPort, *, organization_id: str, project_id: str,
        repair_round: int = 0,
    ) -> None:
        self._store = store
        self._organization_id = organization_id
        self._project_id = project_id
        self._repair_round = repair_round

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult,
    ) -> GateVerdict:
        digests = result.output_artifact_digests
        if not digests:
            return GateVerdict(False, f"loop node {node.node_id!r} produced no outcome artifact")
        ref = ArtifactRef(digests[0], self._organization_id, self._project_id)
        access = ArtifactAccess(self._organization_id, self._project_id)
        try:
            with self._store.open(ref, access) as handle:
                payload = handle.read()
        except Exception as exc:  # noqa: BLE001 — an unreadable receipt is a closed gate
            return GateVerdict(False, f"loop node {node.node_id!r} outcome is unreadable: {exc}")
        try:
            outcome = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return GateVerdict(False, f"loop node {node.node_id!r} outcome is not valid JSON: {exc}")
        if not isinstance(outcome, dict):
            return GateVerdict(False, f"loop node {node.node_id!r} outcome is not a JSON object")

        verdict = self._verify_provenance(node, outcome)
        if verdict is not None:
            return verdict

        status = outcome.get("status")
        reason = outcome.get("reason") or "(no reason recorded)"
        if status == _ACCEPTED_STATUS:
            return GateVerdict(
                True, f"loop reached {status}: {reason}", evidence_digest=digests[0],
            )
        if status in _REJECTED_STATUSES:
            return GateVerdict(
                False, f"loop did not converge ({status}): {reason}", evidence_digest=digests[0],
            )
        return GateVerdict(
            False,
            f"loop node {node.node_id!r} reported an unrecognised status {status!r}: {reason}",
            evidence_digest=digests[0],
        )

    def _verify_provenance(
        self, node: PlannedNode, outcome: dict[str, object],
    ) -> GateVerdict | None:
        """Refuse a receipt that describes different work. ``None`` means provenance is sound.

        Checked BEFORE status, so a stale or foreign artifact can never be accepted merely because
        it happens to say ``DONE``.
        """
        declared = outcome.get("package_digest")
        if declared != node.package_digest:
            return GateVerdict(
                False,
                f"loop node {node.node_id!r} outcome names package digest {declared!r} but the plan "
                f"admitted {node.package_digest!r}",
            )
        if outcome.get("node_id") != node.node_id:
            return GateVerdict(
                False,
                f"loop outcome names node {outcome.get('node_id')!r}, not {node.node_id!r}",
            )
        recorded_round = outcome.get("repair_round", 0)
        if recorded_round != self._repair_round:
            return GateVerdict(
                False,
                f"loop node {node.node_id!r} outcome is from repair round {recorded_round!r}, "
                f"not round {self._repair_round}",
            )
        return None
