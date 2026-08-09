"""Non-secret connection, admission, and execution-grant domain values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

from bounded_loops.graph.domain.authoring import DataClass, Effect


class ConnectionState(str, Enum):
    DISCOVERED = "discovered"
    ADAPTER_VALIDATED = "adapter_validated"
    SMOKE_PROVEN = "smoke_proven"
    ADMITTED = "admitted"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CredentialKind(str, Enum):
    """The controller-owned credential boundary; never a credential value."""

    LOCAL_SESSION = "local_session"
    VAULT_REFERENCE = "vault_reference"


@dataclass(frozen=True)
class RoutePolicy:
    policy_digest: str
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    allowed_regions: frozenset[str]
    fallback_allowed: bool
    route_verifiable: bool
    data_class_max: DataClass


@dataclass(frozen=True)
class ResolvedRoute:
    provider_id: str
    model_id: str
    region: str
    fallback: bool
    policy_digest: str


@dataclass(frozen=True)
class AdmittedConnection:
    connection_id: str
    organization_id: str
    connector_id: str
    connector_version: str
    consent_digest: str
    evidence_digest: str
    expires_at: str
    capabilities: frozenset[str]
    effects: frozenset[Effect]
    transport: str
    data_path: str
    route_policy: RoutePolicy
    state: ConnectionState

    def compiler_snapshot(self) -> str:
        """Canonical public admission metadata without auth/session references."""
        return json.dumps(
            {
                "capabilities": sorted(self.capabilities), "connection_id": self.connection_id,
                "connector_id": self.connector_id, "connector_version": self.connector_version,
                "consent_digest": self.consent_digest, "data_path": self.data_path,
                "effects": sorted(effect.value for effect in self.effects), "evidence_digest": self.evidence_digest,
                "expires_at": self.expires_at, "organization_id": self.organization_id,
                "route_policy": {
                    "allowed_models": sorted(self.route_policy.allowed_models),
                    "allowed_providers": sorted(self.route_policy.allowed_providers),
                    "allowed_regions": sorted(self.route_policy.allowed_regions),
                    "data_class_max": self.route_policy.data_class_max.value,
                    "fallback_allowed": self.route_policy.fallback_allowed,
                    "policy_digest": self.route_policy.policy_digest,
                    "route_verifiable": self.route_policy.route_verifiable,
                },
                "state": self.state.value, "transport": self.transport,
            },
            separators=(",", ":"), sort_keys=True,
        )


@dataclass(frozen=True)
class ExecutionGrant:
    grant_id: str
    run_id: str
    node_id: str
    attempt: int
    connection_id: str
    effects: frozenset[Effect]
    destinations: frozenset[str]
    expires_at: str


@dataclass(frozen=True)
class CredentialBinding:
    """An internal broker key, deliberately without a secret or a backend reference."""

    binding_id: str
    connection_id: str
    kind: CredentialKind


@dataclass(frozen=True)
class CredentialLease:
    """A single-use authority reference for a controller-owned credential proxy."""

    lease_id: str
    grant_id: str
    run_id: str
    node_id: str
    attempt: int
    connection_id: str
    binding_id: str
    effects: frozenset[Effect]
    destination: str
    expires_at: str
