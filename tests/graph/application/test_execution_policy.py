from __future__ import annotations

import pytest

from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    NetworkDestination,
    NetworkMode,
    validate_execution_envelope,
)
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding


def _node(*, effects: frozenset[Effect], isolation: IsolationLevel, binding_id: str | None = "binding-1") -> PlannedNode:
    return PlannedNode(
        node_id="publish", kind="publish", package_digest=None, binding_id=binding_id,
        required_effects=effects, isolation=isolation, hard_deadline_ms=1_000,
        budgets={}, approval_policy={},
    )


def _plan(node: PlannedNode) -> ExecutionPlan:
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "a" * 64,
        source_graph_digest="sha256:" + "b" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=(node,), edges=(), levels=((node.node_id,),),
        package_digests=(), canonical_json=b"{}",
        connection_bindings=(ResolvedBinding(
            binding_id="binding-1", slot_id="model", connector_id="test", connector_version="1",
            connection_id="connection-1", admission_digest="sha256:" + "d" * 64,
            route_policy_digest="sha256:" + "e" * 64, provider_id="provider",
            model_target="model", region="in", fallback=False, transport="api_proxy",
        ),),
    )


def _envelope(
    *,
    effects: frozenset[Effect],
    isolation: IsolationLevel,
    transport: str | None = "api_proxy",
    mode: NetworkMode = NetworkMode.DENY,
    destinations: tuple[NetworkDestination, ...] = (),
) -> ExecutionEnvelope:
    return ExecutionEnvelope(isolation, transport, effects, mode, destinations)


def test_external_effect_denies_without_a_specific_network_allowlist():
    node = _node(effects=frozenset({Effect.EXTERNAL_WRITE}), isolation=IsolationLevel.CONTAINER_RESTRICTED)

    with pytest.raises(GraphValidationError, match="network allowlist"):
        validate_execution_envelope(
            _plan(node), node,
            _envelope(effects=node.required_effects, isolation=node.isolation),
        )


def test_effect_floor_denies_an_under_isolated_execution_envelope():
    node = _node(effects=frozenset({Effect.FINANCIAL}), isolation=IsolationLevel.WORKSPACE_ONLY)

    with pytest.raises(GraphValidationError, match="isolation"):
        validate_execution_envelope(
            _plan(node), node,
            _envelope(
                effects=node.required_effects, isolation=IsolationLevel.PROCESS_RESTRICTED,
                mode=NetworkMode.ALLOWLIST, destinations=(NetworkDestination("payments.example", 443),),
            ),
        )


def test_transport_must_match_the_compiled_connection_binding_exactly():
    node = _node(effects=frozenset({Effect.READ_ONLY}), isolation=IsolationLevel.WORKSPACE_ONLY)

    with pytest.raises(GraphValidationError, match="transport"):
        validate_execution_envelope(
            _plan(node), node,
            _envelope(effects=node.required_effects, isolation=node.isolation, transport="local_cli"),
        )


def test_non_network_effect_denies_destinations_and_open_network_mode():
    node = _node(effects=frozenset({Effect.READ_ONLY}), isolation=IsolationLevel.WORKSPACE_ONLY)

    with pytest.raises(GraphValidationError, match="network"):
        validate_execution_envelope(
            _plan(node), node,
            _envelope(
                effects=node.required_effects, isolation=node.isolation,
                mode=NetworkMode.ALLOWLIST, destinations=(NetworkDestination("model.example", 443),),
            ),
        )


def test_exact_effect_transport_isolation_and_allowlist_produces_immutable_envelope():
    node = _node(effects=frozenset({Effect.EXTERNAL_WRITE}), isolation=IsolationLevel.CONTAINER_RESTRICTED)
    destinations = [NetworkDestination("publish.example", 443)]
    envelope = _envelope(
        effects=node.required_effects, isolation=node.isolation,
        mode=NetworkMode.ALLOWLIST, destinations=tuple(destinations),
    )

    accepted = validate_execution_envelope(_plan(node), node, envelope)
    destinations.append(NetworkDestination("other.example", 443))

    assert accepted.network_destinations == (NetworkDestination("publish.example", 443),)
    assert accepted.allowed_effects == frozenset({Effect.EXTERNAL_WRITE})
