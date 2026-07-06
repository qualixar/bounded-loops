"""
Acceptance tests for AntigravityRunner.

The security-load-bearing tests here are:
  1. approve_policy default is NOT "all".
  2. Invalid approve_policy raises RunnerError at construction, before ever
     reaching argv.
  3. The narrowed false-success check: RunnerError raised ONLY when
     returncode == 0 AND stdout is empty/whitespace-only. A plain non-zero
     exit WITH stdout must return a normal RunResult, not raise — the
     real safety regression this proves is fixed.
"""
from unittest.mock import MagicMock, patch

import pytest

from bounded_loops.adapters.runners.antigravity import AntigravityRunner, _build_prompt
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec() -> Spec:
    return Spec(name="demo-loop", goal="Fix the bug", steps=("step A",),
                stop_condition="pytest exits 0")


def _ctx(workspace) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="trace-ag-1", env={})


def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_build_prompt_reads_prompt_md(tmp_path):
    (tmp_path / "PROMPT.md").write_text("goal text", encoding="utf-8")
    assert _build_prompt(_spec(), _ctx(tmp_path)) == "goal text"


def test_antigravity_runner_default_approve_policy_is_not_all():
    """Fix proof — the unsafe hardcoded 'all' default is gone."""
    runner = AntigravityRunner()
    assert runner.approve_policy != "all"


def test_antigravity_runner_default_approve_policy_is_none():
    runner = AntigravityRunner()
    assert runner.approve_policy == "none"


def test_invalid_approve_policy_raises_runner_error_at_construction():
    with pytest.raises(RunnerError, match="invalid approve_policy"):
        AntigravityRunner(approve_policy="auto-approve-everything")


@pytest.mark.parametrize("policy", ["none", "plan", "all"])
def test_valid_approve_policies_accepted(policy):
    runner = AntigravityRunner(approve_policy=policy)
    assert runner.approve_policy == policy


def test_antigravity_runner_nonzero_exit_with_output_does_not_raise(tmp_path):
    """Fix proof — a normal failed-attempt exit must return a
    RunResult, not raise RunnerError (that's the gate's job to adjudicate)."""
    proc = _fake_proc(returncode=1, stdout="I couldn't finish this turn")
    with patch("subprocess.run", return_value=proc):
        runner = AntigravityRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False
    assert "couldn't finish" in result.log


def test_antigravity_runner_exit_zero_empty_stdout_raises_false_success(tmp_path):
    """The DOCUMENTED agy non-TTY false-success bug — exit 0, empty stdout."""
    proc = _fake_proc(returncode=0, stdout="")
    with patch("subprocess.run", return_value=proc):
        runner = AntigravityRunner()
        with pytest.raises(RunnerError, match="false-success"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_antigravity_runner_exit_zero_whitespace_only_stdout_raises(tmp_path):
    proc = _fake_proc(returncode=0, stdout="   \n  ")
    with patch("subprocess.run", return_value=proc):
        runner = AntigravityRunner()
        with pytest.raises(RunnerError, match="false-success"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_antigravity_runner_exit_zero_with_real_stdout_does_not_raise(tmp_path):
    proc = _fake_proc(returncode=0, stdout="all good, task complete")
    with patch("subprocess.run", return_value=proc):
        runner = AntigravityRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert "task complete" in result.log


def test_run_once_writes_agent_output(tmp_path):
    proc = _fake_proc(returncode=0, stdout="hello output")
    with patch("subprocess.run", return_value=proc):
        runner = AntigravityRunner()
        runner.run_once(_spec(), _ctx(tmp_path))
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "hello output"


def test_run_once_timeout_raises_runner_error(tmp_path):
    import subprocess as sp
    with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="agy", timeout=300)):
        runner = AntigravityRunner()
        with pytest.raises(RunnerError, match="timed out"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_missing_binary_raises_runner_error(tmp_path):
    with patch("subprocess.run", side_effect=OSError("no such file")):
        runner = AntigravityRunner()
        with pytest.raises(RunnerError, match="could not launch"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_builds_argv_with_approve_policy(tmp_path):
    proc = _fake_proc(returncode=0, stdout="ok")
    with patch("subprocess.run", return_value=proc) as mock_run:
        runner = AntigravityRunner(agent_cmd="agy", approve_policy="plan")
        runner.run_once(_spec(), _ctx(tmp_path))
    # _workspace_changed also calls subprocess.run (git diff) — the FIRST
    # call is always the agent invocation itself.
    args, kwargs = mock_run.call_args_list[0]
    argv = args[0]
    assert argv == ["agy", "-p", "--headless", "--approve", "plan"]
    assert kwargs["shell"] is False


def test_run_once_agent_claimed_done_always_false(tmp_path):
    proc = _fake_proc(returncode=0, stdout="task complete!")
    with patch("subprocess.run", return_value=proc):
        runner = AntigravityRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False
