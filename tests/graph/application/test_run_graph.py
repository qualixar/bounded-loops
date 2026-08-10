from __future__ import annotations

from dataclasses import dataclass

import pytest

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
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity


def _plan():
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "one-node-run",
        "version": "1.0.0",
        "nodes": [{
            "id": "research",
            "kind": "research_claim",
            "inputs": {},
            "outputs": {"claim": "text"},
            "budget": {"max_attempts": 1, "max_wallclock_s": 1},
            "effects": ["read_only"],
            "isolation": "workspace_only",
            "connection_slot": "model",
        }],
        "edges": [],
        "connection_slots": [{"id": "model", "requires": ["text_generation"], "data_class_max": "public"}],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })
    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64,
        package_digests=frozenset(),
        connections=({
            "binding_id": "binding-1", "slot_id": "model", "connector_id": "codex-cli",
            "connector_version": "1.0.0", "connection_id": "conn-1",
            "admission_digest": "sha256:" + "b" * 64,
            "route_policy_digest": "sha256:" + "c" * 64, "provider_id": "openai",
            "model_target": "codex", "region": "in", "fallback": False, "capabilities": {"text_generation"},
            "data_class_max": "public", "allowed_effects": {"read_only"},
            "isolation": "workspace_only", "transport": "local_cli", "admitted": True,
        },),
    ))


def _identity(plan) -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1",
        project_id="project-1",
        run_id="run-1",
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )


@dataclass
class _Worker:
    calls: list[str]

    def execute(self, *, plan, node, envelope) -> WorkerResult:
        self.calls.append(node.node_id)
        return WorkerResult(
            ("sha256:" + "d" * 64,),
            ResolvedRoute("openai", "codex", "in", False, "sha256:" + "c" * 64),
            "local_cli",
        )


@dataclass
class _Gate:
    passed: bool
    calls: list[tuple[str, tuple[str, ...]]]

    def evaluate(self, *, plan, node, result) -> GateVerdict:
        self.calls.append((node.node_id, result.output_artifact_digests))
        return GateVerdict(self.passed, "independent fixture gate")


@dataclass
class _Artifacts:
    accepted: set[str]
    calls: list[tuple[str, ...]]

    def verify(self, *, identity, digests: tuple[str, ...]) -> None:
        self.calls.append(digests)
        if not set(digests) <= self.accepted:
            raise GraphIntegrityError("artifact is missing or unavailable to this tenant")


class _ExplodingWorker:
    def execute(self, *, plan, node, envelope) -> WorkerResult:
        raise RuntimeError("provider output must not become a receipt reason")


class _MismatchedRouteWorker:
    def execute(self, *, plan, node, envelope) -> WorkerResult:
        return WorkerResult(("sha256:" + "d" * 64,), ResolvedRoute("other", "codex", "in", False, "sha256:" + "c" * 64))


class _TransportProofWorker:
    def execute(self, *, plan, node, envelope) -> WorkerResult:
        return WorkerResult(
            ("sha256:" + "d" * 64,),
            ResolvedRoute("openai", "codex", "in", False, "sha256:" + "c" * 64),
            "local_cli",
        )


class _DenyPolicy:
    def authorize(self, *, plan, node):
        raise GraphValidationError("execution_envelope", "/node_id", "node is denied")


@dataclass
class _Enforcer:
    calls: list[str]

    def enforce(self, *, plan, node, envelope) -> None:
        self.calls.append(node.node_id)


class _DenyEnforcer:
    def enforce(self, *, plan, node, envelope) -> None:
        raise GraphValidationError("execution_environment", "/envelope", "sandbox is unavailable")


def _artifacts() -> _Artifacts:
    return _Artifacts({"sha256:" + "d" * 64}, [])


def _policy(plan) -> ConfiguredExecutionPolicy:
    return ConfiguredExecutionPolicy({
        node.node_id: ExecutionEnvelope(
            node.isolation,
            next((binding.transport for binding in plan.connection_bindings if binding.binding_id == node.binding_id), None),
            node.required_effects,
            NetworkMode.DENY,
            (),
        )
        for node in plan.nodes
    })


def test_controller_records_worker_and_independent_gate_before_success(tmp_path):
    plan = _plan()
    worker = _Worker([])
    gate = _Gate(True, [])
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=worker,
        gate=gate,
        artifact_verifier=_artifacts(),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "SUCCEEDED"
    assert worker.calls == ["research"]
    assert gate.calls == [("research", ("sha256:" + "d" * 64,))]
    assert [event.event.event_type for event in controller.event_log.replay()] == [
        "run.created", "run.started", "node.ready", "node.starting", "node.running",
        "node.gating", "node.succeeded", "run.succeeded",
    ]
    succeeded = controller.event_log.replay()[-2]
    assert succeeded.event.payload["route"] == {
        "model_id": "codex", "policy_digest": "sha256:" + "c" * 64,
        "provider_id": "openai", "region": "in", "fallback": False,
    }


def test_controller_fails_closed_when_independent_gate_rejects_output(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]),
        gate=_Gate(False, []),
        artifact_verifier=_artifacts(),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    assert "node.succeeded" not in [event.event.event_type for event in controller.event_log.replay()]


def test_controller_refuses_same_object_as_worker_and_gate(tmp_path):
    plan = _plan()
    worker = _Worker([])
    with pytest.raises(GraphIntegrityError, match="independent"):
        GraphRunController(
            plan=plan,
            event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
            worker=worker,
            gate=worker,
            artifact_verifier=_artifacts(),
            execution_policy=_policy(plan),
            execution_enforcer=_Enforcer([]),
            timestamp=lambda: "2026-08-08T00:00:00Z",
        )


def test_controller_records_a_terminal_failure_when_worker_raises(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_ExplodingWorker(),
        gate=_Gate(True, []),
        artifact_verifier=_artifacts(),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    failed = controller.event_log.replay()[-2]
    assert failed.event.event_type == "node.failed"
    assert failed.event.payload["reason"] == "worker execution failed"


def test_controller_fails_before_gate_when_declared_artifact_is_not_verifiable(tmp_path):
    plan = _plan()
    gate = _Gate(True, [])
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]),
        gate=gate,
        artifact_verifier=_Artifacts(set(), []),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    assert gate.calls == []


def test_controller_fails_before_gate_when_worker_route_differs_from_plan(tmp_path):
    plan = _plan()
    gate = _Gate(True, [])
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_MismatchedRouteWorker(), gate=gate, artifact_verifier=_artifacts(),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    assert gate.calls == []


def test_controller_requires_and_records_the_transport_bound_by_the_plan(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_TransportProofWorker(), gate=_Gate(True, []), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "SUCCEEDED"
    assert controller.event_log.replay()[-2].event.payload["transport"] == "local_cli"


def test_controller_denies_execution_policy_before_worker_invocation(tmp_path):
    plan = _plan()
    worker = _Worker([])
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=worker, gate=_Gate(True, []), artifact_verifier=_artifacts(),
        execution_policy=_DenyPolicy(), timestamp=lambda: "2026-08-08T00:00:00Z",
        execution_enforcer=_Enforcer([]),
    )

    assert controller.run().state == "FAILED"
    assert worker.calls == []
    assert controller.event_log.replay()[-2].event.payload["reason"] == "execution policy denied worker"


def test_controller_denies_unenforced_environment_before_worker_invocation(tmp_path):
    plan = _plan()
    worker = _Worker([])
    controller = GraphRunController(
        plan=plan,
        event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=worker, gate=_Gate(True, []), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_DenyEnforcer(),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    assert worker.calls == []
    assert controller.event_log.replay()[-2].event.payload["reason"] == "execution environment denied worker"


# ── F2: durable resume / crash recovery ────────────────────────────────────────

class _SimulatedCrash(BaseException):
    """A BaseException (NOT Exception), so the controller's ``except Exception``
    does not catch it — models a hard process crash mid-node (``node.running`` is
    already persisted, no terminal node event is written)."""


class _CrashWorker:
    def __init__(self, crash_on: str) -> None:
        self._crash_on = crash_on

    def execute(self, *, plan, node, envelope) -> WorkerResult:
        if node.node_id == self._crash_on:
            raise _SimulatedCrash("simulated process crash")
        return WorkerResult(
            ("sha256:" + "d" * 64,),
            ResolvedRoute("openai", "codex", "in", False, "sha256:" + "c" * 64),
            "local_cli",
        )


def _controller(plan, event_log, worker, gate=None):
    return GraphRunController(
        plan=plan, event_log=event_log, worker=worker, gate=gate or _Gate(True, []),
        artifact_verifier=_artifacts(), execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]), timestamp=lambda: "2026-08-08T00:00:00Z",
    )


def _two_node_plan():
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "two-node-run",
        "version": "1.0.0",
        "nodes": [
            {"id": "a", "kind": "research_claim", "inputs": {}, "outputs": {"claim": "text"},
             "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
             "isolation": "workspace_only", "connection_slot": "model_a"},
            {"id": "b", "kind": "research_claim", "inputs": {"ctx": "text"}, "outputs": {"claim": "text"},
             "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
             "isolation": "workspace_only", "connection_slot": "model_b"},
        ],
        "edges": [{"from_node": "a", "from_port": "claim", "to_node": "b", "to_port": "ctx", "when": None}],
        "connection_slots": [
            {"id": "model_a", "requires": ["text_generation"], "data_class_max": "public"},
            {"id": "model_b", "requires": ["text_generation"], "data_class_max": "public"},
        ],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })

    def _binding(binding_id, slot_id, connection_id):
        return {
            "binding_id": binding_id, "slot_id": slot_id, "connector_id": "codex-cli",
            "connector_version": "1.0.0", "connection_id": connection_id,
            "admission_digest": "sha256:" + "b" * 64,
            "route_policy_digest": "sha256:" + "c" * 64, "provider_id": "openai",
            "model_target": "codex", "region": "in", "fallback": False, "capabilities": {"text_generation"},
            "data_class_max": "public", "allowed_effects": {"read_only"},
            "isolation": "workspace_only", "transport": "local_cli", "admitted": True,
        }

    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64, package_digests=frozenset(),
        connections=(
            _binding("binding-1", "model_a", "conn-1"),
            _binding("binding-2", "model_b", "conn-2"),
        ),
    ))


def test_resume_redrives_a_node_interrupted_mid_execution(tmp_path):
    plan = _plan()
    identity = _identity(plan)
    log1 = GraphEventLog(tmp_path / "events.jsonl", identity)
    with pytest.raises(_SimulatedCrash):
        _controller(plan, log1, _CrashWorker(crash_on="research")).run()
    # Mid-node: node.running is persisted, no terminal node event; projection RUNNING.
    assert [e.event.event_type for e in log1.replay()] == [
        "run.created", "run.started", "node.ready", "node.starting", "node.running",
    ]
    assert log1.replay_projection().state == "RUNNING"

    # A fresh controller re-attaches to the same stream and completes.
    worker = _Worker([])
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), worker)
    assert resumed.resume().state == "SUCCEEDED"
    assert worker.calls == ["research"]  # re-driven exactly once
    # No duplicated events — the deterministic prefix re-appended as head-safe no-ops.
    assert [e.event.event_type for e in resumed.event_log.replay()] == [
        "run.created", "run.started", "node.ready", "node.starting", "node.running",
        "node.gating", "node.succeeded", "run.succeeded",
    ]


def test_resume_skips_a_succeeded_node_and_redrives_the_incomplete_one(tmp_path):
    plan = _two_node_plan()
    identity = _identity(plan)
    log1 = GraphEventLog(tmp_path / "events.jsonl", identity)
    with pytest.raises(_SimulatedCrash):  # 'a' succeeds, then crash mid-'b'
        _controller(plan, log1, _CrashWorker(crash_on="b")).run()
    assert log1.replay_projection().state == "RUNNING"

    worker = _Worker([])
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), worker)
    assert resumed.resume().state == "SUCCEEDED"
    assert worker.calls == ["b"]  # 'a' is already SUCCEEDED and is not re-run


def test_resume_of_a_terminal_run_is_idempotent(tmp_path):
    plan = _plan()
    identity = _identity(plan)
    assert _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), _Worker([])).run().state == "SUCCEEDED"
    before = [e.event.event_type for e in GraphEventLog(tmp_path / "events.jsonl", identity).replay()]

    worker = _Worker([])
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), worker)
    assert resumed.resume().state == "SUCCEEDED"
    assert worker.calls == []  # completed work is never re-run
    assert [e.event.event_type for e in resumed.event_log.replay()] == before  # nothing appended


def test_resume_refuses_an_empty_stream(tmp_path):
    plan = _plan()
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    with pytest.raises(GraphIntegrityError, match="empty"):
        _controller(plan, log, _Worker([])).resume()


def test_run_refuses_to_resume_a_nonempty_stream(tmp_path):
    plan = _plan()
    identity = _identity(plan)
    assert _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), _Worker([])).run().state == "SUCCEEDED"
    with pytest.raises(GraphIntegrityError, match="resume"):
        _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), _Worker([])).run()


def test_resume_tolerates_a_live_clock_different_from_the_crashed_run(tmp_path):
    """Production resumes in a NEW process with a live clock: re-appending a node's
    deterministic prefix carries a fresh timestamp, which must be deduped as the
    same logical event — never rejected as a reused idempotency key."""
    import itertools

    plan = _plan()
    identity = _identity(plan)
    with pytest.raises(_SimulatedCrash):  # crash under a fixed T0 clock
        _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), _CrashWorker(crash_on="research")).run()

    tick = itertools.count()  # a DIFFERENT, advancing clock for the resume
    worker = _Worker([])
    resumed = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", identity), worker=worker,
        gate=_Gate(True, []), artifact_verifier=_artifacts(), execution_policy=_policy(plan),
        execution_enforcer=_Enforcer([]), timestamp=lambda: "2026-08-08T00:00:%02dZ" % next(tick),
    )
    assert resumed.resume().state == "SUCCEEDED"
    assert worker.calls == ["research"]
    assert [e.event.event_type for e in resumed.event_log.replay()] == [
        "run.created", "run.started", "node.ready", "node.starting", "node.running",
        "node.gating", "node.succeeded", "run.succeeded",
    ]
