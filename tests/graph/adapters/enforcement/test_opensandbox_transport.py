"""OpenSandbox transport: what it attests, and the three things it refuses to attest.

The interesting assertions here are the negative ones. A backend that publishes a capabilities
endpoint is tempting to read as proof of a cage, and reading it that way would reproduce this
project's own defect class — a control declared in a readable place and enforced somewhere else.
So the tests below pin that ``net``, ``egress`` and ``kernel`` stay UNKNOWN even when every layer
execd DOES report is active, and that a ``degraded`` layer never reads as enforced.

No server is contacted except in the one test that asserts unreachability, which is honest on any
host: it asserts the failure, not a success.
"""

from __future__ import annotations

import json


import pytest

from bounded_loops.graph.adapters.enforcement.provider import (
    Control,
    controls_meet,
)
from bounded_loops.graph.adapters.enforcement.providers.opensandbox import (
    ACCESS_TOKEN_HEADER,
    OpenSandboxCapabilities,
    OpenSandboxTransport,
)
from bounded_loops.graph.adapters.enforcement.providers.remote_exec import (
    RemoteExecError,
    RemoteExecLimits,
    RemoteExecRequest,
    RemoteIsolationProvider,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel


def _caps(**over) -> dict:
    """A capabilities payload in the published shape, fully hardened unless overridden."""
    body = {
        "available": True,
        "isolator": "bwrap",
        "commit_supported": True,
        "diff_supported": True,
        "hardening": {
            "init_mode": "pid1",
            "signal_shield": True,
            "seccomp": {"state": "active"},
            "landlock": {"state": "active"},
            "cap_drop": {"state": "active"},
            "ebpf": {"state": "active"},
        },
    }
    body.update(over)
    return body


class _FakeOpener:
    """Minimal stand-in for a urllib opener; records the headers it was given."""

    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        self._routes = routes
        self.seen_headers: list[dict[str, str]] = []

    def open(self, req, timeout=None):  # noqa: ANN001, ARG002
        self.seen_headers.append(dict(req.headers))
        path = req.full_url.split("44772", 1)[-1] if "44772" in req.full_url else req.full_url
        for suffix, (status, payload) in self._routes.items():
            if path.endswith(suffix):
                raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                return _FakeResponse(status, raw)
        raise OSError(f"no fake route for {req.full_url}")


class _FakeResponse:
    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self._raw = raw

    def read(self, _n: int | None = None) -> bytes:
        return self._raw

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


# ── the refusals, which are the point ────────────────────────────────────────


def test_net_egress_and_kernel_stay_unknown_even_when_fully_hardened() -> None:
    """The central honesty guard. Every layer execd reports is active here.

    A reader could easily conclude "seccomp + Landlock + cap_drop + pid1 means a container-grade
    cage". It does not follow: network denial and authorized egress belong to OpenSandbox's egress
    component, and own-kernel isolation belongs to the container runtime picked at sandbox creation.
    Neither is visible in this response, so neither may be attested.
    """
    controls = OpenSandboxCapabilities.from_payload(_caps()).to_controls()

    assert controls.fs_write is Control.ENFORCED, "Landlock active must attest write confinement"
    assert controls.pid is Control.ENFORCED, "init_mode pid1 must attest a PID boundary"
    assert controls.user is Control.ENFORCED, "cap_drop active must attest privilege reduction"

    assert controls.net is Control.UNKNOWN, "net is not reported by execd capabilities"
    assert controls.egress is Control.UNKNOWN, "egress is a separate OpenSandbox component"
    assert controls.kernel is Control.UNKNOWN, "own-kernel isolation is a runtime property"


def test_a_fully_hardened_response_still_cannot_back_a_container_tier() -> None:
    """The consequence of the refusal above, stated as the decision the registry will make.

    container_restricted requires net ENFORCED. This is the assertion that would start failing if
    someone later "fixed" the mapping by assuming a network cage — the failure would otherwise show
    up as a receipt claiming a cage nobody applied.
    """
    controls = OpenSandboxCapabilities.from_payload(_caps()).to_controls()

    ok, reason = controls_meet(IsolationLevel.CONTAINER_RESTRICTED, NetworkMode.DENY, controls)
    assert not ok
    assert "net" in reason

    kernel_ok, kernel_reason = controls_meet(
        IsolationLevel.CUSTOMER_MANAGED_WORKER, NetworkMode.DENY, controls,
    )
    assert not kernel_ok
    assert "kernel" in kernel_reason

    # It CAN honestly back the tier whose requirement it does prove.
    proc_ok, _ = controls_meet(IsolationLevel.PROCESS_RESTRICTED, NetworkMode.DENY, controls)
    assert proc_ok, "init_mode pid1 proves the pid dimension, so this tier is deliverable"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("active", Control.ENFORCED),
        ("disabled", Control.NOT_ENFORCED),
        ("unsupported", Control.NOT_ENFORCED),
        ("degraded", Control.UNKNOWN),
        ("a-future-value-we-have-never-seen", Control.UNKNOWN),
    ],
)
def test_degraded_never_reads_as_enforced(state: str, expected: Control) -> None:
    """``degraded`` means configured but a prerequisite is missing — a control that looks present
    and constrains less than it claims, which is exactly the class this project publishes about.
    An unrecognised future value is UNKNOWN for the same reason."""
    payload = _caps()
    payload["hardening"]["landlock"] = {"state": state}

    assert OpenSandboxCapabilities.from_payload(payload).to_controls().fs_write is expected


def test_an_absent_layer_is_unknown_not_a_negative() -> None:
    """"The backend did not say" and "the backend said no" are different facts."""
    payload = _caps()
    del payload["hardening"]["landlock"]

    assert OpenSandboxCapabilities.from_payload(payload).to_controls().fs_write is Control.UNKNOWN


def test_subreaper_does_not_prove_a_pid_boundary() -> None:
    """``subreaper`` reaps orphans without the kernel PID 1 signal shield: a weaker thing wearing a
    similar name, and the kind of near-miss that passes a careless reading."""
    payload = _caps()
    payload["hardening"]["init_mode"] = "subreaper"

    assert OpenSandboxCapabilities.from_payload(payload).to_controls().pid is Control.UNKNOWN


def test_the_attestation_does_not_depend_on_what_the_caller_asked_for() -> None:
    """What a backend enforces is a property of the backend. If the requested tier could influence
    the answer, an over-claim would have a route in."""
    opener = _FakeOpener({"/v1/isolated/capabilities": (200, _caps())})
    transport = OpenSandboxTransport(opener=opener)

    for tier in IsolationLevel:
        for mode in (NetworkMode.DENY, NetworkMode.OPEN, NetworkMode.ALLOWLIST):
            controls = transport.attested_controls(tier=tier, network_mode=mode)
            assert controls.net is Control.UNKNOWN
            assert controls.kernel is Control.UNKNOWN
            assert controls.fs_write is Control.ENFORCED


# ── availability, including live fail-closed ─────────────────────────────────


def test_no_server_reachable_fails_closed_with_a_reason() -> None:
    """LIVE on any host: nothing is listening on execd's port in a test environment.

    Asserting the failure rather than a success is what makes this honest to run anywhere. If an
    OpenSandbox server ever IS running locally, this test would be wrong to pass silently, so it
    accepts either a transport failure or an isolator that reports itself unavailable — and refuses
    a bare success.
    """
    transport = OpenSandboxTransport(base_url="http://127.0.0.1:44772")

    ok, reason = transport.backend_reachable()

    assert not ok, "backend_reachable must not report success without a reachable isolator"
    assert "opensandbox" in reason
    # And it attests nothing rather than raising, so the registry can fall through.
    controls = transport.attested_controls(
        tier=IsolationLevel.PROCESS_RESTRICTED, network_mode=NetworkMode.DENY,
    )
    assert controls.pid is Control.UNKNOWN


def test_a_reachable_execd_with_isolation_off_is_distinguished_from_unreachable() -> None:
    """Two different operator problems must not share one message."""
    opener = _FakeOpener({
        "/ping": (200, {}),
        "/v1/isolated/capabilities": (200, _caps(available=False, message="bwrap not installed")),
    })
    transport = OpenSandboxTransport(opener=opener)

    ok, reason = transport.backend_reachable()

    assert not ok
    assert "reports unavailable" in reason
    assert "bwrap not installed" in reason, "the backend's own diagnostic must reach the operator"


def test_backend_reachable_succeeds_when_ping_and_capabilities_both_agree() -> None:
    """The operator diagnostic, which stays meaningful while selection is gated."""
    opener = _FakeOpener({"/ping": (200, {}), "/v1/isolated/capabilities": (200, _caps())})

    assert OpenSandboxTransport(opener=opener).backend_reachable() == (True, "")


def test_a_healthy_backend_is_still_refused_for_selection_while_submit_raises() -> None:
    """The defect this guard closes. Found by asking what a release would actually do.

    ``availability`` returning True while ``submit`` raises means the registry selects this
    provider and then fails at execution, having already paid for every node upstream of it. That
    is a capability declared in a readable place and enforced nowhere — this project's own defect
    class, in this project. So selection is refused independently of whether the platform is up.
    """
    opener = _FakeOpener({"/ping": (200, {}), "/v1/isolated/capabilities": (200, _caps())})
    transport = OpenSandboxTransport(opener=opener)

    assert transport.backend_reachable() == (True, ""), "the backend really is healthy here"

    ok, reason = transport.availability()
    assert not ok, "a provider that cannot execute must never be selectable"
    assert "not implemented" in reason
    # And the refusal must not depend on the server: same answer with nothing listening.
    offline_ok, offline_reason = OpenSandboxTransport(opener=_FakeOpener({})).availability()
    assert not offline_ok
    assert offline_reason == reason, "the refusal must be independent of backend health"


# ── the guards ───────────────────────────────────────────────────────────────


def test_a_non_loopback_endpoint_is_refused_without_explicit_opt_in() -> None:
    """An off-host server is legitimate; deciding to reach one is not this module's call."""
    with pytest.raises(ValueError, match="allow_offhost"):
        OpenSandboxTransport(base_url="https://sandbox.example.internal")

    # And the opt-in works, so the refusal is a gate rather than a wall.
    assert OpenSandboxTransport(
        base_url="https://sandbox.example.internal", allow_offhost=True,
    ).backend_id == "opensandbox"


def test_a_hostname_is_refused_even_when_it_names_loopback() -> None:
    """Trusting ``localhost`` means trusting the resolver. Same rule as the loopback exec transport."""
    with pytest.raises(ValueError, match="allow_offhost"):
        OpenSandboxTransport(base_url="http://localhost:44772")


def test_the_token_is_read_by_name_at_request_time_and_never_retained(monkeypatch) -> None:
    """A rotated token must take effect without rebuilding the transport, and the transport object
    must not hold the value. Asserted on the object's own state, not on behaviour."""
    opener = _FakeOpener({"/v1/isolated/capabilities": (200, _caps())})
    transport = OpenSandboxTransport(opener=opener, token_env="BL_TEST_OSB_TOKEN")

    monkeypatch.setenv("BL_TEST_OSB_TOKEN", "first-value")
    transport.capabilities()
    monkeypatch.setenv("BL_TEST_OSB_TOKEN", "second-value")
    transport.capabilities()

    # urllib normalises header keys through str.capitalize(), so match case-insensitively rather
    # than guessing the casing it happens to produce.
    wanted = ACCESS_TOKEN_HEADER.lower()
    sent = [
        next((v for k, v in h.items() if k.lower() == wanted), None) for h in opener.seen_headers
    ]
    assert sent == ["first-value", "second-value"], "the token must be resolved per request"
    assert not any("first-value" in str(v) for v in vars(transport).values()), (
        "no token value may be retained on the transport"
    )


def test_token_env_must_be_a_name_not_an_assignment() -> None:
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        OpenSandboxTransport(token_env="OPENSANDBOX_ACCESS_TOKEN=hunter2")


def test_submit_fails_closed_rather_than_returning_an_empty_result() -> None:
    """The execution path is not implemented. A silent empty result would be handed to a gate as if
    the work had run, which is the worst available outcome."""
    transport = OpenSandboxTransport(opener=_FakeOpener({}))
    request = RemoteExecRequest(argv=("echo", "hi"), limits=RemoteExecLimits())

    with pytest.raises(RemoteExecError, match="not implemented yet"):
        transport.submit(request)


def test_both_refusal_gates_work_and_in_this_order(monkeypatch) -> None:
    """Two independent reasons this transport cannot back an own-kernel tier, and both must hold.

    The execution gate fires first, so that is what an operator sees today. But the attestation
    refusal is the one that must survive `submit()` landing — otherwise flipping
    ``EXECUTION_IMPLEMENTED`` would silently promote a shared-kernel backend into a tier that
    requires its own kernel. Patching the flag proves the second gate is real and not merely
    shadowed by the first.
    """
    from bounded_loops.graph.adapters.enforcement.providers import opensandbox as osb

    opener = _FakeOpener({"/ping": (200, {}), "/v1/isolated/capabilities": (200, _caps())})
    provider = RemoteIsolationProvider(
        provider_id="microvm", transport=OpenSandboxTransport(opener=opener), require_kernel=True,
    )

    first = provider.probe(
        tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY,
    )
    assert not first.available
    assert "not implemented" in first.reason, "the execution gate is the one that fires today"

    monkeypatch.setattr(osb, "EXECUTION_IMPLEMENTED", True)
    second = provider.probe(
        tier=IsolationLevel.CUSTOMER_MANAGED_WORKER, network_mode=NetworkMode.DENY,
    )
    assert not second.available, "the kernel refusal must survive execution being implemented"
    assert "own-kernel" in second.reason
