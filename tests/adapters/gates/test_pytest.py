"""
Acceptance tests for PytestGate.

PytestGate is a thin, self-contained subclass of CommandGate hardcoded to
"pytest -q" with its own independent EXPECTED_FAIL_CODES = frozenset({1}).
It imports ONLY CommandGate from command.py — no shared constant.
"""

from __future__ import annotations

import pytest

from bounded_loops.adapters.gates.pytest import PytestGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Rung, Verdict


def _ctx(workspace) -> LoopContext:
    return LoopContext(
        workspace=workspace,
        lap=1,
        rung=Rung.L1,
        trace_id="trace-pytest-1",
        env={},
    )


def test_pytest_gate_default_cmd_is_pytest_q():
    gate = PytestGate()
    assert gate.cmd == "pytest -q"


def test_pytest_gate_extra_args_appended():
    gate = PytestGate(extra_args="tests/unit/ -x")
    assert gate.cmd == "pytest -q tests/unit/ -x"


def test_pytest_gate_expected_fail_codes_is_one():
    gate = PytestGate()
    assert gate.expected_fail_codes == frozenset({1})


def test_pytest_gate_exit0_passes(tmp_path):
    (tmp_path / "test_ok.py").write_text(
        "def test_passes():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    gate = PytestGate()
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is True


def test_pytest_gate_exit1_fails_not_exception(tmp_path):
    (tmp_path / "test_fail.py").write_text(
        "def test_fails():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    gate = PytestGate()
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is False


def test_pytest_gate_exit5_no_tests_raises_gate_error(tmp_path):
    # tmp_path is empty — no test files, pytest exits 5.
    gate = PytestGate()
    with pytest.raises(GateError):
        gate.check(_ctx(tmp_path))


def test_pytest_gate_module_imports_only_command_gate_class():
    import ast

    import bounded_loops.adapters.gates.pytest as pytest_gate_module

    tree = ast.parse(open(pytest_gate_module.__file__, encoding="utf-8").read())
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    command_module_imports = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "bounded_loops.adapters.gates.command"
        for alias in node.names
    }

    assert command_module_imports == {"CommandGate"}
    assert "EXPECTED_FAIL_CODES" not in imported_names
