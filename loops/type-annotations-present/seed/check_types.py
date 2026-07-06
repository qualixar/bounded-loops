#!/usr/bin/env python3
"""
check_types.py — a keyless "does every public function fully annotate its
contract?" gate.

Parses a Python module with the standard library `ast` module and flags any
top-level, public (name not starting with `_`) function definition that is
missing a type annotation on any parameter or on its return type. An
unannotated public function is an undocumented contract: callers and static
analyzers can't know what it expects or returns.

Pure Python standard library (`ast`, no mypy/pyright): no network, no
pip-installed type checker, no key. Runs anywhere Python does.

Scope (deliberately narrow, matching the loop's contract):
  - Only TOP-LEVEL `def`/`async def` statements are checked (methods nested
    inside a class or another function are out of scope for this gate).
  - A function is "public" if its name does not start with `_`.
  - `self`/`cls` are not applicable at module top level, so no special-casing
    is needed here.
  - `*args`/`**kwargs` (if present) must also be annotated to count as fully
    annotated.

Exit code: 0 = every public top-level function is fully annotated (gate
passes), 1 = one or more are missing an annotation (gate fails), 2 = could
not run.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _missing_annotations(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return a list of human-readable reasons this function is under-annotated."""
    reasons: list[str] = []

    all_params = [
        *fn.args.posonlyargs,
        *fn.args.args,
        *fn.args.kwonlyargs,
    ]
    for param in all_params:
        if param.annotation is None:
            reasons.append(f"parameter '{param.arg}' has no type annotation")

    if fn.args.vararg is not None and fn.args.vararg.annotation is None:
        reasons.append(f"*{fn.args.vararg.arg} has no type annotation")
    if fn.args.kwarg is not None and fn.args.kwarg.annotation is None:
        reasons.append(f"**{fn.args.kwarg.arg} has no type annotation")

    if fn.returns is None:
        reasons.append("return type has no annotation")

    return reasons


def check(module_path: str) -> int:
    try:
        source = Path(module_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=module_path)
    except (OSError, SyntaxError) as exc:
        print(f"check_types: cannot run: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        reasons = _missing_annotations(node)
        for reason in reasons:
            violations.append(f"'{node.name}' (line {node.lineno}): {reason}")

    if violations:
        print(f"check_types: {len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_types: every public top-level function is fully annotated")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_types.py <module.py>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
