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
    "node.gating": "GATING",
    "node.succeeded": "SUCCEEDED",
    "node.failed": "FAILED",
}


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
            if _unsigned_dict(stored.event) == _unsigned_dict(event):
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
    allowed = required | ({"route", "transport", "isolation"} if event_type == "node.succeeded" else set())
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
    if event_type == "node.failed" and (not isinstance(payload["reason"], str) or not payload["reason"]):
        raise GraphIntegrityError("node.failed requires a non-empty reason")


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


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


def _validate_isolation(value: object) -> None:
    """The per-node isolation receipt: a provider id and a per-dimension control
    matrix whose every value is a known control status."""
    if not isinstance(value, Mapping) or set(value) != {"provider_id", "controls"}:
        raise GraphIntegrityError("node.succeeded isolation has an invalid shape")
    provider_id = value["provider_id"]
    if not isinstance(provider_id, str) or not provider_id:
        raise GraphIntegrityError("node.succeeded isolation provider_id is invalid")
    controls = value["controls"]
    if not isinstance(controls, Mapping) or not controls:
        raise GraphIntegrityError("node.succeeded isolation controls are invalid")
    for dimension, status in controls.items():
        if not isinstance(dimension, str) or not dimension or status not in _CONTROL_STATUSES:
            raise GraphIntegrityError("node.succeeded isolation control value is invalid")


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


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    return value
