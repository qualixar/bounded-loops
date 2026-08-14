"""``bl graph metrics`` — what the gate actually achieved on a persisted run.

The evaluation harness as a command rather than a notebook, for one reason: every figure has to be
reproducible from an archived receipt stream by someone who did not run it. A number a reviewer
cannot regenerate is not evidence.

It reads the log and nothing else. No store, no network, no clock.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.gate_metrics import (
    ADVISORY_BLOCKED_PRECISION_BASELINE,
    INDEPENDENCE_CAVEAT,
    Confusion,
    Rate,
    blocked_precision_cs,
    confusion,
    confusion_by_attempt,
    false_accept_rate_cs,
    false_accept_rate_cs_by_attempt,
    false_reject_rate_cs,
)
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError


def _err(message: str) -> None:
    import sys

    print(f"error: {message}", file=sys.stderr)


def _rate_text(label: str, rate: Rate) -> str:
    if not rate.reportable:
        return (
            f"  {label:<24} not reported — {rate.numerator} of {rate.denominator} "
            f"(too few to support a rate; the counts are real)"
        )
    assert rate.interval is not None
    # Empirical-Bernstein with a predictable plug-in. Labelled by what has been MEASURED
    # (96.9% coverage under simulated correlated retries with optional stopping), NOT by the
    # simultaneous-validity theorem -- the radius carries no stitching term, so that theorem
    # is unverified here. See confidence_sequence.py and the COVERAGE-MEASURED note.
    # Replaces the former "nominal-95% iid (UNCALIBRATED)" Wilson label whose measured
    # coverage under correlated retries was 31-41%. The two sentences that used to close this
    # comment claimed the interval "is valid under optional stopping and makes no independence
    # assumption" -- the guarantee the three lines above deny. Vestigial Wilson-era text that the
    # relabelling prepended to rather than replaced, and a comment a reviewer would quote as intent.
    return (
        f"  {label:<24} {rate.value:.4f}  [{rate.interval.low:.4f}, {rate.interval.high:.4f}] "
        f"emp-Bernstein 95% (COVERAGE-MEASURED)   from {rate.numerator}/{rate.denominator}"
    )


def _rate_dict(rate: Rate) -> dict[str, object]:
    return {
        "numerator": rate.numerator,
        "denominator": rate.denominator,
        "value": rate.value,
        "interval": (
            None if rate.interval is None
            else {"low": rate.interval.low, "high": rate.interval.high}
        ),
        "reported": rate.reportable,
    }


def _confusion_dict(result: Confusion) -> dict[str, object]:
    return {
        "true_accept": result.true_accept,
        "false_accept": result.false_accept,
        "true_reject": result.true_reject,
        "false_reject": result.false_reject,
        "unknown_label": result.unknown_label,
        "unlabelled": result.unlabelled,
        "labelled": result.labelled,
        "false_accept_rate": _rate_dict(result.false_accept_rate()),
        "false_rejection_rate": _rate_dict(result.false_reject_rate()),
        "blocked_precision": _rate_dict(result.blocked_precision()),
    }


def _wrapped(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width)


def cmd_graph_metrics(args: argparse.Namespace) -> int:
    """Report the gate's confusion matrix and the two curves, overall and per attempt index."""
    run_dir = Path(args.run)
    try:
        plan, identity = load_plan_from_run_dir(run_dir)[:2]
        log_path = run_dir / "controller-events.jsonl"
        if not log_path.is_file():
            # ``GraphEventLog.__init__`` touches the file, so simply constructing one CREATED an
            # empty log (and its lock) inside the run directory. A read-only report must not mutate
            # the run it reports on — and an absent log is a real answer, not an empty one.
            _err(f"graph metrics: no receipt stream at {log_path} — nothing to measure")
            return 2
        log = GraphEventLog(log_path, identity)
        receipts = log.replay()
    except (GraphIntegrityError, GraphValidationError, OSError, ValueError) as exc:
        # ValueError included because ``load_plan_from_run_dir``'s symlink guard raises one, so
        # ``bl graph metrics --run /tmp`` printed a traceback instead of saying what was wrong.
        _err(f"graph metrics: {exc}")
        return 2

    overall = confusion(receipts)
    per_attempt = confusion_by_attempt(receipts)

    # Anytime-valid CS-based rates for text output (Wilson kept only in JSON via _confusion_dict)
    fa_cs = false_accept_rate_cs(receipts)
    fr_cs = false_reject_rate_cs(receipts)
    bp_cs = blocked_precision_cs(receipts)
    fa_cs_by_attempt = false_accept_rate_cs_by_attempt(receipts)

    if getattr(args, "json", False):
        print(json.dumps({
            "run": str(run_dir),
            "overall": _confusion_dict(overall),
            "by_attempt": {str(k): _confusion_dict(v) for k, v in per_attempt.items()},
            "advisory_blocked_precision_baseline": ADVISORY_BLOCKED_PRECISION_BASELINE,
            "independence_caveat": INDEPENDENCE_CAVEAT,
        }, indent=2, sort_keys=True))
        return 0

    print("Gate performance — computed from the receipt stream, nothing else")
    print("=" * 70)
    gated = overall.labelled + overall.unlabelled + overall.unknown_label
    if gated == 0:
        # A DIFFERENT fact from "unlabelled", and it was printed identically: this run has nothing to
        # measure because the gate never evaluated anything — every attempt failed before reaching it,
        # or the run has no gated nodes. Saying "no labels" here would send a reader off to label
        # attempts that do not exist.
        print("NO GATED ATTEMPTS in this run — the gate never evaluated anything.")
        print("Every attempt failed before the gate (worker fault, policy denial, unverified")
        print("artifact), or the run has no connector nodes. There is nothing to measure here.")
        return 0
    if overall.labelled == 0:
        # Said first and plainly. The most likely state of a young run is "no ground truth yet", and
        # a table of zeroes would read as "the gate made no mistakes".
        print(f"NO GROUND-TRUTH LABELS in this run ({overall.unlabelled} gated attempts unlabelled).")
        print("Nothing about the gate's accuracy can be computed. Record labels with")
        print("label_node_outcome(...) and re-run; until then this run measures nothing.")
        return 0

    print(f"  gated attempts           {gated}")
    print(f"  labelled                 {overall.labelled}")
    print(f"  unlabelled               {overall.unlabelled}")
    print(f"  reviewed but undecidable {overall.unknown_label}")
    print()
    print(f"  accepted by the gate     {overall.accepted}  ({overall.false_accept} of them wrong)")
    print(f"  blocked by the gate      {overall.rejected}  ({overall.false_reject} of them correct)")
    print()
    print(_rate_text("false-accept rate (α)", fa_cs))
    print(_rate_text("false-REJECTION rate", fr_cs))
    print(_rate_text("blocked precision", bp_cs))
    if bp_cs.reportable:
        # Only beside a real number. Printing a published baseline next to "0/0" invites the reader
        # to compare against nothing, which is worse than omitting the comparison.
        print(f"  {'advisory baseline':<24} {ADVISORY_BLOCKED_PRECISION_BASELINE:.4f} "
              "(arXiv:2605.17998 — the number that demoted a gate to advisory)")

    if any(rate.reportable for rate in (fa_cs, fr_cs, bp_cs)):
        # Beside the numbers, never in a footnote: an interval quoted without its assumption is the
        # thing that ends up in someone's slide deck as a hard bound.
        print()
        print("  ASSUMPTION — read before quoting any interval above:")
        for line in _wrapped(INDEPENDENCE_CAVEAT, 76):
            print(f"    {line}")

    if len(per_attempt) > 1:
        print()
        print("  α by attempt index — a pooled α hides whether the gate degrades on retries:")
        for index in sorted(per_attempt):
            # Prefer CS rate when available; fall back to Wilson for indices with no labelled accepts.
            rate = fa_cs_by_attempt.get(index, per_attempt[index].false_accept_rate())
            print(f"    attempt {index}" + _rate_text("", rate))
    return 0


def add_metrics_parser(graph_subs: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``bl graph metrics``."""
    metrics_p = graph_subs.add_parser(
        "metrics",
        help="Report what the independent gate actually achieved on a run.",
        description=(
            "Compare the gate's verdicts against recorded ground-truth labels and report the "
            "false-accept rate, the false-REJECTION rate, and blocked precision — each with a 95% "
            "confidence interval, and each WITHHELD when too few labels exist to support it. "
            "Computed from the receipt stream alone, so any figure can be regenerated from an "
            "archived run."
        ),
    )
    metrics_p.add_argument("--run", required=True, metavar="<dir>", help="Run directory to read.")
    metrics_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    metrics_p.set_defaults(func=cmd_graph_metrics)
