# bounded_loops/application/ports.py
# THIS FILE IS THE SEAM. Alter only after a HLD contract change.
"""
Port Protocols for bounded-loops.

This file is the seam between the application layer and every concrete
adapter. Nothing in `application/` may import a concrete adapter;
everything in `adapters/` must satisfy at least one of these nine
Protocols. All nine are `@runtime_checkable` so `isinstance(obj, Port)`
works structurally (PEP 544) without inheritance or ABCMeta registration.

No logic, no state, no I/O in this file — pure type declarations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from bounded_loops.domain.models import (
    Bounds,
    LedgerEntry,
    LoopContext,
    RunResult,
    Spec,
    TurnRequest,
    TurnResult,
    Verdict,
    WallclockBudget,
)


@runtime_checkable
class RunnerPort(Protocol):
    """Execute ONE turn of the agent inside the workspace described by ctx."""
    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult: ...


@runtime_checkable
class RunningTurnPort(Protocol):
    """A controller-owned running turn; no adapter exposes its process."""

    def poll(self) -> object: ...
    def cancel(self, reason: str) -> None: ...
    def wait(self, timeout_s: float | None = None) -> TurnResult: ...


@runtime_checkable
class RunnerPortV2(Protocol):
    """Start one cancellable turn; the legacy RunnerPort remains supported."""

    def start(self, request: TurnRequest) -> RunningTurnPort: ...


@runtime_checkable
class GatePort(Protocol):
    """Check the workspace objectively; NEVER call the agent; NEVER see RunResult."""
    def check(self, ctx: LoopContext) -> Verdict: ...


@runtime_checkable
class MemoryPort(Protocol):
    """Load + update loop-scoped state across laps.

    FROZEN at : update() gains a
    `decision` parameter so STATE.md can record what actually happened
    each lap ("continue"/"halt"/...), not just the verdict. This closes
    a real gap in an earlier draft:  called update() without
    it and 's FileMemory couldn't record the decision at all.
    """
    def load(self, ctx: LoopContext) -> str: ...
    def update(self, ctx: LoopContext, lap: int, verdict: Verdict, decision: str) -> None: ...


@runtime_checkable
class LedgerPort(Protocol):
    """Append-only audit trail of every lap decision."""
    def record(self, entry: LedgerEntry) -> None: ...  # append-only
    def path(self) -> Path: ...


@runtime_checkable
class TracerPort(Protocol):
    """Emit an observability span per lap."""
    def span(self, ctx: LoopContext, result: RunResult, verdict: Verdict) -> None: ...


@runtime_checkable
class BudgetMeterPort(Protocol):
    """Track cumulative token spend; report budget overrun.

    FROZEN at : snapshot() is now
    part of the Protocol (was previously accessed via a duck-typed
    hasattr() check in RunLoopUseCase, which the audit flagged as a
    silent-degradation risk). Every BudgetMeterPort implementor MUST
    provide it — it is the sole source of LedgerEntry.budget_spent.
    """
    def spend(self, tokens: int) -> None: ...
    def exceeded(self, lap: int, bounds: Bounds) -> tuple[bool, str]: ...  # (tripped, reason)
    def snapshot(self) -> dict: ...  # {"laps": int, "tokens": int, "wallclock_s": float}
    #: What is left of bounds.max_wallclock_s, or None when no ceiling is declared. Part of the
    #: Protocol rather than a duck-typed probe for the same reason snapshot() is: `exceeded()` only
    #: runs BETWEEN attempts, so without this the declared ceiling cannot constrain the attempt
    #: itself, and a meter that cannot answer must fail loudly instead of quietly disabling a bound.
    #: `for_handoff=True` measures against the FULL declared ceiling rather than the work ceiling,
    #: which is how the reserved wind-down turn spends the reserve that work was kept out of.
    def wallclock_budget(
        self, bounds: Bounds, *, for_handoff: bool = False
    ) -> WallclockBudget | None: ...


@runtime_checkable
class KillSwitchPort(Protocol):
    """External kill signal; polled at the TOP of every lap before any work."""
    def tripped(self) -> bool: ...


@runtime_checkable
class ApprovalPort(Protocol):
    """Human-in-the-loop approval for L2/L3 rungs before a DONE verdict is accepted."""
    def granted(self, verdict: Verdict, ctx: LoopContext) -> bool: ...


@runtime_checkable
class ClockPort(Protocol):
    """Injected clock so LedgerEntry.ts is deterministic in tests."""
    def now_iso(self) -> str: ...
