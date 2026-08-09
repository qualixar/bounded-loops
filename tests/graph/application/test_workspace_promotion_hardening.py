"""C-045 hardening from the bounded Muse Spark 1.2 independent audit of C-043.

Each test encodes a finding from ``evidence/2026-08-09-C-043-muse-audit.md``:
- S1: ``put_many`` commit phase must be all-or-nothing even when a later output
  conflicts at commit time (cross-tenant digest collision), not only when an
  earlier output fails while staging.
- S1: input materialization must fail closed if the target name appears between
  the existence pre-check and publish (no silent rename-replace).
- S2: declared paths with ``.``/empty/``//`` segments must be rejected rather
  than silently normalized by ``PurePosixPath``.
- S2: a racing removal during enumeration must surface as ``GraphIntegrityError``
  (the controller's fail-closed contract), not a raw ``OSError``.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application import workspace_promotion as wp
from bounded_loops.graph.application.workspace_promotion import (
    WorkspaceInput,
    WorkspacePromotionPolicy,
    materialize_workspace_inputs,
    promote_workspace_outputs,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy
from bounded_loops.graph.domain.errors import GraphIntegrityError


def _policy(outputs: dict[str, str], *, max_bytes: int = 64) -> WorkspacePromotionPolicy:
    return WorkspacePromotionPolicy(
        organization_id="org-1",
        project_id="project-1",
        producer_attempt="run-1:node-1:1",
        declared_outputs=outputs,
        max_file_bytes=max_bytes,
        sensitivity="internal",
        retention_class="graph-output",
    )


def _metadata_count(root) -> int:
    return len(list((root / "metadata").glob("*.json")))


def test_promotion_commits_nothing_when_a_later_output_conflicts_at_commit(tmp_path):
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    shared = b"shared-content-bytes"
    # Pre-seed the identical content under a DIFFERENT tenant, so promoting it as
    # org-1 conflicts on the shared digest during the commit phase (not staging).
    store.put(BytesIO(shared), ArtifactPolicy("other-org", "project-1", "up:1", "text/plain", "internal", "seed"))
    assert _metadata_count(root) == 1

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("unique-a", encoding="utf-8")
    (workspace / "z.txt").write_bytes(shared)

    with pytest.raises(GraphIntegrityError):
        promote_workspace_outputs(workspace, _policy({"a.txt": "text/plain", "z.txt": "text/plain"}), store)

    # The earlier, valid output must not remain committed once a later one conflicts.
    assert _metadata_count(root) == 1


def test_materialization_fails_closed_if_target_appears_before_publish(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.put(
        BytesIO(b"immutable source"),
        ArtifactPolicy("org-1", "project-1", "up:1", "text/plain", "internal", "graph-input"),
    ).ref

    real_stage = wp._stage_input

    def _stage_then_squat(parent_fd, temporary, item, access, artifact_reader):
        real_stage(parent_fd, temporary, item, access, artifact_reader)
        # A racing writer/attacker creates the destination after the pre-check.
        (workspace / item.target_path).write_text("squatted", encoding="utf-8")

    monkeypatch.setattr(wp, "_stage_input", _stage_then_squat)

    with pytest.raises(GraphIntegrityError):
        materialize_workspace_inputs(
            workspace,
            (WorkspaceInput("target.txt", ref),),
            ArtifactAccess("org-1", "project-1"),
            store,
        )
    # The squatted file must be left intact (we must not clobber it).
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "squatted"


@pytest.mark.parametrize("bad", ["./report.txt", "a/./b.txt", "a//b.txt", "outputs/"])
def test_declared_output_rejects_noncanonical_segments(bad: str):
    with pytest.raises(GraphIntegrityError):
        _policy({bad: "text/plain"})


def test_enumeration_removal_race_raises_graph_integrity(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_text("ok", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts")

    def _vanish(name, dir_fd):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(wp, "_stat_at", _vanish)

    with pytest.raises(GraphIntegrityError):
        promote_workspace_outputs(workspace, _policy({"report.txt": "text/plain"}), store)
