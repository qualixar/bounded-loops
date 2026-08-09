"""Execute a planned node under a real OS sandbox and promote its artifacts.

This is the concrete ``NodeWorkerPort`` that makes ``bl graph run`` actually
run work — WITHOUT Docker where the host offers a native sandbox. It owns only
sandboxed execution and output promotion; it does not decide whether the output
is *good* (the controller still invokes a separate, independent gate after this
worker returns). That preserves the core invariant: a producer never grades its
own node.

Flow for one node attempt:

1. Re-validate the execution envelope (defense in depth).
2. Ask the capability matrix which mechanism can honestly deliver the node's
   isolation here; fail closed if none can.
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
import math
import os
import re
from pathlib import Path
from typing import Callable, Mapping, Protocol, cast
from uuid import uuid4

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn
from bounded_loops.domain.models import TurnState
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.sandbox import wrap_argv
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    validate_execution_envelope,
)
from bounded_loops.graph.application.run_graph import WorkerResult
from bounded_loops.graph.application.workspace_promotion import (
    ArtifactWriterPort,
    WorkspaceInput,
    WorkspacePromotionPolicy,
    materialize_workspace_inputs,
    promote_workspace_outputs,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024
_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
_DEFAULT_DEADLINE_S = 30.0
_UNSAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")


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
    artifact_store: LocalArtifactStore
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
    _mechanism_used: dict[str, str] = field(default_factory=dict, compare=False)

    def mechanism_for(self, node_id: str) -> str | None:
        """The sandbox mechanism actually used for *node_id* (for receipts)."""
        return self._mechanism_used.get(node_id)

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
    ) -> WorkerResult:
        validate_execution_envelope(plan, node, envelope)
        mechanism, reason = self.capabilities.select_mechanism(envelope.isolation, envelope.network_mode)
        if mechanism is None:
            raise GraphIntegrityError(f"node {node.node_id!r} cannot be sandboxed here: {reason}")

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

        if spec.inputs:
            materialize_workspace_inputs(
                inputs, spec.inputs, ArtifactAccess(self.organization_id, self.project_id), self.artifact_store,
            )

        env = build_subprocess_env()
        env["HOME"] = str(home.resolve())
        env["TMPDIR"] = str(tmp.resolve())
        env["BL_GRAPH_INPUTS"] = str(inputs.resolve())
        env["BL_GRAPH_OUTPUTS"] = str(outputs.resolve())

        wrapped = wrap_argv(
            mechanism,
            inner_argv=spec.argv,
            workspace=outputs,
            home=home,
            tmpdir=tmp,
            network_mode=envelope.network_mode,
            image=spec.container_image,
        )

        deadline_s = (node.hard_deadline_ms / 1000.0) if node.hard_deadline_ms else self.default_deadline_s
        preexec = _rlimit_preexec(int(math.ceil(deadline_s)) + 1) if self.apply_rlimits else None
        turn = ProcessTurn.start(
            wrapped,
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

        policy = WorkspacePromotionPolicy(
            organization_id=self.organization_id,
            project_id=self.project_id,
            producer_attempt="1",
            declared_outputs=dict(spec.declared_outputs),
            max_file_bytes=self.max_file_bytes,
            sensitivity=self.sensitivity,
            retention_class=self.retention_class,
        )
        # The store's put_many accepts BinaryIO; promotion needs only a
        # read()-able source (it streams _BoundedReader). The store satisfies the
        # writer port structurally at runtime; cast bridges the narrower nominal type.
        records = promote_workspace_outputs(
            outputs, policy, cast(ArtifactWriterPort, self.artifact_store),
        )
        digests = tuple(record.digest for record in records)
        self._mechanism_used[node.node_id] = mechanism.value

        route, transport = self._route_for(plan, node)
        return WorkerResult(digests, route, transport)

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
