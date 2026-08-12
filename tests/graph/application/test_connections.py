from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bounded_loops.graph.application.connections import (
    ConnectionAdmissionRequest,
    ExecutionGrantRequest,
    RouteRequest,
    advance_connection,
    authorize_route,
    compiler_connection_snapshot,
    issue_execution_grant,
    register_connection,
    validate_execution_grant,
)
from bounded_loops.graph.domain.authoring import DataClass, Effect, IsolationLevel
from bounded_loops.graph.domain.connections import ConnectionState, RoutePolicy
from bounded_loops.graph.domain.errors import GraphValidationError


_TEST_NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _admission() -> ConnectionAdmissionRequest:
    return ConnectionAdmissionRequest(
        connection_id="conn-1", organization_id="org-1", connector_id="codex-cli",
        connector_version="1.0.0", local_session_ref="vendor-profile:opaque",
        credential_ref=None, consent_digest=_digest("a"), evidence_digest=_digest("b"),
        expires_at="2026-12-31T00:00:00Z", capabilities=frozenset({"text_generation"}),
        effects=frozenset({Effect.READ_ONLY}), transport="local_cli",
        data_path="host-managed vendor session",
        route_policy=RoutePolicy(
            policy_digest=_digest("c"), allowed_providers=frozenset({"openai"}),
            allowed_models=frozenset({"codex"}), allowed_regions=frozenset({"in"}),
            fallback_allowed=False, route_verifiable=True, data_class_max=DataClass.INTERNAL,
        ),
    )


def _admitted():
    connection = register_connection(_admission())
    connection = advance_connection(connection, ConnectionState.ADAPTER_VALIDATED, _digest("d"))
    connection = advance_connection(connection, ConnectionState.SMOKE_PROVEN, _digest("e"))
    return advance_connection(connection, ConnectionState.ADMITTED, _digest("f"))


def test_connection_requires_evidence_backed_lifecycle_before_grant():
    connection = register_connection(_admission())

    assert connection.state is ConnectionState.DISCOVERED
    assert "vendor-profile" not in connection.compiler_snapshot().lower()
    with pytest.raises(GraphValidationError, match="lifecycle"):
        advance_connection(connection, ConnectionState.ADMITTED, _digest("d"))
    with pytest.raises(GraphValidationError, match="admitted"):
        issue_execution_grant(ExecutionGrantRequest(
            run_id="run-1", node_id="node-1", attempt=1, connection=connection,
            effects=frozenset({Effect.READ_ONLY}), destinations=frozenset(),
            expires_at="2026-08-09T00:00:00Z",
        ), now=_TEST_NOW)

    admitted = _admitted()
    grant = issue_execution_grant(ExecutionGrantRequest(
        run_id="run-1", node_id="node-1", attempt=1, connection=admitted,
        effects=frozenset({Effect.READ_ONLY}), destinations=frozenset(),
        expires_at="2026-08-09T00:00:00Z",
    ), now=_TEST_NOW)
    assert validate_execution_grant(grant, "run-1", "node-1", 1, Effect.READ_ONLY, now=_TEST_NOW)
    with pytest.raises(GraphValidationError, match="expired"):
        validate_execution_grant(
            grant, "run-1", "node-1", 1, Effect.READ_ONLY,
            now=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
    assert authorize_route(admitted, RouteRequest("openai", "codex", "in", False, DataClass.PUBLIC)).model_id == "codex"

    expired = replace(admitted, expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(GraphValidationError, match="expired"):
        issue_execution_grant(ExecutionGrantRequest(
            run_id="run-1", node_id="node-1", attempt=1, connection=expired,
            effects=frozenset({Effect.READ_ONLY}), destinations=frozenset(),
            expires_at="2026-08-09T00:00:00Z",
        ), now=_TEST_NOW)


def test_m4_is_nonroutable_and_terminal_lifecycle_states_cannot_reenter():
    request = _admission()
    with pytest.raises(GraphValidationError, match="M4"):
        register_connection(ConnectionAdmissionRequest(**{**request.__dict__, "connector_id": "m4-company-claude"}))

    revoked = advance_connection(_admitted(), ConnectionState.REVOKED, _digest("1"))
    with pytest.raises(GraphValidationError, match="lifecycle"):
        advance_connection(revoked, ConnectionState.ADMITTED, _digest("2"))


def test_route_policy_denies_unverifiable_restricted_fallback_and_unknown_routes():
    connection = _admitted()
    with pytest.raises(GraphValidationError, match="fallback"):
        authorize_route(connection, RouteRequest("openai", "codex", "in", True, DataClass.PUBLIC))
    with pytest.raises(GraphValidationError, match="provider"):
        authorize_route(connection, RouteRequest("other", "codex", "in", False, DataClass.PUBLIC))

    unverifiable = ConnectionAdmissionRequest(**{
        **_admission().__dict__,
        "route_policy": RoutePolicy(
            policy_digest=_digest("3"), allowed_providers=frozenset({"openai"}),
            allowed_models=frozenset({"codex"}), allowed_regions=frozenset({"in"}),
            fallback_allowed=False, route_verifiable=False, data_class_max=DataClass.RESTRICTED,
        ),
    })
    connection = register_connection(unverifiable)
    connection = advance_connection(connection, ConnectionState.ADAPTER_VALIDATED, _digest("4"))
    connection = advance_connection(connection, ConnectionState.SMOKE_PROVEN, _digest("5"))
    connection = advance_connection(connection, ConnectionState.ADMITTED, _digest("6"))
    with pytest.raises(GraphValidationError, match="verifiable"):
        authorize_route(connection, RouteRequest("openai", "codex", "in", False, DataClass.RESTRICTED))


def test_compiler_connection_snapshot_can_only_come_from_an_admitted_authorized_route():
    connection = _admitted()
    route = authorize_route(connection, RouteRequest("openai", "codex", "in", False, DataClass.PUBLIC))

    snapshot = compiler_connection_snapshot(
        connection, route, binding_id="binding-1", slot_id="research-model",
        isolation=IsolationLevel.PROCESS_RESTRICTED,
    )

    assert snapshot["provider_id"] == "openai"
    assert snapshot["model_target"] == "codex"
    assert snapshot["region"] == "in"
    assert "vendor-profile" not in repr(snapshot).lower()

    with pytest.raises(GraphValidationError, match="expired"):
        compiler_connection_snapshot(
            replace(connection, expires_at="2020-01-01T00:00:00Z"), route,
            binding_id="binding-1", slot_id="research-model",
            isolation=IsolationLevel.PROCESS_RESTRICTED,
        )
