"""
BudgetMeter — concrete `BudgetMeterPort` adapter.

The only stateful component here: accumulates token spend and owns
real wallclock measurement via `time.monotonic()`. Thread-safety is not
required — the outer loop is single-threaded.
"""

from __future__ import annotations

import time

from bounded_loops.domain.models import Bounds, WallclockBudget


def effective_reserve_s(bounds: Bounds) -> float:
    """The handoff reserve actually withheld — never more than half the declared ceiling.

    THE RESERVE YIELDS TO THE WORK, NEVER THE REVERSE. The reserve is a courtesy carved out of the
    operator's ceiling; the work budget is the thing the loop exists to spend. So a reserve larger
    than the ceiling can afford is clamped rather than honoured.

    Without the clamp, `Bounds(max_iterations=3, max_wallclock_s=60)` — valid before this field
    existed, and still valid — would pick up the default 90s reserve, leave a work ceiling of zero,
    and halt on the wallclock before running anything. A new default that breaks previously-correct
    code is a bad default, however well-motivated the feature behind it.

    `manifest_bounds.py` separately REFUSES an *authored* reserve at or past half the ceiling, rather
    than clamping it. The asymmetry is deliberate: an author who wrote the number should be told it is
    wrong, while a `Bounds` assembled in code should degrade to a smaller courtesy rather than
    explode. Loud for input, forgiving for internals.

    Since 0.6.6 that refusal can only fire on an authored value, because the manifest's *default*
    reserve is proportional (`default_handoff_reserve_s`) and therefore always strictly below half.
    So this clamp is now unreachable from a `bounds.yaml` and reachable only from code — which is
    exactly the split the paragraph above claims. `test_the_reserve_never_starves_the_work.py`
    asserts both halves; before 0.6.6 neither was tested and the manifest half was wrong.
    """
    if bounds.max_wallclock_s is None:
        raise ValueError("effective_reserve_s requires a declared max_wallclock_s")
    # `(W - 1) // 2`, not `W // 2`. `prop:spend-bound` assumes `0 <= r < W/2` STRICTLY, and
    # `W // 2` sits exactly on the boundary for every even W: a 60 s ceiling yielded a 30 s
    # reserve against a 30 s work budget, so the hypothesis the proposition is stated under
    # was false on this path for 90 of the first 180 declarable ceilings. Found by a
    # proof-referee audit sweeping W numerically rather than by reading either artifact —
    # which is the sixth instance in this project of a bound that was declared in a
    # readable place and not enforced where it said it was.
    #
    # `(W - 1) // 2 < W / 2` holds for every integer W >= 1: for W = 2k it gives k - 1, and
    # for W = 2k + 1 it gives k. The reserve loses at most a second, and the work budget is
    # what gains it, which is the direction this function's whole contract points.
    return float(min(bounds.handoff_reserve_s, (bounds.max_wallclock_s - 1) // 2))


def declared_budget(bounds: Bounds) -> dict[str, object]:
    """The ceilings a receipt should quote, including the one a reader cannot otherwise see.

    `max_wallclock_s` is the operator's number and the honest total: the wind-down turn is PARTITIONED
    out of it, never added to it, so a run cannot outlive the declared ceiling (see
    `application/handoff.py`). Reporting it is correct.

    But WORK stops earlier — at `max_wallclock_s - effective_reserve_s`, which for the shipped
    990 s / 90 s pair is 900 s. Quoting only 990 tells a reader the total and hides the number the
    run is actually cut off at. Both are true; only one of them answers "how close did this come to
    being stopped?", so the receipt carries both rather than choosing.

    `wallclock_work_s` is None when no wallclock ceiling is declared, because there is then no work
    ceiling to derive — not zero, which would read as "no time allowed at all".
    """
    work_ceiling: object = None
    if bounds.max_wallclock_s is not None:
        try:
            work_ceiling = round(bounds.max_wallclock_s - effective_reserve_s(bounds), 2)
        except (ValueError, TypeError):
            work_ceiling = None   # a bound assembled in code that cannot yield a reserve
    return {
        "attempts": bounds.max_iterations,
        "tokens": bounds.max_tokens,
        "wallclock_s": bounds.max_wallclock_s,
        "wallclock_work_s": work_ceiling,
    }

def _work_ceiling(bounds: Bounds) -> float:
    """The wallclock available for WORK: the declared ceiling less the effective reserve.

    One definition, used by both `exceeded()` and `wallclock_budget()`. If those two disagreed about
    where the work budget ends, an attempt could be admitted and then handed zero time — a run that
    halts on a bound it was told it had not reached yet.
    """
    if bounds.max_wallclock_s is None:
        raise ValueError("_work_ceiling requires a declared max_wallclock_s")
    return max(0.0, float(bounds.max_wallclock_s) - effective_reserve_s(bounds))


class BudgetMeter:
    """
    Concrete BudgetMeterPort implementation. Created once per
    RunLoopUseCase.run() call by composition.py.
    """

    def __init__(self) -> None:
        self._tokens_spent: int = 0
        self._start_mono: float = time.monotonic()

    def spend(self, tokens: int) -> None:
        """Accumulate token spend. Called after every runner.run_once call."""
        if tokens < 0:
            raise ValueError(f"tokens must be >= 0, got {tokens}")
        self._tokens_spent += tokens

    def exceeded(self, lap: int, bounds: Bounds) -> tuple[bool, str]:
        """
        Check all budget dimensions at once; called at the top of each lap
        before the runner executes.

        Precedence (first triggered wins): lap cap, then token budget,
        then wallclock limit.
        """
        if lap > bounds.max_iterations:
            return (
                True,
                f"max_iterations {bounds.max_iterations} reached at lap {lap}",
            )

        if bounds.max_tokens is not None:
            if self._tokens_spent >= bounds.max_tokens:
                return (
                    True,
                    f"token budget {bounds.max_tokens} exceeded "
                    f"({self._tokens_spent} spent)",
                )

        if bounds.max_wallclock_s is not None:
            elapsed = time.monotonic() - self._start_mono
            work_ceiling = _work_ceiling(bounds)
            if elapsed >= work_ceiling:
                # Names the DECLARED ceiling, not the work ceiling. The reserve is an internal
                # partition of the operator's number; a reason quoting 900 when bounds.yaml says 990
                # sends a reader looking for a value that is not there.
                return (
                    True,
                    f"wallclock limit {bounds.max_wallclock_s}s exceeded "
                    f"({elapsed:.1f}s elapsed"
                    + (
                        f", {effective_reserve_s(bounds):.0f}s of it reserved for the handoff"
                        if effective_reserve_s(bounds)
                        else ""
                    )
                    + ")",
                )

        return (False, "")

    def wallclock_budget(self, bounds: Bounds, *, for_handoff: bool = False) -> WallclockBudget | None:
        """What is left of the declared ceiling, for the attempt about to start.

        `exceeded()` above answers "may another attempt start?" — a question asked only at a lap
        boundary. This answers "how long may that attempt run?", which is what actually keeps the
        declared ceiling from being decorative. Returns None when the loop declares no ceiling.

        A work attempt is measured against the ceiling MINUS the handoff reserve, so the reserve is
        still there when the work budget runs out. `for_handoff=True` measures against the full
        declared ceiling — that is the wind-down turn spending the reserve, which is the only thing
        the reserve was held back for.

        Never returns a negative remainder: the caller checks `exceeded()` first, and clamping here
        keeps that ordering assumption from turning into a crash if a future caller forgets.
        """
        if bounds.max_wallclock_s is None:
            return None
        ceiling = bounds.max_wallclock_s if for_handoff else _work_ceiling(bounds)
        elapsed = time.monotonic() - self._start_mono
        return WallclockBudget(
            declared_s=bounds.max_wallclock_s,
            remaining_s=max(0.0, ceiling - elapsed),
        )

    def snapshot(self) -> dict:
        """
        Returns the current budget_spent dict for inclusion in LedgerEntry.
        `laps` is left at 0 here — the caller (RunLoopUseCase) owns the lap
        counter and fills it in after calling this method.
        """
        return {
            "laps": 0,
            "tokens": self._tokens_spent,
            "wallclock_s": round(time.monotonic() - self._start_mono, 2),
        }
