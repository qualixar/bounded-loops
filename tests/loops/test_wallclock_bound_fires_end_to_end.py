"""The declared wallclock ceiling stops a real attempt, and the receipt says so correctly.

The unit tests next door pin the arithmetic. This pins the thing an operator actually cares about:
a loop that declares `max_wallclock_s: N` against a worker that will never finish stops at N, is
recorded as a bound firing rather than a malfunction, and names the bound in its reason.

WHY A SLEEPING WORKER AND NOT A MOCK
------------------------------------
The defect being closed was invisible to every mock in the suite, because the mocks asserted on the
timeout the controller *computed* and the gap was between what it computed and what the subprocess
was actually given. So this test runs a real subprocess that really outlives its budget, and reads
the terminal status and the ledger — the two artifacts a user sees.

The ceiling is deliberately small (2s) and the adapter timeout deliberately large (the shipped 300s
default), which is the exact shape of the observed failure: the loop's own limit was tighter than
the adapter's and was the one being ignored.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from bounded_loops.adapters.io.budget import BudgetMeter
from bounded_loops.adapters.io.file_ledger import FileLedger
from bounded_loops.adapters.runners.shell import ShellRunner
from bounded_loops.application.run_loop import RunLoopDeps, RunLoopUseCase
from bounded_loops.domain.models import Bounds, Rung, Spec, Status, Verdict

_CEILING_S = 2


class _NeverPasses:
    """A gate that always refuses, so nothing but a bound can end the run."""

    def check(self, ctx):
        return Verdict(False, "still not done")


class _NoMemory:
    def load(self, ctx):
        return ""

    def update(self, ctx, lap, verdict, decision):
        return None


class _NoTracer:
    def span(self, ctx, result, verdict):
        return None


class _NoKillSwitch:
    def tripped(self):
        return False


class _AlwaysApproves:
    def granted(self, verdict, ctx):
        return True


class _FixedClock:
    def now_iso(self):
        return "2026-08-17T00:00:00Z"


def _deps(workspace: Path, agent_cmd: str) -> RunLoopDeps:
    return RunLoopDeps(
        # 300s is the shipped ShellRunner default and the number that terminated the observed run
        # 180s after the declared ceiling had already been blown.
        runner=ShellRunner(agent_cmd=agent_cmd, timeout_s=300),
        gate=_NeverPasses(),
        memory=_NoMemory(),
        ledger=FileLedger(workspace / ".ledger.jsonl"),
        tracer=_NoTracer(),
        budget=BudgetMeter(),
        killswitch=_NoKillSwitch(),
        approval=_AlwaysApproves(),
        clock=_FixedClock(),
    )


def _spec() -> Spec:
    return Spec(
        name="wallclock-probe",
        goal="outlive the declared ceiling",
        steps=("sleep",),
        stop_condition="never",
    )


def _bounds() -> Bounds:
    # A lap cap high enough that reaching it cannot be what ends the run, and no-progress disabled
    # for the same reason: exactly one bound must be able to fire.
    return Bounds(
        max_iterations=50,
        no_progress_window=999,
        max_wallclock_s=_CEILING_S,
        require_approval=False,
    )


def test_a_worker_that_outlives_the_ceiling_halts_at_the_ceiling(tmp_path: Path) -> None:
    started = time.monotonic()
    outcome = RunLoopUseCase(
        spec=_spec(),
        bounds=_bounds(),
        rung=Rung.L1,
        workspace=tmp_path,
        deps=_deps(tmp_path, "sleep 60"),
        cleanup_workspace=False,
    ).run()
    elapsed = time.monotonic() - started

    assert outcome.status is Status.HALT, (
        f"expected HALT on the declared wallclock ceiling, got {outcome.status.value}: "
        f"{outcome.reason}"
    )
    assert "wallclock" in outcome.reason, outcome.reason
    assert f"{_CEILING_S}s" in outcome.reason, (
        f"the halt reason must name the declared bound so a reader can find it in bounds.yaml; "
        f"got {outcome.reason!r}"
    )
    # The whole point: the run ends near the declared ceiling, not near the adapter's 300s default
    # and not near the worker's own 60s sleep.
    assert elapsed < 30, f"run took {elapsed:.1f}s against a declared ceiling of {_CEILING_S}s"


def test_the_receipt_records_a_halt_that_spent_an_attempt(tmp_path: Path) -> None:
    """The unenforced ceiling had TWO failure shapes on the receipt, and this pins both closed.

    Where the adapter's own timeout was longer than the overrun, the run eventually halted on the
    NEXT lap's pre-turn budget check — correct status, wildly late, and recorded with
    `attempted: false`, because that check runs before a turn. So the receipt showed a run that
    blew a 2s ceiling by 58s while claiming no attempt had been spent. Verified by reverting the
    clamp: this assertion fails on `attempted is False`.

    Where the adapter's timeout was SHORTER than the overrun — the shape seen with a real agent CLI
    at a 300s default against a 120s declared ceiling — the attempt died on the adapter's limit and
    the run was reported as Status.ERROR: "your budget ran out" filed as "the harness crashed".

    Both are the same root cause and both are visible here, in `decision` and `attempted`.
    """
    RunLoopUseCase(
        spec=_spec(),
        bounds=_bounds(),
        rung=Rung.L1,
        workspace=tmp_path,
        deps=_deps(tmp_path, "sleep 60"),
        cleanup_workspace=False,
    ).run()

    rows = [
        json.loads(line)
        for line in (tmp_path / ".ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "no ledger rows were written"
    last = rows[-1]
    assert last["decision"] == "halt", f"final decision was {last['decision']!r}, expected 'halt'"
    # `attempted` distinguishes "the worker ran and ran out of budget" from the two pre-turn
    # checks, where no attempt is spent. Getting this wrong makes bound utilisation unauditable
    # from the receipt, which is how a ceiling halt once read as 11 attempts against a cap of 10.
    assert last["attempted"] is True, (
        "a wallclock halt spends an attempt — the worker did run — so the row must not claim "
        "otherwise"
    )


def test_a_worker_inside_the_ceiling_is_untouched(tmp_path: Path) -> None:
    """The positive control. Without it, a runner that failed instantly would pass everything above.

    The gate never passes, so this run ends on the ceiling too — but only after the worker has
    completed several laps, which proves the clamp is not simply killing every attempt.
    """
    outcome = RunLoopUseCase(
        spec=_spec(),
        bounds=_bounds(),
        rung=Rung.L1,
        workspace=tmp_path,
        deps=_deps(tmp_path, "sleep 0.05"),
        cleanup_workspace=False,
    ).run()

    assert outcome.status is Status.HALT
    assert outcome.laps > 1, (
        f"a worker well inside the ceiling completed only {outcome.laps} lap(s); the clamp is "
        "killing attempts that had budget left"
    )
