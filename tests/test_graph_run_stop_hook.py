"""Acceptance tests for bounded_loops/hooks/graph_run_stop.py.

TDD: these tests are written first. They describe the contract the hook
must honour. Each test name states the condition and the expected decision.

Reference: capability-inventory.md section 9 — run-level terminal states:
  SUCCEEDED, FAILED, HALTED, CANCELLED, EXPIRED (terminal — allow stop)
  RUNNING, CREATED, EMPTY (non-terminal — block stop)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from bounded_loops.hooks import graph_run_stop
from bounded_loops.hooks.graph_run_stop import (
    main,
    _check_workspace,
    _extract_cwd,
    _read_run_state,
)


@pytest.fixture(autouse=True)
def _hook_resolves_from_the_payload_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tests/conftest.py` sets $BOUNDED_LOOPS_WORKSPACE for every test, and `discover()` gives
    an explicit workspace precedence over walking up — correctly, but it would mean these tests
    measured the fixture's directory rather than the one in the hook payload."""
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)

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
        # `event_type`, not `type`. The engine writes `event_type`; this helper wrote `type`,
        # so every test in this file passed against a log shape that does not exist and the
        # hook was inert in production. See test_the_hook_blocks_a_run_the_REAL_ENGINE_produced.
        evt = {**_BASE_EVENT, "sequence": i, "event_type": etype, "payload": {"state": "RUNNING"}}
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


def test_read_run_state_cancelled(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.cancelled")
    assert _read_run_state(run_dir) == "CANCELLED"


def test_run_halted_and_run_expired_are_NOT_events_this_engine_emits(tmp_path: Path) -> None:
    """HALTED and EXPIRED are terminal states with no event type that produces them.

    This test replaces two that asserted the opposite. They were written from the terminal-state
    list in a capability inventory: given HALTED in the set, `run.halted` looks like the obvious
    event name, and both the mapping and its tests were written to agree with each other. Neither
    name appears anywhere in the engine — nothing emits them and `_apply` has no branch for them.

    Kept as a live test rather than a deletion so that if `run.halted` is ever implemented for
    real, this fails and forces the hook's map to be updated deliberately.
    """
    import inspect

    from bounded_loops.graph.adapters.persistence import event_log

    source = inspect.getsource(event_log)
    assert '"run.halted"' not in source
    assert '"run.expired"' not in source

    run_dir = tmp_path / "run-001"
    _write_events(run_dir, "run.created", "run.started", "run.halted")
    # Unrecognised event -> the last state the hook DID recognise. Reading it as terminal would
    # let the host claim "done" on the strength of an event the engine never writes.
    assert _read_run_state(run_dir) == "RUNNING"


def test_read_run_state_invalid_line_is_skipped(tmp_path: Path) -> None:
    """A malformed line in the event log is skipped; the hook must not crash."""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    (run_dir / "controller-events.jsonl").write_text(
        "not json\n" + json.dumps({**_BASE_EVENT, "sequence": 2, "event_type": "run.started", "payload": {"state": "RUNNING"}}),
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


# ── drift tripwires added during orchestrator review ─────────────────────────


def test_the_hook_uses_the_LOGS_terminal_set_not_a_copy() -> None:
    """A terminal state the hook does not recognise reads as "still active".

    That is the worst available failure for this hook: it would block every session in the
    workspace forever, and the user's only recourse would be to remove the plugin. So the set is
    imported from the event log rather than restated here.
    """
    from bounded_loops.graph.adapters.persistence.event_log import _TERMINAL

    assert graph_run_stop._TERMINAL_RUN_STATES is _TERMINAL


def test_every_event_the_hook_maps_is_one_the_LOG_actually_applies() -> None:
    """An invented event name is an entry that can never fire — dead code posing as coverage.

    An earlier draft carried `run.halted` and `run.expired`, inferred from the terminal-state set.
    Neither exists: nothing in the engine emits them and `_apply` has no branch for them.
    """
    import inspect

    from bounded_loops.graph.adapters.persistence import event_log

    applied = inspect.getsource(event_log)
    for event_type in graph_run_stop._STATE_SETTING_EVENTS:
        assert f'"{event_type}"' in applied, (
            f"{event_type!r} is mapped by the hook but the event log never applies it"
        )


def test_the_hook_resolves_the_workspace_through_the_ONE_resolver() -> None:
    """A hand-rolled walk-up looks equivalent to `discover()` and is not.

    `discover()` stops at the git repository root, so a checkout cannot block on runs belonging
    to a workspace above it, and it honours $BOUNDED_LOOPS_WORKSPACE. A second implementation
    would silently check the wrong directory — which, for a hook whose whole job is preventing a
    false "done", means checking nothing.
    """
    import inspect

    source = inspect.getsource(graph_run_stop._discover_project_root)
    assert "from bounded_loops.workspace import discover" in source
    assert "resolved.parents" not in source, "the hand-rolled walk-up is back"


def test_a_workspace_ABOVE_the_git_root_is_not_checked_by_the_hook(
    tmp_path, monkeypatch,
) -> None:
    """The ceiling, end to end through the hook rather than through the resolver.

    An outer directory holds a workspace with an ACTIVE run; the session's cwd is a git checkout
    inside it. The hook must allow the stop: those runs are not this project's.
    """
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)
    outer_runs = tmp_path / ".bounded-loops" / "runs" / "r1"
    outer_runs.mkdir(parents=True)
    (outer_runs / "controller-events.jsonl").write_text(
        json.dumps({"type": "run.started", "payload": {"state": "RUNNING"}}) + "\n",
        encoding="utf-8",
    )
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".git").mkdir()

    assert graph_run_stop._discover_project_root(str(repo)) is None


def test_the_project_can_downgrade_the_BLOCK_to_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing this must never take away someone's ability to end a session.

    Same active run as `test_main_active_run_blocks_exit_2_with_stderr`, which exits 2. The only
    difference is four lines the user put in THEIR project's config, and the exit code changes to
    0 while still reporting what is active.
    """
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-active",
        "run.created", "run.started",
    )
    (ws_root / ".bounded-loops" / "config.toml").write_text(
        "[hooks]\nstop_on_active_run = false\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})(),
    )

    code = main(["graph_run_stop.py", "claude-code"])

    assert code == 0, "the switch did not disable the block"
    assert "run-active" in capsys.readouterr().err, "it went quiet instead of warning"


def test_a_MALFORMED_config_does_not_silently_disable_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open on a broken config would let a typo turn the guard off without telling anyone."""
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-active",
        "run.created", "run.started",
    )
    (ws_root / ".bounded-loops" / "config.toml").write_text("[hooks\nbroken =", encoding="utf-8")
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})(),
    )

    assert main(["graph_run_stop.py", "claude-code"]) == 2


def test_the_switch_must_be_EXPLICITLY_false_not_merely_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stop_on_active_run = true` and an empty [hooks] table both keep the default."""
    ws_root = _make_workspace(tmp_path)
    _write_events(
        ws_root / ".bounded-loops" / "runs" / "run-active",
        "run.created", "run.started",
    )
    config = ws_root / ".bounded-loops" / "config.toml"
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})(),
    )

    for body in ("[hooks]\nstop_on_active_run = true\n", "[hooks]\n", ""):
        config.write_text(body, encoding="utf-8")
        assert main(["graph_run_stop.py", "claude-code"]) == 2, f"config {body!r} disabled the block"


# ── the test that would have caught the inert control ────────────────────────


def test_the_hook_blocks_a_run_the_REAL_ENGINE_produced(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the engine, then point the hook at what it wrote. No fixture in between.

    Every other test in this file builds `controller-events.jsonl` by hand, and for a while the
    helper used the key `type` while the engine writes `event_type`. The hook read `type`, found
    nothing, treated every run as unknown, and allowed every stop — inert in production with 35
    green tests. A hand-written fixture cannot catch that class of defect by construction, because
    the fixture and the code under test can agree with each other and both be wrong.

    So this test asserts against the real artifact. It is slower than the others and that is the
    price of the only check that measures the thing.
    """
    import subprocess

    project = tmp_path / "project"
    project.mkdir()
    completed = subprocess.run(
        ["uv", "run", "bl", "graph", "run", "--execute"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "BOUNDED_LOOPS_WORKSPACE": str(project),
            "TMPDIR": "/tmp",
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    runs_dir = project / ".bounded-loops" / "runs"
    if completed.returncode != 0 or not runs_dir.is_dir():
        pytest.skip(f"the engine could not run here: {completed.stderr[-300:]}")

    run_dir = next(entry for entry in runs_dir.iterdir() if entry.is_dir())
    log = run_dir / "controller-events.jsonl"
    assert log.is_file(), "the engine wrote no receipt log"

    # The demo run SUCCEEDS, so the hook must ALLOW — and it must reach that answer by reading a
    # real terminal state, not by failing to parse anything.
    assert _read_run_state(run_dir) == "SUCCEEDED", (
        "the hook cannot read the state out of a log the engine actually wrote"
    )
    passed, _reason = _check_workspace(project)
    assert passed is True

    # Now truncate the log to before the terminal event: the same real log, mid-run.
    lines = log.read_text(encoding="utf-8").splitlines()
    terminal = next(
        index for index, line in enumerate(lines) if '"run.succeeded"' in line
    )
    log.write_text("\n".join(lines[:terminal]) + "\n", encoding="utf-8")

    assert _read_run_state(run_dir) == "RUNNING"
    passed, reason = _check_workspace(project)
    assert passed is False, "an unfinished REAL run did not block the stop"
    assert run_dir.name in reason


def test_a_SYMLINKED_receipt_log_is_not_followed(tmp_path: Path) -> None:
    """A hook that runs on every Stop event must not become a reader of arbitrary files."""
    ws_root = _make_workspace(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        json.dumps({**_BASE_EVENT, "event_type": "run.started", "payload": {"state": "RUNNING"}}),
        encoding="utf-8",
    )
    run_dir = ws_root / ".bounded-loops" / "runs" / "sneaky"
    run_dir.mkdir(parents=True)
    (run_dir / "controller-events.jsonl").symlink_to(outside)

    assert _read_run_state(run_dir) is None
