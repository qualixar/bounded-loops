"""F2 slice 2 — the human-approval bridge into the run controller.

Proves the resolver's fail-closed, run-scoped contract AND the genuine path: a
decision validated + committed through ``approvals.approve`` lets a paused run
continue on ``resume()``.
"""

from __future__ import annotations

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
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.run_graph import (
    ApprovalOutcome,
    GateVerdict,
    GraphRunController,
)
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.approvals import ApprovalDecision, ApprovalRequest
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity


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

    def execute(self, *, plan, node, envelope):
        raise AssertionError("worker must not run for an approval node")

    def evaluate(self, *, plan, node, result) -> GateVerdict:
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
    assert resolver.resolve(identity=_identity(plan), node=_node(plan), attempt=1) is ApprovalOutcome.PENDING


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
        identity=_identity(plan, run_id="run-2"), node=_node(plan), attempt=1,
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
