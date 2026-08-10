"""Tests for AuditPlanService: validates coverage, persists via the injected
store port, and loads back faithfully."""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.persistence.audit_store import LocalAuditStore
from bounded_loops.graph.application.audit_plan import AuditPlanService, AuditStorePort
from bounded_loops.graph.domain.audits import (
    AuditAssignment,
    AuditCell,
    AuditPlan,
)
from bounded_loops.graph.domain.errors import GraphValidationError

_D = "sha256:" + "a" * 64
_D2 = "sha256:" + "b" * 64


def _assignment(cell: str) -> AuditAssignment:
    return AuditAssignment(
        cell=cell, model_id=f"m-{cell}", tool_id=f"t-{cell}",
        version="1.0", rubric_digest=_D, independence="assessor != producer",
    )


def _cells(*names: str) -> tuple[AuditCell, ...]:
    return tuple(AuditCell(n, True) for n in names)


def _assignments(*cells: str) -> tuple[AuditAssignment, ...]:
    return tuple(_assignment(c) for c in cells)


# ── Protocol conformance ──────────────────────────────────────────────────────

def test_local_audit_store_satisfies_protocol():
    """LocalAuditStore must satisfy the AuditStorePort Protocol structurally."""
    assert isinstance(None, type(None))  # sanity
    # Runtime protocol check (requires typing.runtime_checkable)
    # AuditStorePort is @runtime_checkable by design — verify structural match
    assert issubclass(LocalAuditStore, AuditStorePort)  # type: ignore[arg-type]


# ── Happy-path: create and load ───────────────────────────────────────────────

class TestAuditPlanServiceHappyPath:
    def test_create_returns_plan_and_digest(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        plan, digest = service.create_plan(
            artifact_digest=_D, rubric_digest=_D2,
            cells=_cells("security", "correctness"),
            assignments=_assignments("security", "correctness"),
        )
        assert isinstance(plan, AuditPlan)
        assert digest.startswith("sha256:")

    def test_load_round_trips_the_same_plan(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        plan, digest = service.create_plan(
            artifact_digest=_D, rubric_digest=_D2,
            cells=_cells("security"),
            assignments=_assignments("security"),
        )
        loaded = service.load_plan(digest)
        assert loaded == plan

    def test_create_is_deterministic(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        _, d1 = service.create_plan(
            artifact_digest=_D, rubric_digest=_D2,
            cells=_cells("security"), assignments=_assignments("security"),
        )
        _, d2 = service.create_plan(
            artifact_digest=_D, rubric_digest=_D2,
            cells=_cells("security"), assignments=_assignments("security"),
        )
        assert d1 == d2  # content-addressed → same digest

    def test_all_six_coverage_cells_accepted(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        cell_names = ("contract", "behaviour", "security", "data_claims", "ux_visual", "operations")
        plan, digest = service.create_plan(
            artifact_digest=_D, rubric_digest=_D2,
            cells=_cells(*cell_names),
            assignments=_assignments(*cell_names),
        )
        assert len(plan.mandatory_cells) == 6
        loaded = service.load_plan(digest)
        assert {c.name for c in loaded.mandatory_cells} == set(cell_names)


# ── Validation failures (fail-closed) ────────────────────────────────────────

class TestAuditPlanServiceValidation:
    def test_empty_mandatory_cells_rejected(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        with pytest.raises(GraphValidationError, match="mandatory"):
            service.create_plan(
                artifact_digest=_D, rubric_digest=_D2,
                cells=(), assignments=(),
            )

    def test_mandatory_cell_without_assignment_rejected(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        with pytest.raises(GraphValidationError, match="no assignment"):
            service.create_plan(
                artifact_digest=_D, rubric_digest=_D2,
                cells=_cells("security", "privacy"),
                assignments=_assignments("security"),  # privacy missing
            )

    def test_duplicate_cell_names_rejected(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        with pytest.raises(GraphValidationError, match="duplicate"):
            service.create_plan(
                artifact_digest=_D, rubric_digest=_D2,
                cells=(AuditCell("security", True), AuditCell("security", True)),
                assignments=_assignments("security"),
            )

    def test_bad_artifact_digest_rejected(self, tmp_path):
        service = AuditPlanService(LocalAuditStore(tmp_path))
        with pytest.raises(GraphValidationError, match="SHA-256"):
            service.create_plan(
                artifact_digest="not-a-digest", rubric_digest=_D2,
                cells=_cells("security"), assignments=_assignments("security"),
            )


# ── Port injection (custom in-memory store) ───────────────────────────────────

class _InMemoryStore:
    """Minimal in-memory AuditStorePort for testing dependency injection."""

    def __init__(self) -> None:
        self._plans: dict[str, AuditPlan] = {}

    def put_plan(self, plan: AuditPlan) -> str:
        import hashlib
        import json
        data = {"artifact_digest": plan.artifact_digest}
        key = "sha256:" + hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
        self._plans[key] = plan
        return key

    def load_plan(self, digest: str) -> AuditPlan:
        if digest not in self._plans:
            from bounded_loops.graph.domain.errors import GraphIntegrityError
            raise GraphIntegrityError("not found")
        return self._plans[digest]


def test_service_accepts_any_store_satisfying_the_protocol(tmp_path):
    store = _InMemoryStore()
    service = AuditPlanService(store)  # type: ignore[arg-type]
    plan, digest = service.create_plan(
        artifact_digest=_D, rubric_digest=_D2,
        cells=_cells("security"), assignments=_assignments("security"),
    )
    loaded = service.load_plan(digest)
    assert loaded == plan
