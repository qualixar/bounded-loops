"""MicroVM isolation provider — E2B / Firecracker own-kernel workers (E3, ADR-12 D1).

A microVM gives each node its OWN guest kernel (Firecracker), so it can honestly
deliver ``customer_managed_worker`` (own-kernel isolation) on top of
``container_restricted``. It runs the node OFF this host through the universal
remote-exec seam, holding no backend client of its own: a ``RemoteExecTransport``
is injected (the hosted E2B transport is wired behind the C1 egress broker, so
this adapter never handles the E2B API key or opens a public socket).

It REQUIRES its transport to attest own-kernel isolation; a transport that only
shares the host kernel is refused (that is the container provider's job), so a
receipt can never over-claim the tier. With no admitted microVM transport — a
plain laptop — it declines fail-closed and the registry falls through to the
local providers. Real adapter, honest decline, not a stub.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.enforcement.providers.remote_exec import (
    RemoteExecLimits,
    RemoteExecTransport,
    RemoteIsolationProvider,
)


class MicroVMProvider(RemoteIsolationProvider):
    def __init__(
        self, *, transport: RemoteExecTransport | None = None, limits: RemoteExecLimits | None = None,
    ) -> None:
        super().__init__(provider_id="microvm", transport=transport, require_kernel=True, limits=limits)
