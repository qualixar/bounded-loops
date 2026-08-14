"""Immutable approval request and decision values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bounded_loops.graph.domain.authoring import Effect


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    organization_id: str
    project_id: str
    graph_digest: str
    plan_digest: str
    node_id: str
    attempt: int
    evidence_digest: str
    requested_effects: frozenset[Effect]
    required_role: str
    nonce: str
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_effects", frozenset(self.requested_effects))


@dataclass(frozen=True)
class ApprovalDecision:
    request_digest: str
    actor_id: str
    actor_role: str
    decision: Literal["approve", "reject"]
    auth_context_digest: str
    decided_at: str
    signature: str
    #: WHO decided, as distinct from `actor_id`, which is the authorization SUBJECT.
    #:
    #: They are different questions and used to share one answer. On a local run the authorizer
    #: requires `subject_id == organization_id`, so `actor_id` could only ever be the tenant —
    #: every locally approved irreversible effect produced a receipt saying `local-org` approved
    #: it. `decided_by` carries a person's name; `actor_id` keeps carrying the subject the
    #: authorizer checked, because collapsing them again is how one of the two ends up wrong.
    #:
    #: NOT authenticated on a local run. `decided_by_source` says where the name came from, so a
    #: reader can weigh it; see `bounded_loops.local_identity`. Defaults keep every existing
    #: caller valid and record the absence honestly rather than inventing a name.
    decided_by: str = "unknown"
    decided_by_source: str = "unknown"
