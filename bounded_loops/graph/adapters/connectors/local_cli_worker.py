"""Run an admitted local-CLI connector node by executing the user's own agent CLI (RC Mode 1).

The DEFAULT "run freely" posture (the Open Design model): the CLI runs with the user's REAL
environment — so its subscription login and all its tools work and real agent work completes —
with the network open, in a per-run working directory, bounded only by the node's deadline. Its
stdout reply is captured as the node's content-addressed output artifact.

No credential is read, handled, or logged here: the CLI authenticates itself out-of-band via its
own config (we only choose subscription — print — mode, never an API-key mode). This is NOT the
enterprise egress firewall (that is the opt-in RC-LOCKDOWN tier); it is the trusted-local default,
gated to the compiler-admitted ``local_cli`` transport so only an admitted connector runs this way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
from typing import Mapping, Protocol
from uuid import uuid4

from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn
from bounded_loops.domain.models import TurnState
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkMode
from bounded_loops.graph.application.run_graph import WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactPolicy
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_LOCAL_CLI_TRANSPORT = "local_cli"
_DEFAULT_DEADLINE_S = 120.0
_DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_UNSAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")
# Mask long token-like runs (API keys / bearer tokens) a CLI might print in an error line, while
# leaving human-readable diagnostics ("OAuth session expired") intact.
_SECRET_TOKEN = re.compile(r"(?i)(?:bearer\s+)?[A-Za-z0-9_\-]{24,}")


def _redact_secrets(text: str) -> str:
    return _SECRET_TOKEN.sub("[REDACTED]", text)


@dataclass(frozen=True)
class CliProfile:
    """How to invoke one agent CLI in subscription (print) mode.

    ``prompt_via`` selects how the prompt reaches the CLI: ``"stdin"`` (piped) or ``"arg"``
    (appended as the final positional argument). CLIs differ — ``claude -p`` reads stdin, while
    ``codex``/``grok``/``muse``/``agy`` take the prompt as an argument — so it is explicit here.
    """

    binary: str
    args: tuple[str, ...] = ()
    prompt_via: str = "stdin"
    unset_env: tuple[str, ...] = ()
    set_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.prompt_via not in ("stdin", "arg"):
            raise GraphValidationError("cli_profile", "/prompt_via", "prompt_via must be 'stdin' or 'arg'")


@dataclass(frozen=True)
class CliInvocation:
    """The CLI a node should run + the prompt to feed it."""

    profile: CliProfile
    prompt: str


class LocalCliConnectorPort(Protocol):
    """Deployment-owned: resolve which CLI a node runs and the prompt, from the plan/node."""

    def resolve(self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope) -> CliInvocation: ...


# Subscription (print) mode — never `claude --bare` (which requires an API key and ignores the
# subscription login). Invocations confirmed live on this host (2026-08-11): `claude -p` reads the
# prompt on stdin (CLAUDE_CONFIG_DIR cleared so it uses the default subscription login); the others
# take the prompt as a positional argument, and `codex exec` needs --skip-git-repo-check to run
# outside a git repo.
CLI_PROFILES: Mapping[str, CliProfile] = {
    "claude": CliProfile("claude", ("-p",), prompt_via="stdin", unset_env=("CLAUDE_CONFIG_DIR",)),
    "codex": CliProfile("codex", ("exec", "--skip-git-repo-check"), prompt_via="arg"),
    "grok": CliProfile("grok", ("-p",), prompt_via="arg"),
    "muse": CliProfile("muse", ("exec",), prompt_via="arg"),
    "agy": CliProfile("agy", ("-p",), prompt_via="arg"),
}


@dataclass(frozen=True)
class StaticCliResolver:
    """Resolve every local-CLI node to one configured invocation (tests, demo, reference wiring)."""

    invocation: CliInvocation

    def resolve(self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope) -> CliInvocation:
        return self.invocation


class LocalCliConnectorWorker:
    """A ``NodeWorkerPort`` that runs an admitted local-CLI connector's CLI, freely, deadline-bounded."""

    def __init__(
        self,
        *,
        identity: GraphRunIdentity,
        artifact_store: LocalArtifactStore,
        resolver: LocalCliConnectorPort,
        workspace_root: Path,
        organization_id: str,
        project_id: str,
        default_deadline_s: float = _DEFAULT_DEADLINE_S,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        sensitivity: str = "internal",
        retention_class: str = "standard",
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._identity = identity
        self._store = artifact_store
        self._resolver = resolver
        self._workspace_root = workspace_root
        self._organization_id = organization_id
        self._project_id = project_id
        self._default_deadline_s = default_deadline_s
        self._max_output_bytes = max_output_bytes
        self._sensitivity = sensitivity
        self._retention_class = retention_class
        self._environ = environ

    def execute(self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope) -> WorkerResult:
        route, transport = self._route_for(plan, node)
        if transport != _LOCAL_CLI_TRANSPORT:
            raise GraphIntegrityError(f"node {node.node_id!r} is not an admitted local-CLI connector")
        # Defense in depth: the run-freely posture only applies under an OPEN-network envelope
        # (the controller validates the envelope; a DENY/ALLOWLIST envelope here is a wiring error).
        if envelope.network_mode is not NetworkMode.OPEN:
            raise GraphIntegrityError("a local-CLI connector must run under an open-network envelope")
        invocation = self._resolver.resolve(plan=plan, node=node, envelope=envelope)
        if not isinstance(invocation, CliInvocation):
            raise GraphIntegrityError("local-CLI resolver must return a CliInvocation")
        binary = shutil.which(invocation.profile.binary)
        if binary is None:
            raise GraphIntegrityError(f"agent CLI {invocation.profile.binary!r} is not installed on this host")

        workdir = self._workdir(node)
        deadline = (node.hard_deadline_ms / 1000.0) if node.hard_deadline_ms else self._default_deadline_s
        argv = [binary, *invocation.profile.args]
        stdin_text: str | None = None
        if invocation.profile.prompt_via == "arg":
            argv.append(invocation.prompt)
        else:
            stdin_text = invocation.prompt
        turn = ProcessTurn.start(
            argv,
            cwd=workdir,
            env=self._child_env(invocation.profile),
            output_limit_bytes=self._max_output_bytes,
            input_text=stdin_text,
        )
        result = turn.wait(timeout_s=deadline)
        if result.state is not TurnState.COMPLETED:
            raise GraphIntegrityError(f"local-CLI node {node.node_id!r} did not complete ({result.state.value})")
        if result.returncode != 0:
            # A CLI that exits non-zero (e.g. an expired subscription login) is a CLOSED node
            # failure, never a silent empty "reply". The hint is a bounded, non-secret diagnostic.
            combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            hint = next((line for line in combined.splitlines() if line.strip()), "no output")
            raise GraphIntegrityError(
                f"local-CLI node {node.node_id!r} exited {result.returncode}: {_redact_secrets(hint[:200])}"
            )

        reply = (result.stdout or "").encode("utf-8")
        digest = self._store.put(BytesIO(reply), self._policy()).digest
        return WorkerResult((digest,), route, transport)

    def _child_env(self, profile: CliProfile) -> dict[str, str]:
        # Run freely: the CLI inherits the operator's real environment so its subscription and
        # tools work, minus any keys the profile clears (e.g. CLAUDE_CONFIG_DIR so `claude`
        # uses the default subscription login), plus any the profile sets.
        env = dict(self._environ if self._environ is not None else os.environ)
        for key in profile.unset_env:
            env.pop(key, None)
        env.update(profile.set_env)
        return env

    def _workdir(self, node: PlannedNode) -> Path:
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        base = self._workspace_root / _safe(self._identity.run_id) / f"{_safe(node.node_id)}-{uuid4().hex}"
        base.mkdir(parents=True, exist_ok=False)
        return base

    def _policy(self) -> ArtifactPolicy:
        return ArtifactPolicy(
            organization_id=self._organization_id,
            project_id=self._project_id,
            producer_attempt="1",
            media_type="text/plain",
            sensitivity=self._sensitivity,
            retention_class=self._retention_class,
        )

    def _route_for(self, plan: ExecutionPlan, node: PlannedNode) -> tuple[ResolvedRoute | None, str | None]:
        if node.binding_id is None:
            return (None, None)
        binding = next((b for b in plan.connection_bindings if b.binding_id == node.binding_id), None)
        if binding is None:
            return (None, None)
        route = ResolvedRoute(
            binding.provider_id, binding.model_target, binding.region, binding.fallback, binding.route_policy_digest,
        )
        return (route, binding.transport)


def _safe(value: str) -> str:
    cleaned = _UNSAFE_PATH_COMPONENT.sub("_", value)[:64]
    return cleaned if cleaned not in ("", ".", "..") else "_"
