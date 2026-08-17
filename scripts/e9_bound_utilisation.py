#!/usr/bin/env python3
"""E9 — bound utilisation and soft-bound yield, measured on the SHIPPED loop.

WHAT IS MEASURED
----------------
1. **Bound utilisation** ``U = attempts_consumed / max_iterations``. How much of the declared hard
   budget a run actually spends. Low U means the ceiling is cheap insurance; U approaching 1 means
   it is load-bearing and the run is near the cliff.
2. **Predicted vs observed convergence.** ``loops/record-completeness`` repairs one record per
   attempt, so a workload of *n* records is predicted to terminate at attempt *n*, truncated by the
   ceiling. Predicting before running and recording both is what separates a measurement from a
   demonstration.
3. **Soft-bound yield.** Attempts saved by ``no_progress_window`` against the hard ceiling -- the gap
   between ``w + j*`` and ``m`` that ``prop:no-progress`` bounds analytically and that nothing had
   ever observed.

EVERY CONDITION IS A COPY OF THE SHIPPED LOOP
---------------------------------------------
The sweep copies ``loops/record-completeness`` and edits exactly two things in the copy: how many
records the seed holds, and (for the stall conditions) how many the worker is willing to repair. It
does not author a parallel loop. An earlier version did, and a curve measured on a construction that
ships nowhere says nothing about the artifact a reader can actually run.

WHAT IS NOT MEASURED, AND MUST NOT BE CLAIMED
---------------------------------------------
Nothing here measures how capable a real agent is. The worker is deterministic and its repair rate is
the independent variable. Placing a real model on this axis is a separate experiment needing
credentials.

Usage:  uv run python scripts/e9_bound_utilisation.py [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
LOOP = REPO / "loops" / "record-completeness"

#: Workloads swept. Deliberately crosses the declared ceiling: at n > m the hard bound must truncate
#: the run, and that is the only condition under which the ceiling is observed doing its job.
WORKLOADS = (1, 2, 3, 4, 6, 8, 10, 12, 16)

_ARTIFACTS = (".bounded-loops", ".ledger.jsonl", ".STATE.md.runtime", "__pycache__", "*.pyc")


def _copy(dest: pathlib.Path) -> pathlib.Path:
    shutil.copytree(LOOP, dest, ignore=shutil.ignore_patterns(*_ARTIFACTS))
    return dest


def _set_records(loop_dir: pathlib.Path, n: int) -> None:
    records = [{"id": i, "payload": f"row-{i}", "amount": 100 + i} for i in range(1, n + 1)]
    (loop_dir / "seed" / "records.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8"
    )


def _set_repair_limit(loop_dir: pathlib.Path, repairs: int) -> None:
    """Cap how many records the worker will EVER repair, so it stalls after `repairs` attempts.

    Edits the shipped worker's one declared constant in the copy. `repairs=0` is a worker that never
    changes anything -- the condition under which the soft bound should fire at the window itself.
    """
    worker = loop_dir / "seed" / "worker.py"
    source = worker.read_text(encoding="utf-8")
    marker = "REPAIR_QUOTA = 1"
    if marker not in source:
        raise SystemExit(
            f"{worker} no longer declares {marker!r}. The sweep edits the shipped worker's own "
            "constant on purpose; refusing to guess at a replacement."
        )
    stall = (
        "REPAIR_QUOTA = 1\n"
        f"_STALL_AFTER = {repairs}  # sweep condition: repair this many in total, then stop\n"
    )
    source = source.replace(marker + "\n", stall, 1)
    source = source.replace(
        "    allowed = min(REPAIR_QUOTA, len(missing))",
        "    already = len(records) - len(missing)\n"
        "    allowed = 0 if already >= _STALL_AFTER else min(REPAIR_QUOTA, len(missing))",
    )
    worker.write_text(source, encoding="utf-8")


def _bounds(loop_dir: pathlib.Path) -> tuple[int, int]:
    text = (loop_dir / "bounds.yaml").read_text(encoding="utf-8")
    ceiling = window = 0
    for line in text.splitlines():
        if line.startswith("max_iterations:"):
            ceiling = int(line.split(":", 1)[1])
        elif line.startswith("no_progress_window:"):
            window = int(line.split(":", 1)[1])
    if not ceiling or not window:
        raise SystemExit(f"could not read max_iterations/no_progress_window from {loop_dir}")
    return ceiling, window


def _run(loop_dir: pathlib.Path) -> dict:
    done = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "run", str(loop_dir), "--yes"],
        capture_output=True, text=True, timeout=600, cwd=REPO,
    )
    ledger = loop_dir / ".ledger.jsonl"
    if not ledger.exists():
        raise SystemExit(f"no ledger for {loop_dir.name}:\n{done.stdout}\n{done.stderr}")
    entries = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    # ATTEMPTS, not laps. A ceiling halt writes a row for the lap the budget tripped on, and the
    # worker is never invoked on that lap; counting rows reports 11 attempts against a declared 10.
    return {
        "attempts": sum(1 for e in entries if e.get("attempted", True)),
        "laps_recorded": max(e["lap"] for e in entries),
        "terminal": entries[-1]["decision"],
        "halted_on_no_progress": "no progress" in done.stdout.lower(),
        "stdout": done.stdout,
    }


def assert_instrument_fires(work: pathlib.Path, ceiling: int, window: int) -> None:
    """Refuse to report unless both directions are observed on the shipped loop."""
    converging = _copy(work / "ctl-converge")
    _set_records(converging, 2)
    result = _run(converging)
    if result["terminal"] != "done" or result["attempts"] != 2:
        raise SystemExit(
            "positive control failed: a 2-record workload did not reach DONE at attempt 2 "
            f"(got {result['terminal']} at {result['attempts']}). Refusing to report utilisation "
            "from an instrument that cannot converge."
        )
    stalled = _copy(work / "ctl-stall")
    _set_records(stalled, 2)
    _set_repair_limit(stalled, 0)
    result = _run(stalled)
    if not result["halted_on_no_progress"] or result["attempts"] != window:
        raise SystemExit(
            "positive control failed: a worker that changes nothing did not halt on the soft bound "
            f"at the window (attempts={result['attempts']}, no_progress="
            f"{result['halted_on_no_progress']}). Refusing to report soft-bound yield from an "
            "instrument whose soft bound is not firing."
        )
    print(f"positive control: converges at 2, stalls at the window ({window})\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=pathlib.Path, default=None)
    args = parser.parse_args()

    if not LOOP.is_dir():
        raise SystemExit(f"shipped loop not found at {LOOP}")
    ceiling, window = _bounds(LOOP)
    print(f"shipped loop: {LOOP.name}  max_iterations={ceiling}  no_progress_window={window}\n")

    work = pathlib.Path(tempfile.mkdtemp(prefix="bl-e9-"))
    try:
        assert_instrument_fires(work, ceiling, window)

        utilisation = []
        print(f"{'workload':>9} {'predicted':>10} {'observed':>9} {'terminal':>9} {'U':>6}")
        for n in WORKLOADS:
            loop = _copy(work / f"w{n}")
            _set_records(loop, n)
            outcome = _run(loop)
            predicted = min(n, ceiling)
            row = {
                "workload": n,
                "declared_ceiling": ceiling,
                "predicted_attempts": predicted,
                "observed_attempts": outcome["attempts"],
                "terminal": outcome["terminal"],
                "utilisation": round(outcome["attempts"] / ceiling, 3),
                "truncated_by_ceiling": n > ceiling,
            }
            utilisation.append(row)
            print(f"{n:>9} {predicted:>10} {outcome['attempts']:>9} "
                  f"{outcome['terminal']:>9} {row['utilisation']:>6}")

        print()
        yields = []
        print(f"{'repairs then stalls':>19} {'min{m,w+j*}':>12} {'observed':>9} {'saved':>6} {'reason':>12}")
        for j in (0, 1, 2, 4):
            loop = _copy(work / f"s{j}")
            _set_records(loop, 16)
            _set_repair_limit(loop, j)
            outcome = _run(loop)
            row = {
                "productive_attempts": j,
                "window": window,
                "declared_ceiling": ceiling,
                "predicted_min_m_w_plus_j": min(ceiling, window + j),
                "observed_attempts": outcome["attempts"],
                "attempts_saved_vs_ceiling": ceiling - outcome["attempts"],
                "halted_on_no_progress": outcome["halted_on_no_progress"],
            }
            yields.append(row)
            print(f"{j:>19} {row['predicted_min_m_w_plus_j']:>12} {outcome['attempts']:>9} "
                  f"{row['attempts_saved_vs_ceiling']:>6} "
                  f"{'no-progress' if outcome['halted_on_no_progress'] else 'CEILING':>12}")

        exact_u = sum(1 for r in utilisation if r["observed_attempts"] == r["predicted_attempts"])
        exact_y = sum(1 for r in yields
                      if r["observed_attempts"] == r["predicted_min_m_w_plus_j"])
        summary = {
            "experiment": "E9 bound utilisation and soft-bound yield",
            "measured_on": f"loops/{LOOP.name} (shipped catalogue member)",
            "caveat": (
                "The worker is deterministic and its repair rate is the independent variable. "
                "Nothing here measures agent capability."
            ),
            "declared_ceiling": ceiling,
            "no_progress_window": window,
            "utilisation_predictions_exact": f"{exact_u}/{len(utilisation)}",
            "soft_bound_predictions_exact": f"{exact_y}/{len(yields)}",
            "utilisation": utilisation,
            "soft_bound_yield": yields,
        }
        print(f"\nutilisation predicted == observed: {exact_u}/{len(utilisation)}")
        print(f"min{{m, w+j*}} predicted == observed: {exact_y}/{len(yields)}")
        if args.json:
            args.json.write_text(json.dumps(summary, indent=2) + "\n")
            print(f"written: {args.json}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
