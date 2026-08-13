"""Pure builders and validators for the durable payloads a node writes.

Every function here is a total function of its arguments — no clock, no I/O, no
controller state. That is deliberate: these shapes are what a run directory is READ
back as, months later and possibly by another tool, so they have to be checkable
without standing up an execution.
"""

from __future__ import annotations

from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.usage import WorkerUsage


def node_event_key(node_id: str, event_type: str, attempt: int, repair_round: int = 0) -> str:
    """The idempotency key for one node lifecycle event.

    Attempt 1 keeps the pre-retry key format EXACTLY — ``node_id:event_type`` — so
    run directories written before retry existed still replay and resume.  Later
    attempts append the attempt number because the log raises
    ``GraphIntegrityError`` when one key is reused with a different payload
    (see ``GraphEventLog.append``), which would otherwise make a second
    ``node.running`` crash the run rather than record it.

    Do NOT "tidy" this into one uniform format: doing so silently breaks resume of
    every run directory produced before this change.
    """
    # A repair round re-runs a node from attempt 1, so without the round in the key the second
    # round's receipts collide with the first round's and ``GraphEventLog.append`` returns the
    # HISTORICAL event idempotently — making an entire round of real work invisible in the log it is
    # supposed to be recorded in. Round 0 keeps the pre-repair key EXACTLY, so every existing run
    # directory still replays and resumes.
    suffix = "" if repair_round <= 0 else f":r{repair_round}"
    if attempt <= 1:
        return f"{node_id}:{event_type}{suffix}"
    return f"{node_id}:{event_type}:{attempt}{suffix}"


def validate_worker_result(result: WorkerResult) -> None:
    if not isinstance(result, WorkerResult):
        raise GraphIntegrityError("worker must return WorkerResult")
    if not all(isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 for value in result.output_artifact_digests):
        raise GraphIntegrityError("worker result contains an invalid artifact digest")
    if result.observed_route is not None and not isinstance(result.observed_route, ResolvedRoute):
        raise GraphIntegrityError("worker result contains an invalid route identity")
    if result.observed_transport is not None and (
        not isinstance(result.observed_transport, str) or not result.observed_transport
    ):
        raise GraphIntegrityError("worker result contains an invalid transport identity")
    if result.usage is not None and not isinstance(result.usage, WorkerUsage):
        # A worker handing back a bare dict here would defeat WorkerUsage's own
        # validation, which is the only thing standing between the spend total and a
        # negative "charge" that refunds budget.
        raise GraphIntegrityError("worker result contains invalid usage")


def usage_payload(usage: WorkerUsage | None) -> dict[str, object]:
    """The ``usage`` block for a receipt, or empty when nothing was measured.

    Empty rather than a zero-filled block: a receipt that says nothing about spend and one
    that claims zero spend are different assertions, and only the first is true of a worker
    that cannot meter itself.
    """
    if usage is None:
        return {}
    body = usage.payload()
    return {"usage": body} if body else {}


def validate_observed_route(expected: ResolvedRoute | None, observed: ResolvedRoute | None) -> None:
    if expected != observed:
        raise GraphIntegrityError("worker route identity does not match immutable execution plan")


def validate_observed_transport(expected: str | None, observed: str | None) -> None:
    if expected != observed:
        raise GraphIntegrityError("worker transport does not match immutable execution plan")


def isolation_payload(result: WorkerResult) -> dict[str, object]:
    """The per-node isolation receipt for the durable ``node.succeeded`` event.

    Empty when the worker did not report one (e.g. a legacy worker), so the
    event schema stays backward compatible.
    """
    if not result.isolation_provider_id or result.enforced_controls is None:
        return {}
    return {
        "isolation": {
            "provider_id": result.isolation_provider_id,
            "controls": {str(dim): str(status) for dim, status in dict(result.enforced_controls).items()},
        }
    }


def route_payload(route: ResolvedRoute) -> dict[str, object]:
    return {
        "provider_id": route.provider_id,
        "model_id": route.model_id,
        "region": route.region,
        "fallback": route.fallback,
        "policy_digest": route.policy_digest,
    }


def verdict_is_wellformed(verdict: GateVerdict) -> bool:
    """A gate that returns an empty reason or a non-digest evidence reference is
    malformed; the controller fails the node closed here rather than let a bad
    verdict reach (and be rejected by) the durable log as an uncaught error."""
    if not isinstance(verdict.passed, bool):
        return False
    if not isinstance(verdict.reason, str) or not verdict.reason:
        return False
    digest = verdict.evidence_digest
    if digest is not None and not (
        isinstance(digest, str)
        and digest.startswith("sha256:")
        and len(digest) == 71
        and all(character in "0123456789abcdef" for character in digest[7:])
    ):
        return False
    return True


def verdict_body(verdict: GateVerdict) -> dict[str, object]:
    """The externalized independent-gate verdict for the durable receipt.

    Records the gate's boolean decision and reason (and, when the gate supplies
    one, a content-addressed evidence digest) so a node's terminal state is
    gate-attested in the log, never inferred from the producer.
    """
    body: dict[str, object] = {"passed": verdict.passed, "reason": verdict.reason}
    if verdict.evidence_digest is not None:
        body["evidence_digest"] = verdict.evidence_digest
    return body
