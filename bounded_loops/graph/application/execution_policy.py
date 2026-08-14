"""Fail-closed execution envelopes for graph node workers.

This module validates the authority a runner is allowed to receive.  A runner
adapter is responsible for turning the accepted envelope into actual process,
container, and network controls; it must not add authority beyond it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping, Protocol

from bounded_loops.graph.domain.authoring import Effect, IsolationLevel, NETWORK_EFFECTS
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_ISOLATION_RANK = {
    IsolationLevel.WORKSPACE_ONLY: 0,
    IsolationLevel.PROCESS_RESTRICTED: 1,
    IsolationLevel.CONTAINER_RESTRICTED: 2,
    IsolationLevel.CUSTOMER_MANAGED_WORKER: 3,
}
_EFFECT_MINIMUM = {
    Effect.READ_ONLY: IsolationLevel.WORKSPACE_ONLY,
    Effect.WORKSPACE_WRITE: IsolationLevel.WORKSPACE_ONLY,
    Effect.EXTERNAL_WRITE: IsolationLevel.CONTAINER_RESTRICTED,
    Effect.FINANCIAL: IsolationLevel.CONTAINER_RESTRICTED,
    Effect.IRREVERSIBLE: IsolationLevel.CONTAINER_RESTRICTED,
}
# The compiler-admitted transport of a local-CLI connector node (a sandboxed subprocess
# that runs the user's own authenticated agent CLI). It is the ONLY node that may open
# the network under NetworkMode.OPEN (a trusted-local posture), and only when a deployment
# selects OPEN — never an arbitrary node.
_LOCAL_CLI_TRANSPORT = "local_cli"

# Kinds whose SHIPPED worker runs in-process and opens no socket, so a declared network effect is a
# semantic marker (a publish records that an external write was authorised) rather than actual
# egress. Narrowed to ``publish`` alone: ``approval`` and ``join`` declare no effects at all, so they
# never reach the network branch, and listing them implied a broader carve-out than exists.
#
# **This is a TRUST ASSUMPTION about the shipped worker, not a proof.** The engine cannot show that
# an in-process worker refrains from opening a socket — Python can do it regardless of the envelope.
# The exemption is therefore paired with the ``binding_id is None`` guard below, so a publish node
# that IS bound to a transport still has to satisfy the allowlist, and it is recorded as an audit
# item rather than presented as enforcement.
#
# The reason this is narrow rather than convenient: ``NETWORK_EFFECTS`` is documented in
# ``authoring.py`` as the single source of truth for every network-posture decision across three
# layers. Exempting by KIND severs the declaration from the enforcement it exists to trigger — the
# day a real network-publishing worker ships, a publish node would silently keep the DENY envelope
# it was granted here.
_NONTRANSPORT_KIND_VALUES: frozenset[str] = frozenset({"publish"})


class NetworkMode(str, Enum):
    DENY = "deny"
    # OPEN: full outbound network, filesystem still confined. For an admitted local-CLI
    # connector on a trusted local host (the default "run the agent freely" posture, so
    # the agent reaches its model and its tools and real coding work completes).
    OPEN = "open"
    # ALLOWLIST: outbound only to declared destinations via a loopback proxy (an opt-in enterprise
    # egress firewall). Enforced at the sandbox layer by RC-LOCKDOWN on macOS Seatbelt: the process is
    # caged to a loopback egress proxy that admits only the declared destinations (SSRF/DNS-rebind
    # guarded). Where that cage is not expressible (no Seatbelt), it is refused fail-closed — the
    # network is never opened destination-blind under an allowlist promise.
    ALLOWLIST = "allowlist"


@dataclass(frozen=True)
class NetworkDestination:
    """One exact public network destination; wildcards and local targets deny."""

    hostname: str
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.hostname, str):
            raise GraphValidationError("network_destination", "/network_destinations", "destination must be an exact public hostname")
        normalized = self.hostname.lower()
        if not _HOSTNAME.fullmatch(normalized):
            raise GraphValidationError("network_destination", "/network_destinations", "destination must be an exact public hostname")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise GraphValidationError("network_destination", "/network_destinations", "destination port must be in range")
        object.__setattr__(self, "hostname", normalized)


@dataclass(frozen=True)
class ExecutionEnvelope:
    """The immutable capability set to apply to one node attempt."""

    isolation: IsolationLevel
    transport: str | None
    allowed_effects: frozenset[Effect]
    network_mode: NetworkMode
    network_destinations: tuple[NetworkDestination, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_effects", frozenset(self.allowed_effects))
        object.__setattr__(self, "network_destinations", tuple(self.network_destinations))


class ExecutionPolicyPort(Protocol):
    """Issues the one execution envelope a worker must honor for a node."""

    def authorize(self, *, plan: ExecutionPlan, node: PlannedNode) -> ExecutionEnvelope: ...


class ExecutionEnforcerPort(Protocol):
    """Applies an accepted envelope at the actual runner boundary.

    Implementations own DNS re-resolution, private/reserved-address rejection,
    redirect/proxy validation, and process/container/network controls. A policy
    validator alone is not an egress or isolation enforcement mechanism.
    """

    def enforce(self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope) -> None: ...


class ConfiguredExecutionPolicy:
    """Issue only controller-configured envelopes, then validate them exactly."""

    def __init__(self, envelopes: Mapping[str, ExecutionEnvelope]) -> None:
        self._envelopes = dict(envelopes)

    def authorize(self, *, plan: ExecutionPlan, node: PlannedNode) -> ExecutionEnvelope:
        try:
            envelope = self._envelopes[node.node_id]
        except KeyError as exc:
            raise GraphValidationError("execution_envelope", "/node_id", "node has no configured execution envelope") from exc
        return validate_execution_envelope(plan, node, envelope)


def validate_execution_envelope(
    plan: ExecutionPlan,
    node: PlannedNode,
    envelope: ExecutionEnvelope,
) -> ExecutionEnvelope:
    """Return an exact, least-authority envelope or deny before worker launch."""
    if not isinstance(envelope, ExecutionEnvelope):
        raise GraphValidationError("execution_envelope", "/envelope", "execution envelope is required")
    if envelope.allowed_effects != node.required_effects:
        raise GraphValidationError("execution_effects", "/envelope/allowed_effects", "envelope effects must exactly match planned effects")
    _validate_transport(plan, node, envelope.transport)
    _validate_isolation(node, envelope.isolation)
    _validate_network(node, envelope)
    return envelope


def _validate_transport(plan: ExecutionPlan, node: PlannedNode, transport: str | None) -> None:
    if node.binding_id is None:
        if transport is not None:
            raise GraphValidationError("execution_transport", "/envelope/transport", "an unbound node cannot receive transport authority")
        return
    matching = tuple(binding for binding in plan.connection_bindings if binding.binding_id == node.binding_id)
    if len(matching) != 1 or transport != matching[0].transport:
        raise GraphValidationError("execution_transport", "/envelope/transport", "envelope transport must match the compiled binding")


def _validate_isolation(node: PlannedNode, actual: IsolationLevel) -> None:
    if not isinstance(actual, IsolationLevel):
        raise GraphValidationError("execution_isolation", "/envelope/isolation", "envelope isolation is invalid")
    required = node.isolation
    for effect in node.required_effects:
        required = max(required, _EFFECT_MINIMUM[effect], key=_ISOLATION_RANK.__getitem__)
    if _ISOLATION_RANK[actual] < _ISOLATION_RANK[required]:
        raise GraphValidationError("execution_isolation", "/envelope/isolation", "envelope isolation is below the required policy floor")


def network_mode_for_node(node: PlannedNode) -> NetworkMode:
    """Return the ``NetworkMode`` a node requires based on its declared effects.

    A node that declares any network-bearing effect (``EXTERNAL_WRITE``,
    ``FINANCIAL``, or ``IRREVERSIBLE``) must be admitted on an ``ALLOWLIST``
    (explicit destination list) or ``OPEN`` (local-CLI trust posture) — never
    silently on ``DENY``.  All others get ``DENY``.

    Replaces ``enforcer._network_mode_for`` (ARCH-02): that function was an
    adapter-private 1-liner using only domain types, so the logic belongs in the
    application layer where it can be imported directly without crossing the
    application→adapter boundary.
    """
    return (
        NetworkMode.ALLOWLIST
        if (frozenset(node.required_effects) & NETWORK_EFFECTS)
        else NetworkMode.DENY
    )


def _validate_network(node: PlannedNode, envelope: ExecutionEnvelope) -> None:
    destinations = envelope.network_destinations
    if len(set(destinations)) != len(destinations):
        raise GraphValidationError("execution_network", "/envelope/network_destinations", "network destinations must be unique")
    if envelope.network_mode is NetworkMode.OPEN:
        # Trusted-local CLI connector: full outbound network, filesystem still confined.
        # Gated to the compiler-admitted `local_cli` transport (already checked against the
        # binding in `_validate_transport`), so ONLY an admitted local-CLI connector can open
        # the network, and only when the deployment selects OPEN — never an arbitrary node.
        if envelope.transport != _LOCAL_CLI_TRANSPORT:
            raise GraphValidationError("execution_network", "/envelope/network_mode", "open network is only for an admitted local-CLI connector node")
        if destinations:
            raise GraphValidationError("execution_network", "/envelope/network_destinations", "open network takes no destination allowlist")
        return
    needs_network = bool(node.required_effects & NETWORK_EFFECTS)
    if needs_network:
        # A publish node with NO binding is run by the in-process worker, which opens no socket, so
        # its declared network effect is an authorisation marker rather than egress. The
        # ``binding_id is None`` half of this is the part that is actually CHECKED: bind a publish
        # node to a transport and it must satisfy the allowlist like anything else, so the exemption
        # cannot follow the node into a deployment that gave it a real connection.
        if node.kind in _NONTRANSPORT_KIND_VALUES and node.binding_id is None:
            return
        if envelope.network_mode is not NetworkMode.ALLOWLIST or not destinations:
            raise GraphValidationError("execution_network", "/envelope/network_destinations", "network effects require a specific network allowlist")
        return
    if envelope.network_mode is not NetworkMode.DENY or destinations:
        raise GraphValidationError("execution_network", "/envelope/network_mode", "non-network effects require denied network access")
