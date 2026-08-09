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
