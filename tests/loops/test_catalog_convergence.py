"""Every shipped loop must still work — checked by running it, for all of them.

Until 0.6.2 the catalog had **no default end-to-end coverage at all**. Ten loops
had a `test_keyless_loop_reaches_done`, and all ten carried
``pytest.mark.external_tool``, which ``addopts`` in pyproject.toml deselects:

    addopts = -m "not network and not external_tool and not provider_smoke and not clean_install"

So `pytest` reported green over 58 loops nobody had run and 10 that were skipped.
That is how a change to `check_clauses.py` broke `nda-required-clauses` during the
0.6.2 gate-defect sweep and the suite stayed green. Only running the loops by hand
found it.

These tests are deliberately NOT marked. They take ~25s for the whole catalog —
about 0.4s a loop — which is worth paying on every run for the guarantee that the
thing we ship still does what it says.

Two contracts, and the second matters as much as the first:

1. **Convergence** — the loop reaches DONE. Its gate can be satisfied.
2. **The planted defect is real** — the pristine seed FAILS its gate. A loop whose
   seed already passes demonstrates nothing: the agent has nothing to fix and the
   gate never has to catch anything. Fixing an over-permissive gate (0.6.2 fixed
   fourteen) can silently turn a loop into a no-op, and only this direction catches it.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.loops._copied_loop import copy_loop

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPS_ROOT = REPO_ROOT / "loops"
BL = [sys.executable, "-m", "bounded_loops.cli"]

#: Loops whose gate or runner needs something this repo does not vendor. Skipped
#: with a reason, never silently passed — an environment lacking `checkov` must
#: not report that the checkov loop converged.
NEEDS_EXTERNAL: dict[str, str] = {
    "osv-scanner-example": "osv-scanner binary",
    "checkov-example": "checkov binary",
    "content-fact-gate": "npx + network (markdown-link-check)",
    "adk-example": "google-adk package",
    "autogen-example": "autogen package",
    "crewai-example": "crewai package",
    "langgraph-example": "langgraph package",
}


def _loop_names() -> list[str]:
    names = sorted(p.parent.name for p in LOOPS_ROOT.glob("*/loop.yaml"))
    assert len(names) >= 68, f"catalog shrank to {len(names)} loops — was 68"
    return names


ALL_LOOPS = _loop_names()


def _manifest(name: str) -> dict:
    return yaml.safe_load((LOOPS_ROOT / name / "loop.yaml").read_text(encoding="utf-8"))


def _skip_if_external(name: str) -> None:
    if name in NEEDS_EXTERNAL:
        pytest.skip(f"{name} needs {NEEDS_EXTERNAL[name]}")


@pytest.mark.parametrize("name", ALL_LOOPS)
def test_every_loop_reaches_done(name: str, tmp_path: Path) -> None:
    """The loop's gate can actually be satisfied by the loop's own cassette."""
    _skip_if_external(name)

    loop_dir = copy_loop(LOOPS_ROOT / name, tmp_path)
    result = subprocess.run(
        [*BL, "run", str(loop_dir), "--yes"], capture_output=True, text=True, timeout=180
    )

    assert result.returncode == 0, (
        f"{name} did not converge:\n{result.stdout}\n{result.stderr}"
    )
    assert "Gate verified" in result.stdout or "DONE" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "name",
    [n for n in ALL_LOOPS if (_manifest(n).get("gate") or {}).get("kind") == "command"],
)
def test_the_pristine_seed_fails_its_own_gate(name: str, tmp_path: Path) -> None:
    """The planted defect is real, so the loop demonstrates something.

    Runs the gate command directly against the untouched seed. Exit 0 here means
    the agent would have nothing to do and the gate nothing to catch — the loop
    would reach DONE on lap 1 while proving nothing, which is precisely what an
    over-corrected gate fix looks like from the outside.
    """
    _skip_if_external(name)

    gate = _manifest(name).get("gate") or {}
    command = str(gate.get("run") or "")
    if not command:
        pytest.skip(f"{name} declares no direct gate command")

    loop_dir = copy_loop(LOOPS_ROOT / name, tmp_path)
    result = subprocess.run(
        shlex.split(command), cwd=loop_dir, capture_output=True, text=True, timeout=120
    )

    assert result.returncode != 0, (
        f"{name}: the pristine seed PASSES its own gate, so the loop has no "
        f"planted defect to fix and its gate is never exercised.\n{result.stdout}"
    )
