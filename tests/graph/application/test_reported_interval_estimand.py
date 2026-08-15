"""WHICH interval does the product report, and WHAT does it cover?

Two questions, one file, because getting either wrong produces the same artefact: a coverage
percentage printed next to a quantity it is not the coverage of.

`test_confidence_sequence.py` measures the estimators in isolation, against a single latent
propensity per sequence. That is a run with exactly ONE node. Real runs pool attempts across many
nodes (`gate_metrics._fa_observations` sorts by node, repair round, attempt), and each node carries
its own propensity — so the conditional mean is not constant along the sequence the product actually
builds. Nothing measured that until #38.

What it turned up:

* the anytime-valid sequence covers **μ̄_n**, the mean propensity of the attempts in the log, at
  1.0000 everywhere measured — and that is the quantity an audit of a receipt stream asks about;
* it is **not** a 95% interval for the population marginal rate in general: coverage climbs with the
  number of independent nodes pooled (0.83 → 1.00) and falls with their heterogeneity;
* the 0.5850 marginal figure carried elsewhere in this repository is the single-node corner of that
  surface, stated as though it were the general case.
"""

from __future__ import annotations

import math
import random

import pytest

from bounded_loops.graph.application.confidence_sequence import (
    anytime_valid_interval,
    empirical_bernstein_interval,
)

_TRUE_ALPHA = 0.12
_CS_ALPHA = 0.05
_SEED = 42
#: Small enough to keep this file a few seconds, large enough that the three-sigma band on a 0.95
#: estimate is ~0.027 — so the 0.94 and 0.90 thresholds below discriminate rather than flap.
_N_RUNS = 400


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _pooled_coverage(
    interval_fn,
    *,
    nodes: int,
    attempts: int,
    rho: float,
    n_runs: int = _N_RUNS,
    shrink: float = 1.0,
    shift: float = 0.0,
) -> tuple[float, float]:
    """Ever-covered fractions for (μ̄_n, population marginal), checked at EVERY n.

    Mirrors the shape of the sequence ``gate_metrics`` builds: ``nodes`` independent latent
    propensities, ``attempts`` observations each, concatenated in node order.

    ``shrink`` and ``shift`` perturb the interval so the checks can be shown to discriminate. A
    coverage figure from a check that cannot fail is not a measurement.
    """
    rng = random.Random(_SEED)
    logit_mu = _logit(_TRUE_ALPHA)
    covered_mu_bar = 0
    covered_marginal = 0

    for _ in range(n_runs):
        propensities: list[float] = []
        observations: list[float] = []
        for _node in range(nodes):
            p = _sigmoid(rng.gauss(logit_mu, rho))
            for _attempt in range(attempts):
                propensities.append(p)
                observations.append(1.0 if rng.random() < p else 0.0)

        mu_bar_ok = True
        marginal_ok = True
        running = 0.0
        for n in range(1, len(observations) + 1):
            running += propensities[n - 1]
            mu_bar_n = running / n
            low, high = interval_fn(observations[:n], _CS_ALPHA)
            centre, radius = (low + high) / 2.0, (high - low) / 2.0
            centre += shift
            radius *= shrink
            if not (centre - radius) <= mu_bar_n <= (centre + radius):
                mu_bar_ok = False
            if not (centre - radius) <= _TRUE_ALPHA <= (centre + radius):
                marginal_ok = False
        covered_mu_bar += mu_bar_ok
        covered_marginal += marginal_ok

    return covered_mu_bar / n_runs, covered_marginal / n_runs


# ── what the reported interval covers ────────────────────────────────────────


@pytest.mark.parametrize(
    ("nodes", "attempts", "rho"),
    [(1, 30, 1.8), (6, 5, 1.8), (6, 5, 3.5), (30, 1, 1.8)],
)
def test_the_anytime_sequence_covers_the_LOG_MEAN_in_every_regime(
    nodes: int, attempts: int, rho: float
) -> None:
    """μ̄_n — the mean propensity of the attempts actually observed — is the honest estimand.

    This is what the CLI's ``for-log-mean`` label commits to, and it holds at every shape of pooled
    sequence measured, including the heterogeneous one where marginal coverage fails.
    """
    mu_bar, _marginal = _pooled_coverage(
        anytime_valid_interval, nodes=nodes, attempts=attempts, rho=rho
    )

    assert mu_bar >= 0.95, (
        f"anytime coverage of the log mean fell to {mu_bar:.4f} at {nodes} nodes x {attempts} "
        f"attempts, rho={rho}; the CLI label 'for-log-mean' is no longer supported"
    )


@pytest.mark.parametrize("shrink", [0.6, 0.3])
def test_the_log_mean_check_fails_when_the_interval_is_too_NARROW(shrink: float) -> None:
    """Without this, 1.0000 above is indistinguishable from a check that cannot fail.

    Measured at seed 42: 0.6x drops coverage to ~0.26, 0.3x to ~0.04.
    """
    mu_bar, _ = _pooled_coverage(
        anytime_valid_interval, nodes=6, attempts=5, rho=1.8, shrink=shrink
    )

    assert mu_bar < 0.90, (
        f"shrinking the radius to {shrink}x left coverage at {mu_bar:.4f}; the check is not "
        "sensitive to interval WIDTH and its unperturbed 1.0000 proves nothing"
    )


def test_the_log_mean_check_fails_when_the_interval_is_MIS_CENTRED() -> None:
    """Width and location are different bug classes. A shrink test alone catches only one.

    Measured at seed 42: a +0.10 translation drops coverage to ~0.46.
    """
    mu_bar, _ = _pooled_coverage(
        anytime_valid_interval, nodes=6, attempts=5, rho=1.8, shift=0.10
    )

    assert mu_bar < 0.90, (
        f"translating the centre by +0.10 left coverage at {mu_bar:.4f}; the check is not "
        "sensitive to interval LOCATION"
    )


# ── what it does NOT cover, and how that varies ──────────────────────────────


def test_marginal_coverage_climbs_with_the_number_of_INDEPENDENT_NODES_pooled() -> None:
    """The reason the repository's 0.5850 figure is a corner case rather than the general one.

    Total observations held constant at 30; only the count of independent latent draws varies. One
    node is the pathological end — the sequence carries a single propensity, so it says nothing
    about the population. Averaging across nodes pulls the pooled mean toward the marginal.
    """
    _, one_node = _pooled_coverage(anytime_valid_interval, nodes=1, attempts=30, rho=1.8)
    _, six_nodes = _pooled_coverage(anytime_valid_interval, nodes=6, attempts=5, rho=1.8)
    _, thirty_nodes = _pooled_coverage(anytime_valid_interval, nodes=30, attempts=1, rho=1.8)

    assert one_node < six_nodes < thirty_nodes, (
        f"marginal coverage is meant to be monotone in the number of nodes pooled; got "
        f"{one_node:.4f} / {six_nodes:.4f} / {thirty_nodes:.4f}"
    )
    assert one_node < 0.95, (
        f"at ONE node the interval covered the marginal rate {one_node:.4f} of the time — if this "
        "now clears 0.95, the claim 'not a 95% interval for the population rate' needs re-measuring"
    )
    assert thirty_nodes > 0.95


def test_marginal_coverage_FALLS_as_nodes_become_more_heterogeneous() -> None:
    """The failure direction that matters: more spread between nodes, worse marginal coverage.

    A caller pooling a handful of very unlike nodes gets an interval that is honest about that
    pooled set and misleading about the population. That is why the estimand is labelled.
    """
    _, gentle = _pooled_coverage(anytime_valid_interval, nodes=6, attempts=5, rho=0.5)
    _, harsh = _pooled_coverage(anytime_valid_interval, nodes=6, attempts=5, rho=3.5)

    assert harsh < gentle
    assert harsh < 0.95, (
        f"at rho=3.5 marginal coverage was {harsh:.4f}; the caveat says this regime fails and the "
        "caveat ships beside every number"
    )


def test_the_anytime_sequence_beats_the_fixed_time_radius_in_every_regime() -> None:
    """The measurement that justifies the switch, rather than the theory alone.

    If this ever stops holding, reporting the wider interval is no longer free and the choice has
    to be re-argued instead of assumed.
    """
    regimes = [(1, 30, 1.8), (6, 5, 1.8), (6, 5, 3.5), (30, 1, 1.8)]
    for nodes, attempts, rho in regimes:
        _, anytime = _pooled_coverage(
            anytime_valid_interval, nodes=nodes, attempts=attempts, rho=rho
        )
        _, fixed = _pooled_coverage(
            empirical_bernstein_interval, nodes=nodes, attempts=attempts, rho=rho
        )
        assert anytime > fixed, (
            f"at {nodes}x{attempts}, rho={rho}: anytime {anytime:.4f} did not beat fixed-time "
            f"{fixed:.4f}"
        )


# ── the wiring: which one does the product actually report? ──────────────────


def test_gate_metrics_REPORTS_the_anytime_sequence_not_the_fixed_time_radius() -> None:
    """The regression test for #38, and the one whose absence let the defect ship.

    ``anytime_valid_interval`` was implemented, tested, and called by NOTHING for three releases
    while ``_rate_cs`` used the fixed-time radius. Every test in the suite passed throughout,
    because each function was correct in isolation — the defect lived in which one the product
    reached for. Asserted behaviourally rather than by reading the import, so it survives a
    refactor that moves the call.
    """
    from bounded_loops.graph.application.gate_metrics import _rate_cs

    rng = random.Random(7)
    observations = [1.0 if rng.random() < 0.3 else 0.0 for _ in range(60)]

    rate = _rate_cs(observations)
    assert rate.interval is not None

    expected_low, expected_high = anytime_valid_interval(observations, alpha=0.05)
    fixed_low, fixed_high = empirical_bernstein_interval(observations, alpha=0.05)

    # The two must be distinguishable here, or the assertion below is vacuous.
    assert (expected_high - expected_low) > (fixed_high - fixed_low)

    assert rate.interval.low == pytest.approx(expected_low)
    assert rate.interval.high == pytest.approx(expected_high)


def test_the_reported_interval_is_wide_enough_to_be_the_stitched_one() -> None:
    """A second, independent grip on the same wiring.

    The test above pins exact endpoints and would pass if both functions were accidentally made
    identical. This one pins the PROPERTY that distinguishes a confidence sequence: it pays width
    for time-uniformity.
    """
    from bounded_loops.graph.application.gate_metrics import _rate_cs

    observations = [0.0] * 40 + [1.0] * 20
    rate = _rate_cs(observations)
    assert rate.interval is not None

    fixed_low, fixed_high = empirical_bernstein_interval(observations, alpha=0.05)

    assert (rate.interval.high - rate.interval.low) > (fixed_high - fixed_low), (
        "the reported interval is no wider than the fixed-time radius, so it cannot be carrying "
        "the stitching term"
    )
