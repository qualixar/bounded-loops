"""Bundled demonstration manifest and in-process demonstration collaborators.

Split from ``cli_graph.py`` to keep that module within the 800-line hard cap
(ARCH-05).  All public symbols (``DEMO_MANIFEST_YAML``, ``DEMO_CONNECTIONS_LIST``,
``cmd_graph_demo``) are re-exported from ``cli_graph.py`` for backward
compatibility — existing imports from ``cli_graph`` continue to work unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import (
    GateVerdict,
    GraphRunController,
    WorkerResult,
)
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.domain.artifacts import ArtifactPolicy
from bounded_loops.graph.domain.authoring import _NULL_POLICY_DIGEST
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

# ── bundled demonstration manifest ────────────────────────────────────────────

DEMO_MANIFEST_YAML: str = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: one-node-run
version: "1.0.0"
nodes:
  - id: research
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 1}
    effects: [read_only]
    isolation: workspace_only
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""

# JSON-serialisable list; sets are stored as sorted lists so round-trip
# through json.loads works.  CompileSnapshot converts via _candidate_from_raw.
DEMO_CONNECTIONS_LIST: list[dict[str, object]] = [
    {
        "binding_id": "binding-1",
        "slot_id": "model",
        "connector_id": "codex-cli",
        "connector_version": "1.0.0",
        "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64,
        "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "openai",
        "model_target": "codex",
        "region": "in",
        "fallback": False,
        "capabilities": ["text_generation"],
        "data_class_max": "public",
        "allowed_effects": ["read_only"],
        "isolation": "workspace_only",
        "transport": "local_cli",
        "admitted": True,
    }
]

_DEMO_ORG = "demo-org"
_DEMO_PROJECT = "demo-project"
_DEMO_RUN_ID = "demo-run-1"

# ── in-process demonstration collaborators ────────────────────────────────────


@dataclass
class _DemoWorker:
    """Writes one small in-memory artifact per node; returns the real digest."""

    _store: LocalArtifactStore
    _org: str
    _project: str

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        content = f"DEMONSTRATION NODE: {node.node_id}".encode("utf-8")
        policy = ArtifactPolicy(
            organization_id=self._org,
            project_id=self._project,
            producer_attempt=str(attempt),
            media_type="text/plain",
            sensitivity="public",
            retention_class="standard",
        )
        records = self._store.put_many([(BytesIO(content), policy)])
        digest = records[0].digest
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        if binding is None:
            return WorkerResult((digest,))
        route = ResolvedRoute(
            binding.provider_id,
            binding.model_target,
            binding.region,
            binding.fallback,
            binding.route_policy_digest,
        )
        return WorkerResult((digest,), route, binding.transport)


class _DemoGate:
    """Demonstration gate; always passes.  Distinct object from worker."""

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult
    ) -> GateVerdict:
        return GateVerdict(True, "demonstration gate: always passes")


class _DemoEnforcer:
    """No-op enforcer; the demo makes no isolation or network claims."""

    def enforce(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope
    ) -> None:
        pass


# ── private helpers ────────────────────────────────────────────────────────────


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_demo_plan() -> ExecutionPlan:
    graph = parse_authoring_graph_yaml(DEMO_MANIFEST_YAML)
    snapshot = CompileSnapshot(
        policy_digest=_NULL_POLICY_DIGEST,
        package_digests=frozenset(),
        connections=tuple(DEMO_CONNECTIONS_LIST),  # type: ignore[arg-type]
    )
    return compile_graph(graph, snapshot)


def _build_policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    envelopes: dict[str, ExecutionEnvelope] = {}
    for node in plan.nodes:
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        envelopes[node.node_id] = ExecutionEnvelope(
            isolation=node.isolation,
            transport=binding.transport if binding else None,
            allowed_effects=node.required_effects,
            network_mode=NetworkMode.DENY,
            network_destinations=(),
        )
    return ConfiguredExecutionPolicy(envelopes)


# ── handler ────────────────────────────────────────────────────────────────────


def cmd_graph_demo(args: argparse.Namespace) -> int:
    """bl graph demo --out <dir> — DEMONSTRATION; no sandbox or E2 enforcement."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = _build_demo_plan()
    identity = GraphRunIdentity(
        organization_id=_DEMO_ORG,
        project_id=_DEMO_PROJECT,
        run_id=_DEMO_RUN_ID,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )

    store = LocalArtifactStore(out_dir / "artifacts")
    event_log = GraphEventLog(out_dir / "controller-events.jsonl", identity)
    worker = _DemoWorker(store, _DEMO_ORG, _DEMO_PROJECT)
    gate = _DemoGate()
    verifier = LocalArtifactVerifier(store)
    policy = _build_policy(plan)
    enforcer = _DemoEnforcer()

    controller = GraphRunController(
        plan=plan,
        event_log=event_log,
        worker=worker,
        gate=gate,
        artifact_verifier=verifier,
        execution_policy=policy,
        execution_enforcer=enforcer,
        timestamp=_now_iso,
        actor="graph-controller",
    )
    projection = controller.run()

    # Persist artefacts needed for `status` reconstruction.
    (out_dir / "plan.json").write_bytes(plan.canonical_json)
    (out_dir / "manifest.yaml").write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    (out_dir / "connections.json").write_text(
        json.dumps(DEMO_CONNECTIONS_LIST, sort_keys=True), encoding="utf-8"
    )
    run_meta = {
        "demonstration": True, "organization_id": _DEMO_ORG,
        "plan_id": plan.plan_id, "policy_digest": plan.policy_digest,
        "project_id": _DEMO_PROJECT, "run_id": _DEMO_RUN_ID,
    }
    (out_dir / "run-meta.json").write_text(
        json.dumps(run_meta, sort_keys=True), encoding="utf-8")

    if projection.state != "SUCCEEDED":
        _err(f"graph demo: run ended with state {projection.state!r}; see event log")
        return 2

    _DEMO_NOTICE = (
        "DEMONSTRATION — nodes are NOT executed in a sandbox; "
        "no isolation / network / E2 enforcement."
    )
    banner = "=" * 70 + "\n" + _DEMO_NOTICE + "\n" + "=" * 70

    if getattr(args, "json", False):
        print(json.dumps(
            {
                "demonstration": True,
                "notice": _DEMO_NOTICE,
                "out": str(out_dir),
                "run_id": _DEMO_RUN_ID,
                "run_state": projection.state,
            },
            sort_keys=True,
        ))
    else:
        print(banner)
        print(f"run_state : {projection.state}")
        print(f"run_id    : {_DEMO_RUN_ID}")
        print(f"out       : {out_dir}")
    return 0
