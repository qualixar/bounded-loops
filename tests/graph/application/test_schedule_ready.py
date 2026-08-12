from __future__ import annotations

import pytest

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.schedule_ready import (
    NodeState,
    derive_ready_nodes,
    dispatch_node,
)
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.errors import GraphValidationError


def _plan(join_mode: str | None = None):
    nodes = [
        {"id": "a", "kind": "research_claim", "inputs": {}, "outputs": {"out": "text"}, "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"], "isolation": "workspace_only"},
        {"id": "b", "kind": "research_claim", "inputs": {}, "outputs": {"out": "text"}, "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"], "isolation": "workspace_only"},
    ]
    edges = []
    if join_mode:
        nodes.append({"id": "join", "kind": "join", "inputs": {"left": "text", "right": "text"}, "outputs": {}, "budget": {"max_attempts": 1, "max_wallclock_s": 1}, "effects": ["read_only"], "isolation": "workspace_only", "mode": join_mode})
        edges = [
            {"from_node": "a", "from_port": "out", "to_node": "join", "to_port": "left", "when": None},
            {"from_node": "b", "from_port": "out", "to_node": "join", "to_port": "right", "when": None},
        ]
    graph = validate_authoring_graph({"api_version": "bounded-loops.dev/graph/v1", "graph_id": "schedule", "version": "1.0.0", "nodes": nodes, "edges": edges, "connection_slots": [], "policies": {"data_class": "public", "fail_mode": "fail_closed"}})
    return compile_graph(graph, CompileSnapshot(policy_digest="sha256:" + "a" * 64, package_digests=frozenset(), connections=()))


def test_scheduler_derives_stable_ready_nodes_and_refuses_duplicate_dispatch():
    plan = _plan()
    states = {"b": NodeState.PENDING, "a": NodeState.PENDING}

    assert derive_ready_nodes(plan, states) == ("a", "b")
    dispatched = dispatch_node({**states, "a": NodeState.READY}, "a")
    assert dispatched["a"] is NodeState.STARTING
    with pytest.raises(GraphValidationError, match="READY"):
        dispatch_node(dispatched, "a")


@pytest.mark.parametrize(
    ("mode", "states", "expected"),
    [
        ("all_selected", {"a": NodeState.SUCCEEDED, "b": NodeState.SKIPPED, "join": NodeState.PENDING}, ("join",)),
        ("all_successful", {"a": NodeState.SUCCEEDED, "b": NodeState.SKIPPED, "join": NodeState.PENDING}, ()),
        ("any_successful", {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING, "join": NodeState.PENDING}, ("b", "join")),
    ],
)
def test_scheduler_applies_declared_join_semantics(mode, states, expected):
    assert derive_ready_nodes(_plan(mode), states) == expected
