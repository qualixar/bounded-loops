"""E3 slice 2 — container / microvm / openshell providers + universal remote-exec seam.

Every provider is exercised as a REAL adapter: it publishes honest per-dimension
controls, fails closed when its backend is absent, and builds a concrete launch.
No live Docker / E2B / NemoClaw is required — caps are injected and transports are
faked; the one live touch is a loopback socket to a *closed* port to prove the
reference transport declines cleanly (safe: 127.0.0.1 only).
"""

from __future__ import annotations

import json
import socket

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.provider import (
    Control,
    EnforcedControls,
    controls_meet,
)
from bounded_loops.graph.adapters.enforcement.providers.container import ContainerProvider
from bounded_loops.graph.adapters.enforcement.providers.microvm import MicroVMProvider
from bounded_loops.graph.adapters.enforcement.providers.openshell import OpenShellProvider
from bounded_loops.graph.adapters.enforcement.providers.remote_exec import (
    LoopbackExecTransport,
    RemoteExecError,
    RemoteExecLimits,
    RemoteExecRequest,
    RemoteExecResult,
    RemoteFile,
    RemoteIsolationProvider,
    build_remote_launch,
)
from bounded_loops.graph.adapters.enforcement.registry import default_registry
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError

_SEATBELT = PlatformCapabilities(platform="darwin", docker_available=False, process_groups=True, rlimits=True, seatbelt=True)
_DOCKER_LINUX = PlatformCapabilities(platform="linux", docker_available=True, process_groups=True, rlimits=True)
_BARE = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True)
_IMG = "registry.example.com/runner@sha256:" + "a" * 64


# ── fakes ─────────────────────────────────────────────────────────────────────

class _FakeTransport:
    """A RemoteExecTransport whose attestation is configurable per test."""

    def __init__(self, *, backend_id="fake-vm", available=True, reason="", kernel=Control.ENFORCED):
        self.backend_id = backend_id
        self._available = available
        self._reason = reason
        self._kernel = kernel

    def availability(self):
        return (self._available, self._reason)

    def attested_controls(self, *, tier, network_mode):
        e = Control.ENFORCED
        return EnforcedControls(net=e, fs_write=e, fs_read=e, pid=e, user=e, kernel=self._kernel, egress=Control.NOT_ENFORCED)

    def submit(self, request):  # pragma: no cover - not used in these tests
        raise NotImplementedError


class _FakeResp:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body if (n is None or n < 0) else self._body[:n]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, *, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_method(), req.data, timeout))
        if self._exc is not None:
            raise self._exc
        return self._resp


# ── remote-exec seam: value objects ────────────────────────────────────────────

def test_limits_reject_bad_values():
    with pytest.raises(ValueError):
        RemoteExecLimits(cpus=0)
    with pytest.raises(ValueError):
        RemoteExecLimits(memory_mb=True)  # bool is not an int here
    with pytest.raises(ValueError):
        RemoteExecLimits(output_bytes=10)  # below floor
    assert RemoteExecLimits().as_dict()["cpus"] == 1.0


def test_remote_file_rejects_traversal_and_bad_digest():
    with pytest.raises(ValueError):
        RemoteFile(path="../escape", sha256="sha256:" + "0" * 64, size=1)
    with pytest.raises(ValueError):
        RemoteFile(path="/abs", sha256="sha256:" + "0" * 64, size=1)
    with pytest.raises(ValueError):
        RemoteFile(path="ok.txt", sha256="deadbeef", size=1)
    good = RemoteFile(path="src/main.py", sha256="sha256:" + "0" * 64, size=12, executable=True)
    assert good.as_dict()["path"] == "src/main.py"


def test_request_validation_and_payload_is_deterministic():
    with pytest.raises(ValueError):
        RemoteExecRequest(argv=())
    with pytest.raises(ValueError):
        RemoteExecRequest(argv=["x"], network="open")
    with pytest.raises(ValueError):
        RemoteExecRequest(argv=["x"], workdir="relative")
    req = RemoteExecRequest(argv=["python", "-c", "print(1)"], runtime="python@3.12")
    payload = req.to_payload()
    assert payload["argv"] == ["python", "-c", "print(1)"]
    assert payload["network"] == "deny" and payload["runtime"] == "python@3.12"
    assert "env" not in payload  # no secret-carrying env channel in a launch spec
    assert json.dumps(payload, sort_keys=True)  # JSON-serialisable + deterministic


def test_build_remote_launch_shape():
    req = RemoteExecRequest(argv=["/bin/true"])
    spec = build_remote_launch(backend_id="e2b", request=req)
    assert spec.kind == "remote"
    assert spec.remote is not None and spec.remote["backend"] == "e2b"
    assert spec.remote["request"]["argv"] == ["/bin/true"]
    with pytest.raises(ValueError):
        build_remote_launch(backend_id="", request=req)


# ── reference loopback transport ────────────────────────────────────────────────

def test_loopback_transport_rejects_non_loopback_urls():
    # A hostname (even "localhost") is rejected — only a literal loopback IP is
    # trusted, so a poisoned resolver can never point the transport off-host.
    for bad in ("http://example.com", "http://169.254.169.254", "https://10.0.0.5:2000",
                "ftp://127.0.0.1", "http://localhost:2000", "http://0.0.0.0:2000"):
        with pytest.raises(ValueError):
            LoopbackExecTransport(base_url=bad)
    # literal loopback IPs are accepted
    LoopbackExecTransport(base_url="http://127.0.0.1:2000")
    LoopbackExecTransport(base_url="http://127.5.5.5:2000")
    LoopbackExecTransport(base_url="http://[::1]:2000")


def test_loopback_transport_declines_when_unreachable_live():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # now guaranteed closed
    t = LoopbackExecTransport(base_url=f"http://127.0.0.1:{port}", timeout_s=1.0)
    ok, reason = t.availability()
    assert not ok and "not reachable" in reason


def test_loopback_transport_submit_parses_response():
    body = json.dumps({"exit_code": 0, "stdout": "hi", "stderr": "", "timed_out": False}).encode()
    t = LoopbackExecTransport(base_url="http://127.0.0.1:2000", opener=_FakeOpener(resp=_FakeResp(body=body)))
    result = t.submit(RemoteExecRequest(argv=["echo", "hi"]))
    assert isinstance(result, RemoteExecResult)
    assert result.exit_code == 0 and result.stdout == "hi"
    assert result.attested_controls.kernel is Control.NOT_ENFORCED  # shared kernel, honest


def test_loopback_transport_submit_enforces_output_cap():
    big = b"x" * 4096
    t = LoopbackExecTransport(base_url="http://127.0.0.1:2000", opener=_FakeOpener(resp=_FakeResp(body=big)))
    with pytest.raises(RemoteExecError):
        t.submit(RemoteExecRequest(argv=["echo"], limits=RemoteExecLimits(output_bytes=1024)))


def test_loopback_transport_submit_rejects_non_json():
    t = LoopbackExecTransport(base_url="http://127.0.0.1:2000", opener=_FakeOpener(resp=_FakeResp(body=b"<html>")))
    with pytest.raises(RemoteExecError):
        t.submit(RemoteExecRequest(argv=["echo"]))


def test_loopback_transport_refuses_redirects():
    from urllib.error import URLError

    from bounded_loops.graph.adapters.enforcement.providers.remote_exec import _DenyRedirect

    with pytest.raises(URLError):
        _DenyRedirect().redirect_request(
            None, None, 307, "Temporary Redirect", {}, "http://attacker.example.com/collect",
        )


def test_loopback_transport_backs_only_workspace_only():
    """Honest attestation: an opaque sidecar can back workspace_only (no required
    dimensions) but NOT container_restricted (net/fs_write are UNKNOWN, never
    ENFORCED), so the receipt can never over-claim what the sidecar enforces."""
    transport = LoopbackExecTransport(
        base_url="http://127.0.0.1:2000", opener=_FakeOpener(resp=_FakeResp(status=200)),
    )
    prov = RemoteIsolationProvider(provider_id="remote", transport=transport, require_kernel=False)
    ok = prov.probe(tier=IsolationLevel.WORKSPACE_ONLY, network_mode=NetworkMode.DENY)
    assert ok.available and controls_meet(IsolationLevel.WORKSPACE_ONLY, NetworkMode.DENY, ok.controls)[0]
    weak = prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY)
    assert not controls_meet(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY, weak.controls)[0]


# ── container provider ──────────────────────────────────────────────────────────

def test_container_available_with_docker_and_pinned_image():
    prov = ContainerProvider(_DOCKER_LINUX, image=_IMG)
    avail = prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY)
    assert avail.available
    c = avail.controls
    assert c.net is Control.ENFORCED and c.fs_write is Control.ENFORCED
    assert c.pid is Control.ENFORCED and c.user is Control.ENFORCED
    assert c.kernel is Control.NOT_ENFORCED  # shared host kernel — honest


def test_container_declines_without_daemon_or_image():
    assert not ContainerProvider(_BARE, image=_IMG).probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
    ).available
    assert not ContainerProvider(_DOCKER_LINUX, image=None).probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
    ).available
    # mutable-tag image is refused at probe time (fail closed, not at launch)
    assert not ContainerProvider(_DOCKER_LINUX, image="runner:latest").probe(
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
    ).available


def test_container_is_only_a_container_restricted_tool():
    prov = ContainerProvider(_DOCKER_LINUX, image=_IMG)
    assert not prov.probe(tier=IsolationLevel.WORKSPACE_ONLY, network_mode=NetworkMode.DENY).available
    assert not prov.probe(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY).available
    assert not prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.ALLOWLIST).available


def test_container_build_launch_is_hardened(tmp_path):
    prov = ContainerProvider(_DOCKER_LINUX, image=_IMG)
    spec = prov.build_launch(
        inner_argv=["/usr/bin/python3", "-c", "pass"], workspace=tmp_path, home=tmp_path, tmpdir=tmp_path,
        tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY,
    )
    assert spec.kind == "local" and spec.argv[0] == "docker"
    assert "--network" in spec.argv and "none" in spec.argv
    assert "--cpus" in spec.argv and "--cap-drop" in spec.argv
    assert _IMG in spec.argv


# ── microvm / openshell providers (own-kernel, remote) ──────────────────────────

@pytest.mark.parametrize("provider_cls", [MicroVMProvider, OpenShellProvider])
def test_remote_provider_declines_without_transport(provider_cls):
    avail = provider_cls().probe(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY)
    assert not avail.available and "no remote-exec transport" in avail.reason


@pytest.mark.parametrize("provider_cls", [MicroVMProvider, OpenShellProvider])
def test_remote_provider_delivers_own_kernel_when_attested(provider_cls):
    prov = provider_cls(transport=_FakeTransport(kernel=Control.ENFORCED))
    for tier in (IsolationLevel.CUSTOMER_MANAGED_WORKER, IsolationLevel.CONTAINER_RESTRICTED):
        avail = prov.probe(tier=tier, network_mode=NetworkMode.DENY)
        assert avail.available and avail.controls.kernel is Control.ENFORCED
        assert controls_meet(tier, NetworkMode.DENY, avail.controls)[0]


@pytest.mark.parametrize("provider_cls", [MicroVMProvider, OpenShellProvider])
def test_remote_provider_refuses_shared_kernel_transport(provider_cls):
    prov = provider_cls(transport=_FakeTransport(kernel=Control.NOT_ENFORCED))
    avail = prov.probe(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY)
    assert not avail.available and "own-kernel" in avail.reason


@pytest.mark.parametrize("provider_cls", [MicroVMProvider, OpenShellProvider])
def test_remote_provider_propagates_transport_unavailability(provider_cls):
    prov = provider_cls(transport=_FakeTransport(available=False, reason="no E2B credentials"))
    avail = prov.probe(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY)
    assert not avail.available and "no E2B credentials" in avail.reason


@pytest.mark.parametrize("provider_cls", [MicroVMProvider, OpenShellProvider])
def test_remote_provider_declines_allowlist(provider_cls):
    prov = provider_cls(transport=_FakeTransport())
    assert not prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.ALLOWLIST).available


def test_remote_provider_build_launch_is_remote(tmp_path):
    prov = MicroVMProvider(transport=_FakeTransport(backend_id="e2b"))
    spec = prov.build_launch(
        inner_argv=["/bin/echo", "hi"], workspace=tmp_path, home=tmp_path, tmpdir=tmp_path,
        tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY,
    )
    assert spec.kind == "remote" and spec.remote is not None
    assert spec.remote["backend"] == "e2b"
    assert spec.remote["request"]["argv"] == ["/bin/echo", "hi"]
    with pytest.raises(ValueError):  # cannot open authorized egress yet
        prov.build_launch(
            inner_argv=["/bin/echo"], workspace=tmp_path, home=tmp_path, tmpdir=tmp_path,
            tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.ALLOWLIST,
        )
    with pytest.raises(ValueError):  # no transport
        MicroVMProvider().build_launch(
            inner_argv=["/bin/echo"], workspace=tmp_path, home=tmp_path, tmpdir=tmp_path,
            tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY,
        )


def test_generic_remote_provider_accepts_shared_kernel_at_container_grade():
    """The universal seam: a shared-kernel loopback backend can honestly back a
    container-grade remote node (require_kernel=False)."""
    prov = RemoteIsolationProvider(
        provider_id="remote", transport=_FakeTransport(kernel=Control.NOT_ENFORCED), require_kernel=False,
    )
    avail = prov.probe(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY)
    assert avail.available
    assert controls_meet(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY, avail.controls)[0]


# ── full default registry (fail-closed selection across all five providers) ─────

def test_default_registry_picks_native_for_container_restricted_live(tmp_path):
    """On this (unconfined, no-docker, Seatbelt) Mac, the cheap local floor wins:
    host_managed declines (live), native Seatbelt satisfies container_restricted
    before container / microvm / openshell are even reached."""
    reg = default_registry(_SEATBELT)
    out = reg.select(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=tmp_path)
    assert out.selection.provider_id == "native"


def test_default_registry_falls_through_to_container(tmp_path):
    """A Linux host with Docker but no Seatbelt/bwrap: native cannot deliver
    container_restricted, so the container provider is selected."""
    reg = default_registry(_DOCKER_LINUX, container_image=_IMG)
    out = reg.select(tier=IsolationLevel.CONTAINER_RESTRICTED, network_mode=NetworkMode.DENY, workspace=tmp_path)
    assert out.selection.provider_id == "container"


def test_default_registry_selects_microvm_for_customer_managed_worker(tmp_path):
    reg = default_registry(_SEATBELT, microvm_transport=_FakeTransport(backend_id="e2b", kernel=Control.ENFORCED))
    out = reg.select(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY, workspace=tmp_path)
    assert out.selection.provider_id == "microvm"
    assert out.selection.controls.kernel is Control.ENFORCED


def test_default_registry_fail_closed_when_no_provider_delivers_own_kernel(tmp_path):
    reg = default_registry(_SEATBELT)  # no remote transports configured
    with pytest.raises(GraphValidationError):
        reg.select(tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY, workspace=tmp_path)
