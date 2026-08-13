"""What the receipts say a run has already consumed, per node.

Consumption is derived from the durable log and never held only in memory. That is the
whole point: a budget kept in a process variable is reset by killing the process, so an
external loop that crash-restarts a run would be granted the full budget again on every
restart. Bounds that a `kill -9` can reset are not bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import StoredGraphEvent
from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.domain.usage import WorkerUsage, usage_from_payload

#: Receipt kinds that carry per-node consumption. Listed explicitly rather than matched by
#: prefix so a newly added ``node.*`` event cannot silently start or stop counting.
_CONSUMPTION_EVENTS = frozenset({"node.attempt.failed", "node.running", "node.redrive"})

#: The ONE receipt kind carrying measured spend: one per EXECUTION of an attempt. Per
#: execution, not per attempt, because a re-driven attempt really does spend again — an
#: at-least-once resume pays the provider a second time, and the ledger has to say so.
#: Nothing else carries usage, so no charge can be counted from two places.
_SPEND_EVENTS = frozenset({"node.spend"})

#: An attempt's outcome. Exactly one of these exists per (node, attempt) in an honest log.
_OUTCOME_EVENTS = frozenset({"node.succeeded", "node.attempt.failed"})


@dataclass(frozen=True)
class ResumeCursor:
    """What the receipts say about work already done, per node.

    ``spent`` — attempts that RECORDED their own failure; those are consumed.
    ``started`` — the highest attempt with a ``node.running`` receipt, so the controller can
    tell a fresh attempt from a re-drive of one that never completed.
    ``redrives`` — how many re-drives have already been recorded for that node.
    """

    spent: Mapping[str, int]
    started: Mapping[str, int]
    #: Keyed on (node_id, attempt), NOT on node_id alone. A per-node total would charge one
    #: attempt's re-drives against every later attempt, so a node that legitimately advanced
    #: could be refused a re-drive it had never used — starved by the history of an attempt
    #: that already completed.
    redrives: Mapping[tuple[str, int], int]

    @classmethod
    def empty(cls, node_ids: tuple[str, ...]) -> "ResumeCursor":
        zeros = {node_id: 0 for node_id in node_ids}
        return cls(spent=zeros, started=dict(zeros), redrives={})


@dataclass(frozen=True)
class NodeSpend:
    """What the receipts say one node has consumed, summed over all its attempts.

    ``complete`` is False when at least one recorded attempt reported no usage — a worker
    that crashed before it could measure itself, or a pre-0.5 receipt. The totals are then a
    LOWER BOUND, not the truth, and this flag is what stops a reader from presenting an
    under-count as a measurement.
    """

    tokens: int = 0
    cost_microunits: int = 0
    attempts_recorded: int = 0
    attempts_measured: int = 0

    @property
    def complete(self) -> bool:
        return self.attempts_recorded == self.attempts_measured

    def plus(self, tokens: int | None, cost_microunits: int | None) -> "NodeSpend":
        """Add one attempt's spend. Unmeasured contributes nothing but is still counted."""
        measured = tokens is not None or cost_microunits is not None
        return NodeSpend(
            tokens=self.tokens + (tokens or 0),
            cost_microunits=self.cost_microunits + (cost_microunits or 0),
            attempts_recorded=self.attempts_recorded + 1,
            attempts_measured=self.attempts_measured + (1 if measured else 0),
        )


@dataclass(frozen=True)
class RunBudget:
    """A ceiling on what the WHOLE run may spend, across every node.

    Operational rather than authored: it arrives from the CLI, a budget file, or the UI, not
    from the graph. A graph declares what each step needs; how much this particular execution
    of it is allowed to cost is the operator's call, and it changes run to run.

    Exhausting it PAUSES the run instead of failing it. A node's own cap failing that node is
    proportionate — one step overran its allowance. A run total is different: the operator set
    it, and the right response to reaching it is to stop and ask them, not to throw away a
    run's completed work. Nothing is bypassed to continue: the operator resumes with a higher
    ceiling, and the new ceiling is explicit rather than a grant that could be forged or
    replayed.
    """

    max_tokens: int | None = None
    max_cost_microunits: int | None = None

    def __post_init__(self) -> None:
        for field in ("max_tokens", "max_cost_microunits"):
            value = getattr(self, field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GraphIntegrityError(f"run budget {field} must be a non-negative integer")

    @property
    def declared(self) -> bool:
        return self.max_tokens is not None or self.max_cost_microunits is not None


def consumed_spend_from(
    plan: ExecutionPlan, receipts: tuple[StoredGraphEvent, ...],
) -> dict[str, NodeSpend]:
    """Per-node spend, re-derived from the durable receipts.

    Derived rather than remembered for the same reason attempts are: a total held in a
    process variable is reset by killing the process, so an external loop that crash-restarts
    a run would be handed the full budget again on every restart.

    Exactly one spend record exists per attempt (``_SPEND_EVENTS``), so this is a plain sum
    with no de-duplication — and the closed key sets in the event log are what keep it that
    way, by refusing a usage block on the terminal receipt.
    """
    spend = {node.node_id: NodeSpend() for node in plan.nodes}
    _refuse_two_outcomes_for_one_attempt(plan, receipts)
    for stored in receipts:
        if stored.event.event_type not in _SPEND_EVENTS:
            continue
        node_id = stored.event.payload.get("node_id")
        if not isinstance(node_id, str) or node_id not in spend:
            raise GraphIntegrityError("spend receipt references a node outside the plan")
        raw = stored.event.payload.get("usage")
        usage = usage_from_payload(raw) if raw is not None else None
        spend[node_id] = spend[node_id].plus(
            usage.total_tokens if usage else None,
            # The provider's own charge when it gave one, else the price-table estimate.
            usage.chargeable_cost_microunits if usage else None,
        )
    return spend


def _refuse_two_outcomes_for_one_attempt(
    plan: ExecutionPlan, receipts: tuple[StoredGraphEvent, ...],
) -> None:
    """One attempt has exactly one outcome. Two means the log is corrupt — say so.

    An honest controller cannot produce both: recording a failure marks the attempt spent, so
    the next attempt takes a new number. But a hash-valid log can carry both — the two receipts
    use different idempotency keys, and ``node.attempt.failed`` is additive so the lifecycle
    validation never sees it. When usage rode the outcome receipts, that doubled the attempt's
    charge while ``complete`` still reported the total as exact; usage now lives on node.spend
    alone, so the doubling is gone, but the contradiction itself still means the log is wrong
    about more than money.

    Raising rather than ignoring, because every reader of such a log should find out here.
    """
    node_ids = {node.node_id for node in plan.nodes}
    seen: dict[tuple[str, int], str] = {}
    for stored in receipts:
        if stored.event.event_type not in _OUTCOME_EVENTS:
            continue
        node_id = stored.event.payload.get("node_id")
        if not isinstance(node_id, str) or node_id not in node_ids:
            raise GraphIntegrityError("outcome receipt references a node outside the plan")
        attempt = stored.event.payload.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise GraphIntegrityError("outcome receipt attempt count is invalid")
        previous = seen.get((node_id, attempt))
        if previous is not None:
            raise GraphIntegrityError(
                f"node {node_id!r} attempt {attempt} carries two outcomes ({previous} and "
                f"{stored.event.event_type}); one attempt has exactly one, so nothing derived "
                "from this log can be trusted"
            )
        seen[(node_id, attempt)] = stored.event.event_type


def run_spend(spend: dict[str, NodeSpend]) -> NodeSpend:
    """The whole run's consumption, so a run-level cap is checked against one number."""
    total = NodeSpend()
    for node_spend in spend.values():
        total = NodeSpend(
            tokens=total.tokens + node_spend.tokens,
            cost_microunits=total.cost_microunits + node_spend.cost_microunits,
            attempts_recorded=total.attempts_recorded + node_spend.attempts_recorded,
            attempts_measured=total.attempts_measured + node_spend.attempts_measured,
        )
    return total


def effective_run_budget(
    declared: RunBudget, receipts: tuple[StoredGraphEvent, ...],
) -> RunBudget:
    """The declared ceiling, with any unmentioned dimension carried forward from the pause.

    An operator continuing a paused run types the number they want to change. Reading the
    dimensions they did NOT type as "unbounded" removed a ceiling they still expected to hold:
    a run paused on cost at 2000 of 1500 micro-USD, continued with only a token ceiling, ran on
    to a recorded cost of 4000. They never authorised that.

    Carrying forward can only ADD a bound, never relax one, so it cannot permit spend the
    operator did not allow — which is what makes it safe to do silently. The pause record
    already carries both ceilings, so the base is the run's own history rather than a file the
    caller may not have.
    """
    latest: Mapping[str, object] | None = None
    for stored in receipts:
        if stored.event.event_type == "run.budget.paused":
            latest = stored.event.payload
    if latest is None:
        return declared
    return RunBudget(
        max_tokens=declared.max_tokens if declared.max_tokens is not None
        else _paused_cap(latest, "max_tokens"),
        max_cost_microunits=declared.max_cost_microunits
        if declared.max_cost_microunits is not None
        else _paused_cap(latest, "max_cost_microunits"),
    )


def _paused_cap(payload: Mapping[str, object], field: str) -> int | None:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def unmeasurable_dimension(
    usage: WorkerUsage | None, *, max_tokens: int | None, max_cost_microunits: int | None,
) -> str | None:
    """Which declared cap this attempt cannot be metered against, or ``None``.

    Checked PER DIMENSION, not "did the worker report anything". A worker that reports only
    wallclock — which is every subprocess and CLI worker, since they can measure elapsed time
    and nothing else — satisfies "reported something" while leaving a token cap permanently at
    zero. The cap would never trip, no error would be raised, and the operator would read
    silence as protection. That is the precise failure this rule exists to prevent, so it has
    to name the dimension rather than take a general answer.
    """
    if max_tokens is not None and (
        usage is None or usage.total_tokens is None or usage.total_tokens == 0
    ):
        # Zero counts as unreported, not as free. A real model call cannot consume zero input
        # tokens — the prompt itself is input — so a reported 0 means the metering is not
        # trustworthy, whether the provider is broken or lying. Treating it as a measurement
        # made every token cap a no-op while the total called itself exact: a worker reporting
        # 0/0 ran all ten of its attempts under a cap of 1.
        #
        # Cost is deliberately NOT treated this way. A charge of zero is entirely credible — a
        # free tier, a local model, a zero-priced route — and refusing it would break the
        # ``max_cost_microunits: 0`` case that exists precisely to permit free work.
        return "tokens"
    if max_cost_microunits is not None and (
        usage is None or usage.chargeable_cost_microunits is None
    ):
        # Either the provider billed us or a price table priced the route. Neither means the
        # cost cap has nothing to check against, so the node refuses rather than run unpriced.
        return "cost"
    return None


def spend_refusal(
    *, spend: NodeSpend, max_tokens: int | None, max_cost_microunits: int | None, scope: str,
) -> str | None:
    """Why no FURTHER attempt may start under this cap, or ``None`` to proceed.

    The cap governs whether a new attempt may begin — never whether completed work counts.
    An attempt whose output the gate already accepted is kept even if it overshot: the money
    is spent either way, and discarding paid-for work that passed the gate is strictly worse
    than keeping it. There is no way to retroactively refuse an expense.

    So the guarantee is precisely this, and no more: total spend cannot exceed the cap by
    more than ONE attempt's worth. The runtime cannot know an attempt's cost before making
    it, so a cap of 1000 tokens does not stop a single 50_000-token attempt. Capping a
    provider call itself needs the remaining budget pushed into the request (a per-provider
    output cap), which is a separate mechanism from this accounting.

    When ``spend`` is incomplete the totals are a lower bound, so this check can under-count;
    what still bounds that case is the attempt cap, since each unmeasured attempt consumes one.
    """
    if _is_spent(spend.tokens, max_tokens):
        return _spent_reason(scope, "token", spend.tokens, max_tokens, "", spend.complete)
    if _is_spent(spend.cost_microunits, max_cost_microunits):
        return _spent_reason(
            scope, "cost", spend.cost_microunits, max_cost_microunits, " micro-USD",
            spend.complete,
        )
    return None


def _is_spent(spent: int, cap: int | None) -> bool:
    """Whether ``cap`` leaves room for another attempt.

    ``spent > 0`` is not redundant with ``spent >= cap``. A cost cap of 0 is a real and
    useful declaration — "this node must not cost money" — and a free worker satisfies it, so
    a cap of 0 with nothing spent must NOT refuse the first attempt. Without the first clause
    every zero-cost node would be refused before it ran, on the arithmetic accident that
    0 >= 0.
    """
    return cap is not None and spent > 0 and spent >= cap


def _spent_reason(
    scope: str, dimension: str, spent: int, cap: int | None, unit: str, complete: bool,
) -> str:
    qualifier = "" if complete else " (measured across only some attempts)"
    return (
        f"{scope} {dimension} budget is spent: {spent} of {cap}{unit}{qualifier} consumed, "
        "so no further attempt may start"
    )


def consumed_attempts_from(
    plan: ExecutionPlan, receipts: tuple[StoredGraphEvent, ...],
) -> ResumeCursor:
    """How many attempts each node has ALREADY spent, so a resume continues the count.

    Without this the retry loop restarts at attempt 1 on every resume, which is wrong
    twice over: it re-grants the whole budget each time a run is resumed (so total
    attempts are bounded only by the number of resumes, not by the budget), and it can
    append a LOWER attempt number after a higher one, which makes the finished run
    permanently unreadable to the lifecycle validation in ``latest_node_states``.

    An attempt is spent exactly when it RECORDED its own failure — that is, when a
    ``node.attempt.failed`` receipt exists for it. Counting those is what separates the
    two cases that matter:

    * Interrupted before recording anything (killed during the worker or the gate): no
      attempt record, so it is not spent and is RE-DRIVEN under its own number. This is
      what keeps the documented at-least-once resume contract. Its prefix events
      re-append as head-safe no-ops — ``_same_logical_event`` compares payloads with
      the timestamp excluded, so a resume under a different clock still de-duplicates —
      and no lower attempt number ever follows a higher one.
    * Recorded its failure, then the process died before the node's terminal receipt:
      the attempt IS spent. Re-driving it would let one attempt number carry both a
      rejection and a later acceptance — a contradiction that corrupts the gate
      rejection rate — and a second rejection with a different reason would collide
      with the existing attempt-record key and wedge the resume outright.

    Deriving this from the attempt records rather than from the latest lifecycle state
    is why the raw receipts are needed here: ``latest_node_states`` deliberately ignores
    additive events, so it cannot see them.

    What this bounds is the number of DISTINCT attempts, the quantity that multiplies
    the gate's false-accept probability. Re-driving of an attempt that never recorded
    anything is bounded separately, by the per-attempt re-drive cap in the controller.
    """
    node_ids = tuple(node.node_id for node in plan.nodes)
    spent = {node_id: 0 for node_id in node_ids}
    started = {node_id: 0 for node_id in node_ids}
    redrives: dict[tuple[str, int], int] = {}
    for stored in receipts:
        event_type = stored.event.event_type
        if event_type not in _CONSUMPTION_EVENTS:
            continue
        node_id = stored.event.payload["node_id"]
        if node_id not in spent:
            raise GraphIntegrityError("receipt references a node outside the plan")
        attempt = stored.event.payload["attempt"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise GraphIntegrityError("receipt attempt count is invalid")
        if event_type == "node.attempt.failed":
            spent[node_id] = max(spent[node_id], attempt)
        elif event_type == "node.running":
            started[node_id] = max(started[node_id], attempt)
        else:
            redrives[(node_id, attempt)] = redrives.get((node_id, attempt), 0) + 1
    for node_id, spent_attempt in spent.items():
        started_attempt = started[node_id]
        # An honest controller writes node.running for attempt n before any attempt
        # record for it, and never skips a number: so started is n or n+1 relative to
        # spent, never more and never less. A hash-valid but impossible log — forged, or
        # left by a partial upgrade — otherwise makes resume WRITE a receipt whose
        # attempt number the Arena's lifecycle validation rejects, turning a readable
        # (if corrupt) stream into a permanently unreadable one. Fail closed instead of
        # amplifying it.
        if started_attempt > spent_attempt + 1 or spent_attempt > started_attempt:
            raise GraphIntegrityError(
                f"node {node_id!r} receipts are inconsistent: {spent_attempt} attempt(s) "
                f"recorded a failure but the highest started attempt is {started_attempt}"
            )
    return ResumeCursor(spent=spent, started=started, redrives=redrives)


# Effects whose real-world action cannot be safely repeated by an at-least-once
# re-drive without a per-effect idempotency key (ADR-12 D7).  Aliased from
# NETWORK_EFFECTS in authoring.py — the two sets name the same effects because
# network-bearing effects are exactly those that cannot be safely retried without
# an idempotency key.  They are kept as separate names to preserve the distinct
# semantic axes (ARCH-03).
EFFECTFUL_EFFECTS = NETWORK_EFFECTS


# An attempt that never completes can be re-driven once per resume, and the prefix events
# de-duplicate, so nothing in the log advances.  This caps that: an external loop killing
# the worker before it reaches its gate can no longer buy unbounded executions against a
# bounded attempt count.  Fails closed on exhaustion; the pause-for-approval upgrade
# belongs with the run-level spend budget.
MAX_REDRIVES_PER_ATTEMPT = 3

DEFAULT_MAX_ATTEMPTS = 1
# A ceiling exists so a typo in a manifest cannot request an effectively unbounded
# loop.  It is deliberately far below the authoring schema's own 1..1000 range: the
# retry budget multiplies the gate's per-attempt false-accept probability, so a very
# large budget silently degrades the guarantee the gate is there to provide.
MAX_ATTEMPTS_CEILING = 100


def spend_caps(node: PlannedNode) -> tuple[int | None, int | None]:
    """The node's ``(max_tokens, max_cost_microunits)`` caps, validated at the point of use.

    Same reasoning as ``max_attempts``: ``PlannedNode.budgets`` is untyped, and a plan can
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


def max_attempts(node: PlannedNode) -> int:
    """The node's retry budget, validated at the point of use.

    ``PlannedNode.budgets`` is ``Mapping[str, object]``, so the value is untyped and
    must be checked rather than cast.  Validation lives here as well as in manifest
    validation because a plan can be built programmatically through the runtime
    facade without passing through the manifest validator, and an unbounded loop is
    the one failure this component must never have.
    """
    raw = node.budgets.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    # bool is a subclass of int in Python, so True would otherwise read as 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphIntegrityError("max_attempts must be an integer")
    if raw < 1 or raw > MAX_ATTEMPTS_CEILING:
        raise GraphIntegrityError(f"max_attempts must be between 1 and {MAX_ATTEMPTS_CEILING}")
    if raw > 1 and (node.required_effects & EFFECTFUL_EFFECTS):
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
