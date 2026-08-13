"""Edge guards: the tests that should have existed before 0.4.0 shipped ``when`` as a no-op.

The full suite passing unchanged after guards were enforced proves BACKWARD COMPATIBILITY — it
contains zero non-null ``when`` values, which is precisely how a silently-ignored field survived to a
release. Nothing in it exercises a guard. That is what this file is for.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.edge_guards import ACCEPTED_GUARDS, EdgeGuard, parse_guard
from bounded_loops.graph.application.schedule_ready import (
    Admission,
    NodeState,
    derive_ready_nodes,
    derive_skipped_nodes,
    guard_satisfied,
    predecessors_admission,
)
from bounded_loops.graph.application.skip_untaken import untaken_branches
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.errors import GraphValidationError


def _node(node_id: str, kind: str = "research_claim", **extra: object) -> dict[str, object]:
    return {
        "id": node_id, "kind": kind, "inputs": {}, "outputs": {"out": "text"},
        "budget": {"max_attempts": 1, "max_wallclock_s": 1},
        "effects": ["read_only"], "isolation": "workspace_only", **extra,
    }


def _graph(nodes: list[dict[str, object]], edges: list[dict[str, object]]) -> dict[str, object]:
    # ``continue_declared``, not ``fail_closed``: a failure-conditioned edge is unreachable under
    # fail_closed (the run stops at the first failure) and validation refuses it there. These tests
    # exercise the SCHEDULER's semantics, which is the mode where those edges can be admitted.
    # The controller does not yet honour this mode — see the reachability tests in test_run_graph.
    return {
        "api_version": "bounded-loops.dev/graph/v1", "graph_id": "guards", "version": "1.0.0",
        "nodes": nodes, "edges": edges, "connection_slots": [],
        "policies": {"data_class": "public", "fail_mode": "continue_declared"},
    }


def _compile(nodes: list[dict[str, object]], edges: list[dict[str, object]]):
    graph = validate_authoring_graph(_graph(nodes, edges))
    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64, package_digests=frozenset(), connections=(),
    ))


def _chain(guard: str | None, *, extra_nodes: int = 0):
    """``a -> b`` with ``guard`` on the edge, plus an optional ``b -> c`` tail for cascade tests."""
    nodes = [_node("a"), _node("b", inputs={"feed": "text"})]
    edges = [{"from_node": "a", "from_port": "out", "to_node": "b", "to_port": "feed", "when": guard}]
    if extra_nodes:
        nodes.append(_node("c", inputs={"feed": "text"}))
        edges.append(
            {"from_node": "b", "from_port": "out", "to_node": "c", "to_port": "feed", "when": guard}
        )
    return _compile(nodes, edges)


# ── the grammar is total, and refuses what it cannot enforce ──────────────────────────────


def test_a_null_guard_resolves_to_the_default_so_existing_graphs_are_unchanged():
    assert parse_guard(None, pointer="/e") is EdgeGuard.SUCCEEDED


@pytest.mark.parametrize("literal", ACCEPTED_GUARDS)
def test_every_accepted_literal_parses(literal):
    assert parse_guard(literal, pointer="/e").value == literal


@pytest.mark.parametrize("written", ["Failed", " failed ", "SUCCEEDED"])
def test_incidental_case_and_whitespace_are_tolerated(written):
    assert parse_guard(written, pointer="/e") is EdgeGuard(written.strip().lower())


def test_a_data_expression_is_REFUSED_not_ignored_and_the_message_names_the_whole_set():
    # The exact shape a user would reasonably write, and the shape 0.4.0 accepted then discarded.
    with pytest.raises(GraphValidationError) as caught:
        parse_guard("result.status == 'failed'", pointer="/edges/0/when")
    message = str(caught.value)
    assert "/edges/0/when" in message
    for literal in ACCEPTED_GUARDS:
        assert literal in message
    # The author must be told the old behaviour was silent, or they will assume this ever worked.
    assert "IGNORED" in message


def test_a_blank_guard_is_refused_rather_than_treated_as_absent():
    with pytest.raises(GraphValidationError, match="must not be blank"):
        parse_guard("   ", pointer="/e")


def test_validation_refuses_an_unenforceable_guard_on_a_real_graph():
    with pytest.raises(GraphValidationError, match="unknown edge guard"):
        _compile(
            [_node("a"), _node("b", inputs={"feed": "text"})],
            [{"from_node": "a", "from_port": "out", "to_node": "b", "to_port": "feed",
              "when": "always"}],
        )


def test_a_valid_guard_survives_compilation_onto_the_plan():
    plan = _chain("failed")
    assert [edge.when for edge in plan.edges] == ["failed"]


# ── a guard is only decidable once its source is terminal ─────────────────────────────────


@pytest.mark.parametrize("running", [NodeState.PENDING, NodeState.RUNNING, NodeState.GATING])
def test_a_guard_on_an_unfinished_source_is_undecided(running):
    assert guard_satisfied("failed", running) is None
    assert predecessors_admission("tool", {}, ((running, "failed"),)) is Admission.BLOCK


@pytest.mark.parametrize(
    ("guard", "state", "expected"),
    [
        ("succeeded", NodeState.SUCCEEDED, True),
        ("succeeded", NodeState.FAILED, False),
        ("failed", NodeState.FAILED, True),
        ("failed", NodeState.SUCCEEDED, False),
        ("skipped", NodeState.SKIPPED, True),
        ("skipped", NodeState.FAILED, False),
        ("terminal", NodeState.SUCCEEDED, True),
        ("terminal", NodeState.FAILED, True),
        ("terminal", NodeState.SKIPPED, True),
    ],
)
def test_each_literal_matches_exactly_the_states_it_names(guard, state, expected):
    assert guard_satisfied(guard, state) is expected


@pytest.mark.parametrize("halt", [NodeState.HALTED, NodeState.CANCELLED, NodeState.EXPIRED])
def test_a_run_level_stop_satisfies_neither_succeeded_nor_failed(halt):
    """A fail-closed halt, an operator cancel or a deadline must NOT fire a recovery path."""
    assert guard_satisfied("succeeded", halt) is False
    assert guard_satisfied("failed", halt) is False
    assert guard_satisfied("terminal", halt) is True


# ── guards filter; they never override the decision ───────────────────────────────────────


def test_failure_routing_admits_a_node_whose_upstream_failed():
    plan = _chain("failed")
    assert derive_ready_nodes(plan, {"a": NodeState.FAILED, "b": NodeState.PENDING}) == ("b",)


def test_the_untaken_branch_is_skipped_not_left_pending():
    plan = _chain("failed")
    states = {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING}
    assert derive_ready_nodes(plan, states) == ()
    assert derive_skipped_nodes(plan, states) == ("b",)


def test_an_unguarded_edge_whose_dependency_failed_BLOCKS_and_is_never_skipped():
    """The regression that a naive filter-then-join introduces: a failed dependency must not
    become a green light, and must not be quietly retired as an untaken branch either."""
    plan = _chain(None)
    states = {"a": NodeState.FAILED, "b": NodeState.PENDING}
    assert derive_ready_nodes(plan, states) == ()
    assert derive_skipped_nodes(plan, states) == ()
    assert predecessors_admission("tool", {}, ((NodeState.FAILED, None),)) is Admission.BLOCK


def test_a_mixed_fan_in_does_not_admit_on_one_survivor_while_an_unguarded_parent_failed():
    nodes = [_node("a"), _node("b"), _node("c", inputs={"x": "text", "y": "text"})]
    edges = [
        {"from_node": "a", "from_port": "out", "to_node": "c", "to_port": "x", "when": None},
        {"from_node": "b", "from_port": "out", "to_node": "c", "to_port": "y", "when": "terminal"},
    ]
    plan = _compile(nodes, edges)
    states = {"a": NodeState.FAILED, "b": NodeState.SUCCEEDED, "c": NodeState.PENDING}
    assert derive_ready_nodes(plan, states) == ()


@pytest.mark.parametrize(
    ("mode", "states", "ready"),
    [
        # An unguarded edge must reach the join untouched: all_selected TOLERATES a failed parent
        # and any_successful admits before every parent finishes. A guard that pre-empted either
        # would break both — the second regression this design went through.
        ("all_selected", {"a": NodeState.SUCCEEDED, "b": NodeState.FAILED}, ("join",)),
        ("any_successful", {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING}, ("b", "join")),
        ("all_successful", {"a": NodeState.SUCCEEDED, "b": NodeState.FAILED}, ()),
    ],
)
def test_an_unguarded_edge_reaches_the_join_untouched(mode, states, ready):
    nodes = [
        _node("a"), _node("b"),
        _node("join", kind="join", inputs={"left": "text", "right": "text"}, outputs={},
              mode=mode),
    ]
    edges = [
        {"from_node": "a", "from_port": "out", "to_node": "join", "to_port": "left", "when": None},
        {"from_node": "b", "from_port": "out", "to_node": "join", "to_port": "right", "when": None},
    ]
    plan = _compile(nodes, edges)
    assert derive_ready_nodes(plan, {**states, "join": NodeState.PENDING}) == ready


def test_a_join_whose_every_edge_is_excluded_is_skipped():
    nodes = [
        _node("a"), _node("b"),
        _node("join", kind="join", inputs={"left": "text", "right": "text"}, outputs={},
              mode="any_successful"),
    ]
    edges = [
        {"from_node": "a", "from_port": "out", "to_node": "join", "to_port": "left",
         "when": "failed"},
        {"from_node": "b", "from_port": "out", "to_node": "join", "to_port": "right",
         "when": "failed"},
    ]
    plan = _compile(nodes, edges)
    states = {"a": NodeState.SUCCEEDED, "b": NodeState.SUCCEEDED, "join": NodeState.PENDING}
    assert derive_skipped_nodes(plan, states) == ("join",)


# ── the cascade reaches the end of a branch ───────────────────────────────────────────────


def test_a_skip_cascades_to_the_tail_of_a_multi_node_branch():
    """One pass would strand ``c``: it is only skippable once ``b`` is already SKIPPED."""
    plan = _chain("failed", extra_nodes=1)
    states = {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING, "c": NodeState.PENDING}
    assert derive_skipped_nodes(plan, states) == ("b",)
    cascade = untaken_branches(plan, states)
    assert [node_id for node_id, _ in cascade] == ["b", "c"]


def test_the_cascade_does_not_mutate_the_caller_state_map():
    plan = _chain("failed", extra_nodes=1)
    states = {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING, "c": NodeState.PENDING}
    untaken_branches(plan, states)
    assert states == {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING, "c": NodeState.PENDING}


def test_every_skip_carries_a_reason_naming_the_guard_that_excluded_it():
    plan = _chain("failed")
    cascade = untaken_branches(plan, {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING})
    (_, reason), = cascade
    assert "branch not taken" in reason
    assert "a is SUCCEEDED" in reason
    assert "failed" in reason


# ── the diamond the P4.25a dual audit (Grok) found hanging ────────────────────────────────


def _diamond(merge_kind: str = "research_claim", mode: str | None = None):
    """Grok's exact shape — the commonest conditional graph there is.

        a --when:succeeded--> b --unguarded--> d
        a --when:failed-----> c --unguarded--> d
    """
    merge: dict[str, object] = {"inputs": {"left": "text", "right": "text"}}
    if mode is not None:
        merge["mode"] = mode
    nodes = [
        _node("a"),
        _node("b", inputs={"feed": "text"}),
        _node("c", inputs={"feed": "text"}),
        _node("d", kind=merge_kind, outputs={}, **merge),
    ]
    edges = [
        {"from_node": "a", "from_port": "out", "to_node": "b", "to_port": "feed",
         "when": "succeeded"},
        {"from_node": "a", "from_port": "out", "to_node": "c", "to_port": "feed",
         "when": "failed"},
        {"from_node": "b", "from_port": "out", "to_node": "d", "to_port": "left", "when": None},
        {"from_node": "c", "from_port": "out", "to_node": "d", "to_port": "right", "when": None},
    ]
    return _compile(nodes, edges)


def test_a_diamond_merge_does_not_hang_when_one_branch_was_skipped():
    """Before the fix: ``d`` sat PENDING for ever and the run reported FAILED after the success
    path had finished. A skip must PROPAGATE through an unguarded edge."""
    plan = _diamond()
    states = {
        "a": NodeState.SUCCEEDED, "b": NodeState.SUCCEEDED,
        "c": NodeState.SKIPPED, "d": NodeState.PENDING,
    }
    assert derive_ready_nodes(plan, states) == ()
    assert derive_skipped_nodes(plan, states) == ("d",)


def test_the_diamond_settles_one_snapshot_at_a_time_not_all_at_once():
    """Right after ``a`` succeeds only ``c`` is decidable: ``d`` still has an unguarded edge from a
    PENDING ``b``, which is ordinary waiting, not an untaken branch.

    ``d`` becomes skippable only once ``b`` reaches a terminal state — which is exactly why the
    controller re-runs the cascade on every pass of its loop rather than once per run.
    """
    plan = _diamond()
    states = {
        "a": NodeState.SUCCEEDED, "b": NodeState.PENDING,
        "c": NodeState.PENDING, "d": NodeState.PENDING,
    }
    assert [node for node, _ in untaken_branches(plan, states)] == ["c"]
    assert derive_ready_nodes(plan, states) == ("b",)

    # Second pass, after b succeeded and c was retired — now d is decidable.
    settled = {**states, "b": NodeState.SUCCEEDED, "c": NodeState.SKIPPED}
    assert [node for node, _ in untaken_branches(plan, settled)] == ["d"]


def test_an_all_successful_join_merge_also_settles_rather_than_hanging():
    plan = _diamond(merge_kind="join", mode="all_successful")
    states = {
        "a": NodeState.SUCCEEDED, "b": NodeState.SUCCEEDED,
        "c": NodeState.SKIPPED, "d": NodeState.PENDING,
    }
    assert derive_skipped_nodes(plan, states) == ("d",)


def test_an_any_successful_join_merge_still_RUNS_on_the_taken_branch():
    """The join mode that expresses "either branch is enough" must not be skipped."""
    plan = _diamond(merge_kind="join", mode="any_successful")
    states = {
        "a": NodeState.SUCCEEDED, "b": NodeState.SUCCEEDED,
        "c": NodeState.SKIPPED, "d": NodeState.PENDING,
    }
    assert derive_ready_nodes(plan, states) == ("d",)
    assert derive_skipped_nodes(plan, states) == ()


def test_a_FAILED_unguarded_dependency_still_BLOCKS_and_is_never_propagated_as_a_skip():
    """The distinction the fix rests on. A failure must surface as a failure — converting it into a
    skip would let the run report SUCCEEDED with a broken dependency silently unmet."""
    plan = _diamond()
    states = {
        "a": NodeState.SUCCEEDED, "b": NodeState.FAILED,
        "c": NodeState.SKIPPED, "d": NodeState.PENDING,
    }
    assert derive_ready_nodes(plan, states) == ()
    assert derive_skipped_nodes(plan, states) == ()  # NOT retired — the run must fail


@pytest.mark.parametrize("waiting", [NodeState.RUNNING, NodeState.GATING])
def test_ordinary_waiting_wins_over_both_block_and_skip(waiting):
    """A non-terminal predecessor is just waiting; ``d`` must not be retired early.

    PENDING is deliberately not in this list: a PENDING node whose own guard is already satisfied is
    legitimately READY, so it is not a waiting state for this purpose.
    """
    plan = _diamond()
    states = {
        "a": NodeState.SUCCEEDED, "b": waiting,
        "c": NodeState.SKIPPED, "d": NodeState.PENDING,
    }
    assert derive_ready_nodes(plan, states) == ()
    assert derive_skipped_nodes(plan, states) == ()
