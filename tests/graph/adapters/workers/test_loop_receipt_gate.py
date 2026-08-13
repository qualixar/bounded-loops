"""The loop node's outer gate verifies EVIDENCE; it never re-runs the loop's own gate."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from bounded_loops.graph.adapters.workers.loop_receipt_gate import LoopReceiptGate
from bounded_loops.graph.application.node_contracts import WorkerResult

ORG = "org-1"
PROJECT = "proj-1"
PACKAGE_DIGEST = "a" * 64


@dataclass(frozen=True)
class _Node:
    node_id: str = "validate"
    package_digest: str | None = PACKAGE_DIGEST


class _Handle:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _Store:
    """Minimal ArtifactReaderPort. Records reads so a test can prove the gate re-read the store."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads
        self.reads: list[str] = []

    @contextmanager
    def open(self, ref, access):  # noqa: ANN001 - port shape, not under test here
        self.reads.append(ref.digest)
        if ref.digest not in self._payloads:
            raise FileNotFoundError(ref.digest)
        yield _Handle(self._payloads[ref.digest])


def _outcome(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "status": "DONE",
        "reason": "gate-passed",
        "package_digest": PACKAGE_DIGEST,
        "node_id": "validate",
        "attempt": 1,
        "repair_round": 0,
        "inner_run_id": "run-1.validate.r0.a1.deadbeefdeadbeef",
        "inner_ledger_digest": "b" * 64,
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def _gate(store: _Store, *, repair_round: int = 0) -> LoopReceiptGate:
    return LoopReceiptGate(
        store, organization_id=ORG, project_id=PROJECT, repair_round=repair_round,
    )


def _evaluate(gate: LoopReceiptGate, digest: str = "d1", node: _Node | None = None):
    return gate.evaluate(
        plan=None, node=node or _Node(),
        result=WorkerResult(output_artifact_digests=(digest,)),
    )


def test_a_done_outcome_passes_and_binds_the_evidence_digest():
    store = _Store({"d1": _outcome()})

    verdict = _evaluate(_gate(store))

    assert verdict.passed
    assert "DONE" in verdict.reason
    # The verdict names the artifact it read, so the receipt is tamper-evident rather than
    # merely human-readable.
    assert verdict.evidence_digest == "d1"
    assert store.reads == ["d1"]


@pytest.mark.parametrize("status", ["HALT", "KILLED"])
def test_a_non_converged_loop_is_a_gate_rejection_not_a_worker_fault(status):
    # HALT means the loop ran and its own gate never passed within the bound. Classifying that as a
    # worker fault would have the controller retry a loop that already spent its laps, and would let
    # continue_declared treat an honest "did not converge" as a transient crash.
    store = _Store({"d1": _outcome(status=status, reason="max iterations")})

    verdict = _evaluate(_gate(store))

    assert not verdict.passed
    assert "did not converge" in verdict.reason
    assert verdict.evidence_digest == "d1"


def test_an_unrecognised_status_fails_closed():
    store = _Store({"d1": _outcome(status="PROBABLY_FINE")})

    verdict = _evaluate(_gate(store))

    assert not verdict.passed
    assert "unrecognised status" in verdict.reason


def test_no_artifact_fails_closed():
    verdict = _gate(_Store({})).evaluate(
        plan=None, node=_Node(), result=WorkerResult(output_artifact_digests=()),
    )

    assert not verdict.passed
    assert "no outcome artifact" in verdict.reason


def test_an_unreadable_artifact_fails_closed():
    verdict = _evaluate(_gate(_Store({})), digest="missing")

    assert not verdict.passed
    assert "unreadable" in verdict.reason


@pytest.mark.parametrize(
    "payload", [b"not json at all", b"\xff\xfe binary", b'"a bare string"', b"[1,2,3]"],
)
def test_a_malformed_outcome_fails_closed(payload):
    verdict = _evaluate(_gate(_Store({"d1": payload})))

    assert not verdict.passed
    assert "not valid JSON" in verdict.reason or "not a JSON object" in verdict.reason


# ---------------------------------------------------------------------------
# Provenance. Checked BEFORE status, so a stale or foreign receipt can never be accepted
# just because it happens to say DONE.
# ---------------------------------------------------------------------------


def test_a_receipt_naming_a_different_package_is_refused_even_when_it_says_done():
    store = _Store({"d1": _outcome(package_digest="f" * 64)})

    verdict = _evaluate(_gate(store))

    assert not verdict.passed
    assert "package digest" in verdict.reason


def test_a_receipt_naming_a_different_node_is_refused_even_when_it_says_done():
    store = _Store({"d1": _outcome(node_id="some-other-node")})

    verdict = _evaluate(_gate(store))

    assert not verdict.passed
    assert "names node" in verdict.reason


def test_a_receipt_from_a_previous_repair_round_is_refused():
    # Round 0's artifact says DONE. Gating round 1 against it would let a repair round inherit the
    # original pass without doing any work.
    store = _Store({"d1": _outcome(repair_round=0)})

    verdict = _evaluate(_gate(store, repair_round=1))

    assert not verdict.passed
    assert "repair round" in verdict.reason


def test_a_receipt_from_the_matching_repair_round_passes():
    store = _Store({"d1": _outcome(repair_round=2)})

    verdict = _evaluate(_gate(store, repair_round=2))

    assert verdict.passed


def test_a_receipt_with_no_recorded_round_is_treated_as_round_zero():
    # The bridge omits repair_round at round 0 so pre-repair payloads keep their exact bytes.
    payload = json.loads(_outcome())
    del payload["repair_round"]
    store = _Store({"d1": json.dumps(payload).encode("utf-8")})

    assert _evaluate(_gate(store, repair_round=0)).passed
    assert not _evaluate(_gate(store, repair_round=1)).passed


def test_the_gate_is_a_different_object_from_any_worker():
    # The controller enforces `worker is not gate`. This gate holds only a store, so it has no way
    # to re-execute a producer even if a future edit tried to.
    store = _Store({"d1": _outcome()})
    gate = _gate(store)

    assert not hasattr(gate, "execute")
