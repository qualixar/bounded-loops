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
from typing import Sequence

from bounded_loops.domain.errors import ManifestError
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
from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import StoredGraphEvent


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
    # An anytime-valid confidence sequence (stitched PrPl-EB). The label names the ESTIMAND, not
    # just the level, because the failure mode here is a coverage figure quoted against the wrong
    # quantity -- this line used to read "emp-Bernstein 95% (COVERAGE-MEASURED)", where the measured
    # coverage belonged to a per-run latent rate and the number printed beside it was the pooled
    # one. "for-log-mean" is four characters of prevention.
    # Predecessors, both retired: the fixed-time emp-Bernstein radius (not valid under the optional
    # stopping a live run performs), and before that a "nominal-95% iid" Wilson label whose measured
    # coverage under the same simulated regime was 77.5%.
    return (
        f"  {label:<24} {rate.value:.4f}  [{rate.interval.low:.4f}, {rate.interval.high:.4f}] "
        f"anytime-valid 95% for-log-mean   from {rate.numerator}/{rate.denominator}"
    )


def _rate_dict(rate: Rate, *, method: str) -> dict[str, object]:
    """One rate as JSON, with the interval's METHOD and ESTIMAND named beside it.

    Both fields exist because the JSON used to emit the Wilson interval while the text output
    emitted the empirical-Bernstein one, with nothing on either to say which was which — so a script
    reading ``--json`` published the interval this release retired. An interval whose method a
    consumer has to infer is an interval that gets quoted as whatever the reader assumes.
    """
    return {
        "numerator": rate.numerator,
        "denominator": rate.denominator,
        "value": rate.value,
        "interval": (
            None if rate.interval is None
            else {"low": rate.interval.low, "high": rate.interval.high}
        ),
        "interval_method": method,
        # The estimand, spelled out, because a machine consumer cannot read the caveat prose. The
        # anytime-valid sequence brackets the average false-accept propensity of the attempts IN
        # THIS LOG (measured coverage 1.0000 across every simulated regime). Coverage of the
        # POPULATION marginal rate is a different question answered by a different number, ranging
        # 0.83-1.00 with the count of independent nodes pooled and their heterogeneity.
        "interval_estimand": "mean over the attempts in this log (not the population rate)",
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
        # ``wilson_uncalibrated``: retained for continuity, named so it cannot be quoted as a
        # calibrated 95% interval. Its independence assumption is the one retried attempts violate.
        "false_accept_rate": _rate_dict(
            result.false_accept_rate(), method="wilson_uncalibrated",
        ),
        "false_rejection_rate": _rate_dict(
            result.false_reject_rate(), method="wilson_uncalibrated",
        ),
        "blocked_precision": _rate_dict(
            result.blocked_precision(), method="wilson_uncalibrated",
        ),
    }


def _wrapped(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width)


def load_receipts(run_dir: Path) -> Sequence[StoredGraphEvent]:
    """Replay a run's receipt stream, or raise ManifestError saying why it cannot be read.

    Extracted from ``cmd_graph_metrics`` so the MCP surface can report metrics without going
    through a function that prints: this server speaks JSON-RPC over stdout, and a stray line of
    human text corrupts the transport. One loader, two readers.
    """
    _plan, identity = load_plan_from_run_dir(
        run_dir, package_digests=admitted_loop_package_digests(),
    )[:2]
    log_path = run_dir / "controller-events.jsonl"
    if not log_path.is_file():
        # ``GraphEventLog.__init__`` touches the file, so simply constructing one CREATED an
        # empty log (and its lock) inside the run directory. A read-only report must not mutate
        # the run it reports on — and an absent log is a real answer, not an empty one.
        raise ManifestError(f"no receipt stream at {log_path} — nothing to measure")
    return GraphEventLog(log_path, identity).replay()


def metrics_document(
    run_dir: Path,
    *,
    receipts: Sequence[StoredGraphEvent] | None = None,
) -> dict:
    """The machine-readable metrics document — the same object ``--json`` prints.

    Shared with the MCP `graph_metrics` tool so the two surfaces cannot disagree about which
    method produced a number a reader is about to publish.
    """
    if receipts is None:
        receipts = load_receipts(run_dir)
    # The intervals the TEXT output prints. Keyed by what they ARE, not by the method that produced
    # them last release: this block held fixed-time empirical-Bernstein radii until the anytime-valid
    # sequence replaced them, and a key named for a retired method is how a consumer ends up quoting
    # the wrong guarantee. `empirical_bernstein` is kept below as a deprecated alias to the same
    # object so existing scripts keep reading — every `interval_method` inside self-describes.
    intervals = {
        "false_accept_rate": _rate_dict(
            false_accept_rate_cs(receipts), method="anytime_valid_prpl_eb",
        ),
        "false_rejection_rate": _rate_dict(
            false_reject_rate_cs(receipts), method="anytime_valid_prpl_eb",
        ),
        "blocked_precision": _rate_dict(
            blocked_precision_cs(receipts), method="anytime_valid_prpl_eb",
        ),
    }
    return {
        "run": str(run_dir),
        "overall": _confusion_dict(confusion(receipts)),
        "by_attempt": {
            str(key): _confusion_dict(value)
            for key, value in confusion_by_attempt(receipts).items()
        },
        "anytime_valid": intervals,
        "empirical_bernstein": intervals,  # DEPRECATED alias — removed in a future major
        "advisory_blocked_precision_baseline": ADVISORY_BLOCKED_PRECISION_BASELINE,
        "independence_caveat": INDEPENDENCE_CAVEAT,
    }


def cmd_graph_metrics(args: argparse.Namespace) -> int:
    """Report the gate's confusion matrix and the two curves, overall and per attempt index."""
    run_dir = Path(args.run)
    try:
        receipts = load_receipts(run_dir)
    except ManifestError as exc:
        _err(f"graph metrics: {exc}")
        return 2
    except (GraphIntegrityError, GraphValidationError, OSError, ValueError) as exc:
        # ValueError included because ``load_plan_from_run_dir``'s symlink guard raises one, so
        # ``bl graph metrics --run /tmp`` printed a traceback instead of saying what was wrong.
        _err(f"graph metrics: {exc}")
        return 2

    overall = confusion(receipts)
    per_attempt = confusion_by_attempt(receipts)

    # Fixed-time empirical-Bernstein rates. NOT anytime-valid: the radius carries no stitching
    # term. Emitted in BOTH the text and the JSON output.
    fa_cs = false_accept_rate_cs(receipts)
    fr_cs = false_reject_rate_cs(receipts)
    bp_cs = blocked_precision_cs(receipts)
    fa_cs_by_attempt = false_accept_rate_cs_by_attempt(receipts)

    if getattr(args, "json", False):
        print(json.dumps(
            metrics_document(run_dir, receipts=receipts), indent=2, sort_keys=True,
        ))
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
            "false-accept rate, the false-REJECTION rate, and blocked precision — each with an "
            "empirical-Bernstein interval at a nominal 95% whose COVERAGE IS MEASURED rather than "
            "proven, and each WITHHELD when too few labels exist to support it. "
            "Computed from the receipt stream alone, so any figure can be regenerated from an "
            "archived run."
        ),
    )
    metrics_p.add_argument("--run", required=True, metavar="<dir>", help="Run directory to read.")
    metrics_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    metrics_p.set_defaults(func=cmd_graph_metrics)
