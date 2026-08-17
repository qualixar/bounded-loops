"""Adversarial test for the paper's strongest invariant.

CLAIM (paper §6, Lemma 6.1, and alg:loop line 2.6):
  "The worker's own claim of completion appears in no guard of the procedure.
   There is no branch anywhere in this procedure that reads the worker's claim
   about its own completion."

ADVERSARIAL DESIGN (staff-engineer audit, 2026-08-17):
  If this claim were false — i.e., if run_loop.py branched on
  `result.agent_claimed_done` — then a runner that ALWAYS returns
  agent_claimed_done=True, paired with a gate that ALWAYS returns
  Verdict(passed=False), would eventually produce Status.DONE.

  This test fires that exact scenario across multiple laps and asserts
  HALT is the only possible terminal status. A single line added to
  run_loop.py that reads `result.agent_claimed_done` would fail it.

WHY THIS IS THE STRONGEST CLAIM:
  Every other guarantee in the paper (termination, soundness) rests on this
  invariant. If the agent can route to DONE by claiming done, then soundness
  collapses: the ledger would contain a DONE with no gate receipt.

HOW TO BREAK IT (mutation to catch):
  In run_loop.py, after line 230 (`result = d.runner.run_once(spec, ctx)`),
  add:
      if result.agent_claimed_done:
          entry = _make_entry(lap, "done", Verdict(True, "agent done"), ...)
          d.ledger.record(entry)
          return Outcome(Status.DONE, "agent-claimed-done", lap, ...)

  This test will fail with:
      assert outcome.status == Status.HALT
      assert Status.DONE not in [e.decision for e in ledger.entries]
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass, field

from bounded_loops.domain.models import (
    Bounds, LedgerEntry, LoopContext, RunResult, Rung, Spec,
    Status, Verdict,
)
from bounded_loops.application.run_loop import RunLoopDeps, RunLoopUseCase


# ── Minimal fakes (no mock framework — the claim lives in the production loop) ──

class _ClockFake:
    def now_iso(self) -> str:
        return "2026-01-01T00:00:00Z"


class _TracerFake:
    def span(self, ctx, result, verdict) -> None:
        pass


class _MemoryFake:
    def load(self, ctx) -> str:
        return ""
    def update(self, ctx, lap, verdict, decision) -> None:
        pass


@dataclass
class _LedgerFake:
    entries: list[LedgerEntry] = field(default_factory=list)
    _path: Path = Path("/dev/null")
    def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
    def path(self) -> Path:
        return self._path
    def head(self) -> str:
        return f"fake-{len(self.entries)}"


class _KillSwitchNever:
    def tripped(self) -> bool:
        return False


class _ApprovalNone:
    def granted(self, verdict, ctx) -> bool:
        return True


@dataclass
class _BudgetFake:
    """Halts at lap > max_lap, never trips token or wallclock."""
    max_lap: int = 5
    _tokens: int = 0

    def exceeded(self, lap: int, bounds: Bounds):
        if lap > self.max_lap:
            return True, f"max_iterations {self.max_lap} reached at lap {lap}"
        return False, ""

    def spend(self, tokens: int) -> None:
        self._tokens += tokens

    def wallclock_budget(self, bounds, *, for_handoff=False):
        return None

    def snapshot(self) -> dict:
        return {"laps": 0, "tokens": self._tokens, "wallclock_s": 0.0}


@dataclass
class _RunnerAlwaysClaimsDone:
    """Runner that always returns agent_claimed_done=True.

    This is the mutation bait: if the loop reads agent_claimed_done and acts
    on it, every lap is a potential early DONE.
    """
    call_count: int = 0

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        self.call_count += 1
        return RunResult(
            changed=True,
            agent_claimed_done=True,   # ← agent always insists it is done
            tokens=0,
            log="agent says: DONE (gate is the sole judge)",
        )


@dataclass
class _GateAlwaysFails:
    """Gate that never passes — the only entity allowed to produce DONE."""
    call_count: int = 0

    def check(self, ctx: LoopContext) -> Verdict:
        self.call_count += 1
        return Verdict(
            passed=False,
            detail="gate independent verdict: NOT DONE",
        )


def _use_case(*, max_lap: int = 5) -> tuple[RunLoopUseCase, _LedgerFake, _RunnerAlwaysClaimsDone, _GateAlwaysFails]:
    runner = _RunnerAlwaysClaimsDone()
    gate = _GateAlwaysFails()
    ledger = _LedgerFake()
    budget = _BudgetFake(max_lap=max_lap)

    spec = Spec(
        name="adversarial",
        goal="adversarial: agent claims done every lap",
        steps=("run forever",),
        stop_condition="at_least_one_pass",
    )
    bounds = Bounds(max_iterations=max_lap, handoff_reserve_s=0)
    rung = Rung.L1
    workspace = Path("/dev/null")

    deps = RunLoopDeps(
        runner=runner,
        gate=gate,
        memory=_MemoryFake(),
        ledger=ledger,
        tracer=_TracerFake(),
        budget=budget,
        killswitch=_KillSwitchNever(),
        approval=_ApprovalNone(),
        clock=_ClockFake(),
    )
    uc = RunLoopUseCase(
        spec=spec, bounds=bounds, rung=rung,
        workspace=workspace, deps=deps,
        cleanup_workspace=False,
    )
    return uc, ledger, runner, gate


# ── The adversarial tests ──

def test_agent_claimed_done_true_every_lap_never_produces_done_status():
    """Core invariant: agent_claimed_done=True is invisible to the loop's decision.

    Gate ALWAYS fails. Runner ALWAYS claims done.
    Only valid terminal state: HALT (budget exhausted).

    If run_loop.py reads result.agent_claimed_done in any control-flow guard,
    this test fails.
    """
    uc, ledger, runner, gate = _use_case(max_lap=5)
    outcome = uc.run()

    assert outcome.status == Status.HALT, (
        f"Expected HALT (gate never passes) but got {outcome.status}. "
        "If the loop branched on result.agent_claimed_done, DONE could appear here."
    )
    assert outcome.status != Status.DONE, "DONE requires a gate verdict — never agent_claimed_done"


def test_agent_claimed_done_true_every_lap_no_done_decision_in_ledger():
    """No ledger entry should carry decision='done'.

    A 'done' decision is the receipt of a DONE outcome, and that requires
    the gate to have passed. The gate here never passes. Any 'done' entry
    in the ledger is evidence that agent_claimed_done influenced the outcome.
    """
    uc, ledger, runner, gate = _use_case(max_lap=5)
    uc.run()

    done_entries = [e for e in ledger.entries if e.decision == "done"]
    assert not done_entries, (
        f"Found {len(done_entries)} ledger entries with decision='done' but gate "
        "never returned Verdict(passed=True). This means agent_claimed_done "
        "influenced the terminal decision — a violation of the soundness invariant."
    )


def test_gate_is_called_independently_of_agent_claimed_done():
    """Gate is called every lap regardless of agent_claimed_done.

    The gate call count must equal the runner call count: the loop cannot
    skip the gate because the agent said it was done.
    """
    uc, ledger, runner, gate = _use_case(max_lap=3)
    uc.run()

    assert runner.call_count == gate.call_count, (
        f"Runner called {runner.call_count} times, gate called {gate.call_count} times. "
        "Every runner invocation must be followed by an independent gate check — "
        "if agent_claimed_done=True skips the gate, the counts diverge."
    )
    assert gate.call_count > 0, "Gate must be called at least once"


def test_many_laps_agent_claimed_done_never_produces_done():
    """Scale test: 20 laps, agent_claimed_done=True each time, gate never passes.

    Scales the adversarial signal. A loop that checked agent_claimed_done
    on any lap would terminate early with DONE.
    """
    uc, ledger, runner, gate = _use_case(max_lap=20)
    outcome = uc.run()

    assert outcome.status == Status.HALT
    assert all(e.decision != "done" for e in ledger.entries), (
        "DONE decision appeared in ledger despite gate never passing. "
        "agent_claimed_done must have been read in a guard — invariant violated."
    )
