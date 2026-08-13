"""Append-only, hash-linked controller events for a single bounded run."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import importlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping
import tempfile
import uuid

from bounded_loops.domain.errors import EvidenceError
from bounded_loops.domain.models import Status

_fcntl: Any | None = None
_msvcrt: Any | None = None
try:  # POSIX: shared/exclusive advisory locks.
    _fcntl = importlib.import_module("fcntl")
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows.
    pass
try:  # Windows: exclusive controller lock fallback.
    _msvcrt = importlib.import_module("msvcrt")
except ModuleNotFoundError:  # pragma: no cover - exercised on POSIX.
    pass


_GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class StoredEvent:
    run_id: str
    sequence: int
    event_id: str
    idempotency_key: str
    event_type: str
    payload: Mapping[str, object]
    previous_hash: str
    event_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "payload",
            MappingProxyType({key: _freeze_json_value(value) for key, value in self.payload.items()}),
        )


@dataclass(frozen=True)
class EventCheckpoint:
    run_id: str
    sequence: int
    head_hash: str
    projection_digest: str


class LoopAttemptState(str, Enum):
    """Closed recovery state for one graph-owned legacy-loop attempt."""

    EMPTY = "EMPTY"
    WIRED = "WIRED"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class LoopAttemptProjection:
    """State derived only from a verified, closed bridge-event stream."""

    run_id: str
    node_id: str | None
    attempt: int | None
    state: LoopAttemptState
    status: str | None
    reason: str | None
    sequence: int
    #: Which bounded repair round this attempt belongs to; 0 for the original pass. Defaulted so
    #: every existing construction site keeps working, and so a stream written before repair rounds
    #: existed projects as round 0 rather than as an unknown.
    repair_round: int = 0


class HashChainEventStore:
    """Owns a single run event stream and verifies it before every append."""

    def __init__(self, path: Path, *, run_id: str) -> None:
        if not run_id:
            raise EvidenceError("run_id must not be empty")
        if path.is_symlink():
            raise EvidenceError("event stream path must not be a symlink")
        self._path = path
        self._run_id = run_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def append(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> StoredEvent:
        if not event_type or not idempotency_key:
            raise EvidenceError("event_type and idempotency_key must not be empty")
        with self._locked(exclusive=True):
            events = self._replay_unlocked()
            for event in events:
                if event.idempotency_key == idempotency_key:
                    if event.event_type == event_type and _json_value(event.payload) == _json_value(payload):
                        return event
                    raise EvidenceError("idempotency key was reused with a different event")
            previous_hash = events[-1].event_hash if events else _GENESIS_HASH
            event = StoredEvent(
                run_id=self._run_id,
                sequence=len(events) + 1,
                event_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                event_type=event_type,
                payload=payload,
                previous_hash=previous_hash,
                event_hash="",
            )
            stored = _with_hash(event)
            self._append_line(_canonical_event(stored, include_hash=True))
            return stored

    def replay(self) -> tuple[StoredEvent, ...]:
        with self._locked(exclusive=False):
            return self._replay_unlocked()

    def _replay_unlocked(self) -> tuple[StoredEvent, ...]:
        if self._path.is_symlink():
            raise EvidenceError("event stream path must not be a symlink")
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise EvidenceError(f"cannot read event stream: {exc}") from exc
        events: list[StoredEvent] = []
        previous_hash = _GENESIS_HASH
        keys: set[str] = set()
        for line_number, line in enumerate(lines, 1):
            if not line:
                raise EvidenceError(f"empty event line at sequence {line_number}")
            event = _parse_event(line, line_number)
            if event.run_id != self._run_id:
                raise EvidenceError(f"foreign run id at sequence {line_number}")
            if event.sequence != line_number:
                raise EvidenceError(f"sequence mismatch at sequence {line_number}")
            if event.previous_hash != previous_hash:
                raise EvidenceError(f"previous hash mismatch at sequence {line_number}")
            if event.event_hash != _event_hash(event):
                raise EvidenceError(f"event hash mismatch at sequence {line_number}")
            if event.idempotency_key in keys:
                raise EvidenceError(f"duplicate idempotency key at sequence {line_number}")
            keys.add(event.idempotency_key)
            events.append(event)
            previous_hash = event.event_hash
        return tuple(events)

    def recover_loop_attempt(self) -> LoopAttemptProjection:
        """Derive one bridge attempt state from a fully verified event stream.

        This reducer deliberately accepts only the F0.3 bridge event contract:
        one ``loop.attempt.wired`` event followed by, at most, one terminal
        event for the same node and attempt. Unknown or out-of-order events are
        evidence failures, not forward-compatible best effort.
        """
        events = self.replay()
        if not events:
            return LoopAttemptProjection(
                run_id=self._run_id,
                node_id=None,
                attempt=None,
                state=LoopAttemptState.EMPTY,
                status=None,
                reason=None,
                sequence=0,
            )

        first = events[0]
        if first.event_type != "loop.attempt.wired":
            raise EvidenceError("graph stream must begin with loop.attempt.wired")
        node_id, attempt, repair_round = _bridge_identity(first)
        state = LoopAttemptState.WIRED
        status: str | None = None
        reason: str | None = None

        for event in events[1:]:
            if event.event_type != "loop.attempt.terminal":
                raise EvidenceError(f"unexpected graph event: {event.event_type}")
            event_node_id, event_attempt, event_round = _bridge_identity(event)
            if event_node_id != node_id:
                raise EvidenceError("graph event has different node_id")
            if event_attempt != attempt:
                raise EvidenceError("graph event has different attempt")
            if event_round != repair_round:
                # Same reasoning as the two checks above: one inner run owns exactly one
                # (node, attempt, round), so a terminal event claiming a different round means two
                # attempts have been written into one stream and the projection would be a blend.
                raise EvidenceError("graph event has different repair_round")
            if state is LoopAttemptState.TERMINAL:
                raise EvidenceError("graph stream contains an event after terminal state")
            status, reason = _terminal_values(event)
            state = LoopAttemptState.TERMINAL

        return LoopAttemptProjection(
            run_id=self._run_id,
            node_id=node_id,
            attempt=attempt,
            state=state,
            status=status,
            reason=reason,
            sequence=len(events),
            repair_round=repair_round,
        )

    def checkpoint(self, projection: Mapping[str, object]) -> EventCheckpoint:
        """Write a durable checkpoint bound to the current verified event head."""
        with self._locked(exclusive=True):
            events = self._replay_unlocked()
            checkpoint = EventCheckpoint(
                run_id=self._run_id,
                sequence=len(events),
                head_hash=events[-1].event_hash if events else _GENESIS_HASH,
                projection_digest=_projection_digest(projection),
            )
            _write_json_atomically(
                self._checkpoint_path(),
                {
                    "head_hash": checkpoint.head_hash,
                    "projection_digest": checkpoint.projection_digest,
                    "run_id": checkpoint.run_id,
                    "sequence": checkpoint.sequence,
                },
            )
            return checkpoint

    def verify_checkpoint(self, projection: Mapping[str, object]) -> EventCheckpoint:
        """Verify checkpoint identity, chain head, and derived projection bytes."""
        with self._locked(exclusive=False):
            path = self._checkpoint_path()
            if path.is_symlink():
                raise EvidenceError("checkpoint path must not be a symlink")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EvidenceError("checkpoint is unreadable") from exc
            if not isinstance(raw, dict) or set(raw) != {
                "head_hash", "projection_digest", "run_id", "sequence",
            }:
                raise EvidenceError("checkpoint has invalid shape")
            try:
                checkpoint = EventCheckpoint(**raw)
            except TypeError as exc:
                raise EvidenceError("checkpoint has invalid values") from exc
            events = self._replay_unlocked()
            head_hash = events[-1].event_hash if events else _GENESIS_HASH
            if checkpoint.run_id != self._run_id or checkpoint.sequence != len(events):
                raise EvidenceError("checkpoint does not match verified event sequence")
            if checkpoint.head_hash != head_hash:
                raise EvidenceError("checkpoint does not match verified event head")
            if checkpoint.projection_digest != _projection_digest(projection):
                raise EvidenceError("checkpoint projection digest mismatch")
            return checkpoint

    def _append_line(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _checkpoint_path(self) -> Path:
        return self._path.with_name("checkpoint.json")

    def _lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    @contextmanager
    def _locked(self, *, exclusive: bool):
        """Serialize readers and writers across controller processes.

        F0.3 supports local POSIX controller storage only. A public remote or
        multi-host evidence store belongs to the later connector/runtime phases.
        """
        lock_path = self._lock_path()
        if lock_path.is_symlink():
            raise EvidenceError("event lock path must not be a symlink")
        try:
            with lock_path.open("a+b") as fh:
                _acquire_file_lock(fh, exclusive=exclusive)
                try:
                    yield
                finally:
                    _release_file_lock(fh)
        except OSError as exc:
            raise EvidenceError(f"cannot lock event stream: {exc}") from exc


def _with_hash(event: StoredEvent) -> StoredEvent:
    return StoredEvent(
        run_id=event.run_id,
        sequence=event.sequence,
        event_id=event.event_id,
        idempotency_key=event.idempotency_key,
        event_type=event.event_type,
        payload=event.payload,
        previous_hash=event.previous_hash,
        event_hash=_event_hash(event),
    )


def _event_hash(event: StoredEvent) -> str:
    return hashlib.sha256(_canonical_event(event, include_hash=False).encode("utf-8")).hexdigest()


def _canonical_event(event: StoredEvent, *, include_hash: bool) -> str:
    data: dict[str, object] = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "idempotency_key": event.idempotency_key,
        "payload": _json_value(event.payload),
        "previous_hash": event.previous_hash,
        "run_id": event.run_id,
        "sequence": event.sequence,
    }
    if include_hash:
        data["event_hash"] = event.event_hash
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("event payload is not canonical JSON") from exc


def _parse_event(line: str, line_number: int) -> StoredEvent:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid JSON at sequence {line_number}") from exc
    if not isinstance(raw, dict):
        raise EvidenceError(f"event must be an object at sequence {line_number}")
    required = {
        "event_id", "event_type", "idempotency_key", "payload", "previous_hash",
        "run_id", "sequence", "event_hash",
    }
    if set(raw) != required or not isinstance(raw["payload"], dict):
        raise EvidenceError(f"invalid event shape at sequence {line_number}")
    try:
        return StoredEvent(**raw)
    except TypeError as exc:
        raise EvidenceError(f"invalid event value at sequence {line_number}") from exc


def _bridge_identity(event: StoredEvent) -> tuple[str, int, int]:
    """Read ``(node_id, attempt, repair_round)`` from a bridge event, or refuse the payload.

    The schema stays CLOSED. ``repair_round`` is admitted as the single OPTIONAL field rather than
    by relaxing the check to a subset test, because the exact-set equality here is what stops an
    unrecognised payload key from riding along unnoticed.

    Absent means round 0. The bridge omits the field for the original pass so that an event written
    before repair rounds existed keeps its exact payload and its exact idempotency key — the same
    omit-when-unset discipline that keeps ``plan_id`` stable for already-persisted graph runs.
    """
    payload = event.payload
    expected = {"attempt", "node_id"}
    if event.event_type == "loop.attempt.terminal":
        expected = {"attempt", "node_id", "reason", "status"}
    if set(payload) - {"repair_round"} != expected:
        raise EvidenceError(f"invalid payload shape for {event.event_type}")
    node_id = payload["node_id"]
    attempt = payload["attempt"]
    if not isinstance(node_id, str) or not node_id:
        raise EvidenceError("graph event node_id must be a non-empty string")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise EvidenceError("graph event attempt must be a positive integer")
    repair_round = payload.get("repair_round", 0)
    if not isinstance(repair_round, int) or isinstance(repair_round, bool) or repair_round < 0:
        raise EvidenceError("graph event repair_round must be a non-negative integer")
    if repair_round == 0 and "repair_round" in payload:
        # An explicit zero is the one way a writer can produce two different payloads meaning the
        # same thing, which would give the same attempt two different projection digests.
        raise EvidenceError("graph event repair_round 0 must be omitted, not written explicitly")
    return node_id, attempt, repair_round


def _terminal_values(event: StoredEvent) -> tuple[str, str]:
    status = event.payload["status"]
    reason = event.payload["reason"]
    if not isinstance(status, str):
        raise EvidenceError("terminal status must be a string")
    try:
        Status(status)
    except ValueError as exc:
        raise EvidenceError("terminal status is not a bounded-loop status") from exc
    if not isinstance(reason, str):
        raise EvidenceError("terminal reason must be a string")
    return status, reason


def _projection_digest(projection: Mapping[str, object]) -> str:
    try:
        canonical = json.dumps(
            _json_value(projection), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceError("projection is not canonical JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze_json_value(value: object) -> object:
    """Detach nested JSON-shaped input before it becomes immutable evidence."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_value(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(child) for child in value)
    return value


def _json_value(value: object) -> object:
    """Convert frozen evidence back to standard JSON containers for hashing."""
    if isinstance(value, Mapping):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_value(child) for child in value]
    return value


def _acquire_file_lock(fh: BinaryIO, *, exclusive: bool) -> None:
    if _fcntl is not None:
        mode = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
        _fcntl.flock(fh.fileno(), mode)
        return
    if _msvcrt is not None:
        # Windows exposes only an exclusive byte-range primitive here. F0.3's
        # correctness takes precedence over read concurrency on that platform.
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        _msvcrt.locking(fh.fileno(), _msvcrt.LK_LOCK, 1)
        return
    raise EvidenceError("no supported local file-lock implementation is available")


def _release_file_lock(fh: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        fh.seek(0)
        _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)


def _write_json_atomically(path: Path, value: Mapping[str, object]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
