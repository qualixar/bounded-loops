"""Canonical serialisation of audit domain objects.

Extracted in P3 from the local audit store, which was removed in 0.7.0 as an orphaned
capability. These functions are pure — mapping in, domain object out, no I/O — and the
canonical field set of a domain object is domain knowledge, not a property of whatever
persists it, which is why they outlived the store.

Only the two read-side functions with real callers remain. The four ``*_to_dict`` writers
and two further readers existed solely for the store's ``put_*``/``load_*`` pairs and went
with it; they are recoverable from tag ``v0.6.10``. Their entries in
``scripts/unreachable_allowlist.py`` declared them reachable via the store's private
aliased imports, and that referrer no longer exists — a declared reason that outlives what
it describes is worse than no entry.

Every ``*_from_*`` function validates at the domain-object boundary and raises
``GraphValidationError`` rather than returning a half-built object, so a tampered or hostile
artifact fails closed at the read boundary instead of flowing inward as plausible data.
"""

from __future__ import annotations

from typing import cast

from bounded_loops.graph.domain.audits import (
    AuditAssignment,
    AuditCell,
    AuditFinding,
    AuditPlan,
    AuditResult,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def plan_from_mapping(data: dict[str, object]) -> AuditPlan:
    """Deserialize an :class:`AuditPlan` from a JSON mapping (raises ``GraphValidationError`` on a
    malformed plan — the caller decides whether that blocks release or is surfaced as a note)."""
    raw_cells = cast(list[dict[str, object]], data["mandatory_cells"])
    cells = tuple(
        AuditCell(name=c["name"], mandatory=c["mandatory"])  # type: ignore[arg-type]
        for c in raw_cells
    )
    raw_assignments = cast(list[dict[str, object]], data["assignments"])
    assignments = tuple(
        AuditAssignment(
            cell=a["cell"],  # type: ignore[arg-type]
            model_id=a["model_id"],  # type: ignore[arg-type]
            tool_id=a["tool_id"],  # type: ignore[arg-type]
            version=a["version"],  # type: ignore[arg-type]
            rubric_digest=a["rubric_digest"],  # type: ignore[arg-type]
            independence=a["independence"],  # type: ignore[arg-type]
        )
        for a in raw_assignments
    )
    return AuditPlan(
        artifact_digest=data["artifact_digest"],  # type: ignore[arg-type]
        rubric_digest=data["rubric_digest"],  # type: ignore[arg-type]
        mandatory_cells=cells,
        assignments=assignments,
    )


def result_from_mapping(data: dict[str, object]) -> AuditResult:
    """Deserialize an :class:`AuditResult` from a JSON mapping (raises ``GraphValidationError`` on a
    malformed result so a hostile artifact fails closed at the read boundary)."""
    finding = None
    raw_finding = data["finding"]
    if raw_finding is not None:
        # Explicit type guard (not ``assert`` — that is stripped under ``python -O``, C-079 dual-audit):
        # a non-object ``finding`` must fail closed here so the read-side records a note.
        if not isinstance(raw_finding, dict):
            raise GraphValidationError("audit_result", "/finding", "finding must be a JSON object or null")
        finding = AuditFinding(
            finding_id=raw_finding["finding_id"],  # type: ignore[index]
            severity=raw_finding["severity"],  # type: ignore[index]
            disposition=raw_finding["disposition"],  # type: ignore[index]
        )
    return AuditResult(
        cell=data["cell"],  # type: ignore[arg-type]
        assessor=data["assessor"],  # type: ignore[arg-type]
        producer=data["producer"],  # type: ignore[arg-type]
        finding=finding,
    )


