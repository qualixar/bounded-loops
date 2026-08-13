"""Canonical serialisation of audit domain objects.

Extracted from ``adapters/persistence/audit_store.py`` in P3. These functions are pure —
mapping in, domain object out, no I/O — and the canonical field set of a domain object is
domain knowledge, not a property of the store that happens to persist it. While they lived
in the adapter, the read-side Arena projection (an application module) had to import from
``adapters/`` to deserialize a result, which was one of the layering violations the P3
tripwire now forbids.

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
    AuditedArtifact,
    RepairAttempt,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def plan_to_dict(plan: AuditPlan) -> dict[str, object]:
    return {
        "artifact_digest": plan.artifact_digest,
        "rubric_digest": plan.rubric_digest,
        "mandatory_cells": [
            {"name": c.name, "mandatory": c.mandatory}
            for c in plan.mandatory_cells
        ],
        "assignments": [
            {
                "cell": a.cell,
                "independence": a.independence,
                "model_id": a.model_id,
                "rubric_digest": a.rubric_digest,
                "tool_id": a.tool_id,
                "version": a.version,
            }
            for a in plan.assignments
        ],
    }


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


def result_to_dict(result: AuditResult) -> dict[str, object]:
    finding_dict: dict[str, object] | None = None
    if result.finding is not None:
        finding_dict = {
            "disposition": result.finding.disposition,
            "finding_id": result.finding.finding_id,
            "severity": result.finding.severity,
        }
    return {
        "assessor": result.assessor,
        "cell": result.cell,
        "finding": finding_dict,
        "producer": result.producer,
    }


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


def artifact_to_dict(artifact: AuditedArtifact) -> dict[str, object]:
    return {
        "artifact_digest": artifact.artifact_digest,
        "finding_ids": list(artifact.finding_ids),
    }


def artifact_from_mapping(data: dict[str, object]) -> AuditedArtifact:
    raw_ids = data["finding_ids"]
    if not isinstance(raw_ids, list):
        raise ValueError("finding_ids must be a list")
    return AuditedArtifact(
        artifact_digest=data["artifact_digest"],  # type: ignore[arg-type]
        finding_ids=tuple(str(x) for x in raw_ids),
    )


def repair_to_dict(repair: RepairAttempt) -> dict[str, object]:
    return {
        "addressed_finding_ids": list(repair.addressed_finding_ids),
        "input_artifact_digest": repair.input_artifact_digest,
        "output_artifact_digest": repair.output_artifact_digest,
        "regression_evidence_digest": repair.regression_evidence_digest,
        "repair_id": repair.repair_id,
    }


def repair_from_mapping(data: dict[str, object]) -> RepairAttempt:
    raw_addressed = data["addressed_finding_ids"]
    if not isinstance(raw_addressed, list):
        raise ValueError("addressed_finding_ids must be a list")
    return RepairAttempt(
        repair_id=data["repair_id"],  # type: ignore[arg-type]
        input_artifact_digest=data["input_artifact_digest"],  # type: ignore[arg-type]
        output_artifact_digest=data["output_artifact_digest"],  # type: ignore[arg-type]
        addressed_finding_ids=tuple(str(x) for x in raw_addressed),
        regression_evidence_digest=data["regression_evidence_digest"],  # type: ignore[arg-type]
    )
