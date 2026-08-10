"""Universal remote-execution seam (E3, ADR-12 D6).

A single portable contract that ANY off-host isolation backend can speak —
E2B / Firecracker microVMs, NVIDIA NemoClaw OpenShell, or a self-hosted exec
sidecar. Providers that isolate a node *off* this host (``microvm``,
``openshell``) hold no backend client of their own: they build a portable
``RemoteExecRequest`` and defer to an injected ``RemoteExecTransport``. That
keeps every remote backend behind one audited seam (ports-and-adapters) and lets
the receipt publish the controls the backend *attests* — never controls the
engine merely hopes for.

The seam is shaped "Piston-style" (files + stdin + args + limits + network →
exit / stdout / stderr), so a self-hosted code-exec engine is a first-class
citizen alongside hosted microVM SaaS. ``LoopbackExecTransport`` is the runnable
reference: it targets a self-hosted exec sidecar on LOOPBACK ONLY (never a public
endpoint — that is both an SSRF guard and the network-egress boundary this
project runs under) and honestly attests a SHARED host kernel, so it can back a
container-grade remote node but is refused by the own-kernel providers. Hosted
backends that need credentials or a public endpoint (E2B, remote NemoClaw) plug
into the same ``RemoteExecTransport`` Protocol behind the C1 egress broker and
are injected explicitly; this module never fabricates a transport, never opens a
non-loopback socket, and never handles a secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import re
from typing import Protocol
from urllib import request as _urlrequest
from urllib.error import URLError
from urllib.parse import urlsplit

from bounded_loops.graph.adapters.enforcement.provider import (
    Availability,
    Control,
    EnforcedControls,
    LaunchSpec,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# A relative POSIX path with no NUL, no backslash, and no ``..`` component.
_REL_POSIX = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\x00\\]+$")


class RemoteExecError(RuntimeError):
    """A remote backend failed to execute a request (transport/protocol error)."""


def _require_int(value: object, name: str, low: int, high: int) -> None:
    if type(value) is bool or not isinstance(value, int):
        raise ValueError(f"{name} must be an int")
    if not (low <= value <= high):
        raise ValueError(f"{name} must be in [{low}, {high}]")


@dataclass(frozen=True)
class RemoteExecLimits:
    """Resource ceilings the remote backend must apply. Portable, backend-neutral."""

    cpus: float = 1.0
    memory_mb: int = 1024
    wall_seconds: int = 300
    pids: int = 256
    output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if type(self.cpus) is bool or not isinstance(self.cpus, (int, float)) or not (0 < self.cpus <= 64):
            raise ValueError("cpus must be in (0, 64]")
        _require_int(self.memory_mb, "memory_mb", 1, 262_144)
        _require_int(self.wall_seconds, "wall_seconds", 1, 86_400)
        _require_int(self.pids, "pids", 1, 1_000_000)
        _require_int(self.output_bytes, "output_bytes", 1024, 268_435_456)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cpus": float(self.cpus),
            "memory_mb": self.memory_mb,
            "wall_seconds": self.wall_seconds,
            "pids": self.pids,
            "output_bytes": self.output_bytes,
        }


@dataclass(frozen=True)
class RemoteFile:
    """A content-addressed reference to a workspace file to stage remotely.

    Carries a digest, never inline bytes — secrets and large payloads never ride
    inside the launch spec. The transport resolves contents from the
    content-addressed store when it stages the workspace.
    """

    path: str
    sha256: str
    size: int
    executable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _REL_POSIX.match(self.path):
            raise ValueError("RemoteFile.path must be a relative POSIX path without traversal")
        if not isinstance(self.sha256, str) or not _DIGEST.match(self.sha256):
            raise ValueError("RemoteFile.sha256 must be 'sha256:<64 hex>'")
        _require_int(self.size, "RemoteFile.size", 0, 1 << 40)
        if type(self.executable) is not bool:
            raise ValueError("RemoteFile.executable must be a bool")

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "executable": self.executable}


@dataclass(frozen=True)
class RemoteExecRequest:
    """A portable request to run one node's command on a remote backend.

    Deliberately carries NO environment map: a secret must never ride inside a
    launch spec. The remote backend supplies its own base environment, and any
    non-secret variables a node needs are provisioned out-of-band by the C1
    egress broker (which alone is allowed to touch credentials).
    """

    argv: tuple[str, ...]
    workdir: str = "/workspace"
    stdin: str = ""
    network: str = "deny"
    runtime: str | None = None
    limits: RemoteExecLimits = field(default_factory=RemoteExecLimits)
    files: tuple[RemoteFile, ...] = ()

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError("argv must be a non-empty tuple of non-empty strings")
        object.__setattr__(self, "argv", argv)
        if not (isinstance(self.workdir, str) and self.workdir.startswith("/") and "\x00" not in self.workdir):
            raise ValueError("workdir must be an absolute POSIX path")
        if not isinstance(self.stdin, str):
            raise ValueError("stdin must be a string")
        if self.network not in ("deny", "allowlist"):
            raise ValueError("network must be 'deny' or 'allowlist'")
        if self.runtime is not None and (not isinstance(self.runtime, str) or not self.runtime):
            raise ValueError("runtime must be a non-empty string or None")
        object.__setattr__(self, "files", tuple(self.files))

    def to_payload(self) -> dict[str, object]:
        """A deterministic, JSON-safe dict for the launch spec / transport wire."""
        return {
            "argv": list(self.argv),
            "workdir": self.workdir,
            "stdin": self.stdin,
            "network": self.network,
            "runtime": self.runtime,
            "limits": self.limits.as_dict(),
            "files": [f.as_dict() for f in self.files],
        }


@dataclass(frozen=True)
class RemoteExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    attested_controls: EnforcedControls


class RemoteExecTransport(Protocol):
    """A backend that can run a ``RemoteExecRequest`` under real isolation.

    ``attested_controls`` returns the per-dimension truth the backend can PROVE
    for this tier — providers publish this, never their own hopes.
    """

    backend_id: str

    def availability(self) -> tuple[bool, str]: ...

    def attested_controls(
        self, *, tier: IsolationLevel, network_mode: NetworkMode,
    ) -> EnforcedControls: ...

    def submit(self, request: RemoteExecRequest) -> RemoteExecResult: ...


def build_remote_launch(*, backend_id: str, request: RemoteExecRequest) -> LaunchSpec:
    """Wrap a request as a ``remote`` LaunchSpec the worker hands to a transport."""
    if not (isinstance(backend_id, str) and backend_id):
        raise ValueError("backend_id must be a non-empty string")
    return LaunchSpec(kind="remote", remote={"backend": backend_id, "request": request.to_payload()})


class RemoteIsolationProvider:
    """Base for providers that isolate a node OFF this host via a transport.

    Honest by construction: it never claims a tier its transport cannot attest,
    and (for the own-kernel providers) refuses a transport that only shares the
    host kernel. With no transport it declines fail-closed — a laptop with no
    admitted remote backend simply falls through to the local providers.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        transport: RemoteExecTransport | None = None,
        require_kernel: bool = True,
        limits: RemoteExecLimits | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._transport = transport
        self._require_kernel = require_kernel
        self._limits = limits if limits is not None else RemoteExecLimits()

    @property
    def transport(self) -> RemoteExecTransport | None:
        return self._transport

    def probe(
        self, *, tier: IsolationLevel, network_mode: NetworkMode, workspace=None,
    ) -> Availability:
        if network_mode is NetworkMode.ALLOWLIST:
            return Availability(
                False,
                f"{self.provider_id}: authorized egress requires the C1 egress broker (not yet available)",
                EnforcedControls(),
            )
        if self._transport is None:
            return Availability(False, f"{self.provider_id}: no remote-exec transport configured", EnforcedControls())
        ok, reason = self._transport.availability()
        if not ok:
            return Availability(False, f"{self.provider_id}: transport unavailable ({reason})", EnforcedControls())
        controls = self._transport.attested_controls(tier=tier, network_mode=network_mode)
        if self._require_kernel and controls.kernel is not Control.ENFORCED:
            return Availability(
                False, f"{self.provider_id}: transport does not attest own-kernel isolation", controls,
            )
        return Availability(True, "", controls)

    def build_launch(
        self, *, inner_argv, workspace, home, tmpdir, tier: IsolationLevel, network_mode: NetworkMode,
    ) -> LaunchSpec:
        if network_mode is NetworkMode.ALLOWLIST:
            raise ValueError(f"{self.provider_id}: cannot open authorized egress yet")
        if self._transport is None:
            raise ValueError(f"{self.provider_id}: no transport to launch through")
        inner = tuple(inner_argv)
        if not inner:
            raise ValueError("inner_argv must not be empty")
        request = RemoteExecRequest(argv=inner, workdir="/workspace", network="deny", limits=self._limits)
        return build_remote_launch(backend_id=self._transport.backend_id, request=request)


def _is_loopback(host: str) -> bool:
    """True only for a LITERAL loopback IP (127.0.0.0/8 or ::1).

    A hostname — including ``localhost`` — is deliberately rejected: trusting a
    name means trusting the resolver, and a poisoned or misconfigured resolver
    could point ``localhost`` at a public address. A self-hosted sidecar is always
    reachable at a literal loopback IP, so requiring one closes the DNS-trust hole.
    """
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class _DenyRedirect(_urlrequest.HTTPRedirectHandler):
    """Refuse HTTP redirects. The base URL is validated as loopback once at
    construction; following a 3xx could bounce us (and a request payload) to
    another host, defeating the loopback guarantee. Raising URLError surfaces as
    a normal transport failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]  # noqa: ANN001, ARG002
        raise URLError(f"loopback-exec transport refuses an off-host redirect to {newurl!r}")


class LoopbackExecTransport:
    """Reference ``RemoteExecTransport``: a self-hosted exec sidecar on loopback.

    The sidecar speaks the Piston-style seam over HTTP: ``POST /exec`` with the
    request payload, returning ``{exit_code, stdout, stderr, timed_out}``. It is
    LOOPBACK-ONLY by construction: the constructor accepts only a literal loopback
    IP, and the default opener uses neither an HTTP proxy nor redirects — an SSRF
    guard and the project's egress boundary in one. Because a generic sidecar's
    isolation is opaque to an HTTP client, it attests every OS control it cannot
    prove as UNKNOWN, so it can honestly back only ``workspace_only`` nodes; a
    backend-specific transport (a real E2B / NemoClaw client) attests the controls
    it truly knows its backend enforces.

    ``opener`` is a test seam for injecting a fake HTTP client; an injected opener
    is trusted to preserve the loopback guarantee (no proxies, no redirects), and
    production always uses the safe default built here.
    """

    backend_id = "loopback-exec"

    def __init__(self, *, base_url: str = "http://127.0.0.1:2000", timeout_s: float = 5.0, opener=None) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not _is_loopback(parsed.hostname):
            raise ValueError("LoopbackExecTransport targets a literal loopback IP endpoint only")
        if type(timeout_s) is bool or not isinstance(timeout_s, (int, float)) or not (0 < timeout_s <= 60):
            raise ValueError("timeout_s must be in (0, 60]")
        self._base = base_url.rstrip("/")
        self._timeout = float(timeout_s)
        # Ignore any HTTP(S)_PROXY env (a proxy would route the request off-loopback)
        # and refuse redirects (a 3xx must not bounce us off the loopback host).
        self._opener = opener if opener is not None else _urlrequest.build_opener(
            _DenyRedirect(), _urlrequest.ProxyHandler({}),
        )

    def availability(self) -> tuple[bool, str]:
        try:
            req = _urlrequest.Request(self._base + "/healthz", method="GET")
            with self._opener.open(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                return (False, f"exec sidecar /healthz returned {status}")
            return (True, "")
        except (URLError, OSError, ValueError) as exc:
            return (False, f"self-hosted exec sidecar not reachable on loopback ({type(exc).__name__})")

    def attested_controls(
        self, *, tier: IsolationLevel, network_mode: NetworkMode,
    ) -> EnforcedControls:
        # A generic exec sidecar is opaque: this transport is only an HTTP client
        # and cannot PROVE what the sidecar enforces, so every OS control it cannot
        # verify is UNKNOWN (never ENFORCED — that would be an over-claim). It can
        # assert only two negatives: the node runs on THIS host (no own kernel) and
        # there is no authorized-egress proxy. Honestly, then, it can back only
        # workspace_only nodes; a backend-specific transport (a real E2B or NemoClaw
        # client) attests the controls it actually knows its backend enforces.
        return EnforcedControls(
            net=Control.UNKNOWN,
            fs_write=Control.UNKNOWN,
            fs_read=Control.UNKNOWN,
            pid=Control.UNKNOWN,
            user=Control.UNKNOWN,
            kernel=Control.NOT_ENFORCED,
            egress=Control.NOT_ENFORCED,
            notes=("generic self-hosted exec sidecar: isolation is opaque to the client, so "
                   "unverifiable controls are UNKNOWN; it shares this host's kernel",),
        )

    def submit(self, request: RemoteExecRequest) -> RemoteExecResult:
        body = json.dumps(request.to_payload()).encode("utf-8")
        req = _urlrequest.Request(
            self._base + "/exec", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read(request.limits.output_bytes + 1)
        except (URLError, OSError) as exc:
            raise RemoteExecError(f"exec sidecar request failed: {type(exc).__name__}") from exc
        if len(raw) > request.limits.output_bytes:
            raise RemoteExecError("remote response exceeded output_bytes cap")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteExecError("remote response was not valid JSON") from exc
        if not isinstance(data, dict):
            raise RemoteExecError("remote response must be a JSON object")
        try:
            exit_code = int(data["exit_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteExecError("remote response missing an integer exit_code") from exc
        stdout, stderr = data.get("stdout", ""), data.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise RemoteExecError("remote stdout/stderr must be strings")
        return RemoteExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=bool(data.get("timed_out", False)),
            attested_controls=self.attested_controls(
                tier=IsolationLevel.WORKSPACE_ONLY,
                network_mode=NetworkMode.DENY if request.network == "deny" else NetworkMode.ALLOWLIST,
            ),
        )
