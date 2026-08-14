"""Bridge between the human-approval use case and the run controller's pause.

The controller pauses an approval node at ``AWAITING_APPROVAL`` and consults an
``ApprovalResolverPort`` to decide whether a human has since approved or rejected
it. This module supplies the reference resolver: it turns a decision that has
ALREADY been validated and durably committed through ``approvals.approve`` into
the ``ApprovalOutcome`` the controller reads on resume.

Fail-closed and run-scoped by construction: an outcome is recorded only for one
run's ``(run_id, node_id, attempt, repair_round)``, and a grant requires the durable
``ApprovalCommit`` the use case returns — so a run cannot be advanced past a
human gate by a decision that was never granted, or by a decision made for a
different run of the same plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bounded_loops.graph.application.approvals import ApprovalCommit
from bounded_loops.graph.application.node_contracts import ApprovalOutcome
from bounded_loops.graph.domain.approvals import ApprovalRequest
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import PlannedNode

#: ``(run_id, node_id, attempt, repair_round)``. The round joined the key when the audit
#: showed a round-0 grant satisfying a round-1 pause (Grok 2). Round 0 keeps the same
#: coordinates it always had, so a decision recorded before the round existed still resolves.
_Key = tuple[str, str, int, int]


@dataclass
class RecordedApprovalResolver:
    """An ``ApprovalResolverPort`` backed by committed human decisions.

    Empty by default, so every approval node pauses until a decision is recorded
    for its exact run, node, and attempt.
    """

    _outcomes: dict[_Key, ApprovalOutcome] = field(default_factory=dict)

    def record_committed_approval(
        self, *, identity: GraphRunIdentity, request: ApprovalRequest, commit: ApprovalCommit,
        repair_round: int = 0,
    ) -> None:
        """Record that ``approvals.approve`` GRANTED this node's approval.

        ``approve`` returns a commit only for an ``approve`` decision (it raises on
        anything else), so the durable ``ApprovalCommit`` is itself the proof of
        grant — a caller cannot fabricate an approval the use case never made.
        """
        if not isinstance(commit, ApprovalCommit):
            raise GraphIntegrityError("an approval grant requires a durable ApprovalCommit")
        if commit.approval_id != request.approval_id:
            raise GraphIntegrityError("approval commit does not correspond to the approval request")
        if (request.organization_id, request.project_id) != (identity.organization_id, identity.project_id):
            raise GraphIntegrityError("approval request tenant does not match the run")
        self._outcomes[
            (identity.run_id, request.node_id, request.attempt, repair_round)
        ] = ApprovalOutcome.APPROVED

    def record_rejection(
        self, *, identity: GraphRunIdentity, node_id: str, attempt: int, repair_round: int = 0,
    ) -> None:
        """Record a human rejection for a paused node.

        The approvals use case only grants approvals today; a signed reject path is
        a follow-up, so a rejection is recorded directly here and the controller
        fails the run closed when it reads it.
        """
        self._outcomes[(identity.run_id, node_id, attempt, repair_round)] = ApprovalOutcome.REJECTED

    def resolve(
        self, *, identity: GraphRunIdentity, node: PlannedNode, attempt: int, repair_round: int,
    ) -> ApprovalOutcome:
        return self._outcomes.get(
            (identity.run_id, node.node_id, attempt, repair_round), ApprovalOutcome.PENDING,
        )
