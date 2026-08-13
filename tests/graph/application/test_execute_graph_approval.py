"""RED-first tests: `execute_graph_run` supports approval-checkpoint nodes.

Slice 1 — approvals in `bl graph run --execute`. Before this slice, `_preflight`
unconditionally refused any `kind: approval` node. These tests prove:

1. A fresh run with an unapproved approval node PAUSES (durable AWAITING_APPROVAL)
   instead of being refused or crashing, and reports a clear, actionable, non-error
   exit status (rc == 3, distinct from success (0) and failure (2)).
2. Genuinely unsupported node kinds (e.g. a bare `tool` node with no connector
   binding) are STILL refused by preflight — the approval carve-out is scoped.
3. The existing https-admitted-record preflight check is untouched.
4. A durably pre-recorded approval decision (seeded before the run starts) is
   honored on this SAME fresh run — proof the resolver is wired, not just present.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.graph.graph_composition import execute_graph_run
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir as _load_plan_from_run_dir
from bounded_loops.graph.application.arena_projection import (
    ArenaNodeProjection,
    ArenaProjection,
    ArenaReadRequest,
    read_arena_projection,
)

_ORG, _PROJECT = "local-org", "local-project"

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: execute-approval
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

_TOOL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: execute-unsupported-tool
version: "1.0.0"
nodes:
  - id: sandboxed
    kind: tool
    tool_ref: "some-sandboxed-tool"
    inputs: {}
    outputs: {result: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: process_restricted
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""

_HTTPS_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: execute-https-no-admit
version: "1.0.0"
nodes:
  - id: chat
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [external_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""


def _https_connections() -> list[dict]:
    return [{
        "binding_id": "binding-1", "slot_id": "model", "connector_id": "byok-http",
        "connector_version": "1.0.0", "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64, "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "openai", "model_target": "gpt-4o-mini", "region": "us-east-1",
        "fallback": False, "capabilities": ["text_generation"], "data_class_max": "public",
        "allowed_effects": ["external_write"], "isolation": "process_restricted",
        "transport": "https", "admitted": True,
    }]


def _arena(out: Path, run_id: str = "run-1"):
    plan, identity, meta = _load_plan_from_run_dir(out)
    from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog

    event_log = GraphEventLog(out / "controller-events.jsonl", identity)

    class _Auth:
        def authorize(self, request: ArenaReadRequest) -> bool:
            return True

    class _Verify:
        def verify(self, identity: object, receipts: object) -> None:
            return None

    arena = read_arena_projection(
        plan, event_log,
        ArenaReadRequest(subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id=run_id),
        _Auth(), _Verify(),
    )
    return arena, meta


# ── 1. approval node pauses instead of being refused ─────────────────────────

def test_approval_node_pauses_run_with_actionable_status(tmp_path: Path, capsys) -> None:
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
    )
    # Distinct from success (0) and failure (2) — a paused run is neither.
    assert rc == 3, "an unapproved approval node must PAUSE, not refuse (2) or crash"
    assert (out / "controller-events.jsonl").is_file(), "the run must have started and paused durably"

    arena, _meta = _arena(out)
    assert arena.run_state == "RUNNING"
    assert arena.nodes[0].node_id == "checkpoint"
    assert arena.nodes[0].state == "AWAITING_APPROVAL"

    printed = capsys.readouterr().out
    # DX-01: the paused output must not be mistakable for an executing run.
    # The run_state line is "RUNNING" (the run-level projection has no AWAITING_APPROVAL
    # state); a separate pause_status line must name the paused status explicitly so a
    # newcomer reading only the first few lines cannot think the run is still executing.
    assert "run_state : RUNNING" in printed, (
        "run_state must reflect the run-level projection (RUNNING), not a node state"
    )
    assert "pause_status" in printed, (
        "a pause_status line must appear immediately after run_state so the paused "
        "condition is unambiguous without reading to the bottom of the output"
    )
    assert "PAUSED" in printed, (
        "the word PAUSED must appear in the human-readable output"
    )
    assert "checkpoint" in printed, "the awaiting node id must be named"
    assert "bl graph approve" in printed
    assert "--decision approved|rejected" in printed


def test_approval_node_pauses_run_json_shape(tmp_path: Path, capsys) -> None:
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
        json_out=True,
    )
    assert rc == 3
    data = json.loads(capsys.readouterr().out)
    assert data["paused"] is True
    assert data["awaiting_approval"] == ["checkpoint"]
    assert any("bl graph approve" in cmd and "checkpoint" in cmd for cmd in data["next_commands"])
    assert data["run_state"] == "RUNNING"


# ── 2. approval carve-out is scoped: other unsupported kinds still refused ───

def test_unsupported_tool_node_is_still_refused_by_preflight(tmp_path: Path, capsys) -> None:
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_TOOL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
    )
    assert rc == 2, "a non-approval, non-admitted-connector node must still be refused at preflight"
    assert not (out / "controller-events.jsonl").is_file(), "preflight refusal must precede any receipt"
    # Pin the message to the PREFLIGHT refusal specifically (not a compile failure) —
    # rc == 2 alone is ambiguous between the two, since both precede the event log.
    err = capsys.readouterr().err
    assert "not an admitted connector node" in err
    assert "sandboxed" in err.lower()


# ── 3. https admitted-record preflight check is untouched ───────────────────

def test_https_node_without_admitted_record_still_refused(tmp_path: Path) -> None:
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_HTTPS_MANIFEST, manifest_suffix=".yaml",
        connections_raw=_https_connections(), node_prompts={"chat": "hi"},
        out_dir=out, run_id="run-1", admitted_connections={},
    )
    assert rc == 2
    assert not (out / "controller-events.jsonl").is_file()


# ── 4. a durably pre-seeded decision is honored on the FIRST run ─────────────

def test_preseeded_approval_decision_is_honored_on_fresh_run(tmp_path: Path) -> None:
    """If approvals.json already carries a decision for this run+node before the run
    starts (e.g. a prior partial attempt at the same out_dir), the fresh run's
    resolver must honor it rather than pausing again — proof execute_graph_run wires
    the SAME durable resolver the facade uses, not an always-empty one."""
    import hashlib

    out = tmp_path / "run"
    out.mkdir(parents=True)
    approval_id = hashlib.sha256(
        b"local-org:local-project:run-1:checkpoint"
    ).hexdigest()
    (out / "approvals.json").write_text(json.dumps({
        "resource_version": 2,
        "commits": [{
            "approval_id": approval_id, "new_resource_version": 2,
            "idempotency_key": approval_id, "node_id": "checkpoint",
            "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
        "rejections": [],
    }), encoding="utf-8")

    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
    )
    assert rc == 0, "a pre-approved decision must let the fresh run reach SUCCEEDED"
    arena, _meta = _arena(out)
    assert arena.run_state == "SUCCEEDED"
    assert arena.nodes[0].state == "SUCCEEDED"


# ── 5. dual-audit reconciliation: fail closed, never crash ───────────────────
#
# Both the Grok and Muse adversarial audits of this slice found the SAME two
# uncaught-`GraphIntegrityError` gaps in `execute_graph_run`: a torn ledger, and a
# re-execute on a run that already started. Neither is a bypass of the approval
# control itself (fail-CLOSED still holds either way — the run never reaches a false
# SUCCEEDED) — but an uncaught exception is a crash, not the clean `rc=2` / `error:`
# contract every other refusal in this module honors.

def test_corrupt_approval_ledger_fails_closed_not_crash(tmp_path: Path, capsys) -> None:
    """A torn/corrupt approvals.json must fail the run closed with rc=2 and a clean
    `error:` message on stderr — never let `GraphIntegrityError` escape
    `execute_graph_run` as an uncaught exception."""
    out = tmp_path / "run"
    out.mkdir(parents=True)
    (out / "approvals.json").write_text("{ not valid json", encoding="utf-8")

    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
    )
    assert rc == 2, "a corrupt approval ledger must fail closed (rc=2), not crash"
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "approval ledger" in err.lower()
    assert "corrupt" in err.lower() or "unreadable" in err.lower()


def test_corrupt_approval_ledger_fails_closed_json_mode(tmp_path: Path, capsys) -> None:
    out = tmp_path / "run"
    out.mkdir(parents=True)
    (out / "approvals.json").write_text("{ not valid json", encoding="utf-8")

    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
        json_out=True,
    )
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["execution"] is False
    assert "approval ledger" in data["error"].lower()


def test_reexecute_on_paused_run_fails_closed_with_actionable_message(
    tmp_path: Path, capsys,
) -> None:
    """Re-running `execute_graph_run` a SECOND time at the same out_dir — e.g. a user
    who sees rc=3 and just re-runs the same command, expecting it to "resume" — must
    fail closed with a message pointing at `bl graph approve`, never let the
    controller's `GraphIntegrityError("fresh controller refuses to resume a non-empty
    graph stream")` escape execute_graph_run as an uncaught exception."""
    out = tmp_path / "run"
    rc1 = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
    )
    assert rc1 == 3
    capsys.readouterr()  # drain the first call's output

    rc2 = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
    )
    assert rc2 == 2, "re-executing an already-started run must fail closed, not crash"
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "already holds a run" in err
    assert "bl graph approve" in err


# ── 6. reconciliation MINOR: FAILED must outrank a stale AWAITING_APPROVAL node ──
#
# `_report` decides "paused" purely from whether ANY node's LATEST receipt shows
# AWAITING_APPROVAL — it never separately checks the run's own terminal `run_state`.
# A run whose `run_state` is authoritatively FAILED must never be reported as merely
# paused, so this drives the decision from a hand-built `ArenaProjection` (the real
# controller only returns this specific combination via a rare multi-node race the
# unit boundary makes trivial and deterministic to construct).

def _arena_with_failed_run_and_awaiting_node() -> ArenaProjection:
    node = ArenaNodeProjection(
        node_id="checkpoint", kind="approval", state="AWAITING_APPROVAL", attempt=1,
        required_effects=(), isolation="workspace_only", hard_deadline_ms=30_000,
        artifact_digests=(), route=None, transport=None,
    )
    return ArenaProjection(
        organization_id=_ORG, project_id=_PROJECT, run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64, run_state="FAILED",
        receipt_sequence=3, receipt_head_hash="0" * 64,
        nodes=(node,), edges=(), levels=(("checkpoint",),),
    )


def test_report_prefers_failed_over_paused_when_both_present(
    tmp_path: Path, capsys,
) -> None:
    """A run whose authoritative run_state is FAILED must report failure (rc=2), never
    PAUSED (rc=3) — even if a node's last durable receipt still shows
    AWAITING_APPROVAL. PAUSED implies the run is still resumable; a FAILED run is not."""
    from bounded_loops.graph.graph_composition import _report

    arena = _arena_with_failed_run_and_awaiting_node()
    rc = _report(False, tmp_path, "FAILED", arena, mode="local_cli")
    assert rc == 2, "a FAILED run_state must win over a stale AWAITING_APPROVAL node"
    out = capsys.readouterr().out
    assert "PAUSED" not in out.upper()


def test_report_json_prefers_failed_over_paused(tmp_path: Path, capsys) -> None:
    from bounded_loops.graph.graph_composition import _report

    arena = _arena_with_failed_run_and_awaiting_node()
    rc = _report(True, tmp_path, "FAILED", arena, mode="local_cli")
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["run_state"] == "FAILED"
    assert "paused" not in data
    assert "awaiting_approval" not in data
