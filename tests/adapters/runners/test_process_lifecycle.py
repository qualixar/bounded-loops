"""Executable contract for F0.2 controller-owned subprocess turns."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest

from bounded_loops.adapters.runners.process_lifecycle import (
    ProcessTurn,
    TurnState,
)


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_completed_turn_caps_output_and_redacts_secret(tmp_path: Path) -> None:
    turn = ProcessTurn.start(
        _python("print('prefix ' + 'x' * 100 + ' CANARY_SECRET')"),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=32,
        redactions=("CANARY_SECRET",),
    )

    result = turn.wait(timeout_s=2)

    assert result.state is TurnState.COMPLETED
    assert "CANARY_SECRET" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert len(result.stdout.encode("utf-8")) <= 32
    assert result.output_truncated is True


def test_cancel_terminates_parent_and_descendant_process_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    child_code = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
            f"pathlib.Path({str(grandchild_pid_path)!r}).write_text(str(grandchild.pid), encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    code = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    turn = ProcessTurn.start(
        _python(code),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=1024,
    )
    deadline = time.monotonic() + 2
    while (
        not child_pid_path.exists() or not grandchild_pid_path.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid_path.exists() and grandchild_pid_path.exists(), "fixture descendants did not start"
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

    turn.cancel("test cancellation")
    turn.cancel("idempotent second cancellation")
    result = turn.wait(timeout_s=2)

    assert result.state is TurnState.CANCELLED
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_wait_deadline_terminates_a_hanging_turn(tmp_path: Path) -> None:
    turn = ProcessTurn.start(
        _python("import time; time.sleep(60)"),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=1024,
    )

    result = turn.wait(timeout_s=0.05)

    assert result.state is TurnState.TIMED_OUT
    assert result.returncode is not None
