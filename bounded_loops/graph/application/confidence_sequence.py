"""Interval estimators for the mean of a bounded [0, 1] random variable. **Two, not one.**

| Function | Guarantee | Used by |
|---|---|---|
| ``anytime_valid_interval`` | valid simultaneously over ALL n | **everything that reports a number** |
| ``empirical_bernstein_interval`` | fixed-time only, no stitching term | tests, as the control |

Read that table before quoting either. The two differ by the stitching term, they differ by 12–23
measured coverage points, and only one of them survives the optional stopping a live run performs by
construction — an operator watching a run IS peeking after every observation.

The reported figures used the fixed-time function until this release, while the anytime one sat
implemented, tested, and called by nothing. That is worth stating rather than quietly correcting:
its tests were green the whole time, so the suite read as though the guarantee were in force. Green
tests on unreached code are the most convincing wrong signal a repository can emit.

The radius is taken from the empirical-Bernstein bound in:

    Waudby-Smith, I., & Ramdas, A. (2023). Estimating means of bounded random
    variables by betting. Journal of the Royal Statistical Society: Series B
    (Statistical Methodology), 85(1), 24–45. https://doi.org/10.1093/jrsssb/qkac013

Specifically: the closed-form empirical Bernstein CS bound from Section 5.1
(PrPl-EB) of that paper.  The running variance is the predictably-plugged-in
sum of squared deviations V̂_n = Σ_{t=1}^{n}(X_t − μ̂_{t−1})², initialised at
μ̂_0 = 1/2 (midpoint of [0, 1]).  The radius formula is:

    r_n = sqrt(2 · V̂_n · log(2/α) / n²) + log(2/α) / (3n)

where the 1/(3n) term comes from the Bennett inequality for [0, 1]-bounded
variables (range b − a = 1 enters as (b−a)/(3n) = 1/(3n)).

BACKGROUND ONLY -- WHAT WOULD MAKE THIS ANYTIME-VALID, WHICH THIS MODULE DOES NOT IMPLEMENT.
Read the ``empirical_bernstein_interval`` docstring for what is actually established. The theory below describes the
e-process a STITCHED boundary would invert; the closed-form radius above is the fixed-time bound and
does not carry the stitching term, so none of the simultaneous-over-all-n guarantee follows from it.
This paragraph sits first in the file and would be what a paper citation reads, which is why it says
so here rather than only in the function.

The e-process construction that WOULD make this anytime-valid is Theorem 1 of
Waudby-Smith & Ramdas (2023): the running product
K_n(μ) = Π_{t=1}^{n}(1 + λ_t(X_t − μ)) is a nonnegative e-process for any
fixed μ when the bets λ_t are predictable.  By Markov's inequality for
e-processes (Ville 1939), P(∃n : K_n(μ) ≥ 1/α) ≤ α.  The closed-form radius
r_n is an outer bound on the region where K_n(μ) < 1/α derived from the
Bernstein exponential inequality — see also Corollary 3 of Howard, Ramdas,
McAuliffe & Sekhon (2021), "Time-uniform, nonparametric, nonasymptotic
confidence sequences," Annals of Statistics 49(2), 1055–1080.

**Anytime-validity in plain English — the property a stitched boundary WOULD give, and that
this module does NOT deliver.** The guarantee would be that
P(∀ n ≥ 1 : μ ∈ C_n) ≥ 1 − α holds simultaneously for ALL sample sizes.
A reader who peeks after every observation WOULD not be fooled, and the
probability of ever excluding the true mean WOULD be bounded by α. None of that
is delivered here: without the stitching term this IS a fixed-sample bound, and
the boundary-crossing probability under optional stopping is NOT controlled by
the arithmetic in this module. What IS known is measured, not proven.

**Why this replaces Wilson in bounded-loops.** Wilson assumes independent
Bernoulli trials.  In bounded-loop runs, retries of a node share its worker,
its prompt, and its failure mode — a node that has been rejected twice may be
producing output shaped to pass the gate.  That positive within-sequence
correlation makes Wilson's interval systematically too narrow, and its measured
coverage was **77.5%** rather than the nominal 95% — simulated, at the parameters
pinned in ``tests/graph/application/test_confidence_sequence.py`` (n_runs=4000,
seq_len=30, logit-normal ρ=1.8, seed=42), checked at every sample size.

An earlier figure of **31–41%** appeared throughout this repository, attributed to
"real retry data". Both halves were wrong: it came from a separate simulation run by an
external reviewer during P1, whose parameters were never recorded, and the shipped
harness cannot reproduce it — Wilson measures 0.75–0.80 across ρ ∈ [1.0, 3.5], so
correlation strength does not account for the gap. It has been retired rather than
carried forward, because a comparison figure that the project's own test suite cannot
reproduce is exactly the number that must not reach a paper.

No runtime dependency beyond the standard library.
"""

from __future__ import annotations

import math
from typing import Sequence


def empirical_bernstein_interval(
    observations: Sequence[float],
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Return the (lower, upper) PrPl-EB confidence sequence at time n = len(observations).

    The interval C_n = [μ̂_n − r_n, μ̂_n + r_n], clipped to [0, 1].

    **What is established, and what is not.**

    MEASURED: under the correlated-retry structure this project's data actually has (a per-sequence
    latent propensity, then attempts drawn conditionally on it), checked after every observation,
    simulated coverage was **96.9%** against a nominal 95% — while Wilson on the same data reached
    **77.5%**.  ``tests/graph/application/test_confidence_sequence.py`` is that measurement.

    WHAT THE MEASUREMENT IS OF: coverage of the per-run LATENT rate ``p_run``, not of the marginal
    rate ``E[p_run]``. Two different estimands. The 19-point gap over Wilson is a statement about
    ``p_run`` and must not be quoted as an improvement in α coverage.

    AND THE NUMBER THAT MAKES THAT CONCRETE, because "different estimand" reads as a technicality
    until someone measures it. On the same 4000-run simulation, asking whether each interval contained
    the MARGINAL rate (α = 0.12) instead of that run's ``p_run``:

        coverage of p_run          0.9690
        coverage of marginal α     0.5850

    A 38-point MISS against the marginal, not a 19-point improvement. Found by the P4.5 round-2 audit
    (Grok 6) and reproduced here.

    **TWO SCOPE CORRECTIONS, both from #38, because this passage was over-read twice.**

    First: ``bl graph metrics`` no longer reports an interval from this function at all. It reports
    ``anytime_valid_interval``. Sentences here that used to say "the quantity the CLI labels" were
    describing a wiring that no longer exists.

    Second, and the sharper one: **0.5850 is the single-node corner**, not a general figure. Every
    sequence in this simulation carries ONE latent propensity — a run with one node. Pool several
    independent nodes, as the real product does, and this same estimator's marginal coverage rises
    to 0.8450 at six nodes and 0.9783 at thirty. Quoting 0.5850 as "the interval's marginal coverage"
    would be the same estimand error the paragraph above warns about, committed in the other
    direction. ``tests/graph/application/test_reported_interval_estimand.py`` measures the surface.

    HOW STRICT THE MEASUREMENT IS, measured rather than asserted. The suite perturbs the interval in
    two independent ways and requires coverage to fall below the 0.94 threshold for each: a 20%
    radius shrink (→ 0.4948) and a centre translation at four magnitudes (+0.03 → 0.7778, +0.05 →
    0.6863, −0.05 → 0.9030, +0.10 → 0.5188). So it is sensitive to both scale and location, and
    visibly less sensitive to a downward shift, which the clip at 0 and the skew of the logit-normal
    draw partly absorb.

    An earlier version of this docstring claimed the check "would not catch a wrong centre … a
    constant ``[0.48, 0.52]`` would pass at this parameter setting". That came from an external
    review and was accepted without being run. It is FALSE: a constant ``[0.48, 0.52]`` covers
    ``p_run`` in 1.9% of runs, because ``p_run`` is drawn around 0.12. The remaining honest gap is
    narrower than that claim — an ASYMMETRIC width bug, one tail wrong and the other right, preserves
    both centre and total width and neither perturbation would see it.

    NOT ESTABLISHED: that this is an anytime-valid confidence sequence.  The radius below is the
    fixed-time empirical-Bernstein form and carries **no stitching term** (no ``log log n``), so
    simultaneous validity over all ``n`` does not follow from it.  That gap is why
    ``anytime_valid_interval`` exists and why it, not this, is what the CLI now reports; this
    function survives as the control that makes the comparison measurable.  Its output must never be
    described as anytime-valid.

    The distinction matters because the whole point of replacing Wilson was optional stopping.  A
    measured 96.9% is real evidence and is better than a nominal interval whose coverage was 77.5%,
    but measured coverage at one parameter setting is not a theorem, and this docstring previously
    asserted the theorem.  Stating a guarantee the arithmetic does not deliver is the same class of
    error as printing "95% CI" beside a caveat.

    Parameters
    ----------
    observations:
        Sequence of values in [0, 1] in arrival order.  For a Bernoulli
        false-accept estimator each element is 1.0 (false accept) or 0.0
        (true accept).
    alpha:
        Significance level.  The default 0.05 gives a 95% CS.

    Returns
    -------
    (low, high): Both in [0, 1], with low ≤ high.

    Notes
    -----
    Waudby-Smith & Ramdas (2023), Section 5.1:

        V̂_n  = Σ_{t=1}^{n} (X_t − μ̂_{t−1})²   predictable running SOS
        μ̂_0 = 1/2                               midpoint prior for [0, 1]
        r_n  = sqrt(2 · V̂_n · log(2/α) / n²) + log(2/α) / (3n)
        C_n  = [μ̂_n − r_n, μ̂_n + r_n] ∩ [0, 1]
    """
    n = len(observations)
    if n == 0:
        return 0.0, 1.0

    log2a = math.log(2.0 / alpha)  # log(2/alpha)

    running_sum = 0.0
    running_sos = 0.0   # Σ_{t=1}^{n} (X_t − μ̂_{t−1})²
    mu_prev = 0.5       # μ̂_0 = midpoint prior for [0, 1]

    for t, x in enumerate(observations, start=1):
        running_sos += (x - mu_prev) ** 2
        running_sum += x
        mu_prev = running_sum / t   # μ̂_t for the next iteration

    mu_hat_n = running_sum / n

    # Radius: Waudby-Smith & Ramdas (2023), Section 5.1 closed-form bound
    radius = (
        math.sqrt(2.0 * running_sos * log2a / (n * n))
        + log2a / (3.0 * n)
    )

    return max(0.0, mu_hat_n - radius), min(1.0, mu_hat_n + radius)


# ── the anytime-valid one ────────────────────────────────────────────────────


def _psi_e(lam: float) -> float:
    """ψ_E(λ) = (−log(1−λ) − λ)/4, the empirical-Bernstein exponent (WSR 2023, §3.2)."""
    return (-math.log1p(-lam) - lam) / 4.0


def anytime_valid_interval(
    observations: Sequence[float],
    alpha: float = 0.05,
    *,
    bet_cap: float = 0.5,
) -> tuple[float, float]:
    """A genuine PrPl-EB confidence sequence: valid SIMULTANEOUSLY over every n.

    This is the function `empirical_bernstein_interval` is repeatedly careful to say it is not.
    That one is the closed-form fixed-time radius; peek after every observation and its error
    compounds, because nothing in it is uniform over time. This one is uniform, and the
    difference is not cosmetic — optional stopping is the entire reason a live monitor wants an
    interval at all. An operator watching a run IS peeking after every observation.

    **Construction.** For predictable bets λ_t the capital process

        K_n(μ) = Π_{t≤n} exp( λ_t(X_t − μ) − v_t ψ_E(λ_t) ),   v_t = 4(X_t − μ̂_{t−1})²

    is a non-negative supermartingale with K_0 = 1 when μ is the true mean. Ville's inequality
    (1939) then bounds P(∃n : K_n(μ) ≥ 1/α) ≤ α — over ALL n at once, which is where
    time-uniformity comes from, rather than from any choice of radius. Inverting
    `log K_n(μ) < log(1/α)` for a two-sided interval at level α gives

        C_n = { μ : |Σ λ_t(X_t − μ)| ≤ log(2/α) + Σ v_t ψ_E(λ_t) }

    which is a closed interval, computed below in one pass.

    **The bets.** λ_t is the predictable plug-in of Waudby-Smith & Ramdas (2023, §5.1):

        λ_t = min( bet_cap, sqrt( 2 log(2/α) / (σ̂²_{t−1} · t · log(1+t)) ) )

    Predictable means λ_t may use X_1..X_{t−1} but never X_t — that is what keeps K_n a
    supermartingale, and using X_t here would silently destroy the guarantee while still
    producing plausible-looking numbers. The `log(1+t)` is the stitching that the fixed-time
    radius omits; it is why this interval is wider, and the width IS the guarantee.

    `bet_cap` < 1 keeps `-log(1−λ)` finite. 1/2 is the value WSR use.

    **Coverage is measured, not asserted.** `tests/graph/application/test_confidence_sequence.py`
    simulates trajectories and counts how often the true mean is EVER excluded across all n —
    the quantity anytime-validity bounds. The same test applies that measure to the fixed-time
    interval, which fails it. A coverage check at a single fixed n cannot tell the two apart,
    which is how a fixed-time bound gets described as a sequence in the first place.

    **WHICH mean, though.** ``gate_metrics`` pools observations across nodes, and different nodes
    have different latent false-accept propensities, so the conditional mean is not constant along
    the pooled sequence. Measured on a simulation with that structure (nodes × attempts, logit-normal
    propensities), checking at every n:

        estimand                                          anytime    fixed-time
        μ̄_n, mean propensity of the observed attempts      1.0000        0.9590
        population MARGINAL rate, 1 node pooled            0.8333        0.6067
        population MARGINAL rate, 6 nodes pooled           0.9717        0.8450
        population MARGINAL rate, 30 nodes pooled          0.9983        0.9783
        population MARGINAL rate, ρ=3.5 (heterogeneous)    0.8267        0.6000

    So: **anytime-valid for μ̄_n**, the mean over the attempts actually in the log — which is the
    quantity an audit of a receipt stream is asking about. NOT a 95% interval for the population
    rate in general; that coverage climbs with the number of independent nodes pooled and falls with
    their heterogeneity. The 0.5850 marginal figure quoted elsewhere in this repository is the
    single-node corner of that surface and was stated as though it were the general case.

    The μ̄_n row is 1.0000 because the sequence is conservative there, not because the check is
    vacuous: shrinking the radius to 0.6× drops it to 0.14–0.35 and to 0.3× drops it to 0.00–0.10,
    and translating the centre by +0.10 drops it to 0.44–0.58. Both controls are in the suite.

    Returns (lower, upper), clipped to [0, 1]. An empty sequence returns the whole interval,
    which is the honest answer for no evidence.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if not 0.0 < bet_cap < 1.0:
        raise ValueError("bet_cap must be in (0, 1)")
    if not observations:
        return (0.0, 1.0)

    log2a = math.log(2.0 / alpha)

    running_sum = 0.0
    running_sos = 0.0    # Σ (X_i − μ̂_{i−1})², the plug-in variance numerator
    mu_prev = 0.5        # μ̂_0
    var_prev = 0.25      # σ̂²_0 — the maximal variance on [0, 1], so early bets stay small

    weighted_x = 0.0     # Σ λ_t X_t
    weight = 0.0         # Σ λ_t
    penalty = 0.0        # Σ v_t ψ_E(λ_t)

    for t, x in enumerate(observations, start=1):
        if not 0.0 <= x <= 1.0:
            raise ValueError(f"observation {x!r} is outside [0, 1]")

        # λ_t uses ONLY the past. Computed before x is folded in, deliberately.
        denominator = var_prev * t * math.log(1.0 + t)
        lam = bet_cap if denominator <= 0.0 else min(
            bet_cap, math.sqrt(2.0 * log2a / denominator)
        )

        weighted_x += lam * x
        weight += lam
        penalty += 4.0 * (x - mu_prev) ** 2 * _psi_e(lam)

        # Now update the plug-ins for the NEXT step.
        running_sos += (x - mu_prev) ** 2
        running_sum += x
        mu_prev = running_sum / t
        var_prev = (0.25 + running_sos) / (t + 1)

    if weight <= 0.0:
        return (0.0, 1.0)

    centre = weighted_x / weight
    radius = (log2a + penalty) / weight
    return max(0.0, centre - radius), min(1.0, centre + radius)
