"""Join worker produces a causal receipt; join gate verifies it against the plan.

Worker invariant: receipt encodes node_id + join_mode + sorted predecessor IDs.
Gate invariant:  reads the receipt independently and rejects any mismatch with the plan.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field


from bounded_loops.graph.adapters.workers.join_worker import JoinNodeWorker, JoinReceiptGate
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.domain.artifacts import (
    ArtifactAccess,
    ArtifactPolicy,
    ArtifactRecord,
    ArtifactRef,
    ArtifactState,
)

ORG = "org-join"
PROJECT = "proj-join"


# ── plan stubs ────────────────────────────────────────────────────────────────

@dataclass
class _Edge:
    from_node: str
    to_node: str
    from_port: str = "default"
    to_port: str = "default"
    when: str | None = None


@dataclass
class _Plan:
    edges: tuple[_Edge, ...] = field(default_factory=tuple)
    plan_id: str = "plan-join-1"


@dataclass
class _Node:
    node_id: str = "join-checks"
    kind: str = "join"
    approval_policy: dict = field(default_factory=lambda: {"join_mode": "all_successful"})


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


# ── helper: run worker and decode receipt ─────────────────────────────────────

def _run(
    *,
    node_id: str = "join-checks",
    join_mode: str = "all_successful",
    predecessors: list[str] | None = None,
) -> tuple[_Store, _Node, _Plan, WorkerResult]:
    if predecessors is None:
        predecessors = ["check-ledger", "check-fx", "check-journal"]
    node = _Node(node_id=node_id, approval_policy={"join_mode": join_mode})
    edges = tuple(_Edge(from_node=p, to_node=node_id) for p in predecessors)
    plan = _Plan(edges=edges)
    store = _Store()
    worker = JoinNodeWorker(store=store, organization_id=ORG, project_id=PROJECT)
    result = worker.execute(plan=plan, node=node, envelope=None, attempt=1)
    return store, node, plan, result


def _read_receipt(store: _Store, digest: str) -> dict:
    with store.open(ArtifactRef(digest, ORG, PROJECT), ArtifactAccess(ORG, PROJECT)) as h:
        return json.loads(h.read())


# ── worker tests ──────────────────────────────────────────────────────────────

def test_worker_produces_exactly_one_artifact_digest():
    _, _, _, result = _run()
    assert len(result.output_artifact_digests) == 1


def test_worker_receipt_encodes_node_id():
    store, node, _, result = _run()
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    assert receipt["node_id"] == node.node_id


def test_worker_receipt_encodes_join_mode():
    store, node, _, result = _run(join_mode="any_successful")
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    assert receipt["join_mode"] == "any_successful"


def test_worker_receipt_lists_predecessors_sorted():
    preds = ["check-fx", "check-ledger", "check-journal"]
    store, _, _, result = _run(predecessors=preds)
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    assert receipt["predecessor_node_ids"] == sorted(preds)


def test_worker_with_no_predecessors_records_empty_list():
    store, _, _, result = _run(predecessors=[])
    receipt = _read_receipt(store, result.output_artifact_digests[0])
    assert receipt["predecessor_node_ids"] == []


# ── gate tests: correct path ──────────────────────────────────────────────────

def test_gate_accepts_correct_receipt():
    store, node, plan, result = _run()
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=plan, node=node, result=result)
    assert verdict.passed, f"gate rejected a valid receipt: {verdict.reason}"


def test_gate_verdict_names_mode_and_predecessors():
    store, node, plan, result = _run(
        join_mode="all_successful", predecessors=["a", "b"],
    )
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=plan, node=node, result=result)
    assert verdict.passed
    assert "all_successful" in verdict.reason


# ── gate tests: rejection paths ───────────────────────────────────────────────

def test_gate_rejects_wrong_join_mode():
    # Worker wrote receipt with mode "all_successful"; gate checks node that declares "any_successful".
    store, node, plan, result = _run(join_mode="all_successful")
    wrong_node = _Node(node_id=node.node_id, approval_policy={"join_mode": "any_successful"})
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=plan, node=wrong_node, result=result)
    assert not verdict.passed
    assert "any_successful" in verdict.reason or "all_successful" in verdict.reason


def test_gate_rejects_extra_predecessor_in_plan():
    # Receipt recorded two predecessors; plan now claims three.
    store, node, plan, result = _run(predecessors=["check-a", "check-b"])
    extended_plan = _Plan(edges=(
        _Edge(from_node="check-a", to_node=node.node_id),
        _Edge(from_node="check-b", to_node=node.node_id),
        _Edge(from_node="check-extra", to_node=node.node_id),
    ))
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=extended_plan, node=node, result=result)
    assert not verdict.passed


def test_gate_rejects_missing_predecessor_in_plan():
    # Receipt recorded three predecessors; plan now only has two.
    store, node, plan, result = _run(predecessors=["check-a", "check-b", "check-c"])
    shortened_plan = _Plan(edges=(
        _Edge(from_node="check-a", to_node=node.node_id),
        _Edge(from_node="check-b", to_node=node.node_id),
    ))
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=shortened_plan, node=node, result=result)
    assert not verdict.passed


def test_gate_rejects_wrong_node_id_in_receipt():
    # Receipt was written for "join-checks" but gate is verifying "join-other".
    store, node, plan, result = _run(node_id="join-checks")
    impersonator = _Node(node_id="join-other", approval_policy={"join_mode": "all_successful"})
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=plan, node=impersonator, result=result)
    assert not verdict.passed
    # Error message must name one of the two IDs so the caller can diagnose the mismatch.
    assert "join-other" in verdict.reason or "join-checks" in verdict.reason


def test_gate_rejects_empty_result():
    gate = JoinReceiptGate(_Store(), organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=_Plan(), node=_Node(), result=WorkerResult(()))
    assert not verdict.passed
    assert "no causality receipt" in verdict.reason
