"""
BudgetMeter — concrete `BudgetMeterPort` adapter.

The only stateful component here: accumulates token spend and owns
real wallclock measurement via `time.monotonic()`. Thread-safety is not
required — the outer loop is single-threaded.
"""

from __future__ import annotations

import time

from bounded_loops.domain.models import Bounds


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
            if elapsed >= bounds.max_wallclock_s:
                return (
                    True,
                    f"wallclock limit {bounds.max_wallclock_s}s exceeded "
                    f"({elapsed:.1f}s elapsed)",
                )

        return (False, "")

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
