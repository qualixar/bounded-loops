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
from bounded_loops.graph.domain.events import (
    NodeFailureCause,
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
        allowed = required | {"route", "transport", "isolation", "verdict"}
    elif event_type == "node.failed":
        # budget_exhausted appears only when a retry budget above one was spent, so a
        # reader can separate "ran out of attempts" from "failed on its only attempt".
        allowed = required | {"verdict", "budget_exhausted", "cause"}
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

    elif event_type == "node.attempt.failed":
        # ``verdict`` is present EXACTLY when the attempt failed at the independent
        # gate, and absent when it failed in the worker or artifact verification.
        # Its presence is therefore the machine-readable discriminator between a
        # gate rejection and a worker fault — which is what makes the per-attempt
        # gate error rate computable without parsing the free-text ``reason``.
        required = {"node_id", "attempt", "reason", "cause"}
        if set(payload) - {"verdict"} != required:
            raise GraphIntegrityError("node.attempt.failed payload has an invalid shape")
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
