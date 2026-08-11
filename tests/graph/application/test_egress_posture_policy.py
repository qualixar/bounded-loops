"""Unit tests for `resolve_local_cli_egress_decision` — the seam between the deployment's
resolved egress posture (`egress_posture.py`) and a local_cli connector node's egress.

Ground truth verified by reading `local_cli_worker.py` in full: `LocalCliConnectorWorker`
runs the CLI unwrapped (no Seatbelt profile, no egress proxy) and hard-refuses any envelope
but `NetworkMode.OPEN` as a defense-in-depth guard. So for a plan containing a local_cli
node:

* OPEN   -> unaffected, byte-for-byte today's behavior.
* ALLOWLIST -> refused UNCONDITIONALLY, before host capabilities are even consulted. The
  refusal is NOT capability-gated because a Mac with Seatbelt would not fix it — the worker
  itself has no cage-wrapping integration yet.
* BROKER -> refused: a subscription CLI authenticates out-of-band; the no-secret EgressBroker
  (a lease bound to one declared destination/method/effect) has nothing to mediate.

A plan with NO local_cli node is untouched by any of this (https/DENY nodes are unaffected).
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.application.egress_posture_policy import resolve_local_cli_egress_decision
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding

_NO_CAGE = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True)
_SEATBELT_WITH_PROXY = PlatformCapabilities(
    platform="darwin", docker_available=False, process_groups=True, rlimits=True,
    seatbelt=True, egress_proxy=True,
)


def _binding(*, transport: str) -> ResolvedBinding:
    return ResolvedBinding(
        binding_id="binding-1", slot_id="model", connector_id="test", connector_version="1",
        connection_id="connection-1", admission_digest="sha256:" + "d" * 64,
        route_policy_digest="sha256:" + "e" * 64, provider_id="anthropic",
        model_target="claude", region="local", fallback=False, transport=transport,
    )


def _node(*, transport: str | None, node_id: str = "agent") -> PlannedNode:
    return PlannedNode(
        node_id=node_id, kind="local_cli" if transport else "research_claim", package_digest=None,
        binding_id="binding-1" if transport else None,
        required_effects=frozenset({Effect.WORKSPACE_WRITE}), isolation=IsolationLevel.PROCESS_RESTRICTED,
        hard_deadline_ms=60_000, budgets={}, approval_policy={},
    )


def _plan(*nodes: PlannedNode, transport: str = "local_cli") -> ExecutionPlan:
    # Test scaffolding only supports a single bound node — every fixture in this file uses
    # exactly one — so one shared binding_id ("binding-1", matching _node()'s default) suffices.
    has_bound_node = any(n.binding_id is not None for n in nodes)
    bindings = (_binding(transport=transport),) if has_bound_node else ()
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "a" * 64,
        source_graph_digest="sha256:" + "b" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=nodes, edges=(), levels=(tuple(n.node_id for n in nodes),),
        package_digests=(), canonical_json=b"{}", connection_bindings=bindings,
    )


def _env(**over: str) -> dict[str, str]:
    return dict(over)


# ── OPEN (the default) is unaffected ────────────────────────────────────────────


def test_open_default_yields_open_network_mode_for_a_local_cli_plan(tmp_path):
    plan = _plan(_node(transport="local_cli"))
    decision = resolve_local_cli_egress_decision(
        plan, environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")), capabilities=_NO_CAGE,
    )
    assert decision.network_mode is NetworkMode.OPEN
    assert decision.network_destinations == ()
    assert decision.requires_broker is False


def test_explicit_open_env_yields_open_for_a_local_cli_plan(tmp_path):
    plan = _plan(_node(transport="local_cli"))
    decision = resolve_local_cli_egress_decision(
        plan,
        environ=_env(BOUNDED_LOOPS_EGRESS_POSTURE="open", BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")),
        capabilities=_NO_CAGE,
    )
    assert decision.network_mode is NetworkMode.OPEN


# ── ALLOWLIST is refused unconditionally for a plan with a local_cli node ───────


def test_allowlist_is_refused_for_local_cli_even_with_the_cage_available(tmp_path):
    plan = _plan(_node(transport="local_cli"))
    with pytest.raises(GraphValidationError, match="not yet implemented for local_cli"):
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
                BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_SEATBELT_WITH_PROXY,  # cage IS available — proves this isn't a capability gate
        )


def test_allowlist_is_refused_for_local_cli_without_the_cage_too(tmp_path):
    plan = _plan(_node(transport="local_cli"))
    with pytest.raises(GraphValidationError, match="not yet implemented for local_cli"):
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
                BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_NO_CAGE,
        )


def test_allowlist_refusal_message_does_not_blame_host_capability(tmp_path):
    # The refusal must not imply "get a better host" — the worker itself has no cage
    # integration, so blaming capability would be misleading.
    plan = _plan(_node(transport="local_cli"))
    try:
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
                BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_SEATBELT_WITH_PROXY,
        )
        pytest.fail("expected GraphValidationError")
    except GraphValidationError as exc:
        assert "cage" not in str(exc).lower() or "unsandboxed" in str(exc).lower()


# ── BROKER is refused for a plan with a local_cli node ──────────────────────────


def test_broker_is_refused_for_a_local_cli_plan(tmp_path):
    plan = _plan(_node(transport="local_cli"))
    with pytest.raises(GraphValidationError, match="BROKER"):
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="broker", BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_NO_CAGE,
        )


# ── a plan with NO local_cli node is unaffected by any posture ─────────────────


def test_broker_does_not_raise_for_a_plan_without_a_local_cli_node(tmp_path):
    plan = _plan(_node(transport="https"), transport="https")
    decision = resolve_local_cli_egress_decision(
        plan,
        environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="broker", BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ),
        capabilities=_NO_CAGE,
    )
    assert decision.requires_broker is False  # inert: nothing in this plan consumes it


def test_allowlist_does_not_raise_for_a_plan_without_a_local_cli_node(tmp_path):
    # Critical: must NOT raise even though the injected capabilities have no cage — an
    # https-only run's success must never depend on Seatbelt/egress-proxy availability, which
    # has nothing to do with how https actually works (caught live by
    # test_byok_https_node_unaffected_by_allowlist_egress_posture_with_no_cage).
    plan = _plan(_node(transport="https"), transport="https")
    decision = resolve_local_cli_egress_decision(
        plan,
        environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ),
        capabilities=_NO_CAGE,
    )
    assert decision.network_mode is None  # inert: the capability-dependent check was skipped entirely


def test_allowlist_host_capability_check_is_skipped_entirely_without_a_local_cli_node(tmp_path):
    # Same call, cage AVAILABLE this time — must ALSO be inert (not "happens to succeed"),
    # proving the check is skipped, not merely passing by coincidence.
    plan = _plan(_node(transport="https"), transport="https")
    decision = resolve_local_cli_egress_decision(
        plan,
        environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ),
        capabilities=_SEATBELT_WITH_PROXY,
    )
    assert decision.network_mode is None
    assert decision.rationale == "no local_cli node in this plan; posture not applied"


# ── a genuinely misconfigured resolution still raises cleanly (not a traceback) ─


def test_unrecognized_posture_env_value_raises(tmp_path):
    plan = _plan(_node(transport="local_cli"))
    with pytest.raises(GraphValidationError, match="unrecognized egress posture"):
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="yolo", BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_NO_CAGE,
        )


def test_default_capabilities_probe_the_real_platform_when_omitted(tmp_path):
    # Mirrors build_enforcer()'s / decide_egress_posture()'s own "probe unless injected"
    # convention — must not require every caller to probe the platform themselves.
    plan = _plan(_node(transport="local_cli"))
    decision = resolve_local_cli_egress_decision(
        plan, environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")),
    )
    assert decision.network_mode is NetworkMode.OPEN
