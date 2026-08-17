"""ENG-02: the wind-down reserve is a courtesy, and a courtesy must not cost the work.

My own defect, from the session that added the reserve. Three problems in one field:

1. The default of 90 s was substituted *before* the half-ceiling check, so any loop
   declaring `max_wallclock_s: 180` or lower was refused outright — a manifest that
   had been valid before the field existed, rejected by an error quoting a number the
   author never wrote.
2. Two code paths disagreed about what to do when the reserve did not fit: the
   manifest **refuses**, `Bounds` construction **silently clamps**. Both behaviours
   are defensible; having both with nothing recording which applies where is not.
3. There was no test at the boundary at all, which is how (1) shipped.

The invariant these tests pin: `(W - r) + r = W` with `0 <= r < W/2`, for every W a
user can declare, with no configuration that leaves the work budget at or below half
the declared ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.adapters.io.budget import (
    BudgetMeter,
    _work_ceiling,
    effective_reserve_s,
)
from bounded_loops.application.manifest_bounds import (
    NOMINAL_HANDOFF_RESERVE_S,
    default_handoff_reserve_s,
)
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Bounds


def _bounds(*, wallclock: int, reserve: int | None = None) -> Bounds:
    kwargs = {} if reserve is None else {"handoff_reserve_s": reserve}
    return Bounds(max_iterations=3, max_wallclock_s=wallclock, **kwargs)


def _write_loop(tmp_path: Path, bounds_yaml: str) -> Path:
    """A minimal loop package, so validation runs through the real manifest path."""
    loop = tmp_path / "loop"
    loop.mkdir(parents=True)
    (loop / "bounds.yaml").write_text(bounds_yaml, encoding="utf-8")
    return loop


def _load(loop_dir: Path) -> Bounds:
    from bounded_loops.application.manifest_bounds import _load_bounds

    return _load_bounds(loop_dir / "bounds.yaml", loop_dir)


# ── the defaulted reserve never invalidates a ceiling ────────────────────────────


@pytest.mark.parametrize("wallclock", [30, 60, 119, 120, 179, 180, 181, 270, 360, 990])
def test_a_ceiling_with_no_declared_reserve_always_loads(
    tmp_path: Path, wallclock: int
) -> None:
    """180 and below is the range 0.6.5 refused. It is the range users pick for fast loops."""
    bounds = _load(_write_loop(
        tmp_path, f"max_iterations: 3\nmax_wallclock_s: {wallclock}\n",
    ))
    assert bounds.max_wallclock_s == wallclock
    assert bounds.handoff_reserve_s * 2 < wallclock, "strictly below half, per prop:spend-bound"


@pytest.mark.parametrize("wallclock", [270, 360, 990, 1440, 3600])
def test_every_shipped_ceiling_still_gets_the_full_nominal_reserve(wallclock: int) -> None:
    """The fix must not quietly shrink the catalogue's wind-down. 270 is the crossover."""
    assert default_handoff_reserve_s(wallclock) == NOMINAL_HANDOFF_RESERVE_S


def test_the_catalogue_is_entirely_above_the_crossover() -> None:
    """Asserted against the shipped manifests rather than against my memory of them."""
    from bounded_loops.application.manifest_bounds import _load_bounds

    catalogue = Path(__file__).resolve().parents[2] / "loops"
    ceilings = []
    for bounds_path in sorted(catalogue.glob("*/bounds.yaml")):
        bounds = _load_bounds(bounds_path, bounds_path.parent)
        ceilings.append(bounds.max_wallclock_s)
        assert bounds.handoff_reserve_s == NOMINAL_HANDOFF_RESERVE_S, bounds_path.parent.name
    assert len(ceilings) >= 60, f"only {len(ceilings)} manifests scanned; the glob is wrong"


def test_a_smaller_ceiling_gets_a_proportional_reserve_rather_than_a_refusal() -> None:
    assert default_handoff_reserve_s(120) == 40
    assert default_handoff_reserve_s(60) == 20
    assert default_handoff_reserve_s(30) == 10


def test_a_ceiling_too_small_for_any_reserve_still_loads() -> None:
    """Degenerate but declarable. Zero reserve means no wind-down, which is honest."""
    assert default_handoff_reserve_s(2) == 0
    assert default_handoff_reserve_s(1) == 0


# ── an authored reserve is still refused loudly ──────────────────────────────────


def test_an_authored_reserve_at_half_the_ceiling_is_refused(tmp_path: Path) -> None:
    loop = _write_loop(
        tmp_path, "max_iterations: 3\nmax_wallclock_s: 180\nhandoff_reserve_s: 90\n",
    )
    with pytest.raises(ManifestError, match="at least half of"):
        _load(loop)


def test_an_authored_reserve_below_half_is_honoured_exactly(tmp_path: Path) -> None:
    bounds = _load(_write_loop(
        tmp_path, "max_iterations: 3\nmax_wallclock_s: 180\nhandoff_reserve_s: 45\n",
    ))
    assert bounds.handoff_reserve_s == 45, "no clamping of a number the author chose"


def test_an_authored_zero_declines_the_wind_down(tmp_path: Path) -> None:
    bounds = _load(_write_loop(
        tmp_path, "max_iterations: 3\nmax_wallclock_s: 120\nhandoff_reserve_s: 0\n",
    ))
    assert bounds.handoff_reserve_s == 0
    assert _work_ceiling(bounds) == 120.0, "declining the courtesy returns the whole ceiling"


def test_the_refusal_names_a_number_the_author_wrote(tmp_path: Path) -> None:
    """The 0.6.5 error quoted 90 on manifests that never mentioned the field."""
    loop = _write_loop(
        tmp_path, "max_iterations: 3\nmax_wallclock_s: 100\nhandoff_reserve_s: 60\n",
    )
    with pytest.raises(ManifestError, match="handoff_reserve_s=60"):
        _load(loop)


# ── the partition holds, and the two paths agree ─────────────────────────────────


@pytest.mark.parametrize("wallclock", [10, 60, 120, 180, 360, 990, 3600])
def test_work_plus_reserve_equals_the_declared_ceiling(wallclock: int) -> None:
    """Equation (eq:reserve-partition), executed. The reserve partitions, never extends."""
    bounds = _bounds(wallclock=wallclock, reserve=default_handoff_reserve_s(wallclock))
    assert _work_ceiling(bounds) + effective_reserve_s(bounds) == float(wallclock)


def test_the_internal_clamp_still_protects_code_constructed_bounds() -> None:
    """`Bounds(...)` built in code keeps the forgiving path: degrade, never explode.

    The asymmetry with the manifest is deliberate — loud for authored input, forgiving
    for internals — and it is asserted here rather than only described in a docstring.
    """
    bounds = _bounds(wallclock=60)  # picks up the dataclass default of 90
    assert bounds.handoff_reserve_s == 90, "the field is not rewritten"
    assert effective_reserve_s(bounds) == 30.0, "but only half the ceiling is withheld"
    assert _work_ceiling(bounds) == 30.0
    assert _work_ceiling(bounds) + effective_reserve_s(bounds) == 60.0


def test_the_work_budget_is_never_less_than_half_the_ceiling_on_the_manifest_path(
    tmp_path: Path,
) -> None:
    """The failure this finding is named for: a 120 s ceiling yielding 60 s of work."""
    for wallclock in (60, 120, 180, 240, 360, 990):
        bounds = _load(_write_loop(
            tmp_path / f"w{wallclock}", f"max_iterations: 3\nmax_wallclock_s: {wallclock}\n",
        ))
        work = _work_ceiling(bounds)
        assert work > wallclock / 2, f"{wallclock}s ceiling left only {work}s for work"


def test_a_fresh_meter_reports_the_work_ceiling_not_the_declared_one() -> None:
    """What the runner clamps to. Confusing the two is how the reserve gets spent twice."""
    bounds = _bounds(wallclock=990, reserve=90)
    meter = BudgetMeter()

    work = meter.wallclock_budget(bounds)
    handoff = meter.wallclock_budget(bounds, for_handoff=True)
    assert work is not None and handoff is not None
    assert work.declared_s == 990, "the receipt always names the declared number"
    assert 890 < work.remaining_s <= 900
    assert 980 < handoff.remaining_s <= 990
    assert handoff.remaining_s - work.remaining_s == pytest.approx(90, abs=0.5)
