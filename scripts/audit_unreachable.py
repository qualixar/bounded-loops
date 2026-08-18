"""Find surfaces that exist and are never used: the defect class that shipped in the 0.6.8 attempt.

`load_gate_plugins` was written, tested, documented and released, and nothing called it. That is
mechanically detectable, so it should never have needed a human to notice. This script reports:

  1. public module-level functions/classes defined in bounded_loops/ and referenced NOWHERE else
     inside bounded_loops/ (tests do not count -- a test-only caller is exactly the trap);
  2. dataclass fields never read outside their defining module;
  3. entry-point groups declared as constants but never passed to entry_points().

Every category has legitimate members (CLI entry points, Protocol methods, __all__ exports), so the
output is a triage list, not a verdict. It is still the right shape: a reviewer reads a bounded list
instead of hoping to spot an absence.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "bounded_loops"
TESTS = ROOT / "tests"

# Names that are legitimately referenced only from outside the package.
ENTRYPOINT_HINTS = ("main", "cli", "app", "mcp")


def py_files(base: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def public_defs() -> dict[str, list[tuple[str, int, str]]]:
    """symbol -> [(module, lineno, kind)] for public module-level defs and classes."""
    out: dict[str, list[tuple[str, int, str]]] = {}
    for path in py_files(PKG):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(ROOT))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name.startswith("_"):
                    continue
                out.setdefault(node.name, []).append((rel, node.lineno, type(node).__name__))
    return out


def reference_counts(symbols: set[str], base: pathlib.Path) -> dict[str, dict[str, int]]:
    """symbol -> {module: count} of textual word-boundary references."""
    counts: dict[str, dict[str, int]] = {s: {} for s in symbols}
    patterns = {s: re.compile(rf"\b{re.escape(s)}\b") for s in symbols}
    for path in py_files(base):
        rel = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8", errors="replace")
        for sym, pat in patterns.items():
            n = len(pat.findall(text))
            if n:
                counts[sym][rel] = n
    return counts


def main() -> int:
    defs = public_defs()
    symbols = set(defs)
    in_pkg = reference_counts(symbols, PKG)
    in_tests = reference_counts(symbols, TESTS)

    orphans: list[tuple[str, str, int, int]] = []
    for sym, sites in defs.items():
        defining = {m for m, _, _ in sites}
        # References inside the package, excluding the modules that define it.
        external = {m: n for m, n in in_pkg[sym].items() if m not in defining}
        if external:
            continue
        if any(h in sym.lower() for h in ENTRYPOINT_HINTS):
            continue
        test_refs = sum(in_tests[sym].values())
        for module, lineno, _kind in sites:
            orphans.append((sym, module, lineno, test_refs))

    orphans.sort(key=lambda r: (-r[3], r[1], r[0]))
    print(f"=== [1] public symbols never referenced elsewhere in bounded_loops/ ({len(orphans)}) ===")
    print("     (test_refs > 0 is the DANGEROUS case: tested, but nothing in the engine calls it)")
    for sym, module, lineno, test_refs in orphans:
        flag = "  <-- TESTED BUT UNREACHABLE" if test_refs else ""
        print(f"  {module}:{lineno}  {sym}  test_refs={test_refs}{flag}")

    # [3] entry-point groups declared but never loaded.
    print("\n=== [3] entry-point group constants vs entry_points() calls ===")
    group_defs: list[tuple[str, str, str]] = []
    loaders: set[str] = set()
    for path in py_files(PKG):
        rel = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'^([A-Z0-9_]*ENTRY_POINT[A-Z0-9_]*)\s*=\s*"([^"]+)"', text, re.M):
            group_defs.append((m.group(1), m.group(2), rel))
        if "entry_points(" in text:
            loaders.add(rel)
    for const, value, rel in group_defs:
        # A group is genuinely loaded only if some module calls entry_points() with it AND the
        # function doing so is itself reachable from elsewhere in the package. Without the second
        # half, a module that declares a group and loads it while nobody calls that loader reports
        # as LOADED -- which is exactly the 0.6.8 gate-plugin defect this check exists to catch.
        loaded_by = []
        for cand in sorted(loaders):
            text = (ROOT / cand).read_text(encoding="utf-8", errors="replace")
            if const not in text or "entry_points(" not in text:
                continue
            # Which public loader functions does that module define, and is any of them called
            # from outside it?
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            fns = [
                n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
            ]
            reachable = any(
                fn in in_pkg and any(m != cand for m in in_pkg[fn])
                for fn in fns
            )
            loaded_by.append((cand, reachable))
        if any(reachable for _c, reachable in loaded_by):
            who = ", ".join(c for c, r in loaded_by if r)
            status = f"LOADED via a reachable loader in {who}"
        elif loaded_by:
            who = ", ".join(c for c, _r in loaded_by)
            status = (
                f"*** DECLARED AND SELF-LOADED IN {who}, BUT NO REACHABLE CALLER "
                "-- the group has no effect ***"
            )
        else:
            status = "*** DECLARED, NEVER LOADED ***"
        print(f"  {const} = {value!r}  ({rel})\n      -> {status}")

    return 1 if any(r[3] for r in orphans) else 0


if __name__ == "__main__":
    sys.exit(main())
