"""Content-addressed fetch/store of connector request & response bodies (RB).

The forwarder needs exactly two operations against the tenant's content-addressed artifact
store: fetch a request document by digest, and store a response body (returning its digest).
This port keeps the forwarder tenant-agnostic; the real ``LocalArtifactBody`` binds ONE
tenant + retention policy over ``LocalArtifactStore`` so a forwarder cannot read or write
another tenant's artifacts.
"""

from __future__ import annotations

from io import BytesIO
from typing import Protocol

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef


class ArtifactBodyPort(Protocol):
    """Fetch a body by digest; store a body and return its ``sha256:`` digest."""

    def fetch(self, digest: str) -> bytes: ...

    def store(self, data: bytes) -> str: ...


class LocalArtifactBody:
    """Real ``ArtifactBodyPort`` over ``LocalArtifactStore``, bound to one tenant + policy."""

    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        organization_id: str,
        project_id: str,
        producer_attempt: str,
        media_type: str = "application/json",
        sensitivity: str = "restricted",
        retention_class: str = "connector-io",
        expires_at: str | None = None,
    ) -> None:
        self._store = store
        self._organization_id = organization_id
        self._project_id = project_id
        self._producer_attempt = producer_attempt
        self._media_type = media_type
        self._sensitivity = sensitivity
        self._retention_class = retention_class
        self._expires_at = expires_at

    def fetch(self, digest: str) -> bytes:
        ref = ArtifactRef(digest, self._organization_id, self._project_id)
        with self._store.open(ref, ArtifactAccess(self._organization_id, self._project_id)) as handle:
            return handle.read()

    def store(self, data: bytes) -> str:
        policy = ArtifactPolicy(
            organization_id=self._organization_id,
            project_id=self._project_id,
            producer_attempt=self._producer_attempt,
            media_type=self._media_type,
            sensitivity=self._sensitivity,
            retention_class=self._retention_class,
            expires_at=self._expires_at,
            legal_hold_allowed=False,
        )
        return self._store.put(BytesIO(data), policy).digest
