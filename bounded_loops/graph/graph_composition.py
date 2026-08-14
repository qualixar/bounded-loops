"""Real ``bl graph run --execute <manifest>`` — run an admitted connector graph.

Two connector modes are supported:

* **local_cli** (existing):  Run the user's own subscription agent CLI, OPEN by default or
  Seatbelt-caged under ALLOWLIST (egress_posture_policy.py). No credential is read here.

* **https / BYOK** (new):  Run a frontier-model API connector through the real
  ``HttpConnectorForwarder`` (RB), the no-secret ``ConnectorInvoker`` path, and a
  deployment-supplied ``AdmittedConnectionRecord`` that carries the endpoint + credential
  env-var name.  The ``AdmittedConnectionRequestBuilder`` issues a REAL ``ExecutionGrant``
  from the supplied record — never from the plan/binding alone (anti-dummy rule).

A single graph may mix ``local_cli`` and ``https`` nodes; each is routed to the right
sub-worker by its binding transport via ``_ByokDispatchWorker``.

Mode surface: ``admitted_connections`` is a ``Mapping[connection_id, AdmittedConnectionRecord]``
passed to ``execute_graph_run``.  The CLI exposes this as ``--admitted <json-map-file>``.
Preflight fails closed if an ``https`` node has no matching admitted record.

The per-node prompt is RUN-TIME input (``node_id -> prompt``), never baked into the portable graph.

``build_execution_controller`` is the shared assembly helper reused by ``LocalGraphRuntimeFacade``:
``execute_graph_run`` calls it after compile+preflight; the facade calls it on resume/approve.
"""

from __future__ import annotations

import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
    AdmittedConnectionRecord,
    split_endpoint_host,
)
from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CLI_PROFILES,
    CliProfile,
    LocalCliConnectorWorker,
)
from bounded_loops.graph.adapters.connectors.node_cli_resolver import NodeCliResolver
from bounded_loops.graph.adapters.enforcement import ExecutionEnforcer, probe_platform
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPostureDecision
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.loop_node_wiring import (
    admitted_loop_package_digests,
    build_kind_dispatchers,
    _is_nontransport_kind,
)
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    latest_node_states,
    read_arena_projection,
)
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.run_dir_persistence import persist_run_dir
from bounded_loops.graph.application.failure_policy import (
    continues_after_failure,
)
from bounded_loops.graph.adapters.workers.connector_worker import ConnectorNodeWorker
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.adapters.enforcement.egress_posture_policy import resolve_local_cli_egress_decision
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkDestination,
    NetworkMode,
    network_mode_for_node,
)
from bounded_loops.graph.domain.authoring import AuthoringGraphSpec, IsolationLevel, _NULL_POLICY_DIGEST
from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver
from bounded_loops.graph.application.run_graph import GraphRunController, is_egress_node
from bounded_loops.graph.application.node_spend import RunBudget
from bounded_loops.graph.domain.pricing import PriceTable
from bounded_loops.graph.application.node_contracts import (
    ApprovalResolverPort,
    WorkerResult,
)
from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.byok_worker import _build_https_worker
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.graph_run_report import (
    _fail,
    _report,
)

# Single-tenant identity for `bl graph run --execute` on an operator's own machine. These are
# SENTINELS, deliberately prefixed ``local-``: a deployment serving more than one tenant must pass
# its own values. They are safe here because the CLI gives every run its own artifact store
# (``out_dir / "artifacts"``) and its own receipt stream, so two local runs cannot see each other's
# artifacts even while sharing an org/project label.
#
# They live here, once, because they were duplicated in ``cli_graph.py`` and in this module's
# function defaults — two copies that agreed by luck and would diverge the moment one was edited.
# ``tests/graph/test_layering.py`` asserts these strings appear nowhere else.
LOCAL_ORGANIZATION_ID = "local-org"
LOCAL_PROJECT_ID = "local-project"
LOCAL_RUN_ID = "graph-run"

_LOCAL_CLI_TRANSPORTS = frozenset({"local_cli"})
_HTTPS_TRANSPORTS = frozenset({"https"})
_ALL_EXECUTOR_TRANSPORTS = _LOCAL_CLI_TRANSPORTS | _HTTPS_TRANSPORTS
_DEFAULT_POLICY_DIGEST = _NULL_POLICY_DIGEST  # ARCH-06: canonical sentinel from authoring.py

_ISO_RANK = {
    IsolationLevel.WORKSPACE_ONLY: 0,
    IsolationLevel.PROCESS_RESTRICTED: 1,
    IsolationLevel.CONTAINER_RESTRICTED: 2,
    IsolationLevel.CUSTOMER_MANAGED_WORKER: 3,
}


def _https_isolation(node_isolation: IsolationLevel) -> IsolationLevel:
    # A network (external-write) effect floors at CONTAINER_RESTRICTED; lift to that floor, but
    # NEVER downgrade a node that already declares a HIGHER isolation tier (dual-audit finding).
    floor = IsolationLevel.CONTAINER_RESTRICTED
    return node_isolation if _ISO_RANK[node_isolation] >= _ISO_RANK[floor] else floor


class _ByokDispatchWorker:
    """Route connector execution to the right sub-worker by binding transport.

    A single graph may contain both ``local_cli`` and ``https`` connector nodes.
    This worker dispatches based on the binding transport stored in the plan, so the
    ``GraphRunController`` only needs a single ``connector_worker`` instance.
    """

    def __init__(
        self,
        *,
        local_cli_worker: LocalCliConnectorWorker,
        https_worker: ConnectorNodeWorker,
        plan: ExecutionPlan,
    ) -> None:
        self._local_cli = local_cli_worker
        self._https = https_worker
        self._plan = plan

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        transport = next(
            (b.transport for b in plan.connection_bindings if b.binding_id == node.binding_id),
            None,
        )
        if transport == "https":
            return self._https.execute(plan=plan, node=node, envelope=envelope, attempt=attempt)
        return self._local_cli.execute(plan=plan, node=node, envelope=envelope, attempt=attempt)


class _LocalAuthorizer:
    """Local same-tenant arena read (the run was produced on this host)."""

    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _LocalReceiptVerifier:
    """No-op receipt verifier for a locally produced projection."""

    def verify(self, identity: GraphRunIdentity, receipts: object) -> None:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_manifest(manifest_text: str, manifest_suffix: str) -> AuthoringGraphSpec:
    if manifest_suffix == ".json":
        return parse_authoring_graph_json(manifest_text)
    return parse_authoring_graph_yaml(manifest_text)


def _build_policy(
    plan: ExecutionPlan,
    egress_transports: frozenset[str],
    admitted: Mapping[str, AdmittedConnectionRecord] | None = None,
    *,
    local_cli_decision: EgressPostureDecision,
) -> ConfiguredExecutionPolicy:
    """One envelope per node.

    * **local_cli**: OPEN (unchanged) or, under ALLOWLIST, a REAL Seatbelt-caged envelope
      (isolation lifted like https's); BROKER is refused earlier.
    * **https**: ``NetworkMode.ALLOWLIST`` from the admitted record's endpoint host/port,
      isolation lifted to ``CONTAINER_RESTRICTED`` (``_EFFECT_MINIMUM``) — its own independent,
      credential-broker-mediated ALLOWLIST, unchanged by egress posture.
    * everything else: ``NetworkMode.DENY``.
    """
    admitted_map = admitted or {}
    envelopes: dict[str, ExecutionEnvelope] = {}
    for node in plan.nodes:
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        if (
            is_egress_node(plan, node, _HTTPS_TRANSPORTS)
            and binding is not None
            and binding.connection_id in admitted_map
        ):
            record = admitted_map[binding.connection_id]
            # endpoint_host was validated (host[:port] shape) when the record was constructed,
            # so this parse cannot raise here; the isolation floor never downgrades the node.
            dest_host, dest_port = split_endpoint_host(record.endpoint_host)
            envelopes[node.node_id] = ExecutionEnvelope(
                isolation=_https_isolation(node.isolation),
                transport=binding.transport,
                allowed_effects=node.required_effects,
                network_mode=NetworkMode.ALLOWLIST,
                network_destinations=(NetworkDestination(dest_host, dest_port),),
            )
        elif is_egress_node(plan, node, _LOCAL_CLI_TRANSPORTS):
            mode = local_cli_decision.network_mode
            if mode not in (NetworkMode.OPEN, NetworkMode.ALLOWLIST):
                # Unreachable today (BROKER refused earlier in build_execution_controller);
                # defensive backstop — also protects the facade's resume/approve (no preflight step).
                raise GraphValidationError(
                    "egress_posture", f"/nodes/{node.node_id}",
                    f"node {node.node_id!r} is local_cli under an unsupported egress decision "
                    f"({local_cli_decision.posture.value}); only open/allowlist is supported today",
                )
            allowlisted = mode is NetworkMode.ALLOWLIST
            envelopes[node.node_id] = ExecutionEnvelope(
                isolation=_https_isolation(node.isolation) if allowlisted else node.isolation,
                transport=binding.transport if binding else None,
                allowed_effects=node.required_effects,
                network_mode=mode,
                network_destinations=local_cli_decision.network_destinations if allowlisted else (),
            )
        else:
            envelopes[node.node_id] = ExecutionEnvelope(
                isolation=node.isolation,
                transport=binding.transport if binding else None,
                allowed_effects=node.required_effects,
                network_mode=NetworkMode.DENY,
                network_destinations=(),
            )
    return ConfiguredExecutionPolicy(envelopes)


def unknown_local_cli_provider(
    plan: ExecutionPlan, node: PlannedNode, cli_profiles: Mapping[str, CliProfile],
) -> str | None:
    """Message if this node binds a local-CLI provider the deployment cannot run, else ``None``.

    One rule, two callers, on purpose. ``_preflight`` uses it to refuse a fresh run with a readable
    CLI message; ``build_execution_controller`` uses it to refuse EVERY path that assembles a
    controller — resume, approve, MCP, console — because those bypass ``_preflight`` entirely.

    The gap that made this shared mattered on resume: a run created with ``--providers catalog.toml``
    and resumed WITHOUT it has a plan naming a provider that no longer exists. The resolver would
    still end the node terminally (``WorkerContractError``, so no retry storm), but only after
    starting it — and after that resume pass had already paid for the nodes upstream.

    The profile map is a required argument here. A default of "no map means skip the check" is the
    shape that let this hole exist in the first place.
    """
    if not is_egress_node(plan, node, _LOCAL_CLI_TRANSPORTS):
        return None
    binding = next(
        (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
    )
    if binding is None or binding.provider_id in cli_profiles:
        return None
    known = ", ".join(sorted(cli_profiles)) or "(none configured)"
    return (
        f"node {node.node_id!r} binds local-CLI provider {binding.provider_id!r}, which this "
        f"deployment has no profile for (known: {known}). Add a provider catalog entry for it "
        "(--providers / BOUNDED_LOOPS_PROVIDERS), or bind the node to a provider that is "
        "installed. Refused before any attempt starts: reaching this node would fail every "
        "attempt identically, having already paid for every node upstream of it."
    )


def duplicate_connection_binding(
    plan: ExecutionPlan, node: PlannedNode, admitted: Mapping[str, AdmittedConnectionRecord],
) -> str | None:
    """Message if this node's connection is bound by more than one binding, else ``None``.

    Same shape and same two callers as ``unknown_local_cli_provider``, for the same reason: a
    misconfiguration visible in the plan must be refused from the plan, not from inside a worker
    after the nodes upstream have really run and paid.

    The constraint mirrored here is in ``OpaqueCredentialBroker``: a lease is minted from an
    ``ExecutionGrant``, which carries the ``connection_id`` but NOT the ``binding_id``, so the
    broker recovers the binding by scanning for the one whose connection matches and refuses when
    that is ambiguous ("exactly one broker binding is required for the connection"). Two bindings
    on one connection make every mint on it ambiguous — every node bound to it fails identically.

    Left to the worker that surfaces as ``cause=worker_fault`` with an empty ``node.spend``, which
    cannot distinguish "refused before the request" from "the provider was already paid". It IS
    pre-egress (the mint precedes ``egress_broker.authorize`` and the forwarder, so nothing leaves
    the process), but the receipt cannot say so, and a node upstream on a DIFFERENT connection has
    really paid by then. Refusing from the plan makes the distinction unnecessary: nothing ran.

    Scoped per NODE: a plan may legitimately carry two bindings on a connection no node binds, an
    unused slot never reaches the broker, and refusing it would wedge a working path (the scoping
    lesson on ``_refuse_unrunnable_providers``).
    """
    if not is_egress_node(plan, node, _HTTPS_TRANSPORTS):
        return None
    binding = next(
        (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
    )
    # An absent binding, or one with no admitted record, is the neighbouring rule's to report — and
    # a connection with no record never reaches the broker (byok_worker builds its binding set from
    # the bindings whose connection_id is admitted).
    if binding is None or binding.connection_id not in admitted:
        return None
    sharing = sorted(
        b.binding_id for b in plan.connection_bindings
        if b.connection_id == binding.connection_id
    )
    if len(sharing) < 2:
        return None
    return (
        f"node {node.node_id!r} binds connection_id {binding.connection_id!r}, which is bound by "
        f"{len(sharing)} bindings ({', '.join(repr(b) for b in sharing)}). A credential lease is "
        "resolved from the connection, so more than one binding on it is ambiguous and every node "
        "bound to it fails on every attempt. Give each binding its own connection_id and supply an "
        "admitted-connection record for each (they may name the same endpoint and the same "
        "credential env var). Refused before any attempt starts, so no provider was called."
    )


def _preflight(
    plan: ExecutionPlan,
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None,
    cli_profiles: Mapping[str, CliProfile] = CLI_PROFILES,
) -> str | None:
    """Return a clear error message if the graph has a node this phase cannot run, else None.

    Extended rules vs. the local-CLI-only original:
    * local_cli nodes: allowed only when this deployment has a profile for the provider the
      binding names. Before P3 this check did not exist, and the consequence was not merely a
      late error message: ``NodeCliResolver`` raises ``GraphIntegrityError`` from inside the
      worker, which the controller classifies as a transient ``WORKER_FAULT`` and retries to
      ``max_attempts`` — so a typo in a provider name burned the whole retry budget on a
      failure that could never succeed, *after* every upstream node had really run and paid.
      A misconfiguration that is fully detectable before the first attempt must be refused
      before the first attempt.
    * https nodes: allowed ONLY when a matching ``AdmittedConnectionRecord`` is present;
      fails closed if none supplied — never silently skips or fabricates a grant.
    * Approval checkpoints: ALLOWED — controller pauses at an unapproved gate (AWAITING_APPROVAL).
    * ``kind: loop`` / ``kind: join`` / ``kind: publish``: ALLOWED — these have their own workers
      installed via ``build_kind_dispatchers`` and bind no connector transport.
    * All other nodes (unbound, sandboxed tool, etc.): refused with a clear message.
    """
    admitted = admitted_connections or {}
    for node in plan.nodes:
        if _is_nontransport_kind(node.kind):
            # Approval, loop, join, and publish have dedicated workers — the transport
            # check below does not apply. Compile has already verified runnability.
            continue
        if not is_egress_node(plan, node, _ALL_EXECUTOR_TRANSPORTS):
            return (
                f"node {node.node_id!r} (kind {node.kind}) is not an admitted connector node; "
                "`bl graph run --execute` runs graphs whose nodes bind a connection with "
                "transport 'local_cli' (subscription CLI) or 'https' (BYOK/HTTP connector), "
                "or are an 'approval' human checkpoint. Sandboxed tool execution is a later phase."
            )
        unknown_provider = unknown_local_cli_provider(plan, node, cli_profiles)
        if unknown_provider is not None:
            return unknown_provider
        # https nodes require an admitted record — fail closed if absent.
        if is_egress_node(plan, node, _HTTPS_TRANSPORTS):
            binding = next(
                (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
            )
            if binding is None or binding.connection_id not in admitted:
                conn_id = binding.connection_id if binding else "<unknown>"
                return (
                    f"node {node.node_id!r} uses an https connection ({conn_id!r}) but no "
                    "admitted-connection record was supplied. Pass one via admitted_connections "
                    "(or --admitted on the CLI). A grant cannot be issued without it."
                )
        duplicate_binding = duplicate_connection_binding(plan, node, admitted)
        if duplicate_binding is not None:
            return duplicate_binding
    return None


def _refuse_unrunnable_providers(
    plan: ExecutionPlan,
    event_log: GraphEventLog,
    cli_profiles: Mapping[str, CliProfile],
    admitted: Mapping[str, AdmittedConnectionRecord],
) -> None:
    """Refuse a node this deployment cannot run — for nodes that could still run.

    Both plan-visible refusals live here — a local-CLI provider with no profile, and a connection
    bound by more than one binding — at the chokepoint every path bypassing ``_preflight`` shares.

    A node already SUCCEEDED cannot execute again, so its provider is a historical fact rather than a
    requirement. Reading the log costs one replay, which every assembly path does anyway.
    """
    try:
        completed = {
            node_id
            for node_id, value in latest_node_states(plan, event_log.replay()).items()
            if value.get("state") == "SUCCEEDED"
        }
    except (GraphIntegrityError, GraphValidationError):
        completed = set()  # an unreadable log is the replay's problem to report, not this check's
    for node in plan.nodes:
        if node.node_id in completed or _is_nontransport_kind(node.kind):
            continue
        unknown_provider = unknown_local_cli_provider(plan, node, cli_profiles)
        if unknown_provider is not None:
            raise GraphValidationError(
                "unknown_provider", f"/nodes/{node.node_id}", unknown_provider,
            )
        duplicate_binding = duplicate_connection_binding(plan, node, admitted)
        if duplicate_binding is not None:
            raise GraphValidationError(
                "duplicate_connection_binding", f"/nodes/{node.node_id}", duplicate_binding,
            )


def build_execution_controller(
    *,
    plan: ExecutionPlan,
    identity: GraphRunIdentity,
    out_dir: Path,
    node_prompts: Mapping[str, str],
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None,
    cli_profiles: Mapping[str, CliProfile] = CLI_PROFILES,
    environ: Mapping[str, str] | None = None,
    capabilities: PlatformCapabilities | None = None,
    byok_egress_broker: EgressBroker | None = None,
    byok_credential_resolver: object = None,
    byok_tls_context: ssl.SSLContext | None = None,
    approval_resolver: ApprovalResolverPort | None = None,
    # Operator spend controls. None means no ceiling and no rates — how every pre-0.5 caller
    # behaves: nothing capped, and a node declaring a cost cap fails closed as unmeasurable
    # rather than running against prices nobody supplied.
    run_budget: RunBudget | None = None,
    price_table: PriceTable | None = None,
    # The graph's fail mode, reduced to one bit. False (halt at the first node failure) is every
    # pre-0.5 caller's behaviour, so an un-updated caller cannot silently gain continuation.
    continue_on_failure: bool = False,
    # Where `kind: loop` nodes' packages are looked up, BY DIGEST. None means the shipped `loops/`
    # tree beside the installed package plus `./loops` under the cwd, which is what makes the 68
    # shipped packages composable without configuration. A deployment shipping its own catalogue
    # passes its own roots.
    loop_package_roots: tuple[Path, ...] | None = None,
) -> tuple[GraphRunController, LocalArtifactStore, GraphEventLog]:
    """Shared controller-assembly helper for ``execute_graph_run`` and ``LocalGraphRuntimeFacade``.
    Builds the full wiring (platform-caps check, artifact store, event log, workers, policy)
    for an already-compiled plan; returns a ready-to-use ``GraphRunController`` plus the store
    and event log it owns.

    Raises ``GraphValidationError`` if the platform cannot enforce required isolation for any
    non-egress node, OR if the resolved egress posture (Slice 2, read from *environ*) can't be
    honored by this plan's local_cli node(s) — both existing callers already wrap this call.

    ``out_dir`` must already exist. ``approval_resolver`` continues an approval-node graph past
    human gates; ``capabilities`` defaults to a real platform probe (``build_enforcer``'s convention).
    """
    admitted = admitted_connections or {}

    # ── platform-capability check ────────────────────────────────────────────
    # Egress/connector nodes are routed to ConnectorNodeWorker — no subprocess is
    # sandboxed — so the platform capability check does not apply to them.
    caps = capabilities if capabilities is not None else probe_platform()
    egress_node_ids = frozenset(
        n.node_id for n in plan.nodes
        if is_egress_node(plan, n, _ALL_EXECUTOR_TRANSPORTS)
    )
    for node in plan.nodes:
        if node.node_id in egress_node_ids:
            continue
        # Nontransport kinds (approval, join, publish) are in-process workers — no subprocess is
        # sandboxed — so platform capability enforcement does not apply to them.
        if _is_nontransport_kind(node.kind):
            continue
        ok, reason = caps.can_enforce(node.isolation, network_mode_for_node(node))
        if not ok:
            raise GraphValidationError(
                "execution_enforcement",
                f"/nodes/{node.node_id}",
                f"cannot enforce {node.isolation.value} isolation: {reason}",
            )
    enforcer = ExecutionEnforcer(caps)

    # ── egress posture (Slice 2): resolved ONCE, before any store/worker is built ───────────
    local_cli_decision = resolve_local_cli_egress_decision(plan, environ=environ, capabilities=caps)

    # ── stores ───────────────────────────────────────────────────────────────
    organization_id = identity.organization_id
    project_id = identity.project_id
    run_id = identity.run_id

    store = LocalArtifactStore(out_dir / "artifacts")
    event_log = GraphEventLog(out_dir / "controller-events.jsonl", identity)

    # Every path that assembles a controller passes through here — ``execute_graph_run``, the
    # facade's resume and approve, MCP, the console. ``_preflight`` only guards the first of those,
    # so the provider check belongs at this chokepoint too: a run created with a provider catalog and
    # continued without it names a provider that no longer exists, and the alternative is discovering
    # that after the pass has already paid for the nodes upstream.
    #
    # Scoped to nodes that could STILL RUN. The first version checked every node in the plan, which
    # made a run whose nodes had ALL SUCCEEDED unresumable — resuming a finished run used to return
    # its projection idempotently, and a check about what might execute next has no business refusing
    # a run with nothing left to execute. Third time a new refusal in this phase has wedged a working
    # path; hence the scope.
    _refuse_unrunnable_providers(plan, event_log, cli_profiles, admitted)

    # ── transport detection + worker assembly ────────────────────────────────
    has_local_cli = any(is_egress_node(plan, n, _LOCAL_CLI_TRANSPORTS) for n in plan.nodes)
    has_https = any(is_egress_node(plan, n, _HTTPS_TRANSPORTS) for n in plan.nodes)
    egress_transports = _ALL_EXECUTOR_TRANSPORTS if (has_local_cli and has_https) else (
        _HTTPS_TRANSPORTS if has_https else _LOCAL_CLI_TRANSPORTS
    )

    local_cli_worker = LocalCliConnectorWorker(
        identity=identity,
        artifact_store=store,
        resolver=NodeCliResolver(node_prompts, profiles=cli_profiles),
        workspace_root=out_dir / "work",
        organization_id=organization_id,
        project_id=project_id,
        environ=environ,
        capabilities=caps,
    )

    if has_https:
        https_worker = _build_https_worker(
            plan=plan,
            store=store,
            run_id=run_id,
            node_prompts=node_prompts,
            admitted=admitted,
            organization_id=organization_id,
            project_id=project_id,
            environ=environ,
            egress_broker=byok_egress_broker,
            credential_resolver=byok_credential_resolver,
            tls_context=byok_tls_context,
        )
        if has_local_cli:
            connector_worker: object = _ByokDispatchWorker(
                local_cli_worker=local_cli_worker,
                https_worker=https_worker,
                plan=plan,
            )
        else:
            connector_worker = https_worker
    else:
        connector_worker = local_cli_worker

    # ── kind workers and gates (loop, join, publish) ──────────────────────────
    kind_worker, kind_gate = build_kind_dispatchers(
        store=store, event_log=event_log, identity=identity, out_dir=out_dir,
        caps=caps, loop_package_roots=loop_package_roots,
        organization_id=organization_id, project_id=project_id, run_id=run_id,
    )

    controller = GraphRunController(
        plan=plan,
        event_log=event_log,
        worker=kind_worker,
        gate=kind_gate,
        artifact_verifier=LocalArtifactVerifier(store),
        execution_policy=_build_policy(plan, egress_transports, admitted, local_cli_decision=local_cli_decision),
        execution_enforcer=enforcer,
        timestamp=_now_iso,
        actor="graph-controller",
        connector_worker=connector_worker,  # type: ignore[arg-type]
        egress_transports=egress_transports,
        approval_resolver=approval_resolver,
        run_budget=run_budget,
        price_table=price_table,
        continue_on_failure=continue_on_failure,
    )
    return controller, store, event_log


def execute_graph_run(
    *,
    manifest_text: str,
    manifest_suffix: str,
    connections_raw: Sequence[object],
    node_prompts: Mapping[str, str],
    out_dir: Path,
    organization_id: str = LOCAL_ORGANIZATION_ID,
    project_id: str = LOCAL_PROJECT_ID,
    run_id: str = LOCAL_RUN_ID,
    policy_digest: str = _DEFAULT_POLICY_DIGEST,
    json_out: bool = False,
    cli_profiles: Mapping[str, CliProfile] = CLI_PROFILES,
    environ: Mapping[str, str] | None = None,
    # Egress posture (Slice 2) — injectable PlatformCapabilities for deterministic tests;
    # defaults to a real platform probe. See egress_posture_policy.py.
    capabilities: PlatformCapabilities | None = None,
    # BYOK/HTTP mode — supply admitted-connection authority records (key = connection_id).
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None,
    # Injectable BYOK infrastructure for hermetic testing (all default to production values).
    byok_egress_broker: EgressBroker | None = None,
    byok_credential_resolver: object = None,
    byok_tls_context: ssl.SSLContext | None = None,
    # Optional cross-model audit plan JSON text. When supplied, persisted verbatim
    # as ``audit-plan.json`` in the run directory for read-side arena projection.
    # Does NOT affect the controller loop — read-side only.
    audit_plan_json: str | None = None,
    # Operator spend controls. Default to no ceiling and no rates, which is exactly how every
    # pre-0.5 caller behaves: nothing is capped, and any node declaring a cost cap fails closed
    # as unmeasurable rather than running against prices nobody supplied.
    run_budget: RunBudget | None = None,
    price_table: PriceTable | None = None,
    # The catalog file ``cli_profiles`` was resolved from, recorded on the run so a continuation
    # resolves the same providers. Not derivable from the map itself.
    provider_catalog: Path | None = None,
    loop_package_roots: tuple[Path, ...] | None = None,
) -> int:
    """Compile a user manifest and run its admitted connector nodes for real.

    Mode selection by transport:
    * local_cli nodes  → ``LocalCliConnectorWorker`` (unchanged from before)
    * https nodes      → ``ConnectorNodeWorker`` wired with the real ``HttpConnectorForwarder``
                         and a grant issued from the supplied ``admitted_connections`` records

    A graph may mix both transport types; a ``_ByokDispatchWorker`` routes each node to the
    right sub-worker by its binding transport.

    Preflight fails closed if an https node has no matching admitted-connection record.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        graph = _parse_manifest(manifest_text, manifest_suffix)
        snapshot = CompileSnapshot(
            policy_digest=policy_digest,
            package_digests=admitted_loop_package_digests(loop_package_roots),
            connections=tuple(connections_raw),  # type: ignore[arg-type]
        )
        plan = compile_graph(graph, snapshot)
    except GraphValidationError as exc:
        return _fail(json_out, f"compile failed [{exc.code}] {exc.pointer} — {exc.message}")

    admitted = admitted_connections or {}
    problem = _preflight(plan, admitted, cli_profiles)
    if problem is not None:
        return _fail(json_out, problem)

    identity = GraphRunIdentity(
        organization_id=organization_id,
        project_id=project_id,
        run_id=run_id,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )

    # Mode label is deterministic from the plan — compute before building the controller.
    has_local_cli = any(is_egress_node(plan, n, _LOCAL_CLI_TRANSPORTS) for n in plan.nodes)
    has_https = any(is_egress_node(plan, n, _HTTPS_TRANSPORTS) for n in plan.nodes)
    mode_label = "local_cli+https" if (has_local_cli and has_https) else (
        "https" if has_https else "local_cli"
    )

    # Wired unconditionally: harmless for a graph with no approval nodes (the controller
    # only ever consults this port when it reaches an APPROVAL-kind node), and required
    # for one that has them — a fresh run must be able to PAUSE at an unapproved gate
    # (and to honor a decision durably recorded at this out_dir by an earlier attempt)
    # rather than crash for lack of a resolver. Same function `LocalGraphRuntimeFacade`
    # uses on resume/approve — one implementation, no logic fork.
    #
    # FAIL CLOSED, don't crash: a torn/corrupt approvals.json makes `_load_approvals`
    # raise `GraphIntegrityError` (dual-audit MAJOR — Grok + Muse both flagged this as
    # an uncaught exception, not the clean `rc=2` / `error:` contract every other
    # refusal in this function honors).
    try:
        approval_resolver = build_durable_approval_resolver(
            identity=identity, plan=plan, run_dir=out_dir,
        )
    except GraphIntegrityError as exc:
        return _fail(json_out, f"approval ledger corrupt or unreadable — {exc}")

    try:
        controller, store, event_log = build_execution_controller(
            plan=plan,
            continue_on_failure=continues_after_failure(graph.policies.fail_mode),
            identity=identity,
            out_dir=out_dir,
            node_prompts=node_prompts,
            admitted_connections=admitted,
            cli_profiles=cli_profiles,
            environ=environ,
            capabilities=capabilities,
            byok_egress_broker=byok_egress_broker,
            byok_credential_resolver=byok_credential_resolver,
            byok_tls_context=byok_tls_context,
            approval_resolver=approval_resolver,
            run_budget=run_budget,
            price_table=price_table,
            loop_package_roots=loop_package_roots,
        )
    except GraphValidationError as exc:
        return _fail(json_out, f"execution enforcement refused before run: {exc.message}")

    # FAIL CLOSED, don't crash: re-running `execute_graph_run` at an `out_dir` that
    # already holds a started run (e.g. a user who sees rc=3 PAUSED and just re-runs
    # the same command, expecting a "resume") makes `GraphRunController.run()` raise
    # `GraphIntegrityError("fresh controller refuses to resume a non-empty graph
    # stream")` (dual-audit MAJOR — M1 in the Grok audit). The actionable fix is
    # `bl graph approve`, not a second `run`, so say so instead of letting the
    # exception escape as an uncaught traceback.
    try:
        projection = controller.run()
    except GraphIntegrityError as exc:
        # Distinguish the benign re-run case (a user who saw rc=3 PAUSED and just
        # re-ran the same command, expecting a "resume") from a GENUINE integrity
        # failure (a tampered/torn event log, a worker-integrity violation). Both
        # fail closed (rc=2), but a tamper must NOT be misreported as "just re-run
        # with approve" — preserve the forensic signal (dual-audit convergence MINOR).
        if "non-empty graph stream" in str(exc):
            return _fail(
                json_out,
                f"this --out already holds a run; to continue a paused run use "
                f"`bl graph approve --run {out_dir} --node <node_id> --decision "
                f"approved|rejected` — {exc}",
            )
        return _fail(json_out, f"run integrity failure — {exc}")
    persist_run_dir(
        out_dir, plan, manifest_text, connections_raw, identity,
        mode=mode_label, audit_plan_json=audit_plan_json,
        provider_catalog=provider_catalog, fail_mode=graph.policies.fail_mode,
    )
    arena = read_arena_projection(
        plan,
        event_log,
        ArenaReadRequest(
            subject_id=organization_id,
            organization_id=organization_id,
            project_id=project_id,
            run_id=run_id,
        ),
        _LocalAuthorizer(),
        _LocalReceiptVerifier(),
    )
    return _report(json_out, out_dir, projection.state, arena, mode=mode_label)


