"""
RunLoopUseCase — the orchestration heart of bounded-loops.

Owns exactly one responsibility: implement the  loop algorithm,
coordinating all injected ports. Contains NO I/O of its own — every I/O
call goes through a port. All business logic lives in domain `rules.py`.
`BoundsEnforcer` owns no-progress history.

THE single most important invariant (stated three times in the HLD):
`result.agent_claimed_done` is IGNORED when deciding whether the loop
terminates. Only `gate.check()` returning `Verdict(passed=True)` with
`rules.stop_condition_met(spec, verdict)` returning True drives a DONE
outcome. The agent's own claim is advisory metadata recorded in the
ledger, nothing more.

Application layer imports domain + ports ONLY — no concrete adapters.
"""

from __future__ import annotations

import uuid
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bounded_loops.application.bounds import BoundsEnforcer
from bounded_loops.application.ports import (
    ApprovalPort,
    BudgetMeterPort,
    ClockPort,
    GatePort,
    KillSwitchPort,
    LedgerPort,
    MemoryPort,
    RunnerPort,
    TracerPort,
)
from bounded_loops.domain.errors import GateError, RunnerError
from bounded_loops.domain.models import (
    Bounds,
    LedgerEntry,
    LoopContext,
    Outcome,
    Rung,
    Spec,
    Status,
    Verdict,
)
from bounded_loops.domain.rules import rung_requires_approval, stop_condition_met

Decision = Literal["continue", "done", "halt", "pause", "killed", "error"]
_SCRATCH_MARKER = ".bounded-loops-scratch"


@dataclass
class RunLoopDeps:
    """
    Mutable container bundling all nine injected ports.

    Not frozen — this is an internal wiring container, not a domain value.
    `composition.py` builds one instance per run and passes it in.
    """

    runner: RunnerPort
    gate: GatePort
    memory: MemoryPort
    ledger: LedgerPort
    tracer: TracerPort
    budget: BudgetMeterPort
    killswitch: KillSwitchPort
    approval: ApprovalPort
    clock: ClockPort


def _make_entry(
    lap: int,
    decision: Decision,
    verdict: Verdict,
    budget_spent: dict,
    clock: ClockPort,
) -> LedgerEntry:
    """Build one ledger entry. `decision` is always a plain closed-set value —
    the halt/pause/kill REASON lives exclusively in Outcome.reason, never here."""
    return LedgerEntry(
        lap=lap,
        ts=clock.now_iso(),
        verdict=verdict,
        decision=decision,
        budget_spent=budget_spent,
    )


def _snap(deps: RunLoopDeps, lap: int) -> dict:
    """
    Build the budget_spent dict for the ledger. `snapshot()` is a required
    Protocol method on BudgetMeterPort — called unconditionally.
    No hasattr() fallback: a BudgetMeterPort implementor that omits snapshot()
    must fail loudly, not silently degrade the ledger entry.
    """
    snap = deps.budget.snapshot()
    snap["laps"] = lap
    return snap


def _error_verdict(component: str, exc: Exception) -> Verdict:
    error_type = type(exc).__name__
    detail = f"{component} error: {error_type}: {exc}"
    return Verdict(
        passed=False,
        detail=detail,
        evidence={"component": component, "error_type": error_type},
    )


class RunLoopUseCase:
    """
    Execute the bounded loop for a single Spec/Bounds/Rung combination.

    `run()` returns an Outcome with one of DONE / HALT / PAUSE / KILLED.
    All intermediate state changes go through ports; this method is
    idempotent given the same sequence of port responses.
    """

    def __init__(
        self,
        spec: Spec,
        bounds: Bounds,
        rung: Rung,
        workspace: Path,
        deps: RunLoopDeps,
        env_passthrough: dict[str, str] | None = None,
        cleanup_workspace: bool = True,
    ) -> None:
        self._spec = spec
        self._bounds = bounds
        self._rung = rung
        self._workspace = workspace
        self._deps = deps
        self._enforcer = BoundsEnforcer()  # owns no-progress history
        self._env_passthrough = env_passthrough or {}
        self._cleanup_workspace_on_finish = cleanup_workspace

    def run(self) -> Outcome:
        try:
            return self._run()
        finally:
            self._cleanup_workspace()

    def _run(self) -> Outcome:
        d = self._deps
        spec, bounds, rung = self._spec, self._bounds, self._rung

        # ── INIT ──
        trace_id = uuid.uuid4().hex
        ctx0 = LoopContext(
            workspace=self._workspace,
            lap=0,
            rung=rung,
            trace_id=trace_id,
            env=self._env_passthrough,
        )
        d.memory.load(ctx0)  # populate memory from STATE.md; result is advisory
        lap = 0

        # ── OUTER LOOP ──
        while True:
            lap += 1
            ctx = LoopContext(
                workspace=self._workspace,
                lap=lap,
                rung=rung,
                trace_id=trace_id,
                env=self._env_passthrough,
            )

            # ── 1. Kill-switch check (highest priority) ──
            if d.killswitch.tripped():
                entry = _make_entry(lap, "killed", Verdict(False, "killed"), {}, d.clock)
                d.ledger.record(entry)
                return Outcome(Status.KILLED, "killed", lap, d.ledger.path())

            # ── 2. Budget check (before running the agent) ──
            tripped, why = d.budget.exceeded(lap, bounds)
            if tripped:
                # decision="halt"; the WHY
                # lives ONLY in Outcome.reason, never encoded into decision.
                entry = _make_entry(lap, "halt", Verdict(False, why), _snap(d, lap), d.clock)
                d.ledger.record(entry)
                return Outcome(Status.HALT, why, lap, d.ledger.path())

            # ── 3. Run the agent (one turn) ──
            try:
                result = d.runner.run_once(spec, ctx)
            except RunnerError as exc:
                verdict = _error_verdict("runner", exc)
                entry = _make_entry(lap, "error", verdict, _snap(d, lap), d.clock)
                d.ledger.record(entry)
                return Outcome(Status.ERROR, verdict.detail, lap, d.ledger.path())

            # ── 4. Accumulate token spend AFTER runner returns ──
            d.budget.spend(result.tokens)

            # ── 5. Record lap in no-progress enforcer (before gate) ──
            self._enforcer.record_lap(result)

            # ── 6. Check gate INDEPENDENTLY — agent_claimed_done is NOT READ HERE ──
            try:
                verdict = d.gate.check(ctx)
            except GateError as exc:
                verdict = _error_verdict("gate", exc)
                entry = _make_entry(lap, "error", verdict, _snap(d, lap), d.clock)
                d.ledger.record(entry)
                return Outcome(Status.ERROR, verdict.detail, lap, d.ledger.path())

            # ── 7. Emit tracer span ──
            d.tracer.span(ctx, result, verdict)

            # ── 8. Decide terminal or continue ──
            if verdict.passed and stop_condition_met(spec, verdict):

                # ── 8a. DONE or PAUSE (rung/approval check) ──
                if rung_requires_approval(rung, bounds):
                    approval_granted = d.approval.granted(verdict, ctx)
                    if not approval_granted:
                        entry = _make_entry(lap, "pause", verdict, _snap(d, lap), d.clock)
                        d.ledger.record(entry)
                        return Outcome(Status.PAUSE, "awaiting-approval", lap, d.ledger.path())

                entry = _make_entry(lap, "done", verdict, _snap(d, lap), d.clock)
                d.ledger.record(entry)
                return Outcome(Status.DONE, "gate-passed", lap, d.ledger.path())

            # ── 8b. No-progress check ──
            np_tripped, np_why = self._enforcer.check_no_progress(bounds)
            if np_tripped:
                entry = _make_entry(lap, "halt", Verdict(False, np_why), _snap(d, lap), d.clock)
                d.ledger.record(entry)
                return Outcome(Status.HALT, np_why, lap, d.ledger.path())

            # ── 8c. Continue ──
            d.memory.update(ctx, lap, verdict, "continue")
            entry = _make_entry(lap, "continue", verdict, _snap(d, lap), d.clock)
            d.ledger.record(entry)
            # Loop back to top

    def _cleanup_workspace(self) -> None:
        if not self._cleanup_workspace_on_finish:
            return
        marker = self._workspace / _SCRATCH_MARKER
        if self._workspace.name.startswith("bounded-loops-") and marker.exists():
            shutil.rmtree(self._workspace, ignore_errors=True)
