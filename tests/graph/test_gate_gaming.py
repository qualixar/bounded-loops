"""Can a worker get bad output past the independent gate? Four attacks, and what each proves.

The engine's central claim is that a worker never grades its own node. This file tries to break that
claim four ways. Two of the attacks fail — the design holds. **Two succeed**, and they are the point
of the file: an honesty item that has been carried in prose since P1 becomes an executable
demonstration with a number attached.

A test that documents a real weakness is worth more than a paragraph claiming the weakness is
understood. When P4's evaluation harness reports α, these are the attacks that number has to be read
against: α measures how often the gate was wrong about output it genuinely evaluated, and says
nothing about a worker that arranged for the gate to evaluate something else.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.workers.acceptance_gate import StructuralAcceptanceGate
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactPolicy
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_ORG, _PROJECT = "org-1", "project-1"


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def _policy() -> ArtifactPolicy:
    return ArtifactPolicy(
        organization_id=_ORG, project_id=_PROJECT, producer_attempt="1",
        media_type="text/plain", sensitivity="internal", retention_class="standard",
    )


def _put(store: LocalArtifactStore, payload: bytes) -> str:
    return store.put(BytesIO(payload), _policy()).digest


def _gate(store: LocalArtifactStore) -> StructuralAcceptanceGate:
    return StructuralAcceptanceGate(store, organization_id=_ORG, project_id=_PROJECT)


def _node() -> PlannedNode:
    from bounded_loops.graph.domain.authoring import Effect, IsolationLevel

    return PlannedNode(
        node_id="worker", kind="research_claim", package_digest=None, binding_id=None,
        required_effects=frozenset({Effect.READ_ONLY}),
        isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1000,
        budgets={"max_attempts": 1}, approval_policy={},
    )


# ── Attack 1: produce the expected SHAPE without doing the work ────────────────────────────

def test_a_plausible_artifact_that_did_no_work_passes_the_structural_gate(tmp_path: Path) -> None:
    """SUCCEEDS as an attack, and the gate is not at fault — it is the wrong gate for the job.

    ``StructuralAcceptanceGate`` checks that a node produced a non-empty, UTF-8-decodable artifact.
    A worker that writes ``"done"`` without doing anything satisfies that completely.

    This is a **scope** result, not a defect: the gate is named *structural* and its docstring says it
    is not a semantic review. What the result forbids is the sentence "an independent gate verified the
    output" — the honest sentence is "an independent gate verified an artifact exists and is
    well-formed". Semantic checking is what the cross-model audit graph is for, and a claim about
    correctness needs THAT gate, not this one.
    """
    store = _store(tmp_path)
    plan = ExecutionPlan.__new__(ExecutionPlan)  # only .nodes/.edges are unused by this gate

    fabricated = _put(store, b"done")
    verdict = _gate(store).evaluate(
        plan=plan, node=_node(), result=WorkerResult((fabricated,), None, None),
    )

    assert verdict.passed, "structural acceptance cannot tell work from the appearance of work"


# ── Attack 2: replay an artifact an earlier attempt already produced ───────────────────────

def test_replaying_an_earlier_attempts_digest_passes_the_structural_gate(tmp_path: Path) -> None:
    """SUCCEEDS as an attack. Content addressing makes the replay FREE and undetectable to the gate.

    A worker that returns a digest some earlier attempt produced does no work at all, and the store
    happily serves those bytes because they are already there — that is what a content-addressed store
    is for. The gate re-reads the artifact, finds it non-empty and decodable, and passes.

    What DOES catch this is the receipt stream, not the gate: ``node.spend`` records one receipt per
    execution, so a replayed attempt shows as an execution that consumed nothing. The detection lives
    in the audit trail, and P4's harness is where that comparison belongs. Worth stating precisely
    because "the gate is independent" invites the reader to assume the gate would notice.
    """
    store = _store(tmp_path)
    plan = ExecutionPlan.__new__(ExecutionPlan)
    first = _put(store, b"a genuine answer from attempt 1")

    replayed = _gate(store).evaluate(
        plan=plan, node=_node(), result=WorkerResult((first,), None, None),
    )

    assert replayed.passed, "the gate cannot see that these bytes were not produced by this attempt"


# ── Attack 3: the gate delegates to the worker (GateWrapper) ───────────────────────────────

class _GateWrapper:
    """A 'gate' that asks the worker whether it did well. A different OBJECT, not an independent one."""

    def __init__(self, worker: object) -> None:
        self._worker = worker

    def evaluate(self, *, plan: object, node: object, result: object) -> GateVerdict:
        return GateVerdict(True, "the worker assures me this is fine")


class _SelfCongratulatingWorker:
    def execute(self, *, plan: object, node: object, envelope: object, attempt: int) -> WorkerResult:
        return WorkerResult((), None, None)


def test_the_worker_is_gate_check_forbids_aliasing_not_independence() -> None:
    """SUCCEEDS as an attack — and this is the honesty item carried in prose since P1, now executable.

    ``run_graph`` refuses ``worker is gate``. That check catches the naive mistake of passing one
    object as both. It cannot catch a gate that HOLDS the worker, or one that simply returns
    ``passed=True`` without looking, because independence of judgement is not a property Python can
    check by identity.

    So the guarantee is exactly: *the controller will not let one object play both roles.* It is NOT:
    *the verdict came from something that independently evaluated the work.* The second sentence is
    the one a reader wants, and only a deployment's own wiring discipline can supply it.

    Recorded as a test rather than a comment so that any future claim of "enforced independence" has
    to delete a passing test to be written.
    """
    worker = _SelfCongratulatingWorker()
    gate = _GateWrapper(worker)

    assert gate is not worker, "the identity check the controller performs"
    verdict = gate.evaluate(plan=None, node=None, result=None)
    assert verdict.passed, "a wrapper gate can rubber-stamp anything and still pass the check"


# ── Attack 4: mutate the artifact after the gate approved it ───────────────────────────────

def test_an_artifact_cannot_be_changed_after_the_gate_approved_it(tmp_path: Path) -> None:
    """FAILS as an attack. Content addressing closes this one properly.

    The receipt names a digest, so 'mutating the approved artifact' would require producing different
    bytes with the same SHA-256. Storing the mutated bytes yields a DIFFERENT digest, which the
    receipt does not reference — the approved content remains exactly what was approved.

    This is the attack the design genuinely defeats, and it is worth having in the same file as the
    two it does not: the difference between them is the difference between a cryptographic guarantee
    and a wiring convention.
    """
    store = _store(tmp_path)
    plan = ExecutionPlan.__new__(ExecutionPlan)
    approved = _put(store, b"the approved answer")
    assert _gate(store).evaluate(
        plan=plan, node=_node(), result=WorkerResult((approved,), None, None),
    ).passed

    tampered = _put(store, b"the answer after tampering")

    assert tampered != approved, "different bytes cannot occupy the approved digest"
    from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef

    with store.open(ArtifactRef(approved, _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
        assert handle.read() == b"the approved answer", "the approved content is immutable"


def test_an_empty_or_unreadable_artifact_is_still_refused(tmp_path: Path) -> None:
    """The floor the structural gate does hold: it is not a no-op.

    Attacks 1 and 2 show what this gate cannot do. This shows it does something — so the honest
    summary is "a weak but real check", not "theatre".
    """
    store = _store(tmp_path)
    plan = ExecutionPlan.__new__(ExecutionPlan)
    gate = _gate(store)

    assert not gate.evaluate(
        plan=plan, node=_node(), result=WorkerResult((), None, None),
    ).passed, "no artifact at all must fail"

    blank = _put(store, b"   \n\t ")
    assert not gate.evaluate(
        plan=plan, node=_node(), result=WorkerResult((blank,), None, None),
    ).passed, "whitespace-only output must fail"

    not_utf8 = _put(store, b"\xff\xfe\x00\x01")
    assert not gate.evaluate(
        plan=plan, node=_node(), result=WorkerResult((not_utf8,), None, None),
    ).passed, "undecodable output must fail"


@pytest.mark.parametrize(
    "payload",
    [
        b"null",
        b"{}",
        b"[]",
        b"I cannot help with that.",
        b".",
        "\u200b".encode(),           # zero-width space
        "\u00a0".encode(),           # non-breaking space
        "\ufeff".encode(),           # byte-order mark
        "\u2003\u2003".encode(),     # em spaces
    ],
)
def test_what_the_structural_gate_does_and_does_not_call_a_reply(tmp_path: Path, payload: bytes) -> None:
    """The audit's fifth attack, and the honest boundary of this gate.

    Semantically empty output — ``null``, ``{}``, a refusal sentence — PASSES, and must: judging
    whether a reply answers the question is semantic review, which this gate is explicitly not.

    Output that is only whitespace does NOT pass, and three of these used to. ``bytes.strip()``
    removes ASCII whitespace only, so a zero-width space, an NBSP or a bare BOM read as a non-empty
    reply. Stripping the decoded text uses Unicode's own definition instead.
    """
    store = _store(tmp_path)
    plan = ExecutionPlan.__new__(ExecutionPlan)
    digest = _put(store, payload)

    verdict = _gate(store).evaluate(
        plan=plan, node=_node(), result=WorkerResult((digest,), None, None),
    )

    whitespace_only = not payload.decode("utf-8").replace("\ufeff", "").strip()
    assert verdict.passed is not whitespace_only, (
        f"{payload!r}: whitespace-only output must fail; semantically empty output must pass, "
        "because this gate is structural and says so"
    )


def test_only_the_first_declared_artifact_is_examined(tmp_path: Path) -> None:
    """A worker returning ``(junk, real)`` is judged on ``junk``; ``(real, junk)`` on ``real``.

    ``StructuralAcceptanceGate`` reads ``digests[0]`` and nothing else, so a multi-output node is
    gated on one of its outputs. Recorded because "the gate checked the node's output" invites the
    reader to assume it checked all of them.
    """
    store = _store(tmp_path)
    plan = ExecutionPlan.__new__(ExecutionPlan)
    good = _put(store, b"a real answer")
    blank = _put(store, b"   ")

    assert _gate(store).evaluate(
        plan=plan, node=_node(), result=WorkerResult((good, blank), None, None),
    ).passed, "a blank SECOND output is never looked at"
    assert not _gate(store).evaluate(
        plan=plan, node=_node(), result=WorkerResult((blank, good), None, None),
    ).passed, "a blank FIRST output fails even though a real one follows"


def test_the_honest_headline_is_recorded_where_a_reader_will_find_it() -> None:
    """What this gate is, in one assertion on the module's own words.

    The previous version of this test compared two literals from its own parametrize list and
    invoked no gate at all — a tautology that looked like a scoreboard. The audit called it, correctly.
    What is worth pinning is that the gate's own docstring still says STRUCTURAL, so no future edit
    can quietly promote it to a correctness check without this failing.
    """
    doc = StructuralAcceptanceGate.__doc__ or ""
    module_doc = __import__(
        "bounded_loops.graph.adapters.workers.acceptance_gate", fromlist=["x"],
    ).__doc__ or ""

    assert "STRUCTURAL" in module_doc
    assert "not a semantic review" in module_doc
    assert "non-empty" in doc
