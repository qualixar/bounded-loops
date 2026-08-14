"""Acceptance tests for bounded_loops/hooks/graph_run_stop.py.

TDD: these tests are written first. They describe the contract the hook
must honour. Each test name states the condition and the expected decision.

Reference: capability-inventory.md section 9 — run-level terminal states:
  SUCCEEDED, FAILED, HALTED, CANCELLED, EXPIRED (terminal — allow stop)
  RUNNING, CREATED, EMPTY (non-terminal — block stop)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bounded_loops.hooks.graph_run_stop import (
    main,
    _check_workspace,
    _extract_cwd,
    _read_run_state,
)

# ---------------------------------------------------------------------------
# Helpers to write minimal controller-events.jsonl fixtures
# ---------------------------------------------------------------------------

_BASE_EVENT = {
    "organization_id": "org-test",
    "project_id": "proj-test",
    "graph_digest": "sha256:" + "a" * 64,
    "plan_digest": "sha256:" + "b" * 64,
    "policy_digest": "sha256:" + "c" * 64,
    "run_id": "run-001",
    "sequence": 1,
    "timestamp": "2025-01-01T00:00:00Z",
    "previous_hash": "0" * 64,
    "event_hash": "d" * 64,
}


def _write_events(run_dir: Path, *event_types: str) -> None:
    """Write a minimal controller-events.jsonl with the given event types."""
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, etype in enumerate(event_types, start=1):
        evt = {**_BASE_EVENT, "sequence": i, "type": etype, "payload": {"state": "RUNNING"}}
        if etype in ("run.succeeded",):
            evt["payload"] = {"state": "SUCCEEDED"}
        elif etype in ("run.failed",):
            evt["payload"] = {"state": "FAILED"}
        elif etype in ("run.halted",):
            evt["payload"] = {"state": "HALTED"}
        elif etype in ("run.cancelled",):
            evt["payload"] = {"state": "CANCELLED"}
        elif etype in ("run.expired",):
            evt["payload"] = {"state": "EXPIRED"}
        lines.append(json.dumps(evt))
    (run_dir / "controller-events.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal .bounded-loops workspace directory structure."""
    ws = tmp_path / ".bounded-loops"
    (ws / "runs").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# _read_run_state
# ---------------------------------------------------------------------------


def test_read_run_state_no_file_returns_none(tmp_path: Path) -> None:
    """A run directory without controller-events.jsonl returns None (unknown state)."""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    assert _read_run_state(run_dir) is None


def test_read_run_state_empty_file_returns_none(tmp_path: Path) -> None:
    """An empty controller-events.jsonl has no state yet — return None."""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    (run_dir / "controller-events.jsonl").write_text("", encoding="utf-8")
    assert _read_run_state(run_dir) is None


def test_read_run_state_running(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started")
    assert _read_run_state(run_dir) == "RUNNING"


def test_read_run_state_succeeded(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.succeeded")
    assert _read_run_state(run_dir) == "SUCCEEDED"


def test_read_run_state_failed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.failed")
    assert _read_run_state(run_dir) == "FAILED"


def test_read_run_state_halted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.halted")
    assert _read_run_state(run_dir) == "HALTED"


def test_read_run_state_cancelled(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.cancelled")
    assert _read_run_state(run_dir) == "CANCELLED"


def test_read_run_state_expired(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.expired")
    assert _read_run_state(run_dir) == "EXPIRED"


def test_read_run_state_invalid_line_is_skipped(tmp_path: Path) -> None:
    """A malformed line in the event log is skipped; the hook must not crash."""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    (run_dir / "controller-events.jsonl").write_text(
        "not json\n" + json.dumps({**_BASE_EVENT, "sequence": 2, "type": "run.started", "payload": {"state": "RUNNING"}}),
        encoding="utf-8",
    )
    # The valid run.started line should still be read.
    assert _read_run_state(run_dir) == "RUNNING"


# ---------------------------------------------------------------------------
# _check_workspace
# ---------------------------------------------------------------------------


def test_check_workspace_no_runs_dir_allows(tmp_path: Path) -> None:
    """If the workspace has no runs/ directory, there are no active runs → allow."""
    ws_root = tmp_path
    passed, reason = _check_workspace(ws_root)
    assert passed is True
    assert "no active" in reason.lower() or "no runs" in reason.lower()


def test_check_workspace_empty_runs_dir_allows(tmp_path: Path) -> None:
    """An empty runs/ directory means no runs have ever happened → allow."""
    ws_root = _make_workspace(tmp_path)
    passed, reason = _check_workspace(ws_root)
    assert passed is True


def test_check_workspace_all_runs_terminal_allows(tmp_path: Path) -> None:
    """All runs in terminal states → allow."""
    ws_root = _make_workspace(tmp_path)
    runs_dir = ws_root / ".bounded-loops" / "runs"
    _write_events(runs_dir / "run-001", "run.created", "run.started", "run.succeeded")
    _write_events(runs_dir / "run-002", "run.created", "run.started", "run.failed")
    passed, reason = _check_workspace(ws_root)
    assert passed is True


def test_check_workspace_active_run_blocks(tmp_path: Path) -> None:
    """A run in RUNNING state must block session-stop.

    This is the load-bearing test — the core contract of the hook.
    """
    ws_root = _make_workspace(tmp_path)
    runs_dir = ws_root / ".bounded-loops" / "runs"
    _write_events(runs_dir / "run-active", "run.created", "run.started")
    passed, reason = _check_workspace(ws_root)
    assert passed is False
    assert "run-active" in reason or "RUNNING" in reason or "active" in reason.lower()


def test_check_workspace_mixed_runs_blocks_on_active(tmp_path: Path) -> None:
    """One terminal run + one active run → block (the active one matters)."""
    ws_root = _make_workspace(tmp_path)
    runs_dir = ws_root / ".bounded-loops" / "runs"
    _write_events(runs_dir / "run-done", "run.created", "run.started", "run.succeeded")
    _write_events(runs_dir / "run-live", "run.created", "run.started")
    passed, reason = _check_workspace(ws_root)
    assert passed is False


def test_check_workspace_run_dir_without_events_file_is_skipped(tmp_path: Path) -> None:
    """A run directory with no controller-events.jsonl is ignored (fail-open)."""
    ws_root = _make_workspace(tmp_path)
    runs_dir = ws_root / ".bounded-loops" / "runs"
    (runs_dir / "orphan-dir").mkdir()  # no events file
    passed, reason = _check_workspace(ws_root)
    assert passed is True


def test_check_workspace_run_with_unknown_state_is_skipped(tmp_path: Path) -> None:
    """A run with no recognisable state events is treated as unknown (fail-open)."""
    ws_root = _make_workspace(tmp_path)
    runs_dir = ws_root / ".bounded-loops" / "runs"
    run_dir = runs_dir / "run-weird"
    run_dir.mkdir()
    # Write events that have no state-changing type
    (run_dir / "controller-events.jsonl").write_text(
        json.dumps({**_BASE_EVENT, "type": "node.running", "payload": {"state": "RUNNING", "node_id": "n1", "attempt": 1}}),
        encoding="utf-8",
    )
    passed, reason = _check_workspace(ws_root)
    assert passed is True  # unknown state → fail-open


# ---------------------------------------------------------------------------
# _extract_cwd (same protocol as verify_bounded_loop.py)
# ---------------------------------------------------------------------------


def test_extract_cwd_claude_code_uses_cwd_field() -> None:
    assert _extract_cwd({"cwd": "/workspace"}, "claude-code") == "/workspace"


def test_extract_cwd_codex_uses_cwd_field() -> None:
    assert _extract_cwd({"cwd": "/workspace"}, "codex") == "/workspace"


def test_extract_cwd_antigravity_uses_workspacePaths_first() -> None:
    assert _extract_cwd({"workspacePaths": ["/a", "/b"]}, "antigravity") == "/a"


def test_extract_cwd_missing_returns_none() -> None:
    assert _extract_cwd({}, "claude-code") is None


def test_extract_cwd_unknown_tool_returns_none() -> None:
    assert _extract_cwd({"cwd": "/x"}, "unknown-tool") is None


# ---------------------------------------------------------------------------
# main() — integration over the exit-code / JSON protocol
# ---------------------------------------------------------------------------


def test_main_no_workspace_allows_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory with no .bounded-loops/ workspace must allow (exit 0)."""
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})(),
    )
    code = main(["graph_run_stop.py", "claude-code"])
    assert code == 0


def test_main_active_run_blocks_exit_2_with_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active graph run must block with exit 2 and a human-readable stderr reason."""
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-active",
        "run.created", "run.started",
    )
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})(),
    )
    code = main(["graph_run_stop.py", "claude-code"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.err  # must explain WHY it's blocking


def test_main_active_run_antigravity_prints_deny_json(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antigravity protocol: deny → JSON output + exit 1."""
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-active",
        "run.created", "run.started",
    )
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"workspacePaths": [str(tmp_path)]})})(),
    )
    code = main(["graph_run_stop.py", "antigravity"])
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "deny"
    assert code == 1


def test_main_all_terminal_runs_allows_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-done",
        "run.created", "run.started", "run.succeeded",
    )
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})(),
    )
    code = main(["graph_run_stop.py", "claude-code"])
    assert code == 0


def test_main_malformed_stdin_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed hook payload → fail open (exit 0). Must never crash."""
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: "not json {"})(),
    )
    code = main(["graph_run_stop.py", "claude-code"])
    assert code == 0


def test_main_missing_cwd_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cwd in payload → allow, don't guess."""
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: "{}"})(),
    )
    assert main(["graph_run_stop.py", "claude-code"]) == 0


def test_main_defaults_to_claude_code_when_argv_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: "{}"})())
    assert main(["graph_run_stop.py"]) == 0


def test_main_antigravity_all_terminal_prints_allow_json(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-done",
        "run.created", "run.started", "run.failed",
    )
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"workspacePaths": [str(tmp_path)]})})(),
    )
    code = main(["graph_run_stop.py", "antigravity"])
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "allow"
    assert code == 0
