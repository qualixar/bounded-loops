"""Run one mutant past its loop's REAL gate and record what the gate said.

**This module is deliberately NOT blind, and that is the boundary.** The generator
(`corpus.py`, `operators/`, `mutation.py`) must never see a gate — a generator that could would be
able to stop producing the mutants a gate misses, and every number would be an artefact of that
avoidance. The harness has the opposite job: it must run the gate exactly as the product runs it.
`tests/evaluation/test_generator_is_blind.py` enforces that split by name, and fails if a new
module in this package is not classified as one or the other.

**The gate is the shipped adapter, not a reimplementation.** A local re-run of "python3
seed/check_x.py" would measure this file's idea of how gates work. Building the real
`CommandGate` — same argv tokenisation, same exit-code classification, same environment scrubbing —
means a defect found here is a defect in what users actually run.

**Nothing is written back into the catalog.** Each mutant is materialised into a throwaway copy of
the loop directory, so a corpus run cannot leave a mutated artifact behind in `loops/` — which
would silently corrupt every later run, including the convergence suite.

**Mutants are applied to the CONVERGED artifact, never the pristine seed, and that is not a
detail.** Every loop's seed fails its own gate by design — that is what makes the loop demonstrate
something, and `test_the_pristine_seed_fails_its_own_gate` pins it. So a meaning-preserving edit to
a seed produces an artifact that is still incorrect, and labelling it CORRECT makes every gate that
rejects it look like it committed a false reject.

The first version of this harness did exactly that. It reported 36 false rejects and, tellingly,
**zero true accepts** — no preserving mutant passed any gate, which is not a plausible thing for 68
gates to have in common. The giveaway was in the summary counts, not in any individual result.

So the baseline is established first: converge the loop with its own cassette, then VERIFY the
converged artifact passes its gate. A loop whose baseline does not pass is excluded and reported,
never mutated. That check is a precondition rather than an assertion at the end, because the failure
it prevents is silent and produces numbers that look ordinary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile

import yaml

from bounded_loops.evaluation.corpus import Mutant

#: What the gate said about a mutant. Deliberately three-valued: an ERROR is not a rejection.
VERDICT_PASSED = "passed"
VERDICT_REJECTED = "rejected"
VERDICT_ERROR = "error"


@dataclass(frozen=True)
class MutantOutcome:
    """One mutant, the gate's verdict, and the label the operation fixed in advance."""

    mutant_id: str
    loop: str
    operator: str
    label: str            # ground truth, from the operation
    verdict: str          # what the gate said
    detail: str = ""

    @property
    def is_false_accept(self) -> bool:
        """The gate passed work that was incorrect by construction. This is α."""
        return self.verdict == VERDICT_PASSED and self.label == "incorrect"

    @property
    def is_false_reject(self) -> bool:
        """The gate blocked work that was correct by construction — the cost of gating."""
        return self.verdict == VERDICT_REJECTED and self.label == "correct"

    @property
    def counts_toward_a_rate(self) -> bool:
        """Errors are excluded from every rate.

        A gate that could not run did not judge, and counting a crash as a rejection would credit
        the gate for catching a defect it never saw — flattering α by exactly the amount the
        harness happened to be broken. Same reasoning `gate_metrics` applies to worker faults.
        """
        return self.verdict in (VERDICT_PASSED, VERDICT_REJECTED)


def _gate_command(loop_dir: Path) -> str | None:
    """The loop's declared gate command, or None when it is not a `command` gate.

    Reading `gate:` here is exactly what the generator may not do — and exactly what this module
    exists to do.
    """
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return None
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    gate = manifest.get("gate") or {}
    if gate.get("kind") != "command":
        return None
    run = gate.get("run")
    return run if isinstance(run, str) and run.strip() else None


def _run_gate(command: str, workspace: Path) -> tuple[str, str]:
    """`(verdict, detail)` for one gate invocation, through the shipped adapter."""
    from bounded_loops.adapters.gates.command import CommandGate
    from bounded_loops.domain.errors import GateError

    try:
        verdict = CommandGate(command).check(_context_for(workspace))
    except GateError as exc:
        return VERDICT_ERROR, str(exc)[:200]
    passed = bool(getattr(verdict, "passed", False))
    return (
        VERDICT_PASSED if passed else VERDICT_REJECTED,
        str(getattr(verdict, "reason", ""))[:200],
    )


def establish_baseline(loop_dir: Path, *, into: Path, timeout_s: int = 180) -> Path | None:
    """Converge a copy of the loop with its own cassette; return it only if its gate PASSES.

    `None` means this loop cannot anchor a corpus — it did not converge, or converged to something
    its gate still rejects. Either way its mutants would be measured against an artifact that was
    already incorrect, so it is excluded and counted rather than quietly mutated.

    Uses the loop runner rather than reproducing convergence here: a locally reimplemented "apply
    the cassette" would eventually disagree with what `bl run` does, and then the corpus would be
    measuring gates against artifacts no user would ever produce.
    """
    import subprocess
    import sys

    target = into / loop_dir.name
    shutil.copytree(loop_dir, target, dirs_exist_ok=True)

    # `--run-id` is load-bearing, not cosmetic. Without it the runner converges inside a scratch
    # workspace and DISCARDS it, leaving the pristine (failing) seed in the loop directory — so a
    # baseline check would fail for every loop in the catalog, which is exactly what happened.
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "bounded_loops.cli", "run", str(target),
                "--yes", "--run-id", "corpus-baseline",
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None

    # The converged artifact lives in the RUN WORKSPACE, not in the loop directory. `bl run` copies
    # the loop into a workspace, converges there, and never promotes the result back — so the loop
    # directory still holds the pristine, failing seed after a successful DONE.
    #
    # This cost two wrong runs. The first missed `--run-id` and the workspace was discarded. The
    # second kept it and still read the loop directory, where a spot-check for "does the file
    # contain an assert" said yes — because the seed always had one, just not in the test the gate
    # flags. The gate is the only reliable reader of whether an artifact is converged, which is why
    # the baseline is now VERIFIED by running it rather than inferred from the runner's exit code.
    workspace = target / ".bounded-loops" / "runs" / "corpus-baseline" / "workspace"
    if not workspace.is_dir():
        return None

    command = _gate_command(loop_dir)
    if command is None:
        return None
    verdict, _detail = _run_gate(command, workspace)
    return workspace if verdict == VERDICT_PASSED else None


def materialise(mutant: Mutant, *, baseline: Path, into: Path) -> Path:
    """Copy the CONVERGED baseline and apply the mutation to it."""
    target = into / f"{mutant.loop}-mutant"
    shutil.copytree(baseline, target, dirs_exist_ok=True)

    artifact = target / mutant.mutation.path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(mutant.mutation.mutated_text, encoding="utf-8")
    return target


def run_mutant(mutant: Mutant, *, catalog_root: Path, baseline: Path) -> MutantOutcome:
    """Apply one mutant to the converged baseline, run its gate, classify the verdict."""
    command = _gate_command(catalog_root / mutant.loop)
    if command is None:
        return MutantOutcome(
            mutant_id=mutant.mutant_id, loop=mutant.loop, operator=mutant.mutation.operator,
            label=mutant.mutation.label, verdict=VERDICT_ERROR,
            detail="not a command gate; this tier measures command gates only",
        )

    with tempfile.TemporaryDirectory(prefix="bl-corpus-") as scratch:
        workspace = materialise(mutant, baseline=baseline, into=Path(scratch))
        verdict, detail = _run_gate(command, workspace)

    return MutantOutcome(
        mutant_id=mutant.mutant_id, loop=mutant.loop, operator=mutant.mutation.operator,
        label=mutant.mutation.label, verdict=verdict, detail=detail,
    )


def _context_for(workspace: Path):
    """The `LoopContext` a gate is handed in production, pointed at the throwaway copy.

    `lap=1` because a corpus mutant is one attempt, not a pre-loop init. `L1` is the catalog's own
    default rung. The environment is empty so the gate sees the same scrubbed environment a real
    run gives it — a gate that only passes because the harness leaked a variable would be a
    measurement of this file.
    """
    from bounded_loops.domain.models import LoopContext, Rung

    return LoopContext(
        workspace=workspace, lap=1, rung=Rung.L1, trace_id="corpus-mutant", env={},
    )


def summarise(outcomes: list[MutantOutcome]) -> dict:
    """Counts a reader can check the rates against, per the ledger's refusal discipline.

    Rates are NOT computed here. `gate_metrics` owns that, including its refusal to report a rate
    from too small a sample and its interval with the estimand named. Two places computing α is
    how two different α values end up in one paper.
    """
    judged = [o for o in outcomes if o.counts_toward_a_rate]
    accepted = [o for o in judged if o.verdict == VERDICT_PASSED]
    rejected = [o for o in judged if o.verdict == VERDICT_REJECTED]

    return {
        "total": len(outcomes),
        "judged": len(judged),
        "errors": len(outcomes) - len(judged),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "false_accepts": sum(1 for o in judged if o.is_false_accept),
        "false_rejects": sum(1 for o in judged if o.is_false_reject),
        "loops_covered": len({o.loop for o in judged}),
    }
