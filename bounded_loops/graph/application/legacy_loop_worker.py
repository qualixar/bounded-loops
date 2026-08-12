"""Bridge one digest-pinned legacy loop into the graph worker contract."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bounded_loops.application.loop_bridge import (
    LoopExecutionRequest,
    WiredLoopExecution,
    wire_loop_for_graph,
)
from bounded_loops.application.manifest import LoopManifest
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, validate_execution_envelope
from bounded_loops.graph.application.run_graph import WorkerResult
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


class LegacyLoopWorker:
    """Execute an admitted loop package and emit a sanitized graph receipt.

    Package lookup is digest-only. The worker owns neither connection secrets
    nor an approval decision: the graph controller still invokes a separate
    outer gate after this worker returns its receipt artifact.
    """

    def __init__(
        self,
        *,
        identity: GraphRunIdentity,
        resolve_manifest: Callable[[str], LoopManifest],
        controller_root: Path,
        artifact_store: LocalArtifactStore,
        wire_loop: Callable[[LoopManifest, LoopExecutionRequest], WiredLoopExecution] = wire_loop_for_graph,
    ) -> None:
        self._identity = identity
        self._resolve_manifest = resolve_manifest
        self._controller_root = controller_root
        self._artifact_store = artifact_store
        self._wire_loop = wire_loop

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        validate_execution_envelope(plan, node, envelope)
        raise GraphIntegrityError(
            "legacy loop worker cannot enforce a graph execution envelope; use a sandboxed graph runner"
        )
