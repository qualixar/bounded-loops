"""Fail-closed execution enforcer: refuse any node the platform cannot isolate.

This is the capability GATE the controller invokes before a worker runs. It
never launches a process itself; it decides whether the required isolation can
be delivered here and raises ``GraphValidationError`` if not. The concrete
process/container launch is performed by the sandboxed worker (E2.2) using the
same capability matrix, so the gate and the worker agree.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.enforcement.capabilities import (
    PlatformCapabilities,
    probe_platform,
)
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkMode
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_NETWORK_EFFECTS = frozenset({Effect.EXTERNAL_WRITE, Effect.FINANCIAL, Effect.IRREVERSIBLE})


def _network_mode_for(node: PlannedNode) -> NetworkMode:
    return NetworkMode.ALLOWLIST if (frozenset(node.required_effects) & _NETWORK_EFFECTS) else NetworkMode.DENY


class ExecutionEnforcer:
    """Applies the platform capability matrix at the runner boundary."""

    def __init__(self, capabilities: PlatformCapabilities) -> None:
        self._caps = capabilities

    @property
    def capabilities(self) -> PlatformCapabilities:
        return self._caps

    def enforce(self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope) -> None:
        ok, reason = self._caps.can_enforce(envelope.isolation, envelope.network_mode)
        if not ok:
            raise GraphValidationError(
                "execution_enforcement",
                f"/nodes/{node.node_id}",
                f"cannot enforce {envelope.isolation.value} isolation: {reason}",
            )


def build_enforcer(
    plan: ExecutionPlan,
    *,
    capabilities: PlatformCapabilities | None = None,
) -> ExecutionEnforcer:
    """Probe (or accept injected) capabilities and fail closed BEFORE a run if
    any node's required isolation cannot be enforced on this host."""
    caps = capabilities if capabilities is not None else probe_platform()
    for node in plan.nodes:
        ok, reason = caps.can_enforce(node.isolation, _network_mode_for(node))
        if not ok:
            raise GraphValidationError(
                "execution_enforcement",
                f"/nodes/{node.node_id}",
                f"cannot enforce {node.isolation.value} isolation: {reason}",
            )
    return ExecutionEnforcer(caps)
