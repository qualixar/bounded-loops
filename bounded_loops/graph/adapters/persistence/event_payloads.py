"""What a graph event's PAYLOAD must look like — the shapes, not the stream.

Extracted from ``event_log`` when that module crossed the 800-line cap. The split is by concern, not
by size: ``event_log`` owns the append-only stream, its hash chain and its lock, while this module
owns the closed contract every payload must satisfy. Both halves are validated on append AND on
replay, because a hand-forged but correctly re-hash-chained log must not slip a malformed payload
past a consumer reading the raw stream.

The event-type tables live here too, so a reader finds the vocabulary next to the rules that police
it. ``event_log`` re-exports them, which keeps their import path stable.
"""

from __future__ import annotations

from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import NodeFailureCause
from bounded_loops.graph.domain.usage import usage_from_payload


_NODE_EVENTS = {
    "node.ready": "READY",
    "node.starting": "STARTING",
    "node.running": "RUNNING",
    "node.awaiting_approval": "AWAITING_APPROVAL",
    "node.gating": "GATING",
    "node.succeeded": "SUCCEEDED",
    "node.failed": "FAILED",
    # A node whose every incoming edge was explicitly guarded and EXCLUDED: its branch was not
    # taken. Terminal, but not a failure and not a success — a conditional graph that reports its
    # untaken branch as FAILED is lying, and one that leaves it PENDING never terminates.
    "node.skipped": "SKIPPED",
}
# Additive audit trail events (LLD 06 / ADR-12).  These do NOT transition the
# run state — they annotate a RUNNING graph with coverage and release evidence.
# Payload schemas are validated on both append (fail-closed before persistence)
# and replay (a hand-forged but correctly re-hash-chained log cannot slip a
# malformed audit event past a consumer reading the raw stream).
_AUDIT_EVENTS = frozenset({
    "audit.plan.created",
    "audit.result.published",
    "repair.attempt.created",
    "release.decision.issued",
    # One non-final attempt of a bounded loop.  Additive on purpose: a failed
    # attempt is NOT a node outcome, so it must not transition run state — the
    # node stays in flight and retries.  Only the terminal node.failed /
    # node.succeeded carry the outcome.  Unrelated to "repair.attempt.created"
    # above, which is audit-reconciliation lineage despite the shared word.
    "node.attempt.failed",
    # A resume happened.  Without this a resume left NO trace at all, so repeated
    # re-driving of the same attempt was not merely unbounded but unobservable.
    "run.resumed",
    # One attempt was re-driven by a resume without having completed.  The prefix
    # lifecycle events de-duplicate on re-append, so this is the only record that a
    # re-drive occurred — and the only thing that makes it countable and therefore
    # boundable.
    "node.redrive",
    # Ground truth for one node attempt, recorded after the fact by a human or an oracle.
    # Additive and strictly separate from the gate's verdict: the gate's opinion and what was
    # actually true are different facts, and conflating them would make the gate's own error
    # rate uncomputable.
    "node.outcome.labeled",
    # What ONE execution of one attempt consumed, written the instant the worker returns —
    # before artifact verification and before the gate. Money is spent inside the worker, so
    # any receipt written later can be lost to a kill -9 in between, and a lost spend record
    # reads as free work: four paid executions once measured 0 against a 50-token ceiling.
    # This is the ONLY event carrying usage; the outcome receipts do not, so one number cannot
    # exist in two places and drift.
    "node.spend",
    # The run stopped because the OPERATOR's total budget was reached. Additive on purpose:
    # the run stays RUNNING and therefore resumable, which is the whole difference between
    # pausing for a decision and failing. A failure would discard the run's completed work.
    "run.budget.paused",
})


def _state(payload: Mapping[str, object], expected: str) -> str:
    if set(payload) != {"state"} or payload["state"] != expected:
        raise GraphIntegrityError(f"event must declare state {expected}")
    return expected


def _validate_node_event(
    event_type: str, payload: Mapping[str, object], *, on_append: bool,
) -> None:
    """Validate one lifecycle receipt.

    ``on_append`` is the writer/reader distinction, and it exists for exactly one reason:
    fields added after 0.4.0 are REQUIRED of anything this version writes, but must be
    TOLERATED when absent from a receipt an older version already wrote. Requiring them on
    read would make every pre-existing run directory unreplayable and unresumable — a
    published release's runs are durable data, not something a later version may invalidate.
    """
    expected_state = _NODE_EVENTS[event_type]
    required = {"node_id", "state", "attempt"}
    if event_type == "node.succeeded":
        required.add("artifact_digests")
    elif event_type == "node.failed":
        required.add("reason")
        # A machine-readable cause is required of anything WE write: the free-text reason is
        # for humans, and telling a gate rejection from a worker crash by parsing it is how
        # an attempt that never reached the gate ends up in the gate error denominator.
        # Not required on read — 0.4.0 wrote node.failed without it, and those run
        # directories must still replay and resume.
        if on_append:
            required.add("cause")
    if event_type == "node.succeeded":
        # No "usage": spend lives on node.spend alone, written earlier and never lost.
        allowed = required | {"route", "transport", "isolation", "verdict"}
    elif event_type == "node.failed":
        # budget_exhausted appears only when a retry budget above one was spent, so a
        # reader can separate "ran out of attempts" from "failed on its only attempt".
        # No "usage": the terminal receipt describes an attempt whose own spend is already
        # recorded on its node.attempt.failed record. Allowing it in both places would let a
        # later writer double-count one attempt, and a spend total is the one number that
        # must not drift.
        allowed = required | {"verdict", "budget_exhausted", "cause"}
    elif event_type == "node.skipped":
        # ``reason`` names the guard that excluded the branch. Required of anything we write,
        # because an unexplained skip is indistinguishable from a scheduler bug when read later.
        if on_append:
            required.add("reason")
        allowed = required | {"reason"}
    else:
        allowed = required
    if not required <= set(payload) <= allowed:
        raise GraphIntegrityError(f"{event_type} payload has an invalid shape")
    if not isinstance(payload["node_id"], str) or not payload["node_id"]:
        raise GraphIntegrityError(f"{event_type} requires a non-empty node_id")
    # A SKIPPED node made no attempt, so 0 is the only honest number for it. Recording 1 would
    # assert an attempt that never ran and inflate any per-attempt count derived from the log.
    _attempt_floor = 0 if event_type == "node.skipped" else 1
    if (
        isinstance(payload["attempt"], bool)
        or not isinstance(payload["attempt"], int)
        or payload["attempt"] < _attempt_floor
    ):
        raise GraphIntegrityError(
            f"{event_type} requires an attempt of at least {_attempt_floor}"
        )
    if payload["state"] != expected_state:
        raise GraphIntegrityError(f"{event_type} must declare state {expected_state}")
    if event_type == "node.succeeded":
        artifact_digests = payload["artifact_digests"]
        if not isinstance(artifact_digests, (list, tuple)) or not all(_is_digest(value) for value in artifact_digests):
            raise GraphIntegrityError("node.succeeded requires SHA-256 artifact digests")
        if "route" in payload:
            _validate_route(payload["route"])
        if "transport" in payload and (not isinstance(payload["transport"], str) or not payload["transport"]):
            raise GraphIntegrityError("node.succeeded transport identity is invalid")
        if "isolation" in payload:
            _validate_isolation(payload["isolation"])
        if "verdict" in payload:
            _validate_verdict(payload["verdict"], True)
    if event_type == "node.failed" and (not isinstance(payload["reason"], str) or not payload["reason"]):
        raise GraphIntegrityError("node.failed requires a non-empty reason")
    if event_type == "node.failed" and "cause" in payload:
        _validate_cause(payload["cause"], "node.failed")

        # BOTH directions, as node.attempt.failed already requires: a gate rejection must
        # carry the verdict it rejected on, and no other cause may carry one. One direction
        # alone lets a worker fault ride a verdict, so a reader keying on the verdict's
        # presence counts a gate rejection where a cause-keyed reader sees none — exactly the
        # disagreement this field was added to prevent.
        if (payload["cause"] == NodeFailureCause.GATE_REJECTED.value) != ("verdict" in payload):
            raise GraphIntegrityError(
                "node.failed verdict must be present exactly for a gate rejection"
            )
    if event_type == "node.failed" and "verdict" in payload:
        _validate_verdict(payload["verdict"], False)
    if event_type == "node.failed" and "budget_exhausted" in payload:
        # The key is only ever WRITTEN as true, so a false value is not a legal receipt —
        # a single-attempt failure omits the key entirely rather than declaring it false.
        # Accepting false would leave two encodings for one fact and let a forged log pick
        # whichever a given reader mishandles.
        if payload["budget_exhausted"] is not True:
            raise GraphIntegrityError("node.failed budget_exhausted must be true when present")
        if payload["attempt"] < 2:
            # Exhausting a budget requires more than one attempt to have been available.
            raise GraphIntegrityError("node.failed budget_exhausted requires attempt above one")


_HEX_CHARS = frozenset("0123456789abcdef")


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in _HEX_CHARS for character in value[7:])
    )


def _validate_route(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {"provider_id", "model_id", "region", "fallback", "policy_digest"}:
        raise GraphIntegrityError("node.succeeded route has an invalid shape")
    if not all(isinstance(value[key], str) and value[key] for key in ("provider_id", "model_id", "region")):
        raise GraphIntegrityError("node.succeeded route identity is invalid")
    if not isinstance(value["fallback"], bool):
        raise GraphIntegrityError("node.succeeded route fallback is invalid")
    if not _is_digest(value["policy_digest"]):
        raise GraphIntegrityError("node.succeeded route policy digest is invalid")


_CONTROL_STATUSES = frozenset({"enforced", "not_enforced", "unknown"})
# The complete, closed set of dimensions every receipt must publish (mirrors the
# engine's EnforcedControls). Requiring the FULL set — no more, no fewer — stops a
# forged or buggy emitter from omitting a dimension to hide under-isolation
# (omission must never be read as "not_enforced" by a downstream reader).
_ISOLATION_DIMENSIONS = frozenset({"net", "fs_write", "fs_read", "pid", "user", "kernel", "egress"})


def _validate_isolation(value: object) -> None:
    """The per-node isolation receipt: a provider id and a COMPLETE per-dimension
    control matrix whose every value is a known control status."""
    if not isinstance(value, Mapping) or set(value) != {"provider_id", "controls"}:
        raise GraphIntegrityError("node.succeeded isolation has an invalid shape")
    provider_id = value["provider_id"]
    if not isinstance(provider_id, str) or not (1 <= len(provider_id) <= 64):
        raise GraphIntegrityError("node.succeeded isolation provider_id is invalid")
    controls = value["controls"]
    if not isinstance(controls, Mapping) or set(controls) != _ISOLATION_DIMENSIONS:
        raise GraphIntegrityError("node.succeeded isolation must publish every control dimension exactly once")
    for status in controls.values():
        if status not in _CONTROL_STATUSES:
            raise GraphIntegrityError("node.succeeded isolation control value is invalid")


def _validate_cause(value: object, event_type: str) -> None:
    """The cause must be one of the domain's closed set, so readers can switch on it."""
    if not isinstance(value, str):
        raise GraphIntegrityError(f"{event_type} cause must be a string")
    if value not in {member.value for member in NodeFailureCause}:
        raise GraphIntegrityError(f"{event_type} cause {value!r} is not a declared failure cause")


def _validate_verdict(value: object, expected_passed: bool) -> None:
    """The externalized independent-gate verdict: the gate's boolean decision and a
    non-empty reason, optionally bound to a content-addressed evidence digest. The
    decision MUST agree with the receipt it rides on — a node.succeeded may carry only
    a passed verdict and a node.failed only a failed one — so a receipt can never
    record a gate verdict that contradicts the node's terminal state."""
    if not isinstance(value, Mapping) or not ({"passed", "reason"} <= set(value) <= {"passed", "reason", "evidence_digest"}):
        raise GraphIntegrityError("node verdict has an invalid shape")
    if not isinstance(value["passed"], bool) or value["passed"] != expected_passed:
        raise GraphIntegrityError("node verdict decision does not match the receipt")
    if not isinstance(value["reason"], str) or not value["reason"]:
        raise GraphIntegrityError("node verdict requires a non-empty reason")
    if "evidence_digest" in value and not _is_digest(value["evidence_digest"]):
        raise GraphIntegrityError("node verdict evidence digest is invalid")


def _validate_usage(payload: Mapping[str, object], event_type: str) -> None:
    """Reject a usage block this runtime would never write.

    Runs on read as well as on append: a hand-forged but correctly re-hash-chained log
    could otherwise carry a negative charge, which a spend total re-derived from the
    receipts would apply as a REFUND and use to buy attempts past the cap.

    Absence is always valid — it means nothing was measured, which is what a receipt
    written before usage existed also says.
    """
    if "usage" not in payload:
        return
    try:
        usage_from_payload(payload["usage"])
    except GraphIntegrityError as exc:
        raise GraphIntegrityError(f"{event_type} carries an invalid usage block: {exc}") from exc


def _validate_audit_event(event_type: str, payload: Mapping[str, object]) -> None:
    """Validate the payload of an additive audit trail event.

    Each type has a CLOSED required-key set (no extra keys allowed) and
    per-field type/value rules.  Validation runs on both append and replay —
    matching the node-event pattern — so a malformed audit event is caught
    before it is durably written AND when re-reading an existing stream.
    """
    if event_type == "node.outcome.labeled":
        required = {"node_id", "attempt", "label", "labeller", "artifact_digest", "sequence"}
        if set(payload) != required:
            raise GraphIntegrityError("node.outcome.labeled payload has an invalid shape")
        for field in ("node_id", "labeller"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise GraphIntegrityError(f"node.outcome.labeled {field} must be a non-empty string")
        for field in ("attempt", "sequence"):
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GraphIntegrityError(f"node.outcome.labeled {field} must be a positive integer")
        if payload["label"] not in ("correct", "incorrect", "unknown"):
            raise GraphIntegrityError("node.outcome.labeled label is not a declared outcome label")
        if not _is_digest(payload["artifact_digest"]):
            # The label must name the exact content judged, or it can drift onto a different
            # output than the reviewer actually saw.
            raise GraphIntegrityError("node.outcome.labeled artifact_digest must be a SHA-256 digest")

    elif event_type == "run.resumed":
        if set(payload) != {"resume_ordinal"}:
            raise GraphIntegrityError("run.resumed payload has an invalid shape")
        ordinal = payload["resume_ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            raise GraphIntegrityError("run.resumed resume_ordinal must be a positive integer")

    elif event_type == "node.redrive":
        required = {"node_id", "attempt", "redrive"}
        if set(payload) != required:
            raise GraphIntegrityError("node.redrive payload has an invalid shape")
        if not isinstance(payload["node_id"], str) or not payload["node_id"]:
            raise GraphIntegrityError("node.redrive node_id must be a non-empty string")
        for field in ("attempt", "redrive"):
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GraphIntegrityError(f"node.redrive {field} must be a positive integer")

    elif event_type == "node.spend":
        required = {"node_id", "attempt", "execution"}
        if set(payload) - {"usage"} != required:
            raise GraphIntegrityError("node.spend payload has an invalid shape")
        if not isinstance(payload["node_id"], str) or not payload["node_id"]:
            raise GraphIntegrityError("node.spend node_id must be a non-empty string")
        for field in ("attempt", "execution"):
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GraphIntegrityError(f"node.spend {field} must be a positive integer")
        _validate_usage(payload, event_type)

    elif event_type == "run.budget.paused":
        required = {"node_id", "attempt", "reason", "tokens", "cost_microunits"}
        if set(payload) - {"max_tokens", "max_cost_microunits"} != required:
            raise GraphIntegrityError("run.budget.paused payload has an invalid shape")
        if not isinstance(payload["node_id"], str) or not payload["node_id"]:
            raise GraphIntegrityError("run.budget.paused node_id must be a non-empty string")
        if not isinstance(payload["reason"], str) or not payload["reason"]:
            raise GraphIntegrityError("run.budget.paused reason must be a non-empty string")
        attempt = payload["attempt"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise GraphIntegrityError("run.budget.paused attempt must be a positive integer")
        for field in ("tokens", "cost_microunits", "max_tokens", "max_cost_microunits"):
            if field not in payload:
                continue
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GraphIntegrityError(
                    f"run.budget.paused {field} must be a non-negative integer"
                )
        if "max_tokens" not in payload and "max_cost_microunits" not in payload:
            # A pause with no cap recorded cannot be explained to the operator it exists to
            # inform, and cannot be checked against what they actually authorised.
            raise GraphIntegrityError("run.budget.paused must record the cap it reached")

    elif event_type == "node.attempt.failed":
        # ``verdict`` is present EXACTLY when the attempt failed at the independent
        # gate, and absent when it failed in the worker or artifact verification.
        # Its presence is therefore the machine-readable discriminator between a
        # gate rejection and a worker fault — which is what makes the per-attempt
        # gate error rate computable without parsing the free-text ``reason``.
        required = {"node_id", "attempt", "reason", "cause"}
        # ``artifact_digests`` rides ONLY a gate rejection: the gate read that output and refused
        # it, so the artifact exists and a reviewer can later judge whether refusing it was right.
        # Without this the false-REJECTION rate was structurally uncomputable — ``label_node_outcome``
        # harvests digests from receipts and a rejection carried none, so no block could ever be
        # marked wrong. Found by the P4 audit.
        if set(payload) - {"verdict", "artifact_digests"} != required:
            raise GraphIntegrityError("node.attempt.failed payload has an invalid shape")
        if "artifact_digests" in payload and payload["cause"] != NodeFailureCause.GATE_REJECTED.value:
            raise GraphIntegrityError(
                "node.attempt.failed artifact_digests may accompany only a gate rejection"
            )
        if "artifact_digests" in payload:
            digests = payload["artifact_digests"]
            if not isinstance(digests, (list, tuple)) or not digests or not all(
                _is_digest(value) for value in digests
            ):
                raise GraphIntegrityError(
                    "node.attempt.failed artifact_digests must be a non-empty list of SHA-256 digests"
                )
        _validate_cause(payload["cause"], "node.attempt.failed")
        if not isinstance(payload["node_id"], str) or not payload["node_id"]:
            raise GraphIntegrityError("node.attempt.failed node_id must be a non-empty string")
        attempt = payload["attempt"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise GraphIntegrityError("node.attempt.failed attempt must be a positive integer")
        if not isinstance(payload["reason"], str) or not payload["reason"]:
            raise GraphIntegrityError("node.attempt.failed reason must be a non-empty string")
        if (payload["cause"] == NodeFailureCause.GATE_REJECTED.value) != ("verdict" in payload):
            # The two must agree, in both directions: a gate rejection is the only cause that
            # carries a verdict, and a verdict without that cause would be counted as a
            # rejection by any reader keying on its presence.
            raise GraphIntegrityError(
                "node.attempt.failed verdict must be present exactly for a gate rejection"
            )
        if "verdict" in payload:
            # The SAME closed-shape validation node.failed gets: {passed, reason} with an
            # optional evidence_digest, a non-empty reason, and passed=False to match the
            # receipt it rides on.  A weaker check here would let a hand-forged (but
            # correctly re-hash-chained) log inject extra keys or an empty reason and
            # still be counted as a gate rejection, which would corrupt the very rate
            # these records exist to measure.
            _validate_verdict(payload["verdict"], False)

    elif event_type == "audit.plan.created":
        required = {"plan_digest", "artifact_digest", "rubric_digest", "cell_count"}
        if set(payload) != required:
            raise GraphIntegrityError("audit.plan.created payload has an invalid shape")
        if not _is_digest(payload["plan_digest"]):
            raise GraphIntegrityError("audit.plan.created plan_digest must be a SHA-256 digest")
        if not _is_digest(payload["artifact_digest"]):
            raise GraphIntegrityError("audit.plan.created artifact_digest must be a SHA-256 digest")
        if not _is_digest(payload["rubric_digest"]):
            raise GraphIntegrityError("audit.plan.created rubric_digest must be a SHA-256 digest")
        cell_count = payload["cell_count"]
        if isinstance(cell_count, bool) or not isinstance(cell_count, int) or cell_count < 1:
            raise GraphIntegrityError("audit.plan.created cell_count must be a positive integer")

    elif event_type == "audit.result.published":
        required = {"result_digest", "cell", "assessor", "producer"}
        if set(payload) != required:
            raise GraphIntegrityError("audit.result.published payload has an invalid shape")
        if not _is_digest(payload["result_digest"]):
            raise GraphIntegrityError("audit.result.published result_digest must be a SHA-256 digest")
        for field in ("cell", "assessor", "producer"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise GraphIntegrityError(f"audit.result.published {field} must be a non-empty string")

    elif event_type == "repair.attempt.created":
        required = {"repair_id", "input_artifact_digest", "output_artifact_digest"}
        if set(payload) != required:
            raise GraphIntegrityError("repair.attempt.created payload has an invalid shape")
        if not isinstance(payload["repair_id"], str) or not payload["repair_id"]:
            raise GraphIntegrityError("repair.attempt.created repair_id must be a non-empty string")
        if not _is_digest(payload["input_artifact_digest"]):
            raise GraphIntegrityError("repair.attempt.created input_artifact_digest must be a SHA-256 digest")
        if not _is_digest(payload["output_artifact_digest"]):
            raise GraphIntegrityError("repair.attempt.created output_artifact_digest must be a SHA-256 digest")

    elif event_type == "release.decision.issued":
        required = {"released", "blocking_cells", "reason"}
        if set(payload) != required:
            raise GraphIntegrityError("release.decision.issued payload has an invalid shape")
        if not isinstance(payload["released"], bool):
            raise GraphIntegrityError("release.decision.issued released must be a boolean")
        if not isinstance(payload["blocking_cells"], (list, tuple)):
            raise GraphIntegrityError("release.decision.issued blocking_cells must be a list")
        if not isinstance(payload["reason"], str) or not payload["reason"]:
            raise GraphIntegrityError("release.decision.issued reason must be a non-empty string")

    else:
        raise GraphIntegrityError(f"unsupported audit event type: {event_type}")
