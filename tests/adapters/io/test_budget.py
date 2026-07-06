"""Acceptance tests for BudgetMeter."""
import pytest
from bounded_loops.adapters.io.budget import BudgetMeter
from bounded_loops.domain.models import Bounds


def _bounds(max_iter=10, max_tokens=None, max_wallclock_s=None) -> Bounds:
    return Bounds(
        max_iterations=max_iter,
        max_tokens=max_tokens,
        max_wallclock_s=max_wallclock_s,
    )


# ── Lap cap (Bound #1) ──

def test_lap_cap_not_exceeded_within_limit():
    bm = BudgetMeter()
    tripped, _ = bm.exceeded(lap=5, bounds=_bounds(max_iter=10))
    assert not tripped


def test_lap_cap_trips_exactly_at_max_plus_one():
    bm = BudgetMeter()
    # lap > max_iterations triggers halt; lap=max_iterations is the LAST allowed lap
    tripped, reason = bm.exceeded(lap=11, bounds=_bounds(max_iter=10))
    assert tripped
    assert "max_iterations" in reason


def test_lap_cap_boundary_lap_equals_max_not_tripped():
    bm = BudgetMeter()
    tripped, _ = bm.exceeded(lap=10, bounds=_bounds(max_iter=10))
    assert not tripped  # lap == max_iterations is still allowed


# ── Token budget (Bound #5) ──

def test_token_budget_not_exceeded_below_limit():
    bm = BudgetMeter()
    bm.spend(500)
    tripped, _ = bm.exceeded(lap=1, bounds=_bounds(max_tokens=1000))
    assert not tripped


def test_token_budget_trips_at_or_above_limit():
    bm = BudgetMeter()
    bm.spend(1000)
    tripped, reason = bm.exceeded(lap=1, bounds=_bounds(max_tokens=1000))
    assert tripped
    assert "token budget" in reason


def test_token_budget_accumulates_across_spend_calls():
    bm = BudgetMeter()
    bm.spend(400)
    bm.spend(400)
    bm.spend(300)  # total = 1100
    tripped, _ = bm.exceeded(lap=1, bounds=_bounds(max_tokens=1000))
    assert tripped


def test_token_budget_none_never_trips():
    bm = BudgetMeter()
    bm.spend(10_000_000)
    tripped, _ = bm.exceeded(lap=1, bounds=_bounds(max_tokens=None))
    assert not tripped


def test_spend_negative_raises():
    bm = BudgetMeter()
    with pytest.raises(ValueError):
        bm.spend(-1)


# ── Wallclock (Bound #9) ──

def test_wallclock_not_exceeded_within_limit():
    bm = BudgetMeter()
    tripped, _ = bm.exceeded(lap=1, bounds=_bounds(max_wallclock_s=60))
    assert not tripped


def test_wallclock_trips_after_sleep(monkeypatch):
    # Monkeypatch time.monotonic to avoid real sleeps in CI
    start = 1000.0
    call_count = [0]
    def fake_mono():
        call_count[0] += 1
        return start if call_count[0] == 1 else start + 31.0
    import bounded_loops.adapters.io.budget as budget_mod
    monkeypatch.setattr(budget_mod.time, "monotonic", fake_mono)
    bm = BudgetMeter()  # records start via fake first call
    tripped, reason = bm.exceeded(lap=1, bounds=_bounds(max_wallclock_s=30))
    assert tripped
    assert "wallclock" in reason


def test_wallclock_none_never_trips():
    bm = BudgetMeter()
    tripped, _ = bm.exceeded(lap=1, bounds=_bounds(max_wallclock_s=None))
    assert not tripped


# ── Precedence: lap cap wins over token/wallclock ──

def test_lap_cap_wins_over_token_budget():
    bm = BudgetMeter()
    bm.spend(999_999)
    # lap=11 > max_iterations=10 should fire first
    tripped, reason = bm.exceeded(lap=11, bounds=_bounds(max_iter=10, max_tokens=100))
    assert tripped
    assert "max_iterations" in reason


# ── snapshot ──

def test_snapshot_tokens_matches_spend():
    bm = BudgetMeter()
    bm.spend(250)
    bm.spend(250)
    snap = bm.snapshot()
    assert snap["tokens"] == 500


def test_snapshot_wallclock_is_float():
    bm = BudgetMeter()
    snap = bm.snapshot()
    assert isinstance(snap["wallclock_s"], float)
    assert snap["wallclock_s"] >= 0


# ── Protocol conformance ──

def test_budget_meter_satisfies_protocol():
    from bounded_loops.application.ports import BudgetMeterPort
    bm = BudgetMeter()
    assert isinstance(bm, BudgetMeterPort)
