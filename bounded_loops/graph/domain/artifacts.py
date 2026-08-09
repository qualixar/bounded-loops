"""Immutable artifact references and controller retention metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ArtifactState(str, Enum):
    ACTIVE = "ACTIVE"
    TOMBSTONED = "TOMBSTONED"
    CRYPTO_SHREDDED = "CRYPTO_SHREDDED"


@dataclass(frozen=True)
class ArtifactPolicy:
    organization_id: str
    project_id: str
    producer_attempt: str
    media_type: str
    sensitivity: str
    retention_class: str
    expires_at: str | None = None
    legal_hold_allowed: bool = False


@dataclass(frozen=True)
class ArtifactRef:
    digest: str
    organization_id: str
    project_id: str


@dataclass(frozen=True)
class ArtifactRecord:
    ref: ArtifactRef
    digest: str
    media_type: str
    size: int
    producer_attempt: str
    sensitivity: str
    retention_class: str
    state: ArtifactState
    tombstone_reason: str | None
    expires_at: str | None = None
    legal_hold_allowed: bool = False
    legal_hold: bool = False


@dataclass(frozen=True)
class ArtifactAccess:
    organization_id: str
    project_id: str
