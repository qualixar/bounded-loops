"""Tenant-scoped verification of graph output artifact references."""

from __future__ import annotations

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.events import GraphRunIdentity


class LocalArtifactVerifier:
    """Verify active, content-correct output bytes before an independent gate."""

    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store

    def verify(self, *, identity: GraphRunIdentity, digests: tuple[str, ...]) -> None:
        access = ArtifactAccess(identity.organization_id, identity.project_id)
        for digest in digests:
            ref = ArtifactRef(digest, identity.organization_id, identity.project_id)
            with self._store.open(ref, access):
                pass
