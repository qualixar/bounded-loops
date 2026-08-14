"""F2 slice 2 — the approval-node human-in-the-loop interrupt and its bridge.

Covers the controller's behavior at a kind=approval node (pause, resume-approve,
resume-reject, idempotent re-pause, fail-closed on a missing/raising resolver or a
forged worker path) AND the genuine bridge: a decision validated + committed
through ``approvals.approve`` lets a paused run continue on ``resume()``. The
resolver's fail-closed, run-scoped contract is proved here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.approval_gate import RecordedApprovalResolver
from bounded_loops.graph.application.approvals import (
    ApprovalCommit,
    ApprovalTarget,
    AuthenticatedApprovalContext,
    approve,
    request_digest,
)
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.application.node_contracts import ApprovalOutcome, GateVerdict
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.approvals import ApprovalDecision, ApprovalRequest
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent


def _d(character: str) -> str:
    return "sha256:" + character * 64


def _approval_plan():
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "approval-run",
        "version": "1.0.0",
        "nodes": [{
            "id": "approve", "kind": "approval", "required_role": "editor",
            "inputs": {}, "outputs": {"approved": "text"},
            "budget": {"max_attempts": 1, "max_wallclock_s": 1},
            "effects": ["read_only"], "isolation": "workspace_only",
        }],
        "edges": [],
        "connection_slots": [],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })
    return compile_graph(graph, CompileSnapshot(
        policy_digest=_d("a"), package_digests=frozenset(), connections=(),
    ))


def _identity(plan, run_id: str = "run-1") -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id=run_id,
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )


class _Unused:
    """Every collaborator an approval node must NOT touch: asserts if called."""

    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0):
        raise AssertionError("worker must not run for an approval node")

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        raise AssertionError("independent gate must not run for an approval node")

    def verify(self, *, identity, digests) -> None:
        raise AssertionError("artifact verifier must not run for an approval node")

    def authorize(self, *, plan, node):
        raise AssertionError("execution policy must not run for an approval node")

    def enforce(self, *, plan, node, envelope) -> None:
        raise AssertionError("execution enforcer must not run for an approval node")


def _controller(plan, event_log, resolver):
    return GraphRunController(
        plan=plan, event_log=event_log, worker=_Unused(), gate=_Unused(),
        artifact_verifier=_Unused(), execution_policy=_Unused(),
        execution_enforcer=_Unused(), timestamp=lambda: "2026-08-08T00:00:00Z",
        approval_resolver=resolver,
    )


# ── resolver contract ───────────────────────────────────────────────────────────

def _node(plan):
    return plan.nodes[0]


def test_resolver_defaults_to_pending(tmp_path):
    plan = _approval_plan()
    resolver = RecordedApprovalResolver()
    assert resolver.resolve(identity=_identity(plan), node=_node(plan), attempt=1, repair_round=0) is ApprovalOutcome.PENDING


def test_recording_a_grant_requires_a_durable_commit():
    plan = _approval_plan()
    resolver = RecordedApprovalResolver()
    with pytest.raises(GraphIntegrityError, match="durable ApprovalCommit"):
        resolver.record_committed_approval(identity=_identity(plan), request=_request(), commit="not-a-commit")  # type: ignore[arg-type]


def test_a_decision_recorded_for_one_run_does_not_resolve_another_run():
    plan = _approval_plan()
    resolver = RecordedApprovalResolver()
    resolver.record_rejection(identity=_identity(plan, run_id="run-1"), node_id="approve", attempt=1)
    # A different run of the same plan/node/attempt is unaffected — still PENDING.
    assert resolver.resolve(
        identity=_identity(plan, run_id="run-2"), node=_node(plan), attempt=1, repair_round=0,
    ) is ApprovalOutcome.PENDING


def test_recording_a_commit_for_a_mismatched_request_is_rejected():
    plan = _approval_plan()
    resolver = RecordedApprovalResolver()
    with pytest.raises(GraphIntegrityError, match="does not correspond"):
        resolver.record_committed_approval(
            identity=_identity(plan), request=_request(),
            commit=ApprovalCommit("some-other-approval", 2, "idem-1"),
        )


def test_recording_a_grant_for_a_different_tenant_is_rejected():
    plan = _approval_plan()
    resolver = RecordedApprovalResolver()
    cross_tenant = GraphRunIdentity(
        organization_id="org-2", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )
    with pytest.raises(GraphIntegrityError, match="tenant"):
        resolver.record_committed_approval(
            identity=cross_tenant, request=_request(), commit=ApprovalCommit("approval-1", 2, "idem-1"),
        )


# ── the genuine bridge: approvals.approve() → resume() ──────────────────────────

def _request() -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-1", organization_id="org-1", project_id="project-1",
        graph_digest=_d("a"), plan_digest=_d("b"), node_id="approve", attempt=1,
        evidence_digest=_d("c"), requested_effects=frozenset({Effect.READ_ONLY}),
        required_role="editor", nonce="nonce-1", expires_at="2026-08-09T00:00:00Z",
    )


def _target() -> ApprovalTarget:
    return ApprovalTarget(
        organization_id="org-1", project_id="project-1", graph_digest=_d("a"),
        plan_digest=_d("b"), node_id="approve", attempt=1, evidence_digest=_d("c"),
        requested_effects=frozenset({Effect.READ_ONLY}),
    )


def _decision(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(
        request_digest=request_digest(request), actor_id="user-1", actor_role="editor",
        decision="approve", auth_context_digest=_d("d"),
        decided_at="2026-08-08T00:00:00Z", signature="test-signature",
    )


class _AllowAll:
    def authorize(self, request, context) -> bool:
        return True

    def verify(self, request, decision) -> bool:
        return True


class _CommitPort:
    """Minimal durable transition: the elaborate transactional store lives in
    test_approvals.py — here we only need a real commit once approve() has
    validated the decision end-to-end."""

    def commit(self, command) -> ApprovalCommit:
        return ApprovalCommit(command.request.approval_id, 2, command.idempotency_key)


def test_a_committed_human_approval_lets_the_paused_run_resume_to_success(tmp_path):
    plan = _approval_plan()
    identity = _identity(plan)
    resolver = RecordedApprovalResolver()

    # 1. The run pauses at the approval node.
    paused = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), resolver).run()
    assert paused.state == "RUNNING"

    # 2. A human decision is validated + committed through the real approvals use case.
    request = _request()
    commit = approve(
        request, _target(), _decision(request),
        AuthenticatedApprovalContext("user-1", "org-1", "project-1", _d("d")),
        _AllowAll(), _AllowAll(), _CommitPort(),
        expected_resource_version=1, idempotency_key="idem-1",
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    assert isinstance(commit, ApprovalCommit)

    # 3. Bridging that commit into the resolver lets resume() continue past the gate.
    resolver.record_committed_approval(identity=identity, request=request, commit=commit)
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), resolver)
    projection = resumed.resume()

    assert projection.state == "SUCCEEDED"
    assert [event.event.event_type for event in resumed.event_log.replay()][-2:] == [
        "node.succeeded", "run.succeeded",
    ]


# ── controller behavior at an approval node (pause / resume / fail-closed) ──────

@dataclass
class _FixedApprovalResolver:
    """Always returns one outcome — tests controller handling independently of how
    a decision is recorded."""

    outcome: ApprovalOutcome

    def resolve(self, *, identity, node, attempt, repair_round=0) -> ApprovalOutcome:
        return self.outcome


class _RaisingApprovalResolver:
    def resolve(self, *, identity, node, attempt) -> ApprovalOutcome:
        raise RuntimeError("approval store is unavailable")


def _raw_append(store, head, event_type, key, payload):
    """Append one arbitrary, correctly hash-chained event — the tamperer's tool."""
    return store.append(
        head,
        UnsignedGraphEvent(
            event_id=f"event-{key}", idempotency_key=key, event_type=event_type,
            timestamp="2026-08-08T00:00:00Z", actor="controller", payload=payload,
        ),
    ).event_hash


def test_run_pauses_at_an_approval_node_awaiting_a_human_decision(tmp_path):
    plan = _approval_plan()
    controller = _controller(
        plan, GraphEventLog(tmp_path / "events.jsonl", _identity(plan)), RecordedApprovalResolver(),
    )

    projection = controller.run()

    # Not terminal: paused, durably, awaiting a human.
    assert projection.state == "RUNNING"
    types = [event.event.event_type for event in controller.event_log.replay()]
    assert types == ["run.created", "run.started", "node.ready", "node.awaiting_approval"]
    assert "run.succeeded" not in types and "run.failed" not in types


def test_resume_completes_the_run_once_the_approval_is_granted(tmp_path):
    plan = _approval_plan()
    identity = _identity(plan)
    _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), RecordedApprovalResolver()).run()

    resumed = _controller(
        plan, GraphEventLog(tmp_path / "events.jsonl", identity),
        _FixedApprovalResolver(ApprovalOutcome.APPROVED),
    )
    projection = resumed.resume()

    assert projection.state == "SUCCEEDED"
    assert [event.event.event_type for event in resumed.event_log.replay()] == [
        "run.created", "run.started", "node.ready", "node.awaiting_approval",
        "run.resumed", "node.succeeded", "run.succeeded",
    ]


def test_resume_fails_closed_after_a_recorded_rejection(tmp_path):
    plan = _approval_plan()
    identity = _identity(plan)
    resolver = RecordedApprovalResolver()
    _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), resolver).run()

    resolver.record_rejection(identity=identity, node_id="approve", attempt=1)
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), resolver)
    projection = resumed.resume()

    assert projection.state == "FAILED"
    events = resumed.event_log.replay()
    assert [event.event.event_type for event in events] == [
        "run.created", "run.started", "node.ready", "node.awaiting_approval",
        "run.resumed", "node.failed", "run.failed",
    ]
    assert events[-2].event.payload["reason"] == "human approval was rejected"


def test_resume_without_a_decision_stays_paused_and_appends_no_duplicate_events(tmp_path):
    plan = _approval_plan()
    identity = _identity(plan)
    resolver = RecordedApprovalResolver()
    _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), resolver).run()

    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), resolver)
    projection = resumed.resume()

    assert projection.state == "RUNNING"
    # Idempotent re-pause: the re-driven prefix re-appends as head-safe no-ops. The one
    # genuinely new record is run.resumed — an approval node runs no worker, so there is no
    # attempt to re-drive.
    assert [event.event.event_type for event in resumed.event_log.replay()] == [
        "run.created", "run.started", "node.ready", "node.awaiting_approval", "run.resumed",
    ]


def test_an_approval_node_without_a_resolver_fails_closed(tmp_path):
    plan = _approval_plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Unused(), gate=_Unused(), artifact_verifier=_Unused(),
        execution_policy=_Unused(), execution_enforcer=_Unused(),
        timestamp=lambda: "2026-08-08T00:00:00Z",  # no approval_resolver
    )

    projection = controller.run()

    assert projection.state == "FAILED"
    events = controller.event_log.replay()
    # Records the human gate BEFORE failing, so the receipt stays projectable.
    assert [event.event.event_type for event in events] == [
        "run.created", "run.started", "node.ready", "node.awaiting_approval",
        "node.failed", "run.failed",
    ]
    assert events[-2].event.payload["reason"] == "approval node reached without an approval resolver"
    assert latest_node_states(plan, events)["approve"]["state"] == "FAILED"


def test_run_fails_closed_when_the_approval_resolver_raises(tmp_path):
    plan = _approval_plan()
    controller = _controller(
        plan, GraphEventLog(tmp_path / "events.jsonl", _identity(plan)), _RaisingApprovalResolver(),
    )

    projection = controller.run()

    # A resolver error must not escape uncaught, and must not advance: fail closed.
    assert projection.state == "FAILED"
    events = controller.event_log.replay()
    assert [event.event.event_type for event in events] == [
        "run.created", "run.started", "node.ready", "node.awaiting_approval",
        "node.failed", "run.failed",
    ]
    assert events[-2].event.payload["reason"] == "approval resolver evaluation failed"
    assert latest_node_states(plan, events)["approve"]["state"] == "FAILED"


def test_a_preapproved_node_succeeds_without_pausing_and_stays_projectable(tmp_path):
    plan = _approval_plan()
    controller = _controller(
        plan, GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        _FixedApprovalResolver(ApprovalOutcome.APPROVED),
    )

    projection = controller.run()

    # Even with the decision already present (no pause), the node records the human
    # gate first, so the terminal is AWAITING_APPROVAL -> SUCCEEDED and it projects.
    assert projection.state == "SUCCEEDED"
    events = controller.event_log.replay()
    assert [event.event.event_type for event in events] == [
        "run.created", "run.started", "node.ready", "node.awaiting_approval",
        "node.succeeded", "run.succeeded",
    ]
    assert latest_node_states(plan, events)["approve"]["state"] == "SUCCEEDED"


def test_resume_rejects_an_approval_node_forced_through_the_worker_path(tmp_path):
    # An approval node is a human gate: it must never run a worker. A forged log that
    # drives it READY -> STARTING (the worker path to a no-human SUCCEEDED) is rejected.
    plan = _approval_plan()
    identity = _identity(plan)
    store = GraphEventLog(tmp_path / "events.jsonl", identity)
    head = _raw_append(store, "0" * 64, "run.created", "run-1:run.created", {"state": "PENDING"})
    head = _raw_append(store, head, "run.started", "run-1:run.started", {"state": "RUNNING"})
    head = _raw_append(store, head, "node.ready", "approve:READY", {"node_id": "approve", "state": "READY", "attempt": 1})
    _raw_append(store, head, "node.starting", "approve:STARTING", {"node_id": "approve", "state": "STARTING", "attempt": 1})

    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), RecordedApprovalResolver())
    with pytest.raises(GraphIntegrityError, match="bypassing the human gate"):
        resumed.resume()

# ── a repair round must ask the human AGAIN (P4.5 round-2 audit, Grok 2) ─────────────────


def test_a_grant_in_round_0_does_NOT_satisfy_a_repair_round(tmp_path):
    """The human-in-the-loop bypass the round-2 audit demonstrated.

    A repair resets the approval node to PENDING and re-runs the whole suffix, so the work the human
    is being asked about is DIFFERENT work. With the round missing from the resolver key, the round-0
    grant satisfied the round-1 pause: the node went READY -> AWAITING_APPROVAL -> SUCCEEDED with no
    second decision, and the audit observed two ``node.succeeded`` receipts on one approval node from
    a single grant. The irreversible effect downstream was then authorized by an approval of evidence
    that no longer existed.
    """
    plan = _approval_plan()
    identity = _identity(plan)
    resolver = RecordedApprovalResolver()
    request = _request()
    commit = ApprovalCommit(
        approval_id=request.approval_id, new_resource_version=2, idempotency_key="idem-1",
    )
    resolver.record_committed_approval(identity=identity, request=request, commit=commit)

    approved_round_0 = resolver.resolve(
        identity=identity, node=_node(plan), attempt=1, repair_round=0,
    )
    pending_round_1 = resolver.resolve(
        identity=identity, node=_node(plan), attempt=1, repair_round=1,
    )

    assert approved_round_0 is ApprovalOutcome.APPROVED, "round 0 must keep resolving as it always did"
    assert pending_round_1 is ApprovalOutcome.PENDING, (
        "a repair round inherited the previous grant — the human never saw this round's work"
    )


def test_a_grant_recorded_IN_a_repair_round_resolves_for_that_round():
    """The other direction: once the human decides round 1, round 1 proceeds."""
    plan = _approval_plan()
    identity = _identity(plan)
    resolver = RecordedApprovalResolver()
    request = _request()
    commit = ApprovalCommit(
        approval_id=request.approval_id, new_resource_version=2, idempotency_key="idem-1",
    )
    resolver.record_committed_approval(
        identity=identity, request=request, commit=commit, repair_round=1,
    )

    assert resolver.resolve(
        identity=identity, node=_node(plan), attempt=1, repair_round=1,
    ) is ApprovalOutcome.APPROVED
    # And it does not leak backwards into round 0 either.
    assert resolver.resolve(
        identity=identity, node=_node(plan), attempt=1, repair_round=0,
    ) is ApprovalOutcome.PENDING
