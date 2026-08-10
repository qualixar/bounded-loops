"""NetworkMode.OPEN — trusted-local network-open posture for local-CLI connectors (RC Mode 1).

OPEN gives an admitted `local_cli` connector full outbound network while keeping filesystem
write-confinement, so the agent CLI reaches its model + tools and real work completes. It is
gated to the compiler-admitted `local_cli` transport; ALLOWLIST (the proxy egress firewall)
stays refused at the sandbox layer until RC-LOCKDOWN.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.provider import Control
from bounded_loops.graph.adapters.enforcement.providers.native import NativeProvider
from bounded_loops.graph.adapters.enforcement.sandbox import SandboxMechanism, wrap_argv
from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    NetworkDestination,
    NetworkMode,
    validate_execution_envelope,
)
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding

_SEATBELT = PlatformCapabilities(platform="darwin", docker_available=False, process_groups=True, rlimits=True, seatbelt=True)
_NETNS_ONLY = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True, net_namespace=True)


def _node(*, effects: frozenset[Effect], isolation: IsolationLevel) -> PlannedNode:
    return PlannedNode(
        node_id="agent", kind="local_cli", package_digest=None, binding_id="binding-1",
        required_effects=effects, isolation=isolation, hard_deadline_ms=60_000,
        budgets={}, approval_policy={},
    )


def _plan(node: PlannedNode, *, transport: str) -> ExecutionPlan:
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "a" * 64,
        source_graph_digest="sha256:" + "b" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=(node,), edges=(), levels=((node.node_id,),),
        package_digests=(), canonical_json=b"{}",
        connection_bindings=(ResolvedBinding(
            binding_id="binding-1", slot_id="model", connector_id="test", connector_version="1",
            connection_id="connection-1", admission_digest="sha256:" + "d" * 64,
            route_policy_digest="sha256:" + "e" * 64, provider_id="anthropic",
            model_target="claude", region="local", fallback=False, transport=transport,
        ),),
    )


def test_open_network_is_accepted_for_an_admitted_local_cli_connector():
    node = _node(effects=frozenset({Effect.WORKSPACE_WRITE}), isolation=IsolationLevel.PROCESS_RESTRICTED)
    envelope = ExecutionEnvelope(IsolationLevel.PROCESS_RESTRICTED, "local_cli", node.required_effects, NetworkMode.OPEN, ())
    assert validate_execution_envelope(_plan(node, transport="local_cli"), node, envelope) is envelope


def test_open_network_is_rejected_for_a_non_local_cli_node():
    node = _node(effects=frozenset({Effect.WORKSPACE_WRITE}), isolation=IsolationLevel.PROCESS_RESTRICTED)
    envelope = ExecutionEnvelope(IsolationLevel.PROCESS_RESTRICTED, "api_proxy", node.required_effects, NetworkMode.OPEN, ())
    with pytest.raises(GraphValidationError, match="local-CLI"):
        validate_execution_envelope(_plan(node, transport="api_proxy"), node, envelope)


def test_open_network_rejects_a_destination_allowlist():
    node = _node(effects=frozenset({Effect.WORKSPACE_WRITE}), isolation=IsolationLevel.PROCESS_RESTRICTED)
    envelope = ExecutionEnvelope(
        IsolationLevel.PROCESS_RESTRICTED, "local_cli", node.required_effects, NetworkMode.OPEN,
        (NetworkDestination("api.anthropic.com", 443),),
    )
    with pytest.raises(GraphValidationError, match="allowlist"):
        validate_execution_envelope(_plan(node, transport="local_cli"), node, envelope)


def test_seatbelt_opens_the_network_under_open_but_keeps_write_confinement(tmp_path):
    argv = wrap_argv(
        SandboxMechanism.SEATBELT, inner_argv=["/bin/echo", "hi"],
        workspace=tmp_path, home=tmp_path, tmpdir=tmp_path, network_mode=NetworkMode.OPEN,
    )
    profile = argv[2]  # sandbox-exec -p <profile> ...
    assert "(deny network*)" not in profile  # network is OPEN
    assert '(deny file-write* (subpath "/"))' in profile  # writes still confined
    denied = wrap_argv(
        SandboxMechanism.SEATBELT, inner_argv=["/bin/echo", "hi"],
        workspace=tmp_path, home=tmp_path, tmpdir=tmp_path, network_mode=NetworkMode.DENY,
    )
    assert "(deny network*)" in denied[2]


def test_bubblewrap_shares_net_under_open(tmp_path):
    argv = wrap_argv(
        SandboxMechanism.BUBBLEWRAP, inner_argv=["/bin/echo", "hi"],
        workspace=tmp_path, home=tmp_path, tmpdir=tmp_path, network_mode=NetworkMode.OPEN,
    )
    assert "--share-net" in argv and "--unshare-net" not in argv


def test_wrap_argv_still_refuses_the_allowlist_egress_firewall(tmp_path):
    with pytest.raises(ValueError, match="allowlist"):
        wrap_argv(
            SandboxMechanism.SEATBELT, inner_argv=["/bin/echo"],
            workspace=tmp_path, home=tmp_path, tmpdir=tmp_path, network_mode=NetworkMode.ALLOWLIST,
        )


def test_native_reports_open_network_as_not_enforced():
    provider = NativeProvider(_SEATBELT)
    open_controls = provider._controls(SandboxMechanism.SEATBELT, NetworkMode.OPEN)
    assert open_controls.net is Control.NOT_ENFORCED  # honest: network is open
    assert open_controls.fs_write is Control.ENFORCED  # writes still confined
    deny_controls = provider._controls(SandboxMechanism.SEATBELT, NetworkMode.DENY)
    assert deny_controls.net is Control.ENFORCED


def test_native_skips_the_netns_fallback_under_open():
    provider = NativeProvider(_NETNS_ONLY)
    # unshare -n can only DENY the network, so it must not be chosen for OPEN.
    assert provider._mechanism(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.OPEN) is SandboxMechanism.NONE
    assert provider._mechanism(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.DENY) is SandboxMechanism.UNSHARE_NET


def test_native_refuses_the_allowlist_egress_firewall():
    assert NativeProvider(_SEATBELT)._mechanism(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.ALLOWLIST) is None
