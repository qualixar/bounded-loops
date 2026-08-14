"""Typed, immutable graph-controller event values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


class NodeFailureCause(str, Enum):
    """Why a node attempt failed, as a machine-readable value.

    The free-text ``reason`` on a failure receipt is for humans. Before this existed the
    only way to tell a GATE REJECTION from a worker crash was to parse that text — and
    getting that distinction wrong silently corrupts the per-attempt gate error rate, since
    an attempt that never reached the gate must not appear in its denominator.

    Lives in the domain because both the controller that writes these receipts and the event
    log that validates them need the same closed set.
    """

    #: The independent gate evaluated the output and rejected it. The ONLY cause that
    #: counts toward the gate's error rate.
    GATE_REJECTED = "gate_rejected"
    #: The worker raised before producing a result; the gate never ran.
    WORKER_FAULT = "worker_fault"
    #: The worker returned, but its declared artifacts did not verify; the gate never ran.
    ARTIFACT_UNVERIFIED = "artifact_unverified"
    #: The execution policy refused to authorize the node. Deterministic; not retried.
    POLICY_DENIED = "policy_denied"
    #: The isolation enforcer could not establish the declared environment. Not retried.
    ENVIRONMENT_DENIED = "environment_denied"
    #: An egress node with no connector worker wired. Misconfiguration; not retried.
    NO_WORKER = "no_worker"
    #: The gate itself raised or returned something that is not a well-formed verdict.
    #: A broken gate, not a failed attempt — retrying would mask the defect.
    GATE_BROKEN = "gate_broken"
    #: A human decided against the node at an approval checkpoint.
    APPROVAL_REJECTED = "approval_rejected"
    #: The approval checkpoint could not be resolved (missing or malformed resolver).
    APPROVAL_UNRESOLVED = "approval_unresolved"
    #: The retry budget was already spent when a resume reached this node.
    BUDGET_SPENT = "budget_spent"
    #: One attempt was re-driven by resumes too many times without ever completing.
    REDRIVE_EXHAUSTED = "redrive_exhausted"
    #: The node's token or cost budget was spent, so no further attempt could start.
    #: Distinct from BUDGET_SPENT, which is about the ATTEMPT count: a node can exhaust
    #: its money with attempts to spare, and conflating the two would misreport which
    #: bound actually stopped the work.
    SPEND_EXHAUSTED = "spend_exhausted"
    #: A worker cannot honour its contract — e.g. a CLI whose JSON envelope this version cannot
    #: read. Deterministic, so it is NOT retried: every attempt would fail identically while
    #: paying the provider again. Distinct from WORKER_FAULT, which is transient and worth a retry.
    WORKER_CONTRACT = "worker_contract"
    #: The node declares a spend budget but its worker returned no usage, so the spend
    #: could not be metered. Refused rather than metered as free: a budget checked against
    #: an unmeasurable quantity never trips, which looks exactly like protection and is not.
    #: Deterministic for a given wiring, so it is not retried.
    BUDGET_UNMEASURABLE = "budget_unmeasurable"


@dataclass(frozen=True)
class GraphRunIdentity:
    organization_id: str
    project_id: str
    run_id: str
    graph_digest: str
    plan_digest: str
    policy_digest: str


@dataclass(frozen=True)
class UnsignedGraphEvent:
    event_id: str
    idempotency_key: str
    event_type: str
    timestamp: str
    actor: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class StoredGraphEvent:
    identity: GraphRunIdentity
    sequence: int
    event: UnsignedGraphEvent
    previous_hash: str
    event_hash: str


@dataclass(frozen=True)
class VerifiedGraphEventSnapshot:
    """One coherent verified read of a graph receipt stream."""

    receipts: tuple[StoredGraphEvent, ...]
    projection: GraphRunProjection


@dataclass(frozen=True)
class GraphRunProjection:
    state: str
    sequence: int
    head_hash: str
