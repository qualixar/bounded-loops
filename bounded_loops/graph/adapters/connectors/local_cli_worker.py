"""Run an admitted local-CLI connector node by executing the user's own agent CLI (RC Mode 1),
freely under OPEN, or CAGED under ALLOWLIST (DECISION CHANGE: real cage, not a refusal).

The DEFAULT "run freely" posture (the Open Design model): the CLI runs with the user's REAL
environment — so its subscription login and all its tools work and real agent work completes —
with the network open, in a per-run working directory, bounded only by the node's deadline. Its
stdout reply is captured as the node's content-addressed output artifact.

The ALLOWLIST-caged posture (opt-in, egress_posture_policy.py) reuses the SAME Seatbelt SBPL
builder + loopback egress proxy ``SandboxedNodeWorker``/the ``https`` transport already use
(``sandbox.py`` / ``egress_proxy.py``) — no new cage is invented here. It differs from
``SandboxedNodeWorker``'s own usage in exactly one way, deliberately: filesystem writes are
confined to the workdir PLUS the operator's REAL ``HOME``/``TMPDIR`` (never an isolated empty
``HOME``), because the whole point of "logged-in CLI, restricted egress" is that the subscription
login still works. Reads are never confined by this profile — matching every OTHER caller of it —
so this is not a filesystem-confinement regression from today's fully-open OPEN mode, it is a net
new NETWORK restriction on top of unchanged (broad) filesystem trust for an already-trusted local
tool. See ``docs/graph-egress-posture.md`` for the full design writeup.

No credential is read, handled, or logged here: the CLI authenticates itself out-of-band via its
own config (we only choose subscription — print — mode, never an API-key mode). Gated to the
compiler-admitted ``local_cli`` transport so only an admitted connector runs this way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn
from bounded_loops.domain.models import TurnState
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities, probe_platform
from bounded_loops.graph.adapters.enforcement.egress_proxy import LoopbackEgressProxy
from bounded_loops.graph.adapters.enforcement.sandbox import build_seatbelt_allowlist_profile, seatbelt_argv
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkMode
from bounded_loops.graph.application.run_graph import WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactPolicy
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_LOGGER = logging.getLogger(__name__)
_LOCAL_CLI_TRANSPORT = "local_cli"
_DEFAULT_DEADLINE_S = 120.0
_DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_UNSAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")
# Mask long token-like runs (API keys / bearer tokens) a CLI might print in an error line, while
# leaving human-readable diagnostics ("OAuth session expired") intact.
_SECRET_TOKEN = re.compile(r"(?i)(?:bearer\s+)?[A-Za-z0-9_\-]{24,}")
_PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def _redact_secrets(text: str) -> str:
    return _SECRET_TOKEN.sub("[REDACTED]", text)


def _egress_log_sink(node_id: str) -> Callable[..., None]:
    """Log sink for the RC-LOCKDOWN egress proxy: a DENY is security-relevant (WARNING), an
    allowed tunnel is DEBUG — mirrors ``sandboxed_worker.py``'s own sink, kept local rather than
    importing a private cross-module name for a trivial, worker-specific logger call."""

    def _sink(*, allowed: bool, destination: str, reason: str) -> None:
        if allowed:
            _LOGGER.debug("egress-proxy ALLOW node=%s dest=%s (%s)", node_id, destination, reason)
        else:
            _LOGGER.warning("egress-proxy DENY node=%s dest=%s (%s)", node_id, destination, reason)

    return _sink


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
        capabilities: PlatformCapabilities | None = None,
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
        # Mirrors build_enforcer()'s / decide_egress_posture()'s "probe unless injected"
        # convention: production callers get a real platform probe for free; tests inject a
        # fixed PlatformCapabilities so the ALLOWLIST-without-a-cage path is deterministic.
        self._capabilities = capabilities if capabilities is not None else probe_platform()

    def execute(self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope) -> WorkerResult:
        route, transport = self._route_for(plan, node)
        if transport != _LOCAL_CLI_TRANSPORT:
            raise GraphIntegrityError(f"node {node.node_id!r} is not an admitted local-CLI connector")
        # The controller validates the envelope; only OPEN (run freely) and ALLOWLIST (run
        # caged) are supported local_cli postures — DENY (or anything else) here is a wiring
        # error, never silently treated as one of the two supported modes.
        if envelope.network_mode not in (NetworkMode.OPEN, NetworkMode.ALLOWLIST):
            raise GraphIntegrityError(
                "a local-CLI connector must run under an open-network or allowlist-network "
                f"envelope, got {envelope.network_mode.value}"
            )
        invocation = self._resolver.resolve(plan=plan, node=node, envelope=envelope)
        if not isinstance(invocation, CliInvocation):
            raise GraphIntegrityError("local-CLI resolver must return a CliInvocation")
        binary = shutil.which(invocation.profile.binary)
        if binary is None:
            raise GraphIntegrityError(f"agent CLI {invocation.profile.binary!r} is not installed on this host")

        workdir = self._workdir(node)
        deadline = (node.hard_deadline_ms / 1000.0) if node.hard_deadline_ms else self._default_deadline_s
        inner_argv = [binary, *invocation.profile.args]
        stdin_text: str | None = None
        if invocation.profile.prompt_via == "arg":
            inner_argv.append(invocation.prompt)
        else:
            stdin_text = invocation.prompt

        env = self._child_env(invocation.profile)
        argv: list[str] = inner_argv
        # Declared BEFORE any step that can raise, so a partially-built cage (e.g. the proxy
        # started but the Seatbelt profile failed to build) is still stopped in `finally` —
        # never a leaked listener.
        proxy: LoopbackEgressProxy | None = None
        try:
            if envelope.network_mode is NetworkMode.ALLOWLIST:
                if not (self._capabilities.seatbelt and self._capabilities.egress_proxy):
                    raise GraphIntegrityError(
                        f"node {node.node_id!r}: ALLOWLIST egress posture requires the Seatbelt "
                        "loopback-proxy cage, which this host cannot deliver — refusing to run "
                        "rather than silently falling back to open egress"
                    )
                proxy = LoopbackEgressProxy(allowed=envelope.network_destinations, log=_egress_log_sink(node.node_id))
                proxy_port = proxy.start()
                argv = self._caged_argv(
                    node=node, inner_argv=inner_argv, workdir=workdir, env=env, proxy_port=proxy_port,
                )
            turn = ProcessTurn.start(
                argv, cwd=workdir, env=env, output_limit_bytes=self._max_output_bytes, input_text=stdin_text,
            )
            result = turn.wait(timeout_s=deadline)
        finally:
            if proxy is not None:
                proxy.stop()

        if result.state is not TurnState.COMPLETED:
            raise GraphIntegrityError(f"local-CLI node {node.node_id!r} did not complete ({result.state.value})")
        if result.returncode != 0:
            # A CLI that exits non-zero (e.g. an expired subscription login, or its own network
            # call blocked by the cage) is a CLOSED node failure, never a silent empty "reply".
            # The hint is a bounded, non-secret diagnostic.
            combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            hint = next((line for line in combined.splitlines() if line.strip()), "no output")
            raise GraphIntegrityError(
                f"local-CLI node {node.node_id!r} exited {result.returncode}: {_redact_secrets(hint[:200])}"
            )

        reply = (result.stdout or "").encode("utf-8")
        digest = self._store.put(BytesIO(reply), self._policy()).digest
        return WorkerResult((digest,), route, transport)

    def _caged_argv(
        self, *, node: PlannedNode, inner_argv: list[str], workdir: Path, env: dict[str, str], proxy_port: int,
    ) -> list[str]:
        """Build the Seatbelt-caged argv for one ALLOWLIST-network local-CLI launch.

        Reuses the SAME SBPL builder (``sandbox.py::build_seatbelt_allowlist_profile``) and
        loopback-proxy wiring pattern ``SandboxedNodeWorker``/the ``https`` transport already
        use — no new cage mechanism. Filesystem writes are confined to *workdir* PLUS the
        operator's REAL ``HOME``/``TMPDIR`` (resolved from *env*, which already reflects
        ``_child_env``'s ``unset_env``/``set_env`` — never an isolated empty ``HOME``, which
        would break the subscription login this whole posture exists to keep working). Reads
        are never confined by this profile, matching every other caller of it. ``env`` is
        mutated in place (a freshly-built, per-call dict with a single consumer — the
        subprocess about to be spawned — never shared/aliased state) to add the SAME 6 proxy
        variables ``SandboxedNodeWorker`` sets, so a proxy-aware HTTP client in the CLI reaches
        the right place instead of failing with a confusing EPERM; the Seatbelt cage itself,
        not this env wiring, is the actual enforcement boundary.
        """
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        for var in _PROXY_ENV_VARS:
            env[var] = proxy_url
        # A pre-existing NO_PROXY/no_proxy could make a cooperating client SKIP the proxy for a
        # matching host and attempt a direct connect instead — Seatbelt EPERMs that either way
        # (not a bypass: the cage, not this env wiring, is the enforcement), but clearing it
        # gives a clean "the tool used the proxy and got denied by the allowlist" fail mode
        # instead of a confusing "direct connection refused" one.
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
        home = Path(env.get("HOME") or str(Path.home()))
        tmp = Path(env.get("TMPDIR") or tempfile.gettempdir())
        # The child's ACTUAL env must agree with what the Seatbelt profile allows — if HOME/
        # TMPDIR were absent from env (e.g. a minimal PATH-only environ), the child would see
        # no HOME at all while the cage silently allowed writes to Path.home(): a consistency
        # gap, not just a cosmetic one (a tool that falls back to some OTHER HOME resolution
        # when the env var is unset could try to write somewhere the cage denies).
        env["HOME"] = str(home)
        env["TMPDIR"] = str(tmp)
        try:
            profile = build_seatbelt_allowlist_profile(writable=(workdir, home, tmp), proxy_port=proxy_port)
        except ValueError as exc:
            raise GraphIntegrityError(
                f"could not build the egress cage for node {node.node_id!r}: {exc}"
            ) from exc
        return seatbelt_argv(profile, inner_argv)

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
