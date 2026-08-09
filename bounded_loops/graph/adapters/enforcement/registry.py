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
from bounded_loops.graph.adapters.enforcement.providers.host_managed import HostManagedProvider
from bounded_loops.graph.adapters.enforcement.providers.native import NativeProvider
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


def default_registry(capabilities: PlatformCapabilities | None = None) -> IsolationProviderRegistry:
    """Precedence per ADR-12: host_managed (probe-backed) → native.

    container / microvm / openshell providers are added by the caller (slice 2)
    via a registry constructed with the full provider list.
    """
    caps = capabilities if capabilities is not None else probe_platform()
    return IsolationProviderRegistry([HostManagedProvider(), NativeProvider(caps)])
