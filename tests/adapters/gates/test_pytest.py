"""
Acceptance tests for PytestGate.

PytestGate is a thin, self-contained subclass of CommandGate hardcoded to
"pytest -q" with its own independent EXPECTED_FAIL_CODES. It imports ONLY
CommandGate from command.py — no shared constant.

Exit 2 is the interesting one: pytest returns it both for "the suite could
not be collected because an import raised" (the worker's fault, and a
verdict it can act on) and for "someone interrupted the session" (not the
worker's fault, and unfixable by retrying). The last two tests pin both
halves, because admitting exit 2 wholesale would report a Ctrl-C as the
work having failed.
"""

from __future__ import annotations

import pytest
import shlex
import sys

from pathlib import Path

from bounded_loops.adapters.gates.command import CommandGate
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


def test_pytest_gate_default_cmd_uses_current_interpreter():
    gate = PytestGate()
    assert shlex.split(gate.cmd) == [sys.executable, "-m", "pytest", "-q"]


def test_pytest_gate_extra_args_appended():
    gate = PytestGate(extra_args="tests/unit/ -x")
    assert shlex.split(gate.cmd) == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit/",
        "-x",
    ]


def test_pytest_gate_owns_its_fail_codes_independently_of_CommandGate():
    """The property this has always been for: PytestGate declares its own set.

    It asserted the literal `frozenset({1})`, which made the VALUE the contract rather than the
    independence. When exit 2 was split — a failed collection is the worker's fault, an
    interruption is not — this failed for a change that did not touch the property it is named
    after. Both facts are now asserted for what they are.
    """
    gate = PytestGate()

    assert gate.expected_fail_codes == frozenset({1, 2})
    assert gate.expected_fail_codes != CommandGate("true").expected_fail_codes, (
        "PytestGate's fail codes have diverged from CommandGate's default and must be able to: "
        "exit 2 means 'collection failed' for pytest and nothing in particular for a bare command"
    )
    assert 2 in gate.expected_fail_codes, (
        "exit 2 is admitted so the base class returns a Verdict; PytestGate.check re-raises the "
        "interruption half. Removing it here silently restores the halt-on-broken-import defect"
    )


def test_pytest_gate_exit0_passes(tmp_path):
    (tmp_path / "test_ok.py").write_text(
        "def test_passes():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    gate = PytestGate()
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is True
    assert result.evidence["invocation"] == "current-python-module"


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


# ── exit 2: a failed collection is the worker's fault; an interruption is not ──


def test_a_collection_error_is_a_gate_FAIL_not_a_gate_error(tmp_path: Path) -> None:
    """The most common way a bounded loop's edit goes wrong must reach the worker.

    Emptying the module under test makes `from mod import thing` raise, pytest cannot assemble the
    suite, and it exits 2. That used to raise GateError and halt the run — the loop had a precise,
    actionable diagnosis and threw it away instead of handing it back.
    """
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "from mod import thing\n\n\ndef test_it():\n    assert thing()\n", encoding="utf-8"
    )

    verdict = PytestGate().check(_ctx(tmp_path))

    assert verdict.passed is False
    assert verdict.evidence.get("failure_kind") == "collection-error"
    assert "could not be collected" in verdict.detail


def test_an_exit_2_WITHOUT_a_collection_error_stays_a_gate_error() -> None:
    """The other half of exit 2, and the reason the code alone is not enough to classify it.

    pytest returns 2 for a Ctrl-C as well as for a failed import. Reporting an interruption as the
    work having failed would make the loop retry against a session nobody stopped by accident — so
    an exit 2 this cannot explain keeps the old, safe behaviour.

    Driven through a stubbed base class rather than by interrupting a real pytest, because the only
    way to produce a genuine SIGINT here would be to send one to this test runner.
    """
    from unittest import mock

    gate = PytestGate()
    interrupted = Verdict(
        passed=False,
        detail="gate failed (exit 2)",
        evidence={"code": 2, "tail": "!!!!! KeyboardInterrupt !!!!!\n"},
    )

    with mock.patch.object(CommandGate, "check", return_value=interrupted):
        with pytest.raises(GateError, match="without reporting a collection error"):
            gate.check(_ctx(Path(".")))
