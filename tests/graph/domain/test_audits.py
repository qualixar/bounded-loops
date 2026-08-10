from __future__ import annotations

import pytest

from bounded_loops.graph.domain.audits import (
    AuditCell,
    AuditFinding,
    AuditResult,
    AuditedArtifact,
    RepairAttempt,
    validate_audit_coverage,
    validate_repair_lineage,
)
from bounded_loops.graph.domain.errors import GraphValidationError


_CELLS = ("architecture", "security", "reliability", "correctness", "privacy", "usability", "evidence", "distribution")


def test_audit_coverage_requires_independent_results_for_every_mandatory_cell():
    results = tuple(AuditResult(cell=cell, assessor="sol", producer="terra", finding=None) for cell in _CELLS)
    validate_audit_coverage(tuple(AuditCell(cell, mandatory=True) for cell in _CELLS), results)

    with pytest.raises(GraphValidationError, match="missing mandatory"):
        validate_audit_coverage(tuple(AuditCell(cell, mandatory=True) for cell in _CELLS), results[:-1])
    with pytest.raises(GraphValidationError, match="producer"):
        validate_audit_coverage((AuditCell("security", mandatory=True),), (AuditResult("security", "terra", "terra", None),))
    with pytest.raises(GraphValidationError, match="S1"):
        validate_audit_coverage((AuditCell("security", mandatory=True),), (AuditResult("security", "sol", "terra", AuditFinding("F-1", "S1", "open")),))


def test_repair_lineage_requires_a_new_artifact_and_preserves_prior_finding_ids():
    original = AuditedArtifact("sha256:" + "a" * 64, ("PP-002", "PP-007"))
    repair = RepairAttempt(
        repair_id="repair-1", input_artifact_digest=original.artifact_digest,
        output_artifact_digest="sha256:" + "b" * 64, addressed_finding_ids=("PP-002",),
        regression_evidence_digest="sha256:" + "c" * 64,
    )

    validate_repair_lineage(original, repair)
    assert original.finding_ids == ("PP-002", "PP-007")
    with pytest.raises(GraphValidationError, match="new artifact"):
        validate_repair_lineage(
            original,
            RepairAttempt(
                repair_id="repair-2", input_artifact_digest=original.artifact_digest,
                output_artifact_digest=original.artifact_digest, addressed_finding_ids=("PP-002",),
                regression_evidence_digest="sha256:" + "c" * 64,
            ),
        )
    with pytest.raises(GraphValidationError, match="prior finding"):
        validate_repair_lineage(
            original,
            RepairAttempt(
                repair_id="repair-3", input_artifact_digest=original.artifact_digest,
                output_artifact_digest="sha256:" + "b" * 64, addressed_finding_ids=("PP-999",),
                regression_evidence_digest="sha256:" + "c" * 64,
            ),
        )
