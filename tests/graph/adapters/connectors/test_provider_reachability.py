"""A shipped provider must be SELECTABLE, not merely present in a table.

E1 (task #63) was a provider claim with zero tests behind it: the smoke suite collected nothing
while the project advertised a five-CLI connector. The lesson generalises past that incident — a
profile in `CLI_PROFILES` proves a dict has a key, not that a graph node can bind it and reach an
attempt. Preflight admission is the gate that decides reachability, and until now nothing tested
it in either direction.

Both directions are here on purpose. Asserting only that a known provider is admitted would pass
if `unknown_local_cli_provider` had been stubbed to `return None`, which is precisely how a guard
rots into a no-op.
"""

from __future__ import annotations

import types

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.plan import ResolvedBinding
from bounded_loops.graph.graph_composition import unknown_local_cli_provider


def _plan_binding(provider_id: str):
    binding = ResolvedBinding(
        binding_id="b1", slot_id="model", connector_id="c", connector_version="1",
        connection_id="conn", admission_digest="sha256:" + "d" * 64,
        route_policy_digest="sha256:" + "e" * 64, provider_id=provider_id,
        model_target="m", region="local", fallback=False, transport="local_cli",
    )
    return types.SimpleNamespace(connection_bindings=(binding,))


def _node():
    return types.SimpleNamespace(
        node_id="agent", binding_id="b1", required_effects=frozenset({Effect.WORKSPACE_WRITE}),
        isolation=IsolationLevel.PROCESS_RESTRICTED, hard_deadline_ms=60000,
        transport="local_cli",
    )


@pytest.mark.parametrize("provider", sorted(CLI_PROFILES))
def test_every_shipped_provider_is_admitted_by_preflight(provider: str) -> None:
    """Parametrised over the shipped table, so a seventh provider is covered when it is added.

    This is the assertion that would have failed if `provider_id` were constrained by a closed
    enum somewhere in the schema or the compiler: the profile would exist and no node could ever
    select it. It is validated against the live profile mapping instead, which is why adding a
    profile is sufficient to make a provider reachable.
    """
    refusal = unknown_local_cli_provider(
        _plan_binding(provider), _node(), cli_profiles=CLI_PROFILES,
    )

    assert refusal is None, f"{provider} ships a profile but preflight refuses it: {refusal}"


def test_an_unknown_provider_is_refused_before_any_attempt_starts() -> None:
    """Proof the check above is not vacuous, and that the refusal names the remedy.

    Refusing here rather than at execute time is the point: an unknown provider fails every
    attempt identically, having already paid for every node upstream of it.
    """
    refusal = unknown_local_cli_provider(
        _plan_binding("not-a-real-cli"), _node(), cli_profiles=CLI_PROFILES,
    )

    assert refusal is not None, "an unknown provider must be refused"
    assert "not-a-real-cli" in refusal, "the refusal must name the offending provider"
    assert "provider catalog" in refusal, "the refusal must name the remedy"
    # The known set is listed so an operator can see what they could have bound instead.
    for shipped in CLI_PROFILES:
        assert shipped in refusal, f"the refusal should list {shipped} among known providers"
