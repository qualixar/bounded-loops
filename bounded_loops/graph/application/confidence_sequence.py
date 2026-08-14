"""Fixed-time empirical-Bernstein interval for a bounded [0, 1] random variable.

NOT anytime-valid — see the ``empirical_bernstein_interval`` docstring. The title said "Anytime-valid confidence
sequence" and the first paragraph said "Implements ... PrPl-EB", both of which overstate what the
arithmetic below delivers: this is the CLOSED-FORM FIXED-TIME bound, without the stitching term a
confidence sequence needs.

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
    rate ``E[p_run]`` that ``bl graph metrics`` reports as the false-accept rate. Two different
    estimands. The 19-point gap over Wilson is a statement about ``p_run`` and must not be quoted as
    an improvement in α coverage.

    AND THE NUMBER THAT MAKES THAT CONCRETE, because "different estimand" reads as a technicality
    until someone measures it. On the same 4000-run simulation, asking whether each interval contained
    the MARGINAL rate (α = 0.12) instead of that run's ``p_run``:

        coverage of p_run          0.9690
        coverage of marginal α     0.5850

    So the interval is a 58.5% interval for the quantity the CLI labels, not a 95% one — a 38-point
    MISS, not a 19-point improvement. Found by the P4.5 round-2 audit (Grok 6) and reproduced here.
    A paper that wants a coverage number for the printed false-accept rate must measure THAT
    estimand and publish THAT number; 96.9% is not it.

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
    simultaneous validity over all ``n`` does not follow from it.  A genuine PrPl-EB confidence
    sequence needs either a stitched boundary (Howard et al. 2021) or numerical inversion of the
    e-process.  Until that lands, this function's output must NOT be described as anytime-valid, and
    the CLI label says ``emp-Bernstein 95% (COVERAGE-MEASURED)`` for exactly that reason.

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
