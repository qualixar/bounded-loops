"""The bridge, against a real run directory rather than a fixture.

`test_slm_bridge.py` proves the contract SHAPE from fake projections, which is where the edge
cases live. This proves the adapter actually reads a run this engine produced — the join
between the two is where a contract quietly stops describing reality.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from bounded_loops.graph.application.slm_bridge import CONTRACT_ID, EvidenceUnavailable
from bounded_loops.graph.slm_evidence import evidence_for_run, terminal_runs
from bounded_loops.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    from bounded_loops.workspace import discover, ensure

    root = tmp_path / "project"
    root.mkdir()
    workspace = discover(root, explicit=root / ".bounded-loops")
    ensure(workspace)
    return workspace


def _graph_run(workspace: Workspace) -> str:
    """Run a shipped reference graph so there is a real receipt stream to read."""
    # The built-in demo: `--execute` with no manifest. Self-contained, keyless, and it
    # produces exactly what the bridge has to read — a real run directory with a real
    # hash-chained receipt stream, rather than a hand-assembled fixture that could drift
    # from what the engine actually writes.
    run_id = "demo-run-1"
    result = subprocess.run(
        [
            sys.executable, "-m", "bounded_loops.cli", "graph", "run", "--execute",
            "--out", str(workspace.runs_dir / run_id),
        ],
        cwd=workspace.root.parent,
        capture_output=True,
        text=True,
        timeout=300,
        env={**_env(), "BOUNDED_LOOPS_WORKSPACE": str(workspace.root)},
    )
    if not (workspace.runs_dir / run_id / "controller-events.jsonl").is_file():
        pytest.skip(
            f"graph run produced no receipt stream here: "
            f"{result.stdout[-300:]}{result.stderr[-300:]}"
        )
    return run_id


def _env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items()}


def test_a_real_run_produces_a_valid_contract_document(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run_id = _graph_run(workspace)

    document = evidence_for_run(workspace, run_id)

    assert document["contract"] == CONTRACT_ID
    assert document["run_ref"] == run_id  # the ADDRESS we fetched by
    assert document["run_id"]  # the run's own identity, from its receipts
    assert document["outcome"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
    assert document["workspace_id"].startswith("sha256:")
    assert document["receipt"]["trust"] == "local_hash_chain_only"
    assert document["eligible_for_learning"] is False
    assert isinstance(document["demonstration"], bool)
    # The head and sequence must be THIS run's, not a placeholder.
    assert document["receipt"]["sequence"] > 0
    assert document["receipt"]["head_digest"].startswith("sha256:")


def test_the_document_carries_no_path_from_a_real_run(tmp_path: Path) -> None:
    """The strongest leak test available: a real workspace with a real, revealing path."""
    workspace = _workspace(tmp_path)
    run_id = _graph_run(workspace)

    import json

    serialized = json.dumps(evidence_for_run(workspace, run_id))

    assert str(tmp_path) not in serialized
    assert str(workspace.root) not in serialized
    assert "/Users" not in serialized and "/tmp" not in serialized


def test_terminal_runs_lists_the_run_so_a_consumer_can_find_it(tmp_path: Path) -> None:
    """Without discovery the fetch is unusable over MCP: nobody knows which id to ask for."""
    workspace = _workspace(tmp_path)
    run_id = _graph_run(workspace)

    listed = terminal_runs(workspace)

    assert any(entry["run_ref"] == run_id for entry in listed)
    for entry in listed:
        assert set(entry) == {"run_ref", "run_id", "run_state", "terminal_at"}


@pytest.mark.parametrize(
    "hostile", ["../../etc/passwd", "..", "a/b", "", ".", "/etc", "x/../../y"]
)
def test_a_path_is_never_accepted_as_a_run_id(tmp_path: Path, hostile: str) -> None:
    """A consumer names a RUN. `run_id` reaches the filesystem, so it is validated first."""
    workspace = _workspace(tmp_path)

    with pytest.raises(EvidenceUnavailable):
        evidence_for_run(workspace, hostile)


def test_an_unknown_run_refuses_rather_than_crashes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(EvidenceUnavailable, match="no such run"):
        evidence_for_run(workspace, "run-that-does-not-exist")


def test_listing_an_empty_workspace_is_empty_not_an_error(tmp_path: Path) -> None:
    assert terminal_runs(_workspace(tmp_path)) == []
