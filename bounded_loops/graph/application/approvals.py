"""Approval validation bound to immutable graph evidence and replay protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Protocol

from bounded_loops.graph.domain.approvals import ApprovalDecision, ApprovalRequest
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphValidationError


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ApprovalTarget:
    organization_id: str
    project_id: str
    graph_digest: str
    plan_digest: str
    node_id: str
    attempt: int
    evidence_digest: str
    requested_effects: frozenset[Effect]
    resource_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_effects", frozenset(self.requested_effects))


@dataclass(frozen=True)
class AuthenticatedApprovalContext:
    subject_id: str
    organization_id: str
    project_id: str
    auth_context_digest: str


@dataclass(frozen=True)
class ApprovalCommand:
    """One approval transition, bound to the version the approver actually saw."""

    request: ApprovalRequest
    decision: ApprovalDecision
    context: AuthenticatedApprovalContext
    expected_resource_version: int
    idempotency_key: str


@dataclass(frozen=True)
class ApprovalCommit:
    """Durable result of a single approval state transition."""

    approval_id: str
    new_resource_version: int
    idempotency_key: str


class ApprovalAuthorizationPort(Protocol):
    def authorize(self, request: ApprovalRequest, context: AuthenticatedApprovalContext) -> bool: ...


class ApprovalSignatureVerifierPort(Protocol):
    def verify(self, request: ApprovalRequest, decision: ApprovalDecision) -> bool: ...


class ApprovalCommandPort(Protocol):
    def commit(self, command: ApprovalCommand) -> ApprovalCommit:
        """Atomically execute the approval state transition.

        Implementations MUST, in one tenant-scoped durable transaction: load the
        authoritative target; compare ``expected_resource_version``; re-check
        every request binding against that target; consume the nonce uniquely
        for the tenant and request digest; append actor/idempotency audit data;
        mutate the target; and return the new version.  A stale version MUST
        fail before nonce consumption.  Retrying an identical idempotency key
        MUST return its original commit without repeating the mutation.
        """
        ...


def request_digest(request: ApprovalRequest) -> str:
    """Return the canonical digest the decision must sign and replay-protect."""
    _validate_request(request)
    canonical = json.dumps(
        {
            "approval_id": request.approval_id, "attempt": request.attempt,
            "evidence_digest": request.evidence_digest, "expires_at": request.expires_at,
            "graph_digest": request.graph_digest, "node_id": request.node_id,
            "nonce": request.nonce, "organization_id": request.organization_id,
            "plan_digest": request.plan_digest, "project_id": request.project_id,
            "requested_effects": sorted(effect.value for effect in request.requested_effects),
            "required_role": request.required_role,
        },
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def approve(
    request: ApprovalRequest,
    target: ApprovalTarget,
    decision: ApprovalDecision,
    context: AuthenticatedApprovalContext,
    authorizer: ApprovalAuthorizationPort,
    signature_verifier: ApprovalSignatureVerifierPort,
    command_port: ApprovalCommandPort,
    *,
    expected_resource_version: int,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalCommit:
    """Validate authority, then request one atomic approval transition.

    This use case deliberately has no replay store and no caller-owned mutation:
    those operations are security-sensitive and belong together in
    ``ApprovalCommandPort.commit``.
    """
    _validate_target(request, target)
    _validate_context(request, decision, context)
    _validate_expected_version(expected_resource_version, target)
    _validate_idempotency_key(idempotency_key)
    digest = request_digest(request)
    current = now or datetime.now(timezone.utc)
    if _instant(request.expires_at, "/request/expires_at") <= current:
        raise GraphValidationError("approval_expired", "/request/expires_at", "approval request is expired")
    if decision.request_digest != digest:
        raise GraphValidationError("approval_request", "/decision/request_digest", "decision does not match approval request")
    if decision.decision != "approve":
        raise GraphValidationError("approval_decision", "/decision/decision", "decision is not an approval")
    if decision.actor_role != request.required_role:
        raise GraphValidationError("approval_role", "/decision/actor_role", "actor role does not satisfy approval request")
    _validate_decision(decision, current)
    if not authorizer.authorize(request, context):
        raise GraphValidationError("approval_authorization", "/decision", "authorization context is stale or denied")
    if not signature_verifier.verify(request, decision):
        raise GraphValidationError("approval_signature", "/decision/signature", "approval signature is invalid")
    return command_port.commit(
        ApprovalCommand(request, decision, context, expected_resource_version, idempotency_key)
    )


def _validate_target(request: ApprovalRequest, target: ApprovalTarget) -> None:
    pairs = (
        ("organization", request.organization_id, target.organization_id),
        ("project", request.project_id, target.project_id),
        ("graph", request.graph_digest, target.graph_digest),
        ("plan", request.plan_digest, target.plan_digest),
        ("node", request.node_id, target.node_id),
        ("attempt", request.attempt, target.attempt),
        ("evidence", request.evidence_digest, target.evidence_digest),
        ("effects", request.requested_effects, target.requested_effects),
    )
    for name, expected, actual in pairs:
        if expected != actual:
            raise GraphValidationError("approval_target", f"/target/{name}", f"approval {name} does not match target")


def _validate_context(request: ApprovalRequest, decision: ApprovalDecision, context: AuthenticatedApprovalContext) -> None:
    if (
        not isinstance(context.subject_id, str) or not context.subject_id
        or (context.organization_id, context.project_id) != (request.organization_id, request.project_id)
    ):
        raise GraphValidationError("approval_context", "/context", "authenticated context does not match approval tenant")
    _digest(context.auth_context_digest, "/context/auth_context_digest")
    if context.subject_id != decision.actor_id:
        raise GraphValidationError("approval_context", "/context/subject_id", "authenticated subject does not match approval actor")
    if context.auth_context_digest != decision.auth_context_digest:
        raise GraphValidationError("approval_context", "/context/auth_context_digest", "authorization context is stale")


def _validate_expected_version(expected_resource_version: int, target: ApprovalTarget) -> None:
    if isinstance(expected_resource_version, bool) or not isinstance(expected_resource_version, int) or expected_resource_version < 1:
        raise GraphValidationError("approval_version", "/expected_resource_version", "expected version must be positive")
    if isinstance(target.resource_version, bool) or not isinstance(target.resource_version, int) or target.resource_version < 1:
        raise GraphValidationError("approval_version", "/target/resource_version", "target version must be positive")
    if expected_resource_version != target.resource_version:
        raise GraphValidationError("approval_stale", "/expected_resource_version", "approval target version is stale")


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise GraphValidationError("approval_idempotency", "/idempotency_key", "idempotency key is required")


def _validate_request(request: ApprovalRequest) -> None:
    for value in (request.approval_id, request.organization_id, request.project_id, request.node_id, request.required_role, request.nonce):
        if not isinstance(value, str) or not value:
            raise GraphValidationError("approval_request", "/request", "approval request has an empty required value")
    if isinstance(request.attempt, bool) or not isinstance(request.attempt, int) or request.attempt < 1:
        raise GraphValidationError("approval_request", "/request/attempt", "approval attempt must be positive")
    for value in (request.graph_digest, request.plan_digest, request.evidence_digest):
        _digest(value, "/request")
    _instant(request.expires_at, "/request/expires_at")
    if not request.requested_effects or not all(isinstance(effect, Effect) for effect in request.requested_effects):
        raise GraphValidationError("approval_request", "/request/requested_effects", "approval effects must be declared")


def _validate_decision(decision: ApprovalDecision, now: datetime) -> None:
    for value in (decision.actor_id, decision.actor_role, decision.signature):
        if not isinstance(value, str) or not value:
            raise GraphValidationError("approval_decision", "/decision", "approval decision has an empty required value")
    _digest(decision.request_digest, "/decision/request_digest")
    _digest(decision.auth_context_digest, "/decision/auth_context_digest")
    if _instant(decision.decided_at, "/decision/decided_at") > now:
        raise GraphValidationError("approval_decision", "/decision/decided_at", "approval decision is in the future")


def _digest(value: str, pointer: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise GraphValidationError("approval_digest", pointer, "must be a SHA-256 digest")


def _instant(value: str, pointer: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise GraphValidationError("approval_timestamp", pointer, "must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GraphValidationError("approval_timestamp", pointer, "must include a timezone")
    return parsed.astimezone(timezone.utc)
