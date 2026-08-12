from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.workspace_promotion import (
    WorkspacePromotionPolicy,
    WorkspaceInput,
    materialize_workspace_inputs,
    promote_workspace_outputs,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef
from bounded_loops.graph.domain.errors import GraphIntegrityError


def _policy(*, max_bytes: int = 64) -> WorkspacePromotionPolicy:
    return WorkspacePromotionPolicy(
        organization_id="org-1", project_id="project-1", producer_attempt="run-1:node-1:1",
        declared_outputs={"report.txt": "text/plain"}, max_file_bytes=max_bytes,
        sensitivity="internal", retention_class="graph-output",
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_promotes_only_declared_regular_outputs_to_tenant_artifact_store(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_text("verified output", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts")

    promoted = promote_workspace_outputs(workspace, _policy(), store)

    assert len(promoted) == 1
    record = promoted[0]
    assert record.media_type == "text/plain"
    assert store.open(
        ArtifactRef(record.digest, "org-1", "project-1"), ArtifactAccess("org-1", "project-1"),
    ).read() == b"verified output"


def test_rejects_undeclared_outputs_before_any_promotion(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_text("allowed", encoding="utf-8")
    (workspace / "surprise.txt").write_text("not declared", encoding="utf-8")

    with pytest.raises(GraphIntegrityError, match="undeclared"):
        promote_workspace_outputs(workspace, _policy(), LocalArtifactStore(tmp_path / "artifacts"))


def test_rejects_symlinked_output_before_reading_target(tmp_path):
    workspace = _workspace(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (workspace / "report.txt").symlink_to(target)

    with pytest.raises(GraphIntegrityError, match="symlink"):
        promote_workspace_outputs(workspace, _policy(), LocalArtifactStore(tmp_path / "artifacts"))


def test_rejects_oversized_output_before_any_promotion(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_bytes(b"x" * 65)

    with pytest.raises(GraphIntegrityError, match="oversized"):
        promote_workspace_outputs(workspace, _policy(max_bytes=64), LocalArtifactStore(tmp_path / "artifacts"))


def test_policy_rejects_traversal_and_duplicate_or_empty_output_contracts():
    with pytest.raises(GraphIntegrityError, match="declared output"):
        WorkspacePromotionPolicy(
            organization_id="org-1", project_id="project-1", producer_attempt="run-1:node-1:1",
            declared_outputs={"../outside.txt": "text/plain"}, max_file_bytes=64,
            sensitivity="internal", retention_class="graph-output",
        )
    with pytest.raises(GraphIntegrityError, match="max file"):
        _policy(max_bytes=0)


def _input_artifact(store: LocalArtifactStore) -> ArtifactRef:
    record = store.put(
        BytesIO(b"immutable source"),
        ArtifactPolicy("org-1", "project-1", "upstream:1", "text/plain", "internal", "graph-input"),
    )
    return record.ref


def test_materializes_authorized_input_to_a_read_only_declared_path(tmp_path):
    workspace = _workspace(tmp_path)
    store = LocalArtifactStore(tmp_path / "artifacts")

    targets = materialize_workspace_inputs(
        workspace, (WorkspaceInput("inputs/source.txt", _input_artifact(store)),),
        ArtifactAccess("org-1", "project-1"), store,
    )

    assert targets == (workspace / "inputs" / "source.txt",)
    assert targets[0].read_bytes() == b"immutable source"
    assert targets[0].stat().st_mode & 0o222 == 0


def test_materialization_denies_duplicate_targets_and_foreign_artifacts(tmp_path):
    workspace = _workspace(tmp_path)
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _input_artifact(store)
    with pytest.raises(GraphIntegrityError, match="same path"):
        materialize_workspace_inputs(
            workspace, (WorkspaceInput("input.txt", ref), WorkspaceInput("input.txt", ref)),
            ArtifactAccess("org-1", "project-1"), store,
        )
    with pytest.raises(GraphIntegrityError, match="unauthorized"):
        materialize_workspace_inputs(
            workspace, (WorkspaceInput("input.txt", ref),),
            ArtifactAccess("other-org", "project-1"), store,
        )


def test_materialization_denies_traversal_symlink_parent_and_existing_target(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _input_artifact(store)
    with pytest.raises(GraphIntegrityError, match="traversal"):
        WorkspaceInput("../outside.txt", ref)

    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "inputs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GraphIntegrityError, match="symlink"):
        materialize_workspace_inputs(
            workspace, (WorkspaceInput("inputs/source.txt", ref),),
            ArtifactAccess("org-1", "project-1"), store,
        )

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "source.txt").write_text("already here", encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="already exists"):
        materialize_workspace_inputs(
            clean, (WorkspaceInput("source.txt", ref),), ArtifactAccess("org-1", "project-1"), store,
        )
