#!/usr/bin/env python3
"""
check_okrs.py — a keyless "are these OKRs actually measurable?" gate.

Verifies that every key result in an OKR document carries a numeric
`target`, a non-empty `unit`, and a `deadline` in YYYY-MM-DD form. A key
result that just says "increase conversion" with no number, no unit, and
no date is not measurable — it's a wish, not an OKR. This is the exact
failure behind vague, unaccountable OKRs that nobody can grade at
quarter-end.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every key result is measurable (gate passes), 1 = one or
more key results are vague (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_DEADLINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_vague(kr: dict) -> list[str]:
    """Return a list of reasons this key result is not measurable."""
    reasons: list[str] = []

    target = kr.get("target")
    if target is None or not isinstance(target, (int, float)) or isinstance(target, bool):
        reasons.append("no numeric target")

    unit = kr.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        reasons.append("no unit")

    deadline = kr.get("deadline")
    if not isinstance(deadline, str) or not _DEADLINE_RE.match(deadline):
        reasons.append("no deadline in YYYY-MM-DD form")

    return reasons


def check(okrs_path: str) -> int:
    try:
        data = json.loads(Path(okrs_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_okrs: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, list) or not data:
        print("check_okrs: expected a non-empty JSON list of objectives", file=sys.stderr)
        return 2

    violations: list[str] = []
    for objective in data:
        obj_text = objective.get("objective", "<missing objective>")
        key_results = objective.get("key_results", [])
        for kr in key_results:
            reasons = _is_vague(kr)
            if reasons:
                kr_text = kr.get("text", "<missing text>")
                violations.append(
                    f"[{obj_text}] '{kr_text}': {', '.join(reasons)}"
                )

    if violations:
        print(f"check_okrs: {len(violations)} vague key result(s):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_okrs: every key result has a numeric target, a unit, and a deadline")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_okrs.py <okrs.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
