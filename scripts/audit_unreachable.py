"""Find engine surfaces that no reachable code path can ever use.

`load_gate_plugins` was written, tested, documented and released, and nothing called it. That is
mechanically detectable, so it should never have taken a human to notice.

**Transitive, not one-hop.** The first version of this script asked "is this symbol referenced from
another module?" and reported 164 symbols, most of them fine: `cmd_graph_run` is referenced only at
`set_defaults(func=cmd_graph_run)` inside its own module, and that parser builder is called from the
CLI entry point, so it is perfectly reachable. Answering the real question means a closure:

  roots      = console-script targets, `__all__` exports, and every module's top-level references
               (module bodies execute on import, so a decorator registration is a root)
  reachable  = everything transitively referenced from those roots
  residual   = defined and never reached  <- the defect list

The residual is intended to be EMPTY, or to consist only of symbols carrying an explicit
declaration in `scripts/unreachable_allowlist.py` with a reason. A declaration is the point: the
absence of a caller becomes something a person had to write down and a reviewer can see, rather
than something nobody noticed.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "bounded_loops"


def py_files() -> list[pathlib.Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def names_in(node: ast.AST) -> set[str]:
    """Every identifier mentioned anywhere under `node`, including attribute tails.

    Attribute tails matter: `mod.helper()` should count as a reference to `helper`, because the
    import style in this package mixes `from x import y` and `import x`.
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, (ast.ImportFrom, ast.Import)):
            for alias in sub.names:
                found.add((alias.asname or alias.name).split(".")[-1])
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            # A string can be a dynamic dispatch key or a forward-ref annotation.
            token = sub.value.strip()
            if token.isidentifier():
                found.add(token)
    return found


def build() -> tuple[dict[str, set[str]], dict[str, list[tuple[str, int]]], set[str]]:
    """Return (symbol -> referenced names, symbol -> definition sites, root names)."""
    refs: dict[str, set[str]] = {}
    sites: dict[str, list[tuple[str, int]]] = {}
    roots: set[str] = set()

    for path in py_files():
        rel = str(path.relative_to(ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # Module-level references: the body minus the def/class bodies. Executed on import, so
        # anything named here is a root (this is how decorator registrations stay reachable).
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                key = stmt.name
                sites.setdefault(key, []).append((rel, stmt.lineno))
                refs.setdefault(key, set()).update(names_in(stmt) - {key})
                # A decorated top-level def is registered at import time.
                if stmt.decorator_list:
                    roots.add(key)
                # Its decorators and default values evaluate at import time.
                for dec in stmt.decorator_list:
                    roots.update(names_in(dec))
            else:
                roots.update(names_in(stmt))
    return refs, sites, roots


def declared_roots() -> set[str]:
    data = tomllib.load(open(ROOT / "pyproject.toml", "rb"))
    project = data.get("project", {})
    out: set[str] = set()
    for target in project.get("scripts", {}).values():
        out.add(target.split(":")[-1])
    for group in project.get("entry-points", {}).values():
        for target in group.values():
            out.add(target.split(":")[-1])
    init = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(init):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "__all__" for t in node.targets
        ):
            for element in getattr(node.value, "elts", []):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    out.add(element.value)
    return out


def main() -> int:
    refs, sites, import_roots = build()
    roots = import_roots | declared_roots()

    reachable: set[str] = set()
    queue = [r for r in roots]
    while queue:
        name = queue.pop()
        if name in reachable:
            continue
        reachable.add(name)
        queue.extend(refs.get(name, frozenset()) - reachable)

    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from unreachable_allowlist import ALLOWED  # type: ignore[import-not-found]
    except Exception:
        ALLOWED = {}

    residual = {
        sym: places for sym, places in sites.items()
        if sym not in reachable and not sym.startswith("_")
    }
    undeclared = {s: p for s, p in residual.items() if s not in ALLOWED}
    declared = {s: p for s, p in residual.items() if s in ALLOWED}

    print(f"defined public symbols      : {len([s for s in sites if not s.startswith('_')])}")
    print(f"reachable from roots        : {len([s for s in sites if s in reachable and not s.startswith('_')])}")
    print(f"unreachable, DECLARED       : {len(declared)}")
    print(f"unreachable, UNDECLARED     : {len(undeclared)}")

    if undeclared:
        print("\n=== UNDECLARED UNREACHABLE — each is a defect or needs a declaration ===")
        for sym in sorted(undeclared):
            for rel, line in undeclared[sym]:
                print(f"  {rel}:{line}  {sym}")
    if declared:
        print("\n=== declared unreachable (reason on record) ===")
        for sym in sorted(declared):
            print(f"  {sym}: {ALLOWED[sym]}")

    return 1 if undeclared else 0


if __name__ == "__main__":
    sys.exit(main())
