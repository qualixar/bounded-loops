"""Unit tests for `resolve_local_cli_egress_decision` — the seam between the deployment's
resolved egress posture (`egress_posture.py`) and a local_cli connector node's egress.

DECISION CHANGE (Varun): `LocalCliConnectorWorker` now has a real caged path for ALLOWLIST
(see `local_cli_worker.py` and its tests) — it reuses the SAME Seatbelt loopback-proxy cage
`SandboxedNodeWorker`/`https` already use. So for a plan containing a local_cli node:

* OPEN      -> unaffected, byte-for-byte today's behavior.
* ALLOWLIST -> honored: `decide_egress_posture`'s generic, capability-aware decision applies
  directly (fails closed here too if this host cannot deliver the cage — that check is now
  live for local_cli, not skipped).
* BROKER    -> still refused: a subscription CLI authenticates out-of-band and talks to its
  own vendor over its own TLS; the no-secret EgressBroker (a lease bound to one declared
  destination/method/effect) has nothing to mediate. Genuinely architecturally incoherent,
  not a missing feature — this did not change.

A plan with NO local_cli node is untouched by any of this (https/DENY nodes are unaffected).
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.egress_posture_policy import resolve_local_cli_egress_decision
from bounded_loops.graph.application.execution_policy import NetworkDestination, NetworkMode
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


def _node(
    *, transport: str | None, node_id: str = "agent", effects: frozenset[Effect] = frozenset({Effect.WORKSPACE_WRITE}),
) -> PlannedNode:
    return PlannedNode(
        node_id=node_id, kind="local_cli" if transport else "research_claim", package_digest=None,
        binding_id="binding-1" if transport else None,
        required_effects=effects, isolation=IsolationLevel.PROCESS_RESTRICTED,
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


# ── ALLOWLIST is now honored for a plan with a local_cli node ──────────────────


def test_allowlist_with_cage_available_succeeds_for_a_local_cli_plan(tmp_path):
    # A network effect is required (see the effect-floor tests below) — mirrors the SAME
    # NETWORK_EFFECTS floor validate_execution_envelope already applies to https.
    plan = _plan(_node(transport="local_cli", effects=frozenset({Effect.EXTERNAL_WRITE})))
    decision = resolve_local_cli_egress_decision(
        plan,
        environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ),
        capabilities=_SEATBELT_WITH_PROXY,
    )
    assert decision.network_mode is NetworkMode.ALLOWLIST
    assert decision.network_destinations == (NetworkDestination(hostname="api.anthropic.com", port=443),)
    assert decision.requires_broker is False


def test_allowlist_without_the_cage_still_fails_closed_for_a_local_cli_plan(tmp_path):
    # decide_egress_posture's own generic host-capability check now applies live to local_cli
    # too — the refusal must name OPEN as the danger it refuses to silently fall back to.
    plan = _plan(_node(transport="local_cli", effects=frozenset({Effect.EXTERNAL_WRITE})))
    with pytest.raises(GraphValidationError, match="OPEN"):
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
                BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_NO_CAGE,
        )


# ── FIX 3 (Grok M4): a clear preflight message for the effect-floor requirement ─


def test_allowlist_without_a_network_effect_fails_closed_with_an_actionable_message(tmp_path):
    # A local_cli node declaring ONLY workspace_write cannot be ALLOWLIST-eligible —
    # validate_execution_envelope would refuse it anyway (mid-run, cryptically). Surface this
    # at PREFLIGHT instead, with a message that names the actual fix (declare a network effect,
    # or select OPEN), even when the cage IS available (this is not a capability problem).
    plan = _plan(_node(transport="local_cli", effects=frozenset({Effect.WORKSPACE_WRITE})))
    with pytest.raises(GraphValidationError, match="do not declare a network effect"):
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
                BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_SEATBELT_WITH_PROXY,
        )


def test_allowlist_effect_floor_message_names_the_node_and_the_fix(tmp_path):
    plan = _plan(_node(transport="local_cli", node_id="agent-7", effects=frozenset({Effect.WORKSPACE_WRITE})))
    try:
        resolve_local_cli_egress_decision(
            plan,
            environ=_env(
                BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
                BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
            ),
            capabilities=_SEATBELT_WITH_PROXY,
        )
        pytest.fail("expected GraphValidationError")
    except GraphValidationError as exc:
        message = str(exc)
        assert "agent-7" in message
        assert "external_write" in message or "financial" in message or "irreversible" in message
        assert "OPEN" in message  # names the alternative fix too


def test_allowlist_effect_floor_check_does_not_fire_under_open_posture(tmp_path):
    # The effect-floor requirement is specific to ALLOWLIST; a workspace_write-only node must
    # keep working unchanged under the (default) OPEN posture.
    plan = _plan(_node(transport="local_cli", effects=frozenset({Effect.WORKSPACE_WRITE})))
    decision = resolve_local_cli_egress_decision(
        plan, environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")), capabilities=_NO_CAGE,
    )
    assert decision.network_mode is NetworkMode.OPEN


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
