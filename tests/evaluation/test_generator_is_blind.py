"""The generator must not be able to see what any gate checks.

This is the load-bearing methodological claim of the whole corpus, so it is asserted against the
AST of every module in `bounded_loops/evaluation/` rather than described in a docstring and hoped
for. If the generator could read a gate, it could — deliberately or by drift — stop producing the
mutants that gate happens to miss, and every false-accept rate the corpus reports would be an
artefact of that avoidance rather than a measurement of the gate.

It is a very easy mistake to make. "Skip this operator when the checker uses a regex" reads like a
sensible optimisation and is a way of quietly deleting the evidence.

Four things are forbidden:

1. importing any gate adapter or checker module;
2. reading any `check_*.py`, or the `gate:` block of a loop manifest;
3. branching on a loop's NAME — the route by which a generator special-cases the loops it knows
   are strict;
4. running anything. No subprocess, no exec.

The generator reads `loop.yaml` for exactly one thing: the `forbid` path list, which is the loop's
own declaration of what a worker may not edit. That is checked below to be the only key it touches.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import bounded_loops.evaluation as evaluation_package

_PACKAGE = Path(evaluation_package.__file__).parent

#: The RUNNER. It must import gate adapters and run them — that is its entire job, and it is the
#: reason the split exists rather than a blanket ban. It is excluded from the blindness checks and
#: from nothing else; `test_every_module_is_classified` fails if a new module quietly joins it.
_RUNNER_MODULES = frozenset({"harness.py"})

#: The GENERATOR. Everything that decides WHICH mutants exist, and therefore everything that must
#: not be able to see what a gate checks.
_MODULES = sorted(
    path for path in _PACKAGE.rglob("*.py") if path.name not in _RUNNER_MODULES
)

#: Anything that would let the generator see a gate's implementation or its declaration.
_FORBIDDEN_IMPORT_FRAGMENTS = (
    "adapters.gates",
    "gate_adapter",
    "check_",
    "cli_loops",
    "run_loop",
)

#: Executing something is how a generator learns what a gate does without importing it.
_FORBIDDEN_CALLS = frozenset({"exec", "eval", "compile", "__import__"})
_FORBIDDEN_MODULES = frozenset({"subprocess", "importlib", "runpy", "pty", "os.system"})

#: The ONLY key the generator may read out of a loop manifest. `gate` is the one that matters:
#: reading it would tell the generator what the checker is and defeat the whole design.
_ALLOWED_MANIFEST_KEYS = frozenset({"forbid"})
_MANIFEST_KEYS_THAT_DESCRIBE_A_GATE = frozenset({"gate", "run", "kind", "spec", "runner"})


def _tree(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8"), filename=str(module))


def _module_ids(module: Path) -> str:
    return str(module.relative_to(_PACKAGE))


@pytest.mark.parametrize("module", _MODULES, ids=_module_ids)
def test_the_generator_never_imports_anything_gate_related(module: Path) -> None:
    """A gate reached by import is a gate the generator can be shaped around."""
    offenders: list[str] = []

    for node in ast.walk(_tree(module)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            if any(fragment in name for fragment in _FORBIDDEN_IMPORT_FRAGMENTS):
                offenders.append(f"line {node.lineno}: {name}")
            if name.split(".")[0] in _FORBIDDEN_MODULES:
                offenders.append(f"line {node.lineno}: {name} (execution)")

    assert not offenders, (
        f"{_module_ids(module)} can reach a gate or run code:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", _MODULES, ids=_module_ids)
def test_the_generator_never_executes_anything(module: Path) -> None:
    """Running a checker to see what it accepts would make the corpus a search, not a sample."""
    offenders = [
        f"line {node.lineno}: {node.func.id}()"
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FORBIDDEN_CALLS
    ]

    assert not offenders, f"{_module_ids(module)} executes code:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("module", _MODULES, ids=_module_ids)
def test_the_generator_never_reads_a_checker_file(module: Path) -> None:
    """`check_*.py` is the implementation the corpus is measuring. Reading it is the whole bias."""
    offenders = [
        f"line {node.lineno}: {node.value!r}"
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ("check_" in node.value or node.value.endswith(".py") and "check" in node.value)
    ]

    assert not offenders, (
        f"{_module_ids(module)} names a checker file:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", _MODULES, ids=_module_ids)
def test_the_generator_reads_only_the_forbid_list_from_a_manifest(module: Path) -> None:
    """`forbid` is a path list. `gate` is a description of what to defeat.

    Detected by looking for subscript/`get` access with a manifest-ish key. Crude by design: a
    false positive here costs one comment, and a false negative costs the corpus its credibility.
    """
    offenders: list[str] = []

    for node in ast.walk(_tree(module)):
        key: str | None = None
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            key = node.slice.value if isinstance(node.slice.value, str) else None
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            key = node.args[0].value

        if key in _MANIFEST_KEYS_THAT_DESCRIBE_A_GATE and key not in _ALLOWED_MANIFEST_KEYS:
            offenders.append(f"line {node.lineno}: reads manifest key {key!r}")

    assert not offenders, (
        f"{_module_ids(module)} reads a gate-describing manifest key:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", _MODULES, ids=_module_ids)
def test_the_generator_never_branches_on_a_LOOP_NAME(module: Path) -> None:
    """Special-casing a named loop is how a generator avoids the mutants that loop would catch.

    Checked against the real catalog rather than a pattern, so it stays accurate as loops are
    added: no module may contain the name of any shipped loop as a string literal.
    """
    catalog = Path(__file__).resolve().parents[2] / "loops"
    loop_names = {p.parent.name for p in catalog.glob("*/loop.yaml")}
    assert loop_names, "no catalog found; this guard would pass vacuously"

    literals = {
        node.value
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = sorted(literals & loop_names)

    assert not offenders, (
        f"{_module_ids(module)} names specific loops {offenders} — a generator that knows which "
        "loop it is mutating can avoid the mutants that loop would catch"
    )


def test_every_module_is_classified_as_generator_or_runner() -> None:
    """A new module must be a deliberate choice, not an unexamined default.

    Without this, adding `bounded_loops/evaluation/helpers.py` that reads a gate would silently be
    covered by the blindness checks (good) — but adding it to `_RUNNER_MODULES` to make a failure
    go away would be invisible. This makes the runner set explicit and small, so growing it is a
    reviewable act.

    One runner is the design. If this ever needs to be two, the corpus has grown a second thing
    that runs gates, and that deserves an argument rather than an edit.
    """
    all_modules = {path.name for path in _PACKAGE.rglob("*.py")}
    generator_modules = {path.name for path in _MODULES}

    assert generator_modules | _RUNNER_MODULES == all_modules
    assert not (generator_modules & _RUNNER_MODULES), "a module cannot be both"
    assert len(_RUNNER_MODULES) == 1, (
        f"the runner set has grown to {sorted(_RUNNER_MODULES)}. Each addition is a module allowed "
        "to see gates; that is the one privilege this corpus's credibility depends on withholding"
    )


def test_the_runner_is_the_only_thing_that_touches_a_gate() -> None:
    """The positive half: the harness must ACTUALLY reach the real gate adapters.

    A runner that quietly stopped importing them — reimplementing the check locally, say — would
    pass every blindness test above while measuring something other than the shipped product.
    """
    harness = _PACKAGE / "harness.py"
    source = harness.read_text(encoding="utf-8")

    assert "adapters.gates" in source, (
        "the harness no longer builds a real gate adapter; a local reimplementation measures this "
        "file's idea of how gates work, not what users run"
    )


def test_this_guard_would_actually_fail_on_a_blind_spot() -> None:
    """Proof the checks above can fail, built from a module that violates every one of them.

    Without this, all five could be passing because the AST walk is subtly wrong — which is the
    failure mode of every source-inspection test, and the reason two tests were rewritten in this
    repository last week for being named after properties they did not measure.
    """
    import tempfile

    hostile = (
        "import subprocess\n"
        "from bounded_loops.adapters.gates import something\n"
        "GATE = 'seed/check_pins.py'\n"
        "def go(manifest):\n"
        "    if manifest['gate']:\n"
        "        return exec('1')\n"
    )
    with tempfile.TemporaryDirectory() as directory:
        planted = Path(directory) / "hostile.py"
        planted.write_text(hostile, encoding="utf-8")
        tree = ast.parse(hostile)

        imports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert any(name.split(".")[0] in _FORBIDDEN_MODULES for name in imports)
        assert any(
            any(fragment in name for fragment in _FORBIDDEN_IMPORT_FRAGMENTS) for name in imports
        )
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FORBIDDEN_CALLS
            for node in ast.walk(tree)
        )
        assert any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "check_" in node.value
            for node in ast.walk(tree)
        )
