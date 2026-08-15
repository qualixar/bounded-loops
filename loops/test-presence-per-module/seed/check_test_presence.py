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

import ast
import sys
from pathlib import Path


def _imported_from(tree: ast.Module, module: str) -> set[str] | None:
    """Names this test file imports FROM `module`, or None if it never imports it.

    Accepts the three forms a test can reach a sibling package by: `from src.<mod> import x`,
    `from <mod> import x`, and `import src.<mod>`. The last carries no names, so it yields an
    empty set — imported, nothing named.
    """
    found: set[str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (module, f"src.{module}"):
            found = (found or set()) | {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in (module, f"src.{module}"):
                    found = found or set()
    return found


def _test_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _why_this_is_not_a_real_test(module: str, path: Path, src_file: Path) -> str | None:
    """Why this file does not actually exercise `module`, or None if it does.

    PROMPT.md asks for "at least one real test that imports the module and asserts on its actual
    behavior — not an empty file and not a test that imports nothing and asserts nothing", and
    forbids "an empty or no-op test file just to satisfy the file-presence check".

    Existence alone was the original check, and an empty file passed it. Requiring `def test_`
    somewhere in the text was the first fix, and a held-out corpus authored from the stated purpose
    walked straight through it four different ways: a test function with no import and no assert;
    a test for module `a` that imports module `b`; a test that imports the module and then asserts
    on a standalone calculation that never calls it; and a rename in the SOURCE that leaves the
    test importing a function no longer there.

    Each is a file-presence check passing on a file that verifies nothing — the same vacuity as an
    empty file, dressed to look like a test. So the checks below are the stated purpose read
    literally: imports the module, uses what it imported, asserts something, and the thing it
    imported exists.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "cannot be read"

    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Not a parse-or-pass decision: a file that is not valid Python cannot be a test, and
        # reporting it as one is exactly the failure this gate exists to prevent.
        return "is not valid Python"

    tests = _test_functions(tree)
    if not tests:
        return "defines no test function"

    imported = _imported_from(tree, module)
    if imported is None:
        return f"never imports src/{module}.py, so it cannot be exercising it"

    asserting = [t for t in tests if any(isinstance(n, ast.Assert) for n in ast.walk(t))]
    if not asserting:
        return "has a test function with no assertion, which verifies nothing"

    if imported:
        used = {
            node.id
            for test in asserting
            for node in ast.walk(test)
            if isinstance(node, ast.Name)
        } | {
            node.attr
            for test in asserting
            for node in ast.walk(test)
            if isinstance(node, ast.Attribute)
        }
        if not (imported & used):
            return (
                f"imports {sorted(imported)} from src/{module}.py but never uses it in an "
                "asserting test, so it asserts on something else"
            )

        # The relation, not just the file. A rename in the SOURCE leaves this test importing a
        # name that is gone; the test file is untouched and still looks complete.
        try:
            src_tree = ast.parse(src_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            return f"src/{module}.py cannot be parsed, so nothing can be exercising it"
        defined = {
            node.name
            for node in ast.walk(src_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | {
            target.id
            for node in ast.walk(src_tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        absent = sorted(name for name in imported if name not in defined)
        if absent:
            return f"imports {absent} from src/{module}.py, which does not define {absent}"

    return None


def _find_missing(src_dir: Path, tests_dir: Path) -> list[str]:
    missing: list[str] = []
    for src_file in sorted(src_dir.glob("*.py")):
        if src_file.name == "__init__.py":
            continue
        mod = src_file.stem
        expected_test = tests_dir / f"test_{mod}.py"
        if not expected_test.exists():
            missing.append(f"src/{mod}.py has no tests/test_{mod}.py")
            continue
        reason = _why_this_is_not_a_real_test(mod, expected_test, src_file)
        if reason is not None:
            missing.append(f"tests/test_{mod}.py {reason}")
    return missing


def check(src_path: str, tests_path: str) -> int:
    src_dir = Path(src_path)
    tests_dir = Path(tests_path)

    if not src_dir.is_dir():
        print(f"check_test_presence: cannot run: no such directory {src_dir}", file=sys.stderr)
        return 2

    missing = _find_missing(src_dir, tests_dir)

    if missing:
        print(f"check_test_presence: {len(missing)} module(s) without a real test:")
        for problem in missing:
            print(f"  - {problem}")
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
