"""C-047 third-round hardening from the Grok 4.5 convergence audit.

- S1 (defense-in-depth): rollback removed only known temp/leaf names, so an actor
  that hardlinks a staged temp to an alias could retain artifact bytes past a
  failed materialize. Publish now requires the published leaf to have exactly one
  link (no alias) in addition to matching the staged inode. (In the controller
  flow materialize precedes producer execution, so no concurrent actor exists;
  this is belt-and-suspenders.)
- S2: fail-closed-on-missing-flags must also cover O_NONBLOCK, or a FIFO open
  could block.
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


def test_publish_rejects_extra_hardlink_alias(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _input_ref(store)

    real_stage = wp._stage_input

    def _stage_then_alias(parent_fd, temporary, item, access, artifact_reader):
        result = real_stage(parent_fd, temporary, item, access, artifact_reader)
        # Actor keeps an alias to the staged inode so a later unlink cannot reclaim it.
        os.link(temporary, "leaked-alias.bin", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        return result

    monkeypatch.setattr(wp, "_stage_input", _stage_then_alias)

    with pytest.raises(GraphIntegrityError):
        materialize_workspace_inputs(
            workspace, (WorkspaceInput("in.txt", ref),), ArtifactAccess("org-1", "project-1"), store,
        )


def test_traversal_fails_closed_without_nonblock_flag(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_text("ok", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts")

    monkeypatch.setattr(wp, "_O_NONBLOCK", 0)

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
