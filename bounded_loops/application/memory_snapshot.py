"""Memory-port wrapper for an explicit controller-owned graph snapshot."""

from __future__ import annotations

from bounded_loops.domain.models import LoopContext, Verdict


class SnapshotMemory:
    """Read only a controller-provided graph snapshot.

    The legacy file-backed memory port remains available to ordinary ``wire()``
    calls. A graph-owned attempt cannot update that package-local state: its
    controller must own snapshot evolution in a later explicit graph contract.
    """

    def __init__(self, snapshot: str) -> None:
        self._snapshot = snapshot

    def load(self, ctx: LoopContext) -> str:
        del ctx
        return self._snapshot

    def update(self, ctx: LoopContext, lap: int, verdict: Verdict, decision: str) -> None:
        del ctx, lap, verdict, decision
