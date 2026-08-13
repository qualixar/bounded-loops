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

#: Receipt kinds that carry per-node consumption. Listed explicitly rather than matched by
#: prefix so a newly added ``node.*`` event cannot silently start or stop counting.
_CONSUMPTION_EVENTS = frozenset({"node.attempt.failed", "node.running", "node.redrive"})


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
