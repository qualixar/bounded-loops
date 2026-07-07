"""
Acceptance tests for the starter loops that shipped without automated
coverage: a11y, data-contract, skill-authoring,
agent-authoring, content-fact-gate, osv-scanner-example, checkov-example.

Every loop is lint-checked and asserted keyless-by-manifest. The
pure-keyless loops (no external tool) are also run end-to-end and must
reach DONE. The tool-dependent loops (osv-scanner/checkov/markdown-link-check)
are run only when their binary is present on PATH — skipped, not failed,
in an environment that lacks the tool.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BL = [sys.executable, "-m", "bounded_loops.cli"]

# Loops that run with zero external tools — must reach DONE every time.
KEYLESS_LOOPS = [
    "a11y", "data-contract", "skill-authoring", "agent-authoring",
    # regulated-industry runnable loops (finance / legal / healthcare)
    "ledger-reconciliation", "contract-clause-extraction", "clinical-note-completeness",
]

# Loops whose gate shells out to an external binary — run only if present.
TOOL_LOOPS = {
    "osv-scanner-example": "osv-scanner",
    "checkov-example": "checkov",
    "content-fact-gate": "npx",   # markdown-link-check via npx (+ network)
}

ALL_LOOPS = KEYLESS_LOOPS + list(TOOL_LOOPS)


def _loop_dir(name: str) -> Path:
    d = REPO_ROOT / "loops" / name
    assert d.is_dir(), f"loop folder missing: {d}"
    return d


def _run_bl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*BL, *args], capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("name", ALL_LOOPS)
def test_bl_lint_passes(name):
    result = _run_bl("lint", str(_loop_dir(name)))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", ALL_LOOPS)
def test_manifest_is_keyless_and_no_qualixar_gate(name):
    manifest = yaml.safe_load((_loop_dir(name) / "loop.yaml").read_text())
    assert manifest["runner"]["default"] in {"stub", "shell"}
    assert manifest["gate"]["kind"] not in {
        "agentassert", "agentassay", "skillfortify", "attestar"
    }


def test_loop_catalog_represents_all_anthropic_patterns():
    expected = {
        "augmented-llm", "prompt-chaining", "routing", "parallelization",
        "orchestrator-workers", "evaluator-optimizer", "agents",
    }
    patterns = Counter(
        yaml.safe_load(path.read_text())["pattern"]
        for path in (REPO_ROOT / "loops").glob("*/loop.yaml")
    )
    assert expected.issubset(set(patterns))
    assert all(patterns[pattern] >= 1 for pattern in expected)


@pytest.mark.parametrize("name", KEYLESS_LOOPS)
def test_keyless_loop_reaches_done(name):
    loop_dir = _loop_dir(name)
    ledger = loop_dir / ".ledger.jsonl"
    if ledger.exists():
        ledger.unlink()
    try:
        result = _run_bl("run", str(loop_dir), "--yes")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "DONE" in result.stdout
    finally:
        if ledger.exists():
            ledger.unlink()


@pytest.mark.parametrize("name,binary", list(TOOL_LOOPS.items()))
def test_tool_loop_reaches_done_when_binary_present(name, binary):
    if shutil.which(binary) is None:
        pytest.skip(f"{binary} not installed on this machine")
    loop_dir = _loop_dir(name)
    ledger = loop_dir / ".ledger.jsonl"
    if ledger.exists():
        ledger.unlink()
    try:
        result = _run_bl("run", str(loop_dir), "--yes")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "DONE" in result.stdout
    finally:
        if ledger.exists():
            ledger.unlink()
