"""A bound stops the run; it does not have to destroy the work it interrupted.

THE PROBLEM
-----------
A hard bound has to be hard. But one that reports only "budget exceeded" throws away spend already
paid for, and the next run starts from the same seed with the same budget and no knowledge of what
the last one learned — so a task genuinely needing more than one budget window can never finish,
however many times it is run.

THE RESOLUTION, AND THE PROPERTY THESE TESTS PIN
------------------------------------------------
`handoff_reserve_s` is taken OUT of `max_wallclock_s`, never added to it. Work gets
`ceiling - reserve`, the wind-down turn gets the reserve, and the declared total is unchanged. That
is what keeps every termination guarantee intact, so the tests below check the arithmetic as much as
the feature: a run that produced a handoff must not have outlived its ceiling to do it.

The other property, equally load-bearing: **the wind-down can never change the terminal status.** A
handoff turn that hangs, crashes, or writes nonsense costs the reserve and nothing else. Without
that, "we tried to help you" would be able to turn a clean HALT into a failure — strictly worse than
the brutality it replaced.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bounded_loops.adapters.io.budget import BudgetMeter
from bounded_loops.adapters.io.file_ledger import FileLedger
from bounded_loops.adapters.runners.shell import ShellRunner
from bounded_loops.application.handoff import HANDOFF_FILENAME
from bounded_loops.application.run_loop import RunLoopDeps, RunLoopUseCase
from bounded_loops.domain.models import (
    Bounds,
    RunResult,
    Rung,
    Spec,
    Status,
    Verdict,
)


class _NeverPasses:
    def check(self, ctx):
        return Verdict(False, "3 of 8 records still missing a checksum")


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


class _KillSwitchPulled:
    def tripped(self):
        return True


class _AlwaysApproves:
    def granted(self, verdict, ctx):
        return True


class _FixedClock:
    def now_iso(self):
        return "2026-08-17T00:00:00Z"


class _RecordingRunner:
    """Reports the prompt it was handed, so the wind-down turn is observable without a real CLI."""

    def __init__(self, *, changed: bool = False, raises: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.wallclocks: list[float | None] = []
        self._changed = changed
        self._raises = raises

    def run_once(self, spec, ctx) -> RunResult:
        from bounded_loops.adapters.runners._prompt import build_prompt

        self.prompts.append(build_prompt(spec, ctx))
        self.wallclocks.append(None if ctx.wallclock is None else ctx.wallclock.remaining_s)
        if self._raises is not None:
            raise self._raises
        return RunResult(changed=self._changed, agent_claimed_done=False, tokens=7, log="I did a bit")


def _spec() -> Spec:
    return Spec(
        name="handoff-probe",
        goal="give every record a checksum",
        steps=("read records.json", "add the missing checksums"),
        stop_condition="check_records.py exits 0",
        forbid=("seed/check_records.py",),
    )


def _bounds(**over) -> Bounds:
    base = dict(
        max_iterations=2,
        no_progress_window=99,
        max_wallclock_s=300,
        handoff_reserve_s=60,
        require_approval=False,
    )
    base.update(over)
    return Bounds(**base)  # type: ignore[arg-type]  # heterogeneous kwargs by design


def _deps(workspace: Path, runner, **over) -> RunLoopDeps:
    base = dict(
        runner=runner,
        gate=_NeverPasses(),
        memory=_NoMemory(),
        ledger=FileLedger(workspace / ".ledger.jsonl"),
        tracer=_NoTracer(),
        budget=BudgetMeter(),
        killswitch=_NoKillSwitch(),
        approval=_AlwaysApproves(),
        clock=_FixedClock(),
    )
    base.update(over)
    return RunLoopDeps(**base)


def _run(workspace: Path, runner, bounds=None, **dep_over):
    return RunLoopUseCase(
        spec=_spec(),
        bounds=bounds or _bounds(),
        rung=Rung.L1,
        workspace=workspace,
        deps=_deps(workspace, runner, **dep_over),
        cleanup_workspace=False,
    ).run()


def _rows(workspace: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (workspace / ".ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── B: the harness always leaves evidence ───────────────────────────────────────────────────────

def test_a_lap_cap_halt_writes_a_handoff_naming_the_bound(tmp_path: Path) -> None:
    outcome = _run(tmp_path, _RecordingRunner(changed=True))
    assert outcome.status is Status.HALT

    handoff = (tmp_path / HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "max_iterations 2" in handoff, handoff
    assert "give every record a checksum" in handoff, "the goal is what the next run needs first"
    assert "3 of 8 records still missing a checksum" in handoff, "the gate's last word must survive"
    assert "seed/check_records.py" in handoff, "a handoff that omits `forbid` invites the same error"


def test_the_handoff_is_named_in_the_ledger(tmp_path: Path) -> None:
    """A receipt that does not point at the evidence makes the evidence findable only by convention."""
    _run(tmp_path, _RecordingRunner(changed=True))
    assert _rows(tmp_path)[-1]["handoff"] == HANDOFF_FILENAME


def test_a_no_progress_halt_gets_one_too(tmp_path: Path) -> None:
    """A stuck agent's account of what it tried is the most useful handoff of the set."""
    outcome = _run(
        tmp_path,
        _RecordingRunner(changed=False),
        bounds=_bounds(max_iterations=10, no_progress_window=2),
    )
    assert outcome.status is Status.HALT
    assert "no progress" in outcome.reason
    assert (tmp_path / HANDOFF_FILENAME).exists()


def test_the_handoff_distinguishes_stuck_from_short_of_budget(tmp_path: Path) -> None:
    """The one question a reader has: raise the bound, or fix the approach?"""
    _run(tmp_path, _RecordingRunner(changed=False), bounds=_bounds(max_iterations=10,
                                                                   no_progress_window=2))
    handoff = (tmp_path / HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "changed nothing" in handoff, (
        "per-attempt progress is what separates 'stuck' from 'ran out of budget', and a handoff "
        f"that cannot answer that is decoration:\n{handoff}"
    )


def test_the_kill_switch_gets_no_handoff_and_no_wind_down(tmp_path: Path) -> None:
    """An operator pulling the kill switch wants it to stop NOW, not to spend more of anything."""
    runner = _RecordingRunner()
    outcome = _run(tmp_path, runner, killswitch=_KillSwitchPulled())
    assert outcome.status is Status.KILLED
    assert runner.prompts == [], "the kill switch must not buy the agent another turn"
    assert not (tmp_path / HANDOFF_FILENAME).exists()


# ── A: the reserved wind-down turn ──────────────────────────────────────────────────────────────

def test_the_wind_down_turn_asks_for_a_handoff_not_more_work(tmp_path: Path) -> None:
    runner = _RecordingRunner(changed=True)
    _run(tmp_path, runner)

    last = runner.prompts[-1]
    assert "STOP" in last and "budget is spent" in last, last
    assert "Do NOT continue the task" in last
    # The failure this guards: an agent handed its original goal again keeps working and gets cut
    # off part-way, which is the outcome the whole feature exists to prevent.
    assert "add the missing checksums" not in last, (
        "the wind-down turn was handed the loop's own instructions:\n" + last
    )


def test_the_agents_account_reaches_the_handoff_marked_unverified(tmp_path: Path) -> None:
    _run(tmp_path, _RecordingRunner(changed=True))
    handoff = (tmp_path / HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "I did a bit" in handoff, "the wind-down turn's output was discarded"
    assert "Unverified" in handoff, (
        "the agent's self-report must be marked as unchecked: no gate has looked at it, and the "
        "sections above it were assembled from records"
    )


def test_declining_the_reserve_still_leaves_the_harness_handoff(tmp_path: Path) -> None:
    """`handoff_reserve_s: 0` buys no turn. It must not cost the free evidence."""
    runner = _RecordingRunner(changed=True)
    _run(tmp_path, runner, bounds=_bounds(handoff_reserve_s=0))

    assert len(runner.prompts) == 2, "reserve 0 must not invoke a wind-down turn"
    handoff = (tmp_path / HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "No wind-down turn was recorded" in handoff
    assert "max_iterations 2" in handoff


def test_a_wind_down_turn_that_explodes_cannot_change_the_outcome(tmp_path: Path) -> None:
    """The invariant. Anything else makes the feature able to make a run worse."""

    class _FailsOnTheWindDown:
        def __init__(self):
            self.calls = 0

        def run_once(self, spec, ctx):
            self.calls += 1
            if ctx.prompt_override:
                raise RuntimeError("the CLI fell over during the handoff")
            return RunResult(changed=True, agent_claimed_done=False, tokens=1, log="work")

    runner = _FailsOnTheWindDown()
    outcome = _run(tmp_path, runner)

    assert outcome.status is Status.HALT, (
        f"a failed wind-down changed the terminal status to {outcome.status.value}"
    )
    assert "max_iterations 2" in outcome.reason
    assert (tmp_path / HANDOFF_FILENAME).exists(), "the harness section must survive regardless"


# ── The arithmetic: reserved, not added ─────────────────────────────────────────────────────────

def test_the_work_budget_excludes_the_reserve(tmp_path: Path) -> None:
    """Work attempts see `ceiling - reserve`; the wind-down sees the full ceiling."""
    runner = _RecordingRunner(changed=True)
    _run(tmp_path, runner, bounds=_bounds(max_wallclock_s=300, handoff_reserve_s=60))

    work, wind_down = runner.wallclocks[:-1], runner.wallclocks[-1]
    for remaining in work:
        assert remaining is not None and remaining <= 240 + 1, (
            f"a work attempt was offered {remaining}s of a 300s ceiling holding back 60s"
        )
    assert wind_down is not None and wind_down > 240, (
        f"the wind-down turn got {wind_down}s; the reserve it was held back for was not released"
    )


def test_a_run_that_produced_a_handoff_did_not_outlive_its_ceiling(tmp_path: Path) -> None:
    """The whole claim in one assertion, measured against the wall.

    Ceiling 6s, reserve 3s: the worker sleeps past the work budget, the ceiling fires, a wind-down
    turn runs inside the reserve. Total elapsed must still respect the DECLARED ceiling — the point
    of reserving rather than appending.
    """
    started = time.monotonic()
    outcome = RunLoopUseCase(
        spec=_spec(),
        bounds=Bounds(
            max_iterations=50,
            no_progress_window=999,
            max_wallclock_s=6,
            handoff_reserve_s=3,
            require_approval=False,
        ),
        rung=Rung.L1,
        workspace=tmp_path,
        deps=_deps(tmp_path, ShellRunner(agent_cmd="sleep 30", timeout_s=600)),
        cleanup_workspace=False,
    ).run()
    elapsed = time.monotonic() - started

    assert outcome.status is Status.HALT
    assert "wallclock" in outcome.reason
    # Generous slack for process teardown and the gate, but far below "ceiling plus a free turn":
    # granting the wind-down ON TOP would put this near 6 + 30.
    assert elapsed < 20, (
        f"run took {elapsed:.1f}s against a declared 6s ceiling with a 3s reserve — the reserve is "
        "being added to the ceiling rather than taken out of it"
    )
    assert (tmp_path / HANDOFF_FILENAME).exists()


def _load(tmp_path: Path, body: str):
    from bounded_loops.application.manifest import _load_bounds  # noqa: PLC0415

    path = tmp_path / "bounds.yaml"
    path.write_text(body, encoding="utf-8")
    return _load_bounds(path, tmp_path)


def test_a_reserve_at_half_the_ceiling_is_refused_by_the_manifest(tmp_path: Path) -> None:
    """Load time is the right place to be told, not run time.

    Otherwise the run starts, gets one attempt, and produces a handoff explaining that it had no
    time to do anything — a correctly-behaving loop reporting a configuration mistake as a result.
    """
    from bounded_loops.application.manifest import ManifestError  # noqa: PLC0415

    with pytest.raises(ManifestError, match="at least half"):
        _load(tmp_path, "max_iterations: 3\nmax_wallclock_s: 100\nhandoff_reserve_s: 50\n")


def test_a_negative_reserve_is_refused(tmp_path: Path) -> None:
    from bounded_loops.application.manifest import ManifestError  # noqa: PLC0415

    with pytest.raises(ManifestError, match="handoff_reserve_s must be at least 0"):
        _load(tmp_path, "max_iterations: 3\nmax_wallclock_s: 900\nhandoff_reserve_s: -1\n")


def test_zero_is_a_real_choice_the_schema_accepts(tmp_path: Path) -> None:
    """0 means "decline the wind-down". It must be expressible without abusing null, which for
    every other bound already means "use the conservative platform default"."""
    bounds = _load(tmp_path, "max_iterations: 3\nmax_wallclock_s: 900\nhandoff_reserve_s: 0\n")
    assert bounds.handoff_reserve_s == 0


def test_the_default_is_on(tmp_path: Path) -> None:
    """A reserve that defaults off is a feature nobody gets."""
    bounds = _load(tmp_path, "max_iterations: 10\nmax_wallclock_s: 990\n")
    assert bounds.handoff_reserve_s > 0
