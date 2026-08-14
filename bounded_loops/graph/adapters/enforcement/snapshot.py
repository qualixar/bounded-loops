"""Measure a host's isolation capability into the shape the capability report consumes.

This is the adapter half of the split the import-graph test requires: probing is concrete and
platform-specific, so it lives here, while `capability_report` stays pure application logic that
formats facts it is handed. An entry point (the `bl` CLI, the MCP server, the UI) calls
`platform_snapshot()` and passes the result inward.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.enforcement.capabilities import (
    PlatformCapabilities,
    probe_platform,
)
from bounded_loops.graph.application.capability_report import (
    IsolationFact,
    PlatformSnapshot,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

# The most capable host that can exist: a Linux box with a container runtime, POSIX process
# groups, and rlimits. A tier THIS cannot deliver is unavailable everywhere — which is a
# different and far more useful statement than "unavailable on your laptop", and the only way to
# answer `available_anywhere` without asking every machine in the world.
_MAXIMALLY_CAPABLE = PlatformCapabilities(
    platform="linux", docker_available=True, process_groups=True, rlimits=True,
)


def platform_snapshot(
    *,
    capabilities: PlatformCapabilities | None = None,
) -> PlatformSnapshot:
    """Probe (or accept) a host's capabilities and reduce them to reportable facts.

    `capabilities` is injectable so tests never shell out and so a caller can ask what a
    different host would deliver. Omitted, this probes the real machine — which runs a container
    availability check with a timeout, so a caller on a hot path should cache the result rather
    than calling per request.

    Network mode is `DENY` for every tier check on purpose: it is the strictest posture, so a
    tier reported as deliverable here is deliverable for the most demanding node that names it.
    """
    probed = capabilities if capabilities is not None else probe_platform()

    facts = []
    for level in IsolationLevel:
        deliverable, reason = probed.can_enforce(level, NetworkMode.DENY)
        anywhere, _ = _MAXIMALLY_CAPABLE.can_enforce(level, NetworkMode.DENY)
        facts.append(
            IsolationFact(
                level=level.value,
                deliverable_here=deliverable,
                reason_if_not=reason or None,
                controls_enforced_here=(
                    tuple(probed.enforced_controls(level)) if deliverable else ()
                ),
                available_anywhere=anywhere,
            )
        )

    return PlatformSnapshot(
        platform=probed.platform,
        container_runtime_reachable=probed.docker_available,
        process_groups=probed.process_groups,
        rlimits=probed.rlimits,
        isolation=tuple(facts),
    )
