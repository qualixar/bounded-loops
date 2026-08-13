"""Deterministic, single-controller execution for an immutable graph plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.execution_policy import (
    ExecutionEnvelope,
    ExecutionEnforcerPort,
    ExecutionPolicyPort,
    validate_execution_envelope,
)
from bounded_loops.graph.application.node_contracts import (
    ApprovalOutcome,
    ApprovalResolverPort,
    ArtifactVerifierPort,
    GateVerdict,
    IndependentGatePort,
    NodeWorkerPort,
    WorkerResult,
)
from bounded_loops.graph.application.node_receipts import (
    isolation_payload,
    node_event_key,
    route_payload,
    validate_observed_route,
    validate_observed_transport,
    usage_payload,
    validate_worker_result,
    verdict_body,
    verdict_is_wellformed,
)
from bounded_loops.graph.application.node_spend import (
    NodeSpend,
    ResumeCursor,
    consumed_attempts_from,
    consumed_spend_from,
    spend_refusal,
)
from bounded_loops.graph.application.schedule_ready import NodeState, derive_ready_nodes, dispatch_node
from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS, NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.events import (
    NodeFailureCause,
    GraphRunProjection,
    UnsignedGraphEvent,
)
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.domain.usage import WorkerUsage

# Effects whose real-world action cannot be safely repeated by an at-least-once
# re-drive without a per-effect idempotency key (ADR-12 D7).  Aliased from
# NETWORK_EFFECTS in authoring.py — the two sets name the same effects because
# network-bearing effects are exactly those that cannot be safely retried without
# an idempotency key.  They are kept as separate names to preserve the distinct
# semantic axes (ARCH-03).
_EFFECTFUL_EFFECTS = NETWORK_EFFECTS


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
    cause: NodeFailureCause | None = None
    verdict: dict[str, object] | None = None
    terminal: GraphRunProjection | None = None
    #: What the failed attempt consumed, when the worker got far enough to report it. A
    #: rejected attempt still costs money, so this must survive onto its attempt record.
    usage: WorkerUsage | None = None


# An attempt that never completes can be re-driven once per resume, and the prefix events
# de-duplicate, so nothing in the log advances.  This caps that: an external loop killing
# the worker before it reaches its gate can no longer buy unbounded executions against a
# bounded attempt count.  Fails closed on exhaustion; the pause-for-approval upgrade
# belongs with the run-level spend budget.
_MAX_REDRIVES_PER_ATTEMPT = 3

_DEFAULT_MAX_ATTEMPTS = 1
# A ceiling exists so a typo in a manifest cannot request an effectively unbounded
# loop.  It is deliberately far below the authoring schema's own 1..1000 range: the
# retry budget multiplies the gate's per-attempt false-accept probability, so a very
# large budget silently degrades the guarantee the gate is there to provide.
_MAX_ATTEMPTS_CEILING = 100


def _spend_caps(node: PlannedNode) -> tuple[int | None, int | None]:
    """The node's ``(max_tokens, max_cost_microunits)`` caps, validated at the point of use.

    Same reasoning as ``_max_attempts``: ``PlannedNode.budgets`` is untyped, and a plan can
    be built programmatically through the runtime facade without passing the manifest
    validator, so the value is checked here rather than trusted.

    ``0`` is a legitimate cost cap — "this node may not spend money at all" — so the floor
    differs per dimension: tokens must be at least 1 (a node that may not use a single token
    cannot do anything, which is a mis-authored graph rather than a policy), cost may be 0.
    """
    return (
        _optional_cap(node, "max_tokens", minimum=1),
        _optional_cap(node, "max_cost_microunits", minimum=0),
    )


def _optional_cap(node: PlannedNode, field: str, *, minimum: int) -> int | None:
    raw = node.budgets.get(field)
    if raw is None:
        return None
    # bool is a subclass of int, so True would otherwise read as a cap of 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphIntegrityError(f"{field} must be an integer")
    if raw < minimum:
        raise GraphIntegrityError(f"{field} must be at least {minimum}")
    return raw


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
        # Validate EVERY node's budgets before any work starts, not when its node is
        # reached.  Reaching a node can itself fail first (a denied envelope, for example),
        # which would leave an illegal budget — including an effectful node with a retry
        # budget it must never have — undetected on that run.
        for planned in plan.nodes:
            _max_attempts(planned)
            _spend_caps(planned)
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
        return self._run_loop(
            states, ResumeCursor.empty(tuple(node.node_id for node in self.plan.nodes)),
        )

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
        receipts = self.event_log.replay()
        # Record the resume itself before doing any work. Previously a resume left no trace
        # at all, so neither an operator nor the Arena could tell a run had been resumed —
        # let alone how often.
        # Skip the record when the PREVIOUS event is already a run.resumed: that resume
        # advanced nothing, so this one has nothing new to attest either. A client polling
        # resume() while waiting for a human approval decision would otherwise grow the log
        # without bound, one event per poll, with no work done between them.
        if receipts and receipts[-1].event.event_type != "run.resumed":
            resumes = sum(1 for stored in receipts if stored.event.event_type == "run.resumed")
            self._append(
                f"run.resumed:{resumes + 1}", "run.resumed", {"resume_ordinal": resumes + 1},
            )
        receipts = self.event_log.replay()
        latest = latest_node_states(self.plan, receipts)
        # A crash between node.failed and run.failed leaves a RUNNING stream with a
        # FAILED node; finalize the terminal deterministically rather than re-drive a
        # run that has already failed.
        if any(observed["state"] == "FAILED" for observed in latest.values()):
            self._append("run.failed", "run.failed", {"state": "FAILED"})
            return self.event_log.replay_projection()
        return self._run_loop(self._states_from(latest), consumed_attempts_from(self.plan, receipts))

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

    def _run_loop(
        self, states: dict[str, NodeState], cursor: ResumeCursor,
    ) -> GraphRunProjection:
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
                # The attempt this node is ABOUT to make.  Every fail-closed exit below must
                # carry it: defaulting to 1 would append a lower attempt number after a
                # higher one on any resume that fails re-authorization, which is the same
                # unreadable-stream corruption the attempt cursor exists to prevent.
                at = cursor.spent.get(node_id, 0) + 1
                try:
                    envelope = validate_execution_envelope(
                        self.plan, node,
                        self._execution_policy.authorize(plan=self.plan, node=node),
                    )
                except Exception:
                    return self._fail_node(
                        states, node_id, "execution policy denied worker", attempt=at,
                        cause=NodeFailureCause.POLICY_DENIED,
                    )
                # ONE classification drives BOTH the enforcer skip and the worker choice, so
                # they can never drift into an unsandboxed subprocess (single source of truth).
                egress = is_egress_node(self.plan, node, self._egress_transports)
                worker = self._connector_worker if egress else self._worker
                if worker is None:
                    # Egress node but no connector worker wired: fail closed. Never fall back to
                    # the subprocess worker — that would run egress work on the wrong (sandboxed)
                    # path, and the enforcer was already skipped for this node.
                    return self._fail_node(
                        states, node_id, "no connector worker configured for egress node",
                        attempt=at, cause=NodeFailureCause.NO_WORKER,
                    )
                terminal = self._run_node_loop(
                    states, node_id, node, envelope, egress, worker, cursor,
                )
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
        cursor: ResumeCursor,
    ) -> GraphRunProjection | None:
        """Attempt one node until its independent gate accepts, or the budget runs out.

        Returns ``None`` when the node SUCCEEDED and the caller should drive the rest of
        the graph, or a terminal projection when the run must stop.

        EVERY failed attempt is recorded as an additive ``node.attempt.failed`` event,
        the last one included, so counting gate rejections is one uniform query. Only the
        terminal outcome goes through ``_fail_node``, which appends ``run.failed`` and ends
        the run — a retry must leave the node in flight.
        """
        budget = _max_attempts(node)
        consumed = cursor.spent.get(node_id, 0)
        if consumed >= budget:
            # A resume found the budget already spent.  Fail closed rather than run with
            # no remaining attempts: re-granting the budget here would make total attempts
            # a function of how many times the run was resumed rather than of the budget.
            return self._fail_node(
                states, node_id, "retry budget was already spent before this resume",
                attempt=consumed, budget_exhausted=budget > 1,
                cause=NodeFailureCause.BUDGET_SPENT,
            )
        # Starts at consumed + 1, so every attempt number written is strictly greater than
        # any already recorded.  A lower number appended after a higher one would make the
        # finished run unreadable to the lifecycle validation in latest_node_states.
        max_tokens, max_cost = _spend_caps(node)
        for attempt in range(consumed + 1, budget + 1):
            # Checked before EVERY attempt, and re-derived from the log rather than kept in a
            # local accumulator, so the number enforced against cannot drift from the number a
            # later reader computes from the same run directory. One extra replay per attempt
            # is within the cost profile the log already accepts (every append replays).
            refusal = spend_refusal(
                spend=self._spend_snapshot()[node_id],
                max_tokens=max_tokens, max_cost_microunits=max_cost,
                scope=f"node {node_id!r}",
            )
            if refusal is not None:
                return self._fail_node(
                    states, node_id, refusal, attempt=max(attempt - 1, 1),
                    cause=NodeFailureCause.SPEND_EXHAUSTED,
                )
            if attempt <= cursor.started.get(node_id, 0):
                # This attempt already has a node.running receipt, so a previous run started
                # it and died before it completed. Its prefix events de-duplicate on
                # re-append, meaning nothing in the log would otherwise advance — so without
                # this record a resume loop could re-execute the worker forever against a
                # bounded attempt count.
                redrive = cursor.redrives.get((node_id, attempt), 0) + 1
                if redrive > _MAX_REDRIVES_PER_ATTEMPT:
                    return self._fail_node(
                        states, node_id,
                        f"attempt {attempt} was re-driven {redrive - 1} times without "
                        "completing; refusing to re-execute it again",
                        attempt=attempt, cause=NodeFailureCause.REDRIVE_EXHAUSTED,
                    )
                self._append_redrive(node_id, attempt, redrive)
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
            cause = outcome.cause or NodeFailureCause.WORKER_FAULT
            self._append_attempt_failed(
                node_id, attempt, reason, cause, outcome.verdict, outcome.usage,
            )
            if attempt < budget:
                continue
            return self._fail_node(
                states, node_id, reason, attempt=attempt, cause=cause,
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
                    cause=NodeFailureCause.ENVIRONMENT_DENIED,
                ))
        try:
            result = worker.execute(
                plan=self.plan, node=node, envelope=envelope, attempt=attempt,
            )
        except Exception:
            return _AttemptOutcome(
                failure="worker execution failed", cause=NodeFailureCause.WORKER_FAULT,
            )
        expected_route = self._expected_route_for(node)
        expected_transport = self._expected_transport_for(node)
        try:
            validate_worker_result(result)
            validate_observed_route(expected_route, result.observed_route)
            validate_observed_transport(expected_transport, result.observed_transport)
            self._artifact_verifier.verify(
                identity=self.event_log.identity,
                digests=result.output_artifact_digests,
            )
        except Exception:
            return _AttemptOutcome(
                failure="worker output artifact verification failed",
                cause=NodeFailureCause.ARTIFACT_UNVERIFIED,
                # An attempt whose artifacts did not verify still burned whatever the worker
                # reported. The isinstance guard is load-bearing: validate_worker_result is
                # one of the things that may have raised here, so ``result`` is not yet known
                # to be a WorkerResult and reading .usage off it blindly would raise again.
                usage=result.usage if isinstance(result, WorkerResult) else None,
            )
        # After validate_worker_result, never before: reaching into ``result.usage`` on
        # something that is not a WorkerResult would raise AttributeError and escape this
        # method uncaught, turning a misbehaving worker into a crashed run.
        max_tokens, max_cost = _spend_caps(node)
        if (max_tokens is not None or max_cost is not None) and (
            result.usage is None or not result.usage.measured_anything
        ):
            # The node asked to be bounded by spend and this worker cannot say what it spent.
            # Metering it as free would leave the cap permanently untripped — a budget that can
            # never fire, which is indistinguishable from protection until the bill arrives.
            # Terminal rather than retried: the WIRING is what is wrong, so a further attempt
            # would report exactly as little while costing exactly as much.
            return _AttemptOutcome(terminal=self._fail_node(
                states, node_id,
                f"node {node_id!r} declares a spend budget but its worker reported no usage, "
                "so spend cannot be metered; either remove the budget or bind the node to a "
                "worker that reports usage",
                attempt=attempt, cause=NodeFailureCause.BUDGET_UNMEASURABLE,
            ))
        states[node_id] = NodeState.GATING
        self._append_node(node_id, "node.gating", NodeState.GATING, attempt=attempt)
        try:
            verdict = self._gate.evaluate(plan=self.plan, node=node, result=result)
        except Exception:
            return _AttemptOutcome(terminal=self._fail_node(
                states, node_id, "independent gate evaluation failed", attempt=attempt,
                cause=NodeFailureCause.GATE_BROKEN,
            ))
        if not isinstance(verdict, GateVerdict) or not verdict_is_wellformed(verdict):
            # A broken gate, not a failed attempt: retrying would spend the budget
            # against a verdict this controller cannot interpret, and would hide the
            # defect behind an eventual budget-exhausted failure.
            return _AttemptOutcome(terminal=self._fail_node(
                states, node_id, "independent gate returned an invalid verdict", attempt=attempt,
                cause=NodeFailureCause.GATE_BROKEN,
            ))
        if verdict.passed:
            states[node_id] = NodeState.SUCCEEDED
            self._append_node(
                node_id,
                "node.succeeded",
                NodeState.SUCCEEDED,
                attempt=attempt,
                artifact_digests=list(result.output_artifact_digests),
                verdict=verdict_body(verdict),
                **({"route": route_payload(expected_route)} if expected_route else {}),
                **({"transport": expected_transport} if expected_transport else {}),
                **isolation_payload(result),
                **usage_payload(result.usage),
            )
            return _AttemptOutcome(succeeded=True)
        return _AttemptOutcome(
            failure="independent gate rejected output",
            cause=NodeFailureCause.GATE_REJECTED,
            verdict=verdict_body(verdict),
            # A rejected attempt spent real money. Carrying its usage onto the attempt record
            # is what makes retry spend visible at all: without it, the only measured attempts
            # would be the ones that passed, and a node could retry its way through any cap
            # while every recorded total looked small.
            usage=result.usage,
        )

    def _append_attempt_failed(
        self, node_id: str, attempt: int, reason: str, cause: NodeFailureCause,
        verdict: dict[str, object] | None, usage: WorkerUsage | None = None,
    ) -> None:
        """Record one failed attempt without transitioning run state.

        ``verdict`` is present exactly when the attempt failed at the gate, so its
        presence discriminates a gate rejection from a worker fault when the per-attempt
        gate error rate is computed from the log.

        Writing this record is also what marks the attempt SPENT for a later resume — see
        ``consumed_attempts_from`` — so it must be appended before the node's terminal receipt.
        """
        payload: dict[str, object] = {
            "node_id": node_id, "attempt": attempt, "reason": reason, "cause": cause.value,
        }
        if verdict is not None:
            payload["verdict"] = verdict
        payload.update(usage_payload(usage))
        self._append(f"{node_id}:node.attempt.failed:{attempt}", "node.attempt.failed", payload)

    def _append_redrive(self, node_id: str, attempt: int, redrive: int) -> None:
        """Record that an incomplete attempt is being re-executed by a resume.

        The key includes the ordinal so successive re-drives are distinct events rather than
        one de-duplicated no-op — which is precisely what makes them countable, and so
        boundable.
        """
        self._append(
            f"{node_id}:node.redrive:{attempt}:{redrive}", "node.redrive",
            {"node_id": node_id, "attempt": attempt, "redrive": redrive},
        )

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
            return self._fail_node(
                states, node_id, "approval node reached without an approval resolver",
                cause=NodeFailureCause.APPROVAL_UNRESOLVED,
            )
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
            return self._fail_node(
                states, node_id, "approval resolver evaluation failed",
                cause=NodeFailureCause.APPROVAL_UNRESOLVED,
            )
        if outcome is ApprovalOutcome.APPROVED:
            states[node_id] = NodeState.SUCCEEDED
            self._append_node(node_id, "node.succeeded", NodeState.SUCCEEDED, artifact_digests=[])
            return None
        if outcome is ApprovalOutcome.REJECTED:
            return self._fail_node(
                states, node_id, "human approval was rejected",
                cause=NodeFailureCause.APPROVAL_REJECTED,
            )
        if outcome is not ApprovalOutcome.PENDING:
            return self._fail_node(
                states, node_id, "approval resolver returned an invalid outcome",
                cause=NodeFailureCause.APPROVAL_UNRESOLVED,
            )
        # PENDING: no decision yet — stay paused; the hold receipt is already durable.
        return self.event_log.replay_projection()

    def _spend_snapshot(self) -> dict[str, NodeSpend]:
        """Per-node spend as the DURABLE receipts state it, right now.

        Re-read rather than accumulated in a field on purpose. An in-memory total is a second
        copy of a number that already exists on disk, and the two can disagree — after a
        resume they certainly would, since the process that spent the money is gone. Reading
        the one authoritative copy removes the possibility of drift rather than testing for it.
        """
        return consumed_spend_from(self.plan, self.event_log.replay())

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

    def _fail_node(
        self, states: dict[str, NodeState], node_id: str, reason: str,
        *, cause: NodeFailureCause, verdict: dict[str, object] | None = None,
        attempt: int = 1, budget_exhausted: bool = False,
    ) -> GraphRunProjection:
        states[node_id] = NodeState.FAILED
        # ``cause`` is required, not defaulted: the free-text reason is for humans, and any
        # default here would silently mislabel some failure — which is exactly how an
        # attempt that never reached the gate could end up in the gate's error denominator.
        extra: dict[str, object] = {"cause": cause.value}
        if verdict is not None:
            extra["verdict"] = verdict
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
        self._append(node_event_key(node_id, event_type, attempt), event_type, payload)

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

