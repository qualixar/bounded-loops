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
from bounded_loops.graph.domain.plan import ExecutionPlan
from bounded_loops.graph.domain.usage import WorkerUsage, usage_from_payload

#: Receipt kinds that carry per-node consumption. Listed explicitly rather than matched by
#: prefix so a newly added ``node.*`` event cannot silently start or stop counting.
_CONSUMPTION_EVENTS = frozenset({"node.attempt.failed", "node.running", "node.redrive"})

#: Receipt kinds that carry one attempt's measured spend — exactly one record per attempt.
#: ``node.failed`` is deliberately NOT here: it is the terminal receipt for an attempt whose
#: own spend already rode its ``node.attempt.failed`` record, so counting it too would double
#: every failing attempt's charge.
_SPEND_EVENTS = frozenset({"node.attempt.failed", "node.succeeded"})


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
            usage.cost_microunits if usage else None,
        )
    return spend


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
    if max_tokens is not None and (usage is None or usage.total_tokens is None):
        return "tokens"
    if max_cost_microunits is not None and (usage is None or usage.cost_microunits is None):
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
