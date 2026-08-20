"""Execute a planned node under a real OS sandbox and promote its artifacts.

This is the concrete ``NodeWorkerPort`` that makes ``bl graph run`` actually
run work — WITHOUT Docker where the host offers a native sandbox. It owns only
sandboxed execution and output promotion; it does not decide whether the output
is *good* (the controller still invokes a separate, independent gate after this
worker returns). That preserves the core invariant: a producer never grades its
own node.

Flow for one node attempt:

1. Re-validate the execution envelope (defense in depth).
2. Ask the isolation-provider registry which provider can honestly deliver the
   node's isolation tier here; fail closed if none can.
3. Build a private, per-node workspace (isolated ``outputs/`` ``inputs/``
   ``home/`` ``tmp/``) and materialize declared input artifacts read-only.
4. Wrap the resolved argv in the selected sandbox (network denied, writes
   confined) and run it under the controller-owned bounded process lifecycle
   with an isolated ``HOME`` / ``TMPDIR`` (closes the real-HOME env leak) and
   best-effort CPU / open-file rlimits.
5. Promote exactly the declared outputs into the content-addressed store and
   return their digests. Any undeclared, missing, or oversized output fails
   closed inside the descriptor-safe promotion path.

Known limitation (disclosed, not hidden): under Seatbelt a node that
double-forks + ``setsid`` can outlive its wall-clock deadline as a background
process. It stays fully sandboxed — the inherited profile still denies network
and confines writes, and it cannot corrupt the content-addressed store — but it
can evade the deadline. Bubblewrap's PID namespace prevents this; the Seatbelt
gap is a liveness limit, not an isolation escape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import os
import re
from pathlib import Path
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn
from bounded_loops.domain.models import TurnState
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.egress_proxy import LoopbackEgressProxy
from bounded_loops.graph.adapters.enforcement.provider import EnforcedControls
from bounded_loops.graph.adapters.enforcement.providers.remote_exec import RemoteExecTransport
from bounded_loops.graph.adapters.enforcement.registry import IsolationProviderRegistry, default_registry
from bounded_loops.graph.adapters.enforcement.sandbox import SEATBELT_BINARY
from bounded_loops.graph.application.graph_ports import ArtifactStorePort
from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    NetworkMode,
    validate_execution_envelope,
)
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.application.workspace_promotion import (
    WorkspaceInput,
    WorkspacePromotionPolicy,
    materialize_workspace_inputs,
    promote_workspace_outputs,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess, attempt_provenance
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_LOGGER = logging.getLogger(__name__)
_DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024
_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
_DEFAULT_DEADLINE_S = 30.0
_UNSAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")
# The concrete OS mechanism that actually launched, read from the wrapped
# ``argv[0]`` (ground truth) — the human/receipt mechanism label that sits
# alongside the provider id and the per-dimension controls.
_MECHANISM_BY_ARGV0 = {SEATBELT_BINARY: "seatbelt", "unshare": "unshare_net", "docker": "docker"}


def _egress_log_sink(node_id: str) -> Callable[..., None]:
    """Log sink for the RC-LOCKDOWN egress proxy: a DENY is a security-relevant event (WARNING), an
    allowed tunnel is DEBUG. Every decision is surfaced to the logger — never silently dropped."""

    def _sink(*, allowed: bool, destination: str, reason: str) -> None:
        if allowed:
            _LOGGER.debug("egress-proxy ALLOW node=%s dest=%s (%s)", node_id, destination, reason)
        else:
            _LOGGER.warning("egress-proxy DENY node=%s dest=%s (%s)", node_id, destination, reason)

    return _sink


def _safe_component(value: str) -> str:
    """Reduce an identifier to one safe path component (defeats ``..`` and ``/``)."""
    cleaned = _UNSAFE_PATH_COMPONENT.sub("_", value)[:64]
    if cleaned in ("", ".", ".."):
        return "_"
    return cleaned


@dataclass(frozen=True)
class NodeExecutionSpec:
    """The runnable definition of one node, resolved from its package digest.

    ``argv[0]`` must be an absolute interpreter/binary path so execution does not
    depend on PATH resolution inside the sandbox. ``declared_outputs`` maps each
    workspace-relative output path to its media type; the node may produce only
    these files.
    """

    argv: tuple[str, ...]
    declared_outputs: Mapping[str, str]
    inputs: tuple[WorkspaceInput, ...] = ()
    stdin_text: str | None = None
    container_image: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or not all(isinstance(a, str) and a for a in self.argv):
            raise GraphIntegrityError("node execution spec argv must be a non-empty tuple of strings")
        if not os.path.isabs(self.argv[0]):
            raise GraphIntegrityError("node execution spec argv[0] must be an absolute interpreter/binary path")
        if not self.declared_outputs:
            raise GraphIntegrityError("node execution spec must declare at least one output")


class NodeExecutionResolver(Protocol):
    """Resolves a planned node into its runnable spec (backed by an admitted
    package registry in production; injected in tests and the built-in demo)."""

    def resolve(self, node: PlannedNode) -> NodeExecutionSpec: ...


@dataclass(frozen=True)
class SandboxedNodeWorker:
    """A ``NodeWorkerPort`` that runs each node inside a native OS sandbox."""

    identity: GraphRunIdentity
    artifact_store: ArtifactStorePort
    resolver: NodeExecutionResolver
    capabilities: PlatformCapabilities
    workspace_root: Path
    organization_id: str
    project_id: str
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    default_deadline_s: float = _DEFAULT_DEADLINE_S
    sensitivity: str = "internal"
    retention_class: str = "standard"
    apply_rlimits: bool = True
    isolation_registry: IsolationProviderRegistry | None = None
    container_image: str | None = None
    microvm_transport: RemoteExecTransport | None = None
    openshell_transport: RemoteExecTransport | None = None
    _mechanism_used: dict[str, str] = field(default_factory=dict, compare=False)
    _provider_used: dict[str, str] = field(default_factory=dict, compare=False)
    _controls_used: dict[str, EnforcedControls] = field(default_factory=dict, compare=False)
    _registry_cache: dict[str, IsolationProviderRegistry] = field(default_factory=dict, compare=False)

    def mechanism_for(self, node_id: str) -> str | None:
        """The concrete OS mechanism actually launched for *node_id* (seatbelt /
        unshare_net / docker), or the provider id for the floor."""
        return self._mechanism_used.get(node_id)

    def provider_for(self, node_id: str) -> str | None:
        """The isolation provider selected for *node_id* (native / container / …)."""
        return self._provider_used.get(node_id)

    def controls_for(self, node_id: str) -> EnforcedControls | None:
        """The per-dimension controls actually enforced for *node_id* (receipts)."""
        return self._controls_used.get(node_id)

    def _registry_for(self, image: str | None) -> IsolationProviderRegistry:
        """The isolation registry to select from. An injected registry is used
        verbatim; otherwise a default chain is built and cached per container
        image. host_managed is left OFF here — the worker always applies its own
        isolation unless a deployment injects a host-deferring registry."""
        if self.isolation_registry is not None:
            return self.isolation_registry
        key = image if image is not None else (self.container_image or "")
        registry = self._registry_cache.get(key)
        if registry is None:
            registry = default_registry(
                self.capabilities,
                container_image=image if image is not None else self.container_image,
                microvm_transport=self.microvm_transport,
                openshell_transport=self.openshell_transport,
                include_host_managed=False,
            )
            self._registry_cache[key] = registry
        return registry

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int, repair_round: int,
    ) -> WorkerResult:
        validate_execution_envelope(plan, node, envelope)

        spec = self.resolver.resolve(node)
        if not isinstance(spec, NodeExecutionSpec):
            raise GraphIntegrityError("node execution resolver must return a NodeExecutionSpec")

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if self.workspace_root.is_symlink():
            raise GraphIntegrityError("workspace root must not be a symlink")
        # run_id / node_id are reduced to safe single components so a crafted id
        # cannot use `..` or `/` to escape the controller-owned workspace root.
        base = (
            self.workspace_root
            / _safe_component(self.identity.run_id)
            / f"{_safe_component(node.node_id)}-{uuid4().hex}"
        )
        outputs = base / "outputs"
        inputs = base / "inputs"
        home = base / "home"
        tmp = base / "tmp"
        for directory in (outputs, inputs, home, tmp):
            directory.mkdir(parents=True, exist_ok=False)
        # Defense in depth: a symlinked ancestor must not let a per-node directory
        # (bound writable / whitelisted in the sandbox profile) resolve outside root.
        resolved_root = os.path.realpath(self.workspace_root)
        for directory in (outputs, inputs, home, tmp):
            if os.path.islink(directory) or not os.path.realpath(directory).startswith(resolved_root + os.sep):
                raise GraphIntegrityError("per-node workspace escaped the workspace root")

        # Select an isolation PROVIDER that can honestly deliver this node's tier
        # here, or fail closed. The engine never hardcodes a mechanism; the chosen
        # provider publishes the real per-dimension controls the receipt records.
        # The live workspace is passed so a probe-backed provider (when enabled)
        # tests exactly the confinement the node would inherit.
        registry = self._registry_for(spec.container_image)
        try:
            outcome = registry.select(
                tier=envelope.isolation, network_mode=envelope.network_mode, workspace=outputs,
            )
        except GraphValidationError as exc:
            raise GraphIntegrityError(
                f"node {node.node_id!r} cannot be isolated here: {exc.message}"
            ) from exc
        provider = outcome.provider
        selection = outcome.selection

        if spec.inputs:
            materialize_workspace_inputs(
                inputs, spec.inputs, ArtifactAccess(self.organization_id, self.project_id), self.artifact_store,
            )

        env = build_subprocess_env()
        env["HOME"] = str(home.resolve())
        env["TMPDIR"] = str(tmp.resolve())
        env["BL_GRAPH_INPUTS"] = str(inputs.resolve())
        env["BL_GRAPH_OUTPUTS"] = str(outputs.resolve())

        # RC-LOCKDOWN: for an ALLOWLIST node, start the loopback egress proxy for EXACTLY the admitted
        # destinations, point the process at it, and OS-cage the process so it can reach NOTHING but
        # the proxy. The proxy enforces the destination allowlist + SSRF guard; the cage stops a
        # compromised process from bypassing it. Started INSIDE the try so the finally tears it down
        # even if its own start (or anything after) raises — never a leaked listener (dual-audit D2).
        egress_proxy: LoopbackEgressProxy | None = None
        try:
            egress_proxy_port: int | None = None
            if envelope.network_mode is NetworkMode.ALLOWLIST:
                egress_proxy = LoopbackEgressProxy(
                    allowed=tuple(envelope.network_destinations),
                    log=_egress_log_sink(node.node_id),
                )
                egress_proxy_port = egress_proxy.start()
                proxy_url = f"http://127.0.0.1:{egress_proxy_port}"
                for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                    env[var] = proxy_url

            launch = provider.build_launch(
                inner_argv=spec.argv,
                workspace=outputs,
                home=home,
                tmpdir=tmp,
                tier=envelope.isolation,
                network_mode=envelope.network_mode,
                egress_proxy_port=egress_proxy_port,
            )
            if launch.kind != "local":
                # Remote isolation (microvm / openshell) needs the C1 remote-staging
                # bridge: ship the content-addressed workspace to the backend, run, and
                # fetch declared outputs back through the egress broker. Provider
                # SELECTION and control publishing are wired here in E3; remote dispatch
                # lands with C1. Fail closed until then — never silently drop a remote
                # node onto the local host.
                raise GraphIntegrityError(
                    f"node {node.node_id!r} selected the {selection.provider_id!r} remote isolation provider, "
                    "whose execution bridge is delivered in C1 (remote workspace staging + egress broker)"
                )

            deadline_s = (node.hard_deadline_ms / 1000.0) if node.hard_deadline_ms else self.default_deadline_s
            preexec = _rlimit_preexec(int(math.ceil(deadline_s)) + 1) if self.apply_rlimits else None
            turn = ProcessTurn.start(
                list(launch.argv),
                cwd=outputs,
                env=env,
                output_limit_bytes=self.max_output_bytes,
                input_text=spec.stdin_text,
                preexec_fn=preexec,
            )
            result = turn.wait(timeout_s=deadline_s)
            if result.state is not TurnState.COMPLETED:
                raise GraphIntegrityError(
                    f"node {node.node_id!r} did not complete within its deadline ({result.state.value})"
                )
        finally:
            if egress_proxy is not None:
                egress_proxy.stop()

        policy = WorkspacePromotionPolicy(
            organization_id=self.organization_id,
            project_id=self.project_id,
            producer_attempt=attempt_provenance(attempt, repair_round),
            declared_outputs=dict(spec.declared_outputs),
            max_file_bytes=self.max_file_bytes,
            sensitivity=self.sensitivity,
            retention_class=self.retention_class,
        )
        # No cast needed since P3: ``artifact_store`` is an ``ArtifactStorePort``, which
        # already extends ``ArtifactWriterPort``. The cast that used to sit here bridged a
        # nominal gap the seam removes — and a cast is exactly how a real mismatch would
        # have gone unnoticed.
        records = promote_workspace_outputs(outputs, policy, self.artifact_store)
        digests = tuple(record.digest for record in records)
        self._mechanism_used[node.node_id] = _MECHANISM_BY_ARGV0.get(launch.argv[0], selection.provider_id)
        self._provider_used[node.node_id] = selection.provider_id
        self._controls_used[node.node_id] = selection.controls

        route, transport = self._route_for(plan, node)
        return WorkerResult(
            digests,
            route,
            transport,
            isolation_provider_id=selection.provider_id,
            enforced_controls=selection.controls.as_dict(),
        )

    def _route_for(
        self, plan: ExecutionPlan, node: PlannedNode,
    ) -> tuple[ResolvedRoute | None, str | None]:
        if node.binding_id is None:
            return (None, None)
        binding = next((b for b in plan.connection_bindings if b.binding_id == node.binding_id), None)
        if binding is None:
            return (None, None)
        route = ResolvedRoute(
            binding.provider_id,
            binding.model_target,
            binding.region,
            binding.fallback,
            binding.route_policy_digest,
        )
        return (route, binding.transport)


def _rlimit_preexec(cpu_seconds: int) -> Callable[[], None] | None:
    """A child-side rlimit applier (POSIX only). Each soft value is clamped to the
    inherited hard limit so it applies reliably rather than raising a non-root
    "cannot raise hard limit" error — the published control is therefore real, not
    merely attempted. Address space is left unbounded (Python reserves large
    virtual mappings); memory / pid caps on the native path need cgroups and are
    intentionally not claimed."""
    try:
        import resource
    except Exception:
        return None

    def _clamp(which: int, desired_soft: int, desired_hard: int) -> None:
        try:
            _, hard = resource.getrlimit(which)
        except Exception:
            return
        top = desired_hard if hard == resource.RLIM_INFINITY else min(desired_hard, hard)
        soft = min(desired_soft, top)
        try:
            resource.setrlimit(which, (soft, top))
        except Exception:
            pass

    def _apply() -> None:
        _clamp(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1)
        _clamp(resource.RLIMIT_NOFILE, 256, 256)

    return _apply
