"""Tests for LocalAuditStore: content-addressed append-only persistence,
real read-back, and tamper-evident digest verification."""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.adapters.persistence.audit_store import LocalAuditStore
from bounded_loops.graph.domain.audits import (
    AuditAssignment,
    AuditCell,
    AuditFinding,
    AuditPlan,
    AuditResult,
    AuditedArtifact,
    RepairAttempt,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError

_D = "sha256:" + "a" * 64
_D2 = "sha256:" + "b" * 64
_D3 = "sha256:" + "c" * 64
_D4 = "sha256:" + "d" * 64


def _assignment(cell: str = "security") -> AuditAssignment:
    return AuditAssignment(
        cell=cell, model_id=f"model-{cell}", tool_id=f"tool-{cell}",
        version="1.0", rubric_digest=_D, independence="assessor != producer",
    )


def _plan() -> AuditPlan:
    return AuditPlan(
        artifact_digest=_D, rubric_digest=_D2,
        mandatory_cells=(AuditCell("security", True), AuditCell("correctness", True)),
        assignments=(_assignment("security"), _assignment("correctness")),
    )


def _result(cell: str = "security") -> AuditResult:
    return AuditResult(
        cell=cell, assessor="sol", producer="terra",
        finding=AuditFinding("F-1", "S1", "open"),
    )


def _result_clean(cell: str = "security") -> AuditResult:
    return AuditResult(cell=cell, assessor="sol", producer="terra", finding=None)


def _artifact() -> AuditedArtifact:
    return AuditedArtifact(artifact_digest=_D, finding_ids=("F-1", "F-2"))


def _repair() -> RepairAttempt:
    return RepairAttempt(
        repair_id="R-1", input_artifact_digest=_D,
        output_artifact_digest=_D2, addressed_finding_ids=("F-1",),
        regression_evidence_digest=_D3,
    )


# ── AuditPlan round-trip ──────────────────────────────────────────────────────

class TestAuditPlanRoundTrip:
    def test_put_and_load_plan_returns_equal_object(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        plan = _plan()
        digest = store.put_plan(plan)
        assert digest.startswith("sha256:")
        loaded = store.load_plan(digest)
        assert loaded == plan

    def test_put_is_idempotent_same_digest(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        plan = _plan()
        d1 = store.put_plan(plan)
        d2 = store.put_plan(plan)
        assert d1 == d2

    def test_load_nonexistent_plan_raises(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        with pytest.raises(GraphIntegrityError, match="not found"):
            store.load_plan(_D)

    def test_tampered_plan_detected_on_load(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        plan = _plan()
        digest = store.put_plan(plan)
        # Directly corrupt the stored file
        hex_key = digest.removeprefix("sha256:")
        stored_path = tmp_path / "audit_plans" / f"{hex_key}.json"
        raw = json.loads(stored_path.read_text())
        raw["artifact_digest"] = _D2  # tamper
        stored_path.write_text(json.dumps(raw))
        with pytest.raises(GraphIntegrityError, match="tamper"):
            store.load_plan(digest)

    def test_plan_stored_as_canonical_json(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        digest = store.put_plan(_plan())
        hex_key = digest.removeprefix("sha256:")
        stored_path = tmp_path / "audit_plans" / f"{hex_key}.json"
        content = stored_path.read_text()
        # Canonical = sort_keys + no extra whitespace
        parsed = json.loads(content)
        assert "artifact_digest" in parsed


# ── AuditResult round-trip ───────────────────────────────────────────────────

class TestAuditResultRoundTrip:
    def test_put_and_load_result_with_finding(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        result = _result()
        digest = store.put_result(result)
        loaded = store.load_result(digest)
        assert loaded == result

    def test_put_and_load_result_without_finding(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        result = _result_clean()
        digest = store.put_result(result)
        loaded = store.load_result(digest)
        assert loaded == result
        assert loaded.finding is None

    def test_different_results_get_different_digests(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        d1 = store.put_result(_result("security"))
        d2 = store.put_result(_result("correctness"))
        assert d1 != d2

    def test_tampered_result_detected_on_load(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        digest = store.put_result(_result())
        hex_key = digest.removeprefix("sha256:")
        stored_path = tmp_path / "audit_results" / f"{hex_key}.json"
        raw = json.loads(stored_path.read_text())
        raw["cell"] = "privacy"
        stored_path.write_text(json.dumps(raw))
        with pytest.raises(GraphIntegrityError, match="tamper"):
            store.load_result(digest)


# ── AuditedArtifact round-trip ───────────────────────────────────────────────

class TestAuditedArtifactRoundTrip:
    def test_put_and_load_audited_artifact(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        artifact = _artifact()
        digest = store.put_audited_artifact(artifact)
        loaded = store.load_audited_artifact(digest)
        assert loaded == artifact

    def test_finding_ids_are_preserved(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        artifact = AuditedArtifact(artifact_digest=_D, finding_ids=("F-1", "F-2", "F-3"))
        digest = store.put_audited_artifact(artifact)
        loaded = store.load_audited_artifact(digest)
        assert loaded.finding_ids == ("F-1", "F-2", "F-3")

    def test_tampered_artifact_detected(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        digest = store.put_audited_artifact(_artifact())
        hex_key = digest.removeprefix("sha256:")
        stored_path = tmp_path / "audited_artifacts" / f"{hex_key}.json"
        raw = json.loads(stored_path.read_text())
        raw["finding_ids"] = []
        stored_path.write_text(json.dumps(raw))
        with pytest.raises(GraphIntegrityError, match="tamper"):
            store.load_audited_artifact(digest)


# ── RepairAttempt round-trip ─────────────────────────────────────────────────

class TestRepairAttemptRoundTrip:
    def test_put_and_load_repair(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        repair = _repair()
        digest = store.put_repair(repair)
        loaded = store.load_repair(digest)
        assert loaded == repair

    def test_addressed_finding_ids_are_preserved(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        repair = RepairAttempt(
            repair_id="R-2", input_artifact_digest=_D,
            output_artifact_digest=_D2, addressed_finding_ids=("F-1", "F-2"),
            regression_evidence_digest=_D3,
        )
        digest = store.put_repair(repair)
        loaded = store.load_repair(digest)
        assert loaded.addressed_finding_ids == ("F-1", "F-2")

    def test_tampered_repair_detected(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        digest = store.put_repair(_repair())
        hex_key = digest.removeprefix("sha256:")
        stored_path = tmp_path / "repair_attempts" / f"{hex_key}.json"
        raw = json.loads(stored_path.read_text())
        raw["repair_id"] = "R-FORGED"
        stored_path.write_text(json.dumps(raw))
        with pytest.raises(GraphIntegrityError, match="tamper"):
            store.load_repair(digest)

    def test_load_nonexistent_repair_raises(self, tmp_path):
        store = LocalAuditStore(tmp_path)
        with pytest.raises(GraphIntegrityError, match="not found"):
            store.load_repair(_D)


# ── Root safety ───────────────────────────────────────────────────────────────

class TestStoreRootSafety:
    def test_symlink_root_is_rejected(self, tmp_path):
        link = tmp_path / "link"
        link.symlink_to(tmp_path)
        with pytest.raises(GraphIntegrityError, match="symlink"):
            LocalAuditStore(link)


def test_load_rejects_non_hex_digest_path_traversal(tmp_path):
    """A caller-supplied load digest with path-traversal chars is rejected (no path-escape)."""
    store = LocalAuditStore(tmp_path / "store")
    evil = "sha256:" + "../" * 21 + "x"  # 71 chars, contains path traversal
    with pytest.raises(GraphIntegrityError):
        store.load_plan(evil)
