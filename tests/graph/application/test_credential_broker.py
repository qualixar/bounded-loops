from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from bounded_loops.graph.application.connections import (
    ExecutionGrantRequest,
    issue_execution_grant,
)
from bounded_loops.graph.application.credential_broker import OpaqueCredentialBroker
from bounded_loops.graph.domain.authoring import DataClass, Effect
from bounded_loops.graph.domain.connections import (
    AdmittedConnection,
    ConnectionState,
    CredentialBinding,
    CredentialKind,
    RoutePolicy,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _admitted_connection() -> AdmittedConnection:
    """An admitted connection, constructed directly.

    Built by walking ``register_connection`` and three ``advance_connection`` edges until
    0.7.0 removed that lifecycle as an orphaned capability. The broker under test never
    called it; only fixtures did. Constructing the end state directly is what the broker
    actually receives in production, where nothing negotiates a connection at all.
    """
    return AdmittedConnection(
        connection_id="conn-1", organization_id="org-1", connector_id="codex-cli",
        connector_version="1.0.0", consent_digest=_digest("a"), evidence_digest=_digest("f"),
        expires_at="2026-12-31T00:00:00Z", capabilities=frozenset({"text_generation"}),
        effects=frozenset({Effect.READ_ONLY}), transport="local_cli",
        data_path="host-managed vendor session",
        route_policy=RoutePolicy(
            policy_digest=_digest("c"), allowed_providers=frozenset({"openai"}),
            allowed_models=frozenset({"codex"}), allowed_regions=frozenset({"in"}),
            fallback_allowed=False, route_verifiable=True, data_class_max=DataClass.INTERNAL,
        ),
        state=ConnectionState.ADMITTED,
    )


def _grant():
    return issue_execution_grant(
        ExecutionGrantRequest(
            run_id="run-1", node_id="node-1", attempt=1,
            connection=_admitted_connection(), effects=frozenset({Effect.READ_ONLY}),
            destinations=frozenset({"provider:openai"}), expires_at="2026-08-09T00:00:00Z",
        ),
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )


def _broker() -> OpaqueCredentialBroker:
    return OpaqueCredentialBroker((
        CredentialBinding(
            binding_id="binding-1", connection_id="conn-1",
            kind=CredentialKind.LOCAL_SESSION,
        ),
    ))


def test_broker_mints_an_opaque_lease_without_a_credential_or_backing_reference():
    lease = _broker().mint_lease(
        _grant(), run_id="run-1", node_id="node-1", attempt=1,
        effect=Effect.READ_ONLY, destination="provider:openai",
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert lease.binding_id == "binding-1"
    assert lease.connection_id == "conn-1"
    assert lease.effects == frozenset({Effect.READ_ONLY})
    assert set(asdict(lease)) == {
        "lease_id", "grant_id", "run_id", "node_id", "attempt", "connection_id",
        "binding_id", "effects", "destination", "expires_at",
    }
    assert "vendor-profile" not in repr(lease).lower()
    assert "local_session" not in repr(lease).lower()


def test_broker_denies_wrong_audience_effect_and_expired_grant_before_lease_issuance():
    broker = _broker()
    grant = _grant()

    with pytest.raises(GraphValidationError, match="audience"):
        broker.mint_lease(
            grant, run_id="other-run", node_id="node-1", attempt=1,
            effect=Effect.READ_ONLY, destination="provider:openai",
            now=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
    with pytest.raises(GraphValidationError, match="effect"):
        broker.mint_lease(
            grant, run_id="run-1", node_id="node-1", attempt=1,
            effect=Effect.WORKSPACE_WRITE, destination="provider:openai",
            now=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
    with pytest.raises(GraphValidationError, match="expired"):
        broker.mint_lease(
            grant, run_id="run-1", node_id="node-1", attempt=1,
            effect=Effect.READ_ONLY, destination="provider:openai",
            now=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

    with pytest.raises(GraphValidationError, match="destination"):
        broker.mint_lease(
            grant, run_id="run-1", node_id="node-1", attempt=1,
            effect=Effect.READ_ONLY, destination="provider:other",
            now=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )


def test_broker_refuses_a_binding_for_a_different_connection():
    broker = OpaqueCredentialBroker((
        CredentialBinding(
            binding_id="binding-other", connection_id="other-connection",
            kind=CredentialKind.VAULT_REFERENCE,
        ),
    ))

    with pytest.raises(GraphValidationError, match="binding"):
        broker.mint_lease(
            _grant(), run_id="run-1", node_id="node-1", attempt=1,
            effect=Effect.READ_ONLY, destination="provider:openai",
            now=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
