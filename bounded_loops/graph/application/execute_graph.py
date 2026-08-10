"""Real ``bl graph run --execute <manifest>`` — run an admitted local-CLI connector graph.

The demoable end-to-end (RE): compile a USER manifest against USER-supplied admitted connections,
run its local-CLI connector nodes for real through ``LocalCliConnectorWorker`` (the user's own
subscription agent CLI, run freely under an OPEN-network envelope), gate each node with an
INDEPENDENT structural-acceptance gate, and persist a hash-chained, receipt-backed run directory
that ``bl graph status`` / ``bl graph arena`` render unchanged.

Scope (honest boundary): this phase runs graphs whose executable nodes are admitted ``local_cli``
connector nodes. Sandboxed arbitrary-tool nodes (a package broker) and BYOK/HTTP connector nodes
are later phases; such a node fails closed with a clear message rather than a silent skip. The
per-node prompt is RUN-TIME input (``node_id -> prompt``), never baked into the portable graph.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CLI_PROFILES,
    CliProfile,
    LocalCliConnectorWorker,
)
from bounded_loops.graph.adapters.connectors.node_cli_resolver import NodeCliResolver
from bounded_loops.graph.adapters.enforcement import build_enforcer, probe_platform
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
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import (
    GraphRunController,
    WorkerResult,
    is_egress_node,
)
from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.authoring import AuthoringGraphSpec, NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_LOCAL_CLI_TRANSPORTS = frozenset({"local_cli"})
_DEFAULT_POLICY_DIGEST = "sha256:" + "a" * 64


class _UnsupportedNodeWorker:
    """Fail closed for a node this phase cannot run (the preflight surfaces it first)."""

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
    ) -> WorkerResult:
        raise GraphIntegrityError(
            f"node {node.node_id!r} (kind {node.kind!r}) is not runnable via "
            "`bl graph run --execute` in this phase (admitted local-CLI connector nodes only)"
        )


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


def _build_policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    """One envelope per node: OPEN network for the admitted local-CLI (egress) nodes so the
    agent reaches its model + tools; DENY for every other node (which this phase fails closed)."""
    envelopes: dict[str, ExecutionEnvelope] = {}
    for node in plan.nodes:
        egress = is_egress_node(plan, node, _LOCAL_CLI_TRANSPORTS)
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        envelopes[node.node_id] = ExecutionEnvelope(
            isolation=node.isolation,
            transport=binding.transport if binding else None,
            allowed_effects=node.required_effects,
            network_mode=NetworkMode.OPEN if egress else NetworkMode.DENY,
            network_destinations=(),
        )
    return ConfiguredExecutionPolicy(envelopes)


def _preflight(plan: ExecutionPlan) -> str | None:
    """Return a clear message if the graph has a node this phase cannot run, else None."""
    for node in plan.nodes:
        if node.kind == NodeKind.APPROVAL.value:
            return (
                f"node {node.node_id!r} is an approval checkpoint; human-approval execution "
                "via --execute is a later phase. Remove it to run the local-CLI graph."
            )
        if not is_egress_node(plan, node, _LOCAL_CLI_TRANSPORTS):
            return (
                f"node {node.node_id!r} (kind {node.kind}) is not an admitted local-CLI "
                "connector node; `bl graph run --execute` runs graphs whose nodes bind a "
                "connection with transport 'local_cli' (BYOK/HTTP and sandboxed tool "
                "execution are later phases)."
            )
    return None


def execute_graph_run(
    *,
    manifest_text: str,
    manifest_suffix: str,
    connections_raw: list[object],
    node_prompts: Mapping[str, str],
    out_dir: Path,
    organization_id: str = "local-org",
    project_id: str = "local-project",
    run_id: str = "graph-run",
    policy_digest: str = _DEFAULT_POLICY_DIGEST,
    json_out: bool = False,
    cli_profiles: Mapping[str, CliProfile] = CLI_PROFILES,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Compile a user manifest and run its admitted local-CLI connector nodes for real."""
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

    problem = _preflight(plan)
    if problem is not None:
        return _fail(json_out, problem)

    caps = probe_platform()
    try:
        enforcer = build_enforcer(plan, capabilities=caps)
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
    connector_worker = LocalCliConnectorWorker(
        identity=identity,
        artifact_store=store,
        resolver=NodeCliResolver(node_prompts, profiles=cli_profiles),
        workspace_root=out_dir / "work",
        organization_id=organization_id,
        project_id=project_id,
        environ=environ,
    )
    controller = GraphRunController(
        plan=plan,
        event_log=event_log,
        worker=_UnsupportedNodeWorker(),
        gate=StructuralAcceptanceGate(store, organization_id=organization_id, project_id=project_id),
        artifact_verifier=LocalArtifactVerifier(store),
        execution_policy=_build_policy(plan),
        execution_enforcer=enforcer,
        timestamp=_now_iso,
        actor="graph-controller",
        connector_worker=connector_worker,
        egress_transports=_LOCAL_CLI_TRANSPORTS,
    )
    projection = controller.run()
    _persist_run_dir(out_dir, plan, manifest_text, connections_raw, identity)
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
    return _report(json_out, out_dir, projection.state, arena)


def _persist_run_dir(
    out_dir: Path,
    plan: ExecutionPlan,
    manifest_text: str,
    connections_raw: list[object],
    identity: GraphRunIdentity,
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
        "mode": "local_cli",
        "organization_id": identity.organization_id,
        "plan_id": plan.plan_id,
        "policy_digest": plan.policy_digest,
        "project_id": identity.project_id,
        "run_id": identity.run_id,
        "platform": sys.platform,
    }
    (out_dir / "run-meta.json").write_text(json.dumps(run_meta, sort_keys=True), encoding="utf-8")


def _report(json_out: bool, out_dir: Path, run_state: str, arena: ArenaProjection) -> int:
    succeeded = run_state == "SUCCEEDED"
    digests = [n.artifact_digests[0] for n in arena.nodes if n.artifact_digests]
    if json_out:
        print(json.dumps({
            "execution": True,
            "mode": "local_cli",
            "run_state": run_state,
            "run_id": arena.run_id,
            "out": str(out_dir),
            "artifact_digests": digests,
        }, sort_keys=True))
        return 0 if succeeded else 2
    print("Local-CLI graph run — REAL execution (your own subscription agent CLI)")
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
