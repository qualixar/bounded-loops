"""Ground truth is recordable, separate from the gate's verdict, and yields the false-accept rate.

Nothing in the runtime could express "the gate said SUCCEEDED and it was wrong" before this.
Without it the gate's false-accept rate — the quantity the gate exists to hold down — is not
measurable at any volume of runs, retroactively or otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.label_outcome import OutcomeLabel, label_node_outcome
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent

_DIGEST = "sha256:" + "e" * 64


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="graph-run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
    )


def _running_log(tmp_path: Path) -> GraphEventLog:
    """A minimal RUNNING stream — additive events require one."""
    log = GraphEventLog(tmp_path / "events.jsonl", _identity())
    head = log.replay_projection().head_hash
    for key, event_type, state in (
        ("created", "run.created", "PENDING"), ("started", "run.started", "RUNNING"),
    ):
        stored = log.append(head, UnsignedGraphEvent(
            event_id=key, idempotency_key=key, event_type=event_type,
            timestamp="2026-08-12T00:00:00Z", actor="test", payload={"state": state},
        ))
        head = stored.event_hash
    return log


def _labels(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [
        json.loads(line)["payload"] for line in lines
        if line.strip() and json.loads(line)["event_type"] == "node.outcome.labeled"
    ]


def _label(log: GraphEventLog, **kwargs: object) -> None:
    defaults: dict[str, object] = {
        "node_id": "probe", "attempt": 1, "label": OutcomeLabel.INCORRECT,
        "labeller": "reviewer-a", "artifact_digest": _DIGEST,
        "timestamp": "2026-08-12T01:00:00Z",
    }
    label_node_outcome(log, **{**defaults, **kwargs})  # type: ignore[arg-type]


def test_a_label_records_what_was_actually_true(tmp_path: Path) -> None:
    log = _running_log(tmp_path)

    _label(log)

    recorded = _labels(tmp_path)
    assert len(recorded) == 1
    assert recorded[0]["label"] == "incorrect"
    assert recorded[0]["labeller"] == "reviewer-a"
    # Bound to the exact content judged, so a label cannot drift onto a different output.
    assert recorded[0]["artifact_digest"] == _DIGEST
    # The chain still verifies with the label appended.
    assert log.replay_projection().state == "RUNNING"


def test_a_second_opinion_is_recorded_rather_than_de_duplicated(tmp_path: Path) -> None:
    """Two labellers disagreeing is evidence; collapsing them to one would destroy it."""
    log = _running_log(tmp_path)

    _label(log, labeller="reviewer-a", label=OutcomeLabel.INCORRECT, sequence=1)
    _label(log, labeller="reviewer-b", label=OutcomeLabel.CORRECT, sequence=2)

    recorded = _labels(tmp_path)
    assert [entry["labeller"] for entry in recorded] == ["reviewer-a", "reviewer-b"]
    assert [entry["label"] for entry in recorded] == ["incorrect", "correct"]


def test_a_label_is_not_a_gate_verdict(tmp_path: Path) -> None:
    """The two must be structurally unmistakable, or the gate's own error rate is circular.

    Gate verdicts live under ``verdict`` on lifecycle receipts. A label carries no verdict
    key at all, so a reader counting gate rejections by verdict presence can never pick up a
    reviewer's opinion instead.
    """
    log = _running_log(tmp_path)

    _label(log)

    assert all("verdict" not in entry for entry in _labels(tmp_path))


def test_the_false_accept_rate_is_computable_from_the_log(tmp_path: Path) -> None:
    """The measurement this channel exists for: accepted outputs that were actually wrong."""
    log = _running_log(tmp_path)

    # Three attempts the gate accepted; two were in fact wrong.
    for index, label in enumerate(
        (OutcomeLabel.INCORRECT, OutcomeLabel.CORRECT, OutcomeLabel.INCORRECT), start=1
    ):
        _label(log, node_id=f"node-{index}", label=label)

    recorded = _labels(tmp_path)
    accepted_and_labelled = [e for e in recorded if e["label"] in ("correct", "incorrect")]
    false_accepts = [e for e in accepted_and_labelled if e["label"] == "incorrect"]

    assert len(accepted_and_labelled) == 3
    assert len(false_accepts) == 2
    assert len(false_accepts) / len(accepted_and_labelled) == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_id", ""), ("attempt", 0), ("attempt", True), ("labeller", ""), ("sequence", 0),
    ],
)
def test_a_malformed_label_is_refused(tmp_path: Path, field: str, value: object) -> None:
    log = _running_log(tmp_path)

    with pytest.raises(GraphIntegrityError):
        _label(log, **{field: value})


def test_a_label_must_name_a_real_artifact_digest(tmp_path: Path) -> None:
    """An unbound label could be read as judging output the reviewer never saw."""
    log = _running_log(tmp_path)

    with pytest.raises(GraphIntegrityError, match="digest"):
        _label(log, artifact_digest="not-a-digest")
