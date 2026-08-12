from __future__ import annotations

import pytest

from bounded_loops.graph.application.execution_policy import (
    _EFFECT_MINIMUM,
    ExecutionEnvelope,
    NetworkDestination,
    NetworkMode,
    validate_execution_envelope,
)
from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS, Effect, IsolationLevel
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


def test_network_bearing_and_container_restricted_effect_sets_coincide_today():
    """TRIPWIRE, not a constraint — these two sets answer DIFFERENT questions.

    ``NETWORK_EFFECTS`` answers "may this node egress at all?"; ``_EFFECT_MINIMUM``
    answers "how isolated must this node run?". They happen to name the same three
    effects today, and that coincidence is load-bearing for reviewers reading either
    one in isolation. They are deliberately NOT derived from each other: an effect
    could legitimately be network-bearing without requiring container isolation (or
    the reverse), and deriving one from the other would silently return the wrong
    answer for one axis the moment that happens.

    So if this test fails, nothing is broken — you have made a real policy decision.
    Confirm BOTH axes are right for the new effect, then update this test's expected
    sets and say in the commit message why they now differ.
    """
    container_restricted_effects = frozenset(
        effect for effect, floor in _EFFECT_MINIMUM.items()
        if floor is IsolationLevel.CONTAINER_RESTRICTED
    )

    assert NETWORK_EFFECTS == frozenset(
        {Effect.EXTERNAL_WRITE, Effect.FINANCIAL, Effect.IRREVERSIBLE}
    )
    assert container_restricted_effects == NETWORK_EFFECTS
