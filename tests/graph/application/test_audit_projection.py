"""Tests for the cross-model audit read-side projection (C-075 Arena read path).

Coverage:
1. End-to-end: an AUDIT-kind node whose stand-in CLI outputs a valid AuditResult
   JSON → execute_graph_run persists audit-plan.json → read_audit_projection returns
   correct coverage with released=True → rendered Arena HTML contains the audit section.

2. Fail-closed: a malformed (non-JSON) AUDIT node artifact is recorded as a note;
   the mandatory cell is missing → released=False.

3. Missing mandatory cell: AuditPlan declares cell X, no AUDIT node covers it →
   released=False.

4. Regression: a run without audit-plan.json renders Arena without audit_coverage
   (backward-compatible — existing test expectations unchanged).

Hermetic: stand-in CLI binaries (tiny shell scripts) + in-process artifact stores.
No subscription, no quota, no network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import ArenaReadRequest, read_arena_projection
from bounded_loops.graph.application.audit_projection import read_audit_projection
from bounded_loops.graph.application.execute_graph import execute_graph_run
from bounded_loops.graph.arena.cli_arena import cmd_graph_arena
from bounded_loops.graph.cli_graph import _load_plan_from_run_dir
from bounded_loops.graph.domain.audits import AuditAssignment, AuditCell, AuditPlan

_ORG = "local-org"
_PROJECT = "local-project"
_RUN_ID = "audit-run-1"

# ── shared constants for a valid SHA-256 digest (fake, format-valid) ──────────

_ARTIFACT_DIGEST = "sha256:" + "f" * 64
_RUBRIC_DIGEST   = "sha256:" + "e" * 64
_ASSIGN_RUBRIC   = "sha256:" + "d" * 64

# ── AUDIT-kind manifest ────────────────────────────────────────────────────────
# One local-cli AUDIT node.  It will receive a prompt and print a JSON AuditResult.

_AUDIT_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: audit-graph
version: "1.0.0"
nodes:
  - id: auditor
    kind: audit
    audit_profile: "accuracy-v1"
    inputs: {}
    outputs: {report: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
    connection_slot: evaluator
edges: []
connection_slots:
  - id: evaluator
    requires: [text_generation]
    data_class_max: public
policies: {data_class: public, fail_mode: fail_closed}
"""

_CONNECTIONS = [
    {
        "binding_id": "binding-eval",
        "slot_id": "evaluator",
        "connector_id": "local-cli",
        "connector_version": "1.0.0",
        "connection_id": "conn-eval",
        "admission_digest": "sha256:" + "b" * 64,
        "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "auditor-model",
        "model_target": "eval",
        "region": "local",
        "fallback": False,
        "capabilities": ["text_generation"],
        "data_class_max": "public",
        "allowed_effects": ["read_only"],
        "isolation": "workspace_only",
        "transport": "local_cli",
        "admitted": True,
    }
]

# ── valid AuditResult JSON that the stand-in CLI outputs ──────────────────────

_AUDIT_RESULT_JSON = json.dumps({
    "assessor": "evaluator-model",
    "cell": "accuracy",
    "finding": None,
    "producer": "original-producer",
})

# ── AuditPlan with one mandatory "accuracy" cell ──────────────────────────────

def _make_audit_plan() -> AuditPlan:
    return AuditPlan(
        artifact_digest=_ARTIFACT_DIGEST,
        rubric_digest=_RUBRIC_DIGEST,
        mandatory_cells=(AuditCell(name="accuracy", mandatory=True),),
        assignments=(
            AuditAssignment(
                cell="accuracy",
                model_id="evaluator-model",
                tool_id="audit-tool",
                version="1.0.0",
                rubric_digest=_ASSIGN_RUBRIC,
                independence="assessor != producer",
            ),
        ),
    )


def _audit_plan_json() -> str:
    plan = _make_audit_plan()
    return json.dumps({
        "artifact_digest": plan.artifact_digest,
        "rubric_digest": plan.rubric_digest,
        "mandatory_cells": [
            {"name": c.name, "mandatory": c.mandatory}
            for c in plan.mandatory_cells
        ],
        "assignments": [
            {
                "cell": a.cell,
                "independence": a.independence,
                "model_id": a.model_id,
                "rubric_digest": a.rubric_digest,
                "tool_id": a.tool_id,
                "version": a.version,
            }
            for a in plan.assignments
        ],
    })


# ── test helpers ───────────────────────────────────────────────────────────────

def _standin(tmp_path: Path, body: str) -> str:
    cli = tmp_path / "standin_cli"
    cli.write_text(body)
    cli.chmod(0o755)
    return str(cli)


class _Auth:
    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _Verify:
    def verify(self, identity: object, receipts: object) -> None:
        return None


def _run_with_audit(tmp_path: Path, cli_body: str, plan_json: str | None = None) -> tuple[Path, int]:
    """Run execute_graph_run with an AUDIT node; return (out_dir, exit_code)."""
    standin = _standin(tmp_path, cli_body)
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_AUDIT_MANIFEST,
        manifest_suffix=".yaml",
        connections_raw=_CONNECTIONS,
        node_prompts={"auditor": "evaluate accuracy"},
        out_dir=out,
        run_id=_RUN_ID,
        cli_profiles={"auditor-model": CliProfile(standin)},
        environ={"PATH": os.environ.get("PATH", "")},
        audit_plan_json=plan_json,
    )
    return out, rc


def _load_arena(out: Path):
    plan, identity, meta = _load_plan_from_run_dir(out)
    event_log = GraphEventLog(out / "controller-events.jsonl", identity)
    arena = read_arena_projection(
        plan, event_log,
        ArenaReadRequest(
            subject_id=_ORG, organization_id=_ORG,
            project_id=_PROJECT, run_id=_RUN_ID,
        ),
        _Auth(), _Verify(),
    )
    return plan, identity, event_log, arena, meta


def _arena_data(html: str) -> dict:
    match = re.search(
        r'<script id="arena-data" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    assert match, "arena-data block missing from rendered HTML"
    return json.loads(match.group(1))


# ── test 1: end-to-end valid audit result ─────────────────────────────────────

def test_audit_projection_end_to_end(tmp_path):
    """Valid AUDIT node artifact + audit-plan.json → released=True, correct cell."""
    plan_json = _audit_plan_json()
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{_AUDIT_RESULT_JSON}'\n",
        plan_json=plan_json,
    )
    assert rc == 0, "graph run must succeed"
    assert (out / "audit-plan.json").is_file(), "audit-plan.json must be persisted"
    assert (out / "audit-plan.json").read_text(encoding="utf-8") == plan_json

    plan, identity, event_log, _, _ = _load_arena(out)
    artifact_store = LocalArtifactStore(out / "artifacts")
    audit_plan = _make_audit_plan()

    proj = read_audit_projection(
        plan=plan,
        event_log=event_log,
        artifact_store=artifact_store,
        audit_plan=audit_plan,
        organization_id=_ORG,
        project_id=_PROJECT,
    )

    assert proj.released is True, f"expected released=True, got reason: {proj.reason}"
    assert len(proj.cells) == 1
    cell = proj.cells[0]
    assert cell.cell == "accuracy"
    assert cell.mandatory is True
    assert "evaluator-model" in cell.covered_by
    assert cell.verdict_severity == "none"
    assert cell.blocking is False
    assert proj.blocking_cells == ()
    assert proj.notes == ()


# ── test 2: Arena HTML contains audit section when plan present ───────────────

def test_arena_html_contains_audit_section(tmp_path):
    """When audit-plan.json is present, rendered Arena HTML includes audit_coverage."""
    plan_json = _audit_plan_json()
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{_AUDIT_RESULT_JSON}'\n",
        plan_json=plan_json,
    )
    assert rc == 0

    rc2 = cmd_graph_arena(argparse.Namespace(run=str(out), out=None))
    assert rc2 == 0

    html = (out / "arena.html").read_text(encoding="utf-8")
    data = _arena_data(html)

    assert "audit_coverage" in data, "audit_coverage must be in Arena payload"
    cov = data["audit_coverage"]
    assert cov["released"] is True
    assert len(cov["cells"]) == 1
    assert cov["cells"][0]["cell"] == "accuracy"
    assert cov["notes"] == []
    # The release decision reason must mention "cleared"
    assert "cleared" in cov["reason"]
    # HTML must include the audit section marker
    assert 'id="audit-section"' in html


# ── test 3: malformed artifact is recorded as note, not a crash ───────────────

def test_audit_projection_malformed_artifact_is_noted(tmp_path):
    """AUDIT node outputs non-JSON garbage → note recorded, released=False (missing cell)."""
    out, rc = _run_with_audit(
        tmp_path,
        cli_body="#!/bin/sh\nprintf 'NOT_VALID_JSON_GARBAGE'\n",
        plan_json=_audit_plan_json(),
    )
    assert rc == 0  # the run itself succeeds; the CLI produced output

    plan, identity, event_log, _, _ = _load_arena(out)
    artifact_store = LocalArtifactStore(out / "artifacts")
    audit_plan = _make_audit_plan()

    proj = read_audit_projection(
        plan=plan,
        event_log=event_log,
        artifact_store=artifact_store,
        audit_plan=audit_plan,
        organization_id=_ORG,
        project_id=_PROJECT,
    )

    assert proj.released is False, "malformed artifact → missing cell → must block release"
    assert len(proj.notes) == 1, "exactly one note must be recorded for the malformed artifact"
    assert "auditor" in proj.notes[0], "note must mention the node id"
    # The mandatory cell is missing (not covered by any valid result)
    assert any(c.cell == "accuracy" and c.blocking for c in proj.cells)


# ── test 4: missing mandatory cell yields released=False ─────────────────────

def test_audit_projection_missing_mandatory_cell_blocks_release(tmp_path):
    """AuditPlan has cell 'accuracy'; AUDIT node covers cell 'latency' → released=False."""
    wrong_cell_result = json.dumps({
        "assessor": "evaluator-model",
        "cell": "latency",       # wrong cell — does not cover 'accuracy'
        "finding": None,
        "producer": "original-producer",
    })
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{wrong_cell_result}'\n",
        plan_json=_audit_plan_json(),
    )
    assert rc == 0

    plan, identity, event_log, _, _ = _load_arena(out)
    artifact_store = LocalArtifactStore(out / "artifacts")
    audit_plan = _make_audit_plan()

    proj = read_audit_projection(
        plan=plan,
        event_log=event_log,
        artifact_store=artifact_store,
        audit_plan=audit_plan,
        organization_id=_ORG,
        project_id=_PROJECT,
    )

    assert proj.released is False
    # The mandatory 'accuracy' cell must be missing (blocking)
    assert any(c.cell == "accuracy" and c.blocking for c in proj.cells)
    assert "accuracy" in proj.blocking_cells


# ── test 5: regression — no audit-plan.json → Arena unchanged ────────────────

def test_arena_no_audit_plan_renders_unchanged(tmp_path):
    """A run without audit-plan.json renders an Arena with NO audit_coverage key.

    This is the backward-compatibility regression: nothing in the existing Arena
    payload or HTML changes when the audit plan is absent.
    """
    # Use the demo run — it has no audit-plan.json
    from bounded_loops.graph.cli_graph import cmd_graph_demo
    import argparse as _ap
    demo_out = tmp_path / "demo"
    rc = cmd_graph_demo(_ap.Namespace(out=str(demo_out), json=True))
    assert rc == 0
    assert not (demo_out / "audit-plan.json").exists()

    rc2 = cmd_graph_arena(_ap.Namespace(run=str(demo_out), out=None))
    assert rc2 == 0

    html = (demo_out / "arena.html").read_text(encoding="utf-8")
    data = _arena_data(html)

    # audit_coverage must be ABSENT — not None, not {}, not present at all
    assert "audit_coverage" not in data, (
        "audit_coverage must not appear in payload when no audit-plan.json exists"
    )
    # The existing fields must still be there
    assert "run_id" in data
    assert "nodes" in data
    assert data["demonstration"] is True


# ── C-079 dual-audit regressions ─────────────────────────────────────────────

_POISON_SEVERITY_RESULT = json.dumps({
    "assessor": "evaluator-model",
    "cell": "accuracy",
    "finding": {"finding_id": "F1", "severity": "critical", "disposition": "open"},  # invalid severity
    "producer": "original-producer",
})


def test_audit_projection_poison_severity_is_noted_not_crash(tmp_path):
    """BLOCKER B2/F-02: a well-formed JSON AuditResult carrying an INVALID severity must fail closed
    as a NOTE — never raise past the per-artifact guard and abort the whole projection."""
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{_POISON_SEVERITY_RESULT}'\n",
        plan_json=_audit_plan_json(),
    )
    assert rc == 0

    plan, identity, event_log, _, _ = _load_arena(out)
    artifact_store = LocalArtifactStore(out / "artifacts")
    proj = read_audit_projection(
        plan=plan, event_log=event_log, artifact_store=artifact_store,
        audit_plan=_make_audit_plan(), organization_id=_ORG, project_id=_PROJECT,
    )
    # projection did NOT raise; the poison artifact is a note, the mandatory cell is missing → blocked
    assert proj.released is False
    assert len(proj.notes) == 1, "the poison severity must be recorded as exactly one note"
    assert "auditor" in proj.notes[0]
    assert any(c.cell == "accuracy" and c.blocking and c.missing for c in proj.cells)


def test_arena_poison_severity_keeps_blocked_section(tmp_path):
    """BLOCKER B1+B2: with a poison artifact present, `bl graph arena` must still render a BLOCKED
    audit section — the section must NOT vanish (which would look identical to 'no audit gate')."""
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{_POISON_SEVERITY_RESULT}'\n",
        plan_json=_audit_plan_json(),
    )
    assert rc == 0
    rc2 = cmd_graph_arena(argparse.Namespace(run=str(out), out=None))
    assert rc2 == 0

    html = (out / "arena.html").read_text(encoding="utf-8")
    data = _arena_data(html)
    assert "audit_coverage" in data, "poison artifact must NOT omit the audit section"
    assert data["audit_coverage"]["released"] is False
    assert 'id="audit-section"' in html


def test_arena_corrupt_audit_plan_blocks_not_omitted(tmp_path):
    """BLOCKER B1/F-03: a PRESENT but corrupt audit-plan.json must render a BLOCKED audit section,
    never be silently omitted (fail-OPEN presentation of a release control)."""
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{_AUDIT_RESULT_JSON}'\n",
        plan_json=None,  # a run with NO plan …
    )
    assert rc == 0
    # … then inject a corrupt plan file into the finished run dir
    (out / "audit-plan.json").write_text("{ this is not valid json", encoding="utf-8")

    rc2 = cmd_graph_arena(argparse.Namespace(run=str(out), out=None))
    assert rc2 == 0, "arena must still render the base page"

    html = (out / "arena.html").read_text(encoding="utf-8")
    data = _arena_data(html)
    assert "audit_coverage" in data, "corrupt plan must NOT omit the audit section"
    cov = data["audit_coverage"]
    assert cov["released"] is False
    assert "fail" in cov["reason"].lower() or "invalid" in cov["reason"].lower()


def test_audit_projection_self_grader_is_marked_and_blocks(tmp_path):
    """MAJOR M1/F-01: a self-graded cell (assessor == producer) must show '(self)' in covered_by and
    be producer_only + blocking — the Arena must never present it as independently covered."""
    self_graded = json.dumps({
        "assessor": "same-model", "cell": "accuracy", "finding": None, "producer": "same-model",
    })
    out, rc = _run_with_audit(
        tmp_path,
        cli_body=f"#!/bin/sh\nprintf '%s' '{self_graded}'\n",
        plan_json=_audit_plan_json(),
    )
    assert rc == 0

    plan, identity, event_log, _, _ = _load_arena(out)
    artifact_store = LocalArtifactStore(out / "artifacts")
    proj = read_audit_projection(
        plan=plan, event_log=event_log, artifact_store=artifact_store,
        audit_plan=_make_audit_plan(), organization_id=_ORG, project_id=_PROJECT,
    )
    assert proj.released is False
    cell = next(c for c in proj.cells if c.cell == "accuracy")
    assert cell.producer_only is True
    assert cell.blocking is True
    assert cell.covered_by == ("same-model (self)",), "self-grader must be marked, never shown as independent coverage"
