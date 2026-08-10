"""Tests for additive audit event types in GraphEventLog (LLD 06).

Verifies:
1. Valid audit event payloads are accepted on both append and replay.
2. Malformed payloads are rejected with GraphIntegrityError.
3. Existing node / run events are unaffected (backward compatibility).
4. All four new types: audit.plan.created, audit.result.published,
   repair.attempt.created, release.decision.issued.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent

_DA = "sha256:" + "a" * 64
_DB = "sha256:" + "b" * 64
_DC = "sha256:" + "c" * 64


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="proj-1", run_id="run-1",
        graph_digest=_DA, plan_digest=_DB, policy_digest=_DC,
    )


def _event(event_type: str, key: str, payload: dict) -> UnsignedGraphEvent:
    return UnsignedGraphEvent(
        event_id=f"ev-{key}", idempotency_key=key, event_type=event_type,
        timestamp="2026-08-11T10:00:00Z", actor="controller", payload=payload,
    )


def _running_log(tmp_path):
    """Return (store, head_hash) with run.created + run.started appended."""
    store = GraphEventLog(tmp_path / "events.jsonl", _identity())
    e1 = store.append("0" * 64, _event("run.created", "rc", {"state": "PENDING"}))
    e2 = store.append(e1.event_hash, _event("run.started", "rs", {"state": "RUNNING"}))
    return store, e2.event_hash


# ── audit.plan.created ────────────────────────────────────────────────────────

class TestAuditPlanCreatedEvent:
    def _valid_payload(self) -> dict:
        return {
            "plan_digest": _DA,
            "artifact_digest": _DB,
            "rubric_digest": _DC,
            "cell_count": 6,
        }

    def test_valid_event_appends_and_replays(self, tmp_path):
        store, head = _running_log(tmp_path)
        e = store.append(head, _event("audit.plan.created", "apc", self._valid_payload()))
        assert e.sequence == 3
        assert store.replay_projection().state == "RUNNING"

    def test_missing_plan_digest_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {k: v for k, v in self._valid_payload().items() if k != "plan_digest"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.plan.created", "apc-bad", bad))

    def test_non_sha256_plan_digest_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "plan_digest": "not-a-digest"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.plan.created", "apc-bad2", bad))

    def test_non_positive_cell_count_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "cell_count": 0}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.plan.created", "apc-bad3", bad))

    def test_extra_field_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "unexpected": "field"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.plan.created", "apc-bad4", bad))


# ── audit.result.published ────────────────────────────────────────────────────

class TestAuditResultPublishedEvent:
    def _valid_payload(self) -> dict:
        return {
            "result_digest": _DA,
            "cell": "security",
            "assessor": "sol",
            "producer": "terra",
        }

    def test_valid_event_appends(self, tmp_path):
        store, head = _running_log(tmp_path)
        e = store.append(head, _event("audit.result.published", "arp", self._valid_payload()))
        assert e.sequence == 3

    def test_missing_cell_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {k: v for k, v in self._valid_payload().items() if k != "cell"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.result.published", "arp-bad", bad))

    def test_empty_assessor_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "assessor": ""}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.result.published", "arp-bad2", bad))

    def test_non_sha256_result_digest_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "result_digest": "bad"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("audit.result.published", "arp-bad3", bad))


# ── repair.attempt.created ────────────────────────────────────────────────────

class TestRepairAttemptCreatedEvent:
    def _valid_payload(self) -> dict:
        return {
            "repair_id": "R-1",
            "input_artifact_digest": _DA,
            "output_artifact_digest": _DB,
        }

    def test_valid_event_appends(self, tmp_path):
        store, head = _running_log(tmp_path)
        e = store.append(head, _event("repair.attempt.created", "rac", self._valid_payload()))
        assert e.sequence == 3

    def test_missing_repair_id_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {k: v for k, v in self._valid_payload().items() if k != "repair_id"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("repair.attempt.created", "rac-bad", bad))

    def test_empty_repair_id_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "repair_id": ""}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("repair.attempt.created", "rac-bad2", bad))

    def test_non_sha256_input_digest_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "input_artifact_digest": "bad"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("repair.attempt.created", "rac-bad3", bad))

    def test_non_sha256_output_digest_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "output_artifact_digest": "bad"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("repair.attempt.created", "rac-bad4", bad))


# ── release.decision.issued ───────────────────────────────────────────────────

class TestReleaseDecisionIssuedEvent:
    def _valid_payload(self) -> dict:
        return {
            "released": True,
            "blocking_cells": [],
            "reason": "all mandatory cells covered",
        }

    def test_valid_released_true_event_appends(self, tmp_path):
        store, head = _running_log(tmp_path)
        e = store.append(head, _event("release.decision.issued", "rdi", self._valid_payload()))
        assert e.sequence == 3

    def test_valid_released_false_event_appends(self, tmp_path):
        store, head = _running_log(tmp_path)
        payload = {"released": False, "blocking_cells": ["security"], "reason": "blocked"}
        e = store.append(head, _event("release.decision.issued", "rdi2", payload))
        assert e.sequence == 3

    def test_released_must_be_bool_not_string(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "released": "true"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("release.decision.issued", "rdi-bad", bad))

    def test_empty_reason_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "reason": ""}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("release.decision.issued", "rdi-bad2", bad))

    def test_missing_blocking_cells_rejected(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {k: v for k, v in self._valid_payload().items() if k != "blocking_cells"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("release.decision.issued", "rdi-bad3", bad))

    def test_blocking_cells_must_be_list(self, tmp_path):
        store, head = _running_log(tmp_path)
        bad = {**self._valid_payload(), "blocking_cells": "security"}
        with pytest.raises(GraphIntegrityError):
            store.append(head, _event("release.decision.issued", "rdi-bad4", bad))


# ── Cross-cutting: replay validation mirrors append ───────────────────────────

class TestReplayValidation:
    def test_valid_audit_event_survives_replay(self, tmp_path):
        """An audit event written and replayed is accepted, not rejected."""
        store, head = _running_log(tmp_path)
        payload = {
            "plan_digest": _DA, "artifact_digest": _DB, "rubric_digest": _DC,
            "cell_count": 2,
        }
        store.append(head, _event("audit.plan.created", "apc-ok", payload))
        events = store.replay()
        assert len(events) == 3

    def test_audit_events_do_not_change_projection_state(self, tmp_path):
        store, head = _running_log(tmp_path)
        payload = {
            "result_digest": _DA, "cell": "security",
            "assessor": "sol", "producer": "terra",
        }
        store.append(head, _event("audit.result.published", "arp-ok", payload))
        assert store.replay_projection().state == "RUNNING"


# ── Backward compat: existing event types still work ─────────────────────────

class TestBackwardCompatibility:
    def test_run_lifecycle_events_unaffected(self, tmp_path):
        store = GraphEventLog(tmp_path / "lifecycle.jsonl", _identity())
        e1 = store.append("0" * 64, _event("run.created", "rc", {"state": "PENDING"}))
        e2 = store.append(e1.event_hash, _event("run.started", "rs", {"state": "RUNNING"}))
        e3 = store.append(e2.event_hash, _event("run.succeeded", "rsu", {"state": "SUCCEEDED"}))
        assert store.replay_projection().state == "SUCCEEDED"
        assert e3.sequence == 3

    def test_node_succeeded_event_unaffected(self, tmp_path):
        store, head = _running_log(tmp_path)
        payload = {
            "node_id": "probe", "state": "SUCCEEDED", "attempt": 1,
            "artifact_digests": [_DA],
        }
        e = store.append(head, _event("node.succeeded", "ns", payload))
        assert e.sequence == 3
        assert store.replay_projection().state == "RUNNING"

    def test_audit_event_before_run_started_is_blocked(self, tmp_path):
        """Audit events require RUNNING state — they must not fire before run.started."""
        store = GraphEventLog(tmp_path / "early.jsonl", _identity())
        e1 = store.append("0" * 64, _event("run.created", "rc", {"state": "PENDING"}))
        payload = {
            "plan_digest": _DA, "artifact_digest": _DB, "rubric_digest": _DC,
            "cell_count": 1,
        }
        store.append(e1.event_hash, _event("audit.plan.created", "apc-early", payload))
        with pytest.raises(GraphIntegrityError, match="RUNNING"):
            store.replay_projection()
