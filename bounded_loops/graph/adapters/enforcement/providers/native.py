"""Native OS-sandbox provider — macOS Seatbelt / Linux bubblewrap / unshare.

The zero-dependency floor for standalone hosts (a laptop or CI with no host
sandbox). It reuses the E2.2 mechanism builders (`wrap_argv`) and never uses
Docker — that is the container provider's job — so provider precedence is
honest (native tries the OS-native path first; container is a distinct choice).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.provider import (
    Availability,
    Control,
    EnforcedControls,
    LaunchSpec,
)
from bounded_loops.graph.adapters.enforcement.sandbox import SandboxMechanism, wrap_argv
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

_FLOOR = "workspace-scoped copy + scrubbed env + isolated HOME/TMPDIR"


class NativeProvider:
    provider_id = "native"

    def __init__(self, capabilities: PlatformCapabilities) -> None:
        self._caps = capabilities

    def _mechanism(self, tier: IsolationLevel, network_mode: NetworkMode) -> SandboxMechanism | None:
        caps = self._caps
        if network_mode is NetworkMode.ALLOWLIST:
            return None  # authorized egress needs a proxy — not native's job
        if tier is IsolationLevel.WORKSPACE_ONLY:
            return SandboxMechanism.NONE
        if tier is IsolationLevel.PROCESS_RESTRICTED:
            if not caps.process_groups:
                return None
            if caps.seatbelt:
                return SandboxMechanism.SEATBELT
            if caps.bubblewrap:
                return SandboxMechanism.BUBBLEWRAP
            if caps.net_namespace:
                return SandboxMechanism.UNSHARE_NET
            return SandboxMechanism.NONE
        if tier is IsolationLevel.CONTAINER_RESTRICTED:
            if caps.seatbelt:
                return SandboxMechanism.SEATBELT
            if caps.bubblewrap:
                return SandboxMechanism.BUBBLEWRAP
            return None  # no native container-grade mechanism (container provider may still)
        return None  # customer_managed_worker is not a native tier

    def _controls(self, mechanism: SandboxMechanism) -> EnforcedControls:
        pid = Control.ENFORCED if self._caps.process_groups else Control.NOT_ENFORCED
        if mechanism is SandboxMechanism.SEATBELT:
            return EnforcedControls(
                net=Control.ENFORCED, fs_write=Control.ENFORCED, fs_read=Control.NOT_ENFORCED,
                pid=pid, user=Control.NOT_ENFORCED, kernel=Control.NOT_ENFORCED, egress=Control.NOT_ENFORCED,
                notes=("Seatbelt: outbound network denied; writes confined to workspace/HOME/TMPDIR; reads not confined",),
            )
        if mechanism is SandboxMechanism.BUBBLEWRAP:
            return EnforcedControls(
                net=Control.ENFORCED, fs_write=Control.ENFORCED, fs_read=Control.NOT_ENFORCED,
                pid=Control.ENFORCED, user=Control.ENFORCED, kernel=Control.NOT_ENFORCED, egress=Control.NOT_ENFORCED,
                notes=("bubblewrap: isolated network namespace; read-only rootfs + rw workspace",),
            )
        if mechanism is SandboxMechanism.UNSHARE_NET:
            return EnforcedControls(
                net=Control.ENFORCED, fs_write=Control.NOT_ENFORCED, fs_read=Control.NOT_ENFORCED,
                pid=pid, user=Control.NOT_ENFORCED, kernel=Control.NOT_ENFORCED, egress=Control.NOT_ENFORCED,
                notes=("unshare -n: isolated network namespace; filesystem not confined beyond the workspace copy",),
            )
        # NONE — the floor: no OS network firewall, writes not OS-jailed (scoped copy only)
        return EnforcedControls(
            net=Control.NOT_ENFORCED, fs_write=Control.NOT_ENFORCED, fs_read=Control.NOT_ENFORCED,
            pid=pid, user=Control.NOT_ENFORCED, kernel=Control.NOT_ENFORCED, egress=Control.NOT_ENFORCED,
            notes=(_FLOOR + "; network is NOT OS-firewalled at this level",),
        )

    def probe(
        self, *, tier: IsolationLevel, network_mode: NetworkMode, workspace: Path | None = None,
    ) -> Availability:
        mechanism = self._mechanism(tier, network_mode)
        if mechanism is None:
            return Availability(
                False,
                f"native cannot deliver {tier.value} with {network_mode.value} network on this host",
                EnforcedControls(),
            )
        return Availability(True, "", self._controls(mechanism))

    def build_launch(
        self,
        *,
        inner_argv: Sequence[str],
        workspace: Path,
        home: Path,
        tmpdir: Path,
        tier: IsolationLevel,
        network_mode: NetworkMode,
    ) -> LaunchSpec:
        mechanism = self._mechanism(tier, network_mode)
        if mechanism is None:
            raise ValueError(f"native provider cannot launch {tier.value}/{network_mode.value} here")
        argv = wrap_argv(
            mechanism, inner_argv=inner_argv, workspace=workspace, home=home, tmpdir=tmpdir,
            network_mode=network_mode,
        )
        return LaunchSpec(kind="local", argv=tuple(argv))
