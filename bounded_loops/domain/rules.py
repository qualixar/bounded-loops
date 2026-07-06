"""
Pure domain predicates for bounded-loops loop control.

Three exported functions:
  stop_condition_met(spec, verdict)         → bool
  no_progress(lap_changed, window)          → bool
  rung_requires_approval(rung, bounds)      → bool

Rules:
  - Imports: stdlib only + domain/models.  No I/O.  No side-effects.
  - All three functions are pure (same inputs → same output, no mutation).
  - Called by RunLoopUseCase; never call these from adapters.
"""

from __future__ import annotations

from typing import Sequence

from bounded_loops.domain.models import Bounds, Rung, Spec, Verdict


def stop_condition_met(spec: Spec, verdict: Verdict) -> bool:
    """
    Return True iff the gate verdict satisfies the loop's stop condition.

    Called in RunLoopUseCase only after confirming verdict.passed is True. In v1 this is a conservative identity over verdict.passed:
    if the gate passed, the stop condition is met.

    Args:
        spec:    The loop spec. stop_condition is advisory metadata
                 used by humans and tooling; not evaluated as code here.
        verdict: The gate result for this lap. Must have passed=True
                 when this function is called (caller's responsibility).

    Returns:
        True  → loop may exit (subject to approval check).
        False → loop continues despite a passed gate.

    v1 algorithm:
        RETURN verdict.passed
        (Any True gate verdict satisfies the condition in v1.)

    Future:
        May inspect spec.stop_condition as a structured expression and
        cross-check against verdict.evidence for richer semantics.
    """
    # v1: gate-passed == condition-met
    return verdict.passed


def no_progress(lap_changed: Sequence[bool], window: int) -> bool:
    """
    Return True iff the last `window` laps all show no workspace change.

    Used by RunLoopUseCase (via BoundsEnforcer) to trigger HALT on a
    spinning agent that keeps running without ever touching the workspace.

    Args:
        lap_changed: Ordered `changed` flags, one per completed lap,
                     most-recent-last. Owned/accumulated by BoundsEnforcer
                     — this function does no accumulation itself.
        window:      Consecutive unchanged-lap count that triggers halt.
                     Sourced from Bounds.no_progress_window (default 3).

    Returns:
        True  → the last `window` laps were all changed=False → HALT.
        False → recent progress detected, or not enough laps yet.

    Note: window <= 0 is treated as no-trigger (vacuously False), per C
    edge-case spec ("window = 0: treated as no-trigger"). Callers validate
    window >= 1 in manifest.py; this is a defensive fallback only.
    """
    if window <= 0:
        return False
    tail = lap_changed[-window:]
    if len(tail) < window:
        return False
    return all(c is False for c in tail)


def rung_requires_approval(rung: Rung, bounds: Bounds) -> bool:
    """
    Return True iff a human approval gate is required before exiting DONE.

    Implements bound #8 (require_approval) with the derivation rule from
    bounds.yaml spec: null → L1=False, L2/L3=True.

    Args:
        rung:   The loop's safety rung (Rung.L1 | Rung.L2 | Rung.L3).
        bounds: The frozen Bounds object.
                bounds.require_approval may be True, False, or None.

    Returns:
        True  → loop must call ApprovalPort.granted() before DONE;
                if not granted, emit Outcome(PAUSE, "awaiting-approval").
        False → loop may exit DONE without approval.

    Derivation:
        If bounds.require_approval is not None → use it directly.
        If bounds.require_approval is None:
            L1 → False  (report rung; human reads output but loop exits)
            L2 → True   (assisted; human in the loop for exit approval)
            L3 → True   (unattended; approval enforced by default)
    """
    if bounds.require_approval is not None:
        return bounds.require_approval
    # Derive: L1 → no approval; L2/L3 → approval required
    return rung in (Rung.L2, Rung.L3)
