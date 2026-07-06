#!/usr/bin/env python3
"""
check_gtin.py — a keyless "is this GTIN-13 check digit valid?" gate.

Verifies the mod-10 (Luhn-style GS1) check digit of every product's GTIN-13
in products.json. Weights alternate 1,3,1,3,... across the first 12 digits
(left to right), and the 13th digit must equal
(10 - (weighted_sum mod 10)) mod 10.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does.

Exit code: 0 = every GTIN-13's check digit is valid (gate passes),
1 = one or more GTINs have an invalid check digit (gate fails),
2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _expected_check_digit(digits12: str) -> int:
    total = 0
    for i, ch in enumerate(digits12):
        weight = 3 if i % 2 == 1 else 1
        total += int(ch) * weight
    return (10 - (total % 10)) % 10


def check(products_path: str) -> int:
    try:
        products = json.loads(Path(products_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_gtin: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(products, list):
        print("check_gtin: products.json must be a list of items", file=sys.stderr)
        return 2

    violations: list[str] = []
    for item in products:
        try:
            sku = str(item["sku"])
            gtin = str(item["gtin"])
        except (KeyError, TypeError) as exc:
            print(f"check_gtin: malformed product item {item!r}: {exc}", file=sys.stderr)
            return 2

        if len(gtin) != 13 or not gtin.isdigit():
            violations.append(f"{sku}: gtin {gtin!r} is not a 13-digit numeric string")
            continue

        expected = _expected_check_digit(gtin[:12])
        actual = int(gtin[12])
        if actual != expected:
            violations.append(
                f"{sku}: gtin {gtin} has check digit {actual}, expected {expected}"
            )

    if violations:
        print(f"check_gtin: {len(violations)} invalid GTIN(s):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_gtin: every GTIN-13 check digit is valid")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_gtin.py <products.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
