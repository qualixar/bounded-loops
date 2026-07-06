#!/usr/bin/env python3
"""
check_fx.py — a keyless "are these FX rates internally consistent?" gate.

Verifies three invariants over a table of currency-pair rates:
  1. every rate is strictly positive;
  2. the base-to-base rate ("<base>/<base>") is exactly 1;
  3. for every pair "A/B" that has an inverse "B/A" in the table, the two
     rates multiply to (approximately) 1 — the no-arbitrage identity.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every check passes (gate passes), 1 = one or more
contradictions found (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_TOLERANCE = 1e-6


def check(rates_path: str) -> int:
    try:
        data = json.loads(Path(rates_path).read_text(encoding="utf-8"))
        base = data["base"]
        pairs = data["pairs"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"check_fx: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(pairs, dict) or not pairs:
        print("check_fx: rates has no pairs", file=sys.stderr)
        return 2

    violations: list[str] = []

    for pair, rate in pairs.items():
        try:
            rate_f = float(rate)
        except (TypeError, ValueError):
            violations.append(f"{pair}: rate {rate!r} is not numeric")
            continue
        if rate_f <= 0:
            violations.append(f"{pair}: rate {rate_f} is not strictly positive")

    base_pair = f"{base}/{base}"
    if base_pair in pairs:
        base_rate = float(pairs[base_pair])
        if abs(base_rate - 1.0) > _TOLERANCE:
            violations.append(f"{base_pair}: base-to-base rate is {base_rate}, expected 1.0")
    else:
        violations.append(f"{base_pair}: base-to-base pair is missing from pairs")

    checked_inverses: set[frozenset[str]] = set()
    for pair, rate in pairs.items():
        if "/" not in pair:
            continue
        a, b = pair.split("/", 1)
        inverse_key = f"{b}/{a}"
        pair_key = frozenset({pair, inverse_key})
        if a == b or inverse_key not in pairs or pair_key in checked_inverses:
            continue
        checked_inverses.add(pair_key)
        try:
            product = float(rate) * float(pairs[inverse_key])
        except (TypeError, ValueError):
            continue
        if abs(product - 1.0) > _TOLERANCE:
            violations.append(
                f"{pair} * {inverse_key} = {product}, expected ~1.0 (no-arbitrage violation)"
            )

    if violations:
        print(f"check_fx: {len(violations)} contradiction(s) found:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_fx: all rates positive, base normalized, and inverse pairs consistent")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_fx.py <rates.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
