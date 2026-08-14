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

def attempt_provenance(attempt: int, repair_round: int = 0) -> str:
    """The ``producer_attempt`` string for one unit of work.

    ``attempt`` alone collided across repair rounds: attempts RESET at a boundary, so round 0's
    attempt 1 and round 3's attempt 1 both recorded ``"1"`` and two different artifacts claimed the
    same producer. Found by the P4.5 round-2 audit (Grok 9).

    The round is omitted at 0 so every artifact written before this existed keeps the exact
    provenance string it had — the same convention the receipt keys and the loop bridge use.
    """
    return str(attempt) if repair_round <= 0 else f"{attempt}.r{repair_round}"
