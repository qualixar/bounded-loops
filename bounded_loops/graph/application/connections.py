"""Audience-bound, credential-free execution grants.

Until 0.7.0 this module also held a connection-admission lifecycle — register, advance,
authorize a route, snapshot for the compiler. Four public functions, fully tested, and no
caller anywhere in the engine: the tests were the only callers, which is why no test could
report the gap. It was removed rather than wired. A credential-negotiating admission
lifecycle contradicts this engine's stated posture of no-secret connectors, and shipping a
subsystem whose only exercise is its own unit tests is a claim the product does not make
good on. It remains recoverable from tag ``v0.6.10`` if the posture ever changes.

What stays is the part with real callers: a grant is bound to one (run, node, attempt,
effect) audience and carries no credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import (
    AdmittedConnection,
    ConnectionState,
    ExecutionGrant,
)
from bounded_loops.graph.domain.errors import GraphValidationError


@dataclass(frozen=True)
class ExecutionGrantRequest:
    run_id: str
    node_id: str
    attempt: int
    connection: AdmittedConnection
    effects: frozenset[Effect]
    destinations: frozenset[str]
    expires_at: str


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


def _instant(value: str, pointer: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise GraphValidationError("timestamp", pointer, "must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GraphValidationError("timestamp", pointer, "must include a timezone")
    return parsed.astimezone(timezone.utc)
