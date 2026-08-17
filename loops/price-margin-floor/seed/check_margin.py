#!/usr/bin/env python3
"""
check_margin.py — a keyless "is every SKU priced above its margin floor?" gate.

Verifies that every catalog item's price satisfies the minimum-margin policy:
    price >= cost * (1 + min_margin)

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does. policy.json is the ground truth for the margin
floor; the CATALOG must conform to it, never the other way round.

Exit code: 0 = every SKU meets or exceeds the margin floor (gate passes),
1 = one or more SKUs are priced below the floor (gate fails),
2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(catalog_path: str, policy_path: str) -> int:
    # Read in two steps: the two inputs have different owners, and a shared `try` cannot tell them
    # apart. `policy.json` is in this loop's `forbid` list — the worker may not edit it, so a
    # problem with it means this gate cannot run and no retry will help. `catalog.json` is the
    # worker's output, and anything wrong with it is a verdict the worker can act on.
    try:
        policy = _load(policy_path)
        min_margin = float(policy["min_margin"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"check_margin: cannot run: policy {policy_path}: {exc}", file=sys.stderr)
        return 2

    try:
        catalog = _load(catalog_path)

        # EXISTENCE OBLIGATION. A universal requirement -- "every X satisfies P" -- is
        # vacuously true over zero X, so without this the gate certifies an emptied artifact:
        # adjudicated a genuine false accept in the post-freeze corpus. The gate cannot tell an
        # intentionally-empty artifact from a destroyed one, and a gate certifying a universal
        # while unable to observe whether any subject exists certifies nothing.
        if not catalog:
            print("check_margin: retail catalog lists no SKUs -- refusing to certify an empty artifact")
            return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"check_margin: {catalog_path} is not readable JSON ({exc}) — the catalog must be a "
            "JSON list of items with a price and a cost",
            file=sys.stderr,
        )
        return 1

    if not isinstance(catalog, list):
        print("check_margin: catalog.json must be a list of items", file=sys.stderr)
        return 1  # the worker owns this artifact: a REJECT, not an inability to run

    violations: list[str] = []
    for item in catalog:
        try:
            sku = str(item["sku"])
            cost = float(item["cost"])
            price = float(item["price"])
        except (KeyError, TypeError, ValueError) as exc:
            print(f"check_margin: malformed catalog item {item!r}: {exc}", file=sys.stderr)
            return 1  # the worker owns this artifact: a REJECT, not an inability to run

        floor = cost * (1 + min_margin)
        if price < floor:
            violations.append(
                f"{sku}: price={price:.2f} is below the margin floor "
                f"{floor:.2f} (cost={cost:.2f}, min_margin={min_margin:.2%})"
            )

    if violations:
        print(f"check_margin: {len(violations)} SKU(s) below the margin floor:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_margin: every SKU meets or exceeds the margin floor")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_margin.py <catalog.json> <policy.json>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
