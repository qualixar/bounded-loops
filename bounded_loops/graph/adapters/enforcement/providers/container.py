"""Container isolation provider — hardened local Docker (E3, ADR-12 D1).

Reuses the E2.2 hardened ``docker_argv`` (digest-pinned image, ``--network none``,
``--cap-drop ALL``, ``--security-opt no-new-privileges``, ``--read-only`` rootfs,
cpu/memory/pids limits, non-root ``--user``). It is a LOCAL provider
(``LaunchSpec.kind == "local"``): a container SHARES the host kernel, so it
honestly delivers ``container_restricted`` but NOT ``customer_managed_worker``
(own-kernel isolation — that is the microvm / openshell providers' job), and it
never masquerades as a native cheap tier.

Declines fail-closed when there is no reachable Docker daemon, no digest-pinned
image configured, or authorized (allowlist) egress is requested (the egress proxy
is C1). This is a real adapter, not a stub: a host with Docker + a pinned image
selects it and runs a genuinely hardened container.
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
from bounded_loops.graph.adapters.enforcement.sandbox import docker_argv, is_digest_pinned
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel


class ContainerProvider:
    provider_id = "container"

    def __init__(
        self,
        capabilities: PlatformCapabilities,
        *,
        image: str | None = None,
        cpus: str = "1.0",
        memory: str = "1g",
        pids_limit: str = "256",
    ) -> None:
        self._caps = capabilities
        self._image = image
        self._cpus = cpus
        self._memory = memory
        self._pids_limit = pids_limit

    def _controls(self, network_mode: NetworkMode) -> EnforcedControls:
        return EnforcedControls(
            net=Control.ENFORCED if network_mode is NetworkMode.DENY else Control.NOT_ENFORCED,
            fs_write=Control.ENFORCED,
            fs_read=Control.ENFORCED,
            pid=Control.ENFORCED,
            user=Control.ENFORCED,
            kernel=Control.NOT_ENFORCED,
            egress=Control.NOT_ENFORCED,
            notes=(
                "hardened docker: --network none, --cap-drop ALL, no-new-privileges, "
                "--read-only rootfs, --pids-limit, non-root --user; SHARED host kernel (not a microVM)",
            ),
        )

    def probe(
        self, *, tier: IsolationLevel, network_mode: NetworkMode, workspace: Path | None = None,
    ) -> Availability:
        if network_mode is NetworkMode.ALLOWLIST:
            return Availability(
                False,
                "container: authorized egress requires the C1 egress broker (not yet available)",
                EnforcedControls(),
            )
        if tier is not IsolationLevel.CONTAINER_RESTRICTED:
            # workspace_only / process_restricted are the native provider's cheaper
            # floor; customer_managed_worker needs own-kernel isolation. A
            # shared-kernel container is honestly only a container_restricted tool.
            return Availability(
                False, f"container delivers container_restricted only, not {tier.value}", EnforcedControls(),
            )
        if not self._caps.docker_available:
            return Availability(False, "container: no reachable Docker daemon on this host", EnforcedControls())
        if not is_digest_pinned(self._image):
            return Availability(
                False,
                "container: no digest-pinned image configured (name@sha256:<64 hex>)",
                EnforcedControls(),
            )
        return Availability(True, "", self._controls(network_mode))

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
        if network_mode is NetworkMode.ALLOWLIST:
            raise ValueError("container cannot open authorized egress yet")
        if not is_digest_pinned(self._image):
            raise ValueError("container provider requires a digest-pinned image (name@sha256:<64 hex>)")
        assert self._image is not None  # narrowed by is_digest_pinned
        argv = docker_argv(
            image=self._image,
            inner_argv=inner_argv,
            workspace=workspace,
            deny_network=network_mode is NetworkMode.DENY,
            cpus=self._cpus,
            memory=self._memory,
            pids_limit=self._pids_limit,
        )
        return LaunchSpec(kind="local", argv=tuple(argv))
