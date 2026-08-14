"""C1 slice 4 — connector-node routing / egress enforce-skip (ADR-12 D5).

The controller owns ONE classification (`is_egress_node`, gated by the deployment's opt-in
`egress_transports`) that drives BOTH the sandbox enforce-skip AND the choice of connector vs
subprocess worker — so they can never drift into running egress work as an unsandboxed subprocess.
An egress node (bound to a connection whose transport the deployment declared egress) runs on the
connector worker and skips the process-isolation enforcer; every other node is enforced and runs
on the subprocess worker. Egress declared but no connector worker wired => fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.events import GraphRunIdentity

_ROUTE = ResolvedRoute("openai", "codex", "in", False, "sha256:" + "c" * 64)
_DIGEST = "sha256:" + "d" * 64


def _egress_plan():
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "one-node-run", "version": "1.0.0",
        "nodes": [{
            "id": "research", "kind": "research_claim", "inputs": {}, "outputs": {"claim": "text"},
            "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
            "isolation": "workspace_only", "connection_slot": "model",
        }],
        "edges": [],
        "connection_slots": [{"id": "model", "requires": ["text_generation"], "data_class_max": "public"}],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })
    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64, package_digests=frozenset(),
        connections=({
            "binding_id": "binding-1", "slot_id": "model", "connector_id": "openai-api",
            "connector_version": "1.0.0", "connection_id": "conn-1",
            "admission_digest": "sha256:" + "b" * 64,
            "route_policy_digest": "sha256:" + "c" * 64, "provider_id": "openai",
            "model_target": "codex", "region": "in", "fallback": False, "capabilities": {"text_generation"},
            "data_class_max": "public", "allowed_effects": {"read_only"},
            "isolation": "workspace_only", "transport": "https", "admitted": True,
        },),
    ))


def _identity(plan) -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id, policy_digest=plan.policy_digest,
    )


@dataclass
class _SpyWorker:
    calls: list

    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0) -> WorkerResult:
        self.calls.append(node.node_id)
        return WorkerResult((_DIGEST,), _ROUTE, "https")


@dataclass
class _Gate:
    passed: bool

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        return GateVerdict(self.passed, "independent fixture gate")


class _Artifacts:
    def verify(self, *, identity, digests: tuple[str, ...]) -> None:
        return None


@dataclass
class _Enforcer:
    calls: list

    def enforce(self, *, plan, node, envelope) -> None:
        self.calls.append(node.node_id)


def _policy(plan) -> ConfiguredExecutionPolicy:
    return ConfiguredExecutionPolicy({
        node.node_id: ExecutionEnvelope(
            node.isolation,
            next((b.transport for b in plan.connection_bindings if b.binding_id == node.binding_id), None),
            node.required_effects,
            NetworkMode.DENY,
            (),
        )
        for node in plan.nodes
    })


def _controller(tmp_path, *, worker, enforcer, connector_worker=None, egress_transports=frozenset()):
    plan = _egress_plan()
    return GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=worker, gate=_Gate(True), artifact_verifier=_Artifacts(),
        execution_policy=_policy(plan), execution_enforcer=enforcer,
        timestamp=lambda: "2026-08-08T00:00:00Z",
        connector_worker=connector_worker, egress_transports=egress_transports,
    )


def test_egress_node_routes_to_connector_worker_and_skips_the_sandbox_enforcer(tmp_path):
    subprocess_worker, connector_worker, enforcer = _SpyWorker([]), _SpyWorker([]), _Enforcer([])
    controller = _controller(
        tmp_path, worker=subprocess_worker, enforcer=enforcer,
        connector_worker=connector_worker, egress_transports=frozenset({"https"}),
    )
    assert controller.run().state == "SUCCEEDED"
    assert connector_worker.calls == ["research"]  # egress node ran on the connector worker
    assert subprocess_worker.calls == []           # never on the subprocess worker
    assert enforcer.calls == []                    # and skipped the sandbox enforcer


def test_same_node_is_enforced_and_uses_the_subprocess_worker_when_not_egress(tmp_path):
    # SAME plan, egress_transports empty -> not egress -> enforcer runs + subprocess worker runs.
    subprocess_worker, enforcer = _SpyWorker([]), _Enforcer([])
    controller = _controller(tmp_path, worker=subprocess_worker, enforcer=enforcer)
    assert controller.run().state == "SUCCEEDED"
    assert subprocess_worker.calls == ["research"]
    assert enforcer.calls == ["research"]


def test_egress_node_without_a_connector_worker_fails_closed(tmp_path):
    subprocess_worker = _SpyWorker([])
    controller = _controller(
        tmp_path, worker=subprocess_worker, enforcer=_Enforcer([]),
        egress_transports=frozenset({"https"}),  # egress declared but NO connector worker wired
    )
    assert controller.run().state == "FAILED"
    assert subprocess_worker.calls == []  # never fell back to the subprocess worker
    assert controller.event_log.replay()[-2].event.payload["reason"] == "no connector worker configured for egress node"
