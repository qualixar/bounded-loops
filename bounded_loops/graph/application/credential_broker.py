"""Opaque, controller-only credential leases.

This is intentionally not a vault and does not execute a child process.  It
creates the non-secret lease boundary that a future local-keychain/KMS-backed
proxy must consume.  A worker may receive a lease, never a credential value or
the broker's backend reference.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Iterable

from bounded_loops.graph.application.connections import validate_execution_grant
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import (
    CredentialBinding,
    CredentialLease,
    ExecutionGrant,
)
from bounded_loops.graph.domain.errors import GraphValidationError


class OpaqueCredentialBroker:
    """Mint narrow, serializable leases from controller-held binding identities."""

    def __init__(self, bindings: Iterable[CredentialBinding]) -> None:
        by_id: dict[str, CredentialBinding] = {}
        for binding in bindings:
            if not binding.binding_id or not binding.connection_id:
                raise GraphValidationError("credential_binding", "/bindings", "binding IDs must be non-empty")
            if binding.binding_id in by_id:
                raise GraphValidationError("credential_binding", "/bindings", "binding IDs must be unique")
            by_id[binding.binding_id] = binding
        self._bindings = by_id

    def mint_lease(
        self,
        grant: ExecutionGrant,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        effect: Effect,
        destination: str,
        now: datetime | None = None,
    ) -> CredentialLease:
        """Return a lease only for the grant's exact audience, effect, and destination."""
        validate_execution_grant(grant, run_id, node_id, attempt, effect, now=now)
        if not destination or destination not in grant.destinations:
            raise GraphValidationError(
                "grant_destination", "/grant", "grant does not authorize this destination"
            )
        binding = self._binding_for_connection(grant.connection_id)
        material = json.dumps(
            {
                "attempt": attempt,
                "binding_id": binding.binding_id,
                "destination": destination,
                "effect": effect.value,
                "expires_at": grant.expires_at,
                "grant_id": grant.grant_id,
                "node_id": node_id,
                "run_id": run_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return CredentialLease(
            lease_id="lease:" + hashlib.sha256(material).hexdigest(),
            grant_id=grant.grant_id,
            run_id=run_id,
            node_id=node_id,
            attempt=attempt,
            connection_id=grant.connection_id,
            binding_id=binding.binding_id,
            effects=frozenset({effect}),
            destination=destination,
            expires_at=grant.expires_at,
        )

    def _binding_for_connection(self, connection_id: str) -> CredentialBinding:
        matches = [binding for binding in self._bindings.values() if binding.connection_id == connection_id]
        if len(matches) != 1:
            raise GraphValidationError(
                "credential_binding", "/bindings", "exactly one broker binding is required for the connection"
            )
        return matches[0]
