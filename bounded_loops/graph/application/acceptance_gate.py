"""An independent structural-acceptance gate for real graph runs (RE).

A separate object from the worker (the controller enforces they differ): it re-reads the node's
PROMOTED output artifact from the store — never re-executing the producer — and passes only if the
node produced a non-empty, UTF-8-decodable reply. This is STRUCTURAL acceptance (the node really
produced a well-formed artifact), not a semantic review; the cross-model audit graph is the richer
semantic overlay layered on top of this primitive.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


class StructuralAcceptanceGate:
    """Independent gate: pass only if the node's promoted artifact is a non-empty UTF-8 reply."""

    def __init__(
        self, store: LocalArtifactStore, *, organization_id: str, project_id: str,
    ) -> None:
        self._store = store
        self._organization_id = organization_id
        self._project_id = project_id

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult,
    ) -> GateVerdict:
        digests = result.output_artifact_digests
        if not digests:
            return GateVerdict(False, f"node {node.node_id!r} produced no output artifact")
        ref = ArtifactRef(digests[0], self._organization_id, self._project_id)
        access = ArtifactAccess(self._organization_id, self._project_id)
        try:
            with self._store.open(ref, access) as handle:
                payload = handle.read()
        except Exception as exc:  # noqa: BLE001 — an unreadable artifact is a closed gate failure
            return GateVerdict(False, f"node {node.node_id!r} output artifact is unreadable: {exc}")
        if not payload.strip():
            return GateVerdict(False, f"node {node.node_id!r} produced an empty reply")
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            return GateVerdict(False, f"node {node.node_id!r} output is not valid UTF-8 text")
        return GateVerdict(
            True,
            f"independent gate: node {node.node_id!r} produced a non-empty, well-formed reply",
        )
