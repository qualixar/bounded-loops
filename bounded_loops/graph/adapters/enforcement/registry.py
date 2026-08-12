"""Fail-closed IsolationProvider registry (E3, ADR-12).

Selects the first provider (in precedence order) that is available AND whose
published per-dimension controls MEET the node's required isolation tier. If
none can, it raises — the engine never silently under-isolates. The chosen
provider's real controls travel in the selection so the receipt publishes what
was actually enforced.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities, probe_platform
from bounded_loops.graph.adapters.enforcement.provider import (
    IsolationProvider,
    ProviderSelection,
    controls_meet,
)
from bounded_loops.graph.adapters.enforcement.providers.container import ContainerProvider
from bounded_loops.graph.adapters.enforcement.providers.host_managed import HostManagedProvider
from bounded_loops.graph.adapters.enforcement.providers.microvm import MicroVMProvider
from bounded_loops.graph.adapters.enforcement.providers.native import NativeProvider
from bounded_loops.graph.adapters.enforcement.providers.openshell import OpenShellProvider
from bounded_loops.graph.adapters.enforcement.providers.remote_exec import RemoteExecTransport
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError


@dataclass(frozen=True)
class SelectionOutcome:
    provider: IsolationProvider
    selection: ProviderSelection


class IsolationProviderRegistry:
    def __init__(self, providers: Sequence[IsolationProvider]) -> None:
        if not providers:
            raise ValueError("registry requires at least one provider")
        self._providers = tuple(providers)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(p.provider_id for p in self._providers)

    def select(
        self, *, tier: IsolationLevel, network_mode: NetworkMode, workspace: Path | None = None,
    ) -> SelectionOutcome:
        reasons: list[str] = []
        for provider in self._providers:  # precedence = registration order
            avail = provider.probe(tier=tier, network_mode=network_mode, workspace=workspace)
            if not avail.available:
                reasons.append(f"{provider.provider_id}: {avail.reason}")
                continue
            ok, why = controls_meet(tier, network_mode, avail.controls)
            if not ok:
                reasons.append(f"{provider.provider_id}: {why}")
                continue
            return SelectionOutcome(provider, ProviderSelection(provider.provider_id, avail.controls))
        raise GraphValidationError(
            "isolation_provider",
            "/isolation",
            f"no isolation provider can deliver {tier.value} with {network_mode.value} network; "
            f"tried — {'; '.join(reasons) or 'no providers'}",
        )


def default_registry(
    capabilities: PlatformCapabilities | None = None,
    *,
    container_image: str | None = None,
    microvm_transport: RemoteExecTransport | None = None,
    openshell_transport: RemoteExecTransport | None = None,
    include_host_managed: bool = True,
) -> IsolationProviderRegistry:
    """Assemble the full fail-closed provider chain (ADR-12 D1).

    Precedence — first available provider whose controls meet the tier wins:
    ``host_managed`` (probe-backed ambient sandbox) → ``native`` (Seatbelt /
    bubblewrap floor) → ``container`` (hardened local Docker) → ``microvm``
    (E2B / Firecracker own-kernel) → ``openshell`` (NVIDIA NemoClaw).

    Cheap-local-first: for ``container_restricted`` a native Seatbelt / bwrap
    sandbox is chosen before spinning up Docker or a remote worker. The remote
    providers only win when the local ones cannot deliver the tier (e.g.
    ``customer_managed_worker``, which needs own-kernel isolation). Every remote
    provider whose backend is not configured declines fail-closed, so the default
    chain is safe to assemble in full on any host; a deployment supplies the
    image / transports to light up the extra tiers.

    ``include_host_managed`` gates the ambient-sandbox provider. It runs a live
    negative probe (a child that attempts an out-of-workspace write + a loopback
    socket) on every selection, so deferring to the host is a DELIBERATE
    deployment choice ("this engine runs inside Claude Code / Codex / OpenShell —
    do not double-sandbox"), not an always-on side effect. The node worker leaves
    it off by default and always applies its own isolation; a host-embedded
    deployment injects a registry with it enabled.
    """
    caps = capabilities if capabilities is not None else probe_platform()
    providers: list[IsolationProvider] = []
    if include_host_managed:
        providers.append(HostManagedProvider())
    providers.append(NativeProvider(caps))
    providers.append(ContainerProvider(caps, image=container_image))
    providers.append(MicroVMProvider(transport=microvm_transport))
    providers.append(OpenShellProvider(transport=openshell_transport))
    return IsolationProviderRegistry(providers)
