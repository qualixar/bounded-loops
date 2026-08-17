#!/usr/bin/env python3
"""E3 — interval estimates for the gate's false-accept rate, both guarantees.

WHY THIS FILE EXISTS AT ALL
---------------------------
E3's table appeared in the paper and **no script produced it**. `paper/experiments/` carried scripts
for E5, E7, E8 and E9 and nothing for E3; the only related code was the shipped estimator module. So
the numbers came from something that was never committed — the same defect as "the E5 script lived
only in the private repo", which this project already fixed once. A table nobody can regenerate is a
table nobody can check.

This is the rewrite. It derives every figure from the E7 post-freeze corpus output, so E3 and E7 can
never disagree: E3 reads E7's JSON rather than re-deriving the labels.

WHAT IS REPORTED, AND WHY TWO INTERVALS
---------------------------------------
`bounded_loops/graph/application/confidence_sequence.py` ships two estimators with **different
guarantees**, and reporting only one would overstate what is known:

* `empirical_bernstein_interval` — a FIXED-n interval. Valid for the sample size chosen in advance.
  Correct for a corpus frozen before the run, which is what E7 is.
* `anytime_valid_interval` — a confidence SEQUENCE. Valid simultaneously at every n, so it stays
  honest if someone adds mutants and looks again. Necessarily wider; that width is the price of being
  allowed to peek.

For a corpus of size n with zero false accepts, both bounds answer the practitioner question the
paper actually needs: **not "is alpha zero" but "how small can alpha be claimed to be".**

Usage:
    uv run python scripts/e3_interval_estimates.py --e7 <e7-post-freeze.json> [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bounded_loops.graph.application.confidence_sequence import (  # noqa: E402
    anytime_valid_interval,
    empirical_bernstein_interval,
)

_ALPHA = 0.05


def _indicators(count: int, positives: int) -> list[float]:
    """`count` Bernoulli observations of which `positives` are 1.

    Order does not affect either estimator's endpoints for a fixed multiset, and the corpus has no
    meaningful ordering — mutants are generated per loop, not sampled over time.
    """
    if positives > count:
        raise SystemExit(f"{positives} positives out of {count} observations is impossible")
    return [1.0] * positives + [0.0] * (count - positives)


def _rate(label: str, positives: int, count: int) -> dict:
    if count == 0:
        # "No false accepts" and "nothing was judged" must never print the same way.
        return {
            "quantity": label, "observed": None, "n": 0,
            "note": "no observations; no interval is defined and none is reported",
        }
    observations = _indicators(count, positives)
    fixed_lo, fixed_hi = empirical_bernstein_interval(observations, alpha=_ALPHA)
    any_lo, any_hi = anytime_valid_interval(observations, alpha=_ALPHA)
    return {
        "quantity": label,
        "positives": positives,
        "n": count,
        "observed": positives / count,
        "fixed_n_empirical_bernstein": {"lower": fixed_lo, "upper": fixed_hi},
        "anytime_valid_sequence": {"lower": any_lo, "upper": any_hi},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--e7", type=pathlib.Path, required=True,
                        help="E7 post-freeze JSON; E3 reads its labels rather than re-deriving them")
    parser.add_argument("--json", type=pathlib.Path, default=None)
    args = parser.parse_args()

    if not args.e7.exists():
        raise SystemExit(f"{args.e7} does not exist; run e7_post_freeze.py first")
    e7 = json.loads(args.e7.read_text(encoding="utf-8"))

    for required in ("destroying", "preserving", "false_accepts", "false_rejects"):
        if required not in e7:
            raise SystemExit(f"{args.e7} has no {required!r}; it is not an E7 post-freeze output")

    # Refuse to report an interval over an empty corpus. "Alpha is bounded by X" derived from zero
    # mutants is the vacuous total this project measures in other people's gates.
    if not e7["destroying"] and not e7["preserving"]:
        raise SystemExit(
            "the E7 run judged no mutants at all; refusing to report an interval over an empty "
            "corpus. Fix the corpus, do not narrow the claim."
        )

    rows = [
        _rate("alpha (false accept: a destroyed artifact certified)",
              e7["false_accepts"], e7["destroying"]),
        _rate("beta (false reject: a conforming artifact refused)",
              e7["false_rejects"], e7["preserving"]),
    ]

    print(f"source: {args.e7.name}   confidence: {100 * (1 - _ALPHA):.0f}%\n")
    header = f"{'quantity':<52} {'obs':>10} {'n':>4} {'fixed-n upper':>14} {'anytime upper':>14}"
    print(header)
    print("-" * len(header))
    for row in rows:
        if row.get("n"):
            print(f"{row['quantity']:<52} {row['positives']:>4}/{row['n']:<5} {row['n']:>4} "
                  f"{row['fixed_n_empirical_bernstein']['upper']:>14.4f} "
                  f"{row['anytime_valid_sequence']['upper']:>14.4f}")
        else:
            print(f"{row['quantity']:<52} {'n/a':>10} {0:>4} {'—':>14} {'—':>14}")

    print(
        "\nThe fixed-n bound is the one to quote for this frozen corpus. The anytime-valid bound is\n"
        "wider and is what remains true if the corpus grows and someone looks again — quote it when\n"
        "reporting a rate that will be revisited."
    )

    summary = {
        "experiment": "E3 interval estimates for the gate's error rates",
        "source": str(args.e7.name),
        "confidence": 1 - _ALPHA,
        "derived_from_e7": True,
        "note": (
            "Both estimators ship in bounded_loops.graph.application.confidence_sequence. The "
            "fixed-n interval is valid for a sample size chosen in advance, which a frozen corpus "
            "is. The anytime-valid sequence is valid simultaneously at every n and is therefore "
            "wider; that width is the price of being allowed to peek."
        ),
        "rates": rows,
    }
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
