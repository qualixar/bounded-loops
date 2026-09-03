"""V2 execution-learning producer stays additive and privacy-bounded."""

from __future__ import annotations

from bounded_loops.graph.application.slm_bridge_v2 import execution_evidence_document


def _v1(*, state: str = "SUCCEEDED", demonstration: bool = False, gates=(True,)) -> dict:
    return {
        "contract": "bounded-loops.dev/slm-bridge/v1", "run_state": state,
        "demonstration": demonstration, "run_id": "run-1", "run_ref": "run-1",
        "workspace_id": "sha256:" + "b" * 64, "outcome": state,
        "terminal_at": "2026-09-03T00:00:00Z", "graph_digest": "sha256:" + "c" * 64,
        "plan_digest": "sha256:" + "d" * 64, "policy_digest": "sha256:" + "e" * 64,
        "receipt": {"sequence": 4, "head_digest": "sha256:" + "a" * 64, "trust": "local_hash_chain_only"},
        "nodes": [{"node_id": f"node-{i}", "state": state, "gate_passed": gate, "attempts": 1, "artifact_digests": []} for i, gate in enumerate(gates)],
    }


def test_v2_uses_the_existing_verified_v1_receipt_without_redefining_v1() -> None:
    document = execution_evidence_document(_v1())
    assert document["contract"] == "bounded-loops.dev/slm-bridge/v2"
    assert document["eligible_for_learning"] is True
    assert document["learning_authority"]["scope"] == "execution_reliability_only"
    assert document["learning_authority"] == {
        "scope": "execution_reliability_only", "reason_code": "verified_terminal_receipt",
        "verification_state": "reconciled", "gate_authority": "deterministic_gate",
        "trust_class": "local_hash_chain_only",
    }
    assert set(document) == {
        "contract", "workspace_id", "run_ref", "run_id", "outcome", "run_state",
        "demonstration", "eligible_for_learning", "terminal_at", "graph_digest",
        "plan_digest", "policy_digest", "receipt", "nodes", "learning_authority", "route", "usage",
    }


def test_v2_refuses_demonstrations_and_non_quality_terminal_states() -> None:
    assert execution_evidence_document(_v1(demonstration=True))["eligible_for_learning"] is False
    assert execution_evidence_document(_v1(state="HALTED"))["eligible_for_learning"] is False
    assert execution_evidence_document(_v1(state="CANCELLED"))["eligible_for_learning"] is False


def test_v2_accepts_failed_only_when_a_gate_actually_rejected_execution() -> None:
    assert execution_evidence_document(_v1(state="FAILED", gates=(False,)))["eligible_for_learning"] is True
    assert execution_evidence_document(_v1(state="FAILED", gates=(None,)))["eligible_for_learning"] is False


def test_v2_keeps_the_canonical_node_state_required_by_the_consumer_contract() -> None:
    """Producer tests must run without a sibling SLM checkout or ambient imports."""
    node = execution_evidence_document(_v1())["nodes"][0]
    assert set(node) == {"node_id", "state", "gate_passed", "attempts", "artifact_digests"}
    assert node["state"] == "SUCCEEDED"
