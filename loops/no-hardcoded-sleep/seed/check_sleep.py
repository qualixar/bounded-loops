#!/usr/bin/env python3
"""
check_sleep.py — a keyless "no hardcoded sleeps in tests" gate.

Parses seed/sample_tests.py with the standard-library `ast` module (no
network, no API key, no external tool) and flags any call to `time.sleep(...)`
(or a bare `sleep(...)` imported via `from time import sleep`) inside test
code. A hardcoded sleep is the most common source of flaky-and-slow test
suites: it guesses how long an async operation takes instead of polling a
real completion signal, so it is simultaneously too short (flaky under load)
and too long (slow in the common case).

Exit code: 0 = no hardcoded sleep calls found (gate passes),
1 = one or more `time.sleep(...)` calls found (gate fails),
2 = could not run.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _call_name(node: ast.Call) -> str | None:
    """Return 'time.sleep' or 'sleep' style dotted name for a call, else None."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "sleep":
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.sleep"
        return "sleep"
    if isinstance(func, ast.Name) and func.id == "sleep":
        return "sleep"
    return None


def _find_sleep_calls(tree: ast.Module) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name is not None:
                hits.append((name, node.lineno))
    return hits


def check(sample_path: str) -> int:
    try:
        source = Path(sample_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=sample_path)
    except (OSError, SyntaxError) as exc:
        print(f"check_sleep: cannot run: {exc}", file=sys.stderr)
        return 2

    hits = _find_sleep_calls(tree)

    if hits:
        print(f"check_sleep: {len(hits)} hardcoded sleep call(s) found:")
        for name, lineno in hits:
            print(f"  - {name}(...) at line {lineno}  (hardcoded sleep in test code)")
        return 1

    print("check_sleep: no hardcoded sleep calls in test code")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_sleep.py <sample_tests.py>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
