from __future__ import annotations

import yaml

from bounded_loops.application.loop_audit import (
    audit_contribution,
    audit_loop,
    audit_loops,
    discover_loop_dirs,
)


def _write_loop(tmp_path, *, readme="## Make it real\n", gate=None, bounds=None):
    loop_dir = tmp_path / "loop-a"
    (loop_dir / "seed").mkdir(parents=True)
    (loop_dir / "cassettes").mkdir()
    (loop_dir / "PROMPT.md").write_text("Do not edit the gate.\n", encoding="utf-8")
    (loop_dir / "README.md").write_text(readme, encoding="utf-8")
    (loop_dir / "cassettes" / "default.json").write_text(
        '{"version":1,"interactions":[{"lap":1,"agent_output":"ok","actions":[{"type":"noop"}],"agent_claimed_done":true,"changed":false}]}',
        encoding="utf-8",
    )
    loop_yaml = {
        "name": "loop-a",
        "description": "A loop",
        "pattern": "evaluator-optimizer",
        "role": ["testing"],
        "rung": "L1",
        "runner": {"default": "stub"},
        "gate": gate or {"kind": "pytest"},
    }
    (loop_dir / "loop.yaml").write_text(yaml.dump(loop_yaml), encoding="utf-8")
    (loop_dir / "bounds.yaml").write_text(yaml.dump(bounds or {"max_iterations": 2}), encoding="utf-8")
    return loop_dir


def test_discover_loop_dirs_from_repo_root_shape(tmp_path):
    loop_dir = _write_loop(tmp_path / "loops")
    assert discover_loop_dirs(tmp_path) == [loop_dir.resolve()]


def test_audit_loop_passes_valid_minimal_loop(tmp_path):
    loop_dir = _write_loop(tmp_path)
    result = audit_loop(loop_dir)
    assert result.passed is True
    assert result.errors == []


def test_audit_loop_fails_machine_specific_readme_path(tmp_path):
    loop_dir = _write_loop(tmp_path, readme="/Users/someone/project\n")
    result = audit_loop(loop_dir)
    assert result.passed is False
    assert any("machine-specific" in error for error in result.errors)


def test_audit_loops_returns_all_results(tmp_path):
    _write_loop(tmp_path / "loops")
    results = audit_loops(tmp_path)
    assert len(results) == 1


def test_l2_demo_loop_with_production_bounds_has_no_approval_warning(tmp_path):
    loop_dir = _write_loop(
        tmp_path,
        bounds={"max_iterations": 2, "require_approval": False},
    )
    raw = yaml.safe_load((loop_dir / "loop.yaml").read_text(encoding="utf-8"))
    raw["rung"] = "L2"
    (loop_dir / "loop.yaml").write_text(yaml.dump(raw), encoding="utf-8")
    (loop_dir / "bounds.production.yaml").write_text(
        "max_iterations: 2\nrequire_approval: true\n", encoding="utf-8"
    )

    result = audit_loop(loop_dir)

    assert not any("approval" in warning for warning in result.warnings)


def _write_contribution(tmp_path, *, gate=None, forbid=True):
    loop_dir = _write_loop(
        tmp_path,
        readme=(
            "## Make it real\n\n"
            "The unfixed seed must fail. After the fix, the gate must pass.\n\n"
            "Expected: `\u2713 [DONE] Gate verified on lap 1`\n"
        ),
        gate=gate,
    )
    raw = yaml.safe_load((loop_dir / "loop.yaml").read_text(encoding="utf-8"))
    if forbid:
        raw["forbid"] = ["seed/test_*.py"]
    (loop_dir / "loop.yaml").write_text(yaml.dump(raw), encoding="utf-8")
    return loop_dir


def test_audit_contribution_accepts_real_gate_anchor_and_expected_output(tmp_path):
    loop_dir = _write_contribution(tmp_path)

    assert audit_contribution(loop_dir) == []


def test_audit_contribution_rejects_missing_verification_anchor(tmp_path):
    loop_dir = _write_contribution(tmp_path, forbid=False)

    errors = audit_contribution(loop_dir)

    assert "contribution requires a forbid guard for verification anchors" in errors


def test_audit_contribution_rejects_noop_command_gate(tmp_path):
    loop_dir = _write_contribution(
        tmp_path,
        gate={"kind": "command", "run": "echo ok"},
    )

    errors = audit_contribution(loop_dir)

    assert "contribution gate must verify a real acceptance condition" in errors
