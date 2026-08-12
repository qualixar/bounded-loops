"""Tests for AuditAssignment and AuditPlan domain types (LLD 06 / ADR-12)."""

from __future__ import annotations

import pytest

from bounded_loops.graph.domain.audits import (
    AuditAssignment,
    AuditCell,
    AuditPlan,
)
from bounded_loops.graph.domain.errors import GraphValidationError


_D = "sha256:" + "a" * 64
_D2 = "sha256:" + "b" * 64


def _assignment(cell: str = "security") -> AuditAssignment:
    return AuditAssignment(
        cell=cell,
        model_id="gpt-audit-v1",
        tool_id="static-scanner",
        version="1.0.0",
        rubric_digest=_D,
        independence="assessor != producer",
    )


def _plan(cells=None, assignments=None) -> AuditPlan:
    if cells is None:
        cells = (AuditCell("security", mandatory=True),)
    if assignments is None:
        assignments = (_assignment("security"),)
    return AuditPlan(
        artifact_digest=_D,
        rubric_digest=_D2,
        mandatory_cells=cells,
        assignments=assignments,
    )


# ── AuditAssignment ─────────────────────────────────────────────────────────

class TestAuditAssignment:
    def test_valid_assignment_constructs(self):
        a = _assignment()
        assert a.cell == "security"
        assert a.model_id == "gpt-audit-v1"
        assert a.rubric_digest == _D

    def test_empty_cell_rejected(self):
        with pytest.raises(GraphValidationError, match="cell"):
            AuditAssignment(
                cell="", model_id="m", tool_id="t", version="1",
                rubric_digest=_D, independence="assessor != producer",
            )

    def test_empty_model_id_rejected(self):
        with pytest.raises(GraphValidationError, match="model_id"):
            AuditAssignment(
                cell="security", model_id="", tool_id="t", version="1",
                rubric_digest=_D, independence="assessor != producer",
            )

    def test_empty_tool_id_rejected(self):
        with pytest.raises(GraphValidationError, match="tool_id"):
            AuditAssignment(
                cell="security", model_id="m", tool_id="", version="1",
                rubric_digest=_D, independence="assessor != producer",
            )

    def test_empty_version_rejected(self):
        with pytest.raises(GraphValidationError, match="version"):
            AuditAssignment(
                cell="security", model_id="m", tool_id="t", version="",
                rubric_digest=_D, independence="assessor != producer",
            )

    def test_empty_independence_rejected(self):
        with pytest.raises(GraphValidationError, match="independence"):
            AuditAssignment(
                cell="security", model_id="m", tool_id="t", version="1",
                rubric_digest=_D, independence="",
            )

    def test_invalid_rubric_digest_rejected(self):
        with pytest.raises(GraphValidationError, match="SHA-256"):
            AuditAssignment(
                cell="security", model_id="m", tool_id="t", version="1",
                rubric_digest="not-a-digest", independence="assessor != producer",
            )

    def test_rubric_digest_must_be_full_sha256(self):
        with pytest.raises(GraphValidationError):
            AuditAssignment(
                cell="security", model_id="m", tool_id="t", version="1",
                rubric_digest="sha256:abc", independence="assessor != producer",
            )

    def test_frozen(self):
        a = _assignment()
        with pytest.raises(Exception):
            a.cell = "other"  # type: ignore[misc]


# ── AuditPlan ────────────────────────────────────────────────────────────────

class TestAuditPlan:
    def test_valid_plan_constructs(self):
        p = _plan()
        assert p.artifact_digest == _D
        assert len(p.mandatory_cells) == 1
        assert len(p.assignments) == 1

    def test_invalid_artifact_digest_rejected(self):
        with pytest.raises(GraphValidationError, match="SHA-256"):
            AuditPlan(
                artifact_digest="bad", rubric_digest=_D2,
                mandatory_cells=(AuditCell("security", True),),
                assignments=(_assignment(),),
            )

    def test_invalid_rubric_digest_rejected(self):
        with pytest.raises(GraphValidationError, match="SHA-256"):
            AuditPlan(
                artifact_digest=_D, rubric_digest="bad",
                mandatory_cells=(AuditCell("security", True),),
                assignments=(_assignment(),),
            )

    def test_empty_mandatory_cells_is_fail_closed(self):
        """An AuditPlan with no mandatory cells must never construct — vacuous-pass
        hardening mirrors the reconcile_audit gate."""
        with pytest.raises(GraphValidationError, match="mandatory"):
            AuditPlan(
                artifact_digest=_D, rubric_digest=_D2,
                mandatory_cells=(),
                assignments=(_assignment(),),
            )

    def test_duplicate_cell_names_rejected(self):
        cells = (AuditCell("security", True), AuditCell("security", True))
        with pytest.raises(GraphValidationError, match="duplicate"):
            AuditPlan(
                artifact_digest=_D, rubric_digest=_D2,
                mandatory_cells=cells,
                assignments=(_assignment("security"),),
            )

    def test_mandatory_cell_without_assignment_rejected(self):
        cells = (AuditCell("security", True), AuditCell("privacy", True))
        with pytest.raises(GraphValidationError, match="no assignment"):
            AuditPlan(
                artifact_digest=_D, rubric_digest=_D2,
                mandatory_cells=cells,
                assignments=(_assignment("security"),),  # missing privacy
            )

    def test_non_mandatory_cell_without_assignment_is_allowed(self):
        """Non-mandatory cells are sampled; no assignment required."""
        cells = (AuditCell("security", True), AuditCell("style", False))
        # only security has an assignment — style is optional, so this is fine
        p = AuditPlan(
            artifact_digest=_D, rubric_digest=_D2,
            mandatory_cells=cells,
            assignments=(_assignment("security"),),
        )
        assert len(p.mandatory_cells) == 2

    def test_all_six_coverage_cells_are_accepted(self):
        """The six cells from 04-CROSS-MODEL-AUDIT.md all construct without error."""
        cells = tuple(
            AuditCell(name, True)
            for name in ("contract", "behaviour", "security", "data_claims", "ux_visual", "operations")
        )
        assignments = tuple(_assignment(c.name) for c in cells)
        p = AuditPlan(artifact_digest=_D, rubric_digest=_D2,
                      mandatory_cells=cells, assignments=assignments)
        assert {c.name for c in p.mandatory_cells} == {
            "contract", "behaviour", "security", "data_claims", "ux_visual", "operations"
        }

    def test_frozen(self):
        p = _plan()
        with pytest.raises(Exception):
            p.artifact_digest = _D2  # type: ignore[misc]
