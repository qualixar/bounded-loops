"""Audit reconciliation → release decision (ADR-12 R1 / LLD 06): preserves highest severity and
dissent, blocks on missing/producer-only/unresolved-S0-S1 mandatory cells, and stays consistent
with the existing validate_audit_coverage gate."""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.audit_reconciliation import (
    ValidatedRepairIds,
    reconcile_audit,
    resolve_by_repair,
)
from bounded_loops.graph.domain.audits import (
    AuditCell,
    AuditedArtifact,
    AuditFinding,
    AuditResult,
    RepairAttempt,
    validate_audit_coverage,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def _cell(name: str, mandatory: bool = True) -> AuditCell:
    return AuditCell(name=name, mandatory=mandatory)


def _finding(severity: str, disposition: str = "open", finding_id: str = "F-1") -> AuditFinding:
    return AuditFinding(finding_id=finding_id, severity=severity, disposition=disposition)


def _result(cell: str, assessor: str = "auditor", producer: str = "maker", finding: AuditFinding | None = None) -> AuditResult:
    return AuditResult(cell=cell, assessor=assessor, producer=producer, finding=finding)


def _artifact(*finding_ids: str) -> AuditedArtifact:
    return AuditedArtifact(artifact_digest="sha256:" + "a" * 64, finding_ids=tuple(finding_ids))


def _repair(addressed: tuple[str, ...], *, out: str = "b") -> RepairAttempt:
    return RepairAttempt(
        repair_id="repair-1",
        input_artifact_digest="sha256:" + "a" * 64,
        output_artifact_digest="sha256:" + out * 64,
        addressed_finding_ids=addressed,
        regression_evidence_digest="sha256:" + "c" * 64,
    )


def test_release_cleared_when_all_mandatory_cells_independently_covered():
    cells = (_cell("security"), _cell("correctness"))
    results = (_result("security", "a1", "p1"), _result("correctness", "a2", "p1"))
    decision = reconcile_audit(cells, results)
    assert decision.released is True
    assert decision.blocking_cells == ()


def test_missing_mandatory_cell_blocks():
    cells = (_cell("security"), _cell("correctness"))
    decision = reconcile_audit(cells, (_result("security", "a1", "p1"),))
    assert decision.released is False
    assert "correctness" in decision.blocking_cells


def test_producer_only_audit_blocks():
    cells = (_cell("security"),)
    decision = reconcile_audit(cells, (_result("security", assessor="p1", producer="p1"),))
    assert decision.released is False
    assert "security" in decision.blocking_cells
    assert decision.verdicts[0].producer_only is True


def test_unresolved_blocker_blocks_but_resolved_does_not_and_severity_is_preserved():
    cells = (_cell("security"),)
    unresolved = reconcile_audit(cells, (_result("security", "a1", "p1", _finding("S1", "open")),))
    assert unresolved.released is False and "security" in unresolved.blocking_cells

    resolved = reconcile_audit(cells, (_result("security", "a1", "p1", _finding("S1", "resolved")),))
    assert resolved.released is True
    assert resolved.verdicts[0].highest_severity == "S1"  # severity is never lowered by disposition


def test_preserves_highest_severity_across_lanes():
    cells = (_cell("security"),)
    results = (
        _result("security", "a1", "p1", _finding("S2", "resolved")),
        _result("security", "a2", "p1", _finding("S0", "resolved")),
    )
    decision = reconcile_audit(cells, results)
    assert decision.verdicts[0].highest_severity == "S0"
    assert decision.released is True  # both dispositions resolved


def test_preserves_dissent_between_lanes():
    cells = (_cell("security"),)
    results = (
        _result("security", "a1", "p1", _finding("S1", "open")),
        _result("security", "a2", "p1", None),  # a second lane found it clean
    )
    decision = reconcile_audit(cells, results)
    assert decision.verdicts[0].dissent is True
    assert "security" in decision.dissent_cells
    assert decision.released is False  # unresolved S1 still blocks


def test_nonmandatory_cell_findings_are_surfaced_but_do_not_block_release():
    cells = (_cell("security", mandatory=True), _cell("style", mandatory=False))
    results = (
        _result("security", "a1", "p1", None),
        _result("style", "a2", "p1", _finding("S0", "open")),  # critical, but on a sampled cell
    )
    decision = reconcile_audit(cells, results)
    assert decision.released is True
    style = next(v for v in decision.verdicts if v.cell == "style")
    assert style.highest_severity == "S0" and style.has_unresolved_blocker is True
    assert style.release_blocking is False  # aligns with validate_audit_coverage (skips non-mandatory)


def test_no_mandatory_cells_is_blocked_fail_closed():
    # a plan with no mandatory cell must never vacuously clear a release
    assert reconcile_audit((), ()).released is False
    only_optional = (_cell("style", mandatory=False),)
    decision = reconcile_audit(only_optional, (_result("style", "a1", "p1"),))
    assert decision.released is False and "no mandatory" in decision.reason


def test_malformed_input_raises():
    with pytest.raises(GraphValidationError, match="severity"):
        reconcile_audit((_cell("x"),), (_result("x", "a", "p", _finding("critical", "open")),))
    with pytest.raises(GraphValidationError):
        reconcile_audit((_cell("x"),), (_result("", "a", "p"),))  # empty cell name
    with pytest.raises(GraphValidationError, match="finding_id"):
        reconcile_audit((_cell("x"),), (_result("x", "a", "p", _finding("S0", "open", finding_id="")),))


def test_a_valid_repair_unblocks_a_release():
    cells = (_cell("security"),)
    results = (_result("security", "a1", "p1", _finding("S0", "open", finding_id="F-1")),)
    assert reconcile_audit(cells, results).released is False  # blocked before repair

    resolved = resolve_by_repair(_artifact("F-1"), _repair(("F-1",)))
    assert resolved == frozenset({"F-1"})

    decision = reconcile_audit(cells, results, repaired_finding_ids=resolved)
    assert decision.released is True
    assert decision.verdicts[0].highest_severity == "S0"  # a repair unblocks; it never lowers severity


def test_a_repair_for_a_different_finding_does_not_unblock():
    cells = (_cell("security"),)
    results = (_result("security", "a1", "p1", _finding("S0", "open", finding_id="F-1")),)
    decision = reconcile_audit(cells, results, repaired_finding_ids=ValidatedRepairIds(frozenset({"F-2"})))
    assert decision.released is False
    assert decision.verdicts[0].blocking_finding_ids == ("F-1",)


def test_an_invalid_repair_lineage_raises_and_resolves_nothing():
    # a "repair" that produces no new artifact (output digest == input) is not a real repair
    with pytest.raises(GraphValidationError):
        resolve_by_repair(_artifact("F-1"), _repair(("F-1",), out="a"))


def test_reports_blocking_finding_ids():
    cells = (_cell("security"),)
    results = (_result("security", "a1", "p1", _finding("S1", "open", finding_id="F-9")),)
    assert reconcile_audit(cells, results).verdicts[0].blocking_finding_ids == ("F-9",)


def test_consistent_with_validate_audit_coverage():
    cells = (_cell("security"), _cell("correctness"))
    ok = (_result("security", "a1", "p1"), _result("correctness", "a2", "p1"))
    assert reconcile_audit(cells, ok).released is True
    validate_audit_coverage(cells, ok)  # must not raise when reconcile clears the release

    missing = (_result("security", "a1", "p1"),)
    assert reconcile_audit(cells, missing).released is False
    with pytest.raises(GraphValidationError):
        validate_audit_coverage(cells, missing)  # must raise when reconcile blocks on coverage
