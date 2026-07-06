#!/usr/bin/env python3
"""
check_pins.py — a keyless "every dependency is pinned to an exact version" gate.

Pure Python standard library: no network, no API key, no external tool.
Every non-comment, non-blank line of a requirements.txt-style file must pin
an exact version with `==`. Bare names, or ranges using `>=`/`<=`/`~=`/`>`/
`<`/`!=`, are rejected as unpinned — an unpinned dependency can silently
pull in a new, unreviewed, possibly-compromised release.

Exit code: 0 = every dependency is exactly pinned (gate passes), 1 = one or
more unpinned dependencies (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_EXACT_PIN_RE = re.compile(r"^[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-]+$")


def check(path: str) -> int:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"check_pins: cannot run: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _EXACT_PIN_RE.match(line):
            violations.append(line)

    if violations:
        print(f"check_pins: {len(violations)} unpinned dependenc{'y' if len(violations) == 1 else 'ies'}:")
        for v in violations:
            print(f"  - {v}  (must pin an exact version with ==)")
        return 1

    print("check_pins: every dependency is pinned to an exact version")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_pins.py <requirements_file>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
