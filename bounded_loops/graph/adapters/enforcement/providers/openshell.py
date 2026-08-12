"""OpenShell isolation provider — NVIDIA NemoClaw kernel-level OpenShell (E3, ADR-12 D1).

NemoClaw runs an agent runtime (Hermes / OpenClaw) inside a kernel-level
OpenShell on DGX / WSL, giving own-kernel isolation. Like ``microvm`` it runs the
node off this host through the universal remote-exec seam with an injected
``RemoteExecTransport`` (the NemoClaw OpenShell transport is wired behind the C1
egress broker — this adapter holds no client and no credential).

It requires own-kernel attestation and declines fail-closed when no OpenShell
transport is configured (e.g. on a plain laptop), so the registry falls through
to the local providers. Real adapter, honest decline, not a stub.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.enforcement.providers.remote_exec import (
    RemoteExecLimits,
    RemoteExecTransport,
    RemoteIsolationProvider,
)


class OpenShellProvider(RemoteIsolationProvider):
    def __init__(
        self, *, transport: RemoteExecTransport | None = None, limits: RemoteExecLimits | None = None,
    ) -> None:
        super().__init__(provider_id="openshell", transport=transport, require_kernel=True, limits=limits)
