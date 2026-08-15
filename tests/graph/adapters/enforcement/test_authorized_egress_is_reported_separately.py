"""DENY is not the harder posture, and the snapshot used to claim it was.

`platform_snapshot` evaluated every tier under `NetworkMode.DENY` and said, in its own docstring,
that "a tier reported as deliverable here is deliverable for the most demanding node that names
it". That is false in the unsafe direction.

DENY is the strictest network *policy*. ALLOWLIST demands strictly more of the *host*:

    container-grade isolation  AND  a loopback egress proxy  AND  Seatbelt

so a host can pass the DENY check and fail the ALLOWLIST one at the same tier. A caller planning an
authorized-egress node from `deliverable_here` alone was told yes and then failed closed at run
time. Found by the wave-1 Grok audit.

These tests inject capabilities rather than probing, so they assert the same facts on any machine.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.snapshot import (
    _MAXIMALLY_CAPABLE,
    platform_snapshot,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

#: A perfectly ordinary Linux CI box. Delivers container isolation; cannot build the egress cage.
_LINUX_WITH_DOCKER = PlatformCapabilities(
    platform="linux", docker_available=True, process_groups=True, rlimits=True,
)

#: A macOS host with the proxy available — the only shape that delivers authorized egress today.
_MACOS_WITH_PROXY = PlatformCapabilities(
    platform="darwin", docker_available=False, process_groups=True, rlimits=True,
    seatbelt=True, egress_proxy=True,
)


def _fact(capabilities: PlatformCapabilities, level: IsolationLevel):
    snapshot = platform_snapshot(capabilities=capabilities)
    return next(f for f in snapshot.isolation if f.level == level.value)


def test_a_linux_docker_host_delivers_the_tier_under_DENY_and_not_under_ALLOWLIST() -> None:
    """The exact case the old claim got wrong, and the reason one boolean could not carry both."""
    fact = _fact(_LINUX_WITH_DOCKER, IsolationLevel.CONTAINER_RESTRICTED)

    assert fact.deliverable_here is True, "Docker delivers container_restricted under DENY"
    assert fact.deliverable_with_authorized_egress is False, (
        "the egress cage is Seatbelt-only today, so this host cannot deliver an ALLOWLIST node at "
        "this tier — reporting only the DENY answer told the caller otherwise"
    )
    assert fact.authorized_egress_reason_if_not, (
        "a refusal must say why, or the caller cannot tell a missing capability from a bug"
    )


def test_a_host_that_can_build_the_cage_reports_both() -> None:
    """The control. Without it the assertion above would pass on an always-False field."""
    fact = _fact(_MACOS_WITH_PROXY, IsolationLevel.CONTAINER_RESTRICTED)

    assert fact.deliverable_here is True
    assert fact.deliverable_with_authorized_egress is True
    assert fact.authorized_egress_reason_if_not is None


def test_authorized_egress_is_refused_below_the_container_tier_everywhere() -> None:
    """Authorized egress requires container_restricted, whatever else the host has.

    Asserted against the most capable configuration there is, so it reads as a property of the
    design rather than of one under-equipped machine.
    """
    for level in (IsolationLevel.WORKSPACE_ONLY, IsolationLevel.PROCESS_RESTRICTED):
        fact = _fact(_MACOS_WITH_PROXY, level)
        assert fact.deliverable_with_authorized_egress is False, (
            f"{level.value} must never carry authorized egress"
        )


def test_the_hypothetical_upper_bound_can_actually_deliver_authorized_egress() -> None:
    """`_MAXIMALLY_CAPABLE` answers "could ANY host deliver this", so it must be a real bound.

    It was a Linux box with `seatbelt=False` and `egress_proxy=False` while describing itself as
    "the most capable host that can exist" — so the most capable host that can exist could not
    deliver authorized egress at all. The under-claim never surfaced because `available_anywhere`
    was only ever evaluated under DENY.

    It is deliberately not a real machine: no host has both Seatbelt (macOS) and bubblewrap
    (Linux). It is the union of mechanisms this engine knows, which is what the question asks.
    """
    deliverable, reason = _MAXIMALLY_CAPABLE.can_enforce(
        IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.ALLOWLIST
    )

    assert deliverable, f"the upper bound cannot deliver authorized egress: {reason}"


def test_every_tier_reports_both_postures() -> None:
    """No tier may leave the harder question unanswered — a missing answer reads as a no."""
    snapshot = platform_snapshot(capabilities=_LINUX_WITH_DOCKER)

    assert snapshot.isolation, "the snapshot reported no tiers at all"
    for fact in snapshot.isolation:
        assert isinstance(fact.deliverable_here, bool)
        assert isinstance(fact.deliverable_with_authorized_egress, bool)
