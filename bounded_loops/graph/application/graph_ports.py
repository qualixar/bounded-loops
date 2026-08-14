# bounded_loops/graph/application/graph_ports.py
# THIS FILE IS THE GRAPH SEAM. Alter only after a contract change.
"""
Port Protocols for the graph engine.

This is the graph engine's equivalent of ``bounded_loops/application/ports.py``:
the seam between ``graph/application/`` and every concrete graph adapter. The base
engine was given such a seam and a composition root on day one and has honoured
both; the graph engine was given neither, so twelve application modules grew
direct imports of concrete adapters — ``LocalArtifactStore`` in seven of them,
``GraphEventLog`` in seven.

Two of these Protocols (``ArtifactWriterPort``, ``ArtifactReaderPort``) already
existed, declared halfway down ``workspace_promotion.py`` — a use case, not a
seam. A port that lives inside one of its own consumers cannot be found by the
next consumer, which is exactly how the direct-import habit spread. They are
re-exported from there so no caller breaks.

No logic, no state, no I/O in this file — pure type declarations.
"""

from __future__ import annotations

from datetime import datetime
from typing import BinaryIO, Protocol, Sequence, runtime_checkable

from bounded_loops.graph.domain.artifacts import (
    ArtifactAccess,
    ArtifactPolicy,
    ArtifactRecord,
    ArtifactRef,
)
from bounded_loops.graph.domain.events import (
    GraphRunIdentity,
    GraphRunProjection,
    StoredGraphEvent,
    UnsignedGraphEvent,
    VerifiedGraphEventSnapshot,
)


class ByteSourcePort(Protocol):
    """The only capability an artifact store needs from a byte source: sequential reads.

    Deliberately narrower than ``BinaryIO``. The descriptor-backed reader the workspace
    promoter passes in is not a full ``BinaryIO``, and widening this to one would either
    reject that reader or force a cast that hides the mismatch.
    """

    def read(self, size: int = ...) -> bytes: ...


@runtime_checkable
class ArtifactWriterPort(Protocol):
    """Commit bytes to content-addressed storage, all-or-nothing per batch."""

    def put_many(
        self, items: Sequence[tuple[ByteSourcePort, ArtifactPolicy]],
    ) -> tuple[ArtifactRecord, ...]: ...


@runtime_checkable
class ArtifactReaderPort(Protocol):
    """Read a stored artifact back, subject to the caller's tenant access."""

    def open(self, ref: ArtifactRef, access: ArtifactAccess) -> BinaryIO: ...


@runtime_checkable
class ArtifactStorePort(ArtifactWriterPort, ArtifactReaderPort, Protocol):
    """The full artifact store: write, read, and retention lifecycle.

    Retention is part of the port rather than a separate one because a store that can
    write but cannot tombstone leaves the retention sweeper with no way to honour a
    deletion request — the capability has to be reachable through the same seam the
    writer came through.
    """

    def put(self, stream: ByteSourcePort, policy: ArtifactPolicy) -> ArtifactRecord: ...

    def tombstone(self, ref: ArtifactRef, reason: str) -> ArtifactRecord: ...

    def set_legal_hold(self, ref: ArtifactRef, enabled: bool) -> ArtifactRecord: ...

    def sweep_expired(self, now: datetime) -> tuple[ArtifactRecord, ...]: ...


@runtime_checkable
class EventLogPort(Protocol):
    """The run's sole durable state: an append-only, hash-chained receipt stream.

    ``append`` takes the writer's expected previous hash so a concurrent writer is
    refused rather than silently interleaved — the optimistic-concurrency check is
    part of the contract, not an implementation detail of one adapter.
    """

    @property
    def identity(self) -> GraphRunIdentity: ...

    def append(
        self, expected_previous_hash: str, event: UnsignedGraphEvent,
    ) -> StoredGraphEvent: ...

    def replay(self) -> tuple[StoredGraphEvent, ...]: ...

    def replay_projection(self) -> GraphRunProjection: ...

    def verified_snapshot(self) -> VerifiedGraphEventSnapshot: ...
