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
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurnResult, TurnState
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec() -> Spec:
    return Spec(name="demo-loop", goal="Fix the bug", steps=("step A",),
                stop_condition="pytest exits 0")


def _ctx(workspace) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="trace-ag-1", env={})


def _fake_turn(returncode=0, stdout="", stderr="", state=TurnState.COMPLETED):
    turn = MagicMock()
    turn.wait.return_value = ProcessTurnResult(
        state=state, returncode=returncode, stdout=stdout, stderr=stderr,
        output_truncated=False,
    )
    return turn


def test_build_prompt_reads_prompt_md(tmp_path):
    (tmp_path / "PROMPT.md").write_text("goal text", encoding="utf-8")
    assert _build_prompt(_spec(), _ctx(tmp_path)) == "goal text"


def test_antigravity_runner_default_approve_policy_is_not_all():
    """Fix proof — the unsafe hardcoded 'all' default is gone.

    Constructed bare on purpose: this asserts the DEFAULT, so it must not pass a
    policy. The default is safe rather than convenient, and since agy cannot
    deliver a graded posture, a caller that genuinely wants an acting agent has
    to say so explicitly and thereby accept the posture.
    """
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
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=1, stdout="I couldn't finish this turn")):
        runner = AntigravityRunner(approve_policy="all")
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False
    assert "couldn't finish" in result.log


def test_antigravity_runner_exit_zero_empty_stdout_raises_false_success(tmp_path):
    """The DOCUMENTED agy non-TTY false-success bug — exit 0, empty stdout."""
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout="")):
        runner = AntigravityRunner(approve_policy="all")
        with pytest.raises(RunnerError, match="false-success"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_antigravity_runner_exit_zero_whitespace_only_stdout_raises(tmp_path):
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout="   \n  ")):
        runner = AntigravityRunner(approve_policy="all")
        with pytest.raises(RunnerError, match="false-success"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_antigravity_runner_exit_zero_with_real_stdout_does_not_raise(tmp_path):
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout="all good, task complete")):
        runner = AntigravityRunner(approve_policy="all")
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert "task complete" in result.log


def test_run_once_writes_agent_output(tmp_path):
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout="hello output")):
        runner = AntigravityRunner(approve_policy="all")
        runner.run_once(_spec(), _ctx(tmp_path))
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "hello output"


def test_run_once_timeout_raises_runner_error(tmp_path):
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(state=TurnState.TIMED_OUT)):
        runner = AntigravityRunner(approve_policy="all")
        with pytest.raises(RunnerError, match="timed out"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_missing_binary_raises_runner_error(tmp_path):
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", side_effect=OSError("no such file")):
        runner = AntigravityRunner(approve_policy="all")
        with pytest.raises(RunnerError, match="could not launch"):
            runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_builds_argv_against_the_real_agy_interface(tmp_path):
    """REWRITTEN 2026-08-16 — the argv this used to pin was rejected by the binary.

    It asserted ``["agy", "-p", "--headless", "--approve", "plan"]``. Probed live,
    agy answers that with ``flags provided but not defined: -headless -approve``,
    its usage text, and no work done. So this test passed for as long as it
    existed while the runner it guarded could not run, which is the failure mode
    the paper this work supports is about: a green assertion about a string,
    standing in for a claim about a process.

    Now pinned against what the binary accepts: prompt POSITIONAL (agy does not
    read stdin), auto-approval (its only permission control), and --add-dir
    (without it agy writes to its own scratch directory and the gate sees an
    unchanged workspace).
    """
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout="ok")) as mock_start:
        runner = AntigravityRunner(agent_cmd="agy", approve_policy="all")
        ctx = _ctx(tmp_path)
        runner.run_once(_spec(), ctx)
    argv = mock_start.call_args.args[0]
    assert argv[0] == "agy"
    assert argv[1] == "-p"
    assert argv[2].startswith("# Goal"), "the prompt must be positional, not on stdin"
    assert "--dangerously-skip-permissions" in argv
    assert argv[-2:] == ["--add-dir", str(ctx.workspace)]
    assert "--headless" not in argv and "--approve" not in argv
    assert mock_start.call_args.kwargs["input_text"] == ""


def test_a_policy_agy_cannot_deliver_is_refused_rather_than_silently_downgraded(tmp_path):
    """agy has no graded approval; asking for one must fail loudly.

    Without auto-approval agy's tools are denied in headless mode and it exits
    successfully having changed nothing. A runner that proceeded anyway would
    hand an L1/L2 loop an agent that appears to run and never acts, and the
    ledger could not tell that apart from an agent that tried and failed.
    """
    for policy in ("none", "plan"):
        with pytest.raises(RunnerError, match="no graded approval policy"):
            AntigravityRunner(approve_policy=policy).run_once(_spec(), _ctx(tmp_path))


def test_run_once_agent_claimed_done_always_false(tmp_path):
    with patch("bounded_loops.adapters.runners.antigravity.ProcessTurn.start", return_value=_fake_turn(returncode=0, stdout="task complete!")):
        runner = AntigravityRunner(approve_policy="all")
        result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.agent_claimed_done is False
