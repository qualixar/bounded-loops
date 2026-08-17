"""Parsing and validation of `bounds.yaml`, split out of `manifest.py`.

Extracted when `manifest.py` crossed this project's 800-line cap. Bounds parsing is the natural seam:
it is the one section that reads a separate file, and everything it needs from the rest of the module
is three small validators it imports back.

The cap is not a style preference here. `test_layering.py` records why six modules had crossed it
before: each was already the biggest file in its package, which made it the most convenient place to
add the next thing, and nobody ever decided to write an 900-line module.
"""

from __future__ import annotations

from pathlib import Path

from bounded_loops.application._manifest_validate import (
    _load_yaml_mapping,
    _positive_int,
    _reject_unknown_keys,
    _resolve_contained,
    _strict_bool,
)
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Bounds

MAX_ITERATIONS_CEILING = 1000

#: Wind-down reserve for a loop that does not declare one. Enough for one measured
#: turn (p75 = 72.5 s over n = 31), because a reserve too small to finish a summary
#: buys nothing but a second timeout.
NOMINAL_HANDOFF_RESERVE_S = 90


def default_handoff_reserve_s(max_wallclock_s: int) -> int:
    """The reserve to withhold when the author declared none.

    Proportional below the point where the nominal reserve would dominate, so that
    adding this feature could never invalidate a ceiling that was legal before it
    existed. A third is the bound rather than a half because `prop:spend-bound`
    requires `r < W/2` strictly, and a defaulted value sitting exactly on the
    boundary would make the manifest check fire on a number nobody chose.

    Every ceiling at or above 270 s — which is all 69 shipped loops — gets the
    nominal 90 s, so this changes no declared bound in the catalogue.
    """
    return min(NOMINAL_HANDOFF_RESERVE_S, max_wallclock_s // 3)

_BOUNDS_KEYS = frozenset({
    "max_iterations", "no_progress_window", "max_tokens", "max_wallclock_s",
    "sandbox", "quarantine_inputs", "schema", "trace", "require_approval",
    "handoff_reserve_s",
})


def _load_bounds(bounds_path: Path, loop_dir: Path) -> Bounds:
    if not bounds_path.exists():
        raise ManifestError(f"bounds.yaml not found: {bounds_path}")
    raw = _load_yaml_mapping(bounds_path, "bounds.yaml")
    _reject_unknown_keys(raw, _BOUNDS_KEYS, "bounds")
    if "max_iterations" not in raw:
        raise ManifestError(f"bounds.yaml: max_iterations is required ({bounds_path})")
    max_iter = _positive_int(raw["max_iterations"], "max_iterations")
    assert max_iter is not None
    # Security fix: an unbounded max_iterations + null max_wallclock_s
    # + null max_tokens is an effectively-unbounded-cost loop. Cap it.
    if max_iter > MAX_ITERATIONS_CEILING:
        raise ManifestError(
            f"max_iterations={max_iter} exceeds the {MAX_ITERATIONS_CEILING} "
            f"ceiling, which is hard and non-overridable in v1 — no CLI flag "
            f"exists to raise it. Split the loop or lower max_iterations."
        )
    no_progress_window = _positive_int(raw.get("no_progress_window", 3), "no_progress_window")
    assert no_progress_window is not None
    max_tokens = _positive_int(raw.get("max_tokens"), "max_tokens", allow_none=True)
    max_wallclock_s = _positive_int(raw.get("max_wallclock_s"), "max_wallclock_s", allow_none=True)
    # Security fix: null wallclock does NOT mean "unlimited" — it
    # means "use the conservative platform default" (1 hour). A loop that
    # genuinely needs longer must say so explicitly in bounds.yaml.
    if max_wallclock_s is None:
        max_wallclock_s = 3600
    # Reserved OUT OF max_wallclock_s for the wind-down turn, never added to it. 0 declines it.
    authored_reserve = raw.get("handoff_reserve_s")
    handoff_reserve_s = _positive_int(
        default_handoff_reserve_s(max_wallclock_s) if authored_reserve is None
        else authored_reserve,
        "handoff_reserve_s", allow_zero=True,
    )
    assert handoff_reserve_s is not None
    # A reserve at or past half the ceiling starves the work it is supposed to be summarising, and
    # the manifest is the right place to be told so — at load time, not by a run that gets one
    # attempt and then writes a handoff about having had no time to do anything.
    #
    # This can now only fire on a number the author actually wrote. Until 0.6.6 the default of 90
    # was substituted first, so `max_wallclock_s: 180` or lower was refused outright — with an error
    # quoting a field the author had never written, on a manifest that had been valid before the
    # reserve existed. A courtesy the operator did not ask for must not invalidate their ceiling.
    if handoff_reserve_s * 2 >= max_wallclock_s:
        raise ManifestError(
            f"handoff_reserve_s={handoff_reserve_s} is at least half of "
            f"max_wallclock_s={max_wallclock_s}, leaving "
            f"{max_wallclock_s - handoff_reserve_s}s for the work itself. The reserve is taken OUT "
            f"of the ceiling, not added to it — raise max_wallclock_s or lower the reserve."
        )
    sandbox = _strict_bool(raw.get("sandbox", True), "sandbox")
    quarantine_inputs = _strict_bool(raw.get("quarantine_inputs", True), "quarantine_inputs")
    trace = _strict_bool(raw.get("trace", True), "trace")
    require_approval = _strict_bool(raw.get("require_approval"), "require_approval", allow_none=True)
    assert sandbox is not None
    assert quarantine_inputs is not None
    assert trace is not None
    schema = raw.get("schema")
    if schema is not None:
        if not isinstance(schema, str) or not schema.strip():
            raise ManifestError("schema must be a non-empty string or null")
        _resolve_contained(loop_dir, schema, "bounds.schema")
    return Bounds(
        max_iterations=max_iter,
        no_progress_window=no_progress_window,
        max_tokens=max_tokens,
        max_wallclock_s=max_wallclock_s,
        sandbox=sandbox,
        quarantine_inputs=quarantine_inputs,
        schema=schema,
        trace=trace,
        require_approval=require_approval,
        handoff_reserve_s=handoff_reserve_s,
    )
