#!/usr/bin/env python3
"""
check_expenses.py — a keyless "does every expense comply with policy?" gate.

Verifies that every line in an expense report is in an allowed category and
does not exceed that category's per-line cap. The policy file is ground
truth; the EXPENSES must conform to it, never the other way round.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every expense complies (gate passes), 1 = one or more
expenses violate policy (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(expenses_path: str, policy_path: str) -> int:
    try:
        expenses = json.loads(Path(expenses_path).read_text(encoding="utf-8"))
        policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        allowed = set(policy["allowed_categories"])
        caps = policy["caps"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"check_expenses: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(expenses, list) or not expenses:
        print("check_expenses: expense report has no lines", file=sys.stderr)
        return 2

    violations: list[str] = []
    for idx, line in enumerate(expenses):
        try:
            category = line["category"]
            amount = float(line["amount"])
            description = line.get("description", "")
        except (KeyError, TypeError, ValueError) as exc:
            print(f"check_expenses: cannot run: malformed line {line!r}: {exc}", file=sys.stderr)
            return 2

        if category not in allowed:
            violations.append(
                f"line {idx} ({description!r}): category {category!r} is not allowed "
                f"(allowed={sorted(allowed)})"
            )
            continue

        cap = float(caps.get(category, 0))
        if amount > cap:
            violations.append(
                f"line {idx} ({description!r}): amount={amount} exceeds "
                f"{category!r} cap of {cap}"
            )

    if violations:
        print(f"check_expenses: {len(violations)} policy violation(s) found:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_expenses: every expense line complies with policy")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_expenses.py <expenses.json> <policy.json>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
