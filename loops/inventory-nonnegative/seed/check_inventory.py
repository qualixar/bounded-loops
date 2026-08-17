#!/usr/bin/env python3
"""
check_inventory.py — a keyless "does any SKU oversell below zero?" gate.

Replays a sequence of inventory movements against opening balances, in the
order they occur, and verifies that no SKU's running balance ever goes
negative. Pure Python standard library: no network, no API key, no external
tool. It runs anywhere Python does.

movements.json shape:
{
  "opening": {"<sku>": <int balance>, ...},
  "movements": [{"sku": "<sku>", "delta": <int>}, ...]
}

Exit code: 0 = every SKU's running balance stays >= 0 throughout the replay
(gate passes), 1 = one or more SKUs go negative at some point (gate fails),
2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(movements_path: str) -> int:
    try:
        data = json.loads(Path(movements_path).read_text(encoding="utf-8"))
        opening = dict(data["opening"])
        movements = list(data["movements"])

        # EXISTENCE OBLIGATION. A universal requirement -- "every X satisfies P" -- is
        # vacuously true over zero X, so without this the gate certifies an emptied artifact:
        # adjudicated a genuine false accept in the post-freeze corpus. The gate cannot tell an
        # intentionally-empty artifact from a destroyed one, and a gate certifying a universal
        # while unable to observe whether any subject exists certifies nothing.
        if not movements:
            print("check_inventory: movement log records no movements -- refusing to certify an empty artifact")
            return 1
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"check_inventory: cannot run: {exc}", file=sys.stderr)
        return 1  # the worker owns this artifact: a REJECT, not an inability to run

    balances: dict[str, int] = {}
    for sku, qty in opening.items():
        try:
            balances[str(sku)] = int(qty)
        except (TypeError, ValueError) as exc:
            print(f"check_inventory: malformed opening balance for {sku!r}: {exc}", file=sys.stderr)
            return 1  # the worker owns this artifact: a REJECT, not an inability to run

    violations: list[str] = []
    for idx, mv in enumerate(movements):
        try:
            sku = str(mv["sku"])
            delta = int(mv["delta"])
        except (KeyError, TypeError, ValueError) as exc:
            print(f"check_inventory: malformed movement at index {idx}: {exc}", file=sys.stderr)
            return 1  # the worker owns this artifact: a REJECT, not an inability to run

        balances[sku] = balances.get(sku, 0) + delta
        if balances[sku] < 0:
            violations.append(
                f"{sku}: balance went to {balances[sku]} after movement index "
                f"{idx} (delta={delta:+d})"
            )

    if violations:
        print(f"check_inventory: {len(violations)} oversell(s) detected:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_inventory: every SKU's balance stayed non-negative throughout the replay")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_inventory.py <movements.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
