"""
BoundsEnforcer — pure application-layer helper for the no-progress bound.

Owns the accumulation of per-lap progress flags across a run; delegates the
sliding-window predicate itself to `domain.rules.no_progress`, which is a
pure function over the list this class accumulates.

Does NOT check lap/token/wallclock limits (BudgetMeterPort's job) and does
NOT poll the kill switch (RunLoopUseCase's job).
"""

from __future__ import annotations

from bounded_loops.domain import rules
from bounded_loops.domain.models import Bounds, RunResult


class BoundsEnforcer:
    """
    Pure stateless-except-history helper. RunLoopUseCase creates one
    instance per run and calls `record_lap` then `check_no_progress`
    each lap, after the gate has returned a verdict.
    """

    def __init__(self) -> None:
        # One entry per completed lap; True = agent changed the workspace.
        self._progress_history: list[bool] = []

    def record_lap(self, result: RunResult) -> None:
        """Called once per lap, after the runner returns, before the gate check."""
        self._progress_history.append(result.changed)

    def check_no_progress(self, bounds: Bounds) -> tuple[bool, str]:
        """
        Returns (True, reason) if the last `bounds.no_progress_window` laps
        all had `result.changed == False`; (False, "") otherwise.

        Passes the FULL accumulated history + window to
        `domain.rules.no_progress` — that function owns the slicing.
        """
        window = bounds.no_progress_window
        if rules.no_progress(self._progress_history, window):
            return (True, f"no progress in last {window} laps")
        return (False, "")
