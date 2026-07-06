#!/usr/bin/env python3
"""
check_test_names.py — a keyless "every test function is discoverable" gate.

Parses seed/sample_tests.py with the standard-library `ast` module (no
network, no API key, no external tool) and flags any function that is clearly
INTENDED as a test but does not start with `test_` — the classic silent-skip
failure: a pytest-style runner never collects a misnamed test, so a broken
assertion inside it never runs and nobody notices.

A function is considered "intended as a test" if either:
  - it is a method defined directly inside a class whose name starts with
    `Test` (mirrors pytest's `TestFoo` class-collection rule), or
  - it is a module-level function whose body contains at least one `assert`
    statement (an assertion-bearing top-level function is a de facto test
    even if pytest would never collect it under this name).

Exit code: 0 = every such function is named `test_*` (gate passes),
1 = one or more misnamed test functions found (gate fails),
2 = could not run.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _has_assert(func: ast.FunctionDef) -> bool:
    return any(isinstance(node, ast.Assert) for node in ast.walk(func))


def _find_misnamed(tree: ast.Module) -> list[str]:
    misnamed: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and not item.name.startswith("test_"):
                    misnamed.append(f"{node.name}.{item.name}")

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("test_"):
            if _has_assert(node):
                misnamed.append(node.name)

    return misnamed


def check(sample_path: str) -> int:
    try:
        source = Path(sample_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=sample_path)
    except (OSError, SyntaxError) as exc:
        print(f"check_test_names: cannot run: {exc}", file=sys.stderr)
        return 2

    misnamed = _find_misnamed(tree)

    if misnamed:
        print(f"check_test_names: {len(misnamed)} misnamed test function(s):")
        for name in misnamed:
            print(f"  - {name}  (does not start with 'test_')")
        return 1

    print("check_test_names: every test function is named test_*")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_test_names.py <sample_tests.py>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
