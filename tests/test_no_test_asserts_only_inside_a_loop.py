"""A test whose every assertion sits inside a loop over a DISCOVERED collection passes when that
collection is empty — for any reason, including the discovery silently breaking.

This is the same defect the mutant corpus hunts in gates: *"nothing to check" reported as
"passed"*. It was found in this repository's own suite by stubbing `mcp_authoring.register` to a
no-op and re-running. Three guards still passed, two of them security guards:

* `test_NO_tool_accepts_a_subject_identity_argument` — the guard stopping a model from attributing
  its own decision to a human, satisfied by there being no tools;
* `test_no_tool_takes_a_filesystem_path_for_a_run` — the traversal guard, likewise;
* `test_no_discovery_tool_accepts_anything_secret_shaped` — the credential-channel guard, likewise.

A fourth, `test_the_local_tenant_sentinels_are_declared_exactly_once`, was named for a property it
never checked: it asserted the sentinels were not declared TWICE and would have passed if they were
declared ZERO times.

**What this asserts.** Any test function whose assertions are ALL inside a loop over a discovered
collection — a glob, a comprehension, a `findall`, a dict walk — must first prove that collection
is non-empty. "Prove" means an assertion outside the loop: a length floor, a membership check, a
counter checked afterwards.

**Why the rule is shaped this way.** A loop over a literal cannot silently empty, so it is exempt.
A loop over `Path.glob(...)`, `re.findall(...)` or a registry populated by a function call can, and
those are exactly the loops that keep passing after the thing they walk has gone.

**This is a heuristic and it is allowed to be.** It reads syntax, not meaning, so it will
occasionally ask for a guard where the collection is provably non-empty by other means. That guard
costs one line and documents the assumption. The failure it prevents costs a green suite that
checks nothing, which this repository has now shipped four times.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent

#: Calls whose result can be empty without anything being obviously wrong. A loop over one of these
#: is a loop over whatever happened to be found.
_DISCOVERY_CALLS = frozenset({
    "glob", "rglob", "iterdir", "walk", "findall", "finditer",
    "keys", "values", "items", "splitlines", "split",
})

def _iterates_a_discovered_collection(iter_node: ast.AST) -> bool:
    """Whether this `for`'s iterable could be empty because discovery came up short.

    Wrappers like `sorted(...)` and `list(...)` are NOT evidence by themselves — the question is
    what they wrap, and the walk below reaches it either way. Treating them as evidence flagged
    `for name in sorted(_MUTATING)`, a loop over a module-level literal set that cannot empty and
    whose test was separately proven to fail on an empty registry. A detector that cries wolf on
    provably-safe code gets suppressed, and then it is not a detector.
    """
    for node in ast.walk(iter_node):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _DISCOVERY_CALLS:
                return True
    return False


#: Assertions that are TRUE OF THE EMPTY CASE. A test whose every assertion is one of these, over a
#: collection built by scanning something, passes when the scan found nothing.
def _is_satisfied_by_emptiness(node: ast.Assert) -> bool:
    """`assert not x`, `assert x == []`, `assert len(x) == 0`, `assert x == set()`."""
    test = node.test
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
        right = test.comparators[0]
        if isinstance(right, (ast.List, ast.Set, ast.Dict, ast.Tuple)) and not getattr(
            right, "elts", getattr(right, "keys", [])
        ):
            return True
        if isinstance(right, ast.Constant) and right.value == 0:
            return True
        if isinstance(right, ast.Call) and isinstance(right.func, ast.Name):
            if right.func.id in {"set", "list", "dict", "tuple", "frozenset"} and not right.args:
                return True
    return False


def _unguarded_tests(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not function.name.startswith("test_"):
            continue

        asserts = [n for n in ast.walk(function) if isinstance(n, ast.Assert)]
        if not asserts:
            continue

        loops = [n for n in ast.walk(function) if isinstance(n, (ast.For, ast.AsyncFor))]
        if not any(_iterates_a_discovered_collection(loop.iter) for loop in loops):
            continue

        inside = {
            id(node)
            for loop in loops
            for node in ast.walk(loop)
            if isinstance(node, ast.Assert)
        }

        # SHAPE 1 — every assertion is inside the loop. No iteration, no assertion.
        if inside and len(inside) == len(asserts):
            offenders.append(f"{path.relative_to(_TESTS)}:{function.lineno} {function.name}")
            continue

        # SHAPE 2 — accumulate-then-assert-empty. The assertions sit OUTSIDE the loop and look
        # unconditional, but every one of them is true of the empty case, so a scan that matched
        # nothing satisfies them all. This is the better-hidden form and the more common one:
        # `for x in glob(...): if bad: found.append(x)` then `assert not found`.
        outside = [node for node in asserts if id(node) not in inside]
        if outside and all(_is_satisfied_by_emptiness(node) for node in outside):
            offenders.append(f"{path.relative_to(_TESTS)}:{function.lineno} {function.name}")

    return offenders


def test_no_test_asserts_only_inside_a_loop_over_a_discovered_collection() -> None:
    """Every such test must prove its collection is non-empty before concluding anything from it."""
    offenders: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path == Path(__file__):
            continue
        offenders.extend(_unguarded_tests(path))

    assert not offenders, (
        f"{len(offenders)} test(s) assert only inside a loop over a collection that is never "
        "proven non-empty. Each passes when that collection is empty, whatever emptied it. Add an "
        "assertion OUTSIDE the loop — a length floor, a membership check, or a counter verified "
        "afterwards:\n  " + "\n  ".join(offenders)
    )


def test_this_guard_can_actually_fail() -> None:
    """Proof the detector fires, built from a test function that commits the exact defect.

    Without this, the check above could be passing because the AST walk is subtly wrong — the
    failure mode of every source-inspection test, and the one this file exists to punish elsewhere.
    """
    vacuous = ast.parse(
        "def test_bad():\n"
        "    for path in Path('x').glob('*.py'):\n"
        "        assert path.suffix == '.py'\n"
    )
    guarded = ast.parse(
        "def test_good():\n"
        "    found = list(Path('x').glob('*.py'))\n"
        "    assert found\n"
        "    for path in found:\n"
        "        assert path.suffix == '.py'\n"
    )
    literal = ast.parse(
        "def test_literal():\n"
        "    for value in (1, 2, 3):\n"
        "        assert value > 0\n"
    )
    wrapped_literal = ast.parse(
        "def test_sorted_literal():\n"
        "    for value in sorted(_A_MODULE_LEVEL_SET):\n"
        "        assert value\n"
    )

    def flagged(tree: ast.Module) -> bool:
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef)
        asserts = [n for n in ast.walk(function) if isinstance(n, ast.Assert)]
        loops = [n for n in ast.walk(function) if isinstance(n, ast.For)]
        inside = {id(n) for lp in loops for n in ast.walk(lp) if isinstance(n, ast.Assert)}
        if not inside or len(inside) < len(asserts):
            return False
        return any(_iterates_a_discovered_collection(lp.iter) for lp in loops)

    assert flagged(vacuous), "the detector does not fire on a loop-only assertion over a glob"
    assert not flagged(guarded), "the detector fires on a test that proves its collection non-empty"
    assert not flagged(literal), "the detector fires on a loop over a literal, which cannot empty"
    assert not flagged(wrapped_literal), (
        "the detector fires on `sorted(<a literal>)`. A wrapper is not discovery; what it wraps is"
    )
