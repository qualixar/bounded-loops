"""Deterministic, single-controller execution for an immutable graph plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    ExecutionEnforcerPort,
    ExecutionPolicyPort,
    validate_execution_envelope,
)
from bounded_loops.graph.application.schedule_ready import NodeState, derive_ready_nodes, dispatch_node
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.events import GraphRunIdentity, GraphRunProjection, UnsignedGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

# Effects whose real-world action cannot be safely repeated by an at-least-once
# re-drive without a per-effect idempotency key (ADR-12 D7).
_EFFECTFUL_EFFECTS = frozenset({Effect.EXTERNAL_WRITE, Effect.FINANCIAL, Effect.IRREVERSIBLE})


@dataclass(frozen=True)
class WorkerResult:
    """Declared immutable artifacts produced by one worker attempt.

    ``isolation_provider_id`` and ``enforced_controls`` are the honest per-node
    isolation receipt — which provider ran the node and the per-dimension controls
    it actually enforced ({net, fs_write, fs_read, pid, user, kernel, egress}).
    They are optional so a gate or a legacy worker may return only digests.
    """

    output_artifact_digests: tuple[str, ...]
    observed_route: ResolvedRoute | None = None
    observed_transport: str | None = None
    isolation_provider_id: str | None = None
    enforced_controls: Mapping[str, str] | None = None


@dataclass(frozen=True)
class GateVerdict:
    """The result of a gate evaluated outside the producer interface."""

    passed: bool
    reason: str


class NodeWorkerPort(Protocol):
    """Executes a planned node without deciding whether its output is valid."""

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
    ) -> WorkerResult: ...


class IndependentGatePort(Protocol):
    """Evaluates a worker result without executing the producer node."""

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult,
    ) -> GateVerdict: ...


class ArtifactVerifierPort(Protocol):
    """Confirms declared output digests exist for the owning graph tenant."""

    def verify(self, *, identity: GraphRunIdentity, digests: tuple[str, ...]) -> None: ...


class GraphRunController:
    """Run or resume a graph sequentially with durable transition evidence.

    One controller, one dispatch at a time. ``run()`` starts a fresh stream;
    ``resume()`` re-attaches to an interrupted (RUNNING) stream, rebuilds node
    state from the receipts, and continues at-least-once without weakening
    idempotency or the independent-gate invariant. Parallel capacity remains a
    separate follow-up contract.
    """

    def __init__(
        self,
        *,
        plan: ExecutionPlan,
        event_log: GraphEventLog,
        worker: NodeWorkerPort,
        gate: IndependentGatePort,
        artifact_verifier: ArtifactVerifierPort,
        execution_policy: ExecutionPolicyPort,
        execution_enforcer: ExecutionEnforcerPort,
        timestamp: Callable[[], str],
        actor: str = "graph-controller",
    ) -> None:
        if worker is gate:
            raise GraphIntegrityError("worker and independent gate must be separate objects")
        identity = event_log.identity
        if (
            identity.graph_digest != plan.source_graph_digest
            or identity.plan_digest != plan.plan_id
            or identity.policy_digest != plan.policy_digest
        ):
            raise GraphIntegrityError("event log identity does not match immutable execution plan")
        self.plan = plan
        self.event_log = event_log
        self._worker = worker
        self._gate = gate
        self._artifact_verifier = artifact_verifier
        self._execution_policy = execution_policy
        self._execution_enforcer = execution_enforcer
        self._timestamp = timestamp
        self._actor = actor
        self._head = "0" * 64

    def run(self) -> GraphRunProjection:
        """Execute a NEW run from an empty stream; any gate rejection fails closed."""
        projection = self.event_log.replay_projection()
        if projection.state != "EMPTY":
            raise GraphIntegrityError("fresh controller refuses to resume a non-empty graph stream; call resume()")
        self._head = projection.head_hash
        self._append("run.created", "run.created", {"state": "PENDING"})
        self._append("run.started", "run.started", {"state": "RUNNING"})
        states = {node.node_id: NodeState.PENDING for node in self.plan.nodes}
        return self._run_loop(states)

    def resume(self) -> GraphRunProjection:
        """Resume an interrupted run from its durable event log (at-least-once).

        A fresh controller instance re-attaches to a RUNNING stream, rebuilds each
        node's state from the verified receipts, and continues. A node that reached
        SUCCEEDED is left done; every other node is re-driven — its deterministic
        prefix events re-append as head-safe no-ops (see ``_append``) and its worker
        re-executes at-least-once (a content-addressed workspace re-promotion is
        idempotent; external-effect double-spend is guarded separately by
        idempotency keys). A terminal stream returns its projection unchanged; an
        EMPTY stream is a misuse — call ``run()``.
        """
        projection = self.event_log.replay_projection()
        if projection.state == "EMPTY":
            raise GraphIntegrityError("cannot resume an empty graph stream; call run()")
        # Every terminal state resumes idempotently (a finished run re-runs nothing).
        if projection.state in ("SUCCEEDED", "FAILED", "CANCELLED", "HALTED", "EXPIRED"):
            return projection
        if projection.state not in ("PENDING", "RUNNING"):
            raise GraphIntegrityError(f"cannot resume from graph state {projection.state}")
        self._head = projection.head_hash
        # A crash between run.created and run.started leaves a non-empty PENDING
        # stream that run() refuses; complete the start so it is never wedged.
        if projection.state == "PENDING":
            self._append("run.started", "run.started", {"state": "RUNNING"})
        latest = latest_node_states(self.plan, self.event_log.replay())
        # A crash between node.failed and run.failed leaves a RUNNING stream with a
        # FAILED node; finalize the terminal deterministically rather than re-drive a
        # run that has already failed.
        if any(observed["state"] == "FAILED" for observed in latest.values()):
            self._append("run.failed", "run.failed", {"state": "FAILED"})
            return self.event_log.replay_projection()
        return self._run_loop(self._states_from(latest))

    def _states_from(self, latest: dict[str, dict[str, object]]) -> dict[str, NodeState]:
        """Map rebuilt receipt states to controller states: a SUCCEEDED node stays
        done; every other node re-drives from PENDING. An EFFECTFUL node interrupted
        mid-execution (STARTING/RUNNING/GATING) cannot be re-driven safely without a
        resume idempotency key (ADR-12 D7), so it fails closed rather than risk a
        double external / irreversible effect."""
        states: dict[str, NodeState] = {}
        for node in self.plan.nodes:
            observed = latest[node.node_id]["state"]
            if observed == "SUCCEEDED":
                states[node.node_id] = NodeState.SUCCEEDED
                continue
            if observed in ("STARTING", "RUNNING", "GATING") and (node.required_effects & _EFFECTFUL_EFFECTS):
                raise GraphIntegrityError(
                    f"cannot safely resume: node {node.node_id!r} carries an external / irreversible effect and "
                    "was interrupted mid-execution; a resume idempotency key (D7) is required before re-driving it"
                )
            states[node.node_id] = NodeState.PENDING
        return states

    def _run_loop(self, states: dict[str, NodeState]) -> GraphRunProjection:
        while True:
            ready = derive_ready_nodes(self.plan, states)
            if not ready:
                if all(state is NodeState.SUCCEEDED for state in states.values()):
                    self._append("run.succeeded", "run.succeeded", {"state": "SUCCEEDED"})
                    return self.event_log.replay_projection()
                self._append("run.failed", "run.failed", {"state": "FAILED"})
                return self.event_log.replay_projection()
            for node_id in ready:
                states[node_id] = NodeState.READY
                self._append_node(node_id, "node.ready", NodeState.READY)
                states = dispatch_node(states, node_id)
                self._append_node(node_id, "node.starting", NodeState.STARTING)
                states[node_id] = NodeState.RUNNING
                self._append_node(node_id, "node.running", NodeState.RUNNING)
                node = self._node(node_id)
                try:
                    envelope = validate_execution_envelope(
                        self.plan, node,
                        self._execution_policy.authorize(plan=self.plan, node=node),
                    )
                except Exception:
                    return self._fail_node(states, node_id, "execution policy denied worker")
                try:
                    self._execution_enforcer.enforce(plan=self.plan, node=node, envelope=envelope)
                except Exception:
                    return self._fail_node(states, node_id, "execution environment denied worker")
                try:
                    result = self._worker.execute(plan=self.plan, node=node, envelope=envelope)
                except Exception:
                    return self._fail_node(states, node_id, "worker execution failed")
                try:
                    self._validate_result(result)
                    expected_route = self._expected_route_for(node)
                    self._validate_observed_route(expected_route, result.observed_route)
                    expected_transport = self._expected_transport_for(node)
                    self._validate_observed_transport(expected_transport, result.observed_transport)
                    self._artifact_verifier.verify(
                        identity=self.event_log.identity,
                        digests=result.output_artifact_digests,
                    )
                except Exception:
                    return self._fail_node(states, node_id, "worker output artifact verification failed")
                states[node_id] = NodeState.GATING
                self._append_node(node_id, "node.gating", NodeState.GATING)
                try:
                    verdict = self._gate.evaluate(plan=self.plan, node=node, result=result)
                except Exception:
                    return self._fail_node(states, node_id, "independent gate evaluation failed")
                if not isinstance(verdict, GateVerdict):
                    return self._fail_node(states, node_id, "independent gate returned an invalid verdict")
                if verdict.passed:
                    states[node_id] = NodeState.SUCCEEDED
                    self._append_node(
                        node_id,
                        "node.succeeded",
                        NodeState.SUCCEEDED,
                        artifact_digests=list(result.output_artifact_digests),
                        **({"route": self._route_payload(expected_route)} if expected_route else {}),
                        **({"transport": expected_transport} if expected_transport else {}),
                        **self._isolation_payload(result),
                    )
                    continue
                return self._fail_node(states, node_id, "independent gate rejected output")

    def _node(self, node_id: str) -> PlannedNode:
        return next(node for node in self.plan.nodes if node.node_id == node_id)

    def _expected_route_for(self, node: PlannedNode) -> ResolvedRoute | None:
        if node.binding_id is None:
            return None
        binding = next(binding for binding in self.plan.connection_bindings if binding.binding_id == node.binding_id)
        return ResolvedRoute(
            binding.provider_id, binding.model_target, binding.region,
            binding.fallback, binding.route_policy_digest,
        )

    def _expected_transport_for(self, node: PlannedNode) -> str | None:
        if node.binding_id is None:
            return None
        return next(
            binding.transport for binding in self.plan.connection_bindings
            if binding.binding_id == node.binding_id
        )

    @staticmethod
    def _validate_observed_route(expected: ResolvedRoute | None, observed: ResolvedRoute | None) -> None:
        if expected != observed:
            raise GraphIntegrityError("worker route identity does not match immutable execution plan")

    @staticmethod
    def _validate_observed_transport(expected: str | None, observed: str | None) -> None:
        if expected != observed:
            raise GraphIntegrityError("worker transport does not match immutable execution plan")

    @staticmethod
    def _isolation_payload(result: WorkerResult) -> dict[str, object]:
        """The per-node isolation receipt for the durable ``node.succeeded`` event.

        Empty when the worker did not report one (e.g. a legacy worker), so the
        event schema stays backward compatible.
        """
        if not result.isolation_provider_id or result.enforced_controls is None:
            return {}
        return {
            "isolation": {
                "provider_id": result.isolation_provider_id,
                "controls": {str(dim): str(status) for dim, status in dict(result.enforced_controls).items()},
            }
        }

    @staticmethod
    def _route_payload(route: ResolvedRoute) -> dict[str, object]:
        return {
            "provider_id": route.provider_id,
            "model_id": route.model_id,
            "region": route.region,
            "fallback": route.fallback,
            "policy_digest": route.policy_digest,
        }

    def _fail_node(
        self, states: dict[str, NodeState], node_id: str, reason: str,
    ) -> GraphRunProjection:
        states[node_id] = NodeState.FAILED
        self._append_node(node_id, "node.failed", NodeState.FAILED, reason=reason)
        self._append("run.failed", "run.failed", {"state": "FAILED"})
        return self.event_log.replay_projection()

    def _append_node(
        self,
        node_id: str,
        event_type: str,
        state: NodeState,
        **extra: object,
    ) -> None:
        payload = {"node_id": node_id, "state": state.value, "attempt": 1, **extra}
        self._append(f"{node_id}:{event_type}", event_type, payload)

    def _append(self, key: str, event_type: str, payload: dict[str, object]) -> None:
        stored = self.event_log.append(
            self._head,
            UnsignedGraphEvent(
                event_id=f"{self.event_log.identity.run_id}:{key}",
                idempotency_key=f"{self.event_log.identity.run_id}:{key}",
                event_type=event_type,
                timestamp=self._timestamp(),
                actor=self._actor,
                payload=payload,
            ),
        )
        # Advance the head only when this append EXTENDED the chain from the current
        # head (a new tip). When a resumed node re-drives its already-logged, fully
        # deterministic prefix (node.ready/starting/running/…), append() returns the
        # historical event idempotently; that event's previous_hash is not the live
        # head, so we must NOT move the head backward. The first genuinely-missing
        # event chains from the live head and advances it normally.
        if stored.previous_hash == self._head:
            self._head = stored.event_hash

    @staticmethod
    def _validate_result(result: WorkerResult) -> None:
        if not isinstance(result, WorkerResult):
            raise GraphIntegrityError("worker must return WorkerResult")
        if not all(isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 for value in result.output_artifact_digests):
            raise GraphIntegrityError("worker result contains an invalid artifact digest")
        if result.observed_route is not None and not isinstance(result.observed_route, ResolvedRoute):
            raise GraphIntegrityError("worker result contains an invalid route identity")
        if result.observed_transport is not None and (
            not isinstance(result.observed_transport, str) or not result.observed_transport
        ):
            raise GraphIntegrityError("worker result contains an invalid transport identity")
