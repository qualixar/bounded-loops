"""OpenSandbox transport for the remote-execution seam (C, ADR-12 D6).

OpenSandbox is a general-purpose sandbox platform (Apache-2.0, CNCF Landscape) with Docker and
Kubernetes runtimes, secure container runtimes (gVisor / Kata / Firecracker), per-sandbox egress
control, and a brokered credential vault. It is one layer BELOW this engine, not a competitor: it
answers "where can code run", while this engine answers "who is entitled to declare the work
done". So it plugs into the existing ``RemoteExecTransport`` Protocol rather than replacing
anything, exactly as ``remote_exec``'s module docstring anticipated for off-host backends.

**Why this is an opt-in tier and never a default.** A native Seatbelt or bubblewrap sandbox needs
no daemon, no image, and no server; OpenSandbox's floor is a running server. Making it the default
would trade this engine's cheapest property — confinement on a laptop with nothing installed — for
scale a laptop does not need. The registry already prefers native mechanisms; this provider is for
a deployment that already runs the platform.

**What this transport attests, and the two dimensions it deliberately refuses to.** The execd API
publishes ``GET /v1/isolated/capabilities``, which reports the per-session isolator plus the state
of each hardening layer as ``active`` | ``disabled`` | ``degraded`` | ``unsupported`` — a
four-valued distinction, not a boolean, which maps almost exactly onto this project's
``Control.ENFORCED`` / ``NOT_ENFORCED`` / ``UNKNOWN``. That is why this backend can attest more
than the generic loopback sidecar can.

It still cannot attest everything, and the gaps are structural rather than missing work:

  * ``net`` and ``egress`` are owned by OpenSandbox's *egress* component (OSEP-0001, a separate
    API), not by execd. Nothing in the capabilities response speaks to them, so both stay UNKNOWN.
    Consequence: this transport cannot honestly back a ``container_restricted`` node, because that
    tier requires ``net`` ENFORCED. That is a refusal, not a downgrade.
  * ``kernel`` comes from the container runtime selected when the sandbox was CREATED (the
    lifecycle API's concern — runc shares the host kernel, gVisor / Kata / Firecracker do not).
    execd reports the per-session isolator, not the runtime beneath it, so own-kernel isolation is
    not provable from this endpoint either. Consequence: this transport cannot back
    ``customer_managed_worker``, and ``RemoteIsolationProvider(require_kernel=True)`` will refuse
    it — correctly.

Reading the capabilities response as proof of a container-grade cage would be the exact defect this
project publishes about: a control that is declared in a readable place and enforced somewhere else,
or nowhere. Attesting UNKNOWN costs a tier and keeps the receipt true.

No credential is handled here. The access token is read from an operator-named environment variable
at request time, sent in the header the API requires, and never logged, stored, echoed in an error,
or placed in a launch spec.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from typing import Any, Mapping, Protocol
from urllib import request as _urlrequest
from urllib.error import URLError
from urllib.parse import urlsplit

from bounded_loops.graph.adapters.enforcement.provider import Control, EnforcedControls
from bounded_loops.graph.adapters.enforcement.providers.remote_exec import (
    RemoteExecError,
    RemoteExecRequest,
    RemoteExecResult,
    _DenyRedirect,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

#: The header the execd API requires (``securitySchemes.AccessToken``). A NAME, never a value.
ACCESS_TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
#: Default env var an operator points at their token. Configurable; never read at import time.
DEFAULT_TOKEN_ENV = "OPENSANDBOX_ACCESS_TOKEN"
#: execd's own local-development port, from the published spec's ``servers`` list.
DEFAULT_BASE_URL = "http://127.0.0.1:44772"

#: Execution is not implemented yet (see ``submit``). ``availability`` reads this so a reachable
#: server can never be SELECTED and then fail at execution — a provider that advertises itself and
#: then cannot run a node is the declared-not-enforced shape this project exists to refuse. One
#: constant, read by both, so the guard and the raise cannot drift apart.
EXECUTION_IMPLEMENTED = False

#: A hardening layer counts as enforced only when execd says ``active``. ``degraded`` means
#: "configured but a prerequisite is missing", which is precisely the case that must not read as
#: enforced — it is the shape of a control that looks present and constrains less than it claims.
_ACTIVE = "active"
_KNOWN_NEGATIVE = frozenset({"disabled", "unsupported"})


class _HttpOpener(Protocol):
    """The one method this transport needs from a urllib opener.

    Named rather than typed as ``object`` so the test seam is a stated contract: an injected opener
    must refuse redirects and ignore proxies, and it has to answer ``open``.
    """

    def open(self, req: Any, timeout: float | None = ...) -> Any: ...  # noqa: A003


def _is_loopback(host: str) -> bool:
    """True only for a literal loopback IP. A hostname, including ``localhost``, is rejected.

    Same rule and same reason as ``LoopbackExecTransport``: trusting a name means trusting the
    resolver, and this transport must not be steerable off-host by DNS.
    """
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _layer_control(layer: object) -> Control:
    """One ``HardeningLayerState`` to a ``Control``, refusing to guess.

    A malformed or absent layer is UNKNOWN rather than NOT_ENFORCED: "the backend did not tell us"
    and "the backend told us it is off" are different facts, and only the second is safe to report
    as a definite negative.
    """
    if not isinstance(layer, Mapping):
        return Control.UNKNOWN
    state = layer.get("state")
    if state == _ACTIVE:
        return Control.ENFORCED
    if state in _KNOWN_NEGATIVE:
        return Control.NOT_ENFORCED
    # "degraded", an unrecognised future value, or a missing field.
    return Control.UNKNOWN


@dataclass(frozen=True)
class OpenSandboxCapabilities:
    """The parsed capabilities response, kept as data so the mapping stays pure and testable."""

    available: bool
    isolator: str
    message: str
    init_mode: str
    signal_shield: bool
    seccomp: Control
    landlock: Control
    cap_drop: Control

    @classmethod
    def from_payload(cls, payload: object) -> OpenSandboxCapabilities:
        if not isinstance(payload, Mapping):
            raise RemoteExecError("opensandbox capabilities response must be a JSON object")
        hardening = payload.get("hardening")
        hardening = hardening if isinstance(hardening, Mapping) else {}
        init_mode = hardening.get("init_mode")
        return cls(
            available=bool(payload.get("available", False)),
            isolator=str(payload.get("isolator", "") or ""),
            message=str(payload.get("message", "") or ""),
            init_mode=init_mode if isinstance(init_mode, str) else "",
            signal_shield=bool(hardening.get("signal_shield", False)),
            seccomp=_layer_control(hardening.get("seccomp")),
            landlock=_layer_control(hardening.get("landlock")),
            cap_drop=_layer_control(hardening.get("cap_drop")),
        )

    def to_controls(self) -> EnforcedControls:
        """Map published capabilities onto per-dimension truth.

        ``fs_write`` follows Landlock, which is the layer that actually confines filesystem writes.
        ``pid`` requires ``init_mode == "pid1"``: only then is execd the kernel init of the
        container, which is what makes a PID boundary real — ``subreaper`` reaps orphans without the
        kernel signal shield, so it is a weaker thing wearing a similar name. ``user`` follows
        capability/bounding-set reduction.

        ``net``, ``egress`` and ``kernel`` are UNKNOWN by construction; see the module docstring.
        """
        notes: list[str] = [
            f"opensandbox execd isolator={self.isolator or 'unreported'}; "
            f"init_mode={self.init_mode or 'unreported'}",
            "net and egress are owned by the OpenSandbox egress component (a separate API) and are "
            "not reported by execd capabilities, so they are UNKNOWN here",
            "kernel isolation is a property of the container runtime chosen at sandbox creation, "
            "which execd does not report, so own-kernel isolation is not attested",
        ]
        if not self.available and self.message:
            notes.append(f"isolation unavailable: {self.message}")
        return EnforcedControls(
            net=Control.UNKNOWN,
            fs_write=self.landlock,
            fs_read=Control.UNKNOWN,
            pid=Control.ENFORCED if self.init_mode == "pid1" else Control.UNKNOWN,
            user=self.cap_drop,
            kernel=Control.UNKNOWN,
            egress=Control.UNKNOWN,
            notes=tuple(notes),
        )


class OpenSandboxTransport:
    """A ``RemoteExecTransport`` backed by an OpenSandbox execd endpoint.

    Loopback-only unless an operator opts in explicitly. A non-loopback OpenSandbox server is a
    legitimate enterprise deployment, but reaching one is an egress decision this module will not
    make on an operator's behalf: pass ``allow_offhost=True`` from deployment wiring that has
    already routed it through the C1 egress broker. Redirects and HTTP proxies are refused on every
    path, loopback or not.

    ``opener`` is a test seam. An injected opener is trusted to preserve those guarantees; the
    default built here does not follow redirects and ignores proxy environment variables.
    """

    backend_id = "opensandbox"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token_env: str = DEFAULT_TOKEN_ENV,
        timeout_s: float = 5.0,
        allow_offhost: bool = False,
        opener: _HttpOpener | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("OpenSandboxTransport requires an http(s) base_url with a host")
        if not _is_loopback(parsed.hostname) and not allow_offhost:
            raise ValueError(
                "OpenSandboxTransport refuses a non-loopback endpoint unless allow_offhost=True. "
                "An off-host OpenSandbox server is supported, but routing to it is an egress "
                "decision for deployment wiring behind the C1 broker, not a default of this module."
            )
        if type(timeout_s) is bool or not isinstance(timeout_s, (int, float)) or not (0 < timeout_s <= 60):
            raise ValueError("timeout_s must be in (0, 60]")
        if not (isinstance(token_env, str) and token_env and "=" not in token_env):
            raise ValueError("token_env must be the NAME of an environment variable")
        self._base = base_url.rstrip("/")
        self._token_env = token_env
        self._timeout = float(timeout_s)
        self._opener = opener if opener is not None else _urlrequest.build_opener(
            _DenyRedirect(), _urlrequest.ProxyHandler({}),
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """Request headers, resolving the access token by NAME at call time.

        Read per request rather than captured at construction so a rotated token takes effect
        without rebuilding the transport, and so no token is retained on this object.
        """
        headers = {"Accept": "application/json"}
        token = os.environ.get(self._token_env, "")
        if token:
            headers[ACCESS_TOKEN_HEADER] = token
        return headers

    def _get_json(self, path: str) -> object:
        req = _urlrequest.Request(self._base + path, method="GET", headers=self._headers())
        with self._opener.open(req, timeout=self._timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                raise RemoteExecError(f"opensandbox {path} returned {status}")
            raw = resp.read(1 << 20)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteExecError(f"opensandbox {path} did not return valid JSON") from exc

    def capabilities(self) -> OpenSandboxCapabilities:
        """Fetch and parse ``GET /v1/isolated/capabilities``."""
        return OpenSandboxCapabilities.from_payload(self._get_json("/v1/isolated/capabilities"))

    # ── RemoteExecTransport ──────────────────────────────────────────────────

    def availability(self) -> tuple[bool, str]:
        """May this provider be SELECTED for a node? Fail-closed while execution is unimplemented.

        Deliberately not the same question as "is the backend healthy" — see ``backend_reachable``.
        Returning True here while ``submit`` raises would let the registry choose this provider and
        then fail at execution, having already paid for every node upstream. Never includes the
        token or a response body in the reason.
        """
        if not EXECUTION_IMPLEMENTED:
            # Checked BEFORE the network so the answer cannot depend on a server being up: a
            # deployment must get the same refusal whether or not the platform is running.
            return (
                False,
                "opensandbox: execution is not implemented yet, so this backend must not be "
                "selected — see submit(). Attestation is complete and tested; flip "
                "EXECUTION_IMPLEMENTED when submit() lands.",
            )
        return self.backend_reachable()

    def backend_reachable(self) -> tuple[bool, str]:
        """Whether the backend is up and its isolator reports itself available.

        Split out from ``availability`` so the two questions stay separately answerable and
        separately testable: "is the platform healthy" is an operator diagnostic that remains
        meaningful and exercised while "may this provider be selected" is gated on execution
        existing. Folding them into one function is how the reachability logic would have rotted
        untested behind the guard above.

        Two calls rather than one on purpose: a reachable execd whose isolation is unavailable is a
        different failure from an unreachable one, and an operator needs to be told which.
        """
        try:
            req = _urlrequest.Request(self._base + "/ping", method="GET", headers=self._headers())
            with self._opener.open(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                return (False, f"opensandbox execd /ping returned {status}")
        except (URLError, OSError, ValueError) as exc:
            return (False, f"opensandbox execd not reachable ({type(exc).__name__})")
        try:
            caps = self.capabilities()
        except (RemoteExecError, URLError, OSError, ValueError) as exc:
            return (False, f"opensandbox capabilities unreadable ({type(exc).__name__})")
        if not caps.available:
            detail = caps.message or "no reason given"
            return (False, f"opensandbox isolator reports unavailable ({detail})")
        return (True, "")

    def attested_controls(
        self, *, tier: IsolationLevel, network_mode: NetworkMode,
    ) -> EnforcedControls:
        """Per-dimension truth from the live capabilities response.

        ``tier`` and ``network_mode`` are accepted for Protocol conformance and deliberately do not
        influence the answer: what the backend enforces is a property of the backend, not of what
        the caller wants. Letting the request shape the attestation is how an over-claim gets in.

        An unreachable or unreadable backend attests nothing rather than raising, so the registry
        sees a provider that cannot deliver and falls through to a local one.
        """
        try:
            return self.capabilities().to_controls()
        except (RemoteExecError, URLError, OSError, ValueError):
            return EnforcedControls(
                notes=("opensandbox capabilities could not be read, so nothing is attested",),
            )

    def submit(self, request: RemoteExecRequest) -> RemoteExecResult:
        """Not implemented, and failing closed rather than guessing the wire flow.

        execd's ``POST /command`` responds with ``text/event-stream`` and the exit code arrives
        separately from ``GET /command/status/{id}``, so a correct implementation has to pin how the
        stream reports the command id, how stdout and stderr are distinguished across events, and
        how a timeout surfaces. None of that is settled by reading the specification, and no
        OpenSandbox server was reachable when this was written.

        Writing it from the spec alone is the failure this repository already has a comment about:
        an unread shape recorded as verified. So this raises, which means a node routed here fails
        closed with a reason instead of silently producing an empty result that a gate would then
        judge. ``availability`` and ``attested_controls`` are complete and tested; execution is the
        named remaining work.
        """
        raise RemoteExecError(
            "opensandbox: submit is not implemented yet — execd streams command output as SSE and "
            "reports the exit code from a separate status endpoint, a flow that needs a live server "
            "to pin rather than a specification read. Use a local provider, or supply a "
            "backend-specific transport."
        )
