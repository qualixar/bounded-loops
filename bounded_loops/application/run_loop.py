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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Mapping

from bounded_loops.application.bounds import BoundsEnforcer
from bounded_loops.application.handoff import (
    HANDOFF_FILENAME,
    run_summary,
    wind_down_prompt,
)
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
from bounded_loops.domain.errors import GateError, RunnerError, WallclockExceeded
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
    #: WHICH gate this run wired, computed ONCE by `composition` from its own registry and from
    #: installed package metadata — never asked of the gate. Stamped onto every ledger row so a
    #: verdict in the receipt says what decided it.
    #:
    #: Pushed IN rather than derived here because `application` must not import `composition`
    #: (see `test_layering`), and because the honest source for "which distribution" is the
    #: entry-point scan that only the composer has done.
    gate_provenance: Mapping[str, str] = field(default_factory=dict)
    #: The ceilings this run declared, pushed in for the same reason as `gate_provenance`:
    #: the composer is the layer that has resolved the manifest's bounds.
    budget_declared: Mapping[str, object] = field(default_factory=dict)


def _make_entry(
    lap: int,
    decision: Decision,
    verdict: Verdict,
    budget_spent: dict,
    deps: RunLoopDeps,
    *,
    attempted: bool = True,
    handoff: str = "",
) -> LedgerEntry:
    """Build one ledger entry. `decision` is always a plain closed-set value —
    the halt/pause/kill REASON lives exclusively in Outcome.reason, never here.

    Takes `deps`, not `deps.clock`. Both the timestamp and the gate provenance come from it, and a
    signature that accepted only the clock would need every one of the seven call sites to remember
    to pass provenance as well. "A fix reached one call site and its siblings kept the bug" is the
    defect this codebase has committed most often; one parameter carrying both makes the omission
    unrepresentable rather than merely discouraged.

    `attempted=False` is for the two pre-turn checks only (kill switch, budget ceiling). Those
    record a lap on which the worker was never invoked, so counting rows as attempts overstates
    consumption by one on exactly the runs where the ceiling bites.
    """
    return LedgerEntry(
        lap=lap,
        ts=deps.clock.now_iso(),
        verdict=verdict,
        decision=decision,
        budget_spent=budget_spent,
        attempted=attempted,
        handoff=handoff,
        gate=dict(deps.gate_provenance),
        budget_declared=dict(deps.budget_declared),
    )


def _snap(deps: RunLoopDeps, lap: int) -> dict:
    """
    Build the budget_spent dict for the ledger. `snapshot()` is a required
    Protocol method on BudgetMeterPort — called unconditionally.
    No hasattr() fallback: a BudgetMeterPort implementor that omits snapshot()
    must fail loudly, not silently degrade the ledger entry.
    """
    snap = dict(deps.budget.snapshot())
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
            return self._stamp_ledger_head(self._run())
        finally:
            self._cleanup_workspace()

    def _stamp_ledger_head(self, outcome: Outcome) -> Outcome:
        """Attach the ledger head to every terminal outcome, on one code path.

        Deliberately here and not at the ten `return Outcome(...)` sites: a receipt
        field that each exit branch has to remember to set is a field some branch
        will eventually forget, and the branches that would forget are the failure
        paths — exactly the runs whose evidence matters most. Reading the head after
        `_run` returns also guarantees it commits to the final row, including a
        wind-down handoff written on the way out.
        """
        return replace(outcome, ledger_head=self._deps.ledger.head())

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
        memory_snapshot = d.memory.load(ctx0)
        lap = 0
        # Per-attempt facts for the handoff. The ledger is append-only and write-only from here, so
        # a run cannot read its own receipt back to describe itself; these are the same facts, kept.
        attempt_rows: list[dict] = []
        attempts_spent = 0

        # ── OUTER LOOP ──
        while True:
            lap += 1

            # ── 1. Kill-switch check (highest priority) ──
            if d.killswitch.tripped():
                entry = _make_entry(
                    lap, "killed", Verdict(False, "killed"), {}, d, attempted=False
                )
                d.ledger.record(entry)
                return Outcome(Status.KILLED, "killed", lap, d.ledger.path())

            # ── 2. Budget check (before running the agent) ──
            tripped, why = d.budget.exceeded(lap, bounds)
            if tripped:
                # decision="halt"; the WHY
                # lives ONLY in Outcome.reason, never encoded into decision.
                return self._halt_on_bound(
                    lap=lap,
                    reason=why,
                    rung=rung,
                    trace_id=trace_id,
                    memory_snapshot=memory_snapshot,
                    rows=attempt_rows,
                    attempts_spent=attempts_spent,
                    last_verdict=None,
                    attempted=False,
                )

            # ── 2b. Context is built HERE, after the budget check, so the wallclock remainder it
            # carries is measured as late as possible — immediately before the turn it constrains.
            # `exceeded()` above only answers "may another attempt start?"; the remainder below is
            # what stops an attempt already under way from running past the declared ceiling.
            ctx = LoopContext(
                workspace=self._workspace,
                lap=lap,
                rung=rung,
                trace_id=trace_id,
                env=self._env_passthrough,
                memory_snapshot=memory_snapshot,
                wallclock=d.budget.wallclock_budget(bounds),
            )

            # ── 3. Run the agent (one turn) ──
            try:
                result = d.runner.run_once(spec, ctx)
                attempts_spent += 1
            except WallclockExceeded as exc:
                # The declared ceiling expired mid-attempt. HALT, not ERROR: a bound firing is the
                # harness working, and a run that stopped because it was told to must not be
                # recorded the same way as a run that broke. `attempted` stays True — the worker
                # did run; it ran out of budget, which is the opposite of never having started.
                return self._halt_on_bound(
                    lap=lap,
                    reason=str(exc),
                    rung=rung,
                    trace_id=trace_id,
                    memory_snapshot=memory_snapshot,
                    rows=attempt_rows,
                    attempts_spent=attempts_spent + 1,
                    last_verdict=None,
                    attempted=True,
                )
            except RunnerError as exc:
                verdict = _error_verdict("runner", exc)
                entry = _make_entry(lap, "error", verdict, _snap(d, lap), d)
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
                entry = _make_entry(lap, "error", verdict, _snap(d, lap), d)
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
                        entry = _make_entry(lap, "pause", verdict, _snap(d, lap), d)
                        d.ledger.record(entry)
                        return Outcome(Status.PAUSE, "awaiting-approval", lap, d.ledger.path())

                entry = _make_entry(lap, "done", verdict, _snap(d, lap), d)
                d.ledger.record(entry)
                return Outcome(Status.DONE, "gate-passed", lap, d.ledger.path())

            # Facts for the handoff, gathered per attempt because the ledger is write-only.
            attempt_rows.append(
                {"lap": lap, "changed": result.changed, "detail": verdict.detail}
            )

            # ── 8b. No-progress check ──
            np_tripped, np_why = self._enforcer.check_no_progress(bounds)
            if np_tripped:
                # A stuck agent's account of what it tried is the most useful handoff of the set,
                # not the least — so this bound gets a wind-down like every other.
                return self._halt_on_bound(
                    lap=lap,
                    reason=np_why,
                    rung=rung,
                    trace_id=trace_id,
                    memory_snapshot=memory_snapshot,
                    rows=attempt_rows,
                    attempts_spent=attempts_spent,
                    last_verdict=verdict,
                    attempted=True,
                )

            # ── 8c. Continue ──
            d.memory.update(ctx, lap, verdict, "continue")
            entry = _make_entry(lap, "continue", verdict, _snap(d, lap), d)
            d.ledger.record(entry)
            # Loop back to top

    def _halt_on_bound(
        self,
        *,
        lap: int,
        reason: str,
        rung: Rung,
        trace_id: str,
        memory_snapshot: str,
        rows: list[dict],
        attempts_spent: int,
        last_verdict: Verdict | None,
        attempted: bool,
    ) -> Outcome:
        """Wind the run down, write the handoff, record the halt. Every bound halt comes here.

        One path, because the alternative is four halt sites each deciding for itself whether a
        handoff happens — and the one that forgot would be indistinguishable from a run that had
        nothing to hand off. The kill switch deliberately does NOT come here: an operator pulling it
        wants the run to stop now, not to spend more of anything.

        INVARIANT: nothing in here can change the terminal status. The run halted on a bound before
        this method was called and it halts on that bound after. A wind-down turn that fails, hangs,
        or writes nonsense costs the reserve and no more; a handoff that cannot be written is a
        missing file, not an ERROR. Otherwise "we tried to help you" would be able to turn a clean
        HALT into a failure, which is a strictly worse outcome than the brutality it replaced.
        """
        d = self._deps
        bounds = self._bounds
        agent_account = ""

        if bounds.handoff_reserve_s > 0:
            agent_account = self._wind_down_turn(
                lap=lap,
                reason=reason,
                rung=rung,
                trace_id=trace_id,
                memory_snapshot=memory_snapshot,
                attempts_spent=attempts_spent,
            )

        handoff_name = self._write_handoff(
            reason=reason,
            lap=lap,
            rows=rows,
            attempts_spent=attempts_spent,
            last_verdict=last_verdict,
            agent_account=agent_account,
        )

        entry = _make_entry(
            lap, "halt", Verdict(False, reason), _snap(d, lap), d,
            attempted=attempted, handoff=handoff_name,
        )
        d.ledger.record(entry)
        return Outcome(Status.HALT, reason, lap, d.ledger.path())

    def _wind_down_turn(
        self,
        *,
        lap: int,
        reason: str,
        rung: Rung,
        trace_id: str,
        memory_snapshot: str,
        attempts_spent: int,
    ) -> str:
        """One final turn, paid for out of the reserve, asking only for a handoff.

        `for_handoff=True` measures the remaining time against the FULL declared ceiling — this is
        the turn the reserve was withheld for. The prompt override is what stops the runner reading
        the loop's own `PROMPT.md`: an agent handed its original goal again keeps working, and gets
        cut off part-way, which is exactly the outcome this feature exists to prevent.

        Every failure is swallowed on purpose. See the invariant on `_halt_on_bound`.
        """
        d = self._deps
        try:
            ctx = LoopContext(
                workspace=self._workspace,
                lap=lap,
                rung=rung,
                trace_id=trace_id,
                env=self._env_passthrough,
                memory_snapshot=memory_snapshot,
                wallclock=d.budget.wallclock_budget(self._bounds, for_handoff=True),
                prompt_override=wind_down_prompt(
                    spec=self._spec, reason=reason, attempts_spent=attempts_spent
                ),
            )
            result = d.runner.run_once(self._spec, ctx)
            # The wind-down's own tokens are spend and are metered like any other. The budget is
            # already blown; not counting them would understate what the run cost.
            d.budget.spend(result.tokens)
            return result.log
        except Exception:
            # Deliberately broad. A runner may raise anything, and the run has already halted on a
            # bound: there is no failure here worth converting into a different outcome.
            return ""

    def _write_handoff(
        self,
        *,
        reason: str,
        lap: int,
        rows: list[dict],
        attempts_spent: int,
        last_verdict: Verdict | None,
        agent_account: str,
    ) -> str:
        """Write the handoff next to the ledger, and return its filename (empty if not written).

        Next to the ledger rather than in the workspace because a scratch workspace is deleted when
        the run ends — a handoff written there would be destroyed by the same run that produced it.
        The ledger is the durable receipt, and this belongs with it.
        """
        try:
            document = run_summary(
                spec=self._spec,
                bounds=self._bounds,
                reason=reason,
                laps=lap,
                attempts_spent=attempts_spent,
                rows=rows,
                last_verdict=last_verdict,
                agent_handoff=agent_account,
            )
            target = self._deps.ledger.path().parent / HANDOFF_FILENAME
            target.write_text(document, encoding="utf-8")
            return HANDOFF_FILENAME
        except OSError:
            # An unwritable ledger directory is not a reason to change a HALT into an ERROR.
            return ""

    def _cleanup_workspace(self) -> None:
        if not self._cleanup_workspace_on_finish:
            return
        marker = self._workspace / _SCRATCH_MARKER
        if self._workspace.name.startswith("bounded-loops-") and marker.exists():
            shutil.rmtree(self._workspace, ignore_errors=True)
