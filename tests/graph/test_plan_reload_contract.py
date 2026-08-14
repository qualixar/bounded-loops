"""Every surface that reloads a run must admit the same loop packages.

`load_plan_from_run_dir(package_digests=...)` defaults to the empty set, which means "admit
no loop packages". Forgetting the argument therefore does not raise — it produces a confident,
specific, wrong refusal:

    error: graph status: cannot reconstruct plan — package_unavailable at
    /nodes/check-tests-exist/loop_package: package digest is not admitted

Four surfaces forgot it, so `bl graph status`, `bl graph metrics`, the console and the SSE
watcher all told the operator that a run they had just watched succeed referenced packages
that were never admitted — while the receipt log held `node.succeeded` for those exact
digests. Approve, arena and the MCP surface passed it and worked, which is what made the
disagreement so hard to read: the same run was fine through one door and impossible through
another.

An AST check rather than a grep, because the failing form and the correct form differ only by
a keyword argument.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "bounded_loops"

LOADER = "load_plan_from_run_dir"
REQUIRED_KEYWORD = "package_digests"


def _calls_missing_the_keyword(path: Path) -> list[int]:
    """Line numbers where `LOADER` is called without `package_digests`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != LOADER:
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        # `**kwargs` forwarding (arg is None) counts as passing it along.
        if REQUIRED_KEYWORD not in keywords and None not in keywords:
            missing.append(node.lineno)
    return missing


def test_every_caller_admits_the_installed_loop_packages() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for line in _calls_missing_the_keyword(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")

    assert not offenders, (
        "these calls reload a run without admitting any loop package, so they will refuse a "
        "valid run with 'package digest is not admitted':\n  "
        + "\n  ".join(offenders)
        + "\n\nPass package_digests=admitted_loop_package_digests()."
    )


def test_the_guard_can_actually_SEE_a_bad_call(tmp_path: Path) -> None:
    """The check above passes trivially if the AST walk matches nothing."""
    good = tmp_path / "good.py"
    good.write_text(
        "load_plan_from_run_dir(run_dir, package_digests=admitted_loop_package_digests())\n",
        encoding="utf-8",
    )
    bad = tmp_path / "bad.py"
    bad.write_text("load_plan_from_run_dir(run_dir)\n", encoding="utf-8")
    attribute_style = tmp_path / "attr.py"
    attribute_style.write_text("mod.load_plan_from_run_dir(run_dir)\n", encoding="utf-8")

    assert _calls_missing_the_keyword(good) == []
    assert _calls_missing_the_keyword(bad) == [1]
    assert _calls_missing_the_keyword(attribute_style) == [1]
