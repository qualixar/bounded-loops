"""Audit reconciliation → release decision (ADR-12 R1 / LLD 06): preserves highest severity and
dissent, blocks on missing/producer-only/unresolved-S0-S1 mandatory cells, and stays consistent
with the existing validate_audit_coverage gate."""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.audit_reconciliation import reconcile_audit
from bounded_loops.graph.domain.audits import AuditCell, AuditFinding, AuditResult, validate_audit_coverage
from bounded_loops.graph.domain.errors import GraphValidationError


def _cell(name: str, mandatory: bool = True) -> AuditCell:
    return AuditCell(name=name, mandatory=mandatory)


def _finding(severity: str, disposition: str = "open") -> AuditFinding:
    return AuditFinding(severity=severity, disposition=disposition)


def _result(cell: str, assessor: str = "auditor", producer: str = "maker", finding: AuditFinding | None = None) -> AuditResult:
    return AuditResult(cell=cell, assessor=assessor, producer=producer, finding=finding)


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


def test_consistent_with_validate_audit_coverage():
    cells = (_cell("security"), _cell("correctness"))
    ok = (_result("security", "a1", "p1"), _result("correctness", "a2", "p1"))
    assert reconcile_audit(cells, ok).released is True
    validate_audit_coverage(cells, ok)  # must not raise when reconcile clears the release

    missing = (_result("security", "a1", "p1"),)
    assert reconcile_audit(cells, missing).released is False
    with pytest.raises(GraphValidationError):
        validate_audit_coverage(cells, missing)  # must raise when reconcile blocks on coverage
