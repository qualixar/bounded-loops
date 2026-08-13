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
in twelve attempts", which is compatible with a true rate near 25%. Every rate carries a Wilson score
interval. Wilson rather than the textbook Wald interval because Wald collapses to [0, 0] at zero
observed events, asserting certainty from an absence of evidence, which is precisely the error this
module exists to avoid. Wilson needs no dependency beyond ``math``.

**And the interval's own assumption is violated — stated, because it changes how the number reads.**
Wilson assumes independent Bernoulli trials. Attempts in one run are not independent: retries of a
node share its worker, prompt and failure mode. Correlation makes an iid interval too NARROW, so
every bound here is a LOWER bound on the true uncertainty. That is why ``confusion_by_attempt`` is
the primary figure rather than the pooled one: within a single attempt index there is at most one
observation per node, which is much closer to the assumption than the pooled set is. A properly
cluster-aware interval is P5 work (``confseq``, MIT, gives anytime-valid confidence sequences); until
then ``INDEPENDENCE_CAVEAT`` travels with every reported interval, including through the CLI.

Nothing here reads a store, a clock, or an adapter: receipts in, numbers out. That makes every figure
reproducible from an archived log by anyone, which is the property a reviewer will want.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

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


#: Printed next to every interval, and not optional. A confidence interval whose assumptions are not
#: stated beside it is the artefact most likely to be misquoted — and this one's central assumption is
#: violated by the very data it summarises.
INDEPENDENCE_CAVEAT = (
    "Wilson intervals assume independent Bernoulli trials. Attempts in one run are NOT independent: "
    "retries of a node share its worker, its prompt and its failure mode, and nodes in a graph share "
    "a worker. Correlated observations make an iid interval too NARROW, so treat these bounds as a "
    "LOWER bound on the true uncertainty. The per-attempt-index slices are the more defensible "
    "figures — within attempt index k there is at most one observation per node."
)


@dataclass(frozen=True)
class Interval:
    """A two-sided Wilson score interval for a proportion.

    **Read ``INDEPENDENCE_CAVEAT`` before quoting one of these.** The interval is computed as though
    each gated attempt were an independent Bernoulli trial. They are not, and the direction of the
    error is the unflattering one: correlation makes the interval too narrow, so this understates
    uncertainty rather than overstating it.

    It is reported anyway, with the caveat attached, because the alternative — a bare point estimate —
    understates uncertainty *infinitely*. A too-narrow interval that says so beats no interval.
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


def _labels(receipts: Sequence[StoredGraphEvent]) -> dict[tuple[str, int], str]:
    """Latest ground-truth label per ``(node_id, attempt)``.

    Labels are append-only and may be superseded, so the LAST one wins — the disagreement history
    stays in the log as evidence, while the measurement uses the current conclusion.
    """
    out: dict[tuple[str, int], str] = {}
    for stored in receipts:
        if stored.event.event_type != _LABEL_EVENT:
            continue
        payload = stored.event.payload
        node_id, attempt, label = payload.get("node_id"), payload.get("attempt"), payload.get("label")
        if isinstance(node_id, str) and isinstance(attempt, int) and isinstance(label, str):
            out[(node_id, attempt)] = label
    return out


def _gate_verdicts(receipts: Sequence[StoredGraphEvent]) -> dict[tuple[str, int], bool]:
    """Per attempt: did the GATE pass it? Attempts the gate never saw are absent.

    An attempt that failed before the gate ran — a worker crash, a policy denial, an unverified
    artifact — must not appear in a gate's denominator. Counting those against the gate would credit
    it for catching failures it never evaluated, which flatters α by exactly the amount the worker
    happened to be broken. ``NodeFailureCause.GATE_REJECTED`` is the only cause that means the gate
    looked and said no.
    """
    out: dict[tuple[str, int], bool] = {}
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
                out[(node_id, attempt)] = True
        elif event_type in (_ATTEMPT_FAILED, _FAILED):
            if payload.get("cause") == NodeFailureCause.GATE_REJECTED.value:
                out[(node_id, attempt)] = False
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
    by_index: dict[int, dict[tuple[str, int], bool]] = {}
    for key, passed in verdicts.items():
        by_index.setdefault(key[1], {})[key] = passed
    return {
        index: _confusion_from(slice_verdicts, labels)
        for index, slice_verdicts in sorted(by_index.items())
    }


def _confusion_from(
    verdicts: Mapping[tuple[str, int], bool], labels: Mapping[tuple[str, int], str],
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
