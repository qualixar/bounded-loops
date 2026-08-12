"""Deterministic, single-controller execution for an immutable graph plan."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS, NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.events import GraphRunIdentity, GraphRunProjection, UnsignedGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

# Effects whose real-world action cannot be safely repeated by an at-least-once
# re-drive without a per-effect idempotency key (ADR-12 D7).  Aliased from
# NETWORK_EFFECTS in authoring.py — the two sets name the same effects because
# network-bearing effects are exactly those that cannot be safely retried without
# an idempotency key.  They are kept as separate names to preserve the distinct
# semantic axes (ARCH-03).
_EFFECTFUL_EFFECTS = NETWORK_EFFECTS


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
    """The result of a gate evaluated outside the producer interface.

    ``evidence_digest`` optionally binds the gate's full evaluation record (a
    content-addressed artifact) so the verdict externalized into the receipt is
    tamper-evident, not merely a human-readable reason.
    """

    passed: bool
    reason: str
    evidence_digest: str | None = None


@dataclass(frozen=True)
class _AttemptOutcome:
    """The result of one attempt of a bounded loop node.

    Exactly one of three shapes:

    * ``succeeded`` — the gate accepted; the node is done.
    * ``failure`` set, ``terminal`` None — a RETRYABLE failure.  The caller records an
      attempt event and tries again while budget remains.  ``verdict`` is set only when
      the failure came from the gate, which is what separates a gate rejection from a
      worker fault when the per-attempt gate error rate is computed.
    * ``terminal`` set — the node already failed durably; the run stops.  Used for
      failures a retry cannot fix (denied execution environment, broken gate).
    """

    succeeded: bool = False
    failure: str | None = None
    verdict: dict[str, object] | None = None
    terminal: GraphRunProjection | None = None


_DEFAULT_MAX_ATTEMPTS = 1
# A ceiling exists so a typo in a manifest cannot request an effectively unbounded
# loop.  It is deliberately far below the authoring schema's own 1..1000 range: the
# retry budget multiplies the gate's per-attempt false-accept probability, so a very
# large budget silently degrades the guarantee the gate is there to provide.
_MAX_ATTEMPTS_CEILING = 100


def _node_event_key(node_id: str, event_type: str, attempt: int) -> str:
    """The idempotency key for one node lifecycle event.

    Attempt 1 keeps the pre-retry key format EXACTLY — ``node_id:event_type`` — so
    run directories written before retry existed still replay and resume.  Later
    attempts append the attempt number because the log raises
    ``GraphIntegrityError`` when one key is reused with a different payload
    (see ``GraphEventLog.append``), which would otherwise make a second
    ``node.running`` crash the run rather than record it.

    Do NOT "tidy" this into one uniform format: doing so silently breaks resume of
    every run directory produced before this change.
    """
    if attempt <= 1:
        return f"{node_id}:{event_type}"
    return f"{node_id}:{event_type}:{attempt}"


def _max_attempts(node: PlannedNode) -> int:
    """The node's retry budget, validated at the point of use.

    ``PlannedNode.budgets`` is ``Mapping[str, object]``, so the value is untyped and
    must be checked rather than cast.  Validation lives here as well as in manifest
    validation because a plan can be built programmatically through the runtime
    facade without passing through the manifest validator, and an unbounded loop is
    the one failure this component must never have.
    """
    raw = node.budgets.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)
    # bool is a subclass of int in Python, so True would otherwise read as 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphIntegrityError("max_attempts must be an integer")
    if raw < 1 or raw > _MAX_ATTEMPTS_CEILING:
        raise GraphIntegrityError(f"max_attempts must be between 1 and {_MAX_ATTEMPTS_CEILING}")
    if raw > 1 and (node.required_effects & _EFFECTFUL_EFFECTS):
        # The same D7 rule the resume path already enforces (see ``_states_from``): an
        # external / irreversible effect cannot be re-driven without a per-effect
        # idempotency key.  In-process retry is a re-drive too, so allowing a budget
        # above one here would let a node repeat a payment or an external write that
        # resume explicitly refuses to repeat — an asymmetry that double-spends.
        raise GraphIntegrityError(
            f"node {node.node_id!r} carries an external / irreversible effect, so it cannot "
            "retry without a per-effect idempotency key (D7); declare max_attempts: 1"
        )
    return raw


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


class ApprovalOutcome(str, Enum):
    """The decision the controller reads for a node paused awaiting a human.

    ``PENDING`` is the fail-closed default: with no decided outcome the run stays
    paused rather than proceeding.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalResolverPort(Protocol):
    """Reports whether a human has decided an approval node for THIS run/attempt.

    The controller never itself validates a human decision (roles, signatures,
    nonces live in the approvals use case); it only asks this port for the already
    recorded outcome, and treats ``PENDING`` as "keep waiting".
    """

    def resolve(self, *, identity: GraphRunIdentity, node: PlannedNode, attempt: int) -> "ApprovalOutcome": ...


def is_egress_node(plan: ExecutionPlan, node: PlannedNode, egress_transports: frozenset[str]) -> bool:
    """A connector/EGRESS node's work is an authorized network call over an admitted connection
    (a frontier model API), NOT a sandboxed subprocess — so it is routed to the connector worker
    and does NOT pass the process-isolation enforcer (egress is authorized inside the connector
    path). It is identified by being bound to a connection whose transport the deployment has
    declared an egress transport; ``egress_transports`` defaults to empty, so nothing is egress
    unless a deployment opts in (e.g. a local_cli connector stays a sandboxed subprocess)."""
    if node.binding_id is None:
        return False
    transport = next(
        (binding.transport for binding in plan.connection_bindings if binding.binding_id == node.binding_id),
        None,
    )
    return transport is not None and transport in egress_transports


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
        approval_resolver: ApprovalResolverPort | None = None,
        connector_worker: NodeWorkerPort | None = None,
        egress_transports: frozenset[str] = frozenset(),
    ) -> None:
        if worker is gate or connector_worker is gate:
            raise GraphIntegrityError("worker and independent gate must be separate objects")
        identity = event_log.identity
        if (
            identity.graph_digest != plan.source_graph_digest
            or identity.plan_digest != plan.plan_id
            or identity.policy_digest != plan.policy_digest
        ):
            raise GraphIntegrityError("event log identity does not match immutable execution plan")
        # Validate EVERY node's retry budget before any work starts, not when its node is
        # reached.  Reaching a node can itself fail first (a denied envelope, for example),
        # which would leave an illegal budget — including an effectful node with a retry
        # budget it must never have — undetected on that run.
        for planned in plan.nodes:
            _max_attempts(planned)
        self.plan = plan
        self.event_log = event_log
        self._worker = worker
        self._gate = gate
        self._artifact_verifier = artifact_verifier
        self._execution_policy = execution_policy
        self._execution_enforcer = execution_enforcer
        self._timestamp = timestamp
        self._actor = actor
        self._approval_resolver = approval_resolver
        self._connector_worker = connector_worker
        self._egress_transports = egress_transports
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
            if observed == "FAILED":
                # Unreachable via resume() (the FAILED-finalize gate precedes this) —
                # a defensive guard so the helper stays safe if ever reused: a run
                # that already failed is finalized, never re-driven.
                raise GraphIntegrityError(f"cannot resume: node {node.node_id!r} has already failed")
            if observed == "AWAITING_APPROVAL":
                # Paused for a human decision: no worker ran and no effect fired
                # (approval GATES the effect), so re-driving only re-consults the
                # decision. Safe to re-drive even for an effectful approval node.
                states[node.node_id] = NodeState.PENDING
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
                node = self._node(node_id)
                if node.kind == NodeKind.APPROVAL.value:
                    # A human checkpoint: it runs no worker. Consult the recorded
                    # decision — pause (return) if none yet, fail closed on reject,
                    # or record success on approve and drive on.
                    paused = self._resolve_approval(states, node_id, node)
                    if paused is not None:
                        return paused
                    continue
                states = dispatch_node(states, node_id)
                self._append_node(node_id, "node.starting", NodeState.STARTING)
                # Envelope authorization, egress classification and worker selection are
                # deterministic in the node and plan, so they are resolved ONCE outside the
                # retry loop.  Retrying any of them would burn budget re-deriving an
                # identical answer, and would inflate the attempt count with attempts that
                # never reached the gate — which would corrupt the per-attempt gate error
                # rate the attempt records exist to measure.
                try:
                    envelope = validate_execution_envelope(
                        self.plan, node,
                        self._execution_policy.authorize(plan=self.plan, node=node),
                    )
                except Exception:
                    return self._fail_node(states, node_id, "execution policy denied worker")
                # ONE classification drives BOTH the enforcer skip and the worker choice, so
                # they can never drift into an unsandboxed subprocess (single source of truth).
                egress = is_egress_node(self.plan, node, self._egress_transports)
                worker = self._connector_worker if egress else self._worker
                if worker is None:
                    # Egress node but no connector worker wired: fail closed. Never fall back to
                    # the subprocess worker — that would run egress work on the wrong (sandboxed)
                    # path, and the enforcer was already skipped for this node.
                    return self._fail_node(states, node_id, "no connector worker configured for egress node")
                terminal = self._run_node_loop(states, node_id, node, envelope, egress, worker)
                if terminal is not None:
                    return terminal
                continue

    def _run_node_loop(
        self,
        states: dict[str, NodeState],
        node_id: str,
        node: PlannedNode,
        envelope: ExecutionEnvelope,
        egress: bool,
        worker: NodeWorkerPort,
    ) -> GraphRunProjection | None:
        """Attempt one node until its independent gate accepts, or the budget runs out.

        Returns ``None`` when the node SUCCEEDED and the caller should drive the rest of
        the graph, or a terminal projection when the run must stop.

        Each non-final failure is recorded as an additive ``node.attempt.failed`` event
        rather than routed through ``_fail_node``, because ``_fail_node`` appends
        ``run.failed`` and ends the run — a retry must leave the node in flight.
        """
        budget = _max_attempts(node)
        for attempt in range(1, budget + 1):
            states[node_id] = NodeState.RUNNING
            self._append_node(node_id, "node.running", NodeState.RUNNING, attempt=attempt)
            outcome = self._attempt_node(states, node_id, node, envelope, egress, worker, attempt)
            if outcome.terminal is not None:
                return outcome.terminal
            if outcome.succeeded:
                return None
            reason = outcome.failure or "node attempt failed"
            # EVERY failed attempt is recorded, including the last one, so counting gate
            # rejections is a single uniform query over node.attempt.failed.  Recording
            # only the non-final failures would systematically undercount: whenever the
            # budget runs out ON a gate rejection, that rejection would appear solely on
            # the terminal node.failed and be missed.
            self._append_attempt_failed(node_id, attempt, reason, outcome.verdict)
            if attempt < budget:
                continue
            return self._fail_node(
                states, node_id, reason, attempt=attempt,
                verdict=outcome.verdict,
                # Only meaningful when a retry was actually available: a single-attempt
                # node did not "exhaust" anything, it simply failed.
                budget_exhausted=budget > 1,
            )
        # range(1, budget + 1) is non-empty because _max_attempts guarantees budget >= 1,
        # so the loop always returns.  Kept explicit rather than relying on that.
        raise GraphIntegrityError("node retry loop ended without a terminal outcome")

    def _attempt_node(
        self,
        states: dict[str, NodeState],
        node_id: str,
        node: PlannedNode,
        envelope: ExecutionEnvelope,
        egress: bool,
        worker: NodeWorkerPort,
        attempt: int,
    ) -> _AttemptOutcome:
        """Run one attempt: enforce, execute, verify artifacts, then gate.

        ``terminal`` is set for failures that a retry cannot fix — a denied execution
        environment, or a gate that is itself broken.  Retrying those would burn budget
        on an identical outcome, and retrying a malformed gate would mask the defect.
        """
        if not egress:
            # An egress/connector node runs no subprocess to sandbox — the sandbox
            # enforcer would deny it the network it must use — so egress authorization
            # happens inside the connector worker instead. Every other node is enforced,
            # on EVERY attempt: a later attempt must not run less isolated than the first.
            try:
                self._execution_enforcer.enforce(plan=self.plan, node=node, envelope=envelope)
            except Exception:
                return _AttemptOutcome(terminal=self._fail_node(
                    states, node_id, "execution environment denied worker", attempt=attempt,
                ))
        try:
            result = worker.execute(plan=self.plan, node=node, envelope=envelope)
        except Exception:
            return _AttemptOutcome(failure="worker execution failed")
        expected_route = self._expected_route_for(node)
        expected_transport = self._expected_transport_for(node)
        try:
            self._validate_result(result)
            self._validate_observed_route(expected_route, result.observed_route)
            self._validate_observed_transport(expected_transport, result.observed_transport)
            self._artifact_verifier.verify(
                identity=self.event_log.identity,
                digests=result.output_artifact_digests,
            )
        except Exception:
            return _AttemptOutcome(failure="worker output artifact verification failed")
        states[node_id] = NodeState.GATING
        self._append_node(node_id, "node.gating", NodeState.GATING, attempt=attempt)
        try:
            verdict = self._gate.evaluate(plan=self.plan, node=node, result=result)
        except Exception:
            return _AttemptOutcome(terminal=self._fail_node(
                states, node_id, "independent gate evaluation failed", attempt=attempt,
            ))
        if not isinstance(verdict, GateVerdict) or not self._verdict_is_wellformed(verdict):
            # A broken gate, not a failed attempt: retrying would spend the budget
            # against a verdict this controller cannot interpret, and would hide the
            # defect behind an eventual budget-exhausted failure.
            return _AttemptOutcome(terminal=self._fail_node(
                states, node_id, "independent gate returned an invalid verdict", attempt=attempt,
            ))
        if verdict.passed:
            states[node_id] = NodeState.SUCCEEDED
            self._append_node(
                node_id,
                "node.succeeded",
                NodeState.SUCCEEDED,
                attempt=attempt,
                artifact_digests=list(result.output_artifact_digests),
                verdict=self._verdict_body(verdict),
                **({"route": self._route_payload(expected_route)} if expected_route else {}),
                **({"transport": expected_transport} if expected_transport else {}),
                **self._isolation_payload(result),
            )
            return _AttemptOutcome(succeeded=True)
        return _AttemptOutcome(
            failure="independent gate rejected output",
            verdict=self._verdict_body(verdict),
        )

    def _append_attempt_failed(
        self, node_id: str, attempt: int, reason: str, verdict: dict[str, object] | None,
    ) -> None:
        """Record one non-final attempt without transitioning run state.

        ``verdict`` is present exactly when the attempt failed at the gate, so its
        presence discriminates a gate rejection from a worker fault when the per-attempt
        gate error rate is computed from the log.
        """
        payload: dict[str, object] = {"node_id": node_id, "attempt": attempt, "reason": reason}
        if verdict is not None:
            payload["verdict"] = verdict
        self._append(f"{node_id}:node.attempt.failed:{attempt}", "node.attempt.failed", payload)

    def _resolve_approval(
        self, states: dict[str, NodeState], node_id: str, node: PlannedNode,
    ) -> GraphRunProjection | None:
        """Apply the recorded human decision for an approval node.

        Returns a projection when the run must STOP — a fail-closed failure (no
        resolver, a rejection, or a malformed outcome) or a durable pause (no
        decision yet, so the node is left AWAITING_APPROVAL and the run stays
        resumable). Returns ``None`` when the node was approved: its
        ``node.succeeded`` receipt is written (the human decision is the
        independent gate) and the caller drives the remaining nodes.
        """
        # An approval node ALWAYS records that it reached the human gate first, so
        # every terminal transition is AWAITING_APPROVAL -> {SUCCEEDED, FAILED} — the
        # node never fails or succeeds straight from READY, which would be an
        # unprojectable receipt (READY has no terminal edge). It also keeps the honest
        # story: the receipt shows the node required human approval before its outcome.
        states[node_id] = NodeState.AWAITING_APPROVAL
        self._append_node(node_id, "node.awaiting_approval", NodeState.AWAITING_APPROVAL)
        if self._approval_resolver is None:
            return self._fail_node(states, node_id, "approval node reached without an approval resolver")
        try:
            outcome = self._approval_resolver.resolve(
                identity=self.event_log.identity, node=node, attempt=1,
                # attempt=1: approval nodes do not retry (ARCH-09); only the approval
                # decision itself is durable.  Multi-attempt retry tracking would require
                # a separate event kind and is not supported at this layer.
            )
        except Exception:
            # Consistent with worker/gate/policy failures: a resolver error fails the
            # run closed (durable FAILED) rather than escaping as an uncaught exception.
            return self._fail_node(states, node_id, "approval resolver evaluation failed")
        if outcome is ApprovalOutcome.APPROVED:
            states[node_id] = NodeState.SUCCEEDED
            self._append_node(node_id, "node.succeeded", NodeState.SUCCEEDED, artifact_digests=[])
            return None
        if outcome is ApprovalOutcome.REJECTED:
            return self._fail_node(states, node_id, "human approval was rejected")
        if outcome is not ApprovalOutcome.PENDING:
            return self._fail_node(states, node_id, "approval resolver returned an invalid outcome")
        # PENDING: no decision yet — stay paused; the hold receipt is already durable.
        return self.event_log.replay_projection()

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

    @staticmethod
    def _verdict_is_wellformed(verdict: GateVerdict) -> bool:
        """A gate that returns an empty reason or a non-digest evidence reference is
        malformed; the controller fails the node closed here rather than let a bad
        verdict reach (and be rejected by) the durable log as an uncaught error."""
        if not isinstance(verdict.passed, bool):
            return False
        if not isinstance(verdict.reason, str) or not verdict.reason:
            return False
        digest = verdict.evidence_digest
        if digest is not None and not (
            isinstance(digest, str)
            and digest.startswith("sha256:")
            and len(digest) == 71
            and all(character in "0123456789abcdef" for character in digest[7:])
        ):
            return False
        return True

    @staticmethod
    def _verdict_body(verdict: GateVerdict) -> dict[str, object]:
        """The externalized independent-gate verdict for the durable receipt.

        Records the gate's boolean decision and reason (and, when the gate supplies
        one, a content-addressed evidence digest) so a node's terminal state is
        gate-attested in the log, never inferred from the producer.
        """
        body: dict[str, object] = {"passed": verdict.passed, "reason": verdict.reason}
        if verdict.evidence_digest is not None:
            body["evidence_digest"] = verdict.evidence_digest
        return body

    def _fail_node(
        self, states: dict[str, NodeState], node_id: str, reason: str,
        *, verdict: dict[str, object] | None = None, attempt: int = 1,
        budget_exhausted: bool = False,
    ) -> GraphRunProjection:
        states[node_id] = NodeState.FAILED
        extra: dict[str, object] = {"verdict": verdict} if verdict is not None else {}
        if budget_exhausted:
            # Present only when a retry budget was actually available and spent, so a
            # reader can tell "ran out of attempts" from "failed on its only attempt".
            extra["budget_exhausted"] = True
        self._append_node(
            node_id, "node.failed", NodeState.FAILED, attempt=attempt, reason=reason, **extra,
        )
        self._append("run.failed", "run.failed", {"state": "FAILED"})
        return self.event_log.replay_projection()

    def _append_node(
        self,
        node_id: str,
        event_type: str,
        state: NodeState,
        *,
        attempt: int = 1,
        **extra: object,
    ) -> None:
        payload = {"node_id": node_id, "state": state.value, "attempt": attempt, **extra}
        self._append(_node_event_key(node_id, event_type, attempt), event_type, payload)

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
