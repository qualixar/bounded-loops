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


def validated_verdict_or_none(verdict: object) -> GateVerdict | None:
    """Validate a gate's verdict and return a GateVerdict THIS MODULE built, or None to fail closed.

    Reads every field EXACTLY ONCE into a local, validates the locals, and returns a fresh
    ``GateVerdict`` constructed from them. The caller must use the returned object and discard the
    gate's — that is the entire point, and returning the gate's own object was the defect.

    ``GateVerdict`` is a frozen dataclass, so its fields cannot be REASSIGNED. That is not enough:
    ``isinstance(verdict, GateVerdict)`` accepts a SUBCLASS, and a subclass can define ``passed``,
    ``reason`` or ``evidence_digest`` as properties that answer differently on each call. Validating
    one read and then acting on another read was reproducible two ways:

      * ``passed`` returning False for the wellformedness read and True afterwards — a node marked
        SUCCEEDED on a verdict that validated as a failure.
      * ``evidence_digest`` returning a well-formed sha256 for the format check and a forged string
        for the receipt — which defeats the ONLY thing that field exists for, since its whole job is
        to make the externalized verdict tamper-evident.

    The loop-gate boundary had the identical defect and closed it the same way; see
    ``GuardedGate._validate``. Wrapping graph gates in ``GuardedGate`` itself would be wrong — that
    class validates a different type (``domain.models.Verdict``: passed/detail/evidence) and is
    strictly WEAKER here, because it does not require a reason on a FAILING verdict and has no
    concept of an evidence digest. The lesson transfers; the code does not.
    """
    if not isinstance(verdict, GateVerdict):
        return None

    # ONE read each. Everything below validates and returns these locals, never `verdict`.
    passed = verdict.passed
    reason = verdict.reason
    digest = verdict.evidence_digest

    # Reading once is necessary and was NOT sufficient. A `str` subclass still owns every method
    # the checks below would call: overriding `startswith`, `__len__` and `__getitem__` satisfied
    # the digest format test while the object's real bytes were "not a digest at all", and that
    # string reached the receipt — defeating the only thing the field exists for. One read closes
    # the check/use split; it does not stop a lying method.
    #
    # `str.__str__` and `str.strip` as UNBOUND builtins cannot be intercepted by a subclass and
    # return a genuine `str`. Normalise FIRST, then validate the normalised value, and store that.
    # Same remedy as `GuardedGate._validate`; it belongs here too and did not arrive with it.
    if not isinstance(passed, bool):
        return None
    if not isinstance(reason, str):
        return None
    try:
        reason = str.__str__(reason)
    except TypeError:
        return None  # an object whose __class__ merely CLAIMS str is not one
    if not reason:
        return None
    if digest is not None:
        if not isinstance(digest, str):
            return None
        try:
            digest = str.__str__(digest)
        except TypeError:
            return None
        if not (
            digest.startswith("sha256:")
            and len(digest) == 71
            and all(character in "0123456789abcdef" for character in digest[7:])
        ):
            return None
    return GateVerdict(passed=passed, reason=reason, evidence_digest=digest)


def verdict_is_wellformed(verdict: GateVerdict) -> bool:
    """A gate that returns an empty reason or a non-digest evidence reference is
    malformed; the controller fails the node closed here rather than let a bad
    verdict reach (and be rejected by) the durable log as an uncaught error.

    Delegates so there is exactly ONE validation implementation. A second copy would drift, and the
    copy the controller does not call is the one that would keep looking correct.
    """
    return validated_verdict_or_none(verdict) is not None


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
