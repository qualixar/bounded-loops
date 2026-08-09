from __future__ import annotations

import json

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
    )


def _event(event_type: str, key: str, payload: dict[str, object]) -> UnsignedGraphEvent:
    return UnsignedGraphEvent(
        event_id=f"event-{key}", idempotency_key=key, event_type=event_type,
        timestamp="2026-08-08T00:00:00Z", actor="controller", payload=payload,
    )


def test_graph_event_log_requires_expected_head_and_replays_closed_projection(tmp_path):
    store = GraphEventLog(tmp_path / "events.jsonl", _identity())
    created = store.append(
        "0" * 64, _event("run.created", "created", {"state": "PENDING"}),
    )
    succeeded = store.append(
        created.event_hash, _event("run.succeeded", "succeeded", {"state": "SUCCEEDED"}),
    )

    assert store.replay_projection().state == "SUCCEEDED"
    assert succeeded.sequence == 2
    with pytest.raises(GraphIntegrityError, match="expected previous hash"):
        store.append("0" * 64, _event("run.created", "other", {"state": "PENDING"}))


def test_graph_event_log_rejects_tampering_foreign_identity_and_invalid_transition(tmp_path):
    path = tmp_path / "events.jsonl"
    store = GraphEventLog(path, _identity())
    store.append("0" * 64, _event("run.created", "created", {"state": "PENDING"}))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["organization_id"] = "other-org"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="foreign identity"):
        store.replay()

    invalid = GraphEventLog(tmp_path / "invalid.jsonl", _identity())
    invalid.append("0" * 64, _event("run.succeeded", "succeeded", {"state": "SUCCEEDED"}))
    with pytest.raises(GraphIntegrityError, match="must begin"):
        invalid.replay_projection()


def test_graph_event_log_rejects_node_transitions_before_run_started(tmp_path):
    store = GraphEventLog(tmp_path / "events.jsonl", _identity())
    created = store.append("0" * 64, _event("run.created", "created", {"state": "PENDING"}))
    store.append(
        created.event_hash,
        _event("node.ready", "node-ready", {"node_id": "research", "state": "READY", "attempt": 1}),
    )

    with pytest.raises(GraphIntegrityError, match="RUNNING"):
        store.replay_projection()
