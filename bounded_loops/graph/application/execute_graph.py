"""Real ``bl graph run --execute <manifest>`` — run an admitted connector graph.

Two connector modes are supported:

* **local_cli** (existing):  Run the user's own subscription agent CLI under an open-network
  envelope. No credential is read; the CLI authenticates itself out-of-band.

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

The per-node prompt is RUN-TIME input (``node_id -> prompt``), never baked into the portable
graph.
"""

from __future__ import annotations

import json
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
    AdmittedConnectionRecord,
    AdmittedConnectionRequestBuilder,
    split_endpoint_host,
)
from bounded_loops.graph.adapters.connectors.artifact_body import LocalArtifactBody
from bounded_loops.graph.adapters.connectors.credentials import (
    CredentialSource,
    EnvCredentialResolver,
)
from bounded_loops.graph.adapters.connectors.http_forwarder import HttpConnectorForwarder
from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CLI_PROFILES,
    CliProfile,
    LocalCliConnectorWorker,
)
from bounded_loops.graph.adapters.connectors.node_cli_resolver import NodeCliResolver
from bounded_loops.graph.adapters.enforcement import ExecutionEnforcer, probe_platform
from bounded_loops.graph.adapters.enforcement.enforcer import _network_mode_for as _nmf_for_node
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.acceptance_gate import StructuralAcceptanceGate
from bounded_loops.graph.application.arena_projection import (
    ArenaProjection,
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.connector_forward import ConnectorInvoker
from bounded_loops.graph.application.connector_worker import ConnectorNodeWorker
from bounded_loops.graph.application.credential_broker import OpaqueCredentialBroker
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkDestination,
    NetworkMode,
)
from bounded_loops.graph.domain.authoring import AuthoringGraphSpec, IsolationLevel, NodeKind
from bounded_loops.graph.application.run_graph import (
    GraphRunController,
    WorkerResult,
    is_egress_node,
)
from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.connections import (
    CredentialBinding,
    CredentialKind,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_LOCAL_CLI_TRANSPORTS = frozenset({"local_cli"})
_HTTPS_TRANSPORTS = frozenset({"https"})
_ALL_EXECUTOR_TRANSPORTS = _LOCAL_CLI_TRANSPORTS | _HTTPS_TRANSPORTS
_DEFAULT_POLICY_DIGEST = "sha256:" + "a" * 64

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


class _UnsupportedNodeWorker:
    """Fail closed for a node this phase cannot run (the preflight surfaces it first)."""

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
    ) -> WorkerResult:
        raise GraphIntegrityError(
            f"node {node.node_id!r} (kind {node.kind!r}) is not runnable via "
            "`bl graph run --execute` in this phase (admitted local-CLI or https connector nodes only)"
        )


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
    ) -> WorkerResult:
        transport = next(
            (b.transport for b in plan.connection_bindings if b.binding_id == node.binding_id),
            None,
        )
        if transport == "https":
            return self._https.execute(plan=plan, node=node, envelope=envelope)
        return self._local_cli.execute(plan=plan, node=node, envelope=envelope)


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
) -> ConfiguredExecutionPolicy:
    """One envelope per node.

    * **local_cli** egress nodes: ``NetworkMode.OPEN`` — trusted-local process, no sandbox.
    * **https** egress nodes: ``NetworkMode.ALLOWLIST`` with a ``NetworkDestination`` derived
      from the admitted record's endpoint host/port, and isolation lifted to
      ``CONTAINER_RESTRICTED``.  ``validate_execution_envelope`` enforces a
      ``container_restricted`` floor for ``external_write`` effect (``_EFFECT_MINIMUM``); the
      envelope must honour that floor even if the manifest declared ``process_restricted``.
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
            envelopes[node.node_id] = ExecutionEnvelope(
                isolation=node.isolation,
                transport=binding.transport if binding else None,
                allowed_effects=node.required_effects,
                network_mode=NetworkMode.OPEN,
                network_destinations=(),
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


def _preflight(
    plan: ExecutionPlan,
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None,
) -> str | None:
    """Return a clear error message if the graph has a node this phase cannot run, else None.

    Extended rules vs. the local-CLI-only original:
    * local_cli nodes: still allowed unchanged.
    * https nodes: allowed ONLY when a matching ``AdmittedConnectionRecord`` is present;
      fails closed if none supplied — never silently skips or fabricates a grant.
    * Approval checkpoints: always refused (human-in-the-loop is a later phase).
    * All other nodes (unbound, sandboxed tool, etc.): refused with a clear message.
    """
    admitted = admitted_connections or {}
    for node in plan.nodes:
        if node.kind == NodeKind.APPROVAL.value:
            return (
                f"node {node.node_id!r} is an approval checkpoint; human-approval execution "
                "via --execute is a later phase. Remove it to run the graph."
            )
        if not is_egress_node(plan, node, _ALL_EXECUTOR_TRANSPORTS):
            return (
                f"node {node.node_id!r} (kind {node.kind}) is not an admitted connector node; "
                "`bl graph run --execute` runs graphs whose nodes bind a connection with "
                "transport 'local_cli' (subscription CLI) or 'https' (BYOK/HTTP connector). "
                "Sandboxed tool execution is a later phase."
            )
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
    return None


def execute_graph_run(
    *,
    manifest_text: str,
    manifest_suffix: str,
    connections_raw: Sequence[object],
    node_prompts: Mapping[str, str],
    out_dir: Path,
    organization_id: str = "local-org",
    project_id: str = "local-project",
    run_id: str = "graph-run",
    policy_digest: str = _DEFAULT_POLICY_DIGEST,
    json_out: bool = False,
    cli_profiles: Mapping[str, CliProfile] = CLI_PROFILES,
    environ: Mapping[str, str] | None = None,
    # BYOK/HTTP mode — supply admitted-connection authority records (key = connection_id).
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None,
    # Injectable BYOK infrastructure for hermetic testing (all default to production values).
    byok_egress_broker: EgressBroker | None = None,
    byok_credential_resolver: object = None,
    byok_tls_context: ssl.SSLContext | None = None,
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
            package_digests=frozenset(),
            connections=tuple(connections_raw),  # type: ignore[arg-type]
        )
        plan = compile_graph(graph, snapshot)
    except GraphValidationError as exc:
        return _fail(json_out, f"compile failed [{exc.code}] {exc.pointer} — {exc.message}")

    admitted = admitted_connections or {}
    problem = _preflight(plan, admitted)
    if problem is not None:
        return _fail(json_out, problem)

    caps = probe_platform()
    try:
        # Egress/connector nodes are routed to ConnectorNodeWorker — no subprocess is
        # sandboxed — so the platform capability check (which gates subprocess isolation)
        # does not apply to them.  run_graph.py applies the same single-source-of-truth
        # egress classification at runtime (skips enforcer.enforce for egress nodes);
        # we mirror it here at build time so the enforcer never incorrectly refuses an
        # https connector node that declares external_write but runs via the BYOK HTTP
        # path instead of a sandboxed subprocess.
        egress_node_ids = frozenset(
            n.node_id for n in plan.nodes
            if is_egress_node(plan, n, _ALL_EXECUTOR_TRANSPORTS)
        )
        for node in plan.nodes:
            if node.node_id in egress_node_ids:
                continue
            ok, reason = caps.can_enforce(node.isolation, _nmf_for_node(node))
            if not ok:
                raise GraphValidationError(
                    "execution_enforcement",
                    f"/nodes/{node.node_id}",
                    f"cannot enforce {node.isolation.value} isolation: {reason}",
                )
        enforcer = ExecutionEnforcer(caps)
    except GraphValidationError as exc:
        return _fail(json_out, f"execution enforcement refused before run: {exc.message}")

    identity = GraphRunIdentity(
        organization_id=organization_id,
        project_id=project_id,
        run_id=run_id,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )
    store = LocalArtifactStore(out_dir / "artifacts")
    event_log = GraphEventLog(out_dir / "controller-events.jsonl", identity)

    # Determine active transport sets.
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

    mode_label = "local_cli+https" if (has_local_cli and has_https) else (
        "https" if has_https else "local_cli"
    )

    controller = GraphRunController(
        plan=plan,
        event_log=event_log,
        worker=_UnsupportedNodeWorker(),
        gate=StructuralAcceptanceGate(store, organization_id=organization_id, project_id=project_id),
        artifact_verifier=LocalArtifactVerifier(store),
        execution_policy=_build_policy(plan, egress_transports, admitted),
        execution_enforcer=enforcer,
        timestamp=_now_iso,
        actor="graph-controller",
        connector_worker=connector_worker,  # type: ignore[arg-type]
        egress_transports=egress_transports,
    )
    projection = controller.run()
    _persist_run_dir(out_dir, plan, manifest_text, connections_raw, identity, mode=mode_label)
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


def _build_https_worker(
    *,
    plan: ExecutionPlan,
    store: LocalArtifactStore,
    run_id: str,
    node_prompts: Mapping[str, str],
    admitted: Mapping[str, AdmittedConnectionRecord],
    organization_id: str,
    project_id: str,
    environ: Mapping[str, str] | None,
    egress_broker: EgressBroker | None,
    credential_resolver: object,
    tls_context: ssl.SSLContext | None,
) -> ConnectorNodeWorker:
    """Assemble the real BYOK worker stack for https-transport connector nodes."""
    # Collect bindings whose connection has an admitted record.
    https_bindings = [
        b for b in plan.connection_bindings
        if b.connection_id in admitted
    ]
    # Build OpaqueCredentialBroker — one CredentialBinding per admitted https binding.
    credential_bindings = [
        CredentialBinding(
            binding_id=b.binding_id,
            connection_id=b.connection_id,
            kind=CredentialKind.VAULT_REFERENCE,
        )
        for b in https_bindings
    ]

    # Build EnvCredentialResolver — or use the injected one (for tests).
    if credential_resolver is None:
        cred_sources = {
            b.binding_id: CredentialSource(
                env_var=admitted[b.connection_id].credential_env_var_name,
                header_name=admitted[b.connection_id].credential_header_name,
                value_prefix=admitted[b.connection_id].credential_header_prefix,
            )
            for b in https_bindings
        }
        resolved_credential_resolver = EnvCredentialResolver(cred_sources, environ=environ)
    else:
        resolved_credential_resolver = credential_resolver  # type: ignore[assignment]

    artifact_body: LocalArtifactBody = LocalArtifactBody(
        store,
        organization_id=organization_id,
        project_id=project_id,
        producer_attempt=f"{run_id}-byok-response",
    )

    forwarder = HttpConnectorForwarder(
        artifact_body=artifact_body,
        credential_resolver=resolved_credential_resolver,
        tls_context=tls_context,
    )

    resolved_egress_broker: EgressBroker = egress_broker if egress_broker is not None else EgressBroker()

    invoker = ConnectorInvoker(
        credential_broker=OpaqueCredentialBroker(credential_bindings),
        egress_broker=resolved_egress_broker,
        forwarder=forwarder,
    )

    request_port = AdmittedConnectionRequestBuilder(
        records=admitted,
        artifact_store=store,
        run_id=run_id,
        node_prompts=node_prompts,
        organization_id=organization_id,
        project_id=project_id,
    )

    return ConnectorNodeWorker(run_id=run_id, invoker=invoker, request_port=request_port)


def _persist_run_dir(
    out_dir: Path,
    plan: ExecutionPlan,
    manifest_text: str,
    connections_raw: Sequence[object],
    identity: GraphRunIdentity,
    *,
    mode: str = "local_cli",
) -> None:
    """Persist the four files ``bl graph status`` / ``arena`` reconstruct any run from. Written
    after the run so a crash never leaves a half-written receipt claiming success. Run-time inputs
    (prompts) are deliberately NOT persisted — a prompt may carry a secret, and the content-addressed
    reply artifact is the durable receipt; the portable graph reconstructs from manifest+connections."""
    (out_dir / "plan.json").write_bytes(plan.canonical_json)
    (out_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
    (out_dir / "connections.json").write_text(
        json.dumps(list(connections_raw), sort_keys=True), encoding="utf-8"
    )
    run_meta = {
        "execution": True,
        "mode": mode,
        "organization_id": identity.organization_id,
        "plan_id": plan.plan_id,
        "policy_digest": plan.policy_digest,
        "project_id": identity.project_id,
        "run_id": identity.run_id,
        "platform": sys.platform,
    }
    (out_dir / "run-meta.json").write_text(json.dumps(run_meta, sort_keys=True), encoding="utf-8")


def _report(
    json_out: bool, out_dir: Path, run_state: str, arena: ArenaProjection,
    *, mode: str = "local_cli",
) -> int:
    succeeded = run_state == "SUCCEEDED"
    digests = [n.artifact_digests[0] for n in arena.nodes if n.artifact_digests]
    if json_out:
        print(json.dumps({
            "execution": True,
            "mode": mode,
            "run_state": run_state,
            "run_id": arena.run_id,
            "out": str(out_dir),
            "artifact_digests": digests,
        }, sort_keys=True))
        return 0 if succeeded else 2
    label = "BYOK/HTTP" if mode == "https" else "Local-CLI"
    print(f"{label} graph run — REAL execution")
    print("=" * 62)
    print(f"run_state : {run_state}")
    for node in arena.nodes:
        mark = "OK " if node.state == "SUCCEEDED" else "!! "
        art = (node.artifact_digests[0][:24] + "...") if node.artifact_digests else "-"
        print(f"  {mark}node {node.node_id!r}: {node.state}  artifact={art}")
    print(f"out       : {out_dir}")
    if succeeded:
        print()
        print(f"Open the visual Arena:  bl graph arena --run {out_dir}")
        return 0
    print()
    print("Run did not succeed; inspect the event log in the run directory.")
    return 2


def _fail(json_out: bool, message: str) -> int:
    if json_out:
        print(json.dumps({"execution": False, "error": message}, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2
