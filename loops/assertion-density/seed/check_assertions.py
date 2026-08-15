#!/usr/bin/env python3
"""
check_assertions.py — a keyless "every test actually checks something" gate.

Parses seed/sample_tests.py with the standard-library `ast` module (no
network, no API key, no external tool) and flags any `test_*` function that
contains zero `assert` statements — a test that runs code but never checks
its result. That shape passes every CI run regardless of whether the code
under test is broken: the single most common way a test suite silently
loses coverage without anyone noticing.

Exit code: 0 = every test_* function has at least one assert (gate passes),
1 = one or more assertion-free test functions found (gate fails),
2 = could not run.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


#: Both function shapes. An `async def test_x()` is a test pytest collects and
#: runs; matching only ast.FunctionDef let every assertion-free async test pass.
_ANY_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _has_assert(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(node, ast.Assert) for node in ast.walk(func))


def _find_assertion_free(tree: ast.Module) -> list[str]:
    assertion_free: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, _ANY_FUNC) and node.name.startswith("test_"):
            if not _has_assert(node):
                assertion_free.append(node.name)

    return assertion_free


def check(sample_path: str) -> int:
    try:
        source = Path(sample_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=sample_path)
    except (OSError, SyntaxError) as exc:
        print(f"check_assertions: cannot run: {exc}", file=sys.stderr)
        return 2

    # "Every test_* function contains an assert" is satisfied by a file containing no test_*
    # functions at all — vacuous truth, and the gate used to report it as a pass. Deleting the
    # tests is the cheapest way to make this gate agree with you, and the exact opposite of what
    # the loop asks for. Found by the held-out mutant corpus: empty and whitespace-only both passed.
    tests_found = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, _ANY_FUNC) and node.name.startswith("test_")
    )
    if tests_found == 0:
        print(
            "check_assertions: no test_* functions found. An empty module satisfies "
            "'every test asserts something' vacuously — add assertions, do not remove the tests."
        )
        return 1

    assertion_free = _find_assertion_free(tree)

    if assertion_free:
        print(f"check_assertions: {len(assertion_free)} assertion-free test(s):")
        for name in assertion_free:
            print(f"  - {name}  (no assert statement)")
        return 1

    print("check_assertions: every test_* function contains at least one assert")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_assertions.py <sample_tests.py>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
