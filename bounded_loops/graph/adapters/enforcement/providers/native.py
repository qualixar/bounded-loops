"""Native OS-sandbox provider — macOS Seatbelt / Linux unshare.

Linux bubblewrap was removed in 0.7.0. It was selected here whenever `bwrap` was on PATH,
and sandboxed execution then failed to promote the node's declared output to the workspace —
so a capability the engine advertised could not complete a run. It had never been exercised
because continuous integration had no `bwrap` installed and always took the "this host offers
only Docker" refusal, and a refusal is a passing outcome. Installing the binary so the gate
could run is what exposed it. Rather than ship a documented defect, the claim is withdrawn:
Linux now refuses at preflight instead of selecting a mechanism it cannot honour. The
argv builder and the mechanism enum member went with it; both are recoverable from `v0.6.10`.

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
            # RC-LOCKDOWN: authorized egress is caged by denying ALL network except the loopback
            # egress proxy. Requires CONTAINER_RESTRICTED + Seatbelt (the only mechanism that can
            # express loopback-only egress today) AND an available egress proxy — the SAME gate the
            # capability matrix applies, so the two never disagree (dual-audit D1). Else fail closed.
            if tier is IsolationLevel.CONTAINER_RESTRICTED and caps.seatbelt and caps.egress_proxy:
                return SandboxMechanism.SEATBELT
            return None
        open_network = network_mode is NetworkMode.OPEN
        if tier is IsolationLevel.WORKSPACE_ONLY:
            return SandboxMechanism.NONE
        if tier is IsolationLevel.PROCESS_RESTRICTED:
            if not caps.process_groups:
                return None
            if caps.seatbelt:
                return SandboxMechanism.SEATBELT
            # `unshare -n` can ONLY create an isolated (empty) net namespace — it cannot
            # honor OPEN, so it is not a valid mechanism for a network-open node.
            if caps.net_namespace and not open_network:
                return SandboxMechanism.UNSHARE_NET
            return SandboxMechanism.NONE
        if tier is IsolationLevel.CONTAINER_RESTRICTED:
            if caps.seatbelt:
                return SandboxMechanism.SEATBELT
            return None  # no native container-grade mechanism (container provider may still)
        return None  # customer_managed_worker is not a native tier

    def _controls(self, mechanism: SandboxMechanism, network_mode: NetworkMode) -> EnforcedControls:
        pid = Control.ENFORCED if self._caps.process_groups else Control.NOT_ENFORCED
        # Under OPEN the network is deliberately NOT firewalled — report it honestly so a
        # receipt never overclaims network containment for a trusted-local connector.
        net_open = network_mode is NetworkMode.OPEN
        net = Control.NOT_ENFORCED if net_open else Control.ENFORCED
        if mechanism is SandboxMechanism.SEATBELT:
            allowlist = network_mode is NetworkMode.ALLOWLIST
            # Under ALLOWLIST the Seatbelt profile denies all egress EXCEPT the loopback proxy, so BOTH
            # net (no destination-blind egress) AND egress (authorized-egress proxy in force) are ENFORCED.
            egress = Control.ENFORCED if allowlist else Control.NOT_ENFORCED
            if allowlist:
                note = ("Seatbelt: egress DENIED except the loopback egress proxy (destination-allowlisted); "
                        "writes confined to workspace/HOME/TMPDIR; reads not confined")
            elif net_open:
                note = ("Seatbelt: outbound network OPEN (trusted-local); "
                        "writes confined to workspace/HOME/TMPDIR; reads not confined")
            else:
                note = "Seatbelt: outbound network denied; writes confined to workspace/HOME/TMPDIR; reads not confined"
            return EnforcedControls(
                net=net, fs_write=Control.ENFORCED, fs_read=Control.NOT_ENFORCED,
                pid=pid, user=Control.NOT_ENFORCED, kernel=Control.NOT_ENFORCED, egress=egress,
                notes=(note,),
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
        return Availability(True, "", self._controls(mechanism, network_mode))

    def build_launch(
        self,
        *,
        inner_argv: Sequence[str],
        workspace: Path,
        home: Path,
        tmpdir: Path,
        tier: IsolationLevel,
        network_mode: NetworkMode,
        egress_proxy_port: int | None = None,
    ) -> LaunchSpec:
        mechanism = self._mechanism(tier, network_mode)
        if mechanism is None:
            raise ValueError(f"native provider cannot launch {tier.value}/{network_mode.value} here")
        argv = wrap_argv(
            mechanism, inner_argv=inner_argv, workspace=workspace, home=home, tmpdir=tmpdir,
            network_mode=network_mode, egress_proxy_port=egress_proxy_port,
        )
        return LaunchSpec(kind="local", argv=tuple(argv))
