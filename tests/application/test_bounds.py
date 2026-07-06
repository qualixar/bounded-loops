"""Acceptance tests for BoundsEnforcer."""
from bounded_loops.application.bounds import BoundsEnforcer
from bounded_loops.domain.models import Bounds, RunResult


def _bounds(window: int = 3) -> Bounds:
    return Bounds(max_iterations=10, no_progress_window=window)


def _result(changed: bool) -> RunResult:
    return RunResult(changed=changed, agent_claimed_done=False)


# ── No-progress detection ──

def test_no_progress_false_when_history_shorter_than_window():
    be = BoundsEnforcer()
    for _ in range(2):
        be.record_lap(_result(changed=False))
    tripped, _ = be.check_no_progress(_bounds(window=3))
    assert not tripped


def test_no_progress_trips_exactly_at_window():
    be = BoundsEnforcer()
    for _ in range(3):
        be.record_lap(_result(changed=False))
    tripped, reason = be.check_no_progress(_bounds(window=3))
    assert tripped
    assert "3" in reason


def test_no_progress_resets_when_change_interspersed():
    be = BoundsEnforcer()
    be.record_lap(_result(changed=False))
    be.record_lap(_result(changed=False))
    be.record_lap(_result(changed=True))   # progress! window resets
    tripped, _ = be.check_no_progress(_bounds(window=3))
    assert not tripped


def test_no_progress_window_1_trips_after_single_unchanged_lap():
    be = BoundsEnforcer()
    be.record_lap(_result(changed=False))
    tripped, _ = be.check_no_progress(_bounds(window=1))
    assert tripped


def test_no_progress_window_5_requires_5_unchanged_laps():
    be = BoundsEnforcer()
    for _ in range(4):
        be.record_lap(_result(changed=False))
    tripped, _ = be.check_no_progress(_bounds(window=5))
    assert not tripped
    be.record_lap(_result(changed=False))
    tripped, _ = be.check_no_progress(_bounds(window=5))
    assert tripped


def test_no_progress_false_when_all_laps_changed():
    be = BoundsEnforcer()
    for _ in range(10):
        be.record_lap(_result(changed=True))
    tripped, _ = be.check_no_progress(_bounds(window=3))
    assert not tripped


def test_multiple_instances_are_independent():
    be1, be2 = BoundsEnforcer(), BoundsEnforcer()
    for _ in range(3):
        be1.record_lap(_result(changed=False))
    tripped1, _ = be1.check_no_progress(_bounds(window=3))
    tripped2, _ = be2.check_no_progress(_bounds(window=3))
    assert tripped1
    assert not tripped2
