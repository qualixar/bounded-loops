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
    """A finding against an artifact. ``finding_id`` is a STABLE GLOBAL identity: the same id
    always denotes the same logical finding (and distinct findings carry distinct ids), matching
    ``AuditedArtifact.finding_ids`` so a repair can address it and reconciliation can unblock it."""

    finding_id: str
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


@dataclass(frozen=True)
class AuditAssignment:
    """Binds a coverage cell to an independent evaluator identity.

    Validators run inline at construction time so an ``AuditPlan`` can assert
    that every assignment it holds is already well-formed.  The ``independence``
    field documents the enforced constraint (e.g. ``"assessor != producer"``);
    it is never empty so the intent is always explicit in the persisted plan.
    """

    cell: str
    model_id: str
    tool_id: str
    version: str
    rubric_digest: str
    independence: str

    def __post_init__(self) -> None:
        for field, value in (
            ("cell", self.cell),
            ("model_id", self.model_id),
            ("tool_id", self.tool_id),
            ("version", self.version),
            ("independence", self.independence),
        ):
            if not isinstance(value, str) or not value:
                raise GraphValidationError("audit_assignment", f"/{field}", f"{field} must be non-empty")
        _digest(self.rubric_digest, "/rubric_digest")


@dataclass(frozen=True)
class AuditPlan:
    """An audit plan for one frozen artifact: mandatory coverage cells and their
    evaluator assignments.

    Fail-closed invariants (enforced at construction, mirroring the
    ``reconcile_audit`` vacuous-pass guard):
    - At least one mandatory cell — an empty plan would vacuously clear release.
    - No duplicate cell names.
    - Every mandatory cell has a matching assignment.
    - Both digest fields are valid SHA-256.
    """

    artifact_digest: str
    rubric_digest: str
    mandatory_cells: tuple[AuditCell, ...]
    assignments: tuple[AuditAssignment, ...]

    def __post_init__(self) -> None:
        _digest(self.artifact_digest, "/artifact_digest")
        _digest(self.rubric_digest, "/rubric_digest")
        if not self.mandatory_cells:
            raise GraphValidationError(
                "audit_plan", "/mandatory_cells",
                "audit plan must have at least one mandatory cell (fail-closed: empty plan would vacuously pass)",
            )
        cell_names = [cell.name for cell in self.mandatory_cells]
        if len(set(cell_names)) != len(cell_names):
            raise GraphValidationError("audit_plan", "/mandatory_cells", "duplicate cell names in mandatory cells")
        assigned_cells = {a.cell for a in self.assignments}
        for cell in self.mandatory_cells:
            if cell.mandatory and cell.name not in assigned_cells:
                raise GraphValidationError(
                    "audit_plan", f"/assignments/{cell.name}",
                    f"mandatory cell {cell.name!r} has no assignment",
                )


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
