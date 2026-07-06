"""
Acceptance tests for ShellRunner.

Covers PROMPT.md piping, Spec-fallback prompt assembly, non-zero exit
handling (NOT a RunnerError), timeout/missing-binary error wrapping, the
DONE_SIGNAL heuristic, and the environment-allowlist
security fix: a secret-like variable set in the test process' own
environment must NOT reach the subprocess unless it is in the allowlist
or explicitly passed via ctx.env.
"""

import pytest

from bounded_loops.adapters.runners.shell import ShellRunner, _build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec(goal="Test goal", steps=("step A", "step B")) -> Spec:
    return Spec(
        name="demo-loop",
        goal=goal,
        steps=steps,
        stop_condition="pytest exits 0",
    )


def _ctx(workspace, env=None) -> LoopContext:
    return LoopContext(
        workspace=workspace,
        lap=1,
        rung=Rung.L1,
        trace_id="trace-shell-1",
        env=env or {},
    )


def test_shell_pipes_prompt_md_to_agent(tmp_path):
    (tmp_path / "PROMPT.md").write_text("# Goal\nsolve it", encoding="utf-8")
    runner = ShellRunner(agent_cmd="cat")
    result = runner.run_once(_spec(), _ctx(tmp_path))

    assert "solve it" in result.log
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "# Goal\nsolve it"


def test_shell_falls_back_to_spec_when_no_prompt_md(tmp_path):
    runner = ShellRunner(agent_cmd="cat")
    result = runner.run_once(
        _spec(goal="Test goal", steps=("step A", "step B")), _ctx(tmp_path)
    )

    assert "Test goal" in result.log
    assert "step A" in result.log
    assert "step B" in result.log


def test_shell_agent_nonzero_exit_is_not_exception(tmp_path):
    runner = ShellRunner(agent_cmd="bash -c 'echo done; exit 1'")
    result = runner.run_once(_spec(), _ctx(tmp_path))
    assert "done" in result.log


def test_shell_timeout_raises_runner_error(tmp_path):
    runner = ShellRunner(agent_cmd="sleep 999", timeout_s=1)
    with pytest.raises(RunnerError, match="timed out"):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_shell_missing_binary_raises_runner_error(tmp_path):
    runner = ShellRunner(agent_cmd="this_binary_does_not_exist_xyz_123")
    with pytest.raises(RunnerError):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_shell_malformed_quoting_raises_runner_error(tmp_path):
    # New edge case introduced by the coordinator-directed shlex.split()/
    # shell=False fix: an unterminated quote in agent_cmd
    # is a ValueError from shlex.split(), wrapped as RunnerError rather
    # than propagating a raw ValueError to the caller.
    runner = ShellRunner(agent_cmd="echo 'unterminated")
    with pytest.raises(RunnerError):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_shell_done_signal_in_env(tmp_path):
    runner = ShellRunner(agent_cmd="echo TASK_COMPLETE")
    result = runner.run_once(_spec(), _ctx(tmp_path, env={"DONE_SIGNAL": "TASK_COMPLETE"}))
    assert result.agent_claimed_done is True


def test_shell_no_done_signal(tmp_path):
    runner = ShellRunner(agent_cmd="echo hello")
    result = runner.run_once(_spec(), _ctx(tmp_path, env={}))
    assert result.agent_claimed_done is False


# --- security fix: environment allowlist ---------------------

def test_shell_secret_env_var_not_leaked_to_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SUPER_SECRET_API_KEY", "sk-should-not-leak-12345")
    runner = ShellRunner(agent_cmd="env")
    result = runner.run_once(_spec(), _ctx(tmp_path))

    assert "MY_SUPER_SECRET_API_KEY" not in result.log
    assert "sk-should-not-leak-12345" not in result.log


def test_shell_ctx_env_is_merged_over_allowlist(tmp_path):
    runner = ShellRunner(agent_cmd="env")
    result = runner.run_once(_spec(), _ctx(tmp_path, env={"MY_LOOP_VAR": "loop-value"}))
    assert "MY_LOOP_VAR=loop-value" in result.log


def test_build_subprocess_env_only_allowlisted_keys_plus_ctx_env(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_SUPER_SECRET_API_KEY", "sk-should-not-leak")
    env = _build_subprocess_env({"EXTRA": "value"})

    assert "MY_SUPER_SECRET_API_KEY" not in env
    assert env.get("PATH") == "/usr/bin"
    assert env.get("EXTRA") == "value"
    assert set(env.keys()) <= {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "EXTRA"}
