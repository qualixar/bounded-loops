from __future__ import annotations

import json
import threading

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


def test_concurrent_appends_cannot_fork_the_hash_chain(tmp_path):
    """Two decisions landing at once must not fork the chain (CON-01 regression).

    `append` is a read-check-write cycle. Before the stream lock existed, two concurrent
    writers both observed the SAME head and both wrote an event claiming it; `replay`
    then failed forever with a sequence/previous-hash mismatch and the run could never be
    resumed. This is reachable in normal use — `bl graph approve`, a `bl graph console`
    decision, and a programmatic/MCP `resume` are three entry points into one run dir.

    Each thread builds its OWN log instance so every writer gets a distinct open-file
    description; `flock` therefore serializes them exactly as it would separate processes.
    """
    path = tmp_path / "events.jsonl"
    created = GraphEventLog(path, _identity()).append(
        "0" * 64, _event("run.created", "created", {"state": "PENDING"}),
    )

    barrier = threading.Barrier(2)
    errors: list[BaseException | None] = [None, None]

    def _decide(slot: int) -> None:
        log = GraphEventLog(path, _identity())
        barrier.wait()  # maximize the overlap on the read-check-write cycle
        try:
            log.append(
                created.event_hash,
                _event("run.succeeded", f"decision-{slot}", {"state": "SUCCEEDED"}),
            )
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors[slot] = exc

    threads = [threading.Thread(target=_decide, args=(slot,)) for slot in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Exactly one writer may win; the loser must be TOLD it lost, not silently accepted.
    assert sum(1 for error in errors if error is None) == 1, f"both writers succeeded: {errors}"
    loser = next(error for error in errors if error is not None)
    assert isinstance(loser, GraphIntegrityError), loser
    assert "expected previous hash" in str(loser)

    # The decisive assertion: the stream still verifies, so the run is still recoverable.
    replayed = GraphEventLog(path, _identity()).replay()
    assert len(replayed) == 2
    assert replayed[1].previous_hash == created.event_hash


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


def _running_log(tmp_path):
    store = GraphEventLog(tmp_path / "events.jsonl", _identity())
    created = store.append("0" * 64, _event("run.created", "created", {"state": "PENDING"}))
    started = store.append(created.event_hash, _event("run.started", "started", {"state": "RUNNING"}))
    return store, started.event_hash


def _succeeded_payload(**extra: object) -> dict[str, object]:
    return {
        "node_id": "probe", "state": "SUCCEEDED", "attempt": 1,
        "artifact_digests": ["sha256:" + "a" * 64], **extra,
    }


_FULL_CONTROLS = {
    "net": "enforced", "fs_write": "enforced", "fs_read": "not_enforced",
    "pid": "enforced", "user": "not_enforced", "kernel": "not_enforced", "egress": "not_enforced",
}


def test_node_succeeded_accepts_isolation_receipt(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(isolation={"provider_id": "native", "controls": dict(_FULL_CONTROLS)})
    store.append(head, _event("node.succeeded", "node-ok", payload))
    assert store.replay_projection().state == "RUNNING"  # validates without raising


def test_node_succeeded_rejects_unknown_control_status(tmp_path):
    store, head = _running_log(tmp_path)
    controls = dict(_FULL_CONTROLS)
    controls["net"] = "sometimes"
    payload = _succeeded_payload(isolation={"provider_id": "native", "controls": controls})
    with pytest.raises(GraphIntegrityError, match="isolation"):
        store.append(head, _event("node.succeeded", "node-bad", payload))
        store.replay_projection()


def test_node_succeeded_rejects_incomplete_isolation_matrix(tmp_path):
    """Downgrade-by-omission: a receipt missing a dimension must be rejected, not
    read as 'not_enforced'."""
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(
        isolation={"provider_id": "native", "controls": {"net": "enforced", "fs_write": "enforced"}}
    )
    with pytest.raises(GraphIntegrityError, match="isolation"):
        store.append(head, _event("node.succeeded", "node-partial", payload))
        store.replay_projection()


def test_node_succeeded_rejects_isolation_without_provider(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(isolation={"controls": {"net": "enforced"}})
    with pytest.raises(GraphIntegrityError, match="isolation"):
        store.append(head, _event("node.succeeded", "node-bad2", payload))
        store.replay_projection()


# ── externalized gate verdict (F2 slice 3) ──────────────────────────────────────

def _failed_payload(**extra: object) -> dict[str, object]:
    # ``cause`` is required on a failure receipt: the free-text reason is for humans, and
    # distinguishing a gate rejection from a worker crash by parsing it is how an attempt
    # that never reached the gate ends up in the gate's error denominator.
    return {
        "node_id": "probe", "state": "FAILED", "attempt": 1, "reason": "gate rejected",
        "cause": "gate_rejected", **extra,
    }


def test_node_succeeded_accepts_a_passed_gate_verdict(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(verdict={"passed": True, "reason": "independent gate passed"})
    store.append(head, _event("node.succeeded", "ok", payload))
    assert store.replay_projection().state == "RUNNING"  # validates without raising


def test_node_succeeded_accepts_a_verdict_with_an_evidence_digest(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(
        verdict={"passed": True, "reason": "audited", "evidence_digest": "sha256:" + "e" * 64}
    )
    store.append(head, _event("node.succeeded", "ok2", payload))
    assert store.replay_projection().state == "RUNNING"


def test_node_succeeded_rejects_a_verdict_that_contradicts_the_receipt(tmp_path):
    """A node.succeeded may not carry a failed verdict — the externalized gate
    decision must agree with the terminal state it rides on."""
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(verdict={"passed": False, "reason": "rejected"})
    with pytest.raises(GraphIntegrityError, match="does not match the receipt"):
        store.append(head, _event("node.succeeded", "bad", payload))
        store.replay_projection()


def test_node_failed_accepts_a_failed_gate_verdict(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _failed_payload(verdict={"passed": False, "reason": "independent gate rejected output"})
    store.append(head, _event("node.failed", "fail", payload))
    assert store.replay_projection().state == "RUNNING"


def test_node_failed_rejects_a_passed_verdict(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _failed_payload(verdict={"passed": True, "reason": "passed"})
    with pytest.raises(GraphIntegrityError, match="does not match the receipt"):
        store.append(head, _event("node.failed", "badfail", payload))
        store.replay_projection()


def test_node_verdict_rejects_an_empty_reason(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(verdict={"passed": True, "reason": ""})
    with pytest.raises(GraphIntegrityError, match="verdict requires a non-empty reason"):
        store.append(head, _event("node.succeeded", "emptyreason", payload))
        store.replay_projection()


def test_node_verdict_rejects_a_malformed_evidence_digest(tmp_path):
    store, head = _running_log(tmp_path)
    payload = _succeeded_payload(verdict={"passed": True, "reason": "ok", "evidence_digest": "not-a-digest"})
    with pytest.raises(GraphIntegrityError, match="verdict evidence digest is invalid"):
        store.append(head, _event("node.succeeded", "baddigest", payload))
        store.replay_projection()


def test_append_rejects_a_malformed_node_event_without_persisting_it(tmp_path):
    """A malformed receipt must never become durable: append fails closed BEFORE the
    write, so the log can never be a writable-but-unprojectable wedge."""
    store, head = _running_log(tmp_path)
    before = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    bad = _succeeded_payload(verdict={"passed": False, "reason": "contradiction"})
    with pytest.raises(GraphIntegrityError, match="does not match the receipt"):
        store.append(head, _event("node.succeeded", "bad", bad))
    assert (tmp_path / "events.jsonl").read_text(encoding="utf-8") == before  # not persisted
    assert store.replay_projection().state == "RUNNING"  # log stays projectable


def test_node_succeeded_rejects_a_non_hex_artifact_digest(tmp_path):
    store, head = _running_log(tmp_path)
    payload = {
        "node_id": "probe", "state": "SUCCEEDED", "attempt": 1,
        "artifact_digests": ["sha256:" + "z" * 64],
    }
    with pytest.raises(GraphIntegrityError, match="artifact digests"):
        store.append(head, _event("node.succeeded", "nonhex", payload))
