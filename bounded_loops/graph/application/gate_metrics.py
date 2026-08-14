"""Measuring how well an independent gate actually performed, from the receipt stream alone.

This is the module the paper's numbers come out of, so its first duty is to refuse to produce a
number it cannot support.

The gate's verdict is an opinion; ``node.outcome.labeled`` (P2) records what was actually true. Put
side by side they give a confusion matrix, and from it the two quantities that matter:

* **α, the false-accept rate** — how often the gate passed output that was wrong. The quantity the
  whole apparatus exists to bound, reported here **by attempt index**, because a retry loop
  multiplies the per-attempt rate and the interesting question is whether α degrades as attempts
  climb.
* **The false-REJECT rate** — how often the gate blocked work that was correct. A gate that cuts
  false accepts by rejecting everything has made things worse, and this curve is routinely absent
  from published gate evaluations. Reporting it is most of this module's value.

**What it will not do.** Every rate comes back as ``None`` when the labelled sample is too small to
support it, and the unlabelled count is always reported alongside. A false-accept rate computed from
four labels out of five hundred attempts would be the most misleading number in the paper — worse
than no number, because it looks like evidence. That is the same discipline P2-B's spend accounting
settled on: unmeasurable is not zero, and the honest output of an unmeasurable question is a refusal.

**Intervals, not point estimates.** A rate of 0/12 is not "zero" — it is "no false accept observed
in twelve attempts", which is compatible with a true rate near 25%. So every rate carries an
interval, never a bare estimate: a point estimate understates uncertainty infinitely.

**Which interval, and what is actually known about it.** The reported figures use the FIXED-TIME
empirical-Bernstein radius (``confidence_sequence.empirical_bernstein_interval``). It is NOT an
anytime-valid confidence sequence — the radius carries no stitching term — and what is known about it
is MEASURED, not proven: 96.9% coverage of a per-run latent rate under a simulated correlated-retry
regime, against 77.5% for Wilson on the same data. Coverage of the MARGINAL rate these functions
return is a different estimand and measures 0.5850 on that simulation, so the interval must not be
described as a 95% interval for the rate printed beside it. ``INDEPENDENCE_CAVEAT`` travels with
every number, including through the CLI and the JSON.

The Wilson score interval it replaced is still computed and still emitted under
``interval_method: "wilson_uncalibrated"``, for continuity and for comparison. Wilson assumes
independent Bernoulli trials; attempts in one run are not independent, because retries of a node
share its worker, prompt and failure mode, and correlation makes an iid interval too NARROW. That is
also why ``confusion_by_attempt`` remains the most interpretable figure: within a single attempt index
there is at most one observation per node. A genuinely cluster-aware, anytime-valid interval is P5
work (``confseq`` was verified UNUSABLE on py>=3.11).

Nothing here reads a store, a clock, or an adapter: receipts in, numbers out. That makes every figure
reproducible from an archived log by anyone, which is the property a reviewer will want.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from bounded_loops.graph.application.confidence_sequence import empirical_bernstein_interval
from bounded_loops.graph.domain.events import NodeFailureCause, StoredGraphEvent

#: Below this many labelled attempts, a rate is reported as ``None`` rather than computed. Ten is
#: not a statistical threshold — no threshold makes a small sample informative — it is a floor
#: chosen so that a number, once printed, is at least not absurd. The interval is what carries the
#: real uncertainty; this only stops the most embarrassing kind of claim.
MINIMUM_LABELLED_FOR_A_RATE = 10

#: Published baseline to compare blocked precision against: an advisory-mode gate whose blocks were
#: correct 0.39% of the time — the number that made that team demote their gate out of the enforcing
#: path. arXiv:2605.17998. Carried as a constant so the comparison is explicit in code rather than
#: asserted in prose.
ADVISORY_BLOCKED_PRECISION_BASELINE = 0.0039

_LABEL_EVENT = "node.outcome.labeled"
_ATTEMPT_FAILED = "node.attempt.failed"
_SUCCEEDED = "node.succeeded"
_FAILED = "node.failed"


#: Printed next to every interval, and not optional. The interval method and its validity conditions
#: must travel with the number — the artefact most likely to be misquoted is a CI without its context.
INDEPENDENCE_CAVEAT = (
    "The reported interval is empirical-Bernstein with a predictable plug-in. Its coverage under "
    "correlated retries with optional stopping was MEASURED at 96.9% by simulation, against 77.5% "
    "for Wilson on the same data. It is NOT yet an anytime-valid confidence sequence: the radius "
    "carries no stitching term, so simultaneous validity over all n is unproven here. "
    # The four sentences deleted here asserted that "the 95% guarantee holds simultaneously for all
    # sample sizes and under optional stopping" — the exact claim the sentence above denies. They
    # were vestigial Wilson-era text left behind when the relabelling prepended the honest wording
    # instead of replacing the paragraph. This string prints beside EVERY number and ships as JSON
    # `independence_caveat`, so a quoter could splice the retracted half on its own and cite a
    # guarantee the code disavows two sentences earlier.
    "The MEASURED figure is coverage of the per-run latent rate under a simulated correlated-retry "
    "regime, which is a DIFFERENT ESTIMAND from the marginal false-accept rate reported above — so "
    "it must not be quoted as 'alpha coverage'. This replaces the Wilson interval, whose measured coverage "
    "under the same simulated regime was 77.5% (not 95%). The per-attempt-index slices remain the most "
    "interpretable figures — within attempt index k there is at most one observation per node."
)


@dataclass(frozen=True)
class Interval:
    """A two-sided interval for a proportion. The METHOD is not carried here — see the caller.

    Deliberately method-agnostic: ``*_cs`` functions fill it from the empirical-Bernstein radius,
    ``Confusion.*_rate`` from Wilson, and the CLI labels which is which
    (``interval_method``). This type used to say "a two-sided Wilson score interval", which stopped
    being true when the empirical-Bernstein interval became the reported figure — a stale name on the
    type every number flows through.

    **Read ``INDEPENDENCE_CAVEAT`` before quoting one of these.** Neither method is an anytime-valid
    confidence sequence here, and for Wilson the independence assumption is violated in the
    unflattering direction: correlation makes an iid interval too narrow, so it understates
    uncertainty rather than overstating it.
    """

    low: float
    high: float


@dataclass(frozen=True)
class Rate:
    """An observed proportion with its interval, or an honest refusal.

    ``value is None`` means the sample was too small. ``numerator`` and ``denominator`` are always
    populated so a reader can see exactly what was and was not observed — a refusal that hides its
    own counts cannot be argued with.
    """

    numerator: int
    denominator: int
    value: float | None
    interval: Interval | None

    @property
    def reportable(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Confusion:
    """Gate verdict against ground truth, for one slice of attempts.

    ``unlabelled`` is a first-class member, not a footnote. Every rate below is computed over the
    labelled attempts only, so the unlabelled count is the reader's measure of how much of the run
    the numbers actually cover.
    """

    true_accept: int = 0
    false_accept: int = 0
    true_reject: int = 0
    false_reject: int = 0
    unknown_label: int = 0
    unlabelled: int = 0

    @property
    def accepted(self) -> int:
        return self.true_accept + self.false_accept

    @property
    def rejected(self) -> int:
        return self.true_reject + self.false_reject

    @property
    def labelled(self) -> int:
        return self.accepted + self.rejected

    def false_accept_rate(self) -> Rate:
        """α — of the attempts the gate PASSED, how many were actually wrong."""
        return _rate(self.false_accept, self.accepted)

    def false_reject_rate(self) -> Rate:
        """Of the attempts the gate BLOCKED, how many were actually correct.

        The cost of gating. A gate can drive α to zero by rejecting everything, and only this number
        makes that visible.
        """
        return _rate(self.false_reject, self.rejected)

    def blocked_precision(self) -> Rate:
        """Of the attempts the gate blocked, how many deserved it.

        Compare against ``ADVISORY_BLOCKED_PRECISION_BASELINE``. A gate whose blocks are almost never
        justified is a gate that should be advisory, whatever its α looks like.
        """
        return _rate(self.true_reject, self.rejected)


def naive_compounded_false_accept(alpha: float, attempts: int) -> float:
    """``1 - (1 - α)^m`` — the chance at least one false accept slips through ``m`` attempts.

    **This is a baseline to FALSIFY, not a budget recommender.** It is elementary probability, not a
    result borrowed from anyone: if each attempt were an independent draw with false-accept
    probability α, a retry budget of m would compound the risk exactly this way. Deriving a "safe"
    max_attempts from it would be the single most dangerous use of this module.

    It earns its place because it is the model the measured data should be tested AGAINST. Every
    reason the ``INDEPENDENCE_CAVEAT`` gives — retries share a worker, a prompt, a failure mode —
    predicts that reality departs from this curve, and probably in the unsafe direction: a worker that
    failed the gate twice may be converging on output shaped to pass it, which makes later attempts
    *more* likely to be false accepts, not independently likely. Publishing the gap between this curve
    and ``confusion_by_attempt`` is the interesting result; publishing this curve alone would be
    presenting an assumption as a finding.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha must be a probability")
    if attempts < 0:
        raise ValueError("attempts cannot be negative")
    return 1.0 - (1.0 - alpha) ** attempts


def _wilson(successes: int, trials: int, z: float = 1.959963984540054) -> Interval:
    """Wilson score interval at 95%.

    Chosen over Wald because Wald gives [0, 0] for 0/12 — a claim of certainty derived from having
    seen nothing. Wilson keeps the interval inside [0, 1] and stays sane at the boundaries, which is
    where a gate evaluation actually lives: the interesting runs have very few false accepts.
    """
    if successes < 0 or trials < 0 or successes > trials:
        # Impossible counts mean a caller invariant broke — today ``_rate`` is always called with a
        # numerator drawn from its own denominator, so this cannot fire through the public API. It
        # raises rather than returning [0, 1] because a silent uninformative interval would let a
        # counting bug reach a results table looking like an honest absence of evidence. Found by
        # probing ``_wilson(3, 2)``, which used to surface as a bare ``math domain error``.
        raise ValueError(f"cannot form an interval from {successes} successes in {trials} trials")
    if trials == 0:
        return Interval(0.0, 1.0)
    phat = successes / trials
    denominator = 1 + z * z / trials
    centre = (phat + z * z / (2 * trials)) / denominator
    margin = (
        z * math.sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials))
    ) / denominator
    # Snapped at the boundaries: the arithmetic leaves 2.8e-17 where zero is meant, and a lower
    # bound of 2.8e-17 printed in a results table reads as a bug rather than as zero.
    low, high = centre - margin, centre + margin
    return Interval(0.0 if low < 1e-12 else low, 1.0 if high > 1 - 1e-12 else high)


def _rate(numerator: int, denominator: int) -> Rate:
    if denominator < MINIMUM_LABELLED_FOR_A_RATE:
        # Counts still returned. The refusal is about the RATE, not about the evidence: a reader
        # needs to see 1-of-3 to judge that 33% would have been a meaningless figure.
        return Rate(numerator, denominator, None, None)
    return Rate(numerator, denominator, numerator / denominator, _wilson(numerator, denominator))


def _labels(receipts: Sequence[StoredGraphEvent]) -> dict[tuple[str, int, int], str]:
    """Latest ground-truth label per ``(node_id, repair_round, attempt)``.

    Labels are append-only and may be superseded, so the LAST one wins — the disagreement history
    stays in the log as evidence, while the measurement uses the current conclusion.
    """
    out: dict[tuple[str, int, int], str] = {}
    for stored in receipts:
        if stored.event.event_type != _LABEL_EVENT:
            continue
        payload = stored.event.payload
        node_id, attempt, label = payload.get("node_id"), payload.get("attempt"), payload.get("label")
        if isinstance(node_id, str) and isinstance(attempt, int) and isinstance(label, str):
            out[(node_id, _repair_round_of(payload), attempt)] = label
    return out


def _gate_verdicts(receipts: Sequence[StoredGraphEvent]) -> dict[tuple[str, int, int], bool]:
    """Per attempt: did the GATE pass it? Attempts the gate never saw are absent.

    An attempt that failed before the gate ran — a worker crash, a policy denial, an unverified
    artifact — must not appear in a gate's denominator. Counting those against the gate would credit
    it for catching failures it never evaluated, which flatters α by exactly the amount the worker
    happened to be broken. ``NodeFailureCause.GATE_REJECTED`` is the only cause that means the gate
    looked and said no.
    """
    out: dict[tuple[str, int, int], bool] = {}
    for stored in receipts:
        event_type = stored.event.event_type
        payload = stored.event.payload
        node_id, attempt = payload.get("node_id"), payload.get("attempt")
        if not isinstance(node_id, str) or not isinstance(attempt, int):
            continue
        if event_type == _SUCCEEDED:
            # Only when a VERDICT is present. An approval node writes ``node.succeeded`` with no
            # verdict — a human decided and the gate never ran — and counting that as a gate pass
            # credits the gate for a judgement it did not make. Same reasoning as excluding worker
            # faults from the denominator; found by the P4 audit.
            if "verdict" in payload:
                out[(node_id, _repair_round_of(payload), attempt)] = True
        elif event_type in (_ATTEMPT_FAILED, _FAILED):
            if payload.get("cause") == NodeFailureCause.GATE_REJECTED.value:
                out[(node_id, _repair_round_of(payload), attempt)] = False
    return out


def confusion(receipts: Sequence[StoredGraphEvent]) -> Confusion:
    """One confusion matrix over every gated attempt in the stream."""
    return _confusion_from(_gate_verdicts(receipts), _labels(receipts))


def confusion_by_attempt(receipts: Sequence[StoredGraphEvent]) -> Mapping[int, Confusion]:
    """A confusion matrix per attempt INDEX — the α-by-attempt curve.

    The headline measurement. A bounded loop's guarantee is a per-attempt gate error rate compounded
    over the retry budget, so a single pooled α hides the thing worth knowing: whether the gate gets
    worse as attempts climb. It plausibly does — later attempts are the hard cases, and a worker that
    has already failed twice may be producing output specifically shaped to get past the gate.
    """
    verdicts = _gate_verdicts(receipts)
    labels = _labels(receipts)
    # key is (node_id, repair_round, attempt) — slice on the ATTEMPT, which is index 2. It was
    # index 1 before the round joined the key, and leaving it there would have silently sliced the
    # alpha-drift curve by repair round instead of by attempt index: the headline measurement,
    # computed over the wrong axis. Caught while closing Muse finding 2.
    by_index: dict[int, dict[tuple[str, int, int], bool]] = {}
    for key, passed in verdicts.items():
        by_index.setdefault(key[2], {})[key] = passed
    return {
        index: _confusion_from(slice_verdicts, labels)
        for index, slice_verdicts in sorted(by_index.items())
    }


def _confusion_from(
    verdicts: Mapping[tuple[str, int, int], bool],
    labels: Mapping[tuple[str, int, int], str],
) -> Confusion:
    counts = {
        "true_accept": 0, "false_accept": 0, "true_reject": 0, "false_reject": 0,
        "unknown_label": 0, "unlabelled": 0,
    }
    for key, passed in verdicts.items():
        label = labels.get(key)
        if label is None:
            counts["unlabelled"] += 1
        elif label == "unknown":
            counts["unknown_label"] += 1
        elif label == "correct":
            counts["true_accept" if passed else "false_reject"] += 1
        elif label == "incorrect":
            counts["false_accept" if passed else "true_reject"] += 1
        else:
            # An unrecognised label is not silently treated as unlabelled: a label this version
            # cannot read is a schema change, and pooling it would move counts between cells.
            counts["unknown_label"] += 1
    return Confusion(**counts)


def _repair_round_of(payload: Mapping[str, object]) -> int:
    """Which repair round a receipt belongs to. Absent means round 0 — every pre-repair receipt.

    A repair re-runs a node from attempt 1, so ``(node, attempt)`` REPEATS across rounds. Keying gate
    verdicts and labels on that pair alone let a second round silently overwrite the first: a run that
    the gate rejected twice counted as one rejection, and the published false-accept rate was computed
    over the wrong denominator. Found by the P4.25 dual audit (Muse finding 2) — every other
    repair-aware reader already keys on the round, and this one did not.
    """
    declared = payload.get("repair_round")
    if isinstance(declared, bool) or not isinstance(declared, int):
        return 0
    return declared


# ---------------------------------------------------------------------------
# Anytime-valid CS functions — use these instead of Confusion.false_accept_rate()
# when the ordered sequence of observations is available from the receipt stream.
# ---------------------------------------------------------------------------

def _fa_observations(
    verdicts: Mapping[tuple[str, int, int], bool],
    labels: Mapping[tuple[str, int, int], str],
) -> list[float]:
    """Ordered 0/1 observations for the false-accept estimator.

    1.0 = false accept (gate passed, output was incorrect).
    0.0 = true accept  (gate passed, output was correct).
    Skipped: blocked attempts (not in denominator), unlabelled, unknown.
    Sorted by (node_id, repair_round, attempt) for a deterministic, reproducible order.
    """
    result: list[float] = []
    for key in sorted(verdicts):
        if not verdicts[key]:
            continue  # gate rejected — not in false-accept denominator
        label = labels.get(key)
        if label == "incorrect":
            result.append(1.0)
        elif label == "correct":
            result.append(0.0)
        # unlabelled / unknown / unrecognised: skip, consistent with _confusion_from
    return result


def _fr_observations(
    verdicts: Mapping[tuple[str, int, int], bool],
    labels: Mapping[tuple[str, int, int], str],
) -> list[float]:
    """Ordered 0/1 observations for the false-reject estimator.

    1.0 = false reject (gate blocked, output was correct).
    0.0 = true reject  (gate blocked, output was incorrect).
    """
    result: list[float] = []
    for key in sorted(verdicts):
        if verdicts[key]:
            continue  # gate passed — not in false-reject denominator
        label = labels.get(key)
        if label == "correct":
            result.append(1.0)
        elif label == "incorrect":
            result.append(0.0)
    return result


def _bp_observations(
    verdicts: Mapping[tuple[str, int, int], bool],
    labels: Mapping[tuple[str, int, int], str],
) -> list[float]:
    """Ordered 0/1 observations for the blocked-precision estimator.

    1.0 = correct block (gate blocked, output was incorrect — deserved it).
    0.0 = wrong block   (gate blocked, output was correct — did not deserve it).
    """
    result: list[float] = []
    for key in sorted(verdicts):
        if verdicts[key]:
            continue  # gate passed — not in blocked denominator
        label = labels.get(key)
        if label == "incorrect":
            result.append(1.0)
        elif label == "correct":
            result.append(0.0)
    return result


def _rate_cs(observations: list[float]) -> Rate:
    """Build a Rate using the empirical-Bernstein interval instead of Wilson."""
    n = len(observations)
    numerator = int(sum(observations))
    if n < MINIMUM_LABELLED_FOR_A_RATE:
        return Rate(numerator, n, None, None)
    mu = numerator / n
    low, high = empirical_bernstein_interval(observations, alpha=0.05)
    return Rate(numerator, n, mu, Interval(low, high))


def false_accept_rate_cs(receipts: Sequence[StoredGraphEvent]) -> Rate:
    """α — false-accept rate with an empirical-Bernstein interval (coverage measured, not proven).

    Replaces the Wilson-based ``Confusion.false_accept_rate()``, whose independence assumption the
    retry data violates. The radius is the FIXED-TIME empirical-Bernstein form and carries no
    stitching term, so this is **not** a confidence sequence and simultaneous validity over all
    sample sizes does not follow from it. What is known is measured: 96.9% coverage of the per-run
    latent rate under the simulated correlated-retry regime, against Wilson's 77.5%. Coverage of the
    MARGINAL rate this function returns is a separate estimand that those figures do not measure.

    An earlier version of this docstring said "the CS is valid under optional stopping and without
    the independence assumption" — a theorem this module's own ``INDEPENDENCE_CAVEAT`` disavows
    twelve lines above, and the eighth surviving instance of that claim to be found. It is the kind
    of sentence a paper quotes.
    """
    verdicts = _gate_verdicts(receipts)
    labels = _labels(receipts)
    return _rate_cs(_fa_observations(verdicts, labels))


def false_reject_rate_cs(receipts: Sequence[StoredGraphEvent]) -> Rate:
    """False-reject rate with an empirical-Bernstein interval (coverage measured, not proven)."""
    verdicts = _gate_verdicts(receipts)
    labels = _labels(receipts)
    return _rate_cs(_fr_observations(verdicts, labels))


def blocked_precision_cs(receipts: Sequence[StoredGraphEvent]) -> Rate:
    """Blocked precision with an empirical-Bernstein interval (coverage measured, not proven)."""
    verdicts = _gate_verdicts(receipts)
    labels = _labels(receipts)
    return _rate_cs(_bp_observations(verdicts, labels))


def _fa_observations_by_attempt(
    verdicts: Mapping[tuple[str, int, int], bool],
    labels: Mapping[tuple[str, int, int], str],
) -> dict[int, list[float]]:
    """Per attempt-index 0/1 false-accept observations, in sorted key order."""
    by_attempt: dict[int, list[float]] = {}
    for key in sorted(verdicts):
        attempt_idx = key[2]
        if not verdicts[key]:
            continue
        label = labels.get(key)
        if label == "incorrect":
            by_attempt.setdefault(attempt_idx, []).append(1.0)
        elif label == "correct":
            by_attempt.setdefault(attempt_idx, []).append(0.0)
    return by_attempt


def false_accept_rate_cs_by_attempt(
    receipts: Sequence[StoredGraphEvent],
) -> dict[int, Rate]:
    """PrPl-EB false-accept rate per attempt index.

    Keys match those returned by ``confusion_by_attempt`` but only for attempt
    indices that have at least one labelled accepted attempt.
    """
    verdicts = _gate_verdicts(receipts)
    labels = _labels(receipts)
    return {
        idx: _rate_cs(obs)
        for idx, obs in sorted(_fa_observations_by_attempt(verdicts, labels).items())
    }
