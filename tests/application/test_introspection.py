from __future__ import annotations

import yaml

from bounded_loops.application.introspection import list_gates, show_loop


def _write_loop(tmp_path, *, gate=None, rung="L2", require_approval=False):
    loop_dir = tmp_path / "loop-a"
    (loop_dir / "seed").mkdir(parents=True)
    (loop_dir / "cassettes").mkdir()
    (loop_dir / "PROMPT.md").write_text("Fix it.\n", encoding="utf-8")
    (loop_dir / "README.md").write_text("## Make it real\n", encoding="utf-8")
    (loop_dir / "cassettes" / "default.json").write_text(
        '{"version":1,"interactions":[{"lap":1,"agent_output":"ok","actions":[{"type":"noop"}],"agent_claimed_done":true,"changed":false}]}',
        encoding="utf-8",
    )
    (loop_dir / "loop.yaml").write_text(yaml.dump({
        "name": "loop-a",
        "description": "A loop",
        "pattern": "evaluator-optimizer",
        "role": ["testing"],
        "rung": rung,
        "runner": {"default": "stub"},
        "gate": gate or {"kind": "command", "run": "true"},
    }), encoding="utf-8")
    (loop_dir / "bounds.yaml").write_text(yaml.dump({
        "max_iterations": 2,
        "require_approval": require_approval,
    }), encoding="utf-8")
    (loop_dir / "bounds.production.yaml").write_text(
        "max_iterations: 2\nrequire_approval: true\n", encoding="utf-8"
    )
    return loop_dir


def test_show_loop_includes_risk_and_content_hash(tmp_path):
    loop_dir = _write_loop(tmp_path)
    data = show_loop(loop_dir)
    assert data["name"] == "loop-a"
    assert "demo-approval-bypass" in data["risk"]
    assert "production-bounds-available" in data["risk"]
    assert len(data["content_hash"]) == 64


def test_show_loop_composite_gate_children(tmp_path):
    loop_dir = _write_loop(tmp_path, gate={
        "kind": "composite",
        "mode": "all",
        "gates": [
            {"kind": "command", "run": "true"},
            {"kind": "pytest"},
        ],
    })
    data = show_loop(loop_dir)
    assert data["gate"]["kind"] == "composite"
    assert len(data["gate"]["children"]) == 2


def test_list_gates_includes_core_gates():
    kinds = {gate["kind"] for gate in list_gates()}
    assert {"command", "pytest", "jsonschema", "composite", "osv", "checkov"}.issubset(kinds)