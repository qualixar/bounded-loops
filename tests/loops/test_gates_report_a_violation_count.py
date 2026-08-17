"""A rejecting gate must say HOW MUCH is wrong, not only that something is.

WHY THIS IS A CONTRACT AND NOT A STYLE PREFERENCE
-------------------------------------------------
A gate that prints only pass/fail makes consecutive attempts indistinguishable from each other. A
loop converging over six laps looks identical, from the receipt, to a loop that is stuck — so an
operator cannot tell "working, nearly there" from "burning budget on no progress", and a caller
cannot check a predicted convergence length against the observed one. The count is what makes the
soft bound's job legible to a human instead of only to the controller.

TWO CLASSES OF GATE, AND THE DIFFERENCE MATTERS IN PRACTICE
-----------------------------------------------------------
**Counting gates** check a requirement quantified over many items — every record has a checksum,
every module has a test, every hour is covered. Violations are countable, progress is observable,
and the count belongs in the output.

**Predicate gates** check ONE boolean property of an artifact: is the JWT algorithm `none`; does a
CORS config pair credentials with a wildcard origin. There is nothing to count. Forcing "1 violation"
onto them would be a fake count that says less than the sentence it replaced.

The practitioner consequence, which the paper should state: with a predicate gate you cannot watch
progress across attempts at all, so the no-progress window is your *only* early-stop signal and
setting it well matters more, not less.

`_PREDICATE_GATES` below is an explicit allow-list. A new gate that reports no count fails this test
until someone adds it there, which forces the classification to be a decision rather than an
oversight.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPS_ROOT = REPO_ROOT / "loops"

#: Gates whose requirement is a single boolean property, where a count would be noise.
#: Adding to this set is a claim: "this requirement is not quantified over anything."
_PREDICATE_GATES = frozenset({
    "cors-not-wildcard",   # allow_credentials AND '*' in allow_origins — one property
    "jwt-alg-not-none",    # jwt.algorithm == 'none' — one field
})

#: Gates needing a binary or package this repo does not vendor. Skipped with a reason, never
#: silently passed — an environment without `checkov` must not report that its gate is compliant.
_NEEDS_EXTERNAL = {
    "osv-scanner-example": "osv-scanner binary",
    "checkov-example": "checkov binary",
    "content-fact-gate": "npx + network",
    "adk-example": "google-adk package",
    "autogen-example": "autogen package",
    "crewai-example": "crewai package",
    "langgraph-example": "langgraph package",
}

_ARTIFACTS = (".bounded-loops", ".ledger.jsonl", ".STATE.md.runtime", "__pycache__", "*.pyc")

#: A bare integer, not part of a path, version, flag or hex digest.
_COUNT = re.compile(r"(?<![\w./:-])(\d+)(?![\w./-])")


def _command_gate_loops() -> list[str]:
    names = []
    for manifest in sorted(LOOPS_ROOT.glob("*/loop.yaml")):
        spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        gate = spec.get("gate") or {}
        if gate.get("kind") == "command" and gate.get("run"):
            names.append(manifest.parent.name)
    assert len(names) >= 44, f"command-gate count fell to {len(names)}; the catalogue changed"
    return names


COMMAND_GATE_LOOPS = _command_gate_loops()


def _run_gate_on_pristine_seed(name: str) -> subprocess.CompletedProcess:
    """Run the loop's own gate command against its untouched seed, in a copy.

    A copy because the gate may write alongside the artifact, and mutating the checked-in seed would
    make this test order-dependent and the next run of it meaningless.
    """
    source = LOOPS_ROOT / name
    command = shlex.split(str((yaml.safe_load(
        (source / "loop.yaml").read_text(encoding="utf-8")
    ).get("gate") or {}).get("run")))
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*_ARTIFACTS))
        return subprocess.run(command, cwd=target, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("name", COMMAND_GATE_LOOPS)
def test_a_rejecting_gate_reports_how_much_is_wrong(name: str) -> None:
    if name in _NEEDS_EXTERNAL:
        pytest.skip(f"{name} needs {_NEEDS_EXTERNAL[name]}")

    result = _run_gate_on_pristine_seed(name)
    output = (result.stdout + result.stderr).strip()

    # The pristine seed carries a planted defect, so the gate must reject it. That property has its
    # own test; asserting it here too keeps this test honest — a gate that PASSED would trivially
    # satisfy everything below by saying nothing about violations.
    assert result.returncode != 0, (
        f"{name}: pristine seed passed its own gate, so there is no rejection to describe:\n{output}"
    )
    assert output, f"{name}: gate rejected the seed and printed nothing at all"

    if name in _PREDICATE_GATES:
        pytest.skip(f"{name} is a predicate gate: one boolean property, nothing to count")

    assert _COUNT.search(output), (
        f"{name}: gate rejected the seed without reporting how many violations it found.\n"
        f"Output was:\n  {output}\n\n"
        "Either report a count — 'N of M records missing a checksum' — or, if this requirement is "
        "genuinely a single boolean property, add the loop to _PREDICATE_GATES in this file with a "
        "comment naming the property. Progress a human cannot see is progress the receipt cannot "
        "evidence."
    )


def test_the_predicate_allowlist_names_only_real_predicates() -> None:
    """An allow-list nobody prunes becomes a place to hide regressions.

    Every entry must still exist, still use a command gate, and still decline to report a count. An
    entry that now reports one should be removed, not left as standing permission.
    """
    for name in sorted(_PREDICATE_GATES):
        assert (LOOPS_ROOT / name / "loop.yaml").exists(), (
            f"_PREDICATE_GATES names {name!r}, which is not a loop any more — prune the list"
        )
        assert name in COMMAND_GATE_LOOPS, f"{name!r} no longer uses a command gate"
        if name in _NEEDS_EXTERNAL:
            continue
        output = (lambda r: (r.stdout + r.stderr).strip())(_run_gate_on_pristine_seed(name))
        assert not _COUNT.search(output), (
            f"{name} now reports a count, so it is no longer a predicate gate. Remove it from "
            f"_PREDICATE_GATES rather than leaving a standing exemption.\nOutput:\n  {output}"
        )


def test_every_shipped_gate_script_compiles() -> None:
    """Syntax alone, but nothing else was checking it.

    `ruff` is configured over `bounded_loops/`, `tests/` and `scripts/` — not `loops/`. So the 44
    shipped gate scripts, all Python, had no static check at all. A SyntaxError in one of them was
    caught only by whichever end-to-end test happened to run that loop, and during this session an
    edit that split a `try` from its `except` in five gates passed `ruff` cleanly.

    Compiling is the cheapest possible floor and it belongs here rather than in lint config, so it
    holds whatever a future tool is pointed at.
    """
    import py_compile

    scripts = sorted(LOOPS_ROOT.glob("*/seed/*.py"))
    assert len(scripts) >= 44, f"only {len(scripts)} gate scripts found; the catalogue changed"

    broken = []
    for script in scripts:
        try:
            py_compile.compile(str(script), doraise=True, cfile=None)
        except py_compile.PyCompileError as exc:
            broken.append(f"{script.relative_to(LOOPS_ROOT)}: {exc.msg.strip().splitlines()[-1]}")
    assert not broken, "shipped gate scripts do not compile:\n  " + "\n  ".join(broken)
