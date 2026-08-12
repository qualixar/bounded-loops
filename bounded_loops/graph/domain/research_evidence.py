"""Digest-pinned source and claim evidence contracts for publication graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from urllib.parse import urlparse

from bounded_loops.graph.domain.artifacts import ArtifactRef
from bounded_loops.graph.domain.errors import GraphValidationError


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class SourceLiveness(str, Enum):
    LIVE = "live"
    ARCHIVED = "archived"
    UNREACHABLE = "unreachable"


class ClaimRelation(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    MENTIONS = "mentions"
    INSUFFICIENT = "insufficient"


class ClaimDisposition(str, Enum):
    REQUIRES_SUPPORT = "requires_support"
    UNVERIFIED = "unverified"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    canonical_url: str
    retrieved_at: str
    content_digest: str
    publisher: str | None
    published_at: str | None
    liveness: SourceLiveness
    artifact_ref: ArtifactRef


@dataclass(frozen=True)
class AtomicClaim:
    claim_id: str
    text: str
    claim_type: str
    required_assurance: str
    disposition: ClaimDisposition


@dataclass(frozen=True)
class ClaimAssessment:
    claim_id: str
    source_id: str
    relation: ClaimRelation
    excerpt_bounds: tuple[int, int] | None
    rationale: str
    assessor_identity: str


def validate_publication_evidence(
    sources: tuple[SourceSnapshot, ...],
    claims: tuple[AtomicClaim, ...],
    assessments: tuple[ClaimAssessment, ...],
) -> None:
    """Reject unsupported or contradicted factual claims before publication."""
    source_by_id = _sources(sources)
    claim_by_id = _claims(claims)
    by_claim: dict[str, list[ClaimAssessment]] = {claim_id: [] for claim_id in claim_by_id}
    for assessment in assessments:
        _assessment(assessment, source_by_id, claim_by_id)
        by_claim[assessment.claim_id].append(assessment)

    for claim in claims:
        assessments_for_claim = by_claim[claim.claim_id]
        if any(item.relation is ClaimRelation.CONTRADICTS for item in assessments_for_claim):
            raise GraphValidationError("claim_contradiction", f"/claims/{claim.claim_id}", "claim has an unresolved contradiction")
        if claim.disposition is not ClaimDisposition.REQUIRES_SUPPORT:
            continue
        supporting = [item for item in assessments_for_claim if item.relation is ClaimRelation.SUPPORTS]
        if not supporting:
            raise GraphValidationError("claim_unsupported", f"/claims/{claim.claim_id}", "claim is unsupported")
        if all(source_by_id[item.source_id].liveness is SourceLiveness.UNREACHABLE for item in supporting):
            raise GraphValidationError(
                "source_unreachable", f"/claims/{claim.claim_id}", "claim support is unreachable and unarchived"
            )


def _sources(sources: tuple[SourceSnapshot, ...]) -> dict[str, SourceSnapshot]:
    values: dict[str, SourceSnapshot] = {}
    for source in sources:
        _nonempty(source.source_id, "/sources/source_id", "source ID")
        if source.source_id in values:
            raise GraphValidationError("source_duplicate", "/sources", "source IDs must be unique")
        parsed = urlparse(source.canonical_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GraphValidationError("source_url", "/sources/canonical_url", "source URL must be absolute HTTP(S)")
        _instant(source.retrieved_at, "/sources/retrieved_at")
        if source.published_at is not None:
            _instant(source.published_at, "/sources/published_at")
        _digest(source.content_digest, "/sources/content_digest")
        _artifact_ref(source.artifact_ref)
        if source.content_digest != source.artifact_ref.digest:
            raise GraphValidationError("source_digest", "/sources/artifact_ref", "source artifact digest must match content digest")
        if not isinstance(source.liveness, SourceLiveness):
            raise GraphValidationError("source_liveness", "/sources/liveness", "source liveness is invalid")
        values[source.source_id] = source
    return values


def _claims(claims: tuple[AtomicClaim, ...]) -> dict[str, AtomicClaim]:
    values: dict[str, AtomicClaim] = {}
    for claim in claims:
        for value, pointer, label in (
            (claim.claim_id, "/claims/claim_id", "claim ID"),
            (claim.text, "/claims/text", "claim text"),
            (claim.claim_type, "/claims/claim_type", "claim type"),
            (claim.required_assurance, "/claims/required_assurance", "required assurance"),
        ):
            _nonempty(value, pointer, label)
        if claim.claim_id in values:
            raise GraphValidationError("claim_duplicate", "/claims", "claim IDs must be unique")
        if not isinstance(claim.disposition, ClaimDisposition):
            raise GraphValidationError("claim_disposition", "/claims/disposition", "claim disposition is invalid")
        values[claim.claim_id] = claim
    return values


def _assessment(
    assessment: ClaimAssessment,
    sources: dict[str, SourceSnapshot],
    claims: dict[str, AtomicClaim],
) -> None:
    if assessment.claim_id not in claims:
        raise GraphValidationError("assessment_claim", "/assessments/claim_id", "assessment references a missing claim")
    if assessment.source_id not in sources:
        raise GraphValidationError("assessment_source", "/assessments/source_id", "assessment references a missing source")
    if not isinstance(assessment.relation, ClaimRelation):
        raise GraphValidationError("assessment_relation", "/assessments/relation", "assessment relation is invalid")
    _nonempty(assessment.rationale, "/assessments/rationale", "assessment rationale")
    _nonempty(assessment.assessor_identity, "/assessments/assessor_identity", "assessor identity")
    if assessment.excerpt_bounds is not None:
        if (
            not isinstance(assessment.excerpt_bounds, tuple)
            or len(assessment.excerpt_bounds) != 2
            or not all(isinstance(value, int) and value >= 0 for value in assessment.excerpt_bounds)
            or assessment.excerpt_bounds[0] >= assessment.excerpt_bounds[1]
        ):
            raise GraphValidationError("excerpt_bounds", "/assessments/excerpt_bounds", "excerpt bounds are invalid")


def _artifact_ref(ref: ArtifactRef) -> None:
    _digest(ref.digest, "/sources/artifact_ref/digest")
    _nonempty(ref.organization_id, "/sources/artifact_ref/organization_id", "artifact organization")
    _nonempty(ref.project_id, "/sources/artifact_ref/project_id", "artifact project")


def _digest(value: str, pointer: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise GraphValidationError("source_digest", pointer, "must be a SHA-256 digest")


def _instant(value: str, pointer: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise GraphValidationError("source_timestamp", pointer, "must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GraphValidationError("source_timestamp", pointer, "must include a timezone")


def _nonempty(value: str, pointer: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise GraphValidationError("research_evidence", pointer, f"{label} must be non-empty")
