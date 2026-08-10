"""C-043 adversarial security corpus for controller-owned workspace promotion.

These tests encode the corrected security contract from
``evidence/2026-08-09-E2-WORKSPACE-SECURITY-REVIEW.md``. They are RED against the
provisional C-041/C-042 implementation and must pass only once promotion uses
descriptor-anchored (openat) no-follow traversal, byte caps enforced during
streaming, portable path rejection, and atomic/compensated batch promotion.

The regression corpus in ``test_workspace_promotion.py`` proves compatibility;
this corpus proves the workspace actor is treated as untrusted.
"""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import signal
import threading
import types

import pytest

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application import workspace_promotion as wp
from bounded_loops.graph.application.workspace_promotion import (
    WorkspaceInput,
    WorkspacePromotionPolicy,
    materialize_workspace_inputs,
    promote_workspace_outputs,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef
from bounded_loops.graph.domain.errors import GraphIntegrityError


def _policy(outputs: dict[str, str] | None = None, *, max_bytes: int = 64) -> WorkspacePromotionPolicy:
    return WorkspacePromotionPolicy(
        organization_id="org-1",
        project_id="project-1",
        producer_attempt="run-1:node-1:1",
        declared_outputs=outputs or {"report.txt": "text/plain"},
        max_file_bytes=max_bytes,
        sensitivity="internal",
        retention_class="graph-output",
    )


def _workspace(tmp_path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _committed_metadata(store_root) -> list:
    return list((store_root / "metadata").glob("*.json"))


def _input_artifact(store: LocalArtifactStore) -> ArtifactRef:
    record = store.put(
        BytesIO(b"immutable source"),
        ArtifactPolicy("org-1", "project-1", "upstream:1", "text/plain", "internal", "graph-input"),
    )
    return record.ref


class _Deadline:
    """Fail the test if a call blocks — proves non-blocking opens on FIFO swap."""

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self._old = None

    def __enter__(self):
        def _handler(signum, frame):
            raise TimeoutError("workspace promotion blocked on a special file")

        self._old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(self._seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        if self._old is not None:
            signal.signal(signal.SIGALRM, self._old)
        return False


# --- Finding 3: portable path denial ------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    ["dir\\report.txt", "C:\\report.txt", "\\\\server\\share\\report.txt", "\\report.txt"],
)
def test_declared_output_rejects_windows_separator_drive_and_unc(bad: str) -> None:
    with pytest.raises(GraphIntegrityError):
        WorkspacePromotionPolicy(
            organization_id="org-1",
            project_id="project-1",
            producer_attempt="run-1:node-1:1",
            declared_outputs={bad: "text/plain"},
            max_file_bytes=64,
            sensitivity="internal",
            retention_class="graph-output",
        )


@pytest.mark.parametrize("bad", ["dir\\source.txt", "C:\\source.txt", "\\\\srv\\s\\x.txt"])
def test_workspace_input_rejects_windows_separator_and_drive(bad: str) -> None:
    ref = ArtifactRef("sha256:" + "0" * 64, "org-1", "project-1")
    with pytest.raises(GraphIntegrityError):
        WorkspaceInput(bad, ref)


# --- Finding 4: atomic batch — no partial multi-output promotion ---------------------

def test_partial_multi_output_promotion_commits_nothing_on_later_failure(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.txt").write_text("small", encoding="utf-8")
    (workspace / "z_big.txt").write_bytes(b"x" * 200)  # exceeds cap, sorts last
    store_root = tmp_path / "artifacts"
    store = LocalArtifactStore(store_root)

    policy = _policy({"a.txt": "text/plain", "z_big.txt": "text/plain"}, max_bytes=16)
    with pytest.raises(GraphIntegrityError):
        promote_workspace_outputs(workspace, policy, store)

    # All-or-nothing: the earlier, valid output must not remain committed.
    assert _committed_metadata(store_root) == []


# --- Finding 2: byte cap enforced during streaming, not only via fstat ---------------

def test_output_size_cap_is_enforced_during_streaming(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_bytes(b"y" * 500)
    store_root = tmp_path / "artifacts"
    store = LocalArtifactStore(store_root)

    # Simulate a file that under-reports its size at check time then is large on read
    # (the growth-after-check attack). The cap must still trip while streaming.
    real_stat = wp._stat_fd  # committed internal seam in the fixed implementation

    def _lying_stat(fd):
        real = real_stat(fd)
        return types.SimpleNamespace(st_mode=real.st_mode, st_size=1)

    monkeypatch.setattr(wp, "_stat_fd", _lying_stat)

    with pytest.raises(GraphIntegrityError):
        promote_workspace_outputs(workspace, _policy(max_bytes=16), store)
    assert _committed_metadata(store_root) == []


# --- Finding 1: post-enumeration symlink swap on a path component --------------------

def test_component_symlink_swapped_after_enumeration_is_rejected(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "sub").mkdir()
    (workspace / "sub" / "report.txt").write_text("honest", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.txt").write_text("attacker", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts")

    real_enumerate = wp._enumerate_regular_outputs  # committed internal seam

    def _swap_then_return(root_fd):
        found = real_enumerate(root_fd)
        # Swap the validated directory component for a symlink to attacker content.
        os.rename(workspace / "sub", workspace / "sub_real")
        os.symlink(outside, workspace / "sub", target_is_directory=True)
        return found

    monkeypatch.setattr(wp, "_enumerate_regular_outputs", _swap_then_return)

    with pytest.raises(GraphIntegrityError):
        promote_workspace_outputs(workspace, _policy({"sub/report.txt": "text/plain"}), store)


# --- Finding 5: FIFO swap must not block and must be rejected ------------------------

def test_regular_output_swapped_for_fifo_does_not_block(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "report.txt").write_text("honest", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts")

    real_enumerate = wp._enumerate_regular_outputs

    def _swap_then_return(root_fd):
        found = real_enumerate(root_fd)
        os.remove(workspace / "report.txt")
        os.mkfifo(workspace / "report.txt")
        return found

    monkeypatch.setattr(wp, "_enumerate_regular_outputs", _swap_then_return)

    with _Deadline(5):
        with pytest.raises(GraphIntegrityError):
            promote_workspace_outputs(workspace, _policy(), store)


# --- Materialization must be atomic and descriptor-safe ------------------------------

def test_materialization_is_atomic_across_inputs_on_later_failure(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    store = LocalArtifactStore(tmp_path / "artifacts")
    good = _input_artifact(store)
    missing = ArtifactRef("sha256:" + "e" * 64, "org-1", "project-1")
    with pytest.raises(GraphIntegrityError):
        materialize_workspace_inputs(
            workspace,
            (
                WorkspaceInput("first.txt", good),
                WorkspaceInput("second.txt", missing),  # unreadable -> fails after first is staged
            ),
            ArtifactAccess("org-1", "project-1"),
            store,
        )
    # Neither input may remain — no partial materialization.
    assert not (workspace / "first.txt").exists()
    assert not (workspace / "second.txt").exists()


def test_materialization_parent_symlink_swapped_after_check_is_rejected(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _input_artifact(store)
    # A pre-existing symlink parent must be rejected by descriptor-safe traversal.
    (workspace / "inputs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GraphIntegrityError):
        materialize_workspace_inputs(
            workspace,
            (WorkspaceInput("inputs/source.txt", ref),),
            ArtifactAccess("org-1", "project-1"),
            store,
        )


def test_concurrent_materialization_into_one_workspace_stays_consistent(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _input_artifact(store)
    errors: list[BaseException] = []

    def _worker(name: str) -> None:
        try:
            materialize_workspace_inputs(
                workspace,
                (WorkspaceInput(f"inputs/{name}.txt", ref),),
                ArtifactAccess("org-1", "project-1"),
                store,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(f"n{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    for i in range(4):
        target = workspace / "inputs" / f"n{i}.txt"
        assert target.read_bytes() == b"immutable source"
        assert target.stat().st_mode & 0o222 == 0
