"""Reconcile independent audit results into a release decision (ADR-12 R1 / LLD 06).

Coverage-based cross-model audit produces multiple results per release cell from INDEPENDENT
lanes. Reconciliation collapses them into one decision WITHOUT distorting the evidence: it never
lowers a finding's severity and never averages away disagreement — per cell it preserves the
HIGHEST severity seen and flags DISSENT when lanes disagree. A release is issued only when every
mandatory cell is covered by an independent auditor (producer is never the sole auditor) and
carries no unresolved S0/S1 finding — the same gate as `validate_audit_coverage`, but enumerating
EVERY blocking reason instead of raising on the first, so a release owner gets a whole decision.

This is pure policy over domain audit objects; the append-only event log + receipts stay authority
(a ReleaseDecision is advice for a human release owner, per LLD 06 "human release authority").
"""

from __future__ import annotations

from dataclasses import dataclass

from bounded_loops.graph.domain.audits import AuditCell, AuditResult
from bounded_loops.graph.domain.errors import GraphValidationError

_SEVERITY_RANK = {"none": 0, "S3": 1, "S2": 2, "S1": 3, "S0": 4}
_FINDING_SEVERITIES = frozenset({"S0", "S1", "S2", "S3"})
_BLOCKING_SEVERITIES = frozenset({"S0", "S1"})
_RESOLVED = "resolved"


@dataclass(frozen=True)
class CellVerdict:
    """One reconciled release cell. `highest_severity` is the max severity seen (never lowered);
    `dissent` is set when the lanes disagreed on the outcome."""

    cell: str
    mandatory: bool
    highest_severity: str
    has_unresolved_blocker: bool
    dissent: bool
    missing: bool
    producer_only: bool

    @property
    def release_blocking(self) -> bool:
        # missing / producer_only are only ever set for mandatory cells; an unresolved S0/S1
        # blocks release only on a mandatory cell (non-mandatory cells are sampled, informational
        # — matching validate_audit_coverage, which skips non-mandatory cells entirely).
        return self.missing or self.producer_only or (self.mandatory and self.has_unresolved_blocker)


@dataclass(frozen=True)
class ReleaseDecision:
    released: bool
    reason: str
    blocking_cells: tuple[str, ...]
    dissent_cells: tuple[str, ...]
    verdicts: tuple[CellVerdict, ...]


def reconcile_audit(cells: tuple[AuditCell, ...], results: tuple[AuditResult, ...]) -> ReleaseDecision:
    """Reconcile per-cell audit results into a release decision. Raises on MALFORMED input
    (unknown finding severity, empty identifiers); a well-formed but failing audit is a BLOCKED
    decision, never an exception."""
    _validate_results(results)
    by_cell: dict[str, list[AuditResult]] = {}
    for result in results:
        by_cell.setdefault(result.cell, []).append(result)
    verdicts = tuple(_cell_verdict(cell, by_cell.get(cell.name, [])) for cell in cells)
    blocking = tuple(verdict.cell for verdict in verdicts if verdict.release_blocking)
    dissent = tuple(verdict.cell for verdict in verdicts if verdict.dissent)
    # Fail closed on a misconfigured plan: a release with NO mandatory cell is never "cleared"
    # (that would be a vacuous pass). This is stricter than validate_audit_coverage, which
    # returns quietly on an empty/all-optional plan — reconcile hardens that fail-open.
    if not any(cell.mandatory for cell in cells):
        return ReleaseDecision(False, "release blocked: no mandatory release cells defined", blocking, dissent, verdicts)
    released = not blocking
    reason = (
        "release cleared: every mandatory cell is independently covered with no unresolved S0/S1"
        if released
        else "release blocked on cells: " + ", ".join(blocking)
    )
    return ReleaseDecision(released, reason, blocking, dissent, verdicts)


def _cell_verdict(cell: AuditCell, covered: list[AuditResult]) -> CellVerdict:
    missing = cell.mandatory and not covered
    producer_only = cell.mandatory and bool(covered) and all(r.assessor == r.producer for r in covered)
    severities = [r.finding.severity for r in covered if r.finding is not None]
    highest = max(severities, key=lambda severity: _SEVERITY_RANK[severity], default="none")
    has_blocker = any(
        r.finding is not None
        and r.finding.severity in _BLOCKING_SEVERITIES
        and r.finding.disposition != _RESOLVED
        for r in covered
    )
    dissent = len({_outcome(r) for r in covered}) > 1
    return CellVerdict(
        cell=cell.name,
        mandatory=cell.mandatory,
        highest_severity=highest,
        has_unresolved_blocker=has_blocker,
        dissent=dissent,
        missing=missing,
        producer_only=producer_only,
    )


def _outcome(result: AuditResult) -> str:
    # A lane's outcome for dissent detection: its finding severity, or "clean" if it found nothing.
    return result.finding.severity if result.finding is not None else "clean"


def _validate_results(results: tuple[AuditResult, ...]) -> None:
    for result in results:
        for field, value in (("cell", result.cell), ("assessor", result.assessor), ("producer", result.producer)):
            if not isinstance(value, str) or not value:
                raise GraphValidationError("audit_result", f"/{field}", f"{field} must be a non-empty string")
        finding = result.finding
        if finding is not None:
            if finding.severity not in _FINDING_SEVERITIES:
                raise GraphValidationError("audit_severity", "/finding/severity", "unknown finding severity")
            if not isinstance(finding.disposition, str) or not finding.disposition:
                raise GraphValidationError("audit_disposition", "/finding/disposition", "disposition must be a non-empty string")
