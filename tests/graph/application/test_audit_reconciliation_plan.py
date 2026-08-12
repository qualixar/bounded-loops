"""Tests for AuditPlan-aware reconcile_audit extension (LLD 06).

These tests verify:
1. Existing reconcile_audit call-sites still work (backward compat).
2. When plan= is provided, mandatory cells come from plan.mandatory_cells.
3. Evaluator identity (model_id / tool_id) is attached to CellVerdict when a
   matching assignment exists.
4. Fail-closed: an AuditPlan with no mandatory cells cannot be constructed, so
   reconcile_audit can never receive a vacuously-empty plan.
"""

from __future__ import annotations


from bounded_loops.graph.application.audit_reconciliation import (
    reconcile_audit,
)
from bounded_loops.graph.domain.audits import (
    AuditAssignment,
    AuditCell,
    AuditFinding,
    AuditPlan,
    AuditResult,
)

_D = "sha256:" + "a" * 64
_D2 = "sha256:" + "b" * 64


def _assignment(cell: str) -> AuditAssignment:
    return AuditAssignment(
        cell=cell,
        model_id=f"model-{cell}",
        tool_id=f"tool-{cell}",
        version="1.0",
        rubric_digest=_D,
        independence="assessor != producer",
    )


def _plan(*cell_names: str) -> AuditPlan:
    cells = tuple(AuditCell(n, True) for n in cell_names)
    return AuditPlan(
        artifact_digest=_D,
        rubric_digest=_D2,
        mandatory_cells=cells,
        assignments=tuple(_assignment(n) for n in cell_names),
    )


def _result(cell: str, assessor: str = "a1", producer: str = "p1") -> AuditResult:
    return AuditResult(cell=cell, assessor=assessor, producer=producer, finding=None)


# ── Backward-compat: existing call-sites unchanged ───────────────────────────

class TestExistingCallSitesUnchanged:
    def test_cells_only_path_still_works(self):
        cells = (AuditCell("security", True), AuditCell("correctness", True))
        results = (_result("security"), _result("correctness"))
        decision = reconcile_audit(cells, results)
        assert decision.released is True

    def test_no_plan_verdicts_have_none_evaluator_identity(self):
        cells = (AuditCell("security", True),)
        results = (_result("security"),)
        decision = reconcile_audit(cells, results)
        v = decision.verdicts[0]
        assert v.evaluator_model_id is None
        assert v.evaluator_tool_id is None

    def test_fail_closed_no_mandatory_cells_unchanged(self):
        assert reconcile_audit((), ()).released is False

    def test_existing_blocking_logic_preserved(self):
        cells = (AuditCell("security", True),)
        results = (
            AuditResult("security", "a1", "p1",
                        AuditFinding("F-1", "S1", "open")),
        )
        decision = reconcile_audit(cells, results)
        assert decision.released is False
        assert "security" in decision.blocking_cells


# ── Plan-aware path ──────────────────────────────────────────────────────────

class TestPlanAwarePath:
    def test_plan_mandatory_cells_override_cells_param(self):
        """When plan= provided, mandatory cells come from plan, not from cells."""
        plan = _plan("security", "correctness")
        results = (_result("security"), _result("correctness"))
        # Pass empty cells tuple — plan should override
        decision = reconcile_audit((), results, plan=plan)
        assert decision.released is True

    def test_plan_evaluator_identity_attached_to_cell_verdict(self):
        plan = _plan("security")
        results = (_result("security"),)
        decision = reconcile_audit((), results, plan=plan)
        v = decision.verdicts[0]
        assert v.evaluator_model_id == "model-security"
        assert v.evaluator_tool_id == "tool-security"

    def test_cell_without_assignment_has_none_evaluator_identity(self):
        """Non-mandatory cells have no assignment; evaluator identity is None."""
        cells_in_plan = (AuditCell("security", True), AuditCell("style", False))
        plan = AuditPlan(
            artifact_digest=_D, rubric_digest=_D2,
            mandatory_cells=cells_in_plan,
            assignments=(_assignment("security"),),  # style has no assignment
        )
        results = (_result("security"), _result("style"))
        decision = reconcile_audit((), results, plan=plan)
        style_verdict = next(v for v in decision.verdicts if v.cell == "style")
        assert style_verdict.evaluator_model_id is None
        assert style_verdict.evaluator_tool_id is None

    def test_blocking_logic_still_applies_with_plan(self):
        plan = _plan("security")
        results = (
            AuditResult("security", "a1", "p1",
                        AuditFinding("F-2", "S0", "open")),
        )
        decision = reconcile_audit((), results, plan=plan)
        assert decision.released is False
        assert "security" in decision.blocking_cells

    def test_fail_closed_vacuous_pass_still_guarded(self):
        """reconcile_audit's own vacuous-pass guard still triggers when cells
        list is empty and no plan with mandatory cells is supplied."""
        assert reconcile_audit((), (), plan=None).released is False

    def test_plan_missing_result_blocks_release(self):
        plan = _plan("security", "correctness")
        results = (_result("security"),)  # correctness missing
        decision = reconcile_audit((), results, plan=plan)
        assert decision.released is False
        assert "correctness" in decision.blocking_cells

    def test_severity_never_lowered_with_plan(self):
        plan = _plan("security")
        results = (
            AuditResult("security", "a1", "p1",
                        AuditFinding("F-3", "S1", "resolved")),
        )
        decision = reconcile_audit((), results, plan=plan)
        assert decision.released is True
        assert decision.verdicts[0].highest_severity == "S1"

    def test_cells_param_used_as_fallback_when_plan_is_none(self):
        cells = (AuditCell("security", True),)
        results = (_result("security"),)
        decision = reconcile_audit(cells, results, plan=None)
        assert decision.released is True
        assert decision.verdicts[0].evaluator_model_id is None
