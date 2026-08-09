from __future__ import annotations

from dataclasses import replace

import pytest

from bounded_loops.graph.domain.artifacts import ArtifactRef
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.research_evidence import (
    AtomicClaim,
    ClaimAssessment,
    ClaimDisposition,
    ClaimRelation,
    SourceLiveness,
    SourceSnapshot,
    validate_publication_evidence,
)


def _source(*, liveness: SourceLiveness = SourceLiveness.LIVE) -> SourceSnapshot:
    return SourceSnapshot(
        source_id="source-1", canonical_url="https://example.test/source",
        retrieved_at="2026-08-08T00:00:00Z", content_digest="sha256:" + "a" * 64,
        publisher="Example", published_at="2026-08-01T00:00:00Z", liveness=liveness,
        artifact_ref=ArtifactRef("sha256:" + "a" * 64, "org-1", "project-1"),
    )


def _claim(*, disposition: ClaimDisposition = ClaimDisposition.REQUIRES_SUPPORT) -> AtomicClaim:
    return AtomicClaim(
        claim_id="claim-1", text="The product has a bounded execution policy.",
        claim_type="product_fact", required_assurance="high", disposition=disposition,
    )


def _assessment(*, relation: ClaimRelation = ClaimRelation.SUPPORTS) -> ClaimAssessment:
    return ClaimAssessment(
        claim_id="claim-1", source_id="source-1", relation=relation,
        excerpt_bounds=(0, 32), rationale="The retained source explicitly establishes the claim.",
        assessor_identity="auditor-1",
    )


def test_publication_evidence_requires_live_or_archived_digest_pinned_support():
    validate_publication_evidence((_source(),), (_claim(),), (_assessment(),))
    validate_publication_evidence(
        (_source(liveness=SourceLiveness.ARCHIVED),), (_claim(),), (_assessment(),),
    )

    with pytest.raises(GraphValidationError, match="missing source"):
        validate_publication_evidence((), (_claim(),), (_assessment(),))
    with pytest.raises(GraphValidationError, match="unreachable"):
        validate_publication_evidence(
            (_source(liveness=SourceLiveness.UNREACHABLE),), (_claim(),), (_assessment(),),
        )
    with pytest.raises(GraphValidationError, match="unsupported"):
        validate_publication_evidence((_source(),), (_claim(),), ())
    with pytest.raises(GraphValidationError, match="digest"):
        validate_publication_evidence(
            (replace(_source(), artifact_ref=ArtifactRef("sha256:" + "b" * 64, "org-1", "project-1")),),
            (_claim(),), (_assessment(),),
        )


def test_publication_evidence_surfaces_contradictions_and_bad_excerpt_bounds():
    with pytest.raises(GraphValidationError, match="contradiction"):
        validate_publication_evidence(
            (_source(),), (_claim(),), (_assessment(relation=ClaimRelation.CONTRADICTS),),
        )
    with pytest.raises(GraphValidationError, match="excerpt"):
        validate_publication_evidence(
            (_source(),), (_claim(),),
            (replace(_assessment(), excerpt_bounds=(9, 2)),),
        )


def test_explicit_unverified_or_abstain_claims_are_not_silent_support_requirements():
    validate_publication_evidence(
        (), (_claim(disposition=ClaimDisposition.UNVERIFIED),), (),
    )
    validate_publication_evidence(
        (), (_claim(disposition=ClaimDisposition.ABSTAIN),), (),
    )
