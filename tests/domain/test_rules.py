"""
Tests for domain/rules.py (frozen contracts, flow semantics).
All three predicates are covered with True/False cases and edge cases.

NOTE (deviation from the original design spec, flagged not silently
fixed): the spec's own acceptance-test listing contains a
`TestFlowScenario` class whose `no_progress(...)` calls use a stale 3-arg
pattern (`no_progress(history, result, window=3)`, passing `RunResult`/
list-of-`RunResult` as a second positional arg). That directly contradicts
the frozen 2-arg `no_progress(lap_changed: Sequence[bool], window: int)`
signature fixed everywhere else in the spec ("a pure domain function over
a plain bool list, never touching ... RunResult directly"). This is
exactly the old buggy call pattern this codebase retired.
`TestFlowScenario` below preserves the spec's intent (simulating the
flow sequence across three predicates) but calls `no_progress` only with
the frozen 2-arg form, accumulating `changed` flags into a plain
`list[bool]` the way the spec's own worked example demonstrates.
"""
from typing import Any

from bounded_loops.domain.models import (
    Rung, Bounds, Spec, Verdict, RunResult,
)
from bounded_loops.domain.rules import (
    stop_condition_met,
    no_progress,
    rung_requires_approval,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_spec(**kwargs: Any) -> Spec:
    defaults: dict[str, Any] = dict(
        name="test-loop",
        goal="Do something.",
        steps=("Step 1.",),
        stop_condition="pytest exits 0",
    )
    defaults.update(kwargs)
    return Spec(**defaults)


def make_bounds(**kwargs: Any) -> Bounds:
    defaults: dict[str, Any] = dict(max_iterations=5)
    defaults.update(kwargs)
    return Bounds(**defaults)


def rr(changed: bool, claimed: bool = False, tokens: int = 0) -> RunResult:
    """Convenience RunResult factory (used by stop_condition_met/approval tests only —
    no_progress tests below use plain bool lists per the frozen signature)."""
    return RunResult(changed=changed, agent_claimed_done=claimed, tokens=tokens)


# ---------------------------------------------------------------------------
# stop_condition_met
# ---------------------------------------------------------------------------

class TestStopConditionMet:
    def test_returns_true_when_verdict_passed(self):
        spec = make_spec()
        v = Verdict(passed=True, detail="pytest: 42 passed")
        assert stop_condition_met(spec, v) is True

    def test_returns_false_when_verdict_not_passed(self):
        spec = make_spec()
        v = Verdict(passed=False, detail="pytest: 1 failed")
        assert stop_condition_met(spec, v) is False

    def test_stop_condition_string_content_does_not_affect_result(self):
        # stop_condition is human-readable metadata; not evaluated in v1
        spec_a = make_spec(stop_condition="pytest exits 0")
        spec_b = make_spec(stop_condition="axe finds 0 violations")
        v = Verdict(passed=True, detail="ok")
        assert stop_condition_met(spec_a, v) is True
        assert stop_condition_met(spec_b, v) is True

    def test_evidence_content_does_not_affect_result(self):
        spec = make_spec()
        v_with_evidence = Verdict(
            passed=True,
            detail="ok",
            evidence={"tests_passed": 42, "duration_s": 0.8},
        )
        v_empty_evidence = Verdict(passed=True, detail="ok")
        assert stop_condition_met(spec, v_with_evidence) is True
        assert stop_condition_met(spec, v_empty_evidence) is True

    def test_pure_no_side_effects(self):
        # Calling twice with same inputs returns same result
        spec = make_spec()
        v = Verdict(passed=True, detail="ok")
        assert stop_condition_met(spec, v) is True
        assert stop_condition_met(spec, v) is True

    def test_forbid_field_does_not_affect_result(self):
        spec = make_spec(forbid=("edit tests/",))
        v = Verdict(passed=True, detail="ok")
        assert stop_condition_met(spec, v) is True


# ---------------------------------------------------------------------------
# no_progress
# ---------------------------------------------------------------------------

class TestNoProgress:
    """FROZEN signature: no_progress(lap_changed: Sequence[bool], window: int).
    BoundsEnforcer owns accumulating the list; these tests pass plain bool lists directly."""

    # ---- True cases (trigger → HALT) ----

    def test_window_3_all_unchanged_returns_true(self):
        assert no_progress([False, False, False], window=3) is True

    def test_window_1_single_unchanged_returns_true(self):
        assert no_progress([False], window=1) is True

    def test_window_2_both_unchanged_returns_true(self):
        assert no_progress([False, False], window=2) is True

    def test_window_5_all_unchanged_returns_true(self):
        assert no_progress([False] * 5, window=5) is True

    # ---- False cases (no trigger → continue) ----

    def test_window_3_not_enough_history_returns_false(self):
        assert no_progress([False, False], window=3) is False

    def test_window_3_zero_history_returns_false(self):
        assert no_progress([], window=3) is False

    def test_window_1_changed_returns_false(self):
        assert no_progress([True], window=1) is False

    def test_window_3_first_in_window_changed_returns_false(self):
        assert no_progress([True, False, False], window=3) is False

    def test_window_3_middle_changed_returns_false(self):
        assert no_progress([False, True, False], window=3) is False

    def test_window_3_current_changed_returns_false(self):
        assert no_progress([False, False, True], window=3) is False

    def test_window_3_one_change_in_longer_history_does_not_trigger(self):
        # 4 laps of history, but only the LAST window entries matter
        assert no_progress([True, False, False, False], window=3) is True

    def test_window_3_change_in_last_entry_resets(self):
        assert no_progress([False, False, True, False], window=3) is False

    # ---- Edge cases ----

    def test_window_0_returns_false(self):
        assert no_progress([False], window=0) is False

    def test_pure_does_not_mutate_input(self):
        history = [False, False]
        original_len = len(history)
        no_progress(history, window=3)
        assert len(history) == original_len

    def test_large_window_with_exact_history(self):
        assert no_progress([False] * 10, window=10) is True

    def test_large_window_one_short(self):
        assert no_progress([False] * 9, window=10) is False


# ---------------------------------------------------------------------------
# rung_requires_approval
# ---------------------------------------------------------------------------

class TestRungRequiresApproval:
    # ---- Derived from rung (require_approval=None) ----

    def test_l1_derived_returns_false(self):
        bounds = make_bounds(require_approval=None)
        assert rung_requires_approval(Rung.L1, bounds) is False

    def test_l2_derived_returns_true(self):
        bounds = make_bounds(require_approval=None)
        assert rung_requires_approval(Rung.L2, bounds) is True

    def test_l3_derived_returns_true(self):
        bounds = make_bounds(require_approval=None)
        assert rung_requires_approval(Rung.L3, bounds) is True

    def test_default_bounds_l1(self):
        # Bounds() with no require_approval → None → derive from rung
        bounds = make_bounds()   # require_approval defaults to None
        assert rung_requires_approval(Rung.L1, bounds) is False

    def test_default_bounds_l2(self):
        bounds = make_bounds()
        assert rung_requires_approval(Rung.L2, bounds) is True

    def test_default_bounds_l3(self):
        bounds = make_bounds()
        assert rung_requires_approval(Rung.L3, bounds) is True

    # ---- Explicit override wins (require_approval=True) ----

    def test_explicit_true_overrides_l1_derivation(self):
        bounds = make_bounds(require_approval=True)
        assert rung_requires_approval(Rung.L1, bounds) is True

    def test_explicit_true_l2_returns_true(self):
        bounds = make_bounds(require_approval=True)
        assert rung_requires_approval(Rung.L2, bounds) is True

    def test_explicit_true_l3_returns_true(self):
        bounds = make_bounds(require_approval=True)
        assert rung_requires_approval(Rung.L3, bounds) is True

    # ---- Explicit override wins (require_approval=False) ----

    def test_explicit_false_overrides_l2_derivation(self):
        bounds = make_bounds(require_approval=False)
        assert rung_requires_approval(Rung.L2, bounds) is False

    def test_explicit_false_overrides_l3_derivation(self):
        bounds = make_bounds(require_approval=False)
        assert rung_requires_approval(Rung.L3, bounds) is False

    def test_explicit_false_l1_returns_false(self):
        bounds = make_bounds(require_approval=False)
        assert rung_requires_approval(Rung.L1, bounds) is False

    # ---- Purity ----

    def test_pure_no_side_effects(self):
        bounds = make_bounds()
        result_1 = rung_requires_approval(Rung.L2, bounds)
        result_2 = rung_requires_approval(Rung.L2, bounds)
        assert result_1 == result_2

    def test_does_not_mutate_bounds(self):
        bounds = make_bounds(require_approval=None)
        rung_requires_approval(Rung.L2, bounds)
        # Frozen dataclass — require_approval cannot change
        assert bounds.require_approval is None


# ---------------------------------------------------------------------------
# Integration scenario: flow through the loop's decision predicates
# ---------------------------------------------------------------------------

class TestFlowScenario:
    """
    Tests the three predicates in the sequence used by RunLoopUseCase.
    Simulates a 3-lap loop where the gate passes on lap 3.

    DEVIATION FROM THE ORIGINAL SPEC TEXT (flagged, see module docstring):
    the spec's own listing calls no_progress with a stale 3-arg
    (history, result, window=) pattern here. That contradicts the frozen
    2-arg Sequence[bool] signature fixed everywhere else in the spec. This
    class preserves the spec's flow-sequence intent but accumulates plain
    `changed` bools into a list and calls no_progress(lap_changed, window),
    exactly as the spec's own worked example demonstrates.
    """

    def setup_method(self):
        self.spec = make_spec(
            name="bug-fix-red-green",
            goal="Fix the failing test.",
            steps=("Read.", "Patch.", "Run pytest."),
            stop_condition="pytest exits 0",
        )
        self.bounds = make_bounds(
            max_iterations=10,
            no_progress_window=3,
            require_approval=None,
        )
        self.rung = Rung.L2

    def test_lap1_gate_fails_no_progress_not_triggered(self):
        v = Verdict(passed=False, detail="pytest: 1 failed")
        assert stop_condition_met(self.spec, v) is False
        result = rr(changed=True)
        lap_changed = [result.changed]
        # only 1 lap — window=3 not yet saturated
        assert no_progress(lap_changed, window=3) is False

    def test_lap2_gate_fails_agent_made_changes(self):
        lap_changed = [rr(changed=True).changed, rr(changed=True).changed]
        assert no_progress(lap_changed, window=3) is False

    def test_lap3_gate_passes_l2_requires_approval(self):
        v = Verdict(passed=True, detail="pytest: 42 passed")
        # stop_condition_met → True
        assert stop_condition_met(self.spec, v) is True
        # L2 with require_approval=None → True (must pause for approval)
        assert rung_requires_approval(self.rung, self.bounds) is True

    def test_lap3_gate_passes_l1_no_approval_needed(self):
        v = Verdict(passed=True, detail="pytest: 42 passed")
        assert stop_condition_met(self.spec, v) is True
        assert rung_requires_approval(Rung.L1, self.bounds) is False

    def test_no_progress_triggers_after_3_unchanged_laps(self):
        lap_changed = [
            rr(changed=False).changed,
            rr(changed=False).changed,
            rr(changed=False).changed,
        ]
        assert no_progress(lap_changed, window=3) is True

    def test_no_progress_l3_loop_still_halts(self):
        # Even on L3 (unattended), no-progress fires
        lap_changed = [rr(changed=False).changed] * 3
        assert no_progress(lap_changed, window=3) is True
        # Approval check is separate and only happens on gate-pass, not halt
        assert rung_requires_approval(Rung.L3, self.bounds) is True
