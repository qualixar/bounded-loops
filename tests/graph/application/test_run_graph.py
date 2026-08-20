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
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.application.resume_states import states_from_receipts
from bounded_loops.graph.application.schedule_ready import NodeState
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent


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

    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0) -> WorkerResult:
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

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
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
    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0) -> WorkerResult:
        raise RuntimeError("provider output must not become a receipt reason")


class _MismatchedRouteWorker:
    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0) -> WorkerResult:
        return WorkerResult(("sha256:" + "d" * 64,), ResolvedRoute("other", "codex", "in", False, "sha256:" + "c" * 64))


class _TransportProofWorker:
    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0) -> WorkerResult:
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
        "node.spend", "node.gating", "node.succeeded", "run.succeeded",
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

    def execute(self, *, plan, node, envelope, attempt=1, repair_round=0) -> WorkerResult:
        if node.node_id == self._crash_on:
            raise _SimulatedCrash("simulated process crash")
        return WorkerResult(
            ("sha256:" + "d" * 64,),
            ResolvedRoute("openai", "codex", "in", False, "sha256:" + "c" * 64),
            "local_cli",
        )


def _controller(plan, event_log, worker, gate=None, continue_on_failure=False):
    return GraphRunController(
        plan=plan, event_log=event_log, worker=worker, gate=gate or _Gate(True, []),
        continue_on_failure=continue_on_failure,
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
    # run.resumed and node.redrive are the two records a resume DOES add: without them a
    # resume left no trace, so re-executing an incomplete attempt was unobservable.
    assert [e.event.event_type for e in resumed.event_log.replay()] == [
        "run.created", "run.started", "node.ready", "node.starting", "node.running",
        "run.resumed", "node.redrive", "node.spend", "node.gating", "node.succeeded",
        "run.succeeded",
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
        "run.resumed", "node.redrive", "node.spend", "node.gating", "node.succeeded",
        "run.succeeded",
    ]


class _CrashOnRunFailed(GraphEventLog):
    """Persists everything except the FIRST run.failed — models a crash after
    node.failed is durable but before the terminal run.failed is written."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._armed = True

    def append(self, expected_previous_hash, event):
        if event.event_type == "run.failed" and self._armed:
            self._armed = False
            raise _SimulatedCrash("crash before run.failed is durable")
        return super().append(expected_previous_hash, event)


def _effectful_plan():
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1", "graph_id": "effectful-run", "version": "1.0.0",
        "nodes": [{
            "id": "pay", "kind": "tool", "inputs": {}, "outputs": {"receipt": "text"},
            "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["external_write"],
            "isolation": "container_restricted", "connection_slot": "model", "tool_ref": "charge-v1",
        }],
        "edges": [],
        "connection_slots": [{"id": "model", "requires": ["text_generation"], "data_class_max": "public"}],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })
    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64, package_digests=frozenset(),
        connections=({
            "binding_id": "binding-1", "slot_id": "model", "connector_id": "codex-cli",
            "connector_version": "1.0.0", "connection_id": "conn-1", "admission_digest": "sha256:" + "b" * 64,
            "route_policy_digest": "sha256:" + "c" * 64, "provider_id": "openai", "model_target": "codex",
            "region": "in", "fallback": False, "capabilities": {"text_generation"}, "data_class_max": "public",
            "allowed_effects": {"external_write"}, "isolation": "container_restricted",
            "transport": "local_cli", "admitted": True,
        },),
    ))


def test_resume_completes_a_run_wedged_between_created_and_started(tmp_path):
    """A crash between run.created and run.started leaves a non-empty PENDING stream
    that run() refuses; resume() must complete the start and finish the run."""
    from bounded_loops.graph.domain.events import UnsignedGraphEvent

    plan = _plan()
    identity = _identity(plan)
    log = GraphEventLog(tmp_path / "events.jsonl", identity)
    log.append("0" * 64, UnsignedGraphEvent(
        event_id="run-1:run.created", idempotency_key="run-1:run.created", event_type="run.created",
        timestamp="2026-08-08T00:00:00Z", actor="graph-controller", payload={"state": "PENDING"}))
    assert log.replay_projection().state == "PENDING"

    worker = _Worker([])
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), worker)
    assert resumed.resume().state == "SUCCEEDED"
    assert worker.calls == ["research"]


def test_resume_finalizes_a_run_wedged_between_node_failed_and_run_failed(tmp_path):
    """A crash between node.failed and run.failed leaves a RUNNING stream with a
    FAILED node; resume() must finalize the terminal without re-driving."""
    plan = _plan()
    identity = _identity(plan)
    crash_log = _CrashOnRunFailed(tmp_path / "events.jsonl", identity)
    with pytest.raises(_SimulatedCrash):
        _controller(plan, crash_log, _Worker([]), gate=_Gate(False, [])).run()
    assert crash_log.replay_projection().state == "RUNNING"
    assert [e.event.event_type for e in crash_log.replay()][-1] == "node.failed"

    worker = _Worker([])
    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), worker)
    assert resumed.resume().state == "FAILED"
    assert worker.calls == []  # a run that already failed is never re-driven
    assert [e.event.event_type for e in resumed.event_log.replay()][-1] == "run.failed"


def test_resume_refuses_to_redrive_an_effectful_node_interrupted_mid_execution(tmp_path):
    """An external/irreversible-effect node interrupted mid-execution must fail closed
    (a resume idempotency key is required, ADR-12 D7) — never blindly re-executed."""
    plan = _effectful_plan()
    with pytest.raises(GraphIntegrityError, match="idempotency key"):
        states_from_receipts(
            plan, {"pay": {"state": "RUNNING", "attempt": 1}}, continue_on_failure=False,
        )
    # A never-started effectful node (PENDING) is fine to (re-)drive.
    assert states_from_receipts(
        plan, {"pay": {"state": "PENDING", "attempt": 0}}, continue_on_failure=False,
    )["pay"] is NodeState.PENDING


def _raw_append(store: GraphEventLog, head: str, event_type: str, key: str, payload: dict[str, object]) -> str:
    """Append one arbitrary, correctly hash-chained event — the tamperer's tool: it
    re-chains the log so the hash chain verifies, standing in for an attacker who
    recomputed the entire chain after editing it."""
    return store.append(
        head,
        UnsignedGraphEvent(
            event_id=f"event-{key}", idempotency_key=key, event_type=event_type,
            timestamp="2026-08-08T00:00:00Z", actor="controller", payload=payload,
        ),
    ).event_hash


def test_resume_rejects_a_forged_stream_where_a_child_succeeds_before_its_parent(tmp_path):
    """Cross-node causality guard on the resume path (finding H4a): a tampered, fully
    re-hash-chained log in which child ``b`` runs to SUCCEEDED while parent ``a`` never
    leaves PENDING keeps every per-node lifecycle legal but inverts DAG order. resume()
    rebuilds node state through latest_node_states and must fail closed rather than
    re-drive from an impossible ordering."""
    plan = _two_node_plan()
    identity = _identity(plan)
    store = GraphEventLog(tmp_path / "events.jsonl", identity)
    head = _raw_append(store, "0" * 64, "run.created", "run-1:run.created", {"state": "PENDING"})
    head = _raw_append(store, head, "run.started", "run-1:run.started", {"state": "RUNNING"})
    for event_type, state in (
        ("node.ready", "READY"), ("node.starting", "STARTING"),
        ("node.running", "RUNNING"), ("node.gating", "GATING"),
    ):
        head = _raw_append(store, head, event_type, f"b:{state}", {"node_id": "b", "state": state, "attempt": 1})
    _raw_append(store, head, "node.succeeded", "b:SUCCEEDED", {
        "node_id": "b", "state": "SUCCEEDED", "attempt": 1, "artifact_digests": ["sha256:" + "d" * 64],
    })
    # No terminal run event: the projection stays RUNNING, so resume() proceeds to
    # rebuild node state — exactly where the inverted DAG ordering is caught.
    assert GraphEventLog(tmp_path / "events.jsonl", identity).replay_projection().state == "RUNNING"

    resumed = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), _Worker([]))
    with pytest.raises(GraphIntegrityError, match="causal"):
        resumed.resume()


# ── F2 slice 3: the independent gate's verdict is externalized into the receipt ──

def test_node_succeeded_records_the_independent_gate_verdict(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_Gate(True, []), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "SUCCEEDED"
    succeeded = controller.event_log.replay()[-2]
    assert succeeded.event.event_type == "node.succeeded"
    assert succeeded.event.payload["verdict"] == {"passed": True, "reason": "independent fixture gate"}


def test_gate_rejection_records_the_verdict_on_node_failed(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_Gate(False, []), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "FAILED"
    failed = controller.event_log.replay()[-2]
    assert failed.event.event_type == "node.failed"
    assert failed.event.payload["reason"] == "independent gate rejected output"
    assert failed.event.payload["verdict"] == {"passed": False, "reason": "independent fixture gate"}


@dataclass
class _EvidenceGate:
    calls: list

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        self.calls.append(node.node_id)
        return GateVerdict(True, "audited", evidence_digest="sha256:" + "e" * 64)


def test_gate_evidence_digest_is_recorded_in_the_verdict(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_EvidenceGate([]), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    assert controller.run().state == "SUCCEEDED"
    succeeded = controller.event_log.replay()[-2]
    assert succeeded.event.payload["verdict"] == {
        "passed": True, "reason": "audited", "evidence_digest": "sha256:" + "e" * 64,
    }


@dataclass
class _MalformedVerdictGate:
    calls: list

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        self.calls.append(node.node_id)
        return GateVerdict(True, "", evidence_digest="not-a-digest")


def test_a_malformed_gate_verdict_fails_the_node_closed_and_stays_projectable(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_MalformedVerdictGate([]), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    projection = controller.run()

    # A malformed gate verdict is a clean FAILED, not an uncaught error or a false
    # SUCCEEDED — and the node.failed receipt carries no (malformed) verdict.
    assert projection.state == "FAILED"
    failed = controller.event_log.replay()[-2]
    assert failed.event.event_type == "node.failed"
    assert failed.event.payload["reason"] == "independent gate returned an invalid verdict"
    assert "verdict" not in failed.event.payload
    # No wedge: a fresh reader projects the durable log cleanly.
    assert GraphEventLog(tmp_path / "events.jsonl", _identity(plan)).replay_projection().state == "FAILED"


class _NonBoolVerdictGate:
    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        return GateVerdict(1, "ok")  # type: ignore[arg-type]  # non-bool decision is malformed


def test_a_non_bool_verdict_decision_fails_the_node_closed(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_NonBoolVerdictGate(), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    projection = controller.run()

    assert projection.state == "FAILED"
    failed = controller.event_log.replay()[-2]
    assert failed.event.payload["reason"] == "independent gate returned an invalid verdict"
    assert GraphEventLog(tmp_path / "events.jsonl", _identity(plan)).replay_projection().state == "FAILED"



# ── the verdict the controller CHECKED must be the verdict it ACTS ON ─────────────────────
#
# `verdict_is_wellformed` reads each field, then the controller reads them AGAIN for the
# `if verdict.passed:` branch and for `verdict_body`. A GateVerdict is a frozen dataclass, so
# fields cannot be REASSIGNED — but a subclass can define them as properties that answer
# differently per call, and `isinstance(verdict, GateVerdict)` accepts a subclass. The loop-gate
# boundary had the identical defect (see GuardedGate._validate) and closed it by returning a
# snapshot the harness built.


class _FlippingVerdictGate:
    """False for the wellformedness read, True for the branch that marks a node SUCCEEDED."""

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        class _Flip(GateVerdict):
            def __init__(self) -> None:
                object.__setattr__(self, "_n", 0)
                object.__setattr__(self, "evidence_digest", None)

            @property
            def passed(self):  # type: ignore[override]
                object.__setattr__(self, "_n", self._n + 1)
                return False if self._n <= 1 else True

            @property
            def reason(self):  # type: ignore[override]
                return "looks fine"

        return _Flip()


def test_a_verdict_that_flips_after_validation_cannot_pass_a_node(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_FlippingVerdictGate(), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    projection = controller.run()

    assert projection.state != "SUCCEEDED", (
        "a node succeeded on a verdict that validated as a FAILURE — the controller acted on a "
        "different read than the one it checked"
    )


class _FlippingDigestGate:
    """A well-formed sha256 for the validation read, a forged string for the receipt.

    `evidence_digest` exists so the verdict in the receipt is tamper-EVIDENT. A digest that never
    passed format validation reaching the durable log defeats the only thing that field is for.
    """

    GOOD = "sha256:" + "a" * 64
    FORGED = "sha256:" + "0" * 63 + "Z<forged>"

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        forged, good = self.FORGED, self.GOOD

        class _Flip(GateVerdict):
            def __init__(self) -> None:
                object.__setattr__(self, "_n", 0)

            @property
            def passed(self):  # type: ignore[override]
                return True

            @property
            def reason(self):  # type: ignore[override]
                return "gate passed"

            @property
            def evidence_digest(self):  # type: ignore[override]
                object.__setattr__(self, "_n", self._n + 1)
                return good if self._n <= 1 else forged

        return _Flip()


def test_a_forged_evidence_digest_cannot_reach_the_durable_receipt(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_FlippingDigestGate(), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    controller.run()

    recorded = [
        entry.event.payload.get("verdict", {}).get("evidence_digest")
        for entry in controller.event_log.replay()
        if isinstance(entry.event.payload.get("verdict"), dict)
    ]
    assert _FlippingDigestGate.FORGED not in recorded, (
        f"a digest that never passed validation is in the durable receipt: {recorded}"
    )


class _ExitingGate:
    """SystemExit is not an Exception. The loop-gate boundary catches BaseException for exactly
    this reason — its comment records that "a test with SystemExit(1) broke it"."""

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        raise SystemExit(1)


def test_a_gate_that_exits_the_process_fails_the_node_instead_of_escaping(tmp_path):
    plan = _plan()
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity(plan)),
        worker=_Worker([]), gate=_ExitingGate(), artifact_verifier=_artifacts(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer([]),
        timestamp=lambda: "2026-08-08T00:00:00Z",
    )

    projection = controller.run()   # must not raise SystemExit out of the controller

    assert projection.state == "FAILED"
    failed = controller.event_log.replay()[-2]
    assert failed.event.payload["reason"] == "independent gate evaluation failed"


# ── conditional edges end to end (P4.25a) ────────────────────────────────────────────────


def _guarded_graph(guard: str, fail_mode: str = "fail_closed") -> dict[str, object]:
    return {
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "guarded-run",
        "version": "1.0.0",
        "nodes": [
            {"id": "a", "kind": "research_claim", "inputs": {}, "outputs": {"claim": "text"},
             "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
             "isolation": "workspace_only"},
            {"id": "b", "kind": "research_claim", "inputs": {"ctx": "text"},
             "outputs": {"claim": "text"},
             "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
             "isolation": "workspace_only"},
        ],
        "edges": [{"from_node": "a", "from_port": "claim", "to_node": "b", "to_port": "ctx",
                   "when": guard}],
        "connection_slots": [],
        "policies": {"data_class": "public", "fail_mode": fail_mode},
    }


@pytest.mark.parametrize("guard", ["failed", "skipped", "terminal"])
def test_a_failure_conditioned_edge_is_REFUSED_under_fail_closed(guard):
    """The reachability rule, and the reason this phase is not finished.

    Under ``fail_closed`` the controller returns a terminal projection at the FIRST node failure, so
    the scheduler never runs again and a failure-conditioned edge can never be admitted. Accepting
    one would ship precisely the defect enforcing ``when`` was meant to close: a condition the
    author wrote, the engine stored, and nothing ever applied.

    This mirrors the existing ``_ON_FAILURE_UNIMPLEMENTED`` rule, which already refuses
    ``on_failure: continue|repair|await_human`` for the same reason.
    """
    with pytest.raises(GraphValidationError, match="can never be reached"):
        validate_authoring_graph(_guarded_graph(guard))


def test_a_succeeded_condition_is_accepted_because_it_is_reachable():
    graph = validate_authoring_graph(_guarded_graph("succeeded"))
    assert [edge.when for edge in graph.edges] == ["succeeded"]


def test_an_unconditional_graph_still_runs_unchanged(tmp_path):
    """The backward-compatibility lock: enforcing guards must not alter an unguarded run."""
    plan = _two_node_plan()
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(plan, log, worker).run()

    assert projection.state == "SUCCEEDED"
    assert worker.calls == ["a", "b"]
    assert "node.skipped" not in [e.event.event_type for e in log.replay()]
    assert latest_node_states(plan, log.replay())["b"]["state"] == "SUCCEEDED"


# ── continue_declared: routing around a failure (P4.25a-2) ───────────────────────────────


def _continue_plan(guard: str | None):
    """``a -> b`` under a fail mode that keeps driving the graph after a node fails.

    Mirrors ``_two_node_plan``'s slots and bindings: the shared stubs verify an observed route and
    transport against the node's binding, so an unbound node fails before it ever reaches the gate.
    """
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "continue-run",
        "version": "1.0.0",
        "nodes": [
            {"id": "a", "kind": "research_claim", "inputs": {}, "outputs": {"claim": "text"},
             "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
             "isolation": "workspace_only", "connection_slot": "model_a"},
            {"id": "b", "kind": "research_claim", "inputs": {"ctx": "text"},
             "outputs": {"claim": "text"},
             "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"],
             "isolation": "workspace_only", "connection_slot": "model_b"},
        ],
        "edges": [{"from_node": "a", "from_port": "claim", "to_node": "b", "to_port": "ctx",
                   "when": guard}],
        "connection_slots": [
            {"id": "model_a", "requires": ["text_generation"], "data_class_max": "public"},
            {"id": "model_b", "requires": ["text_generation"], "data_class_max": "public"},
        ],
        "policies": {"data_class": "public", "fail_mode": "continue_declared"},
    })

    def _binding(binding_id, slot_id, connection_id):
        return {
            "binding_id": binding_id, "slot_id": slot_id, "connector_id": "codex-cli",
            "connector_version": "1.0.0", "connection_id": connection_id,
            "admission_digest": "sha256:" + "b" * 64,
            "route_policy_digest": "sha256:" + "c" * 64, "provider_id": "openai",
            "model_target": "codex", "region": "in", "fallback": False,
            "capabilities": {"text_generation"},
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


@dataclass
class _GateRejecting:
    """An independent gate that rejects exactly ONE node, so a single node can fail in a run."""

    reject: str
    calls: list[tuple[str, tuple[str, ...]]]

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        self.calls.append((node.node_id, result.output_artifact_digests))
        return GateVerdict(node.node_id != self.reject, "selective fixture gate")


@dataclass
class _GateExploding:
    """A BROKEN gate: it raises instead of returning a verdict."""

    calls: list[str]

    def evaluate(self, *, plan, node, result, attempt=1, repair_round=0) -> GateVerdict:
        self.calls.append(node.node_id)
        raise RuntimeError("gate is broken")


def test_a_failure_conditioned_branch_runs_when_its_upstream_fails(tmp_path):
    """The whole point of the phase. 'a' is rejected by the independent gate, so 'b' runs.

    The run still reports FAILED — a failure is a failure — but the recovery branch executed.
    """
    plan = _continue_plan("failed")
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(
        plan, log, worker, gate=_GateRejecting("a", []), continue_on_failure=True,
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == ["a", "b"]  # the recovery branch really ran
    events = [e.event.event_type for e in log.replay()]
    assert "node.skipped" not in events  # the branch WAS taken
    # Exactly ONE run.failed, written at the end -- not mid-run, which would have sealed the
    # stream and made every later append illegal.
    assert events.count("run.failed") == 1
    assert events[-1] == "run.failed"


def test_the_untaken_branch_is_skipped_and_the_run_still_succeeds(tmp_path):
    """Mirror case: 'a' succeeds, so the ``failed`` branch is not taken and must be SKIPPED —
    and an untaken branch must not make an otherwise-clean run report FAILED."""
    plan = _continue_plan("failed")
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(plan, log, worker, continue_on_failure=True).run()

    assert projection.state == "SUCCEEDED"
    assert worker.calls == ["a"]
    skipped = [e.event.payload for e in log.replay() if e.event.event_type == "node.skipped"]
    assert skipped[0]["node_id"] == "b"
    assert skipped[0]["attempt"] == 0
    assert "branch not taken" in skipped[0]["reason"]
    assert latest_node_states(plan, log.replay())["b"]["state"] == "SKIPPED"


def test_an_UNGUARDED_dependency_failure_still_blocks_under_continue(tmp_path):
    """Continuing must not turn a failed dependency into a green light: 'b' has an
    unconditional edge, so it must never run even though the run kept going."""
    plan = _continue_plan(None)
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(
        plan, log, worker, gate=_GateRejecting("a", []), continue_on_failure=True,
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == ["a"]
    events = [e.event.event_type for e in log.replay()]
    assert "node.skipped" not in events  # blocked, NOT retired as an untaken branch


def test_a_broken_gate_halts_the_run_even_under_continue(tmp_path):
    """The HALT classification. A gate that raises has proved unreliable, so no later verdict
    from it can be trusted — continuing would keep gating on a known-broken authority."""
    plan = _continue_plan("failed")
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(
        plan, log, worker, gate=_GateExploding([]), continue_on_failure=True,
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == ["a"]  # 'b' never ran, despite its ``failed`` condition
    causes = [
        e.event.payload.get("cause") for e in log.replay()
        if e.event.event_type == "node.failed"
    ]
    assert causes == ["gate_broken"]


def test_the_default_fail_mode_still_stops_at_the_first_failure(tmp_path):
    """Backward compatibility: without continue_on_failure the run seals at the first failure.

    Uses an UNCONDITIONAL plan, because a failure-conditioned one is now refused outright by a
    controller built to halt — see the construction test below.
    """
    plan = _continue_plan(None)
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(plan, log, worker, gate=_GateRejecting("a", [])).run()

    assert projection.state == "FAILED"
    assert worker.calls == ["a"]  # 'b' blocked on its failed dependency



def test_a_controller_built_to_halt_REFUSES_TO_DRIVE_a_failure_conditioned_plan(tmp_path):
    """The hole this closes: a graph may declare a continue fail mode and still reach a controller
    assembled without it. Rather than silently ignore the condition, driving it fails."""
    plan = _continue_plan("failed")
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    with pytest.raises(GraphIntegrityError, match="built to stop at the first node failure"):
        _controller(plan, log, _Worker([])).run()


def test_an_ALREADY_TERMINAL_run_can_still_be_read_back_by_a_halting_controller(tmp_path):
    """The refusal guards DRIVING, and a sealed run drives nothing.

    Checking it in the constructor made an idempotent resume of a finished run raise instead of
    returning its projection, and made a legacy run directory with no recorded fail mode unreadable.
    Found by the P4.25a dual audit (Muse finding 3).
    """
    plan = _continue_plan("failed")
    identity = _identity(plan)
    sealed = _controller(
        plan, GraphEventLog(tmp_path / "events.jsonl", identity), _Worker([]),
        continue_on_failure=True,
    ).run()
    assert sealed.state in ("SUCCEEDED", "FAILED")

    # A fresh controller WITHOUT the flag must still construct and report the terminal projection.
    reader = _controller(plan, GraphEventLog(tmp_path / "events.jsonl", identity), _Worker([]))
    assert reader.resume().state == sealed.state


def test_an_unconditional_plan_is_unaffected_by_that_refusal(tmp_path):
    plan = _continue_plan(None)
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    assert _controller(plan, log, _Worker([])).run().state == "SUCCEEDED"


def test_a_resumed_run_keeps_driving_past_a_FAILED_node_under_continuation(tmp_path):
    """Fixing the resume seal exposed the next bug: states_from_receipts raised on a FAILED node,
    so continue_declared worked on a fresh run and broke the moment the run was resumed."""
    plan = _continue_plan("failed")
    latest = {
        "a": {"state": "FAILED", "attempt": 1},
        "b": {"state": "PENDING", "attempt": 0},
    }

    states = states_from_receipts(plan, latest, continue_on_failure=True)

    assert states["a"] is NodeState.FAILED   # settled, not re-driven, not an error
    assert states["b"] is NodeState.PENDING  # still drivable via its ``failed`` condition


def test_a_resumed_run_refuses_a_FAILED_node_when_it_must_halt(tmp_path):
    """Under a halting fail mode resume() finalizes before reaching here, so this stays a
    defensive guard rather than a reachable path."""
    plan = _continue_plan(None)
    with pytest.raises(GraphIntegrityError, match="has already failed"):
        states_from_receipts(
            plan, {"a": {"state": "FAILED", "attempt": 1}, "b": {"state": "PENDING", "attempt": 0}},
            continue_on_failure=False,
        )


def test_a_resumed_run_does_not_re_open_a_SKIPPED_branch(tmp_path):
    plan = _continue_plan("failed")
    states = states_from_receipts(
        plan,
        {"a": {"state": "SUCCEEDED", "attempt": 1}, "b": {"state": "SKIPPED", "attempt": 0}},
        continue_on_failure=True,
    )
    assert states["b"] is NodeState.SKIPPED


def test_a_forged_evidence_digest_cannot_pass_verdict_validation() -> None:
    """A `str` subclass must not satisfy the digest format check by overriding its methods.

    Found by the 0.7.1 self-attestation sweep. `validated_verdict_or_none` already read every
    field exactly once into a local — the documented fix for the check/use split — and that was
    necessary but NOT sufficient: the object still owned every method the check called. A
    subclass overriding `startswith`, `__len__` and `__getitem__` satisfied

        digest.startswith("sha256:") and len(digest) == 71 and all(c in hexdigits for c in digest[7:])

    while its real bytes were "not a digest at all", and that string was stored in the fresh
    GateVerdict and written into the receipt by `verdict_body` — defeating the only thing the
    field exists for.

    The remedy is `str.__str__` as an UNBOUND builtin, which a subclass cannot intercept:
    normalise first, validate the normalised value, store that.
    """
    from bounded_loops.graph.application.node_contracts import GateVerdict
    from bounded_loops.graph.application.node_receipts import (
        validated_verdict_or_none,
        verdict_body,
        verdict_is_wellformed,
    )

    class ForgedDigest(str):
        def startswith(self, *args: object, **kwargs: object) -> bool:
            return True

        def __len__(self) -> int:
            return 71

        def __getitem__(self, item: object) -> str:
            return "a" * 64 if isinstance(item, slice) else "a"

    forged = GateVerdict(
        passed=True, reason="looks fine", evidence_digest=ForgedDigest("not a digest at all"),
    )
    assert validated_verdict_or_none(forged) is None
    assert verdict_is_wellformed(forged) is False

    class LyingReason(str):
        """Empty bytes, non-zero length: the non-empty check must not consult the object."""

        def __len__(self) -> int:
            return 9

    assert validated_verdict_or_none(
        GateVerdict(passed=True, reason=LyingReason(""), evidence_digest=None),
    ) is None

    # Not over-tightened: a genuine digest is still accepted, and what gets STORED is a plain
    # `str` rather than whatever object the gate handed over.
    honest = GateVerdict(passed=True, reason="ok", evidence_digest="sha256:" + "a" * 64)
    validated = validated_verdict_or_none(honest)
    assert validated is not None
    assert type(validated.evidence_digest) is str
    assert type(validated.reason) is str
    assert verdict_body(validated)["evidence_digest"] == "sha256:" + "a" * 64
