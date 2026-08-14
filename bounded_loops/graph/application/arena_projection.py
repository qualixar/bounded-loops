"""Receipt-derived, read-only data for a future Graph Arena client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from bounded_loops.graph.application.graph_ports import EventLogPort
from bounded_loops.graph.application.failure_policy import RUN_SUCCEEDS_ON
from bounded_loops.graph.application.repair_rounds import (
    REPAIR_ROUND_EVENT,
    assert_boundary_is_legal,
    descendants,
)
from bounded_loops.graph.application.schedule_ready import (
    Admission,
    NodeState,
    predecessors_admission,
)
from bounded_loops.graph.application.node_spend import (
    NodeSpend,
    consumed_spend_from,
    run_spend,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, StoredGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding


_ALLOWED = {
    # PENDING has TWO exits since edge guards became enforceable. SKIPPED is reached when every
    # incoming edge was explicitly guarded and excluded — the branch was not taken. See the
    # admission check below: BOTH exits are causality decisions and both are verified.
    "PENDING": frozenset({"READY", "SKIPPED"}),
    "READY": frozenset({"STARTING", "AWAITING_APPROVAL"}),
    # STARTING -> FAILED is a real outcome, not a corruption: a node can be dispatched and then
    # refused before it ever runs — a denied execution policy, no connector worker wired, a
    # budget already spent. Omitting it made EVERY such run permanently unreadable to the Arena,
    # `bl graph status` and resume; a real `bl graph run --execute` against a policy-denied node
    # crashed with "Arena receipt node lifecycle is invalid" instead of reporting the failure.
    # Found by running the CLI for real — no unit test reached it, because the fixtures all
    # authorise every node. It opens no path to SUCCEEDED, so the independent-gate invariant is
    # untouched.
    "STARTING": frozenset({"RUNNING", "FAILED"}),
    # RUNNING -> RUNNING and GATING -> RUNNING are the two retry edges of a bounded
    # loop: the next attempt re-enters RUNNING either after the gate rejected it
    # (from GATING) or after a worker/artifact fault that never reached the gate
    # (from RUNNING).  Both are only legal when the attempt number advances — see
    # ``_attempt_is_consistent``.
    "RUNNING": frozenset({"GATING", "RUNNING", "FAILED"}),
    "AWAITING_APPROVAL": frozenset({"SUCCEEDED", "FAILED"}),
    "GATING": frozenset({"SUCCEEDED", "RUNNING", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "SKIPPED": frozenset(),
}
# A new attempt re-enters RUNNING from exactly these states.
_RETRY_FROM = frozenset({"RUNNING", "GATING"})
#: The same rule as the controller's, as receipt STATE NAMES — one source, two spellings.
_RUN_SUCCEEDS_ON_NAMES = frozenset(state.value for state in RUN_SUCCEEDS_ON)
# The node lifecycle event types, which are the ONLY ones carrying a ``state``.
# Mirrors ``event_log._NODE_EVENTS``; filtering on the ``node.`` prefix instead would
# also catch the additive ``node.attempt.failed``, which carries no state and would
# raise KeyError.  A tripwire test asserts these two sets stay identical.
_LIFECYCLE_EVENTS = frozenset({
    "node.ready", "node.starting", "node.running", "node.awaiting_approval",
    "node.gating", "node.succeeded", "node.failed", "node.skipped",
})


def _attempt_is_consistent(
    next_state: str, current_state: str, attempt: int, current_attempt: int,
) -> bool:
    """Whether an attempt number is legal for one lifecycle transition.

    Three cases: admission out of PENDING and a retry edge both ADVANCE the attempt by
    one; every other transition moves within a single attempt and must not change it.

    SKIPPED is the exception to the PENDING rule. It is the other exit from PENDING, but no
    attempt was ever made — the branch was not taken — so the count must NOT advance. Advancing it
    would assert an attempt that never ran.
    """
    if next_state == "SKIPPED":
        return attempt == current_attempt
    if current_state == "PENDING" or (next_state == "RUNNING" and current_state in _RETRY_FROM):
        return attempt == current_attempt + 1
    return attempt == current_attempt


@dataclass(frozen=True)
class ArenaReadRequest:
    subject_id: str
    organization_id: str
    project_id: str
    run_id: str


class ArenaAuthorizationPort(Protocol):
    def authorize(self, request: ArenaReadRequest) -> bool: ...


class ArenaReceiptVerifierPort(Protocol):
    """Verifies receipt trust independently of hash-chain integrity."""

    def verify(self, identity: GraphRunIdentity, receipts: tuple[StoredGraphEvent, ...]) -> None: ...


@dataclass(frozen=True)
class ArenaNodeProjection:
    node_id: str
    kind: str
    state: str
    attempt: int
    required_effects: tuple[str, ...]
    isolation: str
    hard_deadline_ms: int
    artifact_digests: tuple[str, ...]
    route: tuple[str, str, str, bool, str] | None
    transport: str | None
    #: What this node has spent across ALL its attempts, from the receipts. ``spend_complete``
    #: is False when some attempt reported nothing, which makes the totals a LOWER BOUND — a
    #: surface that showed an under-count as a measurement would be worse than showing nothing.
    spend_tokens: int = 0
    spend_cost_microunits: int = 0
    spend_complete: bool = True
    #: The INDEPENDENT gate's own words about this node, read from the receipt that recorded them.
    #: This is the evidence — every other field describes what ran, this one says why it counted as
    #: verified. It was persisted from the start (`node.succeeded` carries {"passed", "reason"}) and
    #: merely absent from the projection, so no surface could show it.
    gate_passed: bool | None = None
    gate_reason: str | None = None


@dataclass(frozen=True)
class ArenaProjection:
    organization_id: str
    project_id: str
    run_id: str
    graph_digest: str
    plan_digest: str
    policy_digest: str
    run_state: str
    receipt_sequence: int
    receipt_head_hash: str
    nodes: tuple[ArenaNodeProjection, ...]
    edges: tuple[tuple[str, str], ...]
    levels: tuple[tuple[str, ...], ...]
    #: Why the run stopped, when it stopped on the operator's total rather than on its own
    #: work. A run that is RUNNING but going nowhere is otherwise indistinguishable, in every
    #: surface, from one still making progress.
    budget_pause: dict[str, object] | None = None
    #: The whole run's spend, so a surface can show one number without summing nodes itself.
    #: ``spend_complete`` False means the totals are a LOWER BOUND: some attempt reported
    #: nothing, and presenting an under-count as a measurement is worse than showing nothing.
    spend_tokens: int = 0
    spend_cost_microunits: int = 0
    spend_complete: bool = True
    #: The INDEPENDENT gate's own words about this node, read from the receipt that recorded them.
    #: This is the evidence — every other field describes what ran, this one says why it counted as
    #: verified. It was persisted from the start (`node.succeeded` carries {"passed", "reason"}) and
    #: merely absent from the projection, so no surface could show it.
    gate_passed: bool | None = None
    gate_reason: str | None = None


def read_arena_projection(
    plan: ExecutionPlan,
    event_log: EventLogPort,
    request: ArenaReadRequest,
    authorizer: ArenaAuthorizationPort,
    receipt_verifier: ArenaReceiptVerifierPort,
) -> ArenaProjection:
    """Build display data from one verified, authorized receipt snapshot."""
    identity = event_log.identity
    _authorize(request, identity, authorizer)
    _match_plan(identity, plan)
    snapshot = event_log.verified_snapshot()
    receipt_verifier.verify(identity, snapshot.receipts)
    latest = latest_node_states(plan, snapshot.receipts)
    # SKIPPED counts, exactly as it does for the controller's own terminal verdict
    # (``failure_policy.RUN_SUCCEEDS_ON``): a conditional graph whose untaken branch was correctly
    # skipped did succeed. Requiring every node to be SUCCEEDED made the Arena contradict the
    # controller — the run sealed SUCCEEDED and then could not be read back at all.
    # Found by the P4.25a dual audit (Grok): RUN_SUCCEEDS_ON was threaded into run_graph and nowhere
    # else, so the two halves of one rule disagreed.
    if snapshot.projection.state == "SUCCEEDED" and any(
        value["state"] not in _RUN_SUCCEEDS_ON_NAMES for value in latest.values()
    ):
        raise GraphIntegrityError(
            "Arena succeeded receipt has a planned node that neither succeeded nor was skipped"
        )
    bindings = {binding.binding_id: binding for binding in plan.connection_bindings}
    spend = consumed_spend_from(plan, snapshot.receipts)
    total = run_spend(spend)
    return ArenaProjection(
        organization_id=identity.organization_id,
        project_id=identity.project_id,
        run_id=identity.run_id,
        graph_digest=identity.graph_digest,
        plan_digest=identity.plan_digest,
        policy_digest=identity.policy_digest,
        run_state=snapshot.projection.state,
        receipt_sequence=snapshot.projection.sequence,
        receipt_head_hash=snapshot.projection.head_hash,
        budget_pause=_budget_pause(snapshot.receipts),
        spend_tokens=total.tokens,
        spend_cost_microunits=total.cost_microunits,
        spend_complete=total.complete,
        nodes=tuple(
            _node_projection(node, latest[node.node_id], bindings, spend[node.node_id])
            for node in plan.nodes
        ),
        edges=tuple((edge.from_node, edge.to_node) for edge in plan.edges),
        levels=tuple(tuple(level) for level in plan.levels),
    )


def _authorize(request: ArenaReadRequest, identity: GraphRunIdentity, authorizer: ArenaAuthorizationPort) -> None:
    if not all(isinstance(value, str) and value for value in (request.subject_id, request.organization_id, request.project_id, request.run_id)):
        raise GraphIntegrityError("Arena read request is invalid")
    if (request.organization_id, request.project_id, request.run_id) != (identity.organization_id, identity.project_id, identity.run_id):
        raise GraphIntegrityError("Arena reader does not match receipt tenant")
    if not authorizer.authorize(request):
        raise GraphIntegrityError("Arena reader is unauthorized")


def _match_plan(identity: GraphRunIdentity, plan: ExecutionPlan) -> None:
    if (
        identity.graph_digest != plan.source_graph_digest
        or identity.plan_digest != plan.plan_id
        or identity.policy_digest != plan.policy_digest
    ):
        raise GraphIntegrityError("Arena receipt stream does not match immutable plan")


def latest_node_states(plan: ExecutionPlan, receipts: tuple[StoredGraphEvent, ...]) -> dict[str, dict[str, object]]:
    """Rebuild each planned node's latest receipt state, validating BOTH the per-node
    lifecycle strictly (``_ALLOWED``) AND cross-node DAG causality (a node never leaves
    PENDING before its ``plan.edges`` predecessors admit it). Shared by the Arena read
    model and the controller's resume path — so both fail closed on a tampered, fully
    re-hash-chained log that inverts node order (finding H4a)."""
    values = {node.node_id: {"state": "PENDING", "attempt": 0} for node in plan.nodes}
    nodes_by_id = {node.node_id: node for node in plan.nodes}
    predecessors = _predecessors(plan)
    rounds = 0
    for stored in receipts:
        event = stored.event
        if event.event_type == REPAIR_ROUND_EVENT:
            # A repair-round boundary is the ONE place state may move backward. Everything about it
            # is verified first, because the terminal states it resets are exactly the evidence a
            # reader relies on — an unchecked boundary would let a forged log erase any failure.
            rounds += 1
            target = str(event.payload["target_node"])
            trigger = str(event.payload["trigger_node"])
            if target not in values or trigger not in values:
                raise GraphIntegrityError("repair round names a node outside the immutable plan")
            # Numbering first: it is a property of the LOG, and a gap would let a stream hide a
            # round — which is exactly the count the global budget is measured on.
            declared_round = event.payload["round"]
            if not isinstance(declared_round, int) or declared_round != rounds:
                raise GraphIntegrityError(
                    "repair rounds must be numbered consecutively from 1"
                )
            assert_boundary_is_legal(
                plan, round_index=rounds, trigger_node=trigger, target_node=target,
                trigger_state=str(values[trigger]["state"]),
                trigger_cause=values[trigger].get("cause"),
            )
            # Suffix locality: reset the target and its descendants, nothing else. Resetting a wider
            # set would silently redo unrelated work and break the bound's first condition.
            for reset_id in descendants(plan, target):
                values[reset_id] = {"state": "PENDING", "attempt": 0}
            continue
        if event.event_type not in _LIFECYCLE_EVENTS:
            continue
        node_id = event.payload["node_id"]
        if node_id not in values:
            raise GraphIntegrityError("Arena receipt references a node outside the immutable plan")
        next_state = event.payload["state"]
        attempt = event.payload["attempt"]
        current = values[node_id]
        current_state = current["state"]
        current_attempt = current["attempt"]
        if (
            not isinstance(next_state, str)
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not isinstance(current_state, str)
            or isinstance(current_attempt, bool)
            or not isinstance(current_attempt, int)
            or next_state not in _ALLOWED[current_state]
        ):
            raise GraphIntegrityError("Arena receipt node lifecycle is invalid")
        if not _attempt_is_consistent(next_state, current_state, attempt, current_attempt):
            raise GraphIntegrityError("Arena receipt node attempt sequence is invalid")
        # PENDING has exactly TWO exits (`_ALLOWED`): READY and SKIPPED. Both are cross-node
        # causality decisions, and predecessor states are monotonic thereafter, so checking each
        # exit once here is sufficient and sound.
        #
        # Checking only READY — which was sound while READY was the sole exit — would leave SKIPPED
        # as an unguarded back door: a forged, fully re-hash-chained log could mark a node SKIPPED to
        # walk past an unsatisfied dependency, or claim SKIPPED for a node whose branch was in fact
        # taken. Each exit is therefore matched against the verdict that exit REQUIRES.
        if current_state == "PENDING" and next_state in ("READY", "SKIPPED"):
            _assert_causal_admission(
                nodes_by_id[node_id], predecessors[node_id], values,
                expected=Admission.ADMIT if next_state == "READY" else Admission.SKIP,
            )
        # AWAITING_APPROVAL is an approval-node-only human hold. `_ALLOWED` is
        # kind-agnostic, so enforce the kind here: no other node kind may reach it,
        # in an honest OR a fully re-hash-chained log — a non-approval node still
        # can only succeed through the worker+gate lifecycle.
        if next_state == "AWAITING_APPROVAL" and nodes_by_id[node_id].kind != "approval":
            raise GraphIntegrityError("Arena receipt has a non-approval node awaiting approval")
        # Symmetric guard: an approval node is a HUMAN gate — it never runs a worker,
        # so it may only leave READY via AWAITING_APPROVAL. Rejecting READY->STARTING
        # for kind "approval" means a forged worker path (…->GATING->SUCCEEDED) can
        # never grant an approval node without the human decision.
        if next_state == "STARTING" and nodes_by_id[node_id].kind == "approval":
            raise GraphIntegrityError("Arena receipt has an approval node bypassing the human gate")
        values[node_id] = dict(event.payload)
    return values


def _predecessors(plan: ExecutionPlan) -> dict[str, tuple[tuple[str, str | None], ...]]:
    """Map each planned node to its DAG predecessors as (source id, edge guard) pairs, mirroring the
    scheduler's construction in ``derive_ready_nodes``.

    The guard travels with the source because admission depends on it. Carrying only the id — as this
    did before edge guards were enforced — would let the verifier and the scheduler disagree the
    moment a graph used a conditional edge, which is exactly the divergence this shared predicate
    exists to prevent."""
    sources: dict[str, list[tuple[str, str | None]]] = {node.node_id: [] for node in plan.nodes}
    for edge in plan.edges:
        sources[edge.to_node].append((edge.from_node, edge.when))
    return {node_id: tuple(parents) for node_id, parents in sources.items()}


def _assert_causal_admission(
    node: PlannedNode,
    predecessors: tuple[tuple[str, str | None], ...],
    values: dict[str, dict[str, object]],
    *,
    expected: Admission,
) -> None:
    """Fail closed if a node left PENDING on an exit its predecessors did not authorise.

    This is the receipt-time dual of the scheduler's admission rule
    (``predecessors_admission``): the SAME predicate that decides what ``derive_ready_nodes`` and
    ``derive_skipped_nodes`` may do must hold over the predecessor states rebuilt from the receipt
    sequence so far. A tampered, fully re-hash-chained log that inverts DAG order — a
    child reaching READY (and thus SUCCEEDED) before its parents — is rejected here even
    though every per-node ``_ALLOWED`` lifecycle is individually legal. Join semantics
    are honored exactly, because the check and the scheduler share one predicate.

    ``expected`` is the verdict the taken exit requires: ADMIT for READY, SKIP for SKIPPED. Matching
    the exit against its own verdict is what stops the two exits being interchangeable — a log may
    not skip a node whose branch was taken, nor dispatch one whose branch was not."""
    parents: list[tuple[NodeState, str | None]] = []
    for source, guard in predecessors:
        state = values[source]["state"]
        if not isinstance(state, str) or state not in NodeState.__members__:
            raise GraphIntegrityError("Arena receipt node state is invalid")
        parents.append((NodeState(state), guard))
    if predecessors_admission(node.kind, node.approval_policy, tuple(parents)) is not expected:
        raise GraphIntegrityError(
            f"Arena receipt violates DAG causality: node {node.node_id!r} left PENDING "
            f"on an exit its plan predecessors did not authorise (required {expected.value})"
        )


def _node_projection(
    node: PlannedNode,
    receipt: dict[str, object],
    bindings: dict[str, ResolvedBinding],
    spend: NodeSpend,
) -> ArenaNodeProjection:
    verdict = _gate_verdict(receipt.get("verdict"))
    route = _route(receipt.get("route"))
    transport = receipt.get("transport")
    if transport is not None and (not isinstance(transport, str) or not transport):
        raise GraphIntegrityError("Arena receipt transport is invalid")
    state = receipt["state"]
    attempt = receipt["attempt"]
    if not isinstance(state, str) or isinstance(attempt, bool) or not isinstance(attempt, int):
        raise GraphIntegrityError("Arena receipt node state is invalid")
    binding = bindings.get(node.binding_id) if node.binding_id else None
    _match_binding(node, binding, route, transport, state)
    artifacts = receipt.get("artifact_digests", ())
    if not isinstance(artifacts, (tuple, list)) or not all(isinstance(value, str) for value in artifacts):
        raise GraphIntegrityError("Arena receipt artifacts are invalid")
    return ArenaNodeProjection(
        node_id=node.node_id, kind=node.kind, state=state, attempt=attempt,
        required_effects=tuple(sorted(effect.value for effect in node.required_effects)),
        isolation=node.isolation.value, hard_deadline_ms=node.hard_deadline_ms,
        artifact_digests=tuple(artifacts), route=route, transport=transport,
        spend_tokens=spend.tokens, spend_cost_microunits=spend.cost_microunits,
        spend_complete=spend.complete,
        gate_passed=verdict[0], gate_reason=verdict[1],
    )


def _gate_verdict(value: object) -> tuple[bool | None, str | None]:
    """The gate's (passed, reason) from a receipt, or (None, None) when it has not decided yet.

    A malformed verdict is treated as absent rather than raised on: this is a read-side surface,
    and refusing to render a whole run because one node's verdict is odd would hide the other
    twenty nodes that are fine. `event_payloads` already validates the shape on the WRITE side,
    which is where a bad verdict should be stopped.
    """
    if not isinstance(value, Mapping):
        return (None, None)
    passed = value.get("passed")
    reason = value.get("reason")
    return (
        passed if isinstance(passed, bool) else None,
        reason if isinstance(reason, str) and reason else None,
    )


def _budget_pause(receipts: tuple[StoredGraphEvent, ...]) -> dict[str, object] | None:
    """The most recent budget pause, or ``None`` if the run never hit the operator's total.

    Read from the receipts rather than tracked separately, for the same reason spend is: the
    log is the only thing that survives the process that wrote it. Only surfaced while the run
    is still going — a pause that was later resumed past is history, not the current state.
    """
    for stored in reversed(receipts):
        if stored.event.event_type == "run.budget.paused":
            return dict(stored.event.payload)
    return None


def _route(value: object) -> tuple[str, str, str, bool, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GraphIntegrityError("Arena receipt route is invalid")
    provider, model, region = value.get("provider_id"), value.get("model_id"), value.get("region")
    fallback, policy = value.get("fallback"), value.get("policy_digest")
    if (
        not isinstance(provider, str) or not provider
        or not isinstance(model, str) or not model
        or not isinstance(region, str) or not region
        or not isinstance(policy, str) or not policy
        or not isinstance(fallback, bool)
    ):
        raise GraphIntegrityError("Arena receipt route is invalid")
    return provider, model, region, fallback, policy


def _match_binding(
    node: PlannedNode,
    binding: ResolvedBinding | None,
    route: tuple[str, str, str, bool, str] | None,
    transport: str | None,
    state: str,
) -> None:
    if node.binding_id is None:
        if route is not None or transport is not None:
            raise GraphIntegrityError("Arena receipt has route or transport for an unbound node")
        return
    if binding is None:
        raise GraphIntegrityError("Arena node binding is absent from immutable plan")
    # Route/transport are recorded ONLY on a node's SUCCEEDED receipt (the controller binds them
    # to the admitted route there). So a SUCCEEDED bound node must match its binding exactly; a
    # bound node that did NOT succeed (FAILED / interrupted) recorded neither and must carry
    # neither — which lets a failed connector run still project and render in the Arena (where the
    # user most needs to see WHY it failed) instead of raising a receipt-integrity error.
    if state == "SUCCEEDED":
        expected = (binding.provider_id, binding.model_target, binding.region, binding.fallback, binding.route_policy_digest)
        if route != expected or transport != binding.transport:
            raise GraphIntegrityError("Arena receipt route or transport does not match immutable binding")
        return
    if route is not None or transport is not None:
        raise GraphIntegrityError("Arena receipt has route or transport for a node that did not succeed")
