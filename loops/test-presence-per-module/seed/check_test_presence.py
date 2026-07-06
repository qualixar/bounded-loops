#!/usr/bin/env python3
"""
check_test_presence.py — a keyless "every module has a test file" gate.

Walks `seed/src/*.py` and, for each module `<mod>.py`, checks whether a
matching `seed/tests/test_<mod>.py` file exists. No network, no API key, no
external tool — pure standard library `pathlib` glob. A module with no test
file at all has zero verified coverage: this is the cheapest possible
structural signal that a change shipped without any test.

Exit code: 0 = every src module has a matching test file (gate passes),
1 = one or more src modules have no matching test file (gate fails),
2 = could not run.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_missing(src_dir: Path, tests_dir: Path) -> list[str]:
    missing: list[str] = []
    for src_file in sorted(src_dir.glob("*.py")):
        if src_file.name == "__init__.py":
            continue
        mod = src_file.stem
        expected_test = tests_dir / f"test_{mod}.py"
        if not expected_test.exists():
            missing.append(mod)
    return missing


def check(src_path: str, tests_path: str) -> int:
    src_dir = Path(src_path)
    tests_dir = Path(tests_path)

    if not src_dir.is_dir():
        print(f"check_test_presence: cannot run: no such directory {src_dir}", file=sys.stderr)
        return 2

    missing = _find_missing(src_dir, tests_dir)

    if missing:
        print(f"check_test_presence: {len(missing)} module(s) missing a test file:")
        for mod in missing:
            print(f"  - src/{mod}.py has no tests/test_{mod}.py")
        return 1

    print("check_test_presence: every src module has a matching test file")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_test_presence.py <src_dir> <tests_dir>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
