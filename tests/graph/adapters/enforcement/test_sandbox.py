"""E2.2 — native sandbox mechanism selection + argv/profile builders.

Selection is the honesty core: it decouples "container-grade" isolation from
Docker (Seatbelt satisfies it too) yet still fails closed when no
mechanism can deliver the required tier, and it keeps authorized egress closed
until an egress proxy exists. Injected capabilities keep every case
deterministic on any platform.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.sandbox import (
    SandboxMechanism,
    build_seatbelt_profile,
    docker_argv,
    seatbelt_argv,
    unshare_net_argv,
    wrap_argv,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

_IMG = "example/tool@sha256:" + "a" * 64


def _caps(**over) -> PlatformCapabilities:
    base = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True)
    return replace(base, **over)


# ── selection matrix ───────────────────────────────────────────────────────────

def test_container_grade_prefers_native_over_docker():
    seatbelt = _caps(platform="darwin", seatbelt=True, docker_available=True)
    mech, _ = seatbelt.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)
    assert mech is SandboxMechanism.SEATBELT
    # Linux has no container-grade NATIVE mechanism since 0.7.0 removed bubblewrap, so a
    # Linux host with Docker gets Docker — the preference is real, not merely stated, and it
    # can only be exercised where a native mechanism exists at all.
    linux = _caps(docker_available=True, net_namespace=True)
    mech, _ = linux.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)
    assert mech is SandboxMechanism.DOCKER


def test_container_grade_falls_back_to_docker_then_fails_closed():
    docker_only = _caps(docker_available=True)
    mech, _ = docker_only.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)
    assert mech is SandboxMechanism.DOCKER
    bare = _caps()
    mech, reason = bare.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)
    assert mech is None and "container_restricted requires" in reason


def test_container_restricted_works_without_docker_via_seatbelt():
    """The headline: no Docker daemon, still container-grade via Seatbelt."""
    caps = _caps(platform="darwin", seatbelt=True, docker_available=False)
    ok, _ = caps.can_enforce(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)
    assert ok


def test_process_restricted_strengthens_with_native_but_never_fails_on_network():
    native = _caps(platform="darwin", seatbelt=True)
    mech, _ = native.select_mechanism(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.DENY)
    assert mech is SandboxMechanism.SEATBELT  # OS-enforces the deny
    plain = _caps()
    mech, reason = plain.select_mechanism(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.DENY)
    assert mech is SandboxMechanism.NONE and reason == ""  # floor, not fail-closed


def test_unshare_is_a_process_restricted_net_fallback_only():
    caps = _caps(net_namespace=True)
    mech, _ = caps.select_mechanism(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.DENY)
    assert mech is SandboxMechanism.UNSHARE_NET
    # unshare alone is NOT container-grade (no filesystem confinement).
    mech, _ = caps.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)
    assert mech is None


def test_allowlist_egress_stays_closed_until_a_seatbelt_cage_exists():
    # No egress proxy → fail closed.
    caps = _caps(platform="darwin", seatbelt=True, docker_available=True, egress_proxy=False)
    mech, reason = caps.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.ALLOWLIST)
    assert mech is None and "egress proxy" in reason
    # Egress proxy present but no Seatbelt cage (docker-only) → still fail closed (Seatbelt-only today).
    no_cage = _caps(platform="linux", docker_available=True, egress_proxy=True)
    mech, reason = no_cage.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.ALLOWLIST)
    assert mech is None and "Seatbelt" in reason
    # Egress proxy + Seatbelt → the loopback-only egress cage.
    ok = _caps(platform="darwin", seatbelt=True, egress_proxy=True)
    mech, _ = ok.select_mechanism(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.ALLOWLIST)
    assert mech is SandboxMechanism.SEATBELT


# ── honest published controls ────────────────────────────────────────────────

def test_enforced_controls_name_the_actual_mechanism():
    seatbelt = _caps(platform="darwin", seatbelt=True)
    controls = seatbelt.enforced_controls(IsolationLevel.CONTAINER_RESTRICTED)
    assert any("Seatbelt" in c for c in controls)
    assert any("reads not confined" in c for c in controls)  # discloses the limitation
    unshare = _caps(net_namespace=True)
    controls = unshare.enforced_controls(IsolationLevel.PROCESS_RESTRICTED)
    assert any("unshare" in c for c in controls)
    assert any("NOT confined" in c for c in controls)  # discloses the limitation


# ── argv / profile builders ──────────────────────────────────────────────────

def test_seatbelt_profile_denies_network_and_confines_writes(tmp_path):
    profile = build_seatbelt_profile(writable=[tmp_path / "outputs"], deny_network=True)
    assert "(deny network*)" in profile
    assert '(deny file-write* (subpath "/"))' in profile
    assert "outputs" in profile
    argv = seatbelt_argv(profile, ["/bin/echo", "hi"])
    assert argv[0] == "/usr/bin/sandbox-exec" and argv[-2:] == ["/bin/echo", "hi"]


def test_seatbelt_profile_can_leave_network_open_for_lower_tiers(tmp_path):
    profile = build_seatbelt_profile(writable=[tmp_path], deny_network=False)
    assert "(deny network*)" not in profile


def test_unshare_net_argv_shape():
    assert unshare_net_argv(["/bin/true"]) == ["unshare", "-n", "--", "/bin/true"]


def test_docker_argv_requires_digest_and_limits_cpu(tmp_path):
    argv = docker_argv(image=_IMG, inner_argv=["run"], workspace=tmp_path, deny_network=True)
    assert "--cpus" in argv  # BL-002: cpu limit present
    assert argv[argv.index("--network") + 1] == "none"
    # digest-pinned bind uses --mount (no colon-delimited -v parsing to hijack).
    assert any(a.startswith("type=bind,source=") and a.endswith(",target=/workspace") for a in argv)
    for bad in ("example/tool:latest", "example/tool@sha256:zzz", "example/tool@sha256:" + "a" * 12):
        with pytest.raises(ValueError):
            docker_argv(image=bad, inner_argv=["run"], workspace=tmp_path, deny_network=True)


def test_docker_argv_rejects_flag_like_image(tmp_path):
    # A leading '-' would be parsed by docker as an option (e.g. --privileged).
    with pytest.raises(ValueError):
        docker_argv(image="-v/etc@sha256:" + "a" * 64, inner_argv=["run"], workspace=tmp_path, deny_network=True)


def test_wrap_argv_refuses_allowlist_egress(tmp_path):
    # Latent fail-open guard: never build a network-open wrapper.
    with pytest.raises(ValueError):
        wrap_argv(
            SandboxMechanism.SEATBELT, inner_argv=["/bin/true"], workspace=tmp_path,
            home=tmp_path, tmpdir=tmp_path, network_mode=NetworkMode.ALLOWLIST,
        )


def test_seatbelt_profile_confines_dev_writes(tmp_path):
    profile = build_seatbelt_profile(writable=[tmp_path], deny_network=True)
    assert '(subpath "/dev")' not in profile  # never all of /dev
    assert '(literal "/dev/null")' in profile


def test_wrap_argv_none_is_passthrough_and_dispatch(tmp_path):
    inner = ["/usr/bin/python3", "-c", "pass"]
    passthrough = wrap_argv(
        SandboxMechanism.NONE, inner_argv=inner, workspace=tmp_path, home=tmp_path,
        tmpdir=tmp_path, network_mode=NetworkMode.DENY,
    )
    assert passthrough == inner
    wrapped = wrap_argv(
        SandboxMechanism.SEATBELT, inner_argv=inner, workspace=tmp_path, home=tmp_path,
        tmpdir=tmp_path, network_mode=NetworkMode.DENY,
    )
    assert wrapped[0] == "/usr/bin/sandbox-exec"


def test_canonical_rejects_quotes_in_paths(tmp_path):
    evil = tmp_path / 'a"b'
    with pytest.raises(ValueError):
        build_seatbelt_profile(writable=[evil], deny_network=True)


def test_bubblewrap_is_not_a_mechanism_this_release_offers():
    """The 0.7.0 removal, pinned.

    bubblewrap was selected whenever `bwrap` was on PATH and then did not promote the node's
    declared output to the workspace — a capability the engine advertised and could not
    complete. It had never been exercised because CI had no `bwrap` and always took the
    "this host offers only Docker" refusal, and a refusal is a passing outcome.

    Reintroducing it must be a deliberate act that fails this test, not a merge that quietly
    restores a mechanism whose failure mode is a run that reports success with no output.
    """
    assert not hasattr(SandboxMechanism, "BUBBLEWRAP")
    assert "bubblewrap" not in {mechanism.value for mechanism in SandboxMechanism}

    # And no capability flag can bring it back: the field itself is gone, so a caller that
    # tries to assert bwrap availability gets a TypeError rather than a silent no-op.
    with pytest.raises(TypeError):
        _caps(bubblewrap=True)
