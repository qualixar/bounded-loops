"""E2.1 — fail-closed execution enforcer capability gate.

The enforcer must translate an accepted ExecutionEnvelope into a decision about
whether the CURRENT platform can actually deliver the required isolation, and
refuse (fail-closed) when it cannot. It must never pretend to enforce isolation
it cannot provide. Capabilities are injected so these tests are deterministic
and never touch a real Docker daemon.
"""

from __future__ import annotations

from dataclasses import replace
import types

import pytest

from bounded_loops.graph.adapters.enforcement import (
    ExecutionEnforcer,
    PlatformCapabilities,
    build_enforcer,
)
from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    NetworkDestination,
    NetworkMode,
)
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError

_DEST = (NetworkDestination(hostname="api.example.com", port=443),)


def _caps(**over) -> PlatformCapabilities:
    base = PlatformCapabilities(
        platform="linux",
        docker_available=True,
        process_groups=True,
        rlimits=True,
        egress_proxy=False,
    )
    return replace(base, **over)


def _env(level, effects, mode, dests=()) -> ExecutionEnvelope:
    return ExecutionEnvelope(
        isolation=level,
        transport=None,
        allowed_effects=frozenset(effects),
        network_mode=mode,
        network_destinations=dests,
    )


def _node(node_id, level, effects):
    return types.SimpleNamespace(node_id=node_id, isolation=level, required_effects=frozenset(effects))


def _plan(nodes):
    return types.SimpleNamespace(nodes=tuple(nodes))


def _enforce(caps, level, effects, mode, dests=()):
    node = _node("n", level, effects)
    ExecutionEnforcer(caps).enforce(plan=_plan([node]), node=node, envelope=_env(level, effects, mode, dests))


def test_workspace_only_is_enforceable_on_every_posix_platform():
    _enforce(_caps(platform="darwin", docker_available=False), IsolationLevel.WORKSPACE_ONLY, [Effect.READ_ONLY], NetworkMode.DENY)
    _enforce(_caps(platform="linux", docker_available=False), IsolationLevel.WORKSPACE_ONLY, [Effect.WORKSPACE_WRITE], NetworkMode.DENY)


def test_process_restricted_ok_on_darwin_with_process_groups():
    _enforce(_caps(platform="darwin", docker_available=False, process_groups=True), IsolationLevel.PROCESS_RESTRICTED, [Effect.WORKSPACE_WRITE], NetworkMode.DENY)


def test_process_restricted_fails_closed_without_process_groups():
    with pytest.raises(GraphValidationError):
        _enforce(_caps(platform="win32", process_groups=False), IsolationLevel.PROCESS_RESTRICTED, [Effect.WORKSPACE_WRITE], NetworkMode.DENY)


def test_container_restricted_fails_closed_without_a_docker_daemon():
    with pytest.raises(GraphValidationError):
        _enforce(_caps(docker_available=False), IsolationLevel.CONTAINER_RESTRICTED, [Effect.READ_ONLY], NetworkMode.DENY)


def test_container_restricted_ok_with_docker_and_denied_network():
    _enforce(_caps(docker_available=True), IsolationLevel.CONTAINER_RESTRICTED, [Effect.READ_ONLY], NetworkMode.DENY)


def test_allowlist_egress_fails_closed_without_an_egress_proxy_even_with_docker():
    # external effects require a container AND an egress proxy; the proxy does not
    # exist yet, so an authorized-egress node must fail closed, not pretend.
    with pytest.raises(GraphValidationError):
        _enforce(_caps(docker_available=True, egress_proxy=False), IsolationLevel.CONTAINER_RESTRICTED, [Effect.EXTERNAL_WRITE], NetworkMode.ALLOWLIST, _DEST)


def test_allowlist_egress_ok_when_seatbelt_cage_and_egress_proxy_available():
    # RC-LOCKDOWN: authorized egress is enforceable on macOS Seatbelt (loopback-only egress cage +
    # proxy). egress_proxy availability alone is not enough — the Seatbelt cage must be present.
    _enforce(
        _caps(platform="darwin", seatbelt=True, docker_available=True, egress_proxy=True),
        IsolationLevel.CONTAINER_RESTRICTED, [Effect.EXTERNAL_WRITE], NetworkMode.ALLOWLIST, _DEST,
    )


def test_allowlist_egress_fails_closed_without_a_seatbelt_cage():
    # egress proxy present but no Seatbelt (docker-only Linux) → the loopback cage is not expressible.
    with pytest.raises(GraphValidationError):
        _enforce(
            _caps(platform="linux", seatbelt=False, docker_available=True, egress_proxy=True),
            IsolationLevel.CONTAINER_RESTRICTED, [Effect.EXTERNAL_WRITE], NetworkMode.ALLOWLIST, _DEST,
        )


def test_customer_managed_worker_fails_closed():
    with pytest.raises(GraphValidationError):
        _enforce(_caps(), IsolationLevel.CUSTOMER_MANAGED_WORKER, [Effect.READ_ONLY], NetworkMode.DENY)


def test_capability_matrix_publishes_controls_and_limits_honestly():
    caps = _caps(platform="darwin", docker_available=False)
    ws = caps.enforced_controls(IsolationLevel.WORKSPACE_ONLY)
    assert any("workspace" in c.lower() for c in ws)
    assert any("network" in c.lower() and ("not" in c.lower() or "no" in c.lower()) for c in ws), \
        "workspace_only must disclose it does not OS-enforce network isolation"


def test_build_enforcer_fails_closed_before_run_if_any_node_unenforceable():
    caps = _caps(docker_available=False)
    plan = _plan([
        _node("ok", IsolationLevel.WORKSPACE_ONLY, [Effect.READ_ONLY]),
        _node("needs_container", IsolationLevel.CONTAINER_RESTRICTED, [Effect.EXTERNAL_WRITE]),
    ])
    with pytest.raises(GraphValidationError):
        build_enforcer(plan, capabilities=caps)


def test_build_enforcer_returns_enforcer_when_all_nodes_supported():
    caps = _caps(docker_available=True)
    plan = _plan([
        _node("a", IsolationLevel.WORKSPACE_ONLY, [Effect.READ_ONLY]),
        _node("b", IsolationLevel.CONTAINER_RESTRICTED, [Effect.READ_ONLY]),  # container + no network
    ])
    enforcer = build_enforcer(plan, capabilities=caps)
    assert isinstance(enforcer, ExecutionEnforcer)
