"""IsolationProvider port + honest per-dimension controls (E3, ADR-12).

The engine does not *own* a sandbox. It selects a PROVIDER that can deliver a
node's required isolation tier on this host, or fails closed. Every provider
publishes a per-dimension control matrix — never a bare label — and a dimension
a provider cannot *prove* is ``UNKNOWN`` so a node that requires it fails closed
rather than being silently under-isolated. ``host_managed`` additionally must
PROVE ambient confinement with a live negative probe before it may satisfy a
tier (it never trusts environment detection — that would be a downgrade attack).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel


class Control(str, Enum):
    ENFORCED = "enforced"
    NOT_ENFORCED = "not_enforced"
    UNKNOWN = "unknown"


# The isolation dimensions a receipt publishes for a run.
DIMENSIONS: tuple[str, ...] = ("net", "fs_write", "fs_read", "pid", "user", "kernel", "egress")


@dataclass(frozen=True)
class EnforcedControls:
    """Per-dimension truth about what a provider actually enforces here."""

    net: Control = Control.UNKNOWN
    fs_write: Control = Control.UNKNOWN
    fs_read: Control = Control.UNKNOWN
    pid: Control = Control.UNKNOWN
    user: Control = Control.UNKNOWN
    kernel: Control = Control.UNKNOWN
    egress: Control = Control.UNKNOWN
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        return {dim: getattr(self, dim).value for dim in DIMENSIONS}


@dataclass(frozen=True)
class LaunchSpec:
    """How the worker should launch the node under the selected provider.

    ``local`` runs ``argv`` via the controller-owned process lifecycle; ``remote``
    hands ``remote`` (a portable request payload) to a remote-sandbox transport.
    """

    kind: str  # "local" | "remote"
    argv: tuple[str, ...] = ()
    remote: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("local", "remote"):
            raise ValueError("LaunchSpec.kind must be 'local' or 'remote'")
        if self.kind == "local" and not self.argv:
            raise ValueError("local LaunchSpec requires argv")
        if self.kind == "remote" and self.remote is None:
            raise ValueError("remote LaunchSpec requires a remote payload")


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str
    controls: EnforcedControls


@dataclass(frozen=True)
class ProviderSelection:
    """The registry's fail-closed decision for one node."""

    provider_id: str
    controls: EnforcedControls


def required_dimensions(tier: IsolationLevel, network_mode: NetworkMode) -> tuple[str, ...]:
    """The dimensions that MUST be ENFORCED to honestly deliver *tier*.

    workspace_only is the floor (a scoped copy, no OS guarantee required);
    process_restricted requires OS process control; container_restricted requires
    OS network-denial + write-confinement; customer_managed_worker requires
    strong (own-kernel / attested) isolation. Authorized egress additionally
    requires an egress proxy.
    """
    if tier is IsolationLevel.WORKSPACE_ONLY:
        req: list[str] = []
    elif tier is IsolationLevel.PROCESS_RESTRICTED:
        req = ["pid"]
    elif tier is IsolationLevel.CONTAINER_RESTRICTED:
        req = ["net", "fs_write"]
    elif tier is IsolationLevel.CUSTOMER_MANAGED_WORKER:
        req = ["kernel"]
    else:  # pragma: no cover - defensive
        req = ["net", "fs_write", "kernel"]
    if network_mode is NetworkMode.ALLOWLIST:
        req.append("egress")
    return tuple(req)


def controls_meet(
    tier: IsolationLevel, network_mode: NetworkMode, controls: EnforcedControls,
) -> tuple[bool, str]:
    """Return (ok, reason) — every required dimension must be ENFORCED."""
    for dim in required_dimensions(tier, network_mode):
        status = getattr(controls, dim)
        if status is not Control.ENFORCED:
            return (False, f"required control '{dim}' is {status.value}")
    return (True, "")


class IsolationProvider(Protocol):
    """An adapter that can deliver an isolation tier on some host.

    Implementations must be honest: ``probe`` returns real availability + the
    per-dimension controls it would apply; ``build_launch`` returns the concrete
    launch the worker will run. A provider that needs an absent environment
    returns ``available=False`` with a clear reason — never a silent stub.
    """

    provider_id: str

    def probe(
        self, *, tier: IsolationLevel, network_mode: NetworkMode, workspace: Path | None = None,
    ) -> Availability: ...

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
        """Build the concrete launch. ``egress_proxy_port`` is the loopback port of the RC-LOCKDOWN
        egress proxy the worker has already started for a ``NetworkMode.ALLOWLIST`` node; a provider
        that OS-cages egress uses it to confine the process to that loopback endpoint."""
        ...
