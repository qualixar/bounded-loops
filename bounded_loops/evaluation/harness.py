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


#: Gate commands that shell out to a tool this project does not ship. Their verdicts are that
#: tool's behaviour, not this catalog's, so an alpha computed over them would attribute a third
#: party's judgement to us.
#:
#: `content-fact-gate` is the concrete case: its gate is `npx markdown-link-check`, and an emptied
#: article has no links for it to check, so it passes. That is a real property of delegating a gate
#: to an external tool — and worth reporting as one — but it is not a defect we can fix by editing
#: a checker, and counting it in alpha would be reporting someone else's vacuity as our own.
#:
#: The same loops are already skipped by the convergence suite for needing a binary or a network.
_EXTERNAL_TOOL_PREFIXES = ("npx", "npm", "checkov", "osv-scanner", "semgrep", "trivy", "gitleaks")

#: Gate KINDS that are a third-party scanner. The same reasoning as `_EXTERNAL_TOOL_PREFIXES`, at
#: the other place a delegation can be declared.
#:
#: Missing this list is how `checkov-example` and `osv-scanner-example` slipped past the exclusion
#: guard: `_gate_command` returns None for a non-`command` kind, so the prefix scan above saw no
#: command, concluded "not external", and marked both loops ELIGIBLE — after which they produced no
#: mutants and vanished from the measurement with no stated reason. An exclusion rule that only
#: knows how to read one of the two places a gate can be declared is not an exclusion rule.
_EXTERNAL_TOOL_KINDS = frozenset(
    {"checkov", "osv", "gitleaks", "semgrep", "trivy", "promptfoo", "great_expectations"}
)


def gate_kind(loop_dir: Path) -> str | None:
    """The loop's declared gate kind, or None when there is no readable manifest.

    Reading `gate:` here is exactly what the generator may not do — and exactly what this module
    exists to do.
    """
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return None
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    gate = manifest.get("gate") or {}
    kind = gate.get("kind")
    return kind if isinstance(kind, str) else None


def uses_an_external_tool(loop_dir: Path) -> bool:
    """Whether this loop's gate delegates to a tool this project does not ship.

    Checks BOTH declaration sites — the gate's kind, and the first word of a `command` gate's
    argv — because a delegation declared in either place is equally not our judgement to report.
    """
    kind = gate_kind(loop_dir)
    if kind in _EXTERNAL_TOOL_KINDS:
        return True
    for command in _gate_commands(loop_dir):
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in _EXTERNAL_TOOL_PREFIXES:
            return True
    return False


def _gate_command(loop_dir: Path) -> str | None:
    """The loop's declared gate command, or None when it is not a `command` gate."""
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return None
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    gate = manifest.get("gate") or {}
    if gate.get("kind") != "command":
        return None
    run = gate.get("run")
    return run if isinstance(run, str) and run.strip() else None


def _gate_commands(loop_dir: Path) -> list[str]:
    """Every command this loop's gate runs, including a composite's children."""
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return []
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    gate = manifest.get("gate") or {}

    found: list[str] = []
    for block in (gate, *(gate.get("gates") or [])):
        if not isinstance(block, dict) or block.get("kind") != "command":
            continue
        run = block.get("run")
        if isinstance(run, str) and run.strip():
            found.append(run)
    return found


def build_gate(loop_dir: Path):
    """The REAL gate this loop runs, whatever its kind. Raises on a loop that cannot build one.

    **Built through the shipped wiring, never reconstructed here.** `composition._instantiate_gate`
    is the same function `bl run` calls, so a defect this harness finds is a defect in what users
    actually get — including the constructor conventions (schema path resolution, timeout wiring,
    composite child assembly) that a local reimplementation would drift away from within a release.

    This replaced a `CommandGate(manifest["gate"]["run"])` shortcut that could only build ONE of the
    six kinds in the catalog. The other five — 10 `jsonschema`, 9 `pytest`, 3 `composite`, and the
    two scanners — returned None and were dropped from the measurement without appearing in any
    count. Twenty-four of sixty-eight loops, silently, while the corpus reported a rate.
    """
    from bounded_loops.application.manifest import load
    from bounded_loops.composition import _instantiate_gate

    manifest = load(loop_dir)
    return _instantiate_gate(manifest.gate_kind, manifest)


def _run_gate(gate, workspace: Path) -> tuple[str, str]:
    """`(verdict, detail)` for one gate invocation, through the shipped adapter."""
    from bounded_loops.domain.errors import GateError

    try:
        verdict = gate.check(_context_for(workspace))
    except GateError as exc:
        return VERDICT_ERROR, str(exc)[:200]
    passed = bool(getattr(verdict, "passed", False))
    return (
        VERDICT_PASSED if passed else VERDICT_REJECTED,
        str(getattr(verdict, "detail", ""))[:200],
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

    try:
        gate = build_gate(loop_dir)
    except Exception:  # noqa: BLE001 - an unbuildable gate excludes the loop; it is not a mutant result
        return None
    verdict, _detail = _run_gate(gate, workspace)
    return workspace if verdict == VERDICT_PASSED else None


def judges_artifact(loop_dir: Path, relative_path: str) -> bool:
    """Whether the gate actually judges this file — decided from its own argv, before any verdict.

    The destroying operators claim "an emptied artifact cannot satisfy the loop's stated purpose".
    That holds only for the artifact under judgement. Two loops proved it:

    * `test-presence-per-module` checks that each `src/<mod>.py` has a matching test file. Emptying
      `src/a.py` leaves the test file exactly where it was, so the gate passed — correctly.
    * `broken-internal-links` checks that every link resolves. Emptying one of two documents deletes
      that document's links; it does not break any, so the gate passed — correctly.

    Both were recorded as false accepts and both were mislabelled mutants. The gate was right each
    time.

    **This is not the generator peeking at the gate.** The filter is applied harness-side, uses only
    the paths named in the gate's command line, and is decided before the gate runs — so it cannot
    select for whether a mutant would be caught, which is the bias the blindness guard exists to
    prevent. It excludes mutants that were never about the gate in the first place.

    A file counts as judged when the command names it, or names a directory containing it.

    **Every gate kind answers this differently, and the answer is structural in each case** —
    derived from the gate's declaration or its adapter's fixed target, never from a verdict:

    * `command` — the paths named on its argv.
    * `jsonschema` — `output.json` at the workspace root, which `JsonSchemaGate` reads
      unconditionally. Note this is NOT `seed/output.json`: the seeded copy is an input the worker
      starts from, and the gate never looks at it.
    * `pytest` — every work product that is not test material. **The suite IS the gate here**, so
      a test file is not an artifact under judgement; it is the judge. Emptying it does not violate
      the loop's purpose, it removes the instrument.
    * `composite` — the union over its children.

    **This is necessary and not sufficient**, and the shortfall is stated in
    `tier1_claim_holds` below — being in scope does not make emptying it a violation.
    """
    kind = gate_kind(loop_dir)
    target = Path(relative_path)

    if kind == "jsonschema":
        return relative_path == _JSONSCHEMA_TARGET
    if kind == "pytest":
        return not _is_test_material(relative_path)
    if kind == "composite":
        if _jsonschema_child_declared(loop_dir) and relative_path == _JSONSCHEMA_TARGET:
            return True
        return _named_by_any_command(loop_dir, target)
    if kind == "command":
        return _named_by_any_command(loop_dir, target)
    return False


#: The file `JsonSchemaGate` validates. Hardcoded in the adapter (`ctx.workspace / "output.json"`),
#: so it is a property of the gate kind rather than of any one loop.
_JSONSCHEMA_TARGET = "output.json"


def _is_test_material(relative_path: str) -> bool:
    """Whether this file is part of a pytest suite — pytest's own collection convention.

    Deliberately the collector's rule (`test_*.py` / `*_test.py`, or anything under a `tests/`
    directory) rather than a judgement about content, so it stays true for loops added later.
    """
    parts = relative_path.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    return name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py")


def _jsonschema_child_declared(loop_dir: Path) -> bool:
    """Whether a composite gate has a `jsonschema` child."""
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return False
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    children = (manifest.get("gate") or {}).get("gates") or []
    return any(isinstance(c, dict) and c.get("kind") == "jsonschema" for c in children)


def _named_by_any_command(loop_dir: Path, target: Path) -> bool:
    """Whether any command in this loop's gate names `target`, or a directory containing it."""
    import shlex

    for command in _gate_commands(loop_dir):
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        for word in argv[1:]:
            if word.startswith("-"):
                continue
            named = Path(word)
            if named == target or named in target.parents:
                return True
    return False


#: Loops whose gate states a NEGATIVE requirement — "the artifact must NOT contain X". The
#: destroying operators cannot make a certain claim about these, because an emptied artifact
#: satisfies a prohibition **genuinely**: there is no secret in an empty file.
#:
#: The distinction the corpus rests on:
#:
#:   POSITIVE   "every dependency is pinned"      emptied ⇒ vacuous ⇒ INCORRECT   claim holds
#:   NEGATIVE   "no hardcoded secrets"            emptied ⇒ satisfied            claim fails
#:
#: Discovered by trying to "fix" `secret-scan-keyless` and breaking it. A vacuity guard was added
#: so the gate would reject an emptied config; it immediately failed a 0.6.2 regression pin, because
#: `def f(password: str) -> None:` is a type annotation and this gate must pass it. The gate was
#: right, the corpus was wrong, and the tempting move was to make the gate stricter than its stated
#: purpose so a measurement would come out clean. That is the same error as loosening a gate to
#: improve a number, and it is the one a corpus author is most likely to make.
#:
#: A negative requirement still deserves mutants — a secret ADDED back, in a shape the scanner
#: misses. That is Tier 2: authored from the stated purpose, with the checker withheld.
_NEGATIVE_REQUIREMENT_LOOPS = frozenset({"secret-scan-keyless"})


def states_a_negative_requirement(loop_name: str) -> bool:
    """Whether the destroying operators' claim is inapplicable to this loop."""
    return loop_name in _NEGATIVE_REQUIREMENT_LOOPS


#: Loops whose RUNNER — not gate — imports a framework SDK this project does not depend on. They
#: cannot converge here, so there is no correct artifact to mutate and no baseline to measure from.
#:
#: Enumerated rather than detected, and that is the point. "Whatever failed to converge" is not an
#: exclusion reason, it is the absence of one: it silently absorbs a loop we genuinely broke into
#: the same bucket as a loop that needs `pip install crewai`. Pinning the set means a NEW loop
#: dropping out of the corpus is a failing test naming that loop, not a quietly smaller denominator.
#:
#: **This set is verified against the environment, not trusted.** Whether a loop converges depends
#: on what happens to be installed, so an unverified list makes α's denominator a property of the
#: machine that ran it. `autogen-example` is the case that proved it: it converged under a system
#: interpreter that had `agent-framework` and failed under the project venv that did not, and the
#: corpus would have reported a different population on each without saying so.
#: `test_the_unshipped_package_exclusions_match_reality` asserts both directions — every named
#: package genuinely absent, and no loop outside this set needing one — so installing any of them
#: is a failing test telling you the denominator changed, rather than a silent change.
#:
#: Distribution names are quoted from each loop's own `pip install …` message, so the exclusion
#: reason a reader sees is the instruction the product gives.
_RUNNER_NEEDS_UNSHIPPED_PACKAGE = {
    "adk-example": "google-adk",
    "autogen-example": "agent-framework",
    "crewai-example": "crewai",
    "langgraph-example": "langgraph",
}


def runner_needs_an_unshipped_package(loop_name: str) -> str | None:
    """The package this loop's runner needs and this project does not ship, or None."""
    return _RUNNER_NEEDS_UNSHIPPED_PACKAGE.get(loop_name)


def excluded_reason(loop_dir: Path) -> str | None:
    """Why this loop carries no Tier-1 mutants, decided WITHOUT running anything — or None.

    Every exclusion the corpus performs must be nameable here, before a gate is built and before a
    verdict exists. That ordering is the whole guarantee: a reason computed after seeing results is
    a reason chosen to fit them, and a corpus that drops the loops it finds awkward reports a rate
    for a population it selected afterwards.

    Loops NOT excluded here are required to produce a baseline and mutants. When one does not, that
    is a defect in this repository and the corpus test fails naming it — rather than the loop
    vanishing into a smaller denominator nobody reads.
    """
    if states_a_negative_requirement(loop_dir.name):
        return "states a negative requirement; an emptied artifact satisfies a prohibition"
    if uses_an_external_tool(loop_dir):
        return "gate delegates to a tool this project does not ship"
    package = runner_needs_an_unshipped_package(loop_dir.name)
    if package is not None:
        return f"runner needs {package}, which this project does not depend on"
    return None


def tier1_claim_holds(judged_artifacts: int) -> bool:
    """Whether "emptied ⇒ incorrect" is certain for a loop with this many judged artifacts.

    **It is certain only when the gate judges exactly ONE artifact.** With several, a requirement is
    usually about the RELATION between them, and emptying one removes a subject of the requirement
    instead of violating it:

    * `test-presence-per-module` — "every src module has a test file". Empty `src/a.py` and the
      module still has its test. The requirement holds.
    * `broken-internal-links` — "every link resolves". Empty one of two documents and its links are
      gone, not broken. The requirement holds.

    Both gates were right; both mutants were mislabelled by me. The operator assumed that deleting
    the subjects of a requirement violates it, when it can satisfy it **vacuously** — which is the
    exact defect this corpus was built to find in gates, committed by the corpus itself. Recording
    that plainly matters more than the twelve false accepts it costs: a method that cannot catch
    its own instance of the bug it hunts is not a method anyone should trust.

    Multi-artifact loops are not abandoned. They need a mutant that violates the RELATION — delete
    the test file, break the link target — which requires knowing what the relation is, and that is
    Tier 2: authored per loop from the stated purpose, with the checker withheld.
    """
    return judged_artifacts == 1


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
    try:
        gate = build_gate(catalog_root / mutant.loop)
    except Exception as exc:  # noqa: BLE001 - a gate that cannot be built judged nothing
        return MutantOutcome(
            mutant_id=mutant.mutant_id, loop=mutant.loop, operator=mutant.mutation.operator,
            label=mutant.mutation.label, verdict=VERDICT_ERROR,
            detail=f"gate could not be built: {str(exc)[:150]}",
        )

    with tempfile.TemporaryDirectory(prefix="bl-corpus-") as scratch:
        workspace = materialise(mutant, baseline=baseline, into=Path(scratch))
        verdict, detail = _run_gate(gate, workspace)

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
