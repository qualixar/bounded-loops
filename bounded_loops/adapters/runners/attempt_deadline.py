"""One definition of "how long may this attempt run", and which limit bit.

WHY THIS FILE EXISTS
--------------------
`bounds.max_wallclock_s` was declared in every loop's `bounds.yaml`, wired into every GATE
constructor, and reachable by NO runner: `composition._runner_kwargs` never passed a timeout, so
each adapter used whatever unrelated default it shipped with. The controller compared elapsed time
against the ceiling only at the TOP of a lap, which makes it a between-attempts check. A single
attempt could therefore run arbitrarily past the declared ceiling.

Observed, not theorised: during the cross-model convergence experiment a loop declaring
`max_wallclock_s: 120` ran one attempt for 300.5s and was cut off by `ShellRunner`'s 300s
subprocess default. It was stopped by a limit the loop never declared, 180s after the limit it did
declare had been blown. For a harness whose third guarantee is that the manifest tells you the
ceiling, a declared bound the behaviour ignores is the same defect as a check that always passes.

TWO LIMITS ON ONE ATTEMPT, AND WHY THE DIFFERENCE IS REPORTED
-------------------------------------------------------------
* the runner's own `timeout_s` — an OPERATOR setting on the adapter. Exceeding it means one turn
  took longer than this deployment tolerates. That is a runner-level failure: Status.ERROR.
* the loop's `bounds.max_wallclock_s` — a DECLARED spend ceiling for the whole run. Exceeding it is
  the bound doing its job: Status.HALT, with the bound named.

The tighter limit binds, and which one bound is recorded. Filing a bound firing under the same
heading as a crash would be the reporting failure this project exists to remove — a run that
stopped because it was told to must not read like a run that broke.

WHY THE CEILING IS ANCHORED, NOT RE-DERIVED
-------------------------------------------
`WallclockBudget.remaining_s` is measured by the controller just before hand-off. Anchoring it to
an absolute monotonic instant at the top of `run_once` means everything the runner then does —
building a prompt, digesting the workspace — is spent FROM the budget rather than added ON TOP of
it. Re-deriving "is the ceiling the binding limit?" after the wait cannot work: once a wait has
timed out both limits look nearly expired, and a ceiling between one and two times the configured
timeout classifies wrongly. So the classification is made once, at the same instant as the timeout
it explains, and carried on `WaitBudget`.

WHAT THIS DOES *NOT* CLAMP, DELIBERATELY
----------------------------------------
Gates. A gate has its own `timeout_s` (wired from this same bound at composition time) and is not
cut short by the remaining run budget. Killing a gate mid-check yields no verdict, and this project
already holds the line that a check which could not run must not be recorded as having judged. A
verdict that cost a few extra seconds is worth strictly more than no verdict. The honest statement
of the resulting guarantee is therefore: **no attempt starts after the ceiling and none continues
past it, so total run time is bounded by `max_wallclock_s` plus at most one gate timeout** — not
`max_wallclock_s` exactly. Overstating that would be the same class of error as the bound this file
fixes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from bounded_loops.domain.errors import RunnerError, WallclockExceeded
from bounded_loops.domain.models import LoopContext


@dataclass(frozen=True)
class WaitBudget:
    """A timeout to pass to one blocking wait, plus which limit produced it.

    Both fields are decided together, at one instant, precisely so that the error raised on expiry
    does not have to guess afterwards which limit was responsible.
    """

    #: Seconds to hand to the blocking wait. Never negative.
    timeout_s: float
    #: True when the loop's declared ceiling is the binding limit, False when the adapter's own
    #: configured timeout is tighter (or when no ceiling is declared at all).
    ceiling_binds: bool
    #: `bounds.max_wallclock_s` verbatim, so the halt reason names what the operator wrote.
    #: None when the loop declares no ceiling.
    declared_s: int | None

    def timeout_error(self, component: str, detail: str = "") -> Exception:
        """The exception to raise when a wait using this budget times out.

        Returns rather than raises so the call site reads `raise budget.timeout_error(...)` and the
        traceback starts at the runner, not here.
        """
        suffix = f" ({component}{', ' + detail if detail else ''})"
        if self.ceiling_binds:
            return WallclockExceeded(
                f"wallclock limit {self.declared_s}s exceeded during an attempt{suffix}"
            )
        return RunnerError(f"{component}: timed out after {self.timeout_s:.0f}s{f' {detail}' if detail else ''}")


@dataclass(frozen=True)
class AttemptDeadline:
    """The ceiling for the attempt in progress, anchored to an absolute instant."""

    #: The adapter's own per-attempt timeout, unchanged.
    configured_timeout_s: float
    #: Monotonic instant the declared ceiling expires; None when none is declared.
    ceiling_at_mono: float | None
    declared_s: int | None

    def wait_budget(self) -> WaitBudget:
        """Decide, right now, how long to wait and which limit is binding."""
        if self.ceiling_at_mono is None:
            return WaitBudget(
                timeout_s=self.configured_timeout_s, ceiling_binds=False, declared_s=None
            )
        remaining = self.ceiling_at_mono - time.monotonic()
        if remaining < self.configured_timeout_s:
            # Floor at zero: a wait of "negative seconds" is not a thing, and reaching here with a
            # non-positive remainder means the ceiling expired during the runner's own setup. The
            # attempt then expires immediately, which is the correct outcome — the budget is gone.
            return WaitBudget(
                timeout_s=max(0.0, remaining), ceiling_binds=True, declared_s=self.declared_s
            )
        return WaitBudget(
            timeout_s=self.configured_timeout_s, ceiling_binds=False, declared_s=self.declared_s
        )


def attempt_deadline(configured_timeout_s: float, ctx: LoopContext) -> AttemptDeadline:
    """Anchor the loop's remaining wallclock budget to now.

    Call this at the TOP of `run_once`, before any setup work, so that setup is spent from the
    budget. `ctx.wallclock is None` (no declared ceiling) degrades to the adapter's own timeout,
    which is exactly the pre-existing behaviour for a loop that declares no ceiling.
    """
    budget = ctx.wallclock
    if budget is None:
        return AttemptDeadline(
            configured_timeout_s=float(configured_timeout_s),
            ceiling_at_mono=None,
            declared_s=None,
        )
    return AttemptDeadline(
        configured_timeout_s=float(configured_timeout_s),
        ceiling_at_mono=time.monotonic() + budget.remaining_s,
        declared_s=budget.declared_s,
    )
