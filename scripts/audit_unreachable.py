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


def names_in(node: ast.AST, *, strings: bool = True) -> set[str]:
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
        elif strings and isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            # A string can be a dynamic dispatch key or a forward-ref annotation. `strings=False`
            # recomputes reachability without this rule, which is how the STRING-ONLY section below
            # finds the symbols whose ONLY evidence of a caller is a literal that happens to spell
            # their name. That rule is load-bearing — it is how the P2 gate table reaches CheckovGate
            # — and it is also how a stray `x = "SomeOrphan"` anywhere in the package would silence
            # this whole audit for that symbol. Both, reported separately.
            token = sub.value.strip()
            if token.isidentifier():
                found.add(token)
    return found


def build(*, strings: bool = True) -> tuple[
    dict[str, set[str]], dict[str, list[tuple[str, int]]], set[str]
]:
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
                refs.setdefault(key, set()).update(names_in(stmt, strings=strings) - {key})
                # A decorated top-level def is registered at import time.
                if stmt.decorator_list:
                    roots.add(key)
                # Its decorators and default values evaluate at import time.
                for dec in stmt.decorator_list:
                    roots.update(names_in(dec, strings=strings))
            else:
                roots.update(names_in(stmt, strings=strings))
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


def _reached(*, strings: bool) -> tuple[set[str], dict[str, list[tuple[str, int]]]]:
    """The transitive closure from the roots, and every definition site."""
    refs, sites, import_roots = build(strings=strings)
    roots = import_roots | declared_roots()
    reachable: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in reachable:
            continue
        reachable.add(name)
        queue.extend(refs.get(name, frozenset()) - reachable)
    return reachable, sites


def main() -> int:
    reachable, sites = _reached(strings=True)
    reachable_without_strings, _ = _reached(strings=False)

    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from unreachable_allowlist import (  # type: ignore[import-not-found]
            ALLOWED,
            ALLOWED_AMBIGUOUS,
            ALLOWED_STRING_ONLY,
        )
    except Exception as broken:
        # LOUD, not empty. This used to be `except Exception: ALLOWED = {}`, so a typo in the
        # allowlist made every declaration vanish and the audit reported 68 undeclared orphans as if
        # the file had never existed — a broken gate presenting as a failing one, which at least
        # fails, and would have presented as a PASSING one the moment the residual was empty. An
        # unreadable allowlist is a broken detector and must say so.
        print(f"FATAL: allowlist unreadable — {type(broken).__name__}: {broken}", file=sys.stderr)
        return 2

    public = [name for name in sites if not name.startswith("_")]

    # BLIND SPOT 1 — the graph is keyed by UNQUALIFIED name, because an `ast.Name` does not say which
    # module's `load` it meant. Two modules defining `load` therefore share one node: their reference
    # sets merge, and if EITHER is reached both are called reachable. `evaluation/tier2.load` is
    # test-only and was reported reachable purely because `application/manifest.load` exists. The
    # detector cannot resolve this without real import resolution, so it stops pretending it has and
    # says so: every colliding public name must be declared, naming how each site is reached.
    ambiguous = {
        name: sites[name] for name in public if len(sites[name]) > 1
    }
    # BLIND SPOT 2 — see `names_in`. A symbol reached ONLY because some literal spells its name.
    string_only = {
        name: sites[name] for name in public
        if name in reachable and name not in reachable_without_strings
    }

    residual = {
        sym: places for sym, places in sites.items()
        if sym not in reachable and not sym.startswith("_")
    }
    undeclared = {s: p for s, p in residual.items() if s not in ALLOWED}
    declared = {s: p for s, p in residual.items() if s in ALLOWED}

    undeclared_ambiguous = {n: p for n, p in ambiguous.items() if n not in ALLOWED_AMBIGUOUS}
    undeclared_string_only = {n: p for n, p in string_only.items() if n not in ALLOWED_STRING_ONLY}

    print(f"defined public symbols      : {len(public)}")
    print(f"reachable from roots        : {len([s for s in public if s in reachable])}")
    print(f"unreachable, DECLARED       : {len(declared)}")
    print(f"unreachable, UNDECLARED     : {len(undeclared)}")
    print(f"ambiguous by name, DECLARED : {len(ambiguous) - len(undeclared_ambiguous)}")
    print(f"ambiguous by name, UNDECLARED: {len(undeclared_ambiguous)}")
    print(f"string-only, DECLARED       : {len(string_only) - len(undeclared_string_only)}")
    print(f"string-only, UNDECLARED     : {len(undeclared_string_only)}")

    if undeclared:
        print("\n=== UNDECLARED UNREACHABLE — each is a defect or needs a declaration ===")
        for sym in sorted(undeclared):
            for rel, line in undeclared[sym]:
                print(f"  {rel}:{line}  {sym}")
    if undeclared_ambiguous:
        print("\n=== UNDECLARED AMBIGUOUS — reachability was computed on a merged name ===")
        for sym in sorted(undeclared_ambiguous):
            for rel, line in undeclared_ambiguous[sym]:
                print(f"  {rel}:{line}  {sym}")
    if undeclared_string_only:
        print("\n=== UNDECLARED STRING-ONLY — only a literal spells this name ===")
        for sym in sorted(undeclared_string_only):
            for rel, line in undeclared_string_only[sym]:
                print(f"  {rel}:{line}  {sym}")
    if declared:
        print("\n=== declared unreachable (reason on record) ===")
        for sym in sorted(declared):
            print(f"  {sym}: {ALLOWED[sym]}")

    return 1 if (undeclared or undeclared_ambiguous or undeclared_string_only) else 0


if __name__ == "__main__":
    sys.exit(main())
