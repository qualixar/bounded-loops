from __future__ import annotations

import json

import pytest

from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    validate_authoring_graph,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def _graph() -> dict[str, object]:
    return {
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "research-brief",
        "version": "1.0.0",
        "nodes": [
            {
                "id": "research",
                "kind": "loop",
                "inputs": {"question": "text"},
                "outputs": {"evidence": "evidence_bundle"},
                "budget": {"max_attempts": 2, "max_wallclock_s": 60},
                "effects": ["read_only"],
                "isolation": "process_restricted",
                "connection_slot": "research-model",
                "on_failure": "fail_graph",
                "loop_package": "sha256:" + "a" * 64,
            },
            {
                "id": "review",
                "kind": "approval",
                "inputs": {"evidence": "evidence_bundle"},
                "outputs": {"approved": "boolean"},
                "budget": {"max_attempts": 1, "max_wallclock_s": 30},
                "effects": ["read_only"],
                "isolation": "workspace_only",
                "on_failure": "await_human",
                "required_role": "reviewer",
            },
        ],
        "edges": [
            {
                "from_node": "research",
                "from_port": "evidence",
                "to_node": "review",
                "to_port": "evidence",
                "when": None,
            },
        ],
        "connection_slots": [
            {
                "id": "research-model",
                "requires": ["text_generation", "json_output"],
                "data_class_max": "internal",
                "preferred_modalities": ["text"],
            },
        ],
        "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
        "presentation": {"label": "Research brief"},
    }


def test_valid_authoring_graph_is_immutable_and_deterministically_hashed():
    first = validate_authoring_graph(_graph())
    shuffled = json.loads(json.dumps(_graph(), sort_keys=True))
    second = validate_authoring_graph(shuffled)

    assert first.digest == second.digest
    assert first.nodes[0].inputs["question"] == "text"
    with pytest.raises(TypeError):
        first.nodes[0].inputs["another"] = "text"  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda graph: graph["nodes"][0].update({"unexpected": True}), "unknown_field"),
        (lambda graph: graph["nodes"][1].update({"id": "research"}), "duplicate_node_id"),
        (lambda graph: graph["edges"][0].update({"to_port": "missing"}), "missing_input_port"),
        (lambda graph: graph["nodes"][1]["inputs"].update({"evidence": "text"}), "port_type_mismatch"),
        (lambda graph: graph["nodes"][0].update({"loop_package": "latest"}), "mutable_package_reference"),
        (lambda graph: graph["connection_slots"][0]["requires"].append("openai"), "provider_in_slot"),
        (lambda graph: graph["nodes"][0].update({"tool_ref": "/Users/example/tool"}), "absolute_path"),
    ],
)
def test_authoring_graph_rejects_nonportable_or_invalid_semantics(mutate, code):
    graph = _graph()
    mutate(graph)

    with pytest.raises(GraphValidationError) as raised:
        validate_authoring_graph(graph)
    assert raised.value.code == code


def test_authoring_graph_rejects_cycles_and_duplicate_json_keys():
    graph = _graph()
    graph["nodes"][0]["inputs"]["question"] = "boolean"
    graph["edges"].append(
        {"from_node": "review", "from_port": "approved", "to_node": "research", "to_port": "question", "when": None},
    )
    with pytest.raises(GraphValidationError, match="cycle"):
        validate_authoring_graph(graph)

    duplicate = '{"api_version":"bounded-loops.dev/graph/v1","api_version":"x"}'
    with pytest.raises(GraphValidationError) as raised:
        parse_authoring_graph_json(duplicate)
    assert raised.value.code == "duplicate_key"


# ── TEST-08: uncovered validation error codes ─────────────────────────────────
# The existing parametrized test covers 7 of ~30 error codes. These cover the
# nine codes confirmed missing from coverage. A regression that silently stops
# raising on an invalid input would cause these assertions to fail.

def test_invalid_json_is_rejected():
    """parse_authoring_graph_json raises GraphValidationError with code
    'invalid_json' on syntactically malformed input (line 68 of validate_graph.py)."""
    with pytest.raises(GraphValidationError) as raised:
        parse_authoring_graph_json("{ not valid json }")
    assert raised.value.code == "invalid_json"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        # api_version: wrong API version string (line 86)
        (lambda g: g.update({"api_version": "bounded-loops.dev/graph/v2"}), "api_version"),
        # version: non-semver string like a branch name (line 90)
        (lambda g: g.update({"version": "main"}), "version"),
        # unknown_node_kind: a node kind that is not in the NodeKind enum (line 156)
        (
            lambda g: g["nodes"][0].update({"kind": "oracle"}),
            "unknown_node_kind",
        ),
        # on_failure: invalid on_failure value.
        # validate_graph.py:167 rejects any value not in
        # {"fail_graph", "continue", "repair", "await_human"}.
        # The guard is node-kind-agnostic — it runs for loop, approval, and every
        # other kind inside _validate_node(). nodes[0] is a 'loop' node; this
        # confirms the branch fires for loop nodes (not just approval nodes).
        (
            lambda g: g["nodes"][0].update({"on_failure": "ignore_and_continue"}),
            "on_failure",
        ),
        # edge_condition: edge 'when' that is not a string or null (line 231)
        (
            lambda g: g["edges"][0].update({"when": 42}),
            "edge_condition",
        ),
        # duplicate_slot_id: two connection slots with the same id (line 256)
        (
            lambda g: g["connection_slots"].append(dict(g["connection_slots"][0])),
            "duplicate_slot_id",
        ),
        # fail_mode: invalid fail_mode value in policies (line 266)
        (
            lambda g: g["policies"].update({"fail_mode": "best_effort"}),
            "fail_mode",
        ),
        # audit_profile: non-string required_audit_profile (line 269)
        (
            lambda g: g["policies"].update({"required_audit_profile": 99}),
            "audit_profile",
        ),
    ],
)
def test_additional_graph_validation_error_codes(mutate, code):
    """Regression guard for validation error codes not covered by the original
    parametrized test. Each mutation exercises a specific error-code branch in
    validate_graph.py that was confirmed 0% covered by the audit."""
    graph = _graph()
    mutate(graph)
    with pytest.raises(GraphValidationError) as raised:
        validate_authoring_graph(graph)
    assert raised.value.code == code, (
        f"expected code={code!r}, got code={raised.value.code!r}"
    )
