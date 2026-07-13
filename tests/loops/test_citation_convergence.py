"""The legal flagship must show a failed verdict before convergence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LOOP_DIR = REPO_ROOT / "loops" / "citation-existence-check"


@pytest.fixture(autouse=True)
def _clean_ledger() -> None:
    ledger = LOOP_DIR / ".ledger.jsonl"
    if ledger.exists():
        ledger.unlink()
    yield
    if ledger.exists():
        ledger.unlink()


def test_citation_loop_corrects_real_case_then_removes_fabrication() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bounded_loops.cli",
            "run",
            str(LOOP_DIR),
            "--yes",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "laps: 2" in result.stdout
    entries = [
        json.loads(line)
        for line in (LOOP_DIR / ".ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["decision"] for entry in entries] == ["continue", "done"]
    assert [entry["verdict"]["passed"] for entry in entries] == [False, True]
