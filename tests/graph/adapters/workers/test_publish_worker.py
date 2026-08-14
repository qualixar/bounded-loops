"""Exactly-once publish worker and its receipt gate.

Key invariants under test:
  1. First call fires and records the effect in the ledger.
  2. A repeat call with the SAME payload is a no-op (already_published).
  3. A repeat call with a DIFFERENT payload raises GraphIntegrityError (HALT).
  4. A publish node with no publication_policy fails closed.
  5. Gate verifies: node_id, plan_id, effect_key derivation, payload_digest reproducibility.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.workers.publish_worker import (
    LocalPublicationLedger,
    PublishNodeWorker,
    PublishReceiptGate,
    _derive_payload_digest,
)
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.application.graph_ports import (
    ArtifactReaderPort,
    ArtifactStorePort,
)
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.domain.artifacts import (
    ArtifactAccess,
    ArtifactPolicy,
    ArtifactRecord,
    ArtifactRef,
    ArtifactState,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError

ORG = "org-pub"
PROJECT = "proj-pub"
RUN_ID = "run-pub-xyz"
PLAN_ID = "plan-pub-1"
POLICY = "finance-instruction-v1"


# ── plan stubs ────────────────────────────────────────────────────────────────
#
# These carry only the fields the publish worker and gate actually read. That is deliberate — a real
# ExecutionPlan needs a validated graph and a compile snapshot, which would make every test here a
# compiler test. The typed factories below are where that narrowing is declared ONCE, instead of an
# inline ignore at each of ~30 call sites. `mypy bounded_loops tests` is a CI gate, so an untyped
# stub handed to a typed port is a red build, not a style preference.


@dataclass
class _PlanStub:
    plan_id: str = PLAN_ID
    edges: tuple = field(default_factory=tuple)
    nodes: tuple = field(default_factory=tuple)


@dataclass
class _NodeStub:
    node_id: str = "publish-instruction"
    kind: str = "publish"
    approval_policy: dict = field(default_factory=lambda: {"publication_policy": POLICY})


def _Plan(  # noqa: N802 - a factory standing in for the constructor it replaces
    plan_id: str = PLAN_ID, edges: tuple = (), nodes: tuple = (),
) -> ExecutionPlan:
    return cast(ExecutionPlan, _PlanStub(plan_id=plan_id, edges=edges, nodes=nodes))


def _Node(  # noqa: N802
    node_id: str = "publish-instruction", kind: str = "publish",
    approval_policy: dict | None = None,
) -> PlannedNode:
    stub = _NodeStub(
        node_id=node_id, kind=kind,
        approval_policy={"publication_policy": POLICY}
        if approval_policy is None else approval_policy,
    )
    return cast(PlannedNode, stub)


def _no_envelope() -> ExecutionEnvelope:
    """The publish worker runs no subprocess, so it never reads the envelope."""
    return cast(ExecutionEnvelope, None)


# ── minimal store stub ────────────────────────────────────────────────────────

class _Handle:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _Store:
    """Minimal ArtifactStorePort. Stores blobs by SHA-256 digest."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, stream, policy: ArtifactPolicy) -> ArtifactRecord:
        data = stream.read()
        digest = hashlib.sha256(data).hexdigest()
        self._blobs[digest] = data
        ref = ArtifactRef(digest=digest, organization_id=policy.organization_id, project_id=policy.project_id)
        return ArtifactRecord(
            ref=ref,
            digest=digest,
            media_type=policy.media_type,
            size=len(data),
            producer_attempt=policy.producer_attempt,
            sensitivity=policy.sensitivity,
            retention_class=policy.retention_class,
            state=ArtifactState.ACTIVE,
            tombstone_reason=None,
        )

    @contextmanager
    def open(self, ref: ArtifactRef, access):
        if ref.digest not in self._blobs:
            raise FileNotFoundError(ref.digest)
        yield _Handle(self._blobs[ref.digest])


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_worker(
    store: _Store,
    ledger: LocalPublicationLedger,
    *,
    run_id: str = RUN_ID,
) -> PublishNodeWorker:
    return PublishNodeWorker(
        store=_store_port(store),
        ledger=ledger,
        run_id=run_id,
        organization_id=ORG,
        project_id=PROJECT,
    )


def _run_worker(
    store: _Store,
    ledger: LocalPublicationLedger,
    *,
    node_id: str = "publish-instruction",
    policy: str = POLICY,
    plan_id: str = PLAN_ID,
    run_id: str = RUN_ID,
    attempt: int = 1,
) -> WorkerResult:
    node = _Node(node_id=node_id, approval_policy={"publication_policy": policy})
    plan = _Plan(plan_id=plan_id)
    worker = _make_worker(store, ledger, run_id=run_id)
    return worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=attempt, repair_round=0)


def _read_receipt(store: _Store, digest: str) -> dict:
    with store.open(ArtifactRef(digest, ORG, PROJECT), ArtifactAccess(ORG, PROJECT)) as h:
        return json.loads(h.read())


# ── ledger unit tests ─────────────────────────────────────────────────────────

def test_ledger_first_call_returns_fired(tmp_path: Path):
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    result = ledger.check_and_record("run/plan/node", "digest-abc")
    assert result == "fired"


def test_ledger_same_payload_returns_already_published(tmp_path: Path):
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    ledger.check_and_record("run/plan/node", "digest-abc")
    result = ledger.check_and_record("run/plan/node", "digest-abc")
    assert result == "already_published"


def test_ledger_different_payload_raises_integrity_error(tmp_path: Path):
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    ledger.check_and_record("run/plan/node", "digest-abc")
    with pytest.raises(GraphIntegrityError, match="different payload"):
        ledger.check_and_record("run/plan/node", "digest-DIFFERENT")


def test_ledger_distinct_effect_keys_are_independent(tmp_path: Path):
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    assert ledger.check_and_record("run/plan/node-a", "digest-1") == "fired"
    assert ledger.check_and_record("run/plan/node-b", "digest-2") == "fired"


# ── worker tests ──────────────────────────────────────────────────────────────

def test_worker_produces_one_artifact_digest(tmp_path: Path):
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    result = _run_worker(store, ledger)
    assert len(result.output_artifact_digests) == 1


def test_worker_receipt_outcome_is_fired_on_first_attempt(tmp_path: Path):
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    result = _run_worker(store, ledger)
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    assert receipt["outcome"] == "fired"


def test_same_payload_second_attempt_is_noop(tmp_path: Path):
    """Second attempt with the same (run_id, plan_id, node_id, policy) → already_published, no error."""
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    result_1 = _run_worker(store, ledger, attempt=1)
    result_2 = _run_worker(store, ledger, attempt=2)

    receipt_1 = _read_receipt(store, result_1.output_artifact_digests[0])
    receipt_2 = _read_receipt(store, result_2.output_artifact_digests[0])
    assert receipt_1["outcome"] == "fired"
    assert receipt_2["outcome"] == "already_published"


def test_divergent_payload_second_attempt_halts(tmp_path: Path):
    """Changing publication_policy mid-run must HALT — the effect cannot fire twice with different content."""
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    # First attempt fires with policy v1.
    _run_worker(store, ledger, policy="finance-instruction-v1", attempt=1)
    # A hypothetical second run with a different policy against the same (run, plan, node) must HALT.
    with pytest.raises(GraphIntegrityError, match="different payload"):
        _run_worker(store, ledger, policy="finance-instruction-v2", attempt=2)


def test_worker_fails_closed_on_missing_policy(tmp_path: Path):
    """A publish node whose publication_policy is None or empty must fail closed."""
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    with pytest.raises(GraphIntegrityError, match="no publication_policy"):
        node = _Node(node_id="publish-instruction", approval_policy={"publication_policy": ""})
        plan = _Plan()
        worker = _make_worker(store, ledger)
        worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)


def test_worker_receipt_encodes_effect_key(tmp_path: Path):
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    result = _run_worker(store, ledger)
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    expected_key = f"{RUN_ID}/{PLAN_ID}/publish-instruction"
    assert receipt["effect_key"] == expected_key


def test_worker_receipt_payload_digest_is_reproducible(tmp_path: Path):
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    result = _run_worker(store, ledger)
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    # upstream_digests=() because this fixture wires no reader — the degraded identity-only case.
    # The receipt makes that visible rather than silent via upstream_artifact_count.
    expected_digest = _derive_payload_digest(
        publication_policy=POLICY, plan_id=PLAN_ID, node_id="publish-instruction",
        upstream_digests=(),
    )
    assert receipt["payload_digest"] == expected_digest
    assert receipt["upstream_artifact_count"] == 0


def test_the_payload_digest_changes_when_the_upstream_evidence_changes():
    """The property that makes this a PAYLOAD digest rather than an identity stamp.

    Before this, the digest hashed only {node_id, plan_id, publication_policy}, so the documented
    divergent-payload HALT was unreachable from a compiled plan: publication_policy lives inside
    approval_policy, which is inside _canonical_plan, so changing it changes plan_id and therefore
    changes the effect KEY as well. Same key + different digest could only be produced by mutating
    approval_policy in memory — which is what the HALT test did, making it a property of a hand-built
    object rather than of any graph that could be compiled. Found by the P4.5 audit (Grok finding 1).
    """
    fixed = dict(publication_policy=POLICY, plan_id=PLAN_ID, node_id="publish-instruction")

    none = _derive_payload_digest(**fixed, upstream_digests=())
    one = _derive_payload_digest(**fixed, upstream_digests=("sha256:" + "a" * 64,))
    other = _derive_payload_digest(**fixed, upstream_digests=("sha256:" + "b" * 64,))

    assert len({none, one, other}) == 3, "different evidence must give a different payload digest"
    # Order must NOT matter: the same evidence arriving in a different edge order is the same
    # publication, and treating it as divergent would HALT a healthy retry.
    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    assert _derive_payload_digest(**fixed, upstream_digests=(a, b)) == _derive_payload_digest(
        **fixed, upstream_digests=(b, a)
    )


def _store_port(store: _Store) -> "ArtifactStorePort":
    """``_Store.open`` is a ``@contextmanager``; the port declares ``-> BinaryIO``.

    Both work under ``with``, and a real ``BinaryIO`` is its own context manager — so the stub is
    usable but not structurally the port. Cast once, here, rather than at each construction site.
    """
    return cast("ArtifactStorePort", store)


# ── gate tests ────────────────────────────────────────────────────────────────

def _make_gate(store: _Store, *, run_id: str = RUN_ID) -> PublishReceiptGate:
    return PublishReceiptGate(
        cast("ArtifactReaderPort", store),
        run_id=run_id, organization_id=ORG, project_id=PROJECT,
    )


def test_gate_accepts_correct_receipt(tmp_path: Path):
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    node = _Node()
    plan = _Plan()
    worker = _make_worker(store, ledger)
    result = worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)

    gate = _make_gate(store)
    verdict = gate.evaluate(plan=plan, node=node, result=result, attempt=1, repair_round=0)
    assert verdict.passed, f"gate rejected a valid receipt: {verdict.reason}"


def test_gate_rejects_wrong_node_id(tmp_path: Path):
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    node = _Node(node_id="publish-instruction")
    plan = _Plan()
    worker = _make_worker(store, ledger)
    result = worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)

    # Gate evaluates a different node — must not accept the receipt.
    impersonator = _Node(node_id="publish-other")
    gate = _make_gate(store)
    verdict = gate.evaluate(plan=plan, node=impersonator, result=result, attempt=1, repair_round=0)
    assert not verdict.passed
    assert "publish-other" in verdict.reason or "publish-instruction" in verdict.reason


def test_gate_rejects_wrong_effect_key(tmp_path: Path):
    """Effect key must derive from run_id / plan_id / node_id — a gate with a different run_id must reject."""
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    # Worker wrote receipt with RUN_ID.
    node = _Node()
    plan = _Plan()
    worker = _make_worker(store, ledger, run_id=RUN_ID)
    result = worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)

    # Gate holds a DIFFERENT run_id — its expected effect_key doesn't match.
    gate = _make_gate(store, run_id="run-DIFFERENT")
    verdict = gate.evaluate(plan=plan, node=node, result=result, attempt=1, repair_round=0)
    assert not verdict.passed
    assert "effect_key" in verdict.reason


def test_gate_rejects_tampered_payload_digest(tmp_path: Path):
    """Gate recomputes the digest from the plan; a tampered receipt must not pass."""
    store = _Store()
    ledger = LocalPublicationLedger(tmp_path / "ledger.json")
    node = _Node()
    plan = _Plan()
    worker = _make_worker(store, ledger)
    result = worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)

    # Gate evaluates the same node but with a DIFFERENT publication_policy in approval_policy.
    # The gate recomputes the digest from this different policy and it won't match the receipt.
    tampered_node = _Node(node_id=node.node_id, approval_policy={"publication_policy": "wrong-policy"})
    gate = _make_gate(store)
    verdict = gate.evaluate(plan=plan, node=tampered_node, result=result, attempt=1, repair_round=0)
    assert not verdict.passed
    assert "payload_digest" in verdict.reason


def test_gate_rejects_empty_result(tmp_path: Path):
    gate = _make_gate(_Store())
    verdict = gate.evaluate(plan=_Plan(), node=_Node(), result=WorkerResult(()), attempt=1, repair_round=0)
    assert not verdict.passed
    assert "no receipt" in verdict.reason


def test_a_corrupt_ledger_halts_instead_of_re_firing_the_effect(tmp_path: Path):
    """The most dangerous default there was: a damaged burn record reading as empty.

    ``_load`` swallowed every error and returned ``{}``. A partial write — a crash mid
    ``write_text``, a full disk — yields invalid JSON, the ledger reads as empty, every burned key
    looks fresh, and the irreversible effect FIRES AGAIN. Absent and unreadable are different
    facts, and only the first means "nothing has been published".
    """
    path = tmp_path / "ledger.json"
    ledger = LocalPublicationLedger(path)
    assert ledger.check_and_record("run/plan/node", "digest-abc") == "fired"
    path.write_text('{"run/plan/node": "digest-abc"', encoding="utf-8")  # truncated mid-write

    with pytest.raises(GraphIntegrityError, match="corrupt"):
        ledger.check_and_record("run/plan/node", "digest-abc")


def test_a_ledger_that_is_not_an_object_halts(tmp_path: Path):
    path = tmp_path / "ledger.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")

    with pytest.raises(GraphIntegrityError, match="not a JSON object"):
        LocalPublicationLedger(path).check_and_record("run/plan/node", "d")


def test_a_missing_ledger_is_still_treated_as_empty(tmp_path: Path):
    # Absent genuinely IS a fresh ledger; only unreadable is the dangerous case.
    ledger = LocalPublicationLedger(tmp_path / "never-written.json")

    assert ledger.check_and_record("run/plan/node", "d") == "fired"
