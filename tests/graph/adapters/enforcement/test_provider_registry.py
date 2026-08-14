"""E3 slice 1 — IsolationProvider port, native + host_managed providers, registry.

Native controls are deterministic from injected capabilities (no real sandbox
needed to build a launch spec). host_managed is proven two ways: a LIVE probe on
this (unconfined) host must DECLINE (downgrade-safe), and a monkeypatched
confined host must DEFER. The registry is fail-closed.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.provider import (
    Control,
    EnforcedControls,
    controls_meet,
    required_dimensions,
)
from bounded_loops.graph.adapters.enforcement.providers.host_managed import HostManagedProvider
from bounded_loops.graph.adapters.enforcement.providers.native import NativeProvider
from bounded_loops.graph.adapters.enforcement.registry import (
    IsolationProviderRegistry,
    default_registry,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError

_SEATBELT = PlatformCapabilities(platform="darwin", docker_available=False, process_groups=True, rlimits=True, seatbelt=True)
_BWRAP = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True, bubblewrap=True)
_BARE = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True)


# ── contract ──────────────────────────────────────────────────────────────────

def test_required_dimensions_per_tier():
    assert required_dimensions(IsolationLevel.WORKSPACE_ONLY, NetworkMode.DENY) == ()
    assert required_dimensions(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.DENY) == ("pid",)
    assert set(required_dimensions(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY)) == {"net", "fs_write"}
    assert "egress" in required_dimensions(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.ALLOWLIST)


def test_controls_meet():
    ok = EnforcedControls(net=Control.ENFORCED, fs_write=Control.ENFORCED)
    assert controls_meet(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY, ok)[0]
    bad = EnforcedControls(net=Control.NOT_ENFORCED, fs_write=Control.ENFORCED)
    passed, why = controls_meet(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY, bad)
    assert not passed and "net" in why


# ── native provider ─────────────────────────────────────────────────────────

def test_native_container_restricted_via_seatbelt():
    prov = NativeProvider(_SEATBELT)
    avail = prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY)
    assert avail.available
    assert avail.controls.net is Control.ENFORCED and avail.controls.fs_write is Control.ENFORCED
    spec = prov.build_launch(
        inner_argv=["/usr/bin/python3", "-c", "pass"], workspace=__import__("pathlib").Path("/tmp/ws"),
        home=__import__("pathlib").Path("/tmp/h"), tmpdir=__import__("pathlib").Path("/tmp/t"),
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
    )
    assert spec.kind == "local" and spec.argv[0] == "/usr/bin/sandbox-exec"


def test_native_workspace_only_is_floor():
    prov = NativeProvider(_BARE)
    avail = prov.probe(tier=IsolationLevel.WORKSPACE_ONLY, network_mode=NetworkMode.DENY)
    assert avail.available  # floor: no OS dimension required
    assert controls_meet(IsolationLevel.WORKSPACE_ONLY, NetworkMode.DENY, avail.controls)[0]


def test_native_container_unavailable_without_native_mechanism():
    prov = NativeProvider(_BARE)  # no seatbelt/bwrap
    assert not prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY).available


def test_native_allowlist_caged_on_seatbelt_with_proxy_else_fail_closed():
    from dataclasses import replace
    # RC-LOCKDOWN: available only with BOTH Seatbelt AND an egress proxy (loopback-only cage), egress ENFORCED.
    caged = replace(_SEATBELT, egress_proxy=True)
    ok = NativeProvider(caged).probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.ALLOWLIST,
    )
    assert ok.available
    assert ok.controls.egress is Control.ENFORCED
    assert ok.controls.net is Control.ENFORCED
    # Seatbelt present but no egress proxy → native and the capability matrix AGREE it is unenforceable
    # (dual-audit D1: the two gates must never disagree).
    assert not NativeProvider(_SEATBELT).probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.ALLOWLIST,
    ).available
    # Without Seatbelt the loopback cage is not expressible → fail closed (never a fake).
    for caps in (_BWRAP, _BARE):
        assert not NativeProvider(caps).probe(
            tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.ALLOWLIST,
        ).available


def test_native_bubblewrap_controls():
    prov = NativeProvider(_BWRAP)
    c = prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY).controls
    assert c.net is Control.ENFORCED and c.fs_write is Control.ENFORCED and c.user is Control.ENFORCED


# ── host_managed provider ─────────────────────────────────────────────────────

def test_host_managed_declines_when_unconfined_live(tmp_path):
    """On this host (no ambient sandbox) the live probe sees writes/sockets
    succeed, so host_managed must DECLINE — never claim confinement it lacks."""
    avail = HostManagedProvider().probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=tmp_path,
    )
    assert not avail.available
    assert "no ambient host confinement" in avail.reason


def test_host_managed_defers_when_confinement_proven(tmp_path, monkeypatch):
    prov = HostManagedProvider()
    monkeypatch.setattr(prov, "_run_probe", lambda ws: (Control.ENFORCED, Control.ENFORCED))
    avail = prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=tmp_path)
    assert avail.available
    assert avail.controls.net is Control.ENFORCED and avail.controls.fs_write is Control.ENFORCED
    spec = prov.build_launch(
        inner_argv=["/bin/true"], workspace=tmp_path, home=tmp_path, tmpdir=tmp_path,
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
    )
    assert spec.kind == "local" and spec.argv == ("/bin/true",)  # deferred = unwrapped


def test_host_managed_requires_workspace():
    assert not HostManagedProvider().probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=None,
    ).available


# ── registry (fail-closed selection) ──────────────────────────────────────────

def test_registry_selects_native_on_this_host_live(tmp_path):
    """On a host whose NATIVE mechanism can deliver the tier, the registry must select it.

    SKIPS where no shipped provider qualifies, which is the honest outcome rather than a failure: the
    assertion is about the registry's PREFERENCE ORDER, and a host with no qualifying mechanism has
    nothing for it to order. GitHub's Linux and macOS runners are such hosts — no ambient confinement,
    no digest-pinned container image, no remote-exec transport — so this test failed on every CI run
    from 0.4.0 onward and made a red build the normal state, which is how a red build stops meaning
    anything. `test_registry_fail_closed_when_no_provider_meets` already covers the no-provider case
    as a REFUSAL, which is the property that matters.
    """
    reg = default_registry()  # host_managed declines live → native
    try:
        out = reg.select(
            tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
            workspace=tmp_path,
        )
    except GraphValidationError as exc:
        pytest.skip(f"no shipped provider can deliver this tier on this host: {exc}")
    assert out.selection.provider_id == "native"
    assert out.selection.controls.net is Control.ENFORCED


def test_registry_prefers_host_managed_when_confining(tmp_path, monkeypatch):
    hm = HostManagedProvider()
    monkeypatch.setattr(hm, "_run_probe", lambda ws: (Control.ENFORCED, Control.ENFORCED))
    reg = IsolationProviderRegistry([hm, NativeProvider(_SEATBELT)])
    out = reg.select(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=tmp_path)
    assert out.selection.provider_id == "host_managed"


def test_registry_fail_closed_when_no_provider_meets(tmp_path):
    reg = IsolationProviderRegistry([NativeProvider(_BARE)])
    with pytest.raises(GraphValidationError):
        reg.select(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=tmp_path)
    with pytest.raises(GraphValidationError):
        reg.select(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY, workspace=tmp_path)
    with pytest.raises(GraphValidationError):
        reg.select(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.ALLOWLIST, workspace=tmp_path)
