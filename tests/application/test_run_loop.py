"""
Full-path acceptance tests for RunLoopUseCase.
Each terminal path (DONE / HALT / PAUSE / KILLED) is covered.
Uses only Fake ports — zero real I/O.
"""
from pathlib import Path

from bounded_loops.application.run_loop import RunLoopUseCase, RunLoopDeps
from bounded_loops.domain.models import (
    Spec, Bounds, Verdict, RunResult, Status, Rung,
)
# Import canonical fakes from the ports test file
from tests.application.test_ports import (
    FakeRunner, FakeGate, FakeMemory, FakeLedger,
    FakeTracer, FakeBudget, FakeKillSwitch, FakeApproval, FakeClock,
)


# ── Fixtures ──

WORKSPACE = Path("/tmp/fake-workspace")

def _spec(name: str = "test-loop") -> Spec:
    return Spec(
        name=name, goal="Fix the bug",
        steps=("Fix it.",), stop_condition="pytest passes",
    )

def _bounds(max_iter: int = 10, window: int = 3,
            max_tokens=None, max_wallclock_s=None,
            require_approval=None) -> Bounds:
    return Bounds(
        max_iterations=max_iter,
        no_progress_window=window,
        max_tokens=max_tokens,
        max_wallclock_s=max_wallclock_s,
        require_approval=require_approval,
    )

def _deps(runner=None, gate=None, memory=None, ledger=None,
          tracer=None, budget=None, killswitch=None,
          approval=None, clock=None) -> RunLoopDeps:
    return RunLoopDeps(
        runner     = runner     or FakeRunner(RunResult(changed=True, agent_claimed_done=False)),
        gate       = gate       or FakeGate([Verdict(passed=True, detail="gate ok")]),
        memory     = memory     or FakeMemory(),
        ledger     = ledger     or FakeLedger(),
        tracer     = tracer     or FakeTracer(),
        budget     = budget     or FakeBudget(),
        killswitch = killswitch or FakeKillSwitch(),
        approval   = approval   or FakeApproval(grants=True),
        clock      = clock      or FakeClock(),
    )

def _use_case(spec=None, bounds=None, rung=Rung.L1, deps=None) -> RunLoopUseCase:
    return RunLoopUseCase(
        spec      = spec  or _spec(),
        bounds    = bounds or _bounds(),
        rung      = rung,
        workspace = WORKSPACE,
        deps      = deps  or _deps(),
    )


# ══════════════════════════════════════════════════════════
# PATH 1: DONE — gate passes on first lap
# ══════════════════════════════════════════════════════════

def test_done_on_first_lap_when_gate_passes():
    gate   = FakeGate([Verdict(passed=True, detail="tests green")])
    ledger = FakeLedger()
    uc = _use_case(deps=_deps(gate=gate, ledger=ledger))

    outcome = uc.run()

    assert outcome.status == Status.DONE
    assert outcome.laps == 1
    assert outcome.reason == "gate-passed"


def test_done_records_exactly_one_ledger_entry():
    gate   = FakeGate([Verdict(passed=True, detail="ok")])
    ledger = FakeLedger()
    uc = _use_case(deps=_deps(gate=gate, ledger=ledger))

    uc.run()
    assert len(ledger._entries) == 1
    assert ledger._entries[0].decision == "done"


def test_done_after_three_laps():
    # First 2 laps: gate fails; lap 3: gate passes
    gate   = FakeGate([
        Verdict(passed=False, detail="red"),
        Verdict(passed=False, detail="red"),
        Verdict(passed=True,  detail="green"),
    ])
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
    ledger = FakeLedger()
    uc = _use_case(deps=_deps(runner=runner, gate=gate, ledger=ledger))

    outcome = uc.run()

    assert outcome.status == Status.DONE
    assert outcome.laps == 3
    # 2 "continue" entries + 1 "done"
    decisions = [e.decision for e in ledger._entries]
    assert decisions == ["continue", "continue", "done"]


def test_agent_claimed_done_does_not_cause_done_when_gate_fails():
    """I1: agent_claimed_done=True is IGNORED if gate fails."""
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=True))  # agent CLAIMS done
    gate   = FakeGate([Verdict(passed=False, detail="gate still red")])
    # budget trips on lap 2 so the loop terminates with HALT, not DONE
    budget = FakeBudget(trip_at_lap=2, trip_reason="max_iterations 1 reached at lap 2")
    ledger = FakeLedger()
    uc = _use_case(bounds=_bounds(max_iter=1), deps=_deps(runner=runner, gate=gate, budget=budget, ledger=ledger))

    outcome = uc.run()

    # Must be HALT (budget), never DONE from agent's own claim
    assert outcome.status == Status.HALT
    assert "DONE" not in str(outcome.reason)


# ══════════════════════════════════════════════════════════
# PATH 2: HALT — budget (lap cap)
# ══════════════════════════════════════════════════════════

def test_halt_on_lap_cap():
    # Budget trips at lap 6 (max_iterations=5 → lap 6 > 5)
    gate   = FakeGate([Verdict(passed=False, detail="red")])
    budget = FakeBudget(trip_at_lap=6, trip_reason="max_iterations 5 reached at lap 6")
    ledger = FakeLedger()
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
    uc = _use_case(
        bounds = _bounds(max_iter=5),
        deps   = _deps(runner=runner, gate=gate, budget=budget, ledger=ledger),
    )

    outcome = uc.run()

    assert outcome.status == Status.HALT
    assert "max_iterations" in outcome.reason
    assert outcome.laps == 6


def test_halt_lap_cap_ledger_entry_has_halt_decision():
    gate   = FakeGate([Verdict(passed=False, detail="red")])
    budget = FakeBudget(trip_at_lap=2, trip_reason="max_iterations reached")
    ledger = FakeLedger()
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
    uc = _use_case(deps=_deps(runner=runner, gate=gate, budget=budget, ledger=ledger))

    uc.run()

    # Last entry must be a halt entry, first entry is "continue" from lap 1.
    # decision is the closed literal "halt" — the reason
    # ("max_iterations reached", ...) lives in Outcome.reason, checked
    # separately in test_halt_on_lap_cap above.
    last = ledger._entries[-1]
    assert last.decision == "halt"


# ══════════════════════════════════════════════════════════
# PATH 2b: HALT — no-progress
# ══════════════════════════════════════════════════════════

def test_halt_no_progress_after_window():
    """Agent never changes workspace (changed=False); no-progress window=2 triggers HALT."""
    runner = FakeRunner(RunResult(changed=False, agent_claimed_done=False))
    gate   = FakeGate([Verdict(passed=False, detail="red")])
    ledger = FakeLedger()
    uc = _use_case(
        bounds = _bounds(max_iter=20, window=2),
        deps   = _deps(runner=runner, gate=gate, ledger=ledger),
    )

    outcome = uc.run()

    assert outcome.status == Status.HALT
    assert "no-progress" in outcome.reason or "no progress" in outcome.reason
    assert outcome.laps == 2  # exactly at window


def test_halt_no_progress_not_before_window():
    """no-progress window=3: lap 1+2 with no change should NOT halt yet."""
    call_count = [0]
    results = [
        RunResult(changed=False, agent_claimed_done=False),
        RunResult(changed=False, agent_claimed_done=False),
        RunResult(changed=True,  agent_claimed_done=False),  # lap 3: progress
    ]
    class CountingRunner:
        def run_once(self, spec, ctx):
            i = call_count[0]
            call_count[0] += 1
            return results[i] if i < len(results) else results[-1]

    gate = FakeGate([
        Verdict(passed=False, detail="red"),
        Verdict(passed=False, detail="red"),
        Verdict(passed=True,  detail="green"),
    ])
    ledger = FakeLedger()
    uc = _use_case(
        bounds = _bounds(max_iter=20, window=3),
        deps   = _deps(runner=CountingRunner(), gate=gate, ledger=ledger),
    )

    outcome = uc.run()
    # lap 3 has progress so no-progress clears; gate passes on lap 3 → DONE
    assert outcome.status == Status.DONE


# ══════════════════════════════════════════════════════════
# PATH 3: PAUSE — approval required, not granted
# ══════════════════════════════════════════════════════════

def test_pause_when_l2_rung_and_approval_denied():
    gate     = FakeGate([Verdict(passed=True, detail="gate ok")])
    approval = FakeApproval(grants=False)
    ledger   = FakeLedger()
    uc = _use_case(
        rung   = Rung.L2,
        deps   = _deps(gate=gate, approval=approval, ledger=ledger),
    )

    outcome = uc.run()

    assert outcome.status == Status.PAUSE
    assert outcome.reason == "awaiting-approval"
    assert ledger._entries[-1].decision == "pause"


def test_pause_when_l3_rung_and_approval_denied():
    gate     = FakeGate([Verdict(passed=True, detail="gate ok")])
    approval = FakeApproval(grants=False)
    ledger   = FakeLedger()
    uc = _use_case(
        rung = Rung.L3,
        deps = _deps(gate=gate, approval=approval, ledger=ledger),
    )

    outcome = uc.run()
    assert outcome.status == Status.PAUSE


def test_no_pause_when_l1_rung_regardless_of_approval():
    """L1 rung: approval is not required; even if ApprovalPort returns False, we get DONE."""
    gate     = FakeGate([Verdict(passed=True, detail="ok")])
    approval = FakeApproval(grants=False)
    ledger   = FakeLedger()
    uc = _use_case(
        rung = Rung.L1,
        deps = _deps(gate=gate, approval=approval, ledger=ledger),
    )

    outcome = uc.run()
    assert outcome.status == Status.DONE


def test_done_when_l2_rung_and_approval_granted():
    gate     = FakeGate([Verdict(passed=True, detail="ok")])
    approval = FakeApproval(grants=True)
    ledger   = FakeLedger()
    uc = _use_case(
        rung = Rung.L2,
        deps = _deps(gate=gate, approval=approval, ledger=ledger),
    )

    outcome = uc.run()
    assert outcome.status == Status.DONE


# ══════════════════════════════════════════════════════════
# PATH 4: KILLED
# ══════════════════════════════════════════════════════════

def test_killed_when_killswitch_trips_immediately():
    ks     = FakeKillSwitch(trip_at_call=1)   # trips on first poll
    ledger = FakeLedger()
    uc = _use_case(deps=_deps(killswitch=ks, ledger=ledger))

    outcome = uc.run()

    assert outcome.status == Status.KILLED
    assert outcome.reason == "killed"
    assert ledger._entries[-1].decision == "killed"


def test_killed_after_some_laps():
    """KillSwitch trips on lap 3's poll."""
    ks     = FakeKillSwitch(trip_at_call=3)
    gate   = FakeGate([Verdict(passed=False, detail="red")])
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
    ledger = FakeLedger()
    uc = _use_case(deps=_deps(runner=runner, gate=gate, killswitch=ks, ledger=ledger))

    outcome = uc.run()

    assert outcome.status == Status.KILLED
    assert outcome.laps == 3


def test_killswitch_checked_before_runner():
    """Runner must NOT be called on the lap where killswitch trips."""
    run_count = [0]
    class CountingRunner:
        def run_once(self, spec, ctx):
            run_count[0] += 1
            return RunResult(changed=True, agent_claimed_done=False)

    ks = FakeKillSwitch(trip_at_call=1)   # trips on lap 1 before runner
    uc = _use_case(deps=_deps(runner=CountingRunner(), killswitch=ks))

    uc.run()
    assert run_count[0] == 0   # runner never called when killswitch fires first


# ══════════════════════════════════════════════════════════
# Invariant checks
# ══════════════════════════════════════════════════════════

def test_tracer_span_called_once_per_completed_lap():
    tracer = FakeTracer()
    gate   = FakeGate([
        Verdict(passed=False, detail="red"),
        Verdict(passed=True,  detail="green"),
    ])
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
    uc = _use_case(deps=_deps(runner=runner, gate=gate, tracer=tracer))

    outcome = uc.run()

    assert outcome.status == Status.DONE
    # Tracer called on both completed laps (runner ran, gate ran)
    assert len(tracer.calls) == 2


def test_memory_update_not_called_on_terminal_done():
    """memory.update must only be called on 'continue' laps."""
    class CountingMemory(FakeMemory):
        def __init__(self):
            super().__init__()
            self.update_count = 0

        def update(self, ctx, lap, verdict, decision):
            self.update_count += 1

    gate   = FakeGate([Verdict(passed=True, detail="ok")])
    mem    = CountingMemory()
    uc = _use_case(deps=_deps(gate=gate, memory=mem))

    uc.run()
    assert mem.update_count == 0   # DONE on lap 1; no continue path


def test_memory_update_called_on_continue_laps():
    class CountingMemory(FakeMemory):
        def __init__(self):
            super().__init__()
            self.update_count = 0

        def update(self, ctx, lap, verdict, decision):
            self.update_count += 1
            assert decision == "continue"   # the only value memory.update ever sees

    gate = FakeGate([
        Verdict(passed=False, detail="red"),
        Verdict(passed=False, detail="red"),
        Verdict(passed=True,  detail="green"),
    ])
    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
    mem    = CountingMemory()
    uc = _use_case(deps=_deps(runner=runner, gate=gate, memory=mem))

    uc.run()
    assert mem.update_count == 2  # 2 continue laps before DONE


def test_ledger_entry_ts_from_clock():
    clock  = FakeClock(ts="2099-01-01T00:00:00Z")
    gate   = FakeGate([Verdict(passed=True, detail="ok")])
    ledger = FakeLedger()
    uc = _use_case(deps=_deps(gate=gate, ledger=ledger, clock=clock))

    uc.run()
    assert ledger._entries[0].ts == "2099-01-01T00:00:00Z"


def test_outcome_ledger_path_matches_ledger():
    ledger = FakeLedger(p=Path("/tmp/my-ledger.jsonl"))
    gate   = FakeGate([Verdict(passed=True, detail="ok")])
    uc = _use_case(deps=_deps(gate=gate, ledger=ledger))

    outcome = uc.run()
    assert outcome.ledger_path == Path("/tmp/my-ledger.jsonl")


def test_tokens_accumulated_in_budget():
    """budget.spend() is called with result.tokens each lap."""
    class TrackingBudget(FakeBudget):
        def __init__(self):
            super().__init__()
            self.spent_calls = []

        def spend(self, tokens):
            self.spent_calls.append(tokens)

    runner = FakeRunner(RunResult(changed=True, agent_claimed_done=False, tokens=42))
    gate   = FakeGate([
        Verdict(passed=False, detail="red"),
        Verdict(passed=True,  detail="green"),
    ])
    budget = TrackingBudget()
    uc = _use_case(deps=_deps(runner=runner, gate=gate, budget=budget))

    uc.run()
    # spend called once per lap where runner executed (2 laps before DONE)
    assert budget.spent_calls == [42, 42]


def test_unique_trace_id_per_run():
    """Each run() call generates a fresh trace_id (lap context carries it)."""
    captured = []
    class TraceCapturer(FakeTracer):
        def span(self, ctx, result, verdict):
            captured.append(ctx.trace_id)

    gate   = FakeGate([Verdict(passed=True, detail="ok")])
    tracer = TraceCapturer()
    uc = _use_case(deps=_deps(gate=gate, tracer=tracer))

    uc.run()
    assert len(captured) == 1
    assert len(captured[0]) == 32  # uuid4().hex length


# ══════════════════════════════════════════════════════════
# Require_approval override via Bounds
# ══════════════════════════════════════════════════════════

def test_require_approval_true_in_bounds_forces_approval_on_l1():
    """
    bounds.require_approval=True overrides rung-derived default.
    Even L1 requires approval when the manifest says so.
    """
    gate     = FakeGate([Verdict(passed=True, detail="ok")])
    approval = FakeApproval(grants=False)
    ledger   = FakeLedger()
    bounds   = _bounds(require_approval=True)
    uc = _use_case(
        rung   = Rung.L1,
        bounds = bounds,
        deps   = _deps(gate=gate, approval=approval, ledger=ledger),
    )

    outcome = uc.run()
    assert outcome.status == Status.PAUSE


def test_require_approval_false_in_bounds_skips_approval_on_l2():
    """
    bounds.require_approval=False overrides rung-derived default.
    Even L2 skips approval when the manifest explicitly disables it.
    """
    gate     = FakeGate([Verdict(passed=True, detail="ok")])
    approval = FakeApproval(grants=False)  # would block if called
    bounds   = _bounds(require_approval=False)
    uc = _use_case(
        rung   = Rung.L2,
        bounds = bounds,
        deps   = _deps(gate=gate, approval=approval),
    )

    outcome = uc.run()
    assert outcome.status == Status.DONE
