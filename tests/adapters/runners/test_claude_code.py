"""
Acceptance tests for ClaudeCodeRunner.

Mocks subprocess.run (no real `claude` CLI is assumed to be installed).
Covers: prompt building
via the module-level _build_prompt, `--output-format json` cost parsing,
graceful degradation on non-JSON stdout, agent_output.txt invariant,
agent_claimed_done ALWAYS False (HLD invariant I1), and timeout/launch
error wrapping.
"""
from unittest.mock import MagicMock, patch

import pytest

from bounded_loops.adapters.runners.claude_code import ClaudeCodeRunner, _build_prompt
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec() -> Spec:
    return Spec(
        name="demo-loop",
        goal="Fix the bug",
        steps=("step A",),
        stop_condition="pytest exits 0",
    )


def _ctx(workspace, env=None) -> LoopContext:
    return LoopContext(
        workspace=workspace, lap=1, rung=Rung.L1, trace_id="trace-cc-1", env=env or {},
    )


def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_build_prompt_reads_prompt_md(tmp_path):
    (tmp_path / "PROMPT.md").write_text("# Goal\nsolve it", encoding="utf-8")
    prompt = _build_prompt(_spec(), _ctx(tmp_path))
    assert prompt == "# Goal\nsolve it"


def test_build_prompt_falls_back_to_spec(tmp_path):
    prompt = _build_prompt(_spec(), _ctx(tmp_path))
    assert "Fix the bug" in prompt
    assert "step A" in prompt


def test_run_once_parses_total_cost_usd(tmp_path):
    payload = '{"total_cost_usd": 0.0123, "session_id": "abc"}'
    with patch("subprocess.run", return_value=_fake_proc(stdout=payload)):
        runner = ClaudeCodeRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert "0.0123" in result.log
    assert result.agent_claimed_done is False
    assert result.tokens == 0   # no usage block present -> honest 0


def test_run_once_sums_real_usage_tokens_for_bound_7(tmp_path):
    """Bound #7 made real: the JSON `usage` block's token counts are summed
    into RunResult.tokens, so BudgetMeter can enforce bounds.max_tokens."""
    payload = (
        '{"total_cost_usd": 0.05, "session_id": "s", "usage": '
        '{"input_tokens": 1200, "output_tokens": 340, '
        '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 50}}'
    )
    with patch("subprocess.run", return_value=_fake_proc(stdout=payload)):
        result = ClaudeCodeRunner().run_once(_spec(), _ctx(tmp_path))
    assert result.tokens == 1200 + 340 + 10 + 50


def test_run_once_usage_missing_or_malformed_stays_zero_not_crash(tmp_path):
    """A drifted/absent usage schema degrades to 0, never crashes the runner."""
    payload = '{"total_cost_usd": 0.05, "usage": {"input_tokens": "oops", "weird": true}}'
    with patch("subprocess.run", return_value=_fake_proc(stdout=payload)):
        result = ClaudeCodeRunner().run_once(_spec(), _ctx(tmp_path))
    assert result.tokens == 0


def test_run_once_degrades_gracefully_on_non_json_stdout(tmp_path):
    with patch("subprocess.run", return_value=_fake_proc(stdout="not json at all")):
        runner = ClaudeCodeRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.log == "not json at all"
    assert result.agent_claimed_done is False


def test_run_once_writes_agent_output(tmp_path):
    with patch("subprocess.run", return_value=_fake_proc(stdout="hello")):
        runner = ClaudeCodeRunner()
        runner.run_once(_spec(), _ctx(tmp_path))
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "hello"


def test_run_once_agent_claimed_done_always_false(tmp_path):
    """HLD invariant I1 — this runner never trusts its own CLI's claim."""
    payload = '{"total_cost_usd": 1.0}'
    with patch("subprocess.run", return_value=_fake_proc(returncode=0, stdout=payload)):
        runner = ClaudeCodeRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False


def test_run_once_timeout_raises_runner_error(tmp_path):
    import subprocess as sp
    with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="claude", timeout=300)):
        runner = ClaudeCodeRunner(timeout_s=300)
        with pytest.raises(RunnerError, match="timed out"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_missing_binary_raises_runner_error(tmp_path):
    with patch("subprocess.run", side_effect=OSError("no such file")):
        runner = ClaudeCodeRunner()
        with pytest.raises(RunnerError, match="could not launch"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_builds_argv_with_output_format_json(tmp_path):
    proc = _fake_proc(stdout="{}")
    with patch("subprocess.run", return_value=proc) as mock_run:
        runner = ClaudeCodeRunner(agent_cmd="claude")
        runner.run_once(_spec(), _ctx(tmp_path))
    # _workspace_changed also calls subprocess.run (git diff) — the FIRST
    # call is always the agent invocation itself.
    args, kwargs = mock_run.call_args_list[0]
    argv = args[0]
    assert argv == ["claude", "-p", "--output-format", "json", "--bare"]
    assert kwargs["shell"] is False


def test_run_once_extra_env_merged_into_subprocess_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "should-not-leak")
    proc = _fake_proc(stdout="{}")
    with patch("subprocess.run", return_value=proc) as mock_run:
        runner = ClaudeCodeRunner(extra_env={"MY_TOKEN": "abc123"})
        runner.run_once(_spec(), _ctx(tmp_path))
    _, kwargs = mock_run.call_args_list[0]
    env = kwargs["env"]
    assert env.get("MY_TOKEN") == "abc123"
    assert "MY_SECRET" not in env
