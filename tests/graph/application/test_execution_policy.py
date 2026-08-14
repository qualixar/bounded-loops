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


def _node(
    *,
    effects: frozenset[Effect],
    isolation: IsolationLevel,
    binding_id: str | None = "binding-1",
    kind: str = "loop",
) -> PlannedNode:
    return PlannedNode(
        node_id="test-node", kind=kind, package_digest=None, binding_id=binding_id,
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
    node = _node(effects=frozenset({Effect.EXTERNAL_WRITE}), isolation=IsolationLevel.CONTAINER_RESTRICTED, kind="loop")
    destinations = [NetworkDestination("publish.example", 443)]
    envelope = _envelope(
        effects=node.required_effects, isolation=node.isolation,
        mode=NetworkMode.ALLOWLIST, destinations=tuple(destinations),
    )

    accepted = validate_execution_envelope(_plan(node), node, envelope)
    destinations.append(NetworkDestination("other.example", 443))

    assert accepted.network_destinations == (NetworkDestination("publish.example", 443),)
    assert accepted.allowed_effects == frozenset({Effect.EXTERNAL_WRITE})


def test_an_unbound_publish_node_may_declare_external_write_under_deny() -> None:
    """The shipped publish worker runs in-process and opens no socket, so its declared network
    effect is an authorisation marker rather than egress.

    This is a TRUST ASSUMPTION about that worker, not a proof — the engine cannot show an
    in-process worker refrains from opening a socket. It is scoped as narrowly as the layer allows:
    publish only, and only while the node is UNBOUND (see the two tests below).
    """
    node = _node(
        effects=frozenset({Effect.EXTERNAL_WRITE}),
        isolation=IsolationLevel.CONTAINER_RESTRICTED,
        kind="publish", binding_id=None,
    )
    envelope = _envelope(
        effects=node.required_effects, isolation=node.isolation,
        transport=None, mode=NetworkMode.DENY,
    )
    accepted = validate_execution_envelope(_plan(node), node, envelope)
    assert accepted.network_mode is NetworkMode.DENY
    assert Effect.EXTERNAL_WRITE in accepted.allowed_effects


@pytest.mark.parametrize("kind", ["approval", "join"])
def test_approval_and_join_declaring_a_network_effect_still_require_an_allowlist(kind: str) -> None:
    """The carve-out is NOT extended to them, and that is deliberate.

    Neither declares effects in any shipped graph, so they never reach this branch in practice —
    which means listing them would only ever take effect for a node someone authored by hand, whose
    worker this engine knows nothing about. NETWORK_EFFECTS is documented as the single source of
    truth for network posture across three layers; widening it by KIND severs the declaration from
    the enforcement it exists to trigger.
    """
    node = _node(
        effects=frozenset({Effect.EXTERNAL_WRITE}),
        isolation=IsolationLevel.CONTAINER_RESTRICTED,
        kind=kind, binding_id=None,
    )
    envelope = _envelope(
        effects=node.required_effects, isolation=node.isolation,
        transport=None, mode=NetworkMode.DENY,
    )

    with pytest.raises(GraphValidationError, match="network allowlist"):
        validate_execution_envelope(_plan(node), node, envelope)


def test_a_publish_node_bound_to_a_transport_still_requires_an_allowlist() -> None:
    """The half of the exemption that is actually CHECKED rather than trusted.

    A binding means a real connection, so the in-process reasoning no longer applies and the
    exemption must not follow the node into a deployment that gave it one.
    """
    node = _node(
        effects=frozenset({Effect.EXTERNAL_WRITE}),
        isolation=IsolationLevel.CONTAINER_RESTRICTED,
        kind="publish", binding_id="binding-1",
    )
    envelope = _envelope(
        effects=node.required_effects, isolation=node.isolation,
        transport=None, mode=NetworkMode.DENY,
    )

    with pytest.raises(GraphValidationError):
        validate_execution_envelope(_plan(node), node, envelope)


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
