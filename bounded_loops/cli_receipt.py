"""Rendering the human-readable receipt for one persisted run.

Extracted from ``cli`` when the per-lap view began carrying gate provenance. Not a cosmetic split:
the receipt is about to grow a written artifact and its own ``bl receipt`` command, and the argparse
wiring in ``cli`` is not where that belongs. A reader asking "what exactly does a receipt claim?"
should find one file, not a region of a command dispatcher.

Reads the run directory and prints. No I/O beyond stdout, no re-running of a gate — a receipt is a
record of what already happened, and anything here that recomputed a verdict would be inventing one.
"""
from __future__ import annotations


def _attempts_consumed(entries: list) -> int:
    """Rows on which the worker was actually invoked.

    NOT ``len(entries)``, and not the last row's ``budget_spent["laps"]``. A ceiling halt records a
    final row on which no work was attempted, so counting rows reports 11 attempts against a
    declared 10 — a bound that held EXACTLY, reported as 1.1x over. `attempted` exists to make this
    computable; the receipt is the place that has to actually do it.
    """
    return sum(1 for entry in entries if entry.get("attempted", True))


def _declared(entries: list) -> dict:
    """The ceilings this run ran under, taken from the ledger rows themselves.

    Every row carries them and they are identical, so the last row is as good as the first. Read
    from the LEDGER rather than metadata.json deliberately: `bl verify` hashes the ledger and does
    not hash metadata, so a ceiling quoted from metadata could have been edited upward after the
    run and the receipt would still verify clean.
    """
    for entry in reversed(entries):
        declared = entry.get("budget_declared")
        if isinstance(declared, dict) and declared:
            return declared
    return {}


def _against(consumed: object, ceiling: object, unit: str = "") -> str:
    """One `consumed/ceiling` pair. An undeclared ceiling says so rather than printing nothing."""
    shown = "no ceiling" if ceiling is None else f"{ceiling}{unit}"
    return f"{consumed}{unit}/{shown}"


def _print_bounds_line(entries: list) -> None:
    """What the run was ALLOWED, beside what it used.

    The product is called bounded-loops and until this line existed its own receipt showed only
    consumption: `tokens=205` with nothing to read it against. A spend figure alone cannot support
    the claim the name makes.
    """
    declared = _declared(entries)
    if not declared or not entries:
        return   # a run recorded before this field existed prints exactly as it did before
    spent = entries[-1].get("budget_spent") or {}
    print(
        "bounds: "
        f"attempts {_against(_attempts_consumed(entries), declared.get('attempts'))}   "
        f"tokens {_against(spent.get('tokens', '?'), declared.get('tokens'))}   "
        f"wallclock {_against(spent.get('wallclock_s', '?'), declared.get('wallclock_s'), 's')}"
    )


def _print_run_receipt(receipt: dict) -> None:
    metadata = receipt["metadata"]
    run_id = metadata.get("run_id", "?")
    status = metadata.get("status", "UNKNOWN")
    reason = metadata.get("reason", "unknown")
    print(f"Run {run_id}: {status} ({reason})")
    print(f"Workspace: {metadata.get('workspace', 'unknown')}")
    print(f"Ledger: {metadata.get('ledger_path', 'unknown')}")
    print()
    _print_bounds_line(receipt["entries"])
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
