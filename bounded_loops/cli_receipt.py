"""Rendering the human-readable receipt for one persisted run.

Extracted from ``cli`` when the per-lap view began carrying gate provenance. Not a cosmetic split:
the receipt is about to grow a written artifact and its own ``bl receipt`` command, and the argparse
wiring in ``cli`` is not where that belongs. A reader asking "what exactly does a receipt claim?"
should find one file, not a region of a command dispatcher.

Reads the run directory and prints. No I/O beyond stdout, no re-running of a gate — a receipt is a
record of what already happened, and anything here that recomputed a verdict would be inventing one.
"""
from __future__ import annotations


def _print_run_receipt(receipt: dict) -> None:
    metadata = receipt["metadata"]
    run_id = metadata.get("run_id", "?")
    status = metadata.get("status", "UNKNOWN")
    reason = metadata.get("reason", "unknown")
    print(f"Run {run_id}: {status} ({reason})")
    print(f"Workspace: {metadata.get('workspace', 'unknown')}")
    print(f"Ledger: {metadata.get('ledger_path', 'unknown')}")
    print()
    for entry in receipt["entries"]:
        verdict = entry.get("verdict", {})
        passed = verdict.get("passed") is True
        state = "PASS" if passed else "FAIL"
        budget = entry.get("budget_spent", {})
        print(
            f"Lap {entry.get('lap', '?')}: {state}  "
            f"decision={entry.get('decision', '?')}  "
            f"tokens={budget.get('tokens', '?')}  "
            f"wallclock={budget.get('wallclock_s', '?')}s"
        )
        detail = verdict.get("detail")
        if detail:
            print(f"  {detail}")
        # WHICH gate decided this lap. Printed because a verdict without its source is not
        # reviewable, and because provenance recorded in the ledger and shown nowhere is an
        # orphaned capability — the defect class this project keeps catching in itself. Absent for
        # runs written before the field existed, which print exactly as they did before.
        gate = entry.get("gate") or {}
        if isinstance(gate, dict) and gate.get("kind"):
            source = gate.get("source", "unknown")
            origin = gate.get("distribution") or source
            print(
                f"  gate: {gate['kind']} ({source}"
                + (f", {origin}" if origin != source else "")
                + (f") via {gate['implementation']}" if gate.get("implementation") else ")")
            )
