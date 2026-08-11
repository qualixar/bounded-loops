"""RED-first tests for `bl graph approve` — Slice 1 CLI surface.

End-to-end through the CLI handler functions directly (no subprocess), matching the
existing style in test_cli_graph.py:

1. `bl graph run --execute` on a single-approval-node manifest pauses (rc == 3).
2. `bl graph approve --decision approved` resumes the SAME run to SUCCEEDED (rc == 0).
3. `bl graph approve --decision rejected` fails the run closed, durably (rc == 2).
4. A bogus --node fails closed without poisoning approvals.json; the run stays resumable.
5. Approving then rejecting the SAME node is a durable conflict (fail closed).
6. A two-gate manifest needs two `approve` calls; the first still reports PAUSED (rc == 3).
7. register() wires the `approve` subparser.
8. A run directory NOT in the nested runs_root/org/project/run_id layout is refused
   with a clear message (never silently misaddressed).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from bounded_loops.graph.cli_graph import (
    cmd_graph_approve,
    cmd_graph_run,
    cmd_graph_status,
    register,
)


def _ns(**kw: object) -> argparse.Namespace:
    kw.setdefault("json", False)
    return argparse.Namespace(**kw)


_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: cli-approve-one-gate
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

_TWO_GATE_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: cli-approve-two-gate
version: "1.0.0"
nodes:
  - id: gate1
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
  - id: gate2
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


def _write_manifest(tmp_path: Path, text: str, name: str = "graph.yaml") -> Path:
    manifest = tmp_path / name
    manifest.write_text(text, encoding="utf-8")
    return manifest


def _execute(tmp_path: Path, manifest_text: str, out_name: str, capsys) -> dict:
    """Run `bl graph run --execute` and return the parsed JSON report."""
    manifest = _write_manifest(tmp_path, manifest_text)
    out_dir = tmp_path / out_name
    rc = cmd_graph_run(_ns(manifest=str(manifest), execute=True, out=str(out_dir), json=True))
    data = json.loads(capsys.readouterr().out)
    data["_rc"] = rc
    return data


def _approve(run_dir: str, node: str, decision: str, capsys, *, inputs: str | None = None) -> dict:
    rc = cmd_graph_approve(_ns(run=run_dir, node=node, decision=decision, inputs=inputs, json=True))
    data = json.loads(capsys.readouterr().out)
    data["_rc"] = rc
    return data


# ── 1 + 2: single-gate happy path ────────────────────────────────────────────

def test_execute_pauses_then_approve_reaches_succeeded(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "run1", capsys)
    assert report["_rc"] == 3
    assert report["paused"] is True
    assert report["awaiting_approval"] == ["checkpoint"]
    real_out = report["out"]

    approved = _approve(real_out, "checkpoint", "approved", capsys)
    assert approved["_rc"] == 0
    assert approved["run_state"] == "SUCCEEDED"
    assert approved["node_id"] == "checkpoint"
    assert approved["decision"] == "approved"
    assert approved["paused"] is False

    # status must independently confirm SUCCEEDED (round-trip, no facade needed).
    rc = cmd_graph_status(_ns(run=real_out, json=True))
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["run_state"] == "SUCCEEDED"


# ── 3: rejection fails the run closed ────────────────────────────────────────

def test_execute_pauses_then_reject_fails_run_closed(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "run2", capsys)
    real_out = report["out"]

    rejected = _approve(real_out, "checkpoint", "rejected", capsys)
    assert rejected["_rc"] == 2, "a rejection is not a CLI error, but the run's FAILED outcome maps to rc 2"
    assert rejected["run_state"] == "FAILED"
    assert rejected["decision"] == "rejected"

    rc = cmd_graph_status(_ns(run=real_out, json=True))
    status = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert status["run_state"] == "FAILED"


# ── 4: bogus node fails closed, never poisons the ledger ─────────────────────

def test_approve_unknown_node_fails_closed_without_poisoning_ledger(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "run3", capsys)
    real_out = Path(report["out"])

    rc = cmd_graph_approve(_ns(run=str(real_out), node="ghost", decision="rejected", inputs=None, json=True))
    assert rc == 2
    err = capsys.readouterr().err
    assert err

    ledger = real_out / "approvals.json"
    if ledger.exists():
        stored = json.loads(ledger.read_text(encoding="utf-8"))
        assert all(r.get("node_id") != "ghost" for r in stored.get("rejections", []))
        assert all(c.get("node_id") != "ghost" for c in stored.get("commits", []))

    # The run must still be resumable — not wedged by the failed attempt.
    rc2 = cmd_graph_status(_ns(run=str(real_out), json=True))
    status = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert status["run_state"] == "RUNNING"
    assert status["nodes"][0]["state"] == "AWAITING_APPROVAL"


# ── 5: approve then reject the SAME node is a durable conflict ───────────────

def test_approve_then_reject_same_node_is_a_conflict(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "run4", capsys)
    real_out = report["out"]

    first = _approve(real_out, "checkpoint", "approved", capsys)
    assert first["_rc"] == 0

    rc = cmd_graph_approve(_ns(run=real_out, node="checkpoint", decision="rejected", inputs=None, json=True))
    assert rc == 2
    err = capsys.readouterr().err
    assert "durable approval already exists" in err


# ── 6: multi-gate needs two approve calls; first stays PAUSED ────────────────

def test_multi_gate_requires_two_approve_calls(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _TWO_GATE_MANIFEST, "run5", capsys)
    assert report["_rc"] == 3
    assert report["awaiting_approval"] == ["gate1"]
    real_out = report["out"]

    mid = _approve(real_out, "gate1", "approved", capsys)
    assert mid["_rc"] == 3, "gate2 is still pending — the run must report PAUSED, not DONE"
    assert mid["paused"] is True
    assert mid["awaiting_approval"] == ["gate2"]

    final = _approve(real_out, "gate2", "approved", capsys)
    assert final["_rc"] == 0
    assert final["run_state"] == "SUCCEEDED"


# ── 7: register() wires the approve subparser ────────────────────────────────

def test_register_wires_approve_subcommand() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args([
        "graph", "approve", "--run", "/tmp/x", "--node", "checkpoint", "--decision", "approved",
    ])
    assert args.cmd == "graph"
    assert args.graph_cmd == "approve"
    assert args.run == "/tmp/x"
    assert args.node == "checkpoint"
    assert args.decision == "approved"
    assert hasattr(args, "func")


def test_approve_decision_argument_is_validated_by_argparse() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    with pytest.raises(SystemExit):
        parser.parse_args([
            "graph", "approve", "--run", "/tmp/x", "--node", "checkpoint", "--decision", "maybe",
        ])


# ── 8 (0.4.0 flat-addressing rewrite): a run dir NOT built by the CLI's OLD nested ──
# wrapper now WORKS end-to-end, instead of being refused. Before this refactor,
# `bl graph approve` required a nested runs_root/org/project/run_id layout and refused
# anything else; both the Grok and Muse dual audits flagged that nesting as MAJOR
# public-contract debt (M2/Q4). `execute_graph_run()` called directly with a flat
# `out_dir` — bypassing the CLI entirely — is exactly the shape `for_run_dir` is meant
# to open, so this is now the STANDARD case, not a refusal.

def test_approve_works_on_a_flat_run_dir_not_produced_by_cli_nesting(tmp_path: Path, capsys) -> None:
    from bounded_loops.graph.application.execute_graph import execute_graph_run

    flat_out = tmp_path / "flat-run"
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=flat_out, run_id="run-1",
    )
    assert rc == 3  # paused, laid out flat — never touched the CLI at all
    capsys.readouterr()  # drain execute_graph_run's human-text output before the JSON call

    approved = _approve(str(flat_out), "checkpoint", "approved", capsys)
    assert approved["_rc"] == 0
    assert approved["run_state"] == "SUCCEEDED"

    rc = cmd_graph_status(_ns(run=str(flat_out), json=True))
    status = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert status["run_state"] == "SUCCEEDED"


def test_approve_requires_existing_directory(tmp_path: Path, capsys) -> None:
    rc = cmd_graph_approve(_ns(
        run=str(tmp_path / "does-not-exist"), node="checkpoint", decision="approved", inputs=None,
    ))
    assert rc == 2
    assert capsys.readouterr().err


# ── 9: `bl graph run --execute` writes FLAT — no nested org/project/run_id dirs ──

def test_execute_manifest_writes_flat_no_nested_dirs(tmp_path: Path, capsys) -> None:
    """`bl graph run --execute <manifest> --out <dir>` (the CLI path, through
    `_execute_manifest`) must write directly into `<dir>` — no
    `<dir>/local-org/local-project/graph-run` nesting (0.4.0 flat addressing)."""
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "run-flat-check", capsys)
    out_dir = tmp_path / "run-flat-check"
    assert report["out"] == str(out_dir), "the reported 'out' must be exactly --out, unmodified"
    assert (out_dir / "plan.json").is_file()
    assert (out_dir / "run-meta.json").is_file()
    assert not (out_dir / "local-org").exists(), "no nested org directory must be created"


# ── CRIT 1: a directory that is not a genuine run is refused, never misread ────

def test_approve_refuses_a_directory_that_is_not_a_real_run(tmp_path: Path, capsys) -> None:
    fake = tmp_path / "not-a-run"
    fake.mkdir()
    (fake / "notes.txt").write_text("hello", encoding="utf-8")

    rc = cmd_graph_approve(_ns(run=str(fake), node="checkpoint", decision="approved", inputs=None))
    assert rc == 2
    assert capsys.readouterr().err


# ── CRIT 2: a symlinked run_dir is refused, never opened (TOCTOU escape) ────────

def test_approve_refuses_a_symlinked_run_dir(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "real-run-for-link", capsys)
    real_out = Path(report["out"])

    link = tmp_path / "run-link"
    link.symlink_to(real_out)

    rc = cmd_graph_approve(_ns(run=str(link), node="checkpoint", decision="approved", inputs=None))
    assert rc == 2
    err = capsys.readouterr().err
    assert "symlink" in err.lower()


# ── fix 4: --inputs is refused if it is a symlink ────────────────────────────────

def test_approve_refuses_a_symlinked_inputs_file(tmp_path: Path, capsys) -> None:
    report = _execute(tmp_path, _APPROVAL_MANIFEST, "run-symlinked-inputs", capsys)
    real_out = report["out"]

    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"checkpoint": "hi"}), encoding="utf-8")
    evil_inputs = tmp_path / "evil-inputs.json"
    evil_inputs.symlink_to(secret)

    rc = cmd_graph_approve(_ns(
        run=real_out, node="checkpoint", decision="approved", inputs=str(evil_inputs),
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "symlink" in err.lower()
    assert "inputs" in err.lower()


# ── fix 3: _EXIT_PAUSED is imported from execute_graph, never redefined ─────────

def test_cli_graph_approve_imports_exit_paused_not_redefines_it() -> None:
    """`_EXIT_PAUSED` must be defined ONCE (execute_graph.py) and imported here — a
    second, independently-typed literal `3` could silently drift out of sync."""
    import inspect
    import re

    from bounded_loops.graph import cli_graph_approve
    from bounded_loops.graph.application.execute_graph import _EXIT_PAUSED

    source = inspect.getsource(cli_graph_approve)
    assert not re.search(r"(?m)^_EXIT_PAUSED\s*=\s*\d+", source), (
        "cli_graph_approve.py must import _EXIT_PAUSED, not redefine it as a literal"
    )
    assert cli_graph_approve._EXIT_PAUSED == _EXIT_PAUSED == 3


# ── fix 5: a FAILED run must never report as merely PAUSED via `bl graph approve` ──

def test_report_approve_prefers_failed_over_paused(tmp_path: Path, capsys) -> None:
    from bounded_loops.graph.application.arena_projection import (
        ArenaNodeProjection,
        ArenaProjection,
    )
    from bounded_loops.graph.cli_graph_approve import _report_approve

    node = ArenaNodeProjection(
        node_id="checkpoint", kind="approval", state="AWAITING_APPROVAL", attempt=1,
        required_effects=(), isolation="workspace_only", hard_deadline_ms=30_000,
        artifact_digests=(), route=None, transport=None,
    )
    projection = ArenaProjection(
        organization_id="local-org", project_id="local-project", run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64, run_state="FAILED",
        receipt_sequence=3, receipt_head_hash="0" * 64,
        nodes=(node,), edges=(), levels=(("checkpoint",),),
    )
    rc = _report_approve(
        _ns(node="checkpoint", decision="rejected", json=True),
        run_dir=tmp_path, projection=projection,
    )
    assert rc == 2, "a FAILED run_state must win over a stale AWAITING_APPROVAL node"
    data = json.loads(capsys.readouterr().out)
    assert data["paused"] is False
    assert data["run_state"] == "FAILED"


# ── fix 6a: `--help` documents exit code 3 as PAUSED, not an error ──────────────

def test_approve_help_documents_exit_code_3_as_paused(capsys) -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    with pytest.raises(SystemExit):
        parser.parse_args(["graph", "approve", "--help"])
    out = capsys.readouterr().out
    assert "3" in out
    assert "PAUSED" in out.upper()
    assert "not an error" in out.lower()


def test_run_execute_refuses_a_symlinked_inputs_file(tmp_path: Path, capsys) -> None:
    # M-d (dual-audit convergence MINOR): the --inputs symlink guard on `bl graph approve`
    # must be mirrored on the `bl graph run --execute` path (parity; local FS hygiene).
    manifest = _write_manifest(tmp_path, _APPROVAL_MANIFEST)
    real_inputs = tmp_path / "real_inputs.json"
    real_inputs.write_text(json.dumps({"checkpoint": "go"}), encoding="utf-8")
    link = tmp_path / "inputs_link.json"
    link.symlink_to(real_inputs)
    rc = cmd_graph_run(_ns(
        manifest=str(manifest), execute=True, out=str(tmp_path / "out"),
        inputs=str(link), json=True,
    ))
    assert rc == 2
    assert "symlink" in capsys.readouterr().err.lower()
