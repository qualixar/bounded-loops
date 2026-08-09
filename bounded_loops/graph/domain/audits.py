"""Coverage-based independent production-audit contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re

from bounded_loops.graph.domain.errors import GraphValidationError


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class AuditCell:
    name: str
    mandatory: bool


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    disposition: str


@dataclass(frozen=True)
class AuditResult:
    cell: str
    assessor: str
    producer: str
    finding: AuditFinding | None


@dataclass(frozen=True)
class AuditedArtifact:
    """A frozen audit target and the finding identities recorded against it."""

    artifact_digest: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class RepairAttempt:
    """A new candidate produced in response to findings on an older artifact."""

    repair_id: str
    input_artifact_digest: str
    output_artifact_digest: str
    addressed_finding_ids: tuple[str, ...]
    regression_evidence_digest: str


def validate_audit_coverage(cells: tuple[AuditCell, ...], results: tuple[AuditResult, ...]) -> None:
    """Fail release coverage on missing, self-only, or open S0/S1 cells."""
    by_cell: dict[str, list[AuditResult]] = {}
    for result in results:
        by_cell.setdefault(result.cell, []).append(result)
    for cell in cells:
        if not cell.mandatory:
            continue
        covered = by_cell.get(cell.name, [])
        if not covered:
            raise GraphValidationError("audit_coverage", f"/cells/{cell.name}", "missing mandatory audit cell")
        if all(result.assessor == result.producer for result in covered):
            raise GraphValidationError("audit_independence", f"/cells/{cell.name}", "producer cannot be sole auditor")
        for result in covered:
            if result.finding and result.finding.severity in {"S0", "S1"} and result.finding.disposition != "resolved":
                raise GraphValidationError("audit_blocker", f"/cells/{cell.name}", f"unresolved {result.finding.severity} finding")


def validate_repair_lineage(original: AuditedArtifact, repair: RepairAttempt) -> None:
    """Preserve historical finding identity while requiring a new repaired artifact."""
    _digest(original.artifact_digest, "/original/artifact_digest")
    _digest(repair.input_artifact_digest, "/repair/input_artifact_digest")
    _digest(repair.output_artifact_digest, "/repair/output_artifact_digest")
    _digest(repair.regression_evidence_digest, "/repair/regression_evidence_digest")
    if not isinstance(repair.repair_id, str) or not repair.repair_id:
        raise GraphValidationError("repair_id", "/repair/repair_id", "repair ID must be non-empty")
    if repair.input_artifact_digest != original.artifact_digest:
        raise GraphValidationError("repair_input", "/repair/input_artifact_digest", "repair input must be the audited artifact")
    if repair.output_artifact_digest == original.artifact_digest:
        raise GraphValidationError("repair_output", "/repair/output_artifact_digest", "repair must produce a new artifact digest")
    if not original.finding_ids or len(set(original.finding_ids)) != len(original.finding_ids):
        raise GraphValidationError("repair_findings", "/original/finding_ids", "prior finding IDs must be non-empty and unique")
    if not repair.addressed_finding_ids or len(set(repair.addressed_finding_ids)) != len(repair.addressed_finding_ids):
        raise GraphValidationError("repair_findings", "/repair/addressed_finding_ids", "addressed finding IDs must be non-empty and unique")
    if not set(repair.addressed_finding_ids) <= set(original.finding_ids):
        raise GraphValidationError("repair_findings", "/repair/addressed_finding_ids", "repair must reference a prior finding")


def _digest(value: str, pointer: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise GraphValidationError("audit_digest", pointer, "must be a SHA-256 digest")
