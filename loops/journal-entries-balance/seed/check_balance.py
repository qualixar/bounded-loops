#!/usr/bin/env python3
"""
check_balance.py — a keyless "does every journal entry balance?" gate.

Verifies that for every entry in a double-entry journal, the sum of its
debit lines equals the sum of its credit lines (the fundamental invariant
of double-entry bookkeeping). An unbalanced entry fails the gate.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every entry balances (gate passes), 1 = one or more entries
are unbalanced (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_TOLERANCE = 1e-6


def check(journal_path: str) -> int:
    try:
        entries = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_balance: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(entries, list) or not entries:
        print("check_balance: journal has no entries", file=sys.stderr)
        return 2

    violations: list[str] = []
    for entry in entries:
        try:
            entry_id = entry["entry_id"]
            lines = entry["lines"]
            total_debit = sum(float(line["debit"]) for line in lines)
            total_credit = sum(float(line["credit"]) for line in lines)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"check_balance: cannot run: malformed entry {entry!r}: {exc}", file=sys.stderr)
            return 2

        if abs(total_debit - total_credit) > _TOLERANCE:
            violations.append(
                f"{entry_id}: total_debit={total_debit} total_credit={total_credit}"
            )

    if violations:
        print(f"check_balance: {len(violations)} unbalanced entry(ies):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_balance: every journal entry balances")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_balance.py <journal.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
