"""
Acceptance tests for CommandGate.

Covers the FAIL-vs-ERROR distinction that is the entire point of this
gate: a gate that RUNS and returns a code in expected_fail_codes
is Verdict(passed=False) — never an exception. Anything else (timeout,
missing binary, unexpected exit code) is a GateError.
"""

from __future__ import annotations

import pytest

from bounded_loops.adapters.gates.command import CommandGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Rung, Verdict


def _ctx(workspace, env=None) -> LoopContext:
    return LoopContext(
        workspace=workspace,
        lap=1,
        rung=Rung.L1,
        trace_id="trace-command-1",
        env=env or {},
    )


def test_command_exit0_returns_verdict_passed_true(tmp_path):
    gate = CommandGate(cmd="true")
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is True
    assert result.evidence["code"] == 0
    assert result.evidence["cmd"] == "true"


def test_command_exit1_expected_returns_verdict_passed_false(tmp_path):
    gate = CommandGate(cmd="false", expected_fail_codes=frozenset({1}))
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is False
    assert result.evidence["code"] == 1


def test_command_missing_binary_raises_gate_error(tmp_path):
    gate = CommandGate(cmd="this_binary_absolutely_does_not_exist_bounded_loops_test_xyz")

    with pytest.raises(GateError) as exc_info:
        gate.check(_ctx(tmp_path))

    message = str(exc_info.value)
    # shell=False: a missing binary now raises a real
    # OSError → GateError at launch, not a shell-level exit 127.
    assert "OS error launching" in message or "127" in message


def test_command_evidence_tail_truncated_to_2000_chars(tmp_path):
    # Produce 5000 chars of stdout, then exit 1 — a SINGLE command (no shell
    # `; exit 1` chaining, which shell=False no longer interprets).
    cmd = "python3 -c \"import sys; sys.stdout.write('a' * 5000); sys.exit(1)\""
    gate = CommandGate(cmd=cmd, expected_fail_codes=frozenset({1}))

    result = gate.check(_ctx(tmp_path))

    assert result.passed is False
    tail = result.evidence["tail"]
    assert len(tail) <= 2000
    # Verify it is the TAIL, not the HEAD, of the 5000-char output.
    assert tail[-1] == "a"


def test_command_exit0_short_output_tail_equals_full_output(tmp_path):
    gate = CommandGate(cmd="printf 'ok\\n'")
    result = gate.check(_ctx(tmp_path))

    assert result.evidence["tail"] == "ok\n"
    assert len(result.evidence["tail"]) == 3


def test_command_timeout_raises_gate_error(tmp_path):
    gate = CommandGate(cmd="sleep 999", timeout_s=1)

    with pytest.raises(GateError, match="timed out"):
        gate.check(_ctx(tmp_path))


def test_command_verdict_passed_false_is_not_exception(tmp_path):
    gate = CommandGate(cmd="false", expected_fail_codes=frozenset({1}))
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is False


def test_command_unexpected_exit_code_raises_gate_error(tmp_path):
    gate = CommandGate(cmd="bash -c 'exit 42'", expected_fail_codes=frozenset({1}))

    with pytest.raises(GateError, match="42"):
        gate.check(_ctx(tmp_path))


# --- constructor validation --------------------------------------------------

def test_command_empty_cmd_raises_value_error():
    with pytest.raises(ValueError, match="non-empty"):
        CommandGate(cmd="")


def test_command_whitespace_only_cmd_raises_value_error():
    with pytest.raises(ValueError, match="non-empty"):
        CommandGate(cmd="   ")


# --- environment allowlist (security fix) --------------------

def test_command_env_allowlist_excludes_arbitrary_parent_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TEST_SECRET", "super-secret-value")
    gate = CommandGate(cmd="echo \"secret=$BOUNDED_LOOPS_TEST_SECRET\"")
    result = gate.check(_ctx(tmp_path))

    assert "super-secret-value" not in result.evidence["tail"]


def test_command_ctx_env_is_passed_through(tmp_path):
    # shell=False does no $VAR expansion, so read the env var via python
    # instead of `echo $VAR` — this proves ctx.env reaches the subprocess env.
    gate = CommandGate(
        cmd="python3 -c \"import os; print('value=' + os.environ.get('MY_LOOP_VAR', 'ABSENT'))\""
    )
    result = gate.check(_ctx(tmp_path, env={"MY_LOOP_VAR": "hello-from-ctx"}))

    assert "hello-from-ctx" in result.evidence["tail"]


def test_command_path_is_always_present_in_subprocess_env(tmp_path):
    # PATH must survive the allowlist or "true"/"false"/etc. wouldn't resolve.
    gate = CommandGate(cmd="which true")
    result = gate.check(_ctx(tmp_path))
    assert result.passed is True
