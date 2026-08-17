#!/usr/bin/env python3
"""E9 — bound utilisation and soft-bound yield.

WHAT IS MEASURED
----------------
Three quantities the paper needs and did not have:

1. **Bound utilisation** ``U = attempts_consumed / max_iterations``. How much of the declared hard
   budget a run actually spends. Low U means the ceiling is cheap insurance; U approaching 1 means
   it is load-bearing and the run is near the cliff.
2. **Predicted vs observed convergence.** With a worker that repairs one unit per attempt over a
   workload of ``n``, termination is predicted at attempt ``n``. Predicting before running, and
   recording both, is what separates a measurement from a demonstration.
3. **Soft-bound yield.** Attempts saved by ``no_progress_window`` against the hard ceiling — the
   gap between ``w + j*`` and ``m`` that ``prop:no-progress`` bounds analytically and that nothing
   had ever observed.

WHAT IS NOT MEASURED, AND MUST NOT BE CLAIMED
---------------------------------------------
Nothing here says anything about how capable a real agent is. The worker is synthetic and its
repair rate is the independent variable. A real-provider run is what places a frontier model on
this axis, and it is a separate experiment requiring credentials.

POSITIVE CONTROL
----------------
The harness refuses to report if the instrument is not firing: a converging condition must reach
DONE and a stalled condition must halt on no-progress. Without that, "the soft bound never fired"
and "the experiment never ran" produce the same output — which is the defect class this project
measures, and which this project has already shipped from its own apparatus more than once.

Usage:  python3 e9_bound_utilisation.py [--json OUT] [--ceiling 10] [--window 3]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
GENERATOR = HERE / "generate.py"
REPO = HERE.parents[1]

#: Workloads swept for the utilisation curve. Deliberately crosses the ceiling: at n > m the hard
#: bound must truncate the run, which is the only condition that observes the ceiling doing its job.
WORKLOADS = (1, 2, 3, 4, 6, 8, 10, 12, 16)


def _materialise(dest: pathlib.Path, records: int, policy: str,
                 ceiling: int, window: int, stall_after: int | None = None) -> pathlib.Path:
    argv = [
        sys.executable, str(GENERATOR), "--dest", str(dest),
        "--records", str(records), "--policy", policy,
        "--max-iterations", str(ceiling), "--no-progress-window", str(window),
    ]
    if stall_after is not None:
        argv += ["--stall-after", str(stall_after)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise SystemExit(f"generator failed for records={records} policy={policy}: {done.stderr}")
    return dest


def _run(loop_dir: pathlib.Path) -> dict:
    done = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "run", str(loop_dir), "--yes"],
        capture_output=True, text=True, timeout=600, cwd=REPO,
    )
    ledger = loop_dir / ".ledger.jsonl"
    if not ledger.exists():
        raise SystemExit(f"no ledger for {loop_dir.name}: {done.stdout}\n{done.stderr}")
    entries = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    # ATTEMPTS, not laps. A ceiling halt writes a row on which the worker was never invoked, so
    # counting rows reports 11 attempts against a ceiling of 10 -- utilisation 1.1 for a bound that
    # held exactly. The ledger now records `attempted` per row precisely so this is not a judgement
    # call for whoever reads the receipt.
    attempts = sum(1 for e in entries if e.get("attempted", True))
    return {
        "attempts": attempts,
        "laps_recorded": max(e["lap"] for e in entries),
        "terminal": entries[-1]["decision"],
        "stdout": done.stdout,
        "halted_on_no_progress": "no progress" in done.stdout.lower(),
        "final_gate_tail": entries[-1]["verdict"]["evidence"].get("tail", ""),
    }


def assert_instrument_fires(work: pathlib.Path, ceiling: int, window: int) -> None:
    """Refuse to report unless both directions are observed."""
    converging = _run(_materialise(work / "ctl-converge", 2, "one_per_lap", ceiling, window))
    if converging["terminal"] != "done" or converging["attempts"] != 2:
        raise SystemExit(
            "positive control failed: a 2-unit workload with one repair per attempt did not "
            f"reach DONE at attempt 2 (got {converging['terminal']} at "
            f"{converging['attempts']}). Refusing to report utilisation from an instrument that "
            "cannot converge."
        )
    stalled = _run(_materialise(work / "ctl-stall", 2, "stalled", ceiling, window))
    if not stalled["halted_on_no_progress"] or stalled["attempts"] != window:
        raise SystemExit(
            "positive control failed: a stalled worker did not halt on the soft bound at the "
            f"window (got attempts={stalled['attempts']}, no_progress="
            f"{stalled['halted_on_no_progress']}). Refusing to report soft-bound yield from an "
            "instrument whose soft bound is not firing."
        )
    print(f"positive control: converges at 2, stalls at the window ({window})\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=pathlib.Path, default=None)
    parser.add_argument("--ceiling", type=int, default=10)
    parser.add_argument("--window", type=int, default=3)
    args = parser.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="bl-e9-"))
    try:
        assert_instrument_fires(work, args.ceiling, args.window)

        utilisation = []
        print(f"{'workload':>9} {'predicted':>10} {'observed':>9} {'terminal':>9} {'U':>6}")
        for n in WORKLOADS:
            loop = _materialise(work / f"w{n}", n, "one_per_lap", args.ceiling, args.window)
            outcome = _run(loop)
            # One repair per attempt over n units terminates at attempt n -- unless the hard
            # ceiling truncates first, which is the ceiling doing exactly its job.
            predicted = min(n, args.ceiling)
            row = {
                "workload": n,
                "declared_ceiling": args.ceiling,
                "predicted_attempts": predicted,
                "observed_attempts": outcome["attempts"],
                "terminal": outcome["terminal"],
                "utilisation": round(outcome["attempts"] / args.ceiling, 3),
                "truncated_by_ceiling": n > args.ceiling,
            }
            utilisation.append(row)
            print(f"{n:>9} {predicted:>10} {outcome['attempts']:>9} "
                  f"{outcome['terminal']:>9} {row['utilisation']:>6}")

        print()
        yields = []
        print(f"{'stalls after':>13} {'ceiling':>8} {'observed':>9} {'saved':>6} {'reason':>12}")
        for j in (0, 1, 2, 4):
            name, extra = ("stalled", None) if j == 0 else ("stall_after", j)
            loop = _materialise(work / f"s{j}", 16, name, args.ceiling, args.window, extra)
            outcome = _run(loop)
            hard_cost = args.ceiling  # attempts a run to the ceiling would have spent
            row = {
                "productive_attempts": j,
                "window": args.window,
                "declared_ceiling": args.ceiling,
                "observed_attempts": outcome["attempts"],
                "predicted_min_m_w_plus_j": min(args.ceiling, args.window + j),
                "attempts_saved_vs_ceiling": hard_cost - outcome["attempts"],
                "halted_on_no_progress": outcome["halted_on_no_progress"],
            }
            yields.append(row)
            print(f"{j:>13} {args.ceiling:>8} {outcome['attempts']:>9} "
                  f"{row['attempts_saved_vs_ceiling']:>6} "
                  f"{'no-progress' if outcome['halted_on_no_progress'] else 'CEILING':>12}")

        exact = sum(1 for r in utilisation if r["observed_attempts"] == r["predicted_attempts"])
        summary = {
            "experiment": "E9 bound utilisation and soft-bound yield",
            "caveat": (
                "The worker is synthetic and its repair rate is the independent variable. Nothing "
                "here measures agent capability; a real-provider run is a separate experiment."
            ),
            "declared_ceiling": args.ceiling,
            "no_progress_window": args.window,
            "predictions_exact": f"{exact}/{len(utilisation)}",
            "utilisation": utilisation,
            "soft_bound_yield": yields,
        }
        print(f"\npredicted == observed on {exact}/{len(utilisation)} workloads")
        if args.json:
            args.json.write_text(json.dumps(summary, indent=2) + "\n")
            print(f"written: {args.json}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
