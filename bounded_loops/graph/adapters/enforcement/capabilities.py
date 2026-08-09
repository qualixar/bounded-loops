"""Honest platform capability matrix for execution isolation.

Each isolation level publishes the controls it *actually* applies on a given
host and the limitations it does not hide. The enforcer refuses (fail-closed)
any level whose required controls this host cannot deliver, rather than
pretending to isolate.

Crucially, isolation is decoupled from Docker. ``container_restricted`` means
"OS-enforced network denial + filesystem write-confinement", and that guarantee
is deliverable by a native sandbox (macOS Seatbelt or Linux bubblewrap) just as
well as by Docker. The matrix selects whichever mechanism the host can truly
provide and names it in the published controls, so a receipt never claims a
"container" when a Seatbelt profile did the work. Authorized (allowlist) egress
still additionally requires an egress proxy (DNS/redirect/private-IP denial),
which is not built yet, so those nodes fail closed regardless of mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
import sys

from bounded_loops.graph.adapters.enforcement.sandbox import SEATBELT_BINARY, SandboxMechanism
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel


@dataclass(frozen=True)
class PlatformCapabilities:
    """What isolation this host can truthfully deliver."""

    platform: str
    docker_available: bool
    process_groups: bool
    rlimits: bool
    seatbelt: bool = False  # macOS sandbox-exec (Seatbelt) present
    bubblewrap: bool = False  # Linux bubblewrap (bwrap) present
    net_namespace: bool = False  # Linux `unshare -n` usable (network-namespace fallback)
    egress_proxy: bool = False

    # ── mechanism selection ──────────────────────────────────────────────────

    def _container_grade_mechanism(self) -> SandboxMechanism | None:
        """A mechanism that OS-enforces network denial AND write-confinement.

        Native sandboxes are preferred because they need neither a daemon nor a
        prebuilt image for a local command node; Docker is the last resort.
        """
        if self.seatbelt:
            return SandboxMechanism.SEATBELT
        if self.bubblewrap:
            return SandboxMechanism.BUBBLEWRAP
        if self.docker_available:
            return SandboxMechanism.DOCKER
        return None

    def _net_denial_wrap(self) -> SandboxMechanism | None:
        """A mechanism that can OS-enforce network denial for a process node."""
        if self.seatbelt:
            return SandboxMechanism.SEATBELT
        if self.bubblewrap:
            return SandboxMechanism.BUBBLEWRAP
        if self.net_namespace:
            return SandboxMechanism.UNSHARE_NET
        return None

    def select_mechanism(
        self, level: IsolationLevel, network_mode: NetworkMode,
    ) -> tuple[SandboxMechanism | None, str]:
        """Return the mechanism to use for *level* + *network_mode* here, or
        ``(None, reason)`` when this host cannot honestly deliver it."""
        if network_mode is NetworkMode.ALLOWLIST:
            if level is not IsolationLevel.CONTAINER_RESTRICTED:
                return (None, "authorized egress requires container_restricted isolation")
            if not self.egress_proxy:
                return (
                    None,
                    "authorized egress requires a container egress proxy "
                    "(DNS / redirect / private-IP denial), which is not yet available",
                )
            mechanism = self._container_grade_mechanism()
            if mechanism is None:
                return (None, "authorized egress requires docker, bubblewrap, or sandbox-exec; none available")
            return (mechanism, "")
        if level is IsolationLevel.WORKSPACE_ONLY:
            return (SandboxMechanism.NONE, "")
        if level is IsolationLevel.PROCESS_RESTRICTED:
            if not self.process_groups:
                return (None, "process_restricted requires POSIX process-group control, unavailable here")
            wrap = self._net_denial_wrap()
            return (wrap if wrap is not None else SandboxMechanism.NONE, "")
        if level is IsolationLevel.CONTAINER_RESTRICTED:
            mechanism = self._container_grade_mechanism()
            if mechanism is None:
                return (None, "container_restricted requires docker, bubblewrap, or sandbox-exec; none available")
            return (mechanism, "")
        if level is IsolationLevel.CUSTOMER_MANAGED_WORKER:
            return (None, "no admitted customer-managed worker transport is available")
        return (None, f"unknown isolation level: {level!r}")

    def can_enforce(self, level: IsolationLevel, network_mode: NetworkMode) -> tuple[bool, str]:
        """Return (ok, reason-if-not) for delivering *level* + *network_mode* here."""
        mechanism, reason = self.select_mechanism(level, network_mode)
        return (mechanism is not None, reason)

    # ── honest published controls ────────────────────────────────────────────

    def enforced_controls(
        self, level: IsolationLevel, mechanism: SandboxMechanism | None = None,
    ) -> tuple[str, ...]:
        """The honest, published list of controls applied at *level* here.

        When *mechanism* is omitted the mechanism that would be selected for a
        network-denied node at *level* is used, so the disclosure matches what
        this host would actually do.
        """
        if level is IsolationLevel.WORKSPACE_ONLY:
            return (
                "workspace-scoped copy",
                "scrubbed environment",
                "isolated HOME + private TMPDIR",
                "network is NOT OS-firewalled at this level (denied only by not provisioning credentials)",
            )
        if level is IsolationLevel.CUSTOMER_MANAGED_WORKER:
            return ("customer-managed worker (external attestation required)",)
        if mechanism is None:
            mechanism, _ = self.select_mechanism(level, NetworkMode.DENY)
        floor = ["workspace-scoped copy", "scrubbed environment", "isolated HOME + private TMPDIR"]
        if self.process_groups:
            floor.append("process-group deadline / terminate / kill")
        if self.rlimits:
            floor.append("CPU-time and open-file rlimits")
        if mechanism is SandboxMechanism.SEATBELT:
            return tuple(floor + [
                "sandbox-exec (Seatbelt): outbound network denied (deny network*)",
                "filesystem WRITES confined to workspace / HOME / TMPDIR (reads not confined)",
            ])
        if mechanism is SandboxMechanism.BUBBLEWRAP:
            return tuple(floor + [
                "bubblewrap: isolated network namespace (no external interfaces)",
                "read-only root filesystem; writes confined to workspace / HOME / TMPDIR",
            ])
        if mechanism is SandboxMechanism.UNSHARE_NET:
            return tuple(floor + [
                "unshare: isolated network namespace (network denied)",
                "filesystem writes NOT confined beyond the workspace copy",
            ])
        if mechanism is SandboxMechanism.DOCKER:
            return tuple(floor + [
                "container: network denied by default (--network none)",
                "capabilities dropped, no-new-privileges, read-only rootfs",
                "cpu / memory / pid limits, isolated filesystem and HOME",
            ])
        # SandboxMechanism.NONE (or unenforceable): the floor plus an honest
        # disclosure that no native network firewall is in effect.
        return tuple(floor + ["network is NOT OS-firewalled at this level (no native sandbox present)"])


def probe_platform(*, docker_timeout_s: float = 4.0) -> PlatformCapabilities:
    """Detect what this host can enforce. Called only in production paths, never
    in tests (tests inject a fixed PlatformCapabilities)."""
    return PlatformCapabilities(
        platform=sys.platform,
        docker_available=_docker_daemon_reachable(docker_timeout_s),
        process_groups=hasattr(os, "setsid") and hasattr(os, "killpg"),
        rlimits=_rlimits_available(),
        seatbelt=_seatbelt_available(),
        bubblewrap=shutil.which("bwrap") is not None,
        net_namespace=sys.platform.startswith("linux") and shutil.which("unshare") is not None,
        egress_proxy=False,
    )


def _seatbelt_available() -> bool:
    return sys.platform == "darwin" and os.access(SEATBELT_BINARY, os.X_OK)


def _rlimits_available() -> bool:
    try:
        import resource  # noqa: F401
    except Exception:
        return False
    return True


def _docker_daemon_reachable(timeout_s: float) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
