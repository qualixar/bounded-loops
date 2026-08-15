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
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurnResult, TurnState
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


def _fake_turn(returncode=0, stdout="", stderr="", state=TurnState.COMPLETED):
    turn = MagicMock()
    turn.wait.return_value = ProcessTurnResult(
        state=state, returncode=returncode, stdout=stdout, stderr=stderr,
        output_truncated=False,
    )
    return turn


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
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout=payload)):
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
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout=payload)):
        result = ClaudeCodeRunner().run_once(_spec(), _ctx(tmp_path))
    assert result.tokens == 1200 + 340 + 10 + 50


def test_run_once_usage_missing_or_malformed_stays_zero_not_crash(tmp_path):
    """A drifted/absent usage schema degrades to 0, never crashes the runner."""
    payload = '{"total_cost_usd": 0.05, "usage": {"input_tokens": "oops", "weird": true}}'
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout=payload)):
        result = ClaudeCodeRunner().run_once(_spec(), _ctx(tmp_path))
    assert result.tokens == 0


def test_run_once_degrades_gracefully_on_non_json_stdout(tmp_path):
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout="not json at all")):
        runner = ClaudeCodeRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.log == "not json at all"
    assert result.agent_claimed_done is False


def test_run_once_writes_agent_output(tmp_path):
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout="hello")):
        runner = ClaudeCodeRunner()
        runner.run_once(_spec(), _ctx(tmp_path))
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "hello"


def test_run_once_agent_claimed_done_always_false(tmp_path):
    """HLD invariant I1 — this runner never trusts its own CLI's claim."""
    payload = '{"total_cost_usd": 1.0}'
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout=payload)):
        runner = ClaudeCodeRunner()
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False


def test_run_once_timeout_raises_runner_error(tmp_path):
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(state=TurnState.TIMED_OUT)):
        runner = ClaudeCodeRunner(timeout_s=300)
        with pytest.raises(RunnerError, match="timed out"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_missing_binary_raises_runner_error(tmp_path):
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", side_effect=OSError("no such file")):
        runner = ClaudeCodeRunner()
        with pytest.raises(RunnerError, match="could not launch"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_builds_argv_with_output_format_json(tmp_path):
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout="{}")) as mock_start:
        runner = ClaudeCodeRunner(agent_cmd="claude")
        runner.run_once(_spec(), _ctx(tmp_path))
    argv = mock_start.call_args.args[0]
    assert argv == ["claude", "-p", "--output-format", "json"]
    assert mock_start.call_args.kwargs["input_text"].startswith("# Goal")


def test_run_once_never_passes_bare(tmp_path):
    """`--bare` requires a credential this runner structurally cannot deliver.

    INVERTED, not deleted, on 2026-08-16. This assertion used to REQUIRE
    ``--bare`` in the argv, and so pinned the defect in place: ``ENV_ALLOWLIST``
    is ``{PATH, HOME, LANG, LC_ALL, TMPDIR, SHELL}``, so ``ANTHROPIC_API_KEY``
    never reaches the subprocess, and no shipped loop declares an
    ``env_passthrough`` that would carry it. Every ``--runner claude-code``
    attempt therefore came back ``is_error: true`` with zero tokens and zero
    cost — for every user, not only for those without an API key.

    Probed live the same day with the key unset: WITHOUT ``--bare`` the same
    prompt returns ``is_error: false`` and real usage. HOME is allowlisted, so
    the subscription login is reachable and no key is needed.

    The guard still points at whichever direction is currently the lie.
    """
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout="{}")) as mock_start:
        ClaudeCodeRunner(agent_cmd="claude").run_once(_spec(), _ctx(tmp_path))
    assert "--bare" not in mock_start.call_args.args[0]


def test_anthropic_api_key_is_not_forwarded_so_bare_could_never_authenticate(tmp_path, monkeypatch):
    """The premise of the inverted guard above, asserted rather than asserted-about.

    If this ever fails, ``--bare`` became deliverable and the reasoning in
    ``test_run_once_never_passes_bare`` needs revisiting rather than trusting.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout="{}")) as mock_start:
        ClaudeCodeRunner().run_once(_spec(), _ctx(tmp_path))
    assert "ANTHROPIC_API_KEY" not in mock_start.call_args.kwargs["env"]


def test_run_once_extra_env_merged_into_subprocess_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "should-not-leak")
    with patch("bounded_loops.adapters.runners.claude_code.ProcessTurn.start", return_value=_fake_turn(stdout="{}")) as mock_start:
        runner = ClaudeCodeRunner(extra_env={"MY_TOKEN": "abc123"})
        runner.run_once(_spec(), _ctx(tmp_path))
    env = mock_start.call_args.kwargs["env"]
    assert env.get("MY_TOKEN") == "abc123"
    assert "MY_SECRET" not in env
