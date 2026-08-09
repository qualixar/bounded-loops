"""C-046 second-round hardening from the bounded Grok 4.5 audit of the C-045 code.

Findings (see evidence/2026-08-09-C-043-grok-audit.md):
- S1: materialize publish re-resolved the staged temp *by name*; an actor could
  swap that temp between staging and ``os.link`` and bind a forged/FIFO inode.
  Publish must verify the published leaf is exactly the inode we wrote (a regular
  file with the same device/inode/size) and otherwise fail closed.
- S2: the descriptor-safe open flags must not silently fall back to 0 — a platform
  missing O_NOFOLLOW/O_DIRECTORY must fail closed, never follow symlinks.
"""

from __future__ import annotations

from io import BytesIO
import os

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


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _input_ref(store: LocalArtifactStore):
    return store.put(
        BytesIO(b"authorized-source-bytes"),
        ArtifactPolicy("org-1", "project-1", "up:1", "text/plain", "internal", "graph-input"),
    ).ref


def test_publish_rejects_staged_temp_swapped_for_forged_file(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _input_ref(store)

    real_stage = wp._stage_input

    def _stage_then_swap(parent_fd, temporary, item, access, artifact_reader):
        result = real_stage(parent_fd, temporary, item, access, artifact_reader)
        # Actor replaces the staged temp with forged content before publish.
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent_fd)
        try:
            os.write(fd, b"forged-by-actor")
        finally:
            os.close(fd)
        return result

    monkeypatch.setattr(wp, "_stage_input", _stage_then_swap)

    with pytest.raises(GraphIntegrityError):
        materialize_workspace_inputs(
            workspace, (WorkspaceInput("in.txt", ref),), ArtifactAccess("org-1", "project-1"), store,
        )
    published = workspace / "in.txt"
    if published.exists():
        assert published.read_bytes() != b"forged-by-actor"


def test_traversal_fails_closed_without_nofollow_flag(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_text("ok", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts")

    # Simulate a platform/build whose os module lacks O_NOFOLLOW (fell back to 0).
    monkeypatch.setattr(wp, "_O_NOFOLLOW", 0)

    with pytest.raises(GraphIntegrityError):
        promote_workspace_outputs(
            workspace,
            WorkspacePromotionPolicy(
                organization_id="org-1", project_id="project-1", producer_attempt="r:n:1",
                declared_outputs={"report.txt": "text/plain"}, max_file_bytes=64,
                sensitivity="internal", retention_class="graph-output",
            ),
            store,
        )
