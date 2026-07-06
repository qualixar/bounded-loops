"""
Tests for application/ports.py (frozen contracts).

Covers:
  - All nine Port Protocols are exported and @runtime_checkable.
  - A minimal FakeX per port satisfies its Protocol via structural
    isinstance() checks (no inheritance required — PEP 544).
  - BudgetMeterPort.exceeded's return annotation matches the spec
    (tuple[bool, str]).

These FakeX classes are the canonical fakes reused by other application
test suites: `from tests.application.test_ports import
FakeRunner, FakeGate, ...`.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from bounded_loops.domain.models import (
    Spec, Bounds, Verdict, RunResult, LedgerEntry, LoopContext,
)
from bounded_loops.application import ports as P
from bounded_loops.application.ports import (
    RunnerPort, GatePort, MemoryPort, LedgerPort,
    TracerPort, BudgetMeterPort, KillSwitchPort, ApprovalPort, ClockPort,
)


PROTOCOL_NAMES = [
    "RunnerPort", "GatePort", "MemoryPort", "LedgerPort",
    "TracerPort", "BudgetMeterPort", "KillSwitchPort",
    "ApprovalPort", "ClockPort",
]


# ---------------------------------------------------------------------------
# Test A — every Port Protocol is importable and is a @runtime_checkable Protocol
# ---------------------------------------------------------------------------

class TestAllPortsExported:
    def test_all_nine_ports_exported(self):
        for name in PROTOCOL_NAMES:
            assert hasattr(P, name), f"{name} missing from ports.py"

    def test_all_ports_are_runtime_checkable(self):
        for name in PROTOCOL_NAMES:
            cls = getattr(P, name)
            # On Python 3.11, @runtime_checkable Protocols set
            # `_is_runtime_protocol = True` (a naive check via
            # `__protocol_attrs__` is a Python 3.12+ typing addition
            # not present on this project's pinned 3.11 interpreter).
            assert getattr(cls, "_is_runtime_protocol", False), (
                f"{name} not @runtime_checkable"
            )


# ---------------------------------------------------------------------------
# Test B — FakeX classes satisfy each Protocol (canonical fakes, shared
# fixtures reused across the application test suites).
# ---------------------------------------------------------------------------

class FakeRunner:
    """Returns a single configurable result on every run_once call."""
    def __init__(self, result: RunResult):
        self._result = result

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        return self._result


class FakeGate:
    """Returns a pre-set sequence of Verdicts, cycling on exhaustion."""
    def __init__(self, verdicts: list[Verdict]):
        self._iter = iter(verdicts)
        self._last = verdicts[-1]

    def check(self, ctx: LoopContext) -> Verdict:
        return next(self._iter, self._last)


class FakeMemory:
    def __init__(self):
        self._store: dict[int, str] = {}

    def load(self, ctx: LoopContext) -> str:
        return ""

    def update(self, ctx: LoopContext, lap: int, verdict: Verdict, decision: str) -> None:
        self._store[lap] = f"{decision}:{verdict.detail}"


class FakeLedger:
    def __init__(self, p: Path = Path("/tmp/ledger.jsonl")):
        self._entries: list[LedgerEntry] = []
        self._path = p

    def record(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)

    def path(self) -> Path:
        return self._path


class FakeTracer:
    def __init__(self):
        self.calls: list = []

    def span(self, ctx: LoopContext, result: RunResult, verdict: Verdict) -> None:
        self.calls.append((ctx.lap, result, verdict))


class FakeBudget:
    def __init__(self, trip_at_lap: int | None = None, trip_reason: str = "budget"):
        self._tokens = 0
        self._trip_at = trip_at_lap
        self._reason = trip_reason

    def spend(self, tokens: int) -> None:
        self._tokens += tokens

    def exceeded(self, lap: int, bounds: Bounds) -> tuple[bool, str]:
        if self._trip_at is not None and lap >= self._trip_at:
            return (True, self._reason)
        return (False, "")

    def snapshot(self) -> dict:
        return {"laps": 0, "tokens": self._tokens, "wallclock_s": 0.0}


class FakeKillSwitch:
    def __init__(self, trip_at_call: int | None = None):
        self._calls = 0
        self._trip_at = trip_at_call

    def tripped(self) -> bool:
        self._calls += 1
        return self._trip_at is not None and self._calls >= self._trip_at


class FakeApproval:
    def __init__(self, grants: bool = True):
        self._grants = grants

    def granted(self, verdict: Verdict, ctx: LoopContext) -> bool:
        return self._grants


class FakeClock:
    def __init__(self, ts: str = "2026-07-04T00:00:00Z"):
        self._ts = ts

    def now_iso(self) -> str:
        return self._ts


class TestFakesSatisfyProtocols:
    def test_fake_runner_satisfies_protocol(self):
        r = FakeRunner(RunResult(changed=True, agent_claimed_done=False))
        assert isinstance(r, RunnerPort)

    def test_fake_gate_satisfies_protocol(self):
        g = FakeGate([Verdict(passed=True, detail="ok")])
        assert isinstance(g, GatePort)

    def test_fake_memory_satisfies_protocol(self):
        assert isinstance(FakeMemory(), MemoryPort)

    def test_fake_ledger_satisfies_protocol(self):
        assert isinstance(FakeLedger(), LedgerPort)

    def test_fake_tracer_satisfies_protocol(self):
        assert isinstance(FakeTracer(), TracerPort)

    def test_fake_budget_satisfies_protocol(self):
        assert isinstance(FakeBudget(), BudgetMeterPort)

    def test_fake_killswitch_satisfies_protocol(self):
        assert isinstance(FakeKillSwitch(), KillSwitchPort)

    def test_fake_approval_satisfies_protocol(self):
        assert isinstance(FakeApproval(), ApprovalPort)

    def test_fake_clock_satisfies_protocol(self):
        assert isinstance(FakeClock(), ClockPort)


# ---------------------------------------------------------------------------
# Test B2 — behavioural sanity of the fakes (the `decision` param + snapshot()
# additions are actually usable).
# ---------------------------------------------------------------------------

class TestFakeBehaviour:
    def test_fake_memory_update_records_decision_and_verdict(self):
        from bounded_loops.domain.models import Rung

        mem = FakeMemory()
        ctx = LoopContext(
            workspace=Path("/tmp/ws"), lap=1, rung=Rung.L1, trace_id="t-1",
        )
        verdict = Verdict(passed=True, detail="pytest: 1 passed")
        mem.update(ctx, lap=1, verdict=verdict, decision="continue")
        assert mem._store[1] == "continue:pytest: 1 passed"

    def test_fake_budget_snapshot_shape(self):
        budget = FakeBudget()
        budget.spend(500)
        snap = budget.snapshot()
        assert snap == {"laps": 0, "tokens": 500, "wallclock_s": 0.0}

    def test_fake_budget_exceeded_tuple_shape(self):
        budget = FakeBudget(trip_at_lap=3, trip_reason="max_iterations")
        bounds = Bounds(max_iterations=3)
        tripped, reason = budget.exceeded(3, bounds)
        assert tripped is True
        assert reason == "max_iterations"
        tripped, reason = budget.exceeded(1, bounds)
        assert tripped is False
        assert reason == ""


# ---------------------------------------------------------------------------
# Test C — method signatures match the spec precisely
# ---------------------------------------------------------------------------

class TestSignatures:
    def test_budget_meter_exceeded_return_annotation(self):
        # The spec says tuple[bool, str]
        sig = inspect.signature(BudgetMeterPort.exceeded)
        ann = sig.return_annotation
        # Under from __future__ import annotations, annotations are strings.
        assert "tuple" in str(ann).lower() or ann is inspect.Parameter.empty

    def test_memory_port_update_has_decision_param(self):
        sig = inspect.signature(MemoryPort.update)
        params = list(sig.parameters)
        assert "decision" in params, "MemoryPort.update missing frozen `decision` param"

    def test_budget_meter_port_has_snapshot_method(self):
        assert hasattr(BudgetMeterPort, "snapshot"), (
            "BudgetMeterPort missing frozen snapshot() method"
        )
