"""Fixed-time empirical-Bernstein interval: arithmetic, and MEASURED coverage.

Not titled "anytime-valid confidence sequence" — that was the eighth surviving instance of a
claim this project disavows, and a title is what a reader quotes first.

Two categories:
1. Unit tests on the arithmetic (small, deterministic, no simulation).
2. A coverage simulation that measures empirical coverage under the correlated-retry
   structure that broke Wilson — the primary validation.

The coverage simulation is the deliverable that matters most, and what it measures is precise:
coverage of the per-run LATENT rate under the failure mode documented in gate_metrics.py's
independence caveat, with Wilson measured on identical data for comparison. It does NOT verify a 95%
guarantee for the marginal rate ``bl graph metrics`` prints — that estimand is measured separately
below and comes out at 0.5850, not 0.95.
"""

from __future__ import annotations

import math
import random

import pytest

from bounded_loops.graph.application.confidence_sequence import empirical_bernstein_interval


# ---------------------------------------------------------------------------
# Unit tests — arithmetic properties the formula must satisfy
# ---------------------------------------------------------------------------

class TestPrplEbArithmetic:
    def test_empty_observations_return_the_uninformative_interval(self) -> None:
        """No data means anything is possible."""
        assert empirical_bernstein_interval([]) == (0.0, 1.0)

    def test_single_observation_spans_almost_all_of_zero_one(self) -> None:
        """n=1 gives essentially no information; the interval must be very wide."""
        low, high = empirical_bernstein_interval([0.0], alpha=0.05)
        assert low == 0.0
        assert high > 0.9, f"n=1 should be nearly [0,1], got [{low:.4f}, {high:.4f}]"

    def test_interval_always_contained_in_zero_one(self) -> None:
        cases = [
            [0.0] * 50,
            [1.0] * 50,
            [0.5] * 50,
            [0.0, 1.0] * 25,
        ]
        for obs in cases:
            low, high = empirical_bernstein_interval(obs)
            assert 0.0 <= low <= high <= 1.0, f"interval escaped [0,1]: [{low}, {high}]"

    def test_more_observations_give_a_narrower_interval(self) -> None:
        """Adding observations should tighten the CS for iid data."""
        rng = random.Random(0)
        obs = [1.0 if rng.random() < 0.2 else 0.0 for _ in range(200)]
        lo10, hi10 = empirical_bernstein_interval(obs[:10])
        lo200, hi200 = empirical_bernstein_interval(obs[:200])
        assert (hi200 - lo200) < (hi10 - lo10), "more data should give a narrower CS"

    def test_zero_variance_observations_collapse_near_zero(self) -> None:
        """All 0s: the true mean is 0. With enough data the interval should be tight."""
        low, high = empirical_bernstein_interval([0.0] * 200)
        assert low == 0.0
        assert high < 0.15, f"200 zeros: expected tight upper bound, got {high:.4f}"

    def test_half_ones_center_straddles_half(self) -> None:
        """Alternating 0,1: mean = 0.5, interval should straddle 0.5."""
        obs = [0.0, 1.0] * 100
        low, high = empirical_bernstein_interval(obs)
        assert low < 0.5 < high

    def test_all_ones_upper_bound_is_one(self) -> None:
        low, high = empirical_bernstein_interval([1.0] * 50)
        assert high == 1.0
        assert low > 0.8

    @pytest.mark.parametrize("alpha", [0.01, 0.05, 0.10])
    def test_smaller_alpha_gives_wider_interval(self, alpha: float) -> None:
        """Smaller alpha (higher confidence) → wider interval."""
        obs = [0.0, 1.0] * 20
        lo_a, hi_a = empirical_bernstein_interval(obs, alpha=alpha)
        lo_b, hi_b = empirical_bernstein_interval(obs, alpha=0.50)
        assert (hi_a - lo_a) >= (hi_b - lo_b)


# ---------------------------------------------------------------------------
# Coverage simulation
#
# DESIGN RATIONALE
# ----------------
# The correlation model: per-sequence latent propensity drawn from a logit-
# normal distribution.  Each observation within a sequence is Bernoulli
# conditional on the latent propensity.  This captures the documented failure
# mode: retries of a node share its worker, prompt, and failure mode, so a
# node that has failed the gate twice may produce output specifically shaped to
# pass it — i.e. later attempts are POSITIVELY correlated with earlier ones
# because they share a latent per-sequence propensity.
#
# Optional stopping: coverage is checked at every t from 1 to seq_len. A run
# is "always covered" only if the CS contains the true p at EVERY t.  This is
# the harshest test and is exactly the event whose probability PrPl-EB bounds.
#
# True parameter: p_run (the per-run latent propensity), not the marginal mean.
# The CS estimates the conditional mean E[X_t | run] = p_run.
#
# TOLERANCES (justified below)
# ---
# With n_runs=4000 and true coverage=0.95, the std of the estimate is
# sqrt(0.95*0.05/4000) ≈ 0.0034.  Three-sigma below 0.95 ≈ 0.940.
# The threshold 0.94 therefore rejects any implementation whose true coverage
# is ≤ ~0.93 with high confidence while almost never rejecting a correct one.
# ---------------------------------------------------------------------------

_SEED = 42
_N_RUNS = 4000
_SEQ_LEN = 30
_TRUE_ALPHA = 0.12   # nominal false-accept rate (marginal)
_RHO = 1.8           # logit-normal spread — controls within-run correlation strength
_CS_ALPHA = 0.05     # 95% CS nominal level


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    """Wilson score interval at 95%.  Inline duplicate of gate_metrics._wilson
    to keep the simulation self-contained and avoid importing private names."""
    if trials == 0:
        return 0.0, 1.0
    z = 1.959963984540054
    phat = successes / trials
    denom = 1.0 + z * z / trials
    centre = (phat + z * z / (2 * trials)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials)) / denom
    low = max(0.0, centre - margin)
    high = min(1.0, centre + margin)
    return low, high


def _simulate_coverage(
    n_runs: int,
    seq_len: int,
    true_alpha: float,
    rho: float,
    cs_alpha: float,
    seed: int,
    perturb_factor: float = 1.0,
    perturb_shift: float = 0.0,
) -> tuple[float, float]:
    """Measure empirical coverage of PrPl-EB and Wilson under the correlated-retry model.

    Parameters
    ----------
    perturb_factor:
        Multiply the PrPl-EB interval half-width by this factor.
        1.0 = unperturbed.  0.8 = 20% narrower (used for perturbation check).
    perturb_shift:
        Translate the whole interval by this amount, preserving its width. A scale
        perturbation and a location perturbation are different bug classes: shrinking
        catches an under-wide radius, shifting catches a wrong centre (a mis-signed
        term, a plug-in mean computed over the wrong prefix). The P4.5 audit noted the
        single shrink test could not distinguish them, so both are measured.

    Returns
    -------
    (prpl_eb_coverage, wilson_coverage): fraction of runs where the interval
    contained the true p_run at EVERY t from 1 to seq_len (optional stopping).
    """
    rng = random.Random(seed)
    logit_mu = _logit(true_alpha)

    prpl_covered = 0
    wilson_covered = 0

    for _ in range(n_runs):
        # Draw per-run latent propensity from logit-normal distribution.
        # High rho → high between-run variance → strong positive within-run
        # correlation (all observations in a run share p_run).
        logit_p = rng.gauss(logit_mu, rho)
        p_run = _sigmoid(logit_p)

        # Generate observations iid Bernoulli(p_run) — conditional iid, jointly correlated
        observations = [1.0 if rng.random() < p_run else 0.0 for _ in range(seq_len)]

        prpl_always = True
        wilson_always = True

        for t in range(1, seq_len + 1):
            prefix = observations[:t]

            # PrPl-EB at time t
            lo_cs, hi_cs = empirical_bernstein_interval(prefix, alpha=cs_alpha)
            if perturb_factor != 1.0 or perturb_shift != 0.0:
                mid = (lo_cs + hi_cs) / 2.0 + perturb_shift
                half_w = (hi_cs - lo_cs) / 2.0 * perturb_factor
                lo_cs = max(0.0, mid - half_w)
                hi_cs = min(1.0, mid + half_w)
            if not (lo_cs <= p_run <= hi_cs):
                prpl_always = False

            # Wilson at time t
            n_fa = int(sum(prefix))
            wi_lo, wi_hi = _wilson_interval(n_fa, t)
            if not (wi_lo <= p_run <= wi_hi):
                wilson_always = False

        if prpl_always:
            prpl_covered += 1
        if wilson_always:
            wilson_covered += 1

    return prpl_covered / n_runs, wilson_covered / n_runs


# Module-level cache: run the simulation once, share across tests in the class.
_COVERAGE_RESULT: tuple[float, float] | None = None


def _get_coverage() -> tuple[float, float]:
    global _COVERAGE_RESULT
    if _COVERAGE_RESULT is None:
        _COVERAGE_RESULT = _simulate_coverage(
            n_runs=_N_RUNS,
            seq_len=_SEQ_LEN,
            true_alpha=_TRUE_ALPHA,
            rho=_RHO,
            cs_alpha=_CS_ALPHA,
            seed=_SEED,
            perturb_factor=1.0,
        )
    return _COVERAGE_RESULT


class TestCoverageSimulation:
    """Empirical coverage under correlated-retry optional-stopping structure.

    The simulation uses a latent logit-normal propensity (rho=1.8) to model
    the positive within-sequence correlation documented in gate_metrics.py.
    Coverage is checked at every t from 1 to seq_len (optional stopping).
    """

    def test_prpl_eb_achieves_at_least_94_percent_coverage(self) -> None:
        """The primary guarantee: PrPl-EB covers the true per-run rate at every
        optional-stopping point with probability >= 1 - alpha = 0.95.

        Threshold 0.94 allows for honest Monte Carlo error:
        std(estimate) = sqrt(0.95*0.05/4000) ≈ 0.0034; three-sigma below 0.95 ≈ 0.940.
        A correct 95% CS will essentially never fall below this threshold at seed=42.
        """
        prpl_cov, _ = _get_coverage()
        assert prpl_cov >= 0.94, (
            f"PrPl-EB coverage {prpl_cov:.4f} is below 0.94 — the implementation is wrong. "
            f"(n_runs={_N_RUNS}, seq_len={_SEQ_LEN}, rho={_RHO}, seed={_SEED})"
        )

    def test_wilson_coverage_is_substantially_below_95_percent(self) -> None:
        """Wilson under optional stopping + correlated retries must be well below 95%.

        Measured here: **0.7752** at seed=42. Stable in ρ — 0.7528 / 0.7752 / 0.7823 / 0.7883 /
        0.7990 at ρ = 1.0 / 1.8 / 2.5 / 3.0 / 3.5 — so the threshold is set at 0.85, which leaves
        headroom over the whole range while still failing loudly if Wilson ever approaches nominal
        (which would mean the simulation had stopped modelling the correlated-retry failure).

        This replaced a `< 0.90` floor justified by "the ledger reported 31-41%". That earlier figure
        came from a separate P1 simulation whose parameters were never recorded and which this
        harness cannot reproduce at any ρ. It has been retired everywhere in favour of the number
        this seeded test actually produces (P4.5 audit round, chased down from Muse F10).
        """
        _, wilson_cov = _get_coverage()
        assert wilson_cov < 0.85, (
            f"Wilson coverage {wilson_cov:.4f} is not substantially below 0.90. "
            "The simulation may not be modelling the correlated-retry failure correctly."
        )

    def test_prpl_eb_dominates_wilson_by_at_least_10_percentage_points(self) -> None:
        """The improvement must be concrete.  10 pp is a conservative threshold."""
        prpl_cov, wilson_cov = _get_coverage()
        gap = prpl_cov - wilson_cov
        assert gap >= 0.10, (
            f"PrPl-EB ({prpl_cov:.4f}) does not dominate Wilson ({wilson_cov:.4f}) by 10pp. "
            f"Gap = {gap:.4f}."
        )

    @pytest.mark.parametrize("shift", [0.03, 0.05, -0.05, 0.10])
    def test_perturbation_check_a_shifted_centre_must_fail_coverage(self, shift: float) -> None:
        """PERTURBATION TEST — location, not just scale.

        The shrink test below catches an interval that is too NARROW. It cannot distinguish a
        correct radius around a wrong centre — a mis-signed term, or a plug-in mean taken over
        the wrong prefix — because shrinking and shifting are different bug classes. The P4.5
        audit made exactly that objection: one scale perturbation does not license the claim
        that the suite "catches a wrong implementation".

        Measured at seed=42 (n_runs=4000, width preserved, centre translated):
        ``+0.03 → 0.7778``, ``+0.05 → 0.6863``, ``-0.05 → 0.9030``, ``+0.10 → 0.5188``,
        against ``0.9690`` unperturbed. So the simulation IS sensitive to location, and it is
        markedly less sensitive downward — a shift toward zero is partly absorbed by the clip at
        0 and by the skew of the logit-normal draw. Both directions are asserted so that
        asymmetry stays visible rather than becoming a blind spot.

        Still NOT covered by either perturbation: an asymmetric width bug (one tail correct,
        the other wrong) which preserves both centre and total width.
        """
        perturbed_cov, _ = _simulate_coverage(
            n_runs=_N_RUNS,
            seq_len=_SEQ_LEN,
            true_alpha=_TRUE_ALPHA,
            rho=_RHO,
            cs_alpha=_CS_ALPHA,
            seed=_SEED,
            perturb_shift=shift,
        )
        assert perturbed_cov < 0.94, (
            f"A centre shift of {shift:+.2f} still yields coverage {perturbed_cov:.4f} >= 0.94. "
            "The coverage test cannot tell a correct interval from a mis-centred one."
        )

    def test_perturbation_check_20_percent_narrower_must_fail_coverage(self) -> None:
        """PERTURBATION TEST — scale.

        Shrinking the interval half-width by 20% (perturb_factor=0.8) must cause PrPl-EB
        coverage to fall below 0.94.  If the test still passes, the assertion threshold
        is too lenient and would accept a broken estimator.

        Measured at seed=42: coverage drops to 0.4948 from 0.9690 unperturbed.
        """
        perturbed_cov, _ = _simulate_coverage(
            n_runs=_N_RUNS,
            seq_len=_SEQ_LEN,
            true_alpha=_TRUE_ALPHA,
            rho=_RHO,
            cs_alpha=_CS_ALPHA,
            seed=_SEED,
            perturb_factor=0.8,
        )
        assert perturbed_cov < 0.94, (
            f"Perturbed coverage {perturbed_cov:.4f} is still >= 0.94. "
            "The test is too weak — a 20% shrink of the interval should clearly fail."
        )

    def test_coverage_numbers_visible_in_log(self) -> None:
        """Emits the actual coverage numbers for both methods (visible with pytest -s)."""
        prpl_cov, wilson_cov = _get_coverage()
        assert 0.0 <= prpl_cov <= 1.0
        assert 0.0 <= wilson_cov <= 1.0
        print(
            f"\n[Coverage simulation results]\n"
            f"  PrPl-EB : {prpl_cov:.4f}\n"
            f"  Wilson  : {wilson_cov:.4f}\n"
            f"  Gap     : {prpl_cov - wilson_cov:+.4f}\n"
            f"  Params  : n_runs={_N_RUNS}, seq_len={_SEQ_LEN}, rho={_RHO}, "
            f"true_alpha={_TRUE_ALPHA}, cs_alpha={_CS_ALPHA}, seed={_SEED}"
        )


def test_the_interval_is_NOT_a_95_percent_interval_for_the_MARGINAL_rate() -> None:
    """The number a paper would misquote, measured so it cannot be.

    Everything above measures coverage of ``p_run``, the per-run latent rate. The quantity
    ``bl graph metrics`` prints as the false-accept rate is the MARGINAL rate ``E[p_run]``. Asking the
    same intervals whether they contain the marginal rate gives **0.5850** — so for the printed
    quantity this is a 58.5% interval, a 38-point miss rather than a 19-point improvement over
    Wilson. Found by the P4.5 round-2 audit (Grok 6).

    This test exists so the distinction is a measured number in the suite rather than a caveat in
    prose, and so anyone tempted to put 96.9% next to α in a paper trips over it first.
    """
    rng = random.Random(_SEED)
    logit_mu = _logit(_TRUE_ALPHA)
    covered_marginal = 0
    for _ in range(_N_RUNS):
        p_run = _sigmoid(rng.gauss(logit_mu, _RHO))
        observations = [1.0 if rng.random() < p_run else 0.0 for _ in range(_SEQ_LEN)]
        if all(
            lo <= _TRUE_ALPHA <= hi
            for lo, hi in (
                empirical_bernstein_interval(observations[:t], alpha=_CS_ALPHA) for t in range(1, _SEQ_LEN + 1)
            )
        ):
            covered_marginal += 1
    marginal_coverage = covered_marginal / _N_RUNS

    assert 0.55 < marginal_coverage < 0.62, (
        f"marginal coverage {marginal_coverage:.4f} moved; the 0.5850 figure quoted in "
        "confidence_sequence.py and the CHANGELOG must be re-measured"
    )
    prpl_cov, _wilson = _get_coverage()
    assert prpl_cov - marginal_coverage > 0.30, (
        "the gap between latent and marginal coverage is the whole point of the caveat"
    )


# ── anytime-validity: the property the fixed-time interval does NOT have ─────


def _ever_excluded(interval_fn, *, alpha: float, true_mean: float, trials: int,
                   horizon: int, seed: int) -> float:
    """Fraction of trajectories where `true_mean` leaves the interval at ANY checked n.

    This is the quantity anytime-validity bounds, and the only measure that separates a
    confidence sequence from a fixed-time interval: check at one n and both look fine.
    """
    import random

    rng = random.Random(seed)
    checkpoints = list(range(1, 41)) + list(range(50, horizon + 1, 10))
    misses = 0
    for _ in range(trials):
        draws = [1.0 if rng.random() < true_mean else 0.0 for _ in range(horizon)]
        for n in checkpoints:
            low, high = interval_fn(draws[:n], alpha)
            if not low <= true_mean <= high:
                misses += 1
                break
    return misses / trials


def test_the_anytime_interval_bounds_the_EVER_excluded_rate() -> None:
    """P(∃n : μ ∉ C_n) ≤ α — the guarantee that makes peeking safe.

    An operator watching a live run is peeking after every observation. That is the entire
    reason this exists, and it is exactly what a fixed-time radius does not survive.
    """
    from bounded_loops.graph.application.confidence_sequence import anytime_valid_interval

    rate = _ever_excluded(
        anytime_valid_interval,
        alpha=0.10, true_mean=0.30, trials=200, horizon=300, seed=20260815,
    )

    assert rate <= 0.10, f"the confidence sequence excluded the true mean in {rate:.1%} of runs"


def test_the_FIXED_TIME_interval_does_not_survive_the_same_check() -> None:
    """The control, and the reason the two functions both exist.

    Without this the test above proves nothing: an interval of [0, 1] would also pass it. This
    shows the measure is sharp enough to fail a construction that is genuinely not anytime-valid
    — the one this repo shipped while its docstring said so.
    """
    from bounded_loops.graph.application.confidence_sequence import (
        empirical_bernstein_interval,
    )

    rate = _ever_excluded(
        empirical_bernstein_interval,
        alpha=0.10, true_mean=0.30, trials=200, horizon=300, seed=20260815,
    )

    assert rate > 0.10, (
        f"the fixed-time interval held the anytime bound ({rate:.1%}); either it was replaced "
        "by a confidence sequence or this check has stopped discriminating"
    )


def test_the_anytime_interval_is_WIDER_than_the_fixed_time_one() -> None:
    """The width is the guarantee. A confidence sequence narrower than the fixed-time bound at
    the same n would be getting time-uniformity for free, which is not on offer."""
    import random

    from bounded_loops.graph.application.confidence_sequence import (
        anytime_valid_interval,
        empirical_bernstein_interval,
    )

    rng = random.Random(11)
    draws = [1.0 if rng.random() < 0.3 else 0.0 for _ in range(200)]

    anytime_low, anytime_high = anytime_valid_interval(draws, 0.05)
    fixed_low, fixed_high = empirical_bernstein_interval(draws, 0.05)

    assert (anytime_high - anytime_low) > (fixed_high - fixed_low)


def test_the_bets_never_look_at_the_observation_they_weight() -> None:
    """Predictability is what makes the capital process a supermartingale.

    A λ_t computed from X_t still produces plausible numbers and silently voids the guarantee,
    so this is asserted at the source rather than inferred from output that would look correct
    either way.
    """
    import inspect

    from bounded_loops.graph.application import confidence_sequence

    source = inspect.getsource(confidence_sequence.anytime_valid_interval)
    body = source.split("for t, x in enumerate", 1)[1]
    before_use, _, _ = body.partition("weighted_x += lam * x")

    assert "lam = " in before_use, "λ_t is no longer computed before the observation is used"
    assert "var_prev" in before_use and "x)" not in before_use.split("lam =", 1)[1].split("\n")[0]


def test_no_observations_yields_the_whole_interval() -> None:
    """No evidence must read as no information, not as a narrow interval around a default."""
    from bounded_loops.graph.application.confidence_sequence import anytime_valid_interval

    assert anytime_valid_interval([], 0.05) == (0.0, 1.0)
