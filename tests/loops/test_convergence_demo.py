"""End-to-end acceptance tests for the multi-lap flagship demonstration."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LOOP_DIR = REPO_ROOT / "loops" / "convergence-demo"
BL = [sys.executable, "-m", "bounded_loops.cli"]


@pytest.fixture(autouse=True)
def _clean_ledger() -> None:
    ledger = LOOP_DIR / ".ledger.jsonl"
    if ledger.exists():
        ledger.unlink()
    yield
    if ledger.exists():
        ledger.unlink()


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    assert LOOP_DIR.is_dir(), f"multi-lap flagship loop missing: {LOOP_DIR}"
    return subprocess.run(
        [*BL, "run", str(LOOP_DIR), "--yes", *extra],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ledger_entries() -> list[dict]:
    ledger = LOOP_DIR / ".ledger.jsonl"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]


def test_convergence_demo_fails_twice_then_passes_on_lap_three() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr + result.stdout
    assert "[DONE]" in result.stdout
    assert "laps: 3" in result.stdout
    entries = _ledger_entries()
    assert [entry["decision"] for entry in entries] == ["continue", "continue", "done"]
    assert [entry["verdict"]["passed"] for entry in entries] == [False, False, True]
    assert [entry["budget_spent"]["laps"] for entry in entries] == [1, 2, 3]


def test_convergence_demo_bound_stops_before_the_correct_fix() -> None:
    result = _run("--max-iterations", "2")

    assert result.returncode == 1, result.stderr + result.stdout
    assert "[HALT]" in result.stdout
    assert "max_iterations 2" in result.stdout
    entries = _ledger_entries()
    assert [entry["decision"] for entry in entries] == ["continue", "continue", "halt"]
    assert entries[-1]["verdict"]["passed"] is False
