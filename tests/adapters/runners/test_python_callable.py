"""Acceptance tests for PythonCallableRunner.

All tests exercise the REAL multiprocessing subprocess path (no mocks of
the subprocess machinery) — the only way to actually prove the isolation,
timeout, env-scrub, and large-payload behaviors are real, not asserted.
"""
import time
from pathlib import Path

import pytest

from bounded_loops.adapters.runners.python_callable import PythonCallableRunner
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _ctx(workspace: Path) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1,
                        trace_id="trace-pcr-1", env={})


def _spec() -> Spec:
    return Spec(name="t", goal="test", steps=("step",), stop_condition="x")


def test_constructor_does_not_import_eagerly():
    """Fix: constructing with a bogus module must NOT raise — the
    import only happens inside the isolated child at run_once() time."""
    runner = PythonCallableRunner(module_path="this.module.does.not.exist")
    assert runner.module_path == "this.module.does.not.exist"  # no exception


def test_run_once_raises_runner_error_on_missing_module(tmp_path):
    runner = PythonCallableRunner(module_path="this.module.does.not.exist")
    with pytest.raises(RunnerError, match="could not import"):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_raises_runner_error_on_missing_function(tmp_path):
    runner = PythonCallableRunner(module_path="tests.fixtures.good_glue",
                                   function_name="not_a_real_function")
    with pytest.raises(RunnerError, match="no function named"):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_happy_path(tmp_path):
    runner = PythonCallableRunner(module_path="tests.fixtures.good_glue")
    result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.changed is True
    assert result.agent_claimed_done is True


def test_run_once_hanging_callable_raises_runner_error_within_timeout(tmp_path):
    """The core D10 proof: a callable that never returns must not hang the
    caller past timeout_s, and must not hang the test suite either."""
    runner = PythonCallableRunner(module_path="tests.fixtures.hanging_glue",
                                   timeout_s=1)
    start = time.monotonic()
    with pytest.raises(RunnerError, match="timed out"):
        runner.run_once(_spec(), _ctx(tmp_path))
    elapsed = time.monotonic() - start
    assert elapsed < 5   # bounded by timeout_s=1 (+ small teardown slack), never open-ended


def test_run_once_raising_callable_is_caught_not_crashed(tmp_path):
    runner = PythonCallableRunner(module_path="tests.fixtures.raising_glue")
    with pytest.raises(RunnerError, match="raised"):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_passes_workspace_path_to_callable(tmp_path):
    runner = PythonCallableRunner(module_path="tests.fixtures.workspace_echo_glue")
    result = runner.run_once(_spec(), _ctx(tmp_path))
    assert str(tmp_path) in result.log


def test_run_once_missing_dict_keys_use_run_result_defaults(tmp_path):
    runner = PythonCallableRunner(module_path="tests.fixtures.minimal_glue")
    result = runner.run_once(_spec(), _ctx(tmp_path))
    assert result.tokens == 0
    assert result.log == ""


def test_run_once_non_dict_return_is_clean_runner_error(tmp_path):
    """Fix: a glue function returning a non-dict must produce a
    CLEAR error, not an opaque TypeError from dict(result)."""
    runner = PythonCallableRunner(module_path="tests.fixtures.bad_return_glue")
    with pytest.raises(RunnerError, match="must return a dict"):
        runner.run_once(_spec(), _ctx(tmp_path))


def test_run_once_large_payload_does_not_deadlock(tmp_path):
    """Fix: proves the queue.get(timeout=)-before-join() ordering —
    a large log string must not block the child's feeder thread on a full
    OS pipe while the parent waits on join() first (the original
    empty()-after-join() pattern could deadlock/false-timeout here)."""
    runner = PythonCallableRunner(module_path="tests.fixtures.large_payload_glue",
                                   timeout_s=10)
    start = time.monotonic()
    result = runner.run_once(_spec(), _ctx(tmp_path))
    assert time.monotonic() - start < 5
    assert len(result.log) > 1_000_000


def test_run_once_child_cannot_see_unallowlisted_env_var(tmp_path, monkeypatch):
    """Fix: the core proof the fork/spawn secret-leak is closed."""
    monkeypatch.setenv("BOUNDED_LOOPS_TEST_SECRET", "top-secret-value")
    runner = PythonCallableRunner(module_path="tests.fixtures.env_echo_glue")
    result = runner.run_once(_spec(), _ctx(tmp_path))
    assert "top-secret-value" not in result.log
    assert "ABSENT" in result.log
