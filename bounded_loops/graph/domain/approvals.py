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
