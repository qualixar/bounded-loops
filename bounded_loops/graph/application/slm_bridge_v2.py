"""Additive v2 producer metadata for verified bounded-loop execution evidence.

v1 remains observation-only. v2 contains no prompts, paths, commands, gate
prose, artifacts, or free text; the consumer validates immutable receipts and
decides whether to derive reversible execution-reliability learning.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONTRACT_ID = "bounded-loops.dev/slm-bridge/v2"
CONTRACT_TOOL = "bl_graph_execution_evidence"
CONTRACT_OPERATION = "observe_verified_terminal_run"


def v2_contract_advertisement() -> dict[str, str]:
    return {"id": CONTRACT_ID, "tool": CONTRACT_TOOL, "operation": CONTRACT_OPERATION}


def execution_evidence_document(v1_document: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a v2 execution-learning signal from already validated v1 evidence.

    This function trusts no worker claim. Its caller must first use the v1
    producer path, which reconstructs the graph plan and replays the hashed
    receipt chain. The result retains only fixed-vocabulary execution metadata.
    SLM independently revalidates the immutable source receipt before deriving
    any learning, so this is a producer eligibility statement, not authority.
    """
    if v1_document.get("contract") != "bounded-loops.dev/slm-bridge/v1":
        raise ValueError("v2 requires a v1 bridge document from this producer")
    state = v1_document.get("run_state")
    demonstration = v1_document.get("demonstration") is True
    gates = [node.get("gate_passed") for node in v1_document.get("nodes", ()) if isinstance(node, Mapping)]
    if demonstration:
        eligible, reason = False, "demonstration"
    elif state == "SUCCEEDED" and gates and all(gate is True for gate in gates):
        eligible, reason = True, "verified_gate_success"
    elif state == "FAILED" and any(gate is False for gate in gates):
        eligible, reason = True, "verified_gate_rejection"
    elif state in {"HALTED", "EXPIRED"}:
        eligible, reason = False, "bound_or_policy_stop"
    elif state == "CANCELLED":
        eligible, reason = False, "cancelled"
    else:
        eligible, reason = False, "no_verified_quality_signal"
    receipt = v1_document.get("receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("v2 requires a v1 receipt")
    # Keep the v2 wire shape deliberately exact. The SLM store rejects unknown
    # fields, so producer-local organization/project details cannot accidentally
    # become a cross-product schema extension.
    fields = (
        "workspace_id", "run_ref", "run_id", "outcome", "run_state", "demonstration",
        "terminal_at", "graph_digest", "plan_digest", "policy_digest", "receipt", "nodes",
    )
    document = {field: v1_document.get(field) for field in fields}
    document.update({
        "contract": CONTRACT_ID,
        "eligible_for_learning": eligible,
        "learning_authority": {
            "scope": "execution_reliability_only",
            "reason_code": "verified_terminal_receipt" if eligible else reason,
            "verification_state": "reconciled",
            "gate_authority": "deterministic_gate",
            "trust_class": receipt.get("trust"),
        },
        # Route and usage carry only values this producer can measure. Unknown
        # route dimensions are represented by absence, never guessed labels.
        "route": {},
        "usage": {"attempts": sum(node.get("attempts", 0) for node in v1_document.get("nodes", ()) if isinstance(node, Mapping))},
    })
    return document
