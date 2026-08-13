"""The ports a deployment implements to run one node, and the values they exchange.

Separated from the controller so that implementing a worker, a gate, or an approval
resolver does not mean importing the execution controller — a third party plugging in a
provider needs the contract, not the scheduler. Contracts only: no logic beyond
dataclass field declarations lives here, so this module can never develop a reason to
import the controller back.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
from bounded_loops.graph.domain.usage import WorkerUsage
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope


@dataclass(frozen=True)
class WorkerResult:
    """Declared immutable artifacts produced by one worker attempt.

    ``isolation_provider_id`` and ``enforced_controls`` are the honest per-node
    isolation receipt — which provider ran the node and the per-dimension controls
    it actually enforced ({net, fs_write, fs_read, pid, user, kernel, egress}).
    They are optional so a gate or a legacy worker may return only digests.

    ``usage`` is what this attempt consumed. ``None`` means the worker reports no usage at
    all — which is honest, and is why a node that declares a spend budget refuses to run on
    such a worker instead of metering it as free. New fields are appended, never inserted:
    four of the five construction sites build this positionally.
    """

    output_artifact_digests: tuple[str, ...]
    observed_route: ResolvedRoute | None = None
    observed_transport: str | None = None
    isolation_provider_id: str | None = None
    enforced_controls: Mapping[str, str] | None = None
    usage: WorkerUsage | None = None


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


class NodeWorkerPort(Protocol):
    """Executes a planned node without deciding whether its output is valid.

    ``attempt`` is the 1-based attempt number of the bounded loop.  It is REQUIRED, with
    no default: a worker that silently assumed 1 would stamp attempt-3 work as attempt 1,
    which is what made per-attempt credential audiences and artifact provenance impossible
    to scope once retry existed.
    """

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
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
