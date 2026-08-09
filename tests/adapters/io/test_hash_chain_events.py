from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from bounded_loops.adapters.io.hash_chain_events import (
    HashChainEventStore,
    LoopAttemptState,
)
from bounded_loops.domain.errors import EvidenceError


def test_hash_chain_replays_a_canonical_append_only_run(tmp_path):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")

    created = store.append("run.created", {"node_id": "n1"}, idempotency_key="create")
    started = store.append("node.started", {"attempt": 1}, idempotency_key="start:n1:1")

    replayed = store.replay()
    assert created.sequence == 1
    assert started.sequence == 2
    assert started.previous_hash == created.event_hash
    assert [event.event_type for event in replayed] == ["run.created", "node.started"]


def test_hash_chain_serializes_concurrent_appends_with_a_controller_lock(tmp_path):
    path = tmp_path / "events.jsonl"

    def append(index: int) -> int:
        store = HashChainEventStore(path, run_id="run-1")
        return store.append(
            "run.created", {"node_id": f"n{index}"}, idempotency_key=f"create:{index}",
        ).sequence

    with ThreadPoolExecutor(max_workers=2) as executor:
        sequences = sorted(executor.map(append, range(2)))

    assert sequences == [1, 2]
    assert (tmp_path / "events.jsonl.lock").is_file()
    assert [event.sequence for event in HashChainEventStore(path, run_id="run-1").replay()] == [1, 2]


def test_hash_chain_rejects_tampered_or_reordered_event_bytes(tmp_path):
    path = tmp_path / "events.jsonl"
    store = HashChainEventStore(path, run_id="run-1")
    store.append("run.created", {"node_id": "n1"}, idempotency_key="create")
    store.append("node.started", {"attempt": 1}, idempotency_key="start:n1:1")
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["attempt"] = 2
    path.write_text(lines[0] + "\n" + json.dumps(tampered) + "\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="hash"):
        store.replay()


def test_hash_chain_rejects_idempotency_key_reuse_with_different_payload(tmp_path):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")
    store.append("run.created", {"node_id": "n1"}, idempotency_key="create")

    with pytest.raises(EvidenceError, match="idempotency"):
        store.append("run.created", {"node_id": "different"}, idempotency_key="create")


def test_stored_event_detaches_and_recursively_freezes_nested_payload(tmp_path):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")
    payload = {"evidence": {"files": ["a.py"]}}

    event = store.append("run.created", payload, idempotency_key="create")
    payload["evidence"]["files"].append("mutated.py")

    assert event.payload["evidence"]["files"] == ("a.py",)
    with pytest.raises(TypeError):
        event.payload["evidence"]["new"] = "value"  # type: ignore[index]


def test_checkpoint_binds_the_verified_head_and_projection_digest(tmp_path):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")
    event = store.append("run.created", {"node_id": "n1"}, idempotency_key="create")

    checkpoint = store.checkpoint({"status": "STARTING", "node": "n1"})

    assert checkpoint.sequence == event.sequence
    assert store.verify_checkpoint({"status": "STARTING", "node": "n1"}) == checkpoint
    with pytest.raises(EvidenceError, match="projection"):
        store.verify_checkpoint({"status": "RUNNING", "node": "n1"})


def test_retained_checkpoint_detects_deletion_of_a_valid_event(tmp_path):
    path = tmp_path / "events.jsonl"
    store = HashChainEventStore(path, run_id="run-1")
    store.append("run.created", {"node_id": "n1"}, idempotency_key="create")
    store.append("node.started", {"attempt": 1}, idempotency_key="start:n1:1")
    store.checkpoint({"status": "RUNNING", "node": "n1"})
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    path.write_text(first_line + "\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="checkpoint does not match verified event"):
        store.verify_checkpoint({"status": "RUNNING", "node": "n1"})


def test_hash_chain_rejects_a_partial_crash_tail_instead_of_silently_skipping_it(tmp_path):
    path = tmp_path / "events.jsonl"
    store = HashChainEventStore(path, run_id="run-1")
    store.append("run.created", {"node_id": "n1"}, idempotency_key="create")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"sequence":2')

    with pytest.raises(EvidenceError, match="invalid JSON"):
        store.replay()


def test_graph_attempt_projection_recovers_a_wired_attempt_after_a_crash(tmp_path):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")
    store.append(
        "loop.attempt.wired", {"attempt": 1, "node_id": "node-1"},
        idempotency_key="wired:node-1:1",
    )

    projection = store.recover_loop_attempt()

    assert projection.run_id == "run-1"
    assert projection.node_id == "node-1"
    assert projection.attempt == 1
    assert projection.state is LoopAttemptState.WIRED
    assert projection.status is None
    assert projection.reason is None
    assert projection.sequence == 1


def test_graph_attempt_projection_recovers_a_terminal_outcome(tmp_path):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")
    store.append(
        "loop.attempt.wired", {"attempt": 1, "node_id": "node-1"},
        idempotency_key="wired:node-1:1",
    )
    store.append(
        "loop.attempt.terminal",
        {"attempt": 1, "node_id": "node-1", "reason": "gate-passed", "status": "DONE"},
        idempotency_key="terminal:node-1:1",
    )

    projection = store.recover_loop_attempt()

    assert projection.state is LoopAttemptState.TERMINAL
    assert projection.status == "DONE"
    assert projection.reason == "gate-passed"
    assert projection.sequence == 2


@pytest.mark.parametrize(
    ("events", "message"),
    [
        (
            [("loop.attempt.terminal", {"attempt": 1, "node_id": "node-1", "reason": "x", "status": "DONE"})],
            "must begin with loop.attempt.wired",
        ),
        (
            [
                ("loop.attempt.wired", {"attempt": 1, "node_id": "node-1"}),
                ("loop.attempt.terminal", {"attempt": 2, "node_id": "node-1", "reason": "x", "status": "DONE"}),
            ],
            "different attempt",
        ),
        (
            [
                ("loop.attempt.wired", {"attempt": 1, "node_id": "node-1"}),
                ("node.started", {"attempt": 1}),
            ],
            "unexpected graph event",
        ),
    ],
)
def test_graph_attempt_projection_rejects_invalid_event_sequences(tmp_path, events, message):
    store = HashChainEventStore(tmp_path / "events.jsonl", run_id="run-1")
    for sequence, (event_type, payload) in enumerate(events, 1):
        store.append(event_type, payload, idempotency_key=f"event-{sequence}")

    with pytest.raises(EvidenceError, match=message):
        store.recover_loop_attempt()
