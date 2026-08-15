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

# A HYPOTHETICAL upper bound, deliberately not a real machine: no host has both Seatbelt (macOS)
# and bubblewrap (Linux). It is the union of every mechanism this engine knows how to use, which is
# what `available_anywhere` actually asks — "could ANY host deliver this tier", not "could one
# particular host deliver all of them at once". A tier THIS cannot deliver is unavailable
# everywhere, which is a far more useful statement than "unavailable on your laptop" and is the only
# way to answer the question without polling every machine in the world.
#
# It previously described itself as "the most capable host that can exist" while being a Linux box
# with `seatbelt=False` and `egress_proxy=False` — so the most capable host that can exist could not
# deliver authorized egress at all, since that path is Seatbelt-only. The under-claim never surfaced
# because `available_anywhere` was only ever evaluated under `DENY`.
_MAXIMALLY_CAPABLE = PlatformCapabilities(
    platform="linux", docker_available=True, process_groups=True, rlimits=True,
    seatbelt=True, bubblewrap=True, net_namespace=True, egress_proxy=True,
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

    **Two network modes are reported, because DENY is not the more demanding one.** `deliverable_here`
    answers the `DENY` posture; `deliverable_with_authorized_egress` answers `ALLOWLIST`.

    This used to say "`DENY` … is the strictest posture, so a tier reported as deliverable here is
    deliverable for the most demanding node that names it." That is false, and in the unsafe
    direction. `DENY` is the strictest network *policy*, but `ALLOWLIST` demands strictly more from
    the *host*: container-grade isolation AND a loopback egress proxy AND Seatbelt. A Linux box with
    Docker reports `container_restricted` deliverable under `DENY` while an authorized-egress node at
    that same tier is refused outright, because the egress cage is Seatbelt-only today. A caller
    planning an ALLOWLIST node from `deliverable_here` alone would have been told yes and then failed
    closed at run time.
    """
    probed = capabilities if capabilities is not None else probe_platform()

    facts = []
    for level in IsolationLevel:
        deliverable, reason = probed.can_enforce(level, NetworkMode.DENY)
        with_egress, egress_reason = probed.can_enforce(level, NetworkMode.ALLOWLIST)
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
                deliverable_with_authorized_egress=with_egress,
                authorized_egress_reason_if_not=egress_reason or None,
            )
        )

    return PlatformSnapshot(
        platform=probed.platform,
        container_runtime_reachable=probed.docker_available,
        process_groups=probed.process_groups,
        rlimits=probed.rlimits,
        isolation=tuple(facts),
    )
