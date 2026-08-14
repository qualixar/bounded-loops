"""Join worker produces a causal receipt; join gate verifies it against the plan.

Worker invariant: receipt encodes node_id + join_mode + sorted predecessor IDs.
Gate invariant:  reads the receipt independently and rejects any mismatch with the plan.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast


from bounded_loops.graph.adapters.workers.join_worker import JoinNodeWorker, JoinReceiptGate
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.graph_ports import ArtifactStorePort
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
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
class _PlanStub:
    edges: tuple[_Edge, ...] = field(default_factory=tuple)
    plan_id: str = "plan-join-1"


@dataclass
class _NodeStub:
    node_id: str = "join-checks"
    kind: str = "join"
    approval_policy: dict = field(default_factory=lambda: {"join_mode": "all_successful"})


# The stubs above carry only the fields the join worker and gate read. A real ExecutionPlan needs a
# validated graph and a compile snapshot, which would turn every test here into a compiler test.
# These factories declare that narrowing once — `mypy bounded_loops tests` is a CI gate, so an
# untyped stub handed to a typed port is a red build.


def _Plan(  # noqa: N802 - a factory standing in for the constructor it replaces
    edges: tuple[_Edge, ...] = (), plan_id: str = "plan-join-1",
) -> ExecutionPlan:
    return cast(ExecutionPlan, _PlanStub(edges=edges, plan_id=plan_id))


def _Node(  # noqa: N802
    node_id: str = "join-checks", kind: str = "join", approval_policy: dict | None = None,
) -> PlannedNode:
    stub = _NodeStub(
        node_id=node_id, kind=kind,
        approval_policy={"join_mode": "all_successful"}
        if approval_policy is None else approval_policy,
    )
    return cast(PlannedNode, stub)


def _no_envelope() -> ExecutionEnvelope:
    """The join worker runs no subprocess, so it never reads the envelope."""
    return cast(ExecutionEnvelope, None)


def _store_port(store: "_Store") -> ArtifactStorePort:
    """``_Store.open`` is a ``@contextmanager``; the port declares ``-> BinaryIO``. Both work under
    ``with``, so the stub is usable but not structurally the port."""
    return cast(ArtifactStorePort, store)


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
) -> tuple[_Store, PlannedNode, ExecutionPlan, WorkerResult]:
    if predecessors is None:
        predecessors = ["check-ledger", "check-fx", "check-journal"]
    node = _Node(node_id=node_id, approval_policy={"join_mode": join_mode})
    edges = tuple(_Edge(from_node=p, to_node=node_id) for p in predecessors)
    plan = _Plan(edges=edges)
    store = _Store()
    worker = JoinNodeWorker(
        store=_store_port(store), organization_id=ORG, project_id=PROJECT,
    )
    result = worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)
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
    verdict = gate.evaluate(plan=plan, node=node, result=result, attempt=1, repair_round=0)
    assert verdict.passed, f"gate rejected a valid receipt: {verdict.reason}"


def test_a_receipt_with_no_observed_parents_passes_but_says_causality_is_unverified():
    """Honest degradation. With no event log wired the worker cannot see parent states, so the gate
    checks the plan shape only — and SAYS so instead of reporting "causality verified".

    That distinction is the whole finding: previously both worker and gate read the plan, so the
    causality claim compared the compiler to itself and could not fail.
    """
    store, node, plan, result = _run(
        join_mode="all_successful", predecessors=["a", "b"],
    )
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)

    verdict = gate.evaluate(plan=plan, node=node, result=result, attempt=1, repair_round=0)

    assert verdict.passed
    assert "NOT observed" in verdict.reason
    assert "unverified" in verdict.reason


# ── gate tests: rejection paths ───────────────────────────────────────────────

def test_gate_rejects_wrong_join_mode():
    # Worker wrote receipt with mode "all_successful"; gate checks node that declares "any_successful".
    store, node, plan, result = _run(join_mode="all_successful")
    wrong_node = _Node(node_id=node.node_id, approval_policy={"join_mode": "any_successful"})
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=plan, node=wrong_node, result=result, attempt=1, repair_round=0)
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
    verdict = gate.evaluate(plan=extended_plan, node=node, result=result, attempt=1, repair_round=0)
    assert not verdict.passed


def test_gate_rejects_missing_predecessor_in_plan():
    # Receipt recorded three predecessors; plan now only has two.
    store, node, plan, result = _run(predecessors=["check-a", "check-b", "check-c"])
    shortened_plan = _Plan(edges=(
        _Edge(from_node="check-a", to_node=node.node_id),
        _Edge(from_node="check-b", to_node=node.node_id),
    ))
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=shortened_plan, node=node, result=result, attempt=1, repair_round=0)
    assert not verdict.passed


def test_gate_rejects_wrong_node_id_in_receipt():
    # Receipt was written for "join-checks" but gate is verifying "join-other".
    store, node, plan, result = _run(node_id="join-checks")
    impersonator = _Node(node_id="join-other", approval_policy={"join_mode": "all_successful"})
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=plan, node=impersonator, result=result, attempt=1, repair_round=0)
    assert not verdict.passed
    # Error message must name one of the two IDs so the caller can diagnose the mismatch.
    assert "join-other" in verdict.reason or "join-checks" in verdict.reason


def test_gate_rejects_empty_result():
    gate = JoinReceiptGate(_Store(), organization_id=ORG, project_id=PROJECT)
    verdict = gate.evaluate(plan=_Plan(), node=_Node(), result=WorkerResult(()), attempt=1, repair_round=0)
    assert not verdict.passed
    assert "no causality receipt" in verdict.reason


# ---------------------------------------------------------------------------
# Causality replay. These are the tests the previous receipt shape could not have.
# ---------------------------------------------------------------------------


def _run_with_states(
    *,
    join_mode: str,
    parents: dict[str, str],
    guards: dict[str, str | None] | None = None,
    node_id: str = "join-checks",
):
    """Run a join whose worker can SEE its parents' live states."""
    guards = guards or {}
    node = _Node(node_id=node_id, approval_policy={"join_mode": join_mode})
    edges = tuple(
        _Edge(from_node=name, to_node=node_id, when=guards.get(name)) for name in parents
    )
    plan = _Plan(edges=edges)
    store = _Store()
    worker = JoinNodeWorker(
        store=_store_port(store), organization_id=ORG, project_id=PROJECT,
        node_states_fn=lambda _plan: dict(parents),
    )
    result = worker.execute(plan=plan, node=node, envelope=_no_envelope(), attempt=1, repair_round=0)
    return store, node, plan, result


def test_the_receipt_records_the_live_parent_states_not_just_the_edge_list():
    store, _, _, result = _run_with_states(
        join_mode="all_successful", parents={"a": "SUCCEEDED", "b": "SUCCEEDED"},
    )

    receipt = _read_receipt(store, result.output_artifact_digests[0])

    assert receipt["parents_observed"] is True
    assert sorted(entry[0] for entry in receipt["parents"]) == ["a", "b"]
    assert all(entry[1] == "SUCCEEDED" for entry in receipt["parents"])


def test_a_join_whose_recorded_states_would_not_admit_it_is_refused():
    """The check the old receipt shape made impossible.

    ``all_successful`` with a FAILED parent must not admit. Previously the receipt carried only the
    plan's edge list, so the gate re-read the plan and passed — a silent wrong number in a
    hash-chained receipt. Now the gate replays the scheduler's own predicate over the recorded facts.
    """
    store, node, plan, result = _run_with_states(
        join_mode="all_successful", parents={"a": "SUCCEEDED", "b": "FAILED"},
    )
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)

    verdict = gate.evaluate(plan=plan, node=node, result=result, attempt=1, repair_round=0)

    assert not verdict.passed
    assert "would have produced" in verdict.reason


def test_a_guard_excluded_parent_does_not_block_the_join():
    """The exact shape from the audit: ``a --when:succeeded-->`` and ``b --when:failed-->``.

    With ``a`` SUCCEEDED, the scheduler EXCLUDES ``b`` because its failed-guard is unsatisfied, and
    admits. The replay must agree — the recorded facts include ``b``, but with its guard, so the
    predicate can see that it was excluded rather than ignored.
    """
    store, node, plan, result = _run_with_states(
        join_mode="all_successful",
        parents={"a": "SUCCEEDED", "b": "SUCCEEDED"},
        guards={"a": "succeeded", "b": "failed"},
    )
    gate = JoinReceiptGate(store, organization_id=ORG, project_id=PROJECT)

    verdict = gate.evaluate(plan=plan, node=node, result=result, attempt=1, repair_round=0)

    assert verdict.passed, verdict.reason
    # And the guard travelled into the receipt, so the exclusion is auditable rather than implied.
    guards_recorded = {entry[0]: entry[2] for entry in _read_receipt(
        store, result.output_artifact_digests[0]
    )["parents"]}
    assert guards_recorded == {"a": "succeeded", "b": "failed"}
