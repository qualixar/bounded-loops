"""What the evaluation harness reports, and — mostly — what it refuses to report.

These are the numbers that would go in a paper, so the tests that matter most here are the ones
asserting that a number is WITHHELD. A false-accept rate computed from four labels is worse than no
number at all, because it looks like evidence.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.gate_metrics import (
    ADVISORY_BLOCKED_PRECISION_BASELINE,
    MINIMUM_LABELLED_FOR_A_RATE,
    Confusion,
    confusion,
    confusion_by_attempt,
)
from bounded_loops.graph.domain.events import (
    GraphRunIdentity,
    NodeFailureCause,
    StoredGraphEvent,
    UnsignedGraphEvent,
)

_DIGEST = "sha256:" + "a" * 64


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity("org", "project", "run", _DIGEST, _DIGEST, _DIGEST)


def _event(sequence: int, event_type: str, payload: dict[str, object]) -> StoredGraphEvent:
    return StoredGraphEvent(
        identity=_identity(),
        sequence=sequence,
        event=UnsignedGraphEvent(
            event_id=f"e{sequence}", idempotency_key=f"k{sequence}", event_type=event_type,
            timestamp="2026-01-01T00:00:00Z", actor="test", payload=payload,
        ),
        previous_hash="0" * 64,
        event_hash="f" * 64,
    )


def _stream(*specs: tuple[str, int, bool | None, str | None]) -> list[StoredGraphEvent]:
    """``(node_id, attempt, gate_passed | None, label | None)`` → a receipt stream.

    ``gate_passed=None`` means the attempt failed BEFORE the gate — a worker fault — which must not
    appear in any gate denominator.
    """
    out: list[StoredGraphEvent] = []
    sequence = 0
    for node_id, attempt, passed, label in specs:
        sequence += 1
        if passed is True:
            out.append(_event(sequence, "node.succeeded", {"node_id": node_id, "attempt": attempt}))
        elif passed is False:
            out.append(_event(sequence, "node.attempt.failed", {
                "node_id": node_id, "attempt": attempt,
                "cause": NodeFailureCause.GATE_REJECTED.value,
            }))
        else:
            out.append(_event(sequence, "node.attempt.failed", {
                "node_id": node_id, "attempt": attempt,
                "cause": NodeFailureCause.WORKER_FAULT.value,
            }))
        if label is not None:
            sequence += 1
            out.append(_event(sequence, "node.outcome.labeled", {
                "node_id": node_id, "attempt": attempt, "label": label,
                "artifact_digest": _DIGEST, "labeller": "reviewer",
            }))
    return out


class TestTheConfusionMatrix:
    def test_the_four_cells_are_counted_from_verdict_against_ground_truth(self) -> None:
        result = confusion(_stream(
            ("a", 1, True, "correct"),      # true accept
            ("b", 1, True, "incorrect"),    # FALSE ACCEPT — the quantity that matters
            ("c", 1, False, "incorrect"),   # true reject
            ("d", 1, False, "correct"),     # FALSE REJECT — the cost of gating
        ))

        assert (result.true_accept, result.false_accept) == (1, 1)
        assert (result.true_reject, result.false_reject) == (1, 1)

    def test_an_attempt_that_never_reached_the_gate_is_not_in_the_gates_denominator(self) -> None:
        """A worker crash is not a gate decision.

        Counting it would credit the gate for catching a failure it never evaluated — flattering α by
        exactly however broken the worker happened to be. Only ``GATE_REJECTED`` means the gate looked.
        """
        result = confusion(_stream(
            ("a", 1, None, "incorrect"),    # worker fault, labelled — still not the gate's
            ("b", 1, True, "correct"),
        ))

        assert result.labelled == 1
        assert result.rejected == 0

    def test_an_unlabelled_attempt_is_counted_as_unlabelled_not_dropped(self) -> None:
        """The unlabelled count is how a reader judges how much of the run the rates cover."""
        result = confusion(_stream(("a", 1, True, None), ("b", 1, True, "correct")))

        assert result.unlabelled == 1
        assert result.labelled == 1

    def test_an_explicitly_unknown_label_is_kept_separate_from_unlabelled(self) -> None:
        """Reviewed-and-undecidable is not the same fact as never-reviewed, and pooling them would
        overstate how much of the run went unexamined."""
        result = confusion(_stream(("a", 1, True, "unknown"), ("b", 1, True, None)))

        assert (result.unknown_label, result.unlabelled) == (1, 1)

    def test_a_label_this_version_cannot_read_does_not_move_counts_between_cells(self) -> None:
        result = confusion(_stream(("a", 1, True, "probably-fine-ish")))

        assert result.unknown_label == 1
        assert result.accepted == 0

    def test_the_latest_label_wins_because_labels_are_append_only(self) -> None:
        """A label may be superseded but never erased — the disagreement stays in the log as
        evidence while the measurement uses the current conclusion."""
        stream = _stream(("a", 1, True, "correct"))
        stream.append(_event(99, "node.outcome.labeled", {
            "node_id": "a", "attempt": 1, "label": "incorrect",
            "artifact_digest": _DIGEST, "labeller": "second-reviewer",
        }))

        result = confusion(stream)

        assert (result.false_accept, result.true_accept) == (1, 0)


class TestTheRefusalToReportASmallSample:
    def test_a_rate_over_too_few_labels_is_withheld_but_its_counts_are_not(self) -> None:
        """The central discipline of this module. 1-of-3 is not "33%" — it is not a measurement.

        The counts still come back, so a reader can see WHY the rate was withheld and is not asked to
        take the refusal on trust.
        """
        result = confusion(_stream(
            ("a", 1, True, "incorrect"), ("b", 1, True, "correct"), ("c", 1, True, "correct"),
        ))
        rate = result.false_accept_rate()

        assert rate.value is None and not rate.reportable
        assert (rate.numerator, rate.denominator) == (1, 3)

    def test_a_rate_is_reported_once_the_sample_clears_the_floor(self) -> None:
        specs = [("n%d" % i, 1, True, "correct") for i in range(MINIMUM_LABELLED_FOR_A_RATE - 1)]
        specs.append(("bad", 1, True, "incorrect"))

        rate = confusion(_stream(*specs)).false_accept_rate()

        assert rate.reportable
        assert rate.denominator == MINIMUM_LABELLED_FOR_A_RATE
        assert rate.value == 1 / MINIMUM_LABELLED_FOR_A_RATE

    def test_zero_observed_false_accepts_is_not_reported_as_certainty(self) -> None:
        """0/12 does not mean α is zero. It means no false accept was observed in twelve attempts,
        which is compatible with a true rate near 25% — and the interval has to say so.

        The textbook Wald interval gives [0, 0] here, asserting certainty from an absence of evidence.
        That single property is why this module uses Wilson.
        """
        specs = [("n%d" % i, 1, True, "correct") for i in range(12)]

        rate = confusion(_stream(*specs)).false_accept_rate()

        assert rate.value == 0.0
        assert rate.interval is not None
        assert rate.interval.low == 0.0
        assert rate.interval.high > 0.2, "an upper bound of ~0 would be a false claim of certainty"

    def test_an_empty_stream_reports_nothing_rather_than_zero(self) -> None:
        result = confusion([])

        assert result.labelled == 0
        assert result.false_accept_rate().value is None
        assert result.false_reject_rate().value is None


class TestTheCurvesThePaperNeeds:
    def test_alpha_is_reported_per_attempt_index_not_pooled(self) -> None:
        """The headline measurement. A pooled α hides whether the gate degrades as attempts climb —
        which it plausibly does, since later attempts are the hard cases and a worker that has already
        failed twice may be producing output shaped to get past the gate.
        """
        by_attempt = confusion_by_attempt(_stream(
            ("a", 1, True, "correct"), ("b", 1, True, "correct"),
            ("c", 2, True, "incorrect"), ("d", 2, True, "incorrect"),
        ))

        assert by_attempt[1].false_accept == 0
        assert by_attempt[2].false_accept == 2
        assert sorted(by_attempt) == [1, 2]

    def test_the_false_rejection_rate_is_computable_at_all(self) -> None:
        """The curve routinely absent from published gate evaluations. A gate can drive α to zero by
        rejecting everything, and only this number makes that visible."""
        specs = [("n%d" % i, 1, False, "correct") for i in range(10)]

        rate = confusion(_stream(*specs)).false_reject_rate()

        assert rate.reportable
        assert rate.value == 1.0, "a gate that blocked ten correct outputs rejected 100% wrongly"

    def test_blocked_precision_can_be_compared_to_the_published_advisory_baseline(self) -> None:
        """arXiv:2605.17998 reported an advisory gate whose blocks were justified 0.39% of the time —
        the number that made that team take it out of the enforcing path."""
        specs = [("n%d" % i, 1, False, "incorrect") for i in range(9)]
        specs.append(("good", 1, False, "correct"))

        rate = confusion(_stream(*specs)).blocked_precision()

        assert rate.reportable
        assert rate.value == 0.9
        assert rate.value > ADVISORY_BLOCKED_PRECISION_BASELINE

    def test_a_gate_that_rejects_everything_scores_perfectly_on_alpha_and_the_other_curve_shows_why(
        self,
    ) -> None:
        """The whole argument for reporting both curves, in one test.

        Reject every attempt and α is a flawless 0/0 — there are no accepts to be wrong about. The
        false-rejection rate is what exposes the trade, and a paper reporting only α would present
        this gate as perfect.
        """
        specs = [("n%d" % i, 1, False, "correct") for i in range(10)]
        result = confusion(_stream(*specs))

        assert result.accepted == 0
        assert result.false_accept_rate().value is None, "no accepts means no α, not a good α"
        assert result.false_reject_rate().value == 1.0


def test_a_confusion_with_no_labels_at_all_is_honest_about_it() -> None:
    """The likely real-world state of an early run: plenty of attempts, no ground truth yet."""
    result = Confusion(unlabelled=500)

    assert result.labelled == 0
    assert result.false_accept_rate().value is None
    assert result.unlabelled == 500


class TestTheIndependenceCaveat:
    """The interval's own assumption is violated by the data. That has to travel with the number."""

    def test_the_caveat_says_which_direction_the_error_goes(self) -> None:
        """A caveat that only says "assumptions apply" is decoration. This one has to say that the
        interval is too NARROW, because a reader who does not know the direction will assume the
        conservative one."""
        from bounded_loops.graph.application.gate_metrics import INDEPENDENCE_CAVEAT

        assert "NOT independent" in INDEPENDENCE_CAVEAT
        assert "NARROW" in INDEPENDENCE_CAVEAT
        assert "LOWER bound" in INDEPENDENCE_CAVEAT

    def test_the_cli_prints_the_caveat_whenever_it_prints_an_interval(self, tmp_path, capsys) -> None:
        """Wired so the caveat cannot be dropped while the numbers stay — the failure mode being that
        an interval ends up in a slide deck as a hard bound."""
        import inspect

        from bounded_loops.graph import cli_graph_metrics

        source = inspect.getsource(cli_graph_metrics.cmd_graph_metrics)
        assert "INDEPENDENCE_CAVEAT" in source
        assert "reportable" in source, "printed when a rate is reported, not unconditionally"


class TestTheCompoundingBaseline:
    """``1-(1-α)^m`` is the naive model, present to be falsified rather than followed."""

    def test_it_compounds_the_way_elementary_probability_says(self) -> None:
        from bounded_loops.graph.application.gate_metrics import naive_compounded_false_accept

        assert naive_compounded_false_accept(0.0, 10) == 0.0
        assert naive_compounded_false_accept(1.0, 3) == 1.0
        assert naive_compounded_false_accept(0.1, 1) == pytest.approx(0.1)
        assert naive_compounded_false_accept(0.1, 3) == pytest.approx(0.271)

    def test_zero_attempts_cannot_produce_a_false_accept(self) -> None:
        from bounded_loops.graph.application.gate_metrics import naive_compounded_false_accept

        assert naive_compounded_false_accept(0.5, 0) == 0.0

    @pytest.mark.parametrize(("alpha", "attempts"), [(-0.1, 1), (1.1, 1), (0.5, -1)])
    def test_nonsense_inputs_raise_rather_than_returning_a_plausible_number(
        self, alpha: float, attempts: int,
    ) -> None:
        """A silently-clamped probability would produce a real-looking curve from invalid input."""
        from bounded_loops.graph.application.gate_metrics import naive_compounded_false_accept

        with pytest.raises(ValueError):
            naive_compounded_false_accept(alpha, attempts)

    def test_it_is_documented_as_a_baseline_to_falsify_not_a_recommender(self) -> None:
        """The most dangerous possible use of this module is deriving max_attempts from this curve.
        The docstring has to forbid it in as many words, because someone will try."""
        from bounded_loops.graph.application.gate_metrics import naive_compounded_false_accept

        doc = naive_compounded_false_accept.__doc__ or ""
        assert "FALSIFY" in doc
        assert "not a budget recommender" in doc


class TestTheIntervalMathsItself:
    """These numbers would go in a paper, so the formula is checked against published values."""

    @pytest.mark.parametrize(
        ("successes", "trials", "low", "high"),
        [
            (0, 10, 0.0000, 0.2775),
            (1, 10, 0.0179, 0.4041),
            (5, 10, 0.2366, 0.7634),
            (9, 10, 0.5958, 0.9821),
            (10, 10, 0.7225, 1.0000),
            (2, 20, 0.0277, 0.3015),
        ],
    )
    def test_it_matches_published_wilson_intervals(
        self, successes: int, trials: int, low: float, high: float,
    ) -> None:
        """Standard Wilson 95% values (Newcombe 1998). A formula that is subtly wrong here produces
        confident-looking bounds that are simply false, which no amount of surrounding care fixes."""
        from bounded_loops.graph.application.gate_metrics import _wilson

        interval = _wilson(successes, trials)

        assert interval.low == pytest.approx(low, abs=5e-4)
        assert interval.high == pytest.approx(high, abs=5e-4)

    def test_impossible_counts_raise_instead_of_a_math_domain_error(self) -> None:
        """``_wilson(3, 2)`` used to surface as a bare ``ValueError: math domain error``.

        It raises deliberately rather than returning an uninformative [0, 1]: more successes than
        trials means a counting bug upstream, and a silent wide interval would carry that bug into a
        results table looking like an honest absence of evidence.
        """
        from bounded_loops.graph.application.gate_metrics import _wilson

        with pytest.raises(ValueError, match="cannot form an interval"):
            _wilson(3, 2)
        with pytest.raises(ValueError, match="cannot form an interval"):
            _wilson(-1, 5)

    @pytest.mark.parametrize(("successes", "trials"), [(0, 0), (0, 1), (1, 1), (0, 3), (7, 7)])
    def test_no_interval_ever_escapes_zero_to_one_or_inverts(
        self, successes: int, trials: int,
    ) -> None:
        from bounded_loops.graph.application.gate_metrics import _wilson

        interval = _wilson(successes, trials)

        assert 0.0 <= interval.low <= interval.high <= 1.0

    def test_an_empty_sample_yields_the_uninformative_interval_not_a_claim(self) -> None:
        """0 of 0 is the widest possible statement: the rate could be anything."""
        from bounded_loops.graph.application.gate_metrics import _wilson

        assert (_wilson(0, 0).low, _wilson(0, 0).high) == (0.0, 1.0)
