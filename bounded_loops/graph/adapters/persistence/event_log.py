"""Append-only typed graph-event stream with deterministic replay."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.adapters.persistence.event_payloads import (
    _AUDIT_EVENTS,
    _NODE_EVENTS,
    _state,
    _validate_audit_event,
    _validate_node_event,
)
from bounded_loops.graph.domain.events import (
    GraphRunIdentity,
    GraphRunProjection,
    StoredGraphEvent,
    UnsignedGraphEvent,
    VerifiedGraphEventSnapshot,
)


_GENESIS = "0" * 64
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "HALTED", "CANCELLED", "EXPIRED"})

# Cross-process advisory locking for the append-only stream. `append()` is a
# read-check-write cycle (replay to find the head, verify the caller's expected head,
# then write); without serialization two concurrent writers both observe the SAME head
# and both write an event claiming it, which FORKS the hash chain and leaves the run
# permanently unreplayable — `replay()` then fails with a sequence/previous-hash
# mismatch and the run cannot be resumed. That is reachable in normal use: a CLI
# `bl graph approve`, a `bl graph console` decision, and a programmatic/MCP `resume`
# are three independent entry points into the same run directory.
#
# This mirrors `bounded_loops/adapters/io/hash_chain_events.py`, which already wraps the
# identical cycle in the same primitives — the graph stream simply had not adopted it.
_fcntl: Any | None = None
_msvcrt: Any | None = None
try:  # POSIX: shared/exclusive advisory locks.
    _fcntl = importlib.import_module("fcntl")
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows.
    pass
try:  # Windows: exclusive-only fallback.
    _msvcrt = importlib.import_module("msvcrt")
except ModuleNotFoundError:  # pragma: no cover - exercised on POSIX.
    pass


def _acquire_stream_lock(handle: BinaryIO, *, exclusive: bool) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH)
        return
    if _msvcrt is not None:
        # Windows offers only an exclusive byte-range lock here; correctness of the
        # chain outranks reader concurrency on that platform.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        return
    raise GraphIntegrityError("no supported file-lock implementation is available")


def _release_stream_lock(handle: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - exercised on Windows.
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


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

    def _lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        """Serialize this run's stream across processes via a sidecar lock file.

        A sidecar (rather than locking the stream itself) keeps the lock independent of
        the append handle's lifetime and matches ``hash_chain_events.py``. Never nest:
        the exclusive holder must call ``_replay_unlocked``, not ``replay``, because
        ``flock`` is held per open-file-description and a second acquisition from the
        same process would block on itself.
        """
        lock_path = self._lock_path()
        if lock_path.is_symlink():
            raise GraphIntegrityError("event lock path must not be a symlink")
        try:
            with lock_path.open("a+b") as handle:
                _acquire_stream_lock(handle, exclusive=exclusive)
                try:
                    yield
                finally:
                    _release_stream_lock(handle)
        except OSError as exc:
            raise GraphIntegrityError(f"cannot lock event stream: {exc}") from exc

    def append(self, expected_previous_hash: str, event: UnsignedGraphEvent) -> StoredGraphEvent:
        """Append under an EXCLUSIVE stream lock.

        The head check and the write must be one atomic step: see the module-level note
        on why an unserialized read-check-write forks the hash chain.
        """
        with self._locked(exclusive=True):
            return self._append_checked(expected_previous_hash, event)

    def _append_checked(
        self, expected_previous_hash: str, event: UnsignedGraphEvent
    ) -> StoredGraphEvent:
        # CON-05 note: _replay_unlocked() reads and hash-verifies the ENTIRE
        # file on every append — O(n) per append, O(n²) total for a run with n
        # events.  This is deliberate: we do NOT cache the head hash in memory
        # and check only the last line, because the full chain re-read is the
        # integrity guarantee.  A cached-head approach would skip verifying the
        # middle of the chain on each append, allowing a hand-crafted partial
        # corruption (e.g. a forged event inserted at sequence k where k < n)
        # to go undetected until an explicit replay() call.  The append path's
        # full re-read keeps the integrity check continuous.
        #
        # For typical graph runs (tens to low hundreds of events) the O(n²)
        # cost is acceptable.  If a future use-case accumulates thousands of
        # events in a single run, the right fix is to keep an in-memory cache
        # that tracks (head_hash, sequence, idempotency_key_set) AND to add a
        # periodic full-replay cross-check against the on-disk state — not to
        # drop the per-append verification silently.
        events = self._replay_unlocked()
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
            _validate_node_event(
                stored.event.event_type, stored.event.payload, on_append=True,
            )
        # Mirror the same fail-closed gate for the additive audit event types.
        if stored.event.event_type in _AUDIT_EVENTS:
            _validate_audit_event(stored.event.event_type, stored.event.payload)
        self._append(_canonical(stored, include_hash=True))
        return stored

    def replay(self) -> tuple[StoredGraphEvent, ...]:
        """Read and verify the whole stream under a SHARED lock.

        The shared lock keeps a reader from observing the stream mid-append (a writer
        holds it exclusively), so a concurrent decision can no longer surface as a
        spurious "partial or empty event tail".
        """
        with self._locked(exclusive=False):
            return self._replay_unlocked()

    def _replay_unlocked(self) -> tuple[StoredGraphEvent, ...]:
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
                _validate_node_event(
                    stored.event.event_type, stored.event.payload, on_append=False,
                )
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
    elif event_type == "node.outcome.labeled":
        # Ordered BEFORE the terminal guard, and deliberately: ground truth arrives after the
        # run, and the run being finished is the NORMAL time to label it. Every other event
        # type describes something the run did, so one arriving after a terminal state is
        # corruption; a label describes what a reviewer later concluded ABOUT the run, which
        # is only knowable once it has stopped.
        #
        # It carries the state forward unchanged, so labelling can never move a run's
        # outcome — a reviewer records the truth, the run records what it decided, and
        # neither overwrites the other.
        _validate_audit_event(event_type, stored.event.payload)
        state = projection.state
    elif projection.state in _TERMINAL:
        raise GraphIntegrityError("event after terminal graph state")
    elif event_type == "run.started":
        state = _state(stored.event.payload, "RUNNING")
    elif event_type in _NODE_EVENTS:
        if projection.state != "RUNNING":
            raise GraphIntegrityError("node transition requires graph run to be RUNNING")
        _validate_node_event(event_type, stored.event.payload, on_append=False)
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
