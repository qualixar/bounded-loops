from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock, Thread

import pytest

from bounded_loops.graph.application.approvals import (
    ApprovalTarget,
    ApprovalCommand,
    ApprovalCommit,
    AuthenticatedApprovalContext,
    approve,
    request_digest,
)
from bounded_loops.graph.domain.approvals import ApprovalDecision, ApprovalRequest
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphValidationError


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-1", organization_id="org-1", project_id="project-1",
        graph_digest=_digest("a"), plan_digest=_digest("b"), node_id="publish", attempt=1,
        evidence_digest=_digest("c"), requested_effects=frozenset({Effect.EXTERNAL_WRITE}),
        required_role="publisher", nonce="nonce-1", expires_at="2026-08-09T00:00:00Z",
    )


def _target() -> ApprovalTarget:
    return ApprovalTarget(
        organization_id="org-1", project_id="project-1", graph_digest=_digest("a"),
        plan_digest=_digest("b"), node_id="publish", attempt=1, evidence_digest=_digest("c"),
        requested_effects=frozenset({Effect.EXTERNAL_WRITE}),
    )


def _decision(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(
        request_digest=request_digest(request), actor_id="user-1", actor_role="publisher",
        decision="approve", auth_context_digest=_digest("d"),
        decided_at="2026-08-08T00:00:00Z", signature="test-signature",
    )


@dataclass
class _Authorizer:
    allowed: bool = True

    def authorize(self, request, context) -> bool:
        return self.allowed


@dataclass
class _SignatureVerifier:
    valid: bool = True

    def verify(self, request, decision) -> bool:
        return self.valid


@dataclass
class _CommandStore:
    """Reference transaction fake: all security-sensitive state changes share one lock."""

    target: ApprovalTarget
    consumed: set[tuple[str, str, str, str]] = field(default_factory=set)
    audit: list[tuple[str, str]] = field(default_factory=list)
    idempotent: dict[str, tuple[ApprovalCommand, ApprovalCommit]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def commit(self, command: ApprovalCommand) -> ApprovalCommit:
        with self._lock:
            prior = self.idempotent.get(command.idempotency_key)
            if prior is not None:
                if prior[0] != command:
                    raise GraphValidationError("approval_idempotency", "/idempotency_key", "idempotency key was reused")
                return prior[1]
            if command.expected_resource_version != self.target.resource_version:
                raise GraphValidationError("approval_stale", "/expected_resource_version", "authoritative target version is stale")
            for expected, actual in (
                (command.request.organization_id, self.target.organization_id),
                (command.request.project_id, self.target.project_id),
                (command.request.graph_digest, self.target.graph_digest),
                (command.request.plan_digest, self.target.plan_digest),
                (command.request.node_id, self.target.node_id),
                (command.request.attempt, self.target.attempt),
                (command.request.evidence_digest, self.target.evidence_digest),
                (command.request.requested_effects, self.target.requested_effects),
            ):
                if expected != actual:
                    raise GraphValidationError("approval_target", "/target", "authoritative target does not match approval")
            nonce_key = (
                command.request.organization_id,
                command.request.project_id,
                request_digest(command.request),
                command.request.nonce,
            )
            if nonce_key in self.consumed:
                raise GraphValidationError("approval_replay", "/request/nonce", "approval nonce was already consumed")
            self.consumed.add(nonce_key)
            self.target = replace(self.target, resource_version=self.target.resource_version + 1)
            commit = ApprovalCommit(
                command.request.approval_id, self.target.resource_version, command.idempotency_key
            )
            self.audit.append((command.context.subject_id, command.idempotency_key))
            self.idempotent[command.idempotency_key] = (command, commit)
            return commit


def _approve(
    request: ApprovalRequest,
    store: _CommandStore,
    *,
    target: ApprovalTarget | None = None,
    expected_version: int = 1,
    idempotency_key: str = "command-1",
) -> ApprovalCommit:
    return approve(
        request, target or _target(), _decision(request), _context(), _Authorizer(), _SignatureVerifier(), store,
        expected_resource_version=expected_version, idempotency_key=idempotency_key,
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )


def test_approval_is_bound_to_exact_target_role_signature_and_one_time_nonce():
    request = _request()
    store = _CommandStore(_target())

    _approve(request, store)
    with pytest.raises(GraphValidationError, match="replay"):
        _approve(
            request, store, target=store.target, expected_version=2, idempotency_key="command-2"
        )


@pytest.mark.parametrize(
    ("target", "decision", "authorizer", "verifier", "now", "message"),
    [
        (replace(_target(), plan_digest=_digest("e")), _decision(_request()), _Authorizer(), _SignatureVerifier(), datetime(2026, 8, 8, tzinfo=timezone.utc), "plan"),
        (replace(_target(), evidence_digest=_digest("e")), _decision(_request()), _Authorizer(), _SignatureVerifier(), datetime(2026, 8, 8, tzinfo=timezone.utc), "evidence"),
        (replace(_target(), requested_effects=frozenset({Effect.FINANCIAL})), _decision(_request()), _Authorizer(), _SignatureVerifier(), datetime(2026, 8, 8, tzinfo=timezone.utc), "effects"),
        (_target(), replace(_decision(_request()), actor_role="viewer"), _Authorizer(), _SignatureVerifier(), datetime(2026, 8, 8, tzinfo=timezone.utc), "role"),
        (_target(), _decision(_request()), _Authorizer(False), _SignatureVerifier(), datetime(2026, 8, 8, tzinfo=timezone.utc), "authorization"),
        (_target(), _decision(_request()), _Authorizer(), _SignatureVerifier(False), datetime(2026, 8, 8, tzinfo=timezone.utc), "signature"),
        (_target(), _decision(_request()), _Authorizer(), _SignatureVerifier(), datetime(2026, 8, 10, tzinfo=timezone.utc), "expired"),
    ],
)
def test_approval_denies_changed_or_untrusted_authority(
    target, decision, authorizer, verifier, now, message,
):
    with pytest.raises(GraphValidationError, match=message):
        approve(
            _request(), target, decision, _context(), authorizer, verifier, _CommandStore(_target()),
            expected_resource_version=1, idempotency_key="command-1", now=now,
        )


def _context(*, subject_id: str = "user-1", digest: str | None = None) -> AuthenticatedApprovalContext:
    return AuthenticatedApprovalContext(subject_id, "org-1", "project-1", digest or _digest("d"))


def test_approval_denies_caller_controlled_actor_context_and_freezes_effects():
    effects = {Effect.EXTERNAL_WRITE}
    request = replace(_request(), requested_effects=effects)
    digest = request_digest(request)
    effects.add(Effect.FINANCIAL)
    assert request.requested_effects == frozenset({Effect.EXTERNAL_WRITE})
    assert request_digest(request) == digest
    with pytest.raises(GraphValidationError, match="subject"):
        approve(
            _request(), _target(), _decision(_request()), _context(subject_id="user-2"), _Authorizer(), _SignatureVerifier(), _CommandStore(_target()),
            expected_resource_version=1, idempotency_key="command-1", now=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
    with pytest.raises(GraphValidationError, match="stale"):
        approve(
            _request(), _target(), _decision(_request()), _context(digest=_digest("e")), _Authorizer(), _SignatureVerifier(), _CommandStore(_target()),
            expected_resource_version=1, idempotency_key="command-1", now=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )


def test_approval_command_is_idempotent_and_records_one_actor_audit_entry():
    request = _request()
    store = _CommandStore(_target())

    first = _approve(request, store)
    retry = _approve(request, store)

    assert retry == first
    assert store.audit == [("user-1", "command-1")]
    assert len(store.consumed) == 1


def test_authoritative_stale_version_denies_before_nonce_consumption():
    request = _request()
    store = _CommandStore(replace(_target(), resource_version=2))

    with pytest.raises(GraphValidationError, match="stale"):
        _approve(request, store)

    assert store.consumed == set()
    assert store.audit == []


def test_parallel_approvals_allow_exactly_one_mutation_before_stale_denial():
    request = _request()
    store = _CommandStore(_target())
    successes: list[ApprovalCommit] = []
    failures: list[GraphValidationError] = []

    def submit(key: str) -> None:
        try:
            successes.append(_approve(request, store, idempotency_key=key))
        except GraphValidationError as error:
            failures.append(error)

    first = Thread(target=submit, args=("command-1",))
    second = Thread(target=submit, args=("command-2",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "approval_stale"
    assert len(store.consumed) == len(store.audit) == 1
