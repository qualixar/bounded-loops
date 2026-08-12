"""The approval nonce must be unpredictable and recorded (SEC M-2 regression).

It was previously `sha256(f"{run_id}:{node_id}:nonce")`. Both inputs are visible to anyone
who can read the run directory, so the value a decision is signed over was fully predictable
for every run and node — the opposite of what a nonce is for. It was also never persisted,
so a signed decision could not be re-verified afterwards even in principle.

These tests fail if the derivation goes back to being a function of public values, if two
decisions ever share a nonce, or if the nonce and digest stop being recorded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.application.execute_graph import execute_graph_run
from bounded_loops.graph.application.graph_runtime_facade import LocalGraphRuntimeFacade

# `execute_graph_run` writes the run under these local defaults.
_ORG, _PROJECT = "local-org", "local-project"


def _ctx(run_id: str) -> ArenaReadRequest:
    return ArenaReadRequest(
        subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id=run_id,
    )

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: nonce-check
version: "1.0.0"
nodes:
  - id: checkpoint
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def _paused_run(out: Path, run_id: str) -> None:
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id=run_id,
    )
    assert rc == 3, "the approval node must pause so there is a decision to record"


def _approve(out: Path, run_id: str) -> dict:
    facade = LocalGraphRuntimeFacade.for_run_dir(out)
    facade.approve(_ctx(run_id), node_id="checkpoint", decision="approved")
    return json.loads((out / "approvals.json").read_text(encoding="utf-8"))


def _commit(record: dict) -> dict:
    commits = record["commits"]
    assert len(commits) == 1, commits
    return commits[0]


def test_nonce_is_not_derivable_from_public_run_and_node_identifiers(tmp_path: Path) -> None:
    out = tmp_path / "run"
    _paused_run(out, "run-1")

    commit = _commit(_approve(out, "run-1"))

    legacy = hashlib.sha256(b"run-1:checkpoint:nonce").hexdigest()
    assert commit["nonce"] != legacy, (
        "the nonce is a hash of the run id and node id, so anyone who can read the run "
        "directory can compute the value a decision is signed over"
    )
    assert len(commit["nonce"]) == 64, "expected 256 bits of hex"
    assert int(commit["nonce"], 16) >= 0, "nonce must be hex"


def test_two_decisions_on_the_SAME_run_and_node_ids_still_get_different_nonces(
    tmp_path: Path,
) -> None:
    """Distinct decisions must never share a nonce — the property that makes it single-use.

    Deliberately uses the SAME run id and node id for both decisions. Comparing two
    DIFFERENT run ids would pass under the old derivation too (different inputs, different
    hash) and so would not discriminate; identical inputs are what force the value to come
    from a random source rather than from the identifiers.
    """
    first, second = tmp_path / "a", tmp_path / "b"
    _paused_run(first, "run-1")
    _paused_run(second, "run-1")

    assert _commit(_approve(first, "run-1"))["nonce"] != _commit(_approve(second, "run-1"))["nonce"]


def test_the_nonce_and_signed_digest_are_recorded_with_the_decision(tmp_path: Path) -> None:
    """A random nonce that is not recorded makes the signed digest unreconstructible."""
    out = tmp_path / "run"
    _paused_run(out, "run-1")

    commit = _commit(_approve(out, "run-1"))

    assert commit["nonce"], "the nonce must be persisted or the decision cannot be re-verified"
    assert commit["request_digest"], "the digest the decision was made over must be persisted"


def test_a_ledger_written_without_a_nonce_still_loads(tmp_path: Path) -> None:
    """Backward compatibility: a record from before nonces were randomised must still resume.

    The nonce cannot be re-derived, so rehydration falls back to the old derivation rather
    than refusing to load a run that was mid-flight across the upgrade.
    """
    out = tmp_path / "run"
    _paused_run(out, "run-1")
    record = _approve(out, "run-1")
    for entry in record["commits"]:
        entry.pop("nonce", None)
        entry.pop("request_digest", None)
    (out / "approvals.json").write_text(json.dumps(record), encoding="utf-8")

    facade = LocalGraphRuntimeFacade.for_run_dir(out)
    projection = facade.status(_ctx("run-1"))

    assert projection.run_id == "run-1"
