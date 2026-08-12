"""Connection admission and audience-bound, credential-free execution grants."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re

from bounded_loops.graph.domain.authoring import DataClass, Effect, IsolationLevel
from bounded_loops.graph.domain.connections import (
    AdmittedConnection,
    ConnectionState,
    ExecutionGrant,
    ResolvedRoute,
    RoutePolicy,
)
from bounded_loops.graph.domain.errors import GraphValidationError


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_M4_CONNECTORS = frozenset({"m4-company-claude", "corporate-external-reviewer"})


@dataclass(frozen=True)
class ConnectionAdmissionRequest:
    connection_id: str
    organization_id: str
    connector_id: str
    connector_version: str
    local_session_ref: str | None
    credential_ref: str | None
    consent_digest: str
    evidence_digest: str
    expires_at: str
    capabilities: frozenset[str]
    effects: frozenset[Effect]
    transport: str
    data_path: str
    route_policy: RoutePolicy


@dataclass(frozen=True)
class ExecutionGrantRequest:
    run_id: str
    node_id: str
    attempt: int
    connection: AdmittedConnection
    effects: frozenset[Effect]
    destinations: frozenset[str]
    expires_at: str


@dataclass(frozen=True)
class RouteRequest:
    provider_id: str
    model_id: str
    region: str
    fallback: bool
    data_class: DataClass


_TRANSITIONS = {
    ConnectionState.DISCOVERED: frozenset({ConnectionState.ADAPTER_VALIDATED}),
    ConnectionState.ADAPTER_VALIDATED: frozenset({ConnectionState.SMOKE_PROVEN}),
    ConnectionState.SMOKE_PROVEN: frozenset({ConnectionState.ADMITTED}),
    ConnectionState.ADMITTED: frozenset({
        ConnectionState.SUSPENDED, ConnectionState.REVOKED, ConnectionState.EXPIRED,
    }),
    ConnectionState.SUSPENDED: frozenset({ConnectionState.ADMITTED, ConnectionState.REVOKED, ConnectionState.EXPIRED}),
    ConnectionState.REVOKED: frozenset(),
    ConnectionState.EXPIRED: frozenset(),
}
_DATA_RANK = {
    DataClass.PUBLIC: 0,
    DataClass.INTERNAL: 1,
    DataClass.CONFIDENTIAL: 2,
    DataClass.RESTRICTED: 3,
}


def register_connection(request: ConnectionAdmissionRequest) -> AdmittedConnection:
    """Register only a discovered non-secret connection; it is never routable yet."""
    _nonempty(request.connection_id, "connection_id")
    _nonempty(request.organization_id, "organization_id")
    _nonempty(request.connector_id, "connector_id")
    if request.connector_id in _M4_CONNECTORS:
        raise GraphValidationError("m4_nonroutable", "/connector_id", "M4 is GitHub-only external review and cannot be admitted")
    if request.local_session_ref is not None and request.credential_ref is not None:
        raise GraphValidationError("connection_auth", "/", "connection may use one local session or one opaque credential reference")
    _digest(request.consent_digest, "/consent_digest")
    _digest(request.evidence_digest, "/evidence_digest")
    _nonempty(request.expires_at, "expires_at")
    _nonempty(request.transport, "transport")
    _nonempty(request.data_path, "data_path")
    if not request.capabilities:
        raise GraphValidationError("capabilities", "/capabilities", "connection must declare capabilities")
    _validate_route_policy(request.route_policy)
    return AdmittedConnection(
        connection_id=request.connection_id,
        organization_id=request.organization_id,
        connector_id=request.connector_id,
        connector_version=request.connector_version,
        consent_digest=request.consent_digest,
        evidence_digest=request.evidence_digest,
        expires_at=request.expires_at,
        capabilities=frozenset(request.capabilities),
        effects=frozenset(request.effects),
        transport=request.transport,
        data_path=request.data_path,
        route_policy=request.route_policy,
        state=ConnectionState.DISCOVERED,
    )


def advance_connection(
    connection: AdmittedConnection,
    target: ConnectionState,
    evidence_digest: str,
) -> AdmittedConnection:
    """Advance one evidence-backed lifecycle edge; terminal states never re-enter."""
    _digest(evidence_digest, "/evidence_digest")
    if target not in _TRANSITIONS[connection.state]:
        raise GraphValidationError("connection_lifecycle", "/state", "invalid connection lifecycle transition")
    return replace(connection, evidence_digest=evidence_digest, state=target)


def authorize_route(connection: AdmittedConnection, request: RouteRequest) -> ResolvedRoute:
    """Deny a route before any connector can receive graph data."""
    if connection.state is not ConnectionState.ADMITTED:
        raise GraphValidationError("not_admitted", "/connection", "only admitted connections may route")
    policy = connection.route_policy
    if request.provider_id not in policy.allowed_providers:
        raise GraphValidationError("route_provider", "/provider_id", "provider is not allowed by route policy")
    if request.model_id not in policy.allowed_models:
        raise GraphValidationError("route_model", "/model_id", "model is not allowed by route policy")
    if request.region not in policy.allowed_regions:
        raise GraphValidationError("route_region", "/region", "region is not allowed by route policy")
    if request.fallback and not policy.fallback_allowed:
        raise GraphValidationError("route_fallback", "/fallback", "fallback is not allowed by route policy")
    if _DATA_RANK[request.data_class] > _DATA_RANK[policy.data_class_max]:
        raise GraphValidationError("route_data_class", "/data_class", "data class exceeds route policy")
    if request.data_class is DataClass.RESTRICTED and not policy.route_verifiable:
        raise GraphValidationError("route_verifiability", "/data_class", "restricted data requires a verifiable route")
    return ResolvedRoute(
        provider_id=request.provider_id,
        model_id=request.model_id,
        region=request.region,
        fallback=request.fallback,
        policy_digest=policy.policy_digest,
    )


def compiler_connection_snapshot(
    connection: AdmittedConnection,
    route: ResolvedRoute,
    *,
    binding_id: str,
    slot_id: str,
    isolation: IsolationLevel,
    now: datetime | None = None,
) -> dict[str, object]:
    """Produce the compiler's non-secret input only from an admitted route."""
    if connection.state is not ConnectionState.ADMITTED:
        raise GraphValidationError("not_admitted", "/connection", "only admitted connections may compile")
    if _instant(connection.expires_at, "/connection/expires_at") <= (now or datetime.now(timezone.utc)):
        raise GraphValidationError("connection_expired", "/connection/expires_at", "connection certificate is expired")
    if route.policy_digest != connection.route_policy.policy_digest:
        raise GraphValidationError("route_policy", "/route", "route policy does not match the connection")
    _nonempty(binding_id, "binding_id")
    _nonempty(slot_id, "slot_id")
    return {
        "binding_id": binding_id,
        "slot_id": slot_id,
        "connector_id": connection.connector_id,
        "connector_version": connection.connector_version,
        "connection_id": connection.connection_id,
        "admission_digest": connection.evidence_digest,
        "route_policy_digest": route.policy_digest,
        "provider_id": route.provider_id,
        "model_target": route.model_id,
        "region": route.region,
        "fallback": route.fallback,
        "capabilities": connection.capabilities,
        "data_class_max": connection.route_policy.data_class_max,
        "allowed_effects": connection.effects,
        "isolation": isolation,
        "transport": connection.transport,
        "admitted": True,
    }


def issue_execution_grant(
    request: ExecutionGrantRequest,
    *,
    now: datetime | None = None,
) -> ExecutionGrant:
    if request.connection.state is not ConnectionState.ADMITTED:
        raise GraphValidationError("not_admitted", "/connection", "only admitted connections can issue grants")
    current = now or datetime.now(timezone.utc)
    connection_expiry = _instant(request.connection.expires_at, "/connection/expires_at")
    grant_expiry = _instant(request.expires_at, "/expires_at")
    if connection_expiry <= current:
        raise GraphValidationError("connection_expired", "/connection/expires_at", "connection certificate is expired")
    if grant_expiry <= current or grant_expiry > connection_expiry:
        raise GraphValidationError("grant_expiry", "/expires_at", "grant expiry must be future and within the connection certificate")
    if request.attempt < 1:
        raise GraphValidationError("grant_audience", "/attempt", "attempt must be positive")
    if not request.effects <= request.connection.effects:
        raise GraphValidationError("grant_effect", "/effects", "grant requests an undeclared connection effect")
    material = json.dumps(
        {"attempt": request.attempt, "connection_id": request.connection.connection_id, "destinations": sorted(request.destinations), "effects": sorted(effect.value for effect in request.effects), "expires_at": request.expires_at, "node_id": request.node_id, "run_id": request.run_id},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return ExecutionGrant(
        grant_id="grant:" + hashlib.sha256(material).hexdigest(), run_id=request.run_id,
        node_id=request.node_id, attempt=request.attempt, connection_id=request.connection.connection_id,
        effects=frozenset(request.effects), destinations=frozenset(request.destinations), expires_at=request.expires_at,
    )


def validate_execution_grant(
    grant: ExecutionGrant,
    run_id: str,
    node_id: str,
    attempt: int,
    effect: Effect,
    *,
    now: datetime | None = None,
) -> bool:
    if (grant.run_id, grant.node_id, grant.attempt) != (run_id, node_id, attempt):
        raise GraphValidationError("grant_audience", "/grant", "grant audience does not match the execution attempt")
    if effect not in grant.effects:
        raise GraphValidationError("grant_effect", "/grant", "grant does not authorize this effect")
    if _instant(grant.expires_at, "/grant/expires_at") <= (now or datetime.now(timezone.utc)):
        raise GraphValidationError("grant_expired", "/grant/expires_at", "execution grant is expired")
    return True


def _digest(value: str, pointer: str) -> None:
    if not _DIGEST.fullmatch(value):
        raise GraphValidationError("digest", pointer, "must be a sha256 digest")


def _validate_route_policy(policy: RoutePolicy) -> None:
    _digest(policy.policy_digest, "/route_policy/policy_digest")
    if not isinstance(policy.data_class_max, DataClass):
        raise GraphValidationError("route_policy", "/route_policy/data_class_max", "must be a data classification")
    for pointer, values in (
        ("allowed_providers", policy.allowed_providers),
        ("allowed_models", policy.allowed_models),
        ("allowed_regions", policy.allowed_regions),
    ):
        if not values or not all(isinstance(value, str) and value for value in values):
            raise GraphValidationError("route_policy", f"/route_policy/{pointer}", "must contain non-empty values")


def _instant(value: str, pointer: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise GraphValidationError("timestamp", pointer, "must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GraphValidationError("timestamp", pointer, "must include a timezone")
    return parsed.astimezone(timezone.utc)


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise GraphValidationError("connection", f"/{name}", "must be a non-empty string")
