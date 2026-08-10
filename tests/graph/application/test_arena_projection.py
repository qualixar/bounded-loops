from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    latest_node_states,
    read_arena_projection,
)
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedEdge, PlannedNode, ResolvedBinding


def _plan() -> ExecutionPlan:
    source = "sha256:" + "a" * 64
    policy = "sha256:" + "c" * 64
    canonical = b'{"plan":"arena-fixture"}'
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "b" * 64,
        source_graph_digest=source, policy_digest=policy,
        compiler_version="bounded-loops.graph-compiler/v1",
        nodes=(
            PlannedNode(
                node_id="research", kind="research_claim", package_digest=None,
                binding_id=None, required_effects=frozenset({Effect.READ_ONLY}),
                isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1000,
                budgets={"max_attempts": 1}, approval_policy={},
            ),
            PlannedNode(
                node_id="review", kind="approval", package_digest=None,
                binding_id=None, required_effects=frozenset({Effect.READ_ONLY}),
                isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1000,
                budgets={"max_attempts": 1}, approval_policy={"required": True},
            ),
        ),
        edges=(PlannedEdge("research", "claim", "review", "claim", None),),
        levels=(("research",), ("review",)), package_digests=(), connection_bindings=(),
        canonical_json=canonical,
    )


def _identity(plan: ExecutionPlan) -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )


def _bound_plan() -> ExecutionPlan:
    plan = _plan()
    binding = ResolvedBinding(
        binding_id="binding-1", slot_id="model", connector_id="codex-cli",
        connector_version="1.0.0", connection_id="conn-1",
        admission_digest="sha256:" + "d" * 64,
        route_policy_digest="sha256:" + "e" * 64, provider_id="openai",
        model_target="codex", region="in", fallback=False, transport="local_cli",
    )
    return replace(
        plan, nodes=(replace(plan.nodes[0], binding_id=binding.binding_id), plan.nodes[1]),
        connection_bindings=(binding,),
    )


def _join_plan() -> ExecutionPlan:
    """Two roots (``a``, ``b``) feeding an ``any_successful`` ``join``: the join may
    leave PENDING as soon as ONE parent has SUCCEEDED, even while the other is still
    PENDING (see ``schedule_ready`` join semantics). Used to prove the causality
    guard does not regress legitimate join admission."""
    def _leaf(node_id: str, kind: str, approval: dict[str, object]) -> PlannedNode:
        return PlannedNode(
            node_id=node_id, kind=kind, package_digest=None, binding_id=None,
            required_effects=frozenset({Effect.READ_ONLY}), isolation=IsolationLevel.WORKSPACE_ONLY,
            hard_deadline_ms=1000, budgets={"max_attempts": 1}, approval_policy=approval,
        )

    return replace(
        _plan(),
        nodes=(
            _leaf("a", "research_claim", {}),
            _leaf("b", "research_claim", {}),
            _leaf("join", "join", {"join_mode": "any_successful"}),
        ),
        edges=(
            PlannedEdge("a", "out", "join", "left", None),
            PlannedEdge("b", "out", "join", "right", None),
        ),
        levels=(("a", "b"), ("join",)),
    )


def _append(store: GraphEventLog, head: str, event_type: str, key: str, payload: dict[str, object]) -> str:
    return store.append(
        head,
        UnsignedGraphEvent(
            event_id=f"event-{key}", idempotency_key=key, event_type=event_type,
            timestamp="2026-08-08T00:00:00Z", actor="controller", payload=payload,
        ),
    ).event_hash


@dataclass
class _Authorizer:
    allowed: bool

    def authorize(self, request: ArenaReadRequest) -> bool:
        return self.allowed


@dataclass
class _ReceiptVerifier:
    trusted: bool

    def verify(self, identity, receipts) -> None:
        if not self.trusted:
            raise GraphIntegrityError("receipt signature is untrusted")


def _request(plan: ExecutionPlan, *, organization_id: str = "org-1", project_id: str = "project-1") -> ArenaReadRequest:
    return ArenaReadRequest("viewer-1", organization_id, project_id, "run-1")


def _read(plan: ExecutionPlan, store: GraphEventLog, *, request: ArenaReadRequest | None = None, allowed: bool = True, trusted: bool = True):
    return read_arena_projection(
        plan, store, request or _request(plan), _Authorizer(allowed), _ReceiptVerifier(trusted),
    )


def test_arena_projection_is_receipt_derived_and_does_not_mutate_execution_evidence(tmp_path):
    plan = _plan()
    path = tmp_path / "events.jsonl"
    store = GraphEventLog(path, _identity(plan))
    head = "0" * 64
    head = _append(store, head, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    head = _append(store, head, "node.ready", "ready", {"node_id": "research", "state": "READY", "attempt": 1})
    head = _append(store, head, "node.starting", "starting", {"node_id": "research", "state": "STARTING", "attempt": 1})
    head = _append(store, head, "node.running", "running", {"node_id": "research", "state": "RUNNING", "attempt": 1})
    head = _append(store, head, "node.gating", "gating", {"node_id": "research", "state": "GATING", "attempt": 1})
    head = _append(store, head, "node.succeeded", "succeeded", {
        "node_id": "research", "state": "SUCCEEDED", "attempt": 1,
        "artifact_digests": ["sha256:" + "d" * 64],
    })
    head = _append(store, head, "node.ready", "review-ready", {"node_id": "review", "state": "READY", "attempt": 1})
    # review is an approval (human-gate) node: it reaches SUCCEEDED via the approval
    # lifecycle (READY -> AWAITING_APPROVAL -> SUCCEEDED), never the worker path.
    head = _append(store, head, "node.awaiting_approval", "review-awaiting", {"node_id": "review", "state": "AWAITING_APPROVAL", "attempt": 1})
    head = _append(store, head, "node.succeeded", "review-succeeded", {
        "node_id": "review", "state": "SUCCEEDED", "attempt": 1, "artifact_digests": [],
    })
    _append(store, head, "run.succeeded", "done", {"state": "SUCCEEDED"})
    before = path.read_bytes()

    arena = _read(plan, store)

    assert path.read_bytes() == before
    assert arena.run_state == "SUCCEEDED"
    assert arena.levels == (("research",), ("review",))
    assert [(node.node_id, node.state, node.attempt) for node in arena.nodes] == [
        ("research", "SUCCEEDED", 1), ("review", "SUCCEEDED", 1),
    ]
    assert arena.nodes[0].artifact_digests == ("sha256:" + "d" * 64,)
    assert arena.edges == (("research", "review"),)


def test_arena_projection_rejects_a_receipt_stream_for_a_different_immutable_plan(tmp_path):
    plan = _plan()
    foreign = ExecutionPlan(
        api_version=plan.api_version, plan_id="sha256:" + "e" * 64,
        source_graph_digest=plan.source_graph_digest, policy_digest=plan.policy_digest,
        compiler_version=plan.compiler_version, nodes=plan.nodes, edges=plan.edges,
        levels=plan.levels, package_digests=plan.package_digests,
        connection_bindings=plan.connection_bindings, canonical_json=plan.canonical_json,
    )
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    with pytest.raises(GraphIntegrityError, match="immutable plan"):
        _read(foreign, store)


def test_arena_projection_denies_a_cross_tenant_reader_and_an_untrusted_receipt(tmp_path):
    plan = _plan()
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    with pytest.raises(GraphIntegrityError, match="tenant"):
        _read(plan, store, request=_request(plan, organization_id="other-org"))
    with pytest.raises(GraphIntegrityError, match="unauthorized"):
        _read(plan, store, allowed=False)
    with pytest.raises(GraphIntegrityError, match="untrusted"):
        _read(plan, store, trusted=False)


def test_arena_projection_rejects_succeeded_run_with_pending_node_or_illegal_lifecycle(tmp_path):
    plan = _plan()
    store = GraphEventLog(tmp_path / "pending.jsonl", _identity(plan))
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    _append(store, head, "run.succeeded", "done", {"state": "SUCCEEDED"})
    with pytest.raises(GraphIntegrityError, match="planned node"):
        _read(plan, store)

    invalid = GraphEventLog(tmp_path / "invalid.jsonl", _identity(plan))
    head = _append(invalid, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(invalid, head, "run.started", "started", {"state": "RUNNING"})
    _append(invalid, head, "node.running", "running", {"node_id": "research", "state": "RUNNING", "attempt": 1})
    with pytest.raises(GraphIntegrityError, match="lifecycle"):
        _read(plan, invalid)


def test_arena_projection_rejects_route_or_transport_on_an_unbound_node(tmp_path):
    plan = _plan()
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    head = _append(store, head, "node.ready", "ready", {"node_id": "research", "state": "READY", "attempt": 1})
    head = _append(store, head, "node.starting", "starting", {"node_id": "research", "state": "STARTING", "attempt": 1})
    head = _append(store, head, "node.running", "running", {"node_id": "research", "state": "RUNNING", "attempt": 1})
    head = _append(store, head, "node.gating", "gating", {"node_id": "research", "state": "GATING", "attempt": 1})
    _append(store, head, "node.succeeded", "succeeded", {
        "node_id": "research", "state": "SUCCEEDED", "attempt": 1, "artifact_digests": [],
        "transport": "local_cli",
    })
    with pytest.raises(GraphIntegrityError, match="unbound"):
        _read(plan, store)


def test_arena_projection_preserves_only_a_route_and_transport_matching_the_binding(tmp_path):
    plan = _bound_plan()
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    head = _append(store, head, "node.ready", "ready", {"node_id": "research", "state": "READY", "attempt": 1})
    head = _append(store, head, "node.starting", "starting", {"node_id": "research", "state": "STARTING", "attempt": 1})
    head = _append(store, head, "node.running", "running", {"node_id": "research", "state": "RUNNING", "attempt": 1})
    head = _append(store, head, "node.gating", "gating", {"node_id": "research", "state": "GATING", "attempt": 1})
    _append(store, head, "node.succeeded", "succeeded", {
        "node_id": "research", "state": "SUCCEEDED", "attempt": 1, "artifact_digests": [],
        "route": {"provider_id": "openai", "model_id": "codex", "region": "in", "fallback": False, "policy_digest": "sha256:" + "e" * 64},
        "transport": "local_cli",
    })

    arena = _read(plan, store)

    assert arena.nodes[0].route == ("openai", "codex", "in", False, "sha256:" + "e" * 64)
    assert arena.nodes[0].transport == "local_cli"


def test_arena_projection_rejects_a_child_that_succeeds_before_its_parent(tmp_path):
    """A tampered, fully re-hash-chained stream can keep every per-node lifecycle
    legal yet invert DAG order — here the child ``review`` runs to SUCCEEDED while its
    parent ``research`` never leaves PENDING. The per-node ``_ALLOWED`` lifecycle
    cannot see this; the cross-node causality guard must fail closed (finding H4a)."""
    plan = _plan()
    store = GraphEventLog(tmp_path / "forged.jsonl", _identity(plan))
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    head = _append(store, head, "node.ready", "review-ready", {"node_id": "review", "state": "READY", "attempt": 1})
    head = _append(store, head, "node.starting", "review-starting", {"node_id": "review", "state": "STARTING", "attempt": 1})
    head = _append(store, head, "node.running", "review-running", {"node_id": "review", "state": "RUNNING", "attempt": 1})
    head = _append(store, head, "node.gating", "review-gating", {"node_id": "review", "state": "GATING", "attempt": 1})
    _append(store, head, "node.succeeded", "review-succeeded", {
        "node_id": "review", "state": "SUCCEEDED", "attempt": 1, "artifact_digests": [],
    })

    with pytest.raises(GraphIntegrityError, match="causal"):
        _read(plan, store)


def test_latest_node_states_admits_a_valid_any_successful_join_with_an_unfinished_parent(tmp_path):
    """The causality guard is the dual of the scheduler's admission rule, so it must
    NOT reject a legitimate ``any_successful`` join that proceeds while one parent is
    still PENDING. Guards Option B against a naive all-predecessors-SUCCEEDED rule
    that would break join semantics (schedule_ready)."""
    plan = _join_plan()
    store = GraphEventLog(tmp_path / "join.jsonl", _identity(plan))
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    lifecycle = (("node.ready", "READY"), ("node.starting", "STARTING"), ("node.running", "RUNNING"), ("node.gating", "GATING"))
    for node_id in ("a", "join"):  # NB: parent 'b' is never driven — it stays PENDING
        for event_type, state in lifecycle:
            head = _append(store, head, event_type, f"{node_id}-{state}", {"node_id": node_id, "state": state, "attempt": 1})
        head = _append(store, head, "node.succeeded", f"{node_id}-succeeded", {
            "node_id": node_id, "state": "SUCCEEDED", "attempt": 1, "artifact_digests": [],
        })

    latest = latest_node_states(plan, store.replay())

    assert latest["a"]["state"] == "SUCCEEDED"
    assert latest["b"]["state"] == "PENDING"
    assert latest["join"]["state"] == "SUCCEEDED"


def test_arena_projection_rejects_a_non_approval_node_awaiting_approval(tmp_path):
    """AWAITING_APPROVAL is an approval-node-only hold. A forged, fully re-hash-chained
    log that parks a non-approval node (``research``, kind research_claim) there — a
    shortcut that would otherwise skip the worker+gate lifecycle — must be rejected
    even though the raw READY->AWAITING_APPROVAL edge is in the kind-agnostic table."""
    plan = _plan()
    store = GraphEventLog(tmp_path / "forged.jsonl", _identity(plan))
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    head = _append(store, head, "run.started", "started", {"state": "RUNNING"})
    head = _append(store, head, "node.ready", "research-ready", {"node_id": "research", "state": "READY", "attempt": 1})
    _append(store, head, "node.awaiting_approval", "research-awaiting", {
        "node_id": "research", "state": "AWAITING_APPROVAL", "attempt": 1,
    })

    with pytest.raises(GraphIntegrityError, match="non-approval"):
        _read(plan, store)
