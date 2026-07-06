#!/usr/bin/env python3
"""
check_imports.py — a keyless "does this module import anything it never
uses?" gate.

Parses a Python module with the standard library `ast` module and flags any
imported name that is never referenced anywhere else in the source. Unused
imports are dead weight: they slow down import time, confuse readers about
real dependencies, and often signal a half-finished refactor.

Pure Python standard library (`ast`, no external linter): no network, no
pip-installed linter, no key. Runs anywhere Python does.

Handles:
  - `import x` / `import x as y` — the bound name is `y` if aliased, else `x`
    (or the top-level package of a dotted import, e.g. `import a.b` binds `a`).
  - `from pkg import x` / `from pkg import x as y` — the bound name is `y` if
    aliased, else `x`. `from pkg import *` is ignored (can't statically know
    what it introduces).
  - A name is "used" if it appears as a `Name`/`Attribute` load anywhere in
    the module outside the import statements themselves.

Exit code: 0 = no unused imports (gate passes), 1 = one or more unused
imports found (gate fails), 2 = could not run.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _bound_name(alias: ast.alias, is_from: bool) -> str:
    if alias.asname:
        return alias.asname
    if is_from:
        return alias.name
    # `import a.b.c` binds the top-level name `a` in the local namespace.
    return alias.name.split(".")[0]


def _collect_imports(tree: ast.AST) -> dict[str, int]:
    """Map bound import name -> line number, skipping `import *`."""
    imports: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[_bound_name(alias, is_from=False)] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                # `from __future__ import annotations` (etc.) changes
                # interpreter behavior at compile time; it has no runtime
                # name to "use" and is never dead code.
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                imports[_bound_name(alias, is_from=True)] = node.lineno
    return imports


def _collect_used_names(tree: ast.AST) -> set[str]:
    """All Name loads and the root of Attribute chains, module-wide."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
    return used


def check(module_path: str) -> int:
    try:
        source = Path(module_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=module_path)
    except (OSError, SyntaxError) as exc:
        print(f"check_imports: cannot run: {exc}", file=sys.stderr)
        return 2

    imports = _collect_imports(tree)
    used = _collect_used_names(tree)

    unused = sorted(
        ((name, line) for name, line in imports.items() if name not in used),
        key=lambda pair: pair[1],
    )

    if unused:
        print(f"check_imports: {len(unused)} unused import(s):")
        for name, line in unused:
            print(f"  - '{name}' imported at line {line} but never referenced")
        return 1

    print("check_imports: every import is referenced")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_imports.py <module.py>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
