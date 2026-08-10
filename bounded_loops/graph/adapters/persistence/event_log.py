"""Append-only typed graph-event stream with deterministic replay."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import (
    GraphRunIdentity,
    GraphRunProjection,
    StoredGraphEvent,
    UnsignedGraphEvent,
    VerifiedGraphEventSnapshot,
)


_GENESIS = "0" * 64
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "HALTED", "CANCELLED", "EXPIRED"})
_NODE_EVENTS = {
    "node.ready": "READY",
    "node.starting": "STARTING",
    "node.running": "RUNNING",
    "node.awaiting_approval": "AWAITING_APPROVAL",
    "node.gating": "GATING",
    "node.succeeded": "SUCCEEDED",
    "node.failed": "FAILED",
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
})


class GraphEventLog:
    """One controller-owned run stream; callers supply the expected head."""

    def __init__(self, path: Path, identity: GraphRunIdentity) -> None:
        _validate_identity(identity)
        if path.is_symlink():
            raise GraphIntegrityError("event stream path must not be a symlink")
        self._path = path
        self._identity = identity
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    @property
    def identity(self) -> GraphRunIdentity:
        """The immutable identity bound to every event in this stream."""
        return self._identity

    def append(self, expected_previous_hash: str, event: UnsignedGraphEvent) -> StoredGraphEvent:
        events = self.replay()
        head = events[-1].event_hash if events else _GENESIS
        if expected_previous_hash != head:
            raise GraphIntegrityError("expected previous hash does not match stream head")
        for stored in events:
            if stored.event.idempotency_key != event.idempotency_key:
                continue
            if _same_logical_event(stored.event, event):
                return stored
            raise GraphIntegrityError("idempotency key was reused with a different event")
        stored = StoredGraphEvent(
            identity=self._identity,
            sequence=len(events) + 1,
            event=event,
            previous_hash=head,
            event_hash="",
        )
        stored = StoredGraphEvent(
            identity=stored.identity,
            sequence=stored.sequence,
            event=stored.event,
            previous_hash=stored.previous_hash,
            event_hash=_hash(stored),
        )
        # Validate the node-event payload BEFORE persisting: the append-only log must
        # never durably hold a receipt that a later projection would reject (a
        # malformed verdict/isolation would otherwise wedge the stream — writable but
        # unprojectable). Fail closed here, before the write.
        if stored.event.event_type in _NODE_EVENTS:
            _validate_node_event(stored.event.event_type, stored.event.payload)
        # Mirror the same fail-closed gate for the additive audit event types.
        if stored.event.event_type in _AUDIT_EVENTS:
            _validate_audit_event(stored.event.event_type, stored.event.payload)
        self._append(_canonical(stored, include_hash=True))
        return stored

    def replay(self) -> tuple[StoredGraphEvent, ...]:
        if self._path.is_symlink():
            raise GraphIntegrityError("event stream path must not be a symlink")
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise GraphIntegrityError("event stream is unreadable") from exc
        previous = _GENESIS
        keys: set[str] = set()
        events: list[StoredGraphEvent] = []
        for number, line in enumerate(lines, 1):
            if not line:
                raise GraphIntegrityError(f"partial or empty event tail at sequence {number}")
            stored = _parse(line, number)
            if stored.identity != self._identity:
                raise GraphIntegrityError(f"foreign identity at sequence {number}")
            if stored.sequence != number or stored.previous_hash != previous:
                raise GraphIntegrityError(f"sequence or previous hash mismatch at sequence {number}")
            if stored.event_hash != _hash(stored):
                raise GraphIntegrityError(f"event hash mismatch at sequence {number}")
            if stored.event.idempotency_key in keys:
                raise GraphIntegrityError(f"duplicate idempotency key at sequence {number}")
            # Validate node-event payloads on READ too, so a hand-forged (but
            # correctly re-hash-chained) log cannot slip a malformed receipt past a
            # consumer that reads the raw stream rather than the projection.
            if stored.event.event_type in _NODE_EVENTS:
                _validate_node_event(stored.event.event_type, stored.event.payload)
            # Mirror the same on-read gate for additive audit events.
            if stored.event.event_type in _AUDIT_EVENTS:
                _validate_audit_event(stored.event.event_type, stored.event.payload)
            events.append(stored)
            keys.add(stored.event.idempotency_key)
            previous = stored.event_hash
        return tuple(events)

    def replay_projection(self) -> GraphRunProjection:
        return _project(self.replay())

    def verified_snapshot(self) -> VerifiedGraphEventSnapshot:
        """Read once, verify once, and retain matching receipts and projection."""
        receipts = self.replay()
        return VerifiedGraphEventSnapshot(receipts, _project(receipts))

    def _append(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _project(receipts: tuple[StoredGraphEvent, ...]) -> GraphRunProjection:
    projection = GraphRunProjection("EMPTY", 0, _GENESIS)
    for stored in receipts:
        projection = _apply(projection, stored)
    return projection


def _apply(projection: GraphRunProjection, stored: StoredGraphEvent) -> GraphRunProjection:
    event_type = stored.event.event_type
    if projection.state == "EMPTY":
        if event_type != "run.created":
            raise GraphIntegrityError("graph run stream must begin with run.created")
        state = _state(stored.event.payload, "PENDING")
        if state != "PENDING":
            raise GraphIntegrityError("run.created must declare PENDING")
    elif projection.state in _TERMINAL:
        raise GraphIntegrityError("event after terminal graph state")
    elif event_type == "run.started":
        state = _state(stored.event.payload, "RUNNING")
    elif event_type in _NODE_EVENTS:
        if projection.state != "RUNNING":
            raise GraphIntegrityError("node transition requires graph run to be RUNNING")
        _validate_node_event(event_type, stored.event.payload)
        state = "RUNNING"
    elif event_type == "run.succeeded":
        state = _state(stored.event.payload, "SUCCEEDED")
    elif event_type == "run.failed":
        state = _state(stored.event.payload, "FAILED")
    elif event_type == "run.cancelled":
        state = _state(stored.event.payload, "CANCELLED")
    elif event_type in _AUDIT_EVENTS:
        # Audit events are additive annotation events: they require a RUNNING
        # graph (audits happen during an active run) but do not change state.
        if projection.state != "RUNNING":
            raise GraphIntegrityError("audit event requires graph run to be RUNNING")
        _validate_audit_event(event_type, stored.event.payload)
        state = "RUNNING"
    else:
        raise GraphIntegrityError(f"unsupported graph event type: {event_type}")
    return GraphRunProjection(state, stored.sequence, stored.event_hash)


def _state(payload: Mapping[str, object], expected: str) -> str:
    if set(payload) != {"state"} or payload["state"] != expected:
        raise GraphIntegrityError(f"event must declare state {expected}")
    return expected


def _validate_node_event(event_type: str, payload: Mapping[str, object]) -> None:
    expected_state = _NODE_EVENTS[event_type]
    required = {"node_id", "state", "attempt"}
    if event_type == "node.succeeded":
        required.add("artifact_digests")
    elif event_type == "node.failed":
        required.add("reason")
    if event_type == "node.succeeded":
        allowed = required | {"route", "transport", "isolation", "verdict"}
    elif event_type == "node.failed":
        allowed = required | {"verdict"}
    else:
        allowed = required
    if not required <= set(payload) <= allowed:
        raise GraphIntegrityError(f"{event_type} payload has an invalid shape")
    if not isinstance(payload["node_id"], str) or not payload["node_id"]:
        raise GraphIntegrityError(f"{event_type} requires a non-empty node_id")
    if isinstance(payload["attempt"], bool) or not isinstance(payload["attempt"], int) or payload["attempt"] < 1:
        raise GraphIntegrityError(f"{event_type} requires a positive attempt")
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
    if event_type == "node.failed" and "verdict" in payload:
        _validate_verdict(payload["verdict"], False)


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


def _validate_audit_event(event_type: str, payload: Mapping[str, object]) -> None:
    """Validate the payload of an additive audit trail event.

    Each type has a CLOSED required-key set (no extra keys allowed) and
    per-field type/value rules.  Validation runs on both append and replay —
    matching the node-event pattern — so a malformed audit event is caught
    before it is durably written AND when re-reading an existing stream.
    """
    if event_type == "audit.plan.created":
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


def _validate_identity(identity: GraphRunIdentity) -> None:
    for name, value in asdict(identity).items():
        if not isinstance(value, str) or not value:
            raise GraphIntegrityError(f"identity {name} must be a non-empty string")
    for value in (identity.graph_digest, identity.plan_digest, identity.policy_digest):
        if not value.startswith("sha256:") or len(value) != 71:
            raise GraphIntegrityError("identity digest is invalid")


def _hash(stored: StoredGraphEvent) -> str:
    return hashlib.sha256(_canonical(stored, include_hash=False).encode("utf-8")).hexdigest()


def _canonical(stored: StoredGraphEvent, *, include_hash: bool) -> str:
    data = {
        "actor": stored.event.actor,
        "event_id": stored.event.event_id,
        "event_type": stored.event.event_type,
        "graph_digest": stored.identity.graph_digest,
        "idempotency_key": stored.event.idempotency_key,
        "organization_id": stored.identity.organization_id,
        "payload": _plain(stored.event.payload),
        "plan_digest": stored.identity.plan_digest,
        "policy_digest": stored.identity.policy_digest,
        "previous_hash": stored.previous_hash,
        "project_id": stored.identity.project_id,
        "run_id": stored.identity.run_id,
        "sequence": stored.sequence,
        "timestamp": stored.event.timestamp,
    }
    if include_hash:
        data["event_hash"] = stored.event_hash
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse(line: str, number: int) -> StoredGraphEvent:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise GraphIntegrityError(f"invalid event JSON at sequence {number}") from exc
    required = {
        "actor", "event_id", "event_type", "event_hash", "graph_digest", "idempotency_key",
        "organization_id", "payload", "plan_digest", "policy_digest", "previous_hash", "project_id",
        "run_id", "sequence", "timestamp",
    }
    if not isinstance(raw, dict) or set(raw) != required or not isinstance(raw["payload"], dict):
        raise GraphIntegrityError(f"invalid event shape at sequence {number}")
    identity = GraphRunIdentity(
        organization_id=raw["organization_id"], project_id=raw["project_id"], run_id=raw["run_id"],
        graph_digest=raw["graph_digest"], plan_digest=raw["plan_digest"], policy_digest=raw["policy_digest"],
    )
    event = UnsignedGraphEvent(
        event_id=raw["event_id"], idempotency_key=raw["idempotency_key"], event_type=raw["event_type"],
        timestamp=raw["timestamp"], actor=raw["actor"], payload=raw["payload"],
    )
    if not isinstance(raw["sequence"], int) or not isinstance(raw["previous_hash"], str) or not isinstance(raw["event_hash"], str):
        raise GraphIntegrityError(f"invalid event values at sequence {number}")
    return StoredGraphEvent(identity, raw["sequence"], event, raw["previous_hash"], raw["event_hash"])


def _unsigned_dict(event: UnsignedGraphEvent) -> dict[str, object]:
    return {"actor": event.actor, "event_id": event.event_id, "event_type": event.event_type, "idempotency_key": event.idempotency_key, "payload": _plain(event.payload), "timestamp": event.timestamp}


def _same_logical_event(a: UnsignedGraphEvent, b: UnsignedGraphEvent) -> bool:
    """Two events sharing an idempotency key are the SAME logical event — an
    at-least-once retry, or a resume re-appending a node's already-logged
    deterministic prefix — when everything but the timestamp matches. The timestamp
    naturally differs across a retry (a resumed run has a live clock), and is not
    part of logical identity, so a faithful re-append is DEDUPLICATED (the existing
    event is returned), never rejected as a reused key. A genuinely different
    event (different actor / id / type / payload) under the same key still raises.
    """
    left, right = _unsigned_dict(a), _unsigned_dict(b)
    left.pop("timestamp")
    right.pop("timestamp")
    return left == right


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    return value
