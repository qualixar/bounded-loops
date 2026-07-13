"""
Acceptance tests for CodexRunner.

Mocks subprocess.run. Covers: JSONL turn.completed/turn.failed parsing,
sandbox_mode is a real constructor param (not hardcoded, derivation itself
is composition.py's job), agent_output.txt invariant,
timeout/launch error wrapping, and the exact argv shape.
"""
from unittest.mock import MagicMock, patch

import pytest

from bounded_loops.adapters.runners.codex import CodexRunner, _build_prompt
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec() -> Spec:
    return Spec(name="demo-loop", goal="Fix the bug", steps=("step A",),
                stop_condition="pytest exits 0")


def _ctx(workspace) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="trace-cx-1", env={})


def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_build_prompt_reads_prompt_md(tmp_path):
    (tmp_path / "PROMPT.md").write_text("goal text", encoding="utf-8")
    assert _build_prompt(_spec(), _ctx(tmp_path)) == "goal text"


def test_default_sandbox_mode_is_read_only():
    """Constructor default is conservative; composition.py overrides per
    Bounds/Rung — this test only proves the constructor itself is safe if
    ever instantiated without an explicit sandbox_mode."""
    runner = CodexRunner()
    assert runner.sandbox_mode == "read-only"


def test_run_once_agent_claimed_done_always_false_even_on_turn_completed(tmp_path):
    """agent_claimed_done is ALWAYS False for CodexRunner,
    matching ClaudeCodeRunner/AntigravityRunner. The engine never reads it for
    termination (only the gate does); deriving it from the CLI's own signal gave
    the field a divergent per-runner meaning."""
    jsonl = '{"type": "turn.started"}\n{"type": "turn.completed"}\n'
    with patch("subprocess.run", return_value=_fake_proc(stdout=jsonl)):
        runner = CodexRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False


def test_run_once_ignores_valid_json_non_dict_line_without_crashing(tmp_path):
    """Fix: a valid-JSON-but-not-an-object line (a bare array/number/
    string on its own line) must be SKIPPED, not crash the engine loop with
    AttributeError from event.get(...)."""
    jsonl = '[1, 2, 3]\n42\n"a string"\n{"type": "turn.completed"}\n'
    with patch("subprocess.run", return_value=_fake_proc(stdout=jsonl)):
        runner = CodexRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))  # must not raise
    assert result.agent_claimed_done is False


def test_run_once_turn_failed_is_surfaced_in_log(tmp_path):
    """turn.failed is recorded in the log (not the advisory agent_claimed_done
    field, which is always False now)."""
    jsonl = '{"type": "turn.started"}\n{"type": "turn.failed"}\n'
    with patch("subprocess.run", return_value=_fake_proc(stdout=jsonl)):
        runner = CodexRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False
    assert "turn.failed" in result.log


def test_run_once_ignores_malformed_jsonl_lines(tmp_path):
    jsonl = 'not json\n{"type": "turn.completed"}\n'
    with patch("subprocess.run", return_value=_fake_proc(stdout=jsonl)):
        runner = CodexRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))  # must not raise
    assert result.agent_claimed_done is False


def test_run_once_writes_agent_output(tmp_path):
    with patch("subprocess.run", return_value=_fake_proc(stdout="raw stream")):
        runner = CodexRunner()
        runner.run_once(_spec(), _ctx(tmp_path))
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "raw stream"


def test_run_once_timeout_raises_runner_error(tmp_path):
    import subprocess as sp
    with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="codex", timeout=300)):
        runner = CodexRunner()
        with pytest.raises(RunnerError, match="timed out"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_missing_binary_raises_runner_error(tmp_path):
    with patch("subprocess.run", side_effect=OSError("no such file")):
        runner = CodexRunner()
        with pytest.raises(RunnerError, match="could not launch"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_builds_argv_with_sandbox_mode(tmp_path):
    proc = _fake_proc(stdout="")
    with patch("subprocess.run", return_value=proc) as mock_run:
        runner = CodexRunner(agent_cmd="codex", sandbox_mode="workspace-write")
        runner.run_once(_spec(), _ctx(tmp_path))
    # _workspace_changed also calls subprocess.run (git diff) — the FIRST
    # call is always the agent invocation itself.
    args, kwargs = mock_run.call_args_list[0]
    argv = args[0]
    assert argv == ["codex", "exec", "--json", "--sandbox", "workspace-write", "-"]
    assert kwargs["shell"] is False


def test_run_once_parses_live_turn_completed_usage(tmp_path):
    jsonl = (
        '{"type":"turn.completed","usage":'
        '{"input_tokens":120,"cached_input_tokens":40,"output_tokens":30,'
        '"reasoning_output_tokens":12}}\n'
    )
    with patch("subprocess.run", return_value=_fake_proc(stdout=jsonl)):
        runner = CodexRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.tokens == 150


def test_run_once_turn_failed_raises_runner_error(tmp_path):
    jsonl = '{"type":"turn.failed","error":{"message":"model unavailable"}}\n'
    with patch("subprocess.run", return_value=_fake_proc(returncode=1, stdout=jsonl)):
        runner = CodexRunner()
        with pytest.raises(RunnerError, match="model unavailable"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_nonzero_exit_without_failure_event_raises_runner_error(tmp_path):
    with patch(
        "subprocess.run",
        return_value=_fake_proc(returncode=2, stdout="", stderr="bad option"),
    ):
        runner = CodexRunner()
        with pytest.raises(RunnerError, match="exit 2.*bad option"):
            runner.run_once(_spec(), _ctx(tmp_path))
