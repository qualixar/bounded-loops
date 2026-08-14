"""Gap #43 — approval effects substantiveness.

Three properties are verified:

1. ``gated_effects_for_approval`` returns the union of downstream node effects (the fix to the
   construction site).  For the finance-pattern approval that means ``{external_write}`` from
   ``publish-instruction``.

2. An ``ApprovalRequest`` with no ``requested_effects`` is ACCEPTED, because once the derivation is
   correct an empty set truthfully means "this approval gates nothing effect-bearing".

3. A two-node graph (approval -> publish(external_write)) round-trips through
   ``gated_effects_for_approval`` correctly: the approval node's own empty effects do NOT bleed
   into the result, and the publish node's effect DOES appear.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.approvals import request_digest
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.repair_rounds import gated_effects_for_approval
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.approvals import ApprovalRequest
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.errors import GraphValidationError


_POLICY_DIGEST = "sha256:" + "a" * 64
_SNAPSHOT = CompileSnapshot(
    policy_digest=_POLICY_DIGEST, package_digests=frozenset(), connections=()
)


def _digest(char: str) -> str:
    return "sha256:" + char * 64


# ── helpers ───────────────────────────────────────────────────────────────────


def _compile_finance_pattern():
    """Minimal graph matching the finance-payment-assurance approval shape.

    approve-finance (effects=[]) → publish-instruction (effects=[external_write])
    """
    spec = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "finance-approval-test",
        "version": "1.0.0",
        "nodes": [
            {
                "id": "approve-finance",
                "kind": "approval",
                "required_role": "finance-controller",
                "inputs": {"cleared": "internal"},
                "outputs": {"decision": "internal"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 86400},
                "effects": [],
                "isolation": "workspace_only",
            },
            {
                "id": "publish-instruction",
                "kind": "publish",
                "publication_policy": "finance-instruction-v1",
                "inputs": {"decision": "internal"},
                "outputs": {"receipt": "internal"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 300},
                "effects": ["external_write"],
                "isolation": "container_restricted",
            },
        ],
        "edges": [
            {
                "from_node": "approve-finance",
                "from_port": "decision",
                "to_node": "publish-instruction",
                "to_port": "decision",
            },
        ],
        "connection_slots": [],
        "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
    })
    return compile_graph(spec, _SNAPSHOT)


def _valid_request(**overrides: object) -> ApprovalRequest:
    defaults: dict[str, object] = dict(
        approval_id="approval-1",
        organization_id="org-1",
        project_id="project-1",
        graph_digest=_digest("a"),
        plan_digest=_digest("b"),
        node_id="approve-finance",
        attempt=1,
        evidence_digest=_digest("c"),
        requested_effects=frozenset({Effect.EXTERNAL_WRITE}),
        required_role="finance-controller",
        nonce="nonce-1",
        expires_at="2026-08-09T00:00:00Z",
    )
    defaults.update(overrides)
    return ApprovalRequest(**defaults)  # type: ignore[arg-type]


# ── 1. gated_effects_for_approval ─────────────────────────────────────────────


def test_gated_effects_for_approval_finance_pattern_returns_external_write():
    """approve-finance gates publish-instruction(external_write): result must be {external_write}."""
    plan = _compile_finance_pattern()
    effects = gated_effects_for_approval(plan, "approve-finance")
    assert effects == frozenset({Effect.EXTERNAL_WRITE})


def test_gated_effects_excludes_the_approval_node_own_effects():
    """Approval node's own effects (empty for kind:approval) must not bleed into the result."""
    plan = _compile_finance_pattern()
    approval_node = next(n for n in plan.nodes if n.node_id == "approve-finance")
    # Confirm the approval itself has no effects — the whole point is it cannot self-report.
    assert not approval_node.required_effects
    effects = gated_effects_for_approval(plan, "approve-finance")
    assert Effect.EXTERNAL_WRITE in effects


def test_gated_effects_for_terminal_approval_returns_empty():
    """An approval with no downstream nodes returns an empty frozenset, and that is CORRECT.

    Not a defect: it is a plain checkpoint gating nothing irreversible, which real graphs in this
    repo use. The defect was the derivation reading the approval node's OWN (always-empty) effects
    while a downstream publish fired external_write — an empty set that meant "unknown" rather than
    "nothing". Now it means nothing, truthfully.
    """
    spec = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "solo-approval-test",
        "version": "1.0.0",
        "nodes": [
            {
                "id": "gate",
                "kind": "approval",
                "required_role": "reviewer",
                "inputs": {},
                "outputs": {"approved": "text"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 30},
                "effects": [],
                "isolation": "workspace_only",
            },
        ],
        "edges": [],
        "connection_slots": [],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })
    plan = compile_graph(spec, _SNAPSHOT)
    assert gated_effects_for_approval(plan, "gate") == frozenset()


def test_gated_effects_two_approval_chain_reachability():
    """Two approvals in sequence: A gates B and publish. B gates only publish.

    Reachability (over-approximation) is correct here: when A is signed the human is
    told about B's existence and the downstream external_write even though B strictly
    gates publish. Telling the human about MORE effects is the safe direction.
    """
    spec = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "two-approval-chain-test",
        "version": "1.0.0",
        "nodes": [
            {
                "id": "approve-a",
                "kind": "approval",
                "required_role": "manager",
                "inputs": {},
                "outputs": {"ok": "text"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 3600},
                "effects": [],
                "isolation": "workspace_only",
            },
            {
                "id": "approve-b",
                "kind": "approval",
                "required_role": "director",
                "inputs": {"ok": "text"},
                "outputs": {"final": "text"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 3600},
                "effects": [],
                "isolation": "workspace_only",
            },
            {
                "id": "publish-node",
                "kind": "publish",
                "publication_policy": "payment-v1",
                "inputs": {"final": "text"},
                "outputs": {"receipt": "text"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 300},
                "effects": ["external_write"],
                "isolation": "container_restricted",
            },
        ],
        "edges": [
            {"from_node": "approve-a", "from_port": "ok", "to_node": "approve-b", "to_port": "ok"},
            {"from_node": "approve-b", "from_port": "final", "to_node": "publish-node", "to_port": "final"},
        ],
        "connection_slots": [],
        "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
    })
    plan = compile_graph(spec, _SNAPSHOT)

    effects_a = gated_effects_for_approval(plan, "approve-a")
    effects_b = gated_effects_for_approval(plan, "approve-b")

    # A reaches B and publish — sees external_write via publish
    assert Effect.EXTERNAL_WRITE in effects_a
    # B reaches only publish — sees external_write
    assert effects_b == frozenset({Effect.EXTERNAL_WRITE})


# ── 2. _validate_request refuses empty requested_effects ──────────────────────


def test_request_digest_succeeds_with_gated_effects():
    """request_digest accepts a valid ApprovalRequest with at least one gated effect."""
    req = _valid_request()
    digest_val = request_digest(req)
    assert digest_val.startswith("sha256:")


def test_an_approval_that_gates_nothing_effect_bearing_records_an_empty_set():
    """An empty set is ACCEPTED, and that is the correct behaviour once the derivation is right.

    The original defect was not the empty set — it was that ``requested_effects`` came from the
    approval node's OWN effects, which are always empty, so the decision bound to nothing while a
    downstream publish fired ``external_write``. That is fixed at the source by deriving the set from
    every node reachable from the approval.

    With a correct derivation, empty MEANS "this approval gates nothing effect-bearing", which is a
    legitimate shape for a plain checkpoint and one that real graphs in this repo use. Re-adding a
    non-empty guard made the CLI refuse those graphs and broke 19 tests — a guard compensating for a
    wrong derivation should be deleted once the derivation is right, not kept as a second opinion.
    """
    req = _valid_request(requested_effects=frozenset())

    assert request_digest(req)  # accepted, and digestible


def test_request_digest_refuses_invalid_effect_type():
    """Non-Effect string values in requested_effects are also refused."""
    req = _valid_request(requested_effects=frozenset({"not_a_real_effect"}))  # type: ignore[arg-type]
    with pytest.raises(GraphValidationError) as exc_info:
        request_digest(req)
    assert exc_info.value.code == "approval_request"
