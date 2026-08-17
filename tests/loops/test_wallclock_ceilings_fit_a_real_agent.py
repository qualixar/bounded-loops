"""Every shipped loop's wallclock ceiling must be survivable by a real agent provider.

WHAT THIS CAUGHT
----------------
Enforcing `max_wallclock_s` inside an attempt immediately exposed that the number itself was wrong
almost everywhere: 65 of 69 shipped loops declared a 60s total budget against `max_iterations: 10`,
which is **6 seconds per attempt**. The shipped demo worker runs in about 0.4s a lap, so nothing ever
noticed. A real agent CLI takes far longer, and the measurements below are from four of them running
four loops in the cross-model convergence experiment, over two independent runs:

    n=31 completed turns   min 20.1s   median 48.3s   p75 72.5s   p90 145.3s   max 270.6s

**The p75 came out at 72.5s in both runs independently.** The tail moved substantially between
them — the slowest turn went from 177.6s to 270.6s, and one cell nearly doubled at 1.86x — while
the body of the distribution did not move. That is the justification for sizing from the p75 rather
than the maximum: the typical turn is a stable quantity across runs and the tail demonstrably is
not, so a ceiling derived from the tail would be derived from noise.

So the previous ceilings could not accommodate a single real turn, let alone ten. While the bound was
unenforced that was invisible; once enforced it would have halted every real-agent run mid-first
attempt. Two defects, one of them hiding the other — which is the argument for enforcing a declared
bound even when nothing appears to be wrong.

THE RULE, AND WHY THE CEILING IS ALLOWED TO FIRE
------------------------------------------------
    max_wallclock_s >= max_iterations * PER_ATTEMPT_ALLOWANCE_S + handoff_reserve_s

The reserve term is there because `handoff_reserve_s` is taken OUT of the ceiling, not added to it.
A ceiling sized for the work alone would silently spend the last 90 seconds of the work budget on
the wind-down turn, so the last declared attempt would be the one that never got to run.

`PER_ATTEMPT_ALLOWANCE_S` is 90: comfortably above the p75 turn, deliberately below the slowest
observed one. That last part is a choice, not an oversight. A run whose every turn is as slow as the
worst measured turn will halt before reaching its lap cap, and that is the ceiling doing its job —
it is a spend limit, and a spend limit that can never bind is decorative. Sizing it to the *maximum*
observed turn would make it exactly redundant with `max_iterations` times the runner's own
per-attempt timeout, i.e. a bound that is arithmetically incapable of firing.

The complementary guard on one hung turn is the runner's own `timeout_s` (300s by default), which is
a different limit with a different job. See adapters/runners/attempt_deadline.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

LOOPS_ROOT = Path(__file__).resolve().parents[2] / "loops"

#: Seconds of budget each declared attempt must be allowed. Derived from measurement, not taste —
#: see the module docstring for the sample.
PER_ATTEMPT_ALLOWANCE_S = 90

#: Slowest single real-agent turn observed, over both runs. A ceiling below this cannot complete
#: that turn, which is acceptable for a spend limit but must be a conscious choice, so it is named
#: here. Raising it requires a new measurement and a note saying where the sample came from.
SLOWEST_OBSERVED_TURN_S = 270.6


def _manifests() -> list[Path]:
    found = sorted(LOOPS_ROOT.glob("*/bounds.yaml"))
    assert len(found) >= 50, (
        f"only {len(found)} loop bounds files found under {LOOPS_ROOT}; this test silently passing "
        "on an empty catalogue is the vacuity it exists to prevent"
    )
    return found


@pytest.mark.parametrize("manifest", _manifests(), ids=lambda p: p.parent.name)
def test_the_ceiling_leaves_every_declared_attempt_a_real_agents_worth_of_time(
    manifest: Path,
) -> None:
    spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    iterations = spec.get("max_iterations")
    ceiling = spec.get("max_wallclock_s")

    assert isinstance(iterations, int) and iterations >= 1, (
        f"{manifest.parent.name}: max_iterations is {iterations!r}"
    )
    # A null ceiling is permitted by the schema and means "no wallclock bound". It is not permitted
    # in the shipped catalogue: every shipped loop should demonstrate a complete bound set, and a
    # reader comparing loops should not have to wonder whether a missing number is a decision.
    assert isinstance(ceiling, int), (
        f"{manifest.parent.name}: max_wallclock_s is {ceiling!r}; every shipped loop declares one"
    )

    reserve = spec.get("handoff_reserve_s", 90)
    assert isinstance(reserve, int) and reserve >= 0, (
        f"{manifest.parent.name}: handoff_reserve_s is {reserve!r}"
    )

    required = iterations * PER_ATTEMPT_ALLOWANCE_S + reserve
    work_budget = ceiling - reserve
    assert ceiling >= required, (
        f"{manifest.parent.name}: max_wallclock_s={ceiling} minus a handoff_reserve_s={reserve} "
        f"leaves {work_budget}s of work budget, i.e. {work_budget / iterations:.1f}s per attempt "
        f"across max_iterations={iterations}. A real agent turn takes "
        f"{PER_ATTEMPT_ALLOWANCE_S}s at the 75th percentile, so this ceiling would halt a healthy "
        f"run part-way through an attempt. Set it to at least {required}."
    )


def test_the_allowance_is_below_the_slowest_measured_turn_so_the_bound_can_still_fire() -> None:
    """Guards the rule itself against being loosened until it means nothing.

    Raising `PER_ATTEMPT_ALLOWANCE_S` past the slowest turn ever measured would make every ceiling
    large enough that `max_iterations` always fires first — a bound present in the manifest and
    incapable of affecting a run, which is the exact defect class this release closes.
    """
    assert PER_ATTEMPT_ALLOWANCE_S < SLOWEST_OBSERVED_TURN_S, (
        f"a per-attempt allowance of {PER_ATTEMPT_ALLOWANCE_S}s exceeds the slowest turn ever "
        f"measured ({SLOWEST_OBSERVED_TURN_S}s), so no shipped wallclock ceiling could ever bind "
        "before the lap cap. If real providers genuinely got slower, replace the measurement and "
        "say where the new sample came from — do not raise the allowance to make a red test green."
    )
