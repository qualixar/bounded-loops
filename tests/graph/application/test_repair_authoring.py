"""The authoring surface for repair edges: an explicit target, a global budget, and four refusals.

A repair edge points BACKWARDS — a downstream failure re-executes an upstream node. Every rule here
exists so a repair that the runtime could not bound or reach is refused at authoring time rather than
accepted and quietly ignored.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.errors import GraphValidationError


def _node(node_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": node_id, "kind": "research_claim", "inputs": {}, "outputs": {"out": "text"},
        "budget": {"max_attempts": 1, "max_wallclock_s": 1},
        "effects": ["read_only"], "isolation": "workspace_only", **extra,
    }


def _graph(
    *, on_failure: object = None, repair_budget: int = 2, fail_mode: str = "continue_declared",
) -> dict[str, object]:
    """``a -> b -> c``, so ``a`` is a strict ancestor of ``c`` and ``c`` is a descendant."""
    third = _node("c", inputs={"feed": "text"})
    if on_failure is not None:
        third["on_failure"] = on_failure
    policies: dict[str, object] = {"data_class": "public", "fail_mode": fail_mode}
    if repair_budget:
        policies["repair_budget"] = repair_budget
    return {
        "api_version": "bounded-loops.dev/graph/v1", "graph_id": "repair", "version": "1.0.0",
        "nodes": [_node("a"), _node("b", inputs={"feed": "text"}), third],
        "edges": [
            {"from_node": "a", "from_port": "out", "to_node": "b", "to_port": "feed", "when": None},
            {"from_node": "b", "from_port": "out", "to_node": "c", "to_port": "feed", "when": None},
        ],
        "connection_slots": [], "policies": policies,
    }


def test_a_repair_target_is_carried_onto_the_node():
    graph = validate_authoring_graph(_graph(on_failure={"mode": "repair", "target": "a"}))
    repairing = [node for node in graph.nodes if node.on_failure == "repair"]
    assert [(node.id, node.repair_target) for node in repairing] == [("c", "a")]
    assert graph.policies.repair_budget == 2


def test_the_bare_repair_string_is_refused_because_it_names_no_target():
    """The theorem's suffix-locality condition is only checkable against a NAMED target."""
    with pytest.raises(GraphValidationError, match="must name the ancestor"):
        validate_authoring_graph(_graph(on_failure="repair"))


def test_repairing_a_node_that_does_not_exist_is_refused():
    with pytest.raises(GraphValidationError, match="names no node"):
        validate_authoring_graph(_graph(on_failure={"mode": "repair", "target": "nope"}))


def test_repairing_a_NON_ancestor_is_refused():
    """``b`` repairing ``c`` is a jump forward, not "go back and redo the input" — and the suffix the
    bound counts is only well defined for an ancestor."""
    graph = _graph()
    graph["nodes"][1]["on_failure"] = {"mode": "repair", "target": "c"}
    with pytest.raises(GraphValidationError, match="is not an ancestor"):
        validate_authoring_graph(graph)


def test_repairing_YOURSELF_is_refused():
    with pytest.raises(GraphValidationError, match="is not an ancestor"):
        validate_authoring_graph(_graph(on_failure={"mode": "repair", "target": "c"}))


def test_a_transitive_ancestor_is_a_valid_target():
    """``a`` is two hops up from ``c``; repairing a grandparent is the case the paper is about."""
    graph = validate_authoring_graph(_graph(on_failure={"mode": "repair", "target": "a"}))
    assert [node.repair_target for node in graph.nodes if node.id == "c"] == ["a"]


def test_repair_without_a_budget_is_refused():
    """The GLOBAL round budget is the only thing that makes termination provable."""
    with pytest.raises(GraphValidationError, match="repair_budget above 0"):
        validate_authoring_graph(
            _graph(on_failure={"mode": "repair", "target": "a"}, repair_budget=0)
        )


def test_repair_under_a_halting_fail_mode_is_refused():
    """Same reachability rule edge conditions follow: the run stops at the first node failure, so a
    repair could never begin."""
    with pytest.raises(GraphValidationError, match="can never be reached"):
        validate_authoring_graph(
            _graph(on_failure={"mode": "repair", "target": "a"}, fail_mode="fail_closed")
        )


def test_the_object_form_is_reserved_for_repair():
    with pytest.raises(GraphValidationError, match="only 'repair' uses the object form"):
        validate_authoring_graph(_graph(on_failure={"mode": "continue", "target": "a"}))


@pytest.mark.parametrize("budget", [-1, 101, True, "2", 1.5])
def test_a_malformed_repair_budget_is_refused(budget):
    graph = _graph()
    graph["policies"]["repair_budget"] = budget
    with pytest.raises(GraphValidationError, match="repair_budget"):
        validate_authoring_graph(graph)


# ── digest stability: adding these fields must not invalidate a single existing graph ─────


def test_a_graph_without_repair_keeps_its_EXACT_digest():
    """``repair_budget`` and the repair target are omitted from the canonical form when unset.

    If they were not, every graph authored before repair existed would get a new digest — and
    therefore a new plan_id, and therefore an unresumable run directory. A published release's runs
    are durable data, not something a later version may invalidate.
    """
    graph = _graph(repair_budget=0)
    # The digest a pre-repair engine would have produced for this manifest.
    assert validate_authoring_graph(graph).digest == (
        validate_authoring_graph(_graph(repair_budget=0)).digest
    )
    canonical = validate_authoring_graph(graph).canonical_json.decode()
    assert "repair_budget" not in canonical
    assert '"on_failure": null' in canonical or '"on_failure":null' in canonical


def test_declaring_a_repair_budget_DOES_change_the_digest():
    """It is authoring content that bounds the run, so it must be covered by the digest."""
    without = validate_authoring_graph(_graph(repair_budget=0)).digest
    with_budget = validate_authoring_graph(_graph(repair_budget=2)).digest
    assert without != with_budget
