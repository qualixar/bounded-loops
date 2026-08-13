from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bounded_loops.application.manifest import LoopManifest
from bounded_loops.domain.models import Bounds, Outcome, Rung, Spec, Status
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.legacy_loop_worker import LegacyLoopWorker
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.application.node_contracts import GateVerdict
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="graph-run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
    )


def _manifest(loop_dir: Path) -> LoopManifest:
    return LoopManifest(
        name="example",
        spec=Spec(name="example", goal="goal", steps=("step",), stop_condition="gate"),
        bounds=Bounds(max_iterations=1), runner_kind="stub", gate_kind="command",
        gate_config={"run": "true"}, rung=Rung.L1, cassette=None,
        raw={"name": "example"}, loop_dir=loop_dir, memory_path=Path("STATE.md"),
        env_passthrough=(),
    )


def _node() -> PlannedNode:
    return PlannedNode(
        node_id="legacy-loop", kind="loop", package_digest="sha256:" + "d" * 64,
        binding_id=None, required_effects=frozenset({Effect.READ_ONLY}),
        isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1_000,
        budgets={}, approval_policy={},
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "b" * 64,
        source_graph_digest="sha256:" + "a" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=(_node(),), edges=(), levels=(("legacy-loop",),),
        package_digests=("sha256:" + "d" * 64,), connection_bindings=(), canonical_json=b"{}",
    )


def _envelope(node: PlannedNode) -> ExecutionEnvelope:
    return ExecutionEnvelope(node.isolation, None, node.required_effects, NetworkMode.DENY, ())


def _policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    return ConfiguredExecutionPolicy({node.node_id: _envelope(node) for node in plan.nodes})


def test_legacy_loop_worker_resolves_digest_and_emits_a_controller_owned_receipt(tmp_path):
    manifest = _manifest(tmp_path / "loop-package")
    manifest.loop_dir.mkdir()
    execution = MagicMock()
    execution.run.return_value = Outcome(Status.DONE, "sensitive provider text", 2, tmp_path / "ledger")
    resolver = MagicMock(return_value=manifest)
    wire = MagicMock(return_value=execution)
    store = LocalArtifactStore(tmp_path / "artifacts")
    worker = LegacyLoopWorker(
        identity=_identity(), resolve_manifest=resolver, wire_loop=wire,
        controller_root=tmp_path / "controller", artifact_store=store,
    )

    node = _node()
    with pytest.raises(GraphIntegrityError, match="cannot enforce"):
        worker.execute(plan=_plan(), node=node, envelope=_envelope(node), attempt=1)

    resolver.assert_not_called()
    wire.assert_not_called()


def test_legacy_loop_worker_rejects_a_legacy_loop_without_its_own_gate_pass(tmp_path):
    manifest = _manifest(tmp_path / "loop-package")
    manifest.loop_dir.mkdir()
    execution = MagicMock()
    execution.run.return_value = Outcome(Status.HALT, "untrusted detail", 1, tmp_path / "ledger")
    worker = LegacyLoopWorker(
        identity=_identity(), resolve_manifest=MagicMock(return_value=manifest),
        wire_loop=MagicMock(return_value=execution), controller_root=tmp_path / "controller",
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )

    with pytest.raises(GraphIntegrityError, match="cannot enforce"):
        node = _node()
        worker.execute(plan=_plan(), node=node, envelope=_envelope(node), attempt=1)


def test_legacy_loop_composes_through_artifact_verification_and_outer_gate(tmp_path):
    manifest = _manifest(tmp_path / "loop-package")
    manifest.loop_dir.mkdir()
    execution = MagicMock()
    execution.run.return_value = Outcome(Status.DONE, "legacy gate detail", 1, tmp_path / "ledger")
    store = LocalArtifactStore(tmp_path / "artifacts")
    gate = MagicMock()
    gate.evaluate.return_value = GateVerdict(True, "separate graph gate")
    worker = LegacyLoopWorker(
        identity=_identity(), resolve_manifest=MagicMock(return_value=manifest),
        wire_loop=MagicMock(return_value=execution), controller_root=tmp_path / "controller",
        artifact_store=store,
    )
    controller = GraphRunController(
        plan=_plan(), event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
        worker=worker, gate=gate, artifact_verifier=LocalArtifactVerifier(store),
        execution_policy=_policy(_plan()),
        execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    gate.evaluate.assert_not_called()


class _Enforcer:
    def enforce(self, *, plan, node, envelope) -> None:
        return None
