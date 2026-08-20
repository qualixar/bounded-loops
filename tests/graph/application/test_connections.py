"""Execution grants: bound to one audience, carrying no credential.

The connection-admission lifecycle these tests used to cover was removed in 0.7.0 as a
confirmed orphaned capability — four public functions with no engine caller, whose only
callers were the tests in this file. That is the shape the release guard names: a unit test
can never report missing wiring when the test IS the wiring.

One assertion was deliberately NOT carried over. The old suite checked that a connection's
compiler snapshot did not leak ``local_session_ref``, which was meaningful because the
admission request carried that field and ``register_connection`` dropped it on purpose. With
the request type gone, ``AdmittedConnection`` has no such field to leak, so the same
assertion would now hold by construction — vacuous under this repository's own definition,
and a vacuous guard is worse than no guard because it reads like cover.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bounded_loops.graph.application.connections import (
    ExecutionGrantRequest,
    issue_execution_grant,
    validate_execution_grant,
)
from bounded_loops.graph.domain.authoring import DataClass, Effect
from bounded_loops.graph.domain.connections import (
    AdmittedConnection,
    ConnectionState,
    RoutePolicy,
)
from bounded_loops.graph.domain.errors import GraphValidationError


_TEST_NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _connection(state: ConnectionState = ConnectionState.ADMITTED) -> AdmittedConnection:
    return AdmittedConnection(
        connection_id="conn-1", organization_id="org-1", connector_id="codex-cli",
        connector_version="1.0.0", consent_digest=_digest("a"), evidence_digest=_digest("b"),
        expires_at="2026-12-31T00:00:00Z", capabilities=frozenset({"text_generation"}),
        effects=frozenset({Effect.READ_ONLY}), transport="local_cli",
        data_path="host-managed vendor session",
        route_policy=RoutePolicy(
            policy_digest=_digest("c"), allowed_providers=frozenset({"openai"}),
            allowed_models=frozenset({"codex"}), allowed_regions=frozenset({"in"}),
            fallback_allowed=False, route_verifiable=True, data_class_max=DataClass.INTERNAL,
        ),
        state=state,
    )


def _request(connection: AdmittedConnection) -> ExecutionGrantRequest:
    return ExecutionGrantRequest(
        run_id="run-1", node_id="node-1", attempt=1, connection=connection,
        effects=frozenset({Effect.READ_ONLY}), destinations=frozenset(),
        expires_at="2026-08-09T00:00:00Z",
    )


def test_only_an_admitted_connection_issues_a_grant() -> None:
    with pytest.raises(GraphValidationError, match="admitted"):
        issue_execution_grant(_request(_connection(ConnectionState.DISCOVERED)), now=_TEST_NOW)

    grant = issue_execution_grant(_request(_connection()), now=_TEST_NOW)
    assert validate_execution_grant(grant, "run-1", "node-1", 1, Effect.READ_ONLY, now=_TEST_NOW)


def test_a_grant_is_bound_to_one_run_node_attempt_and_effect() -> None:
    grant = issue_execution_grant(_request(_connection()), now=_TEST_NOW)

    for run_id, node_id, attempt in (("run-2", "node-1", 1), ("run-1", "node-2", 1), ("run-1", "node-1", 2)):
        with pytest.raises(GraphValidationError, match="audience"):
            validate_execution_grant(grant, run_id, node_id, attempt, Effect.READ_ONLY, now=_TEST_NOW)

    with pytest.raises(GraphValidationError, match="effect"):
        validate_execution_grant(grant, "run-1", "node-1", 1, Effect.EXTERNAL_WRITE, now=_TEST_NOW)


def test_a_grant_expires_and_cannot_outlive_its_connection() -> None:
    grant = issue_execution_grant(_request(_connection()), now=_TEST_NOW)
    with pytest.raises(GraphValidationError, match="expired"):
        validate_execution_grant(
            grant, "run-1", "node-1", 1, Effect.READ_ONLY,
            now=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

    expired = replace(_connection(), expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(GraphValidationError, match="expired"):
        issue_execution_grant(_request(expired), now=_TEST_NOW)

    outliving = replace(_request(_connection()), expires_at="2027-06-01T00:00:00Z")
    with pytest.raises(GraphValidationError, match="expiry"):
        issue_execution_grant(outliving, now=_TEST_NOW)


def test_a_grant_cannot_request_an_effect_the_connection_never_declared() -> None:
    escalating = replace(_request(_connection()), effects=frozenset({Effect.READ_ONLY, Effect.EXTERNAL_WRITE}))
    with pytest.raises(GraphValidationError, match="effect"):
        issue_execution_grant(escalating, now=_TEST_NOW)
