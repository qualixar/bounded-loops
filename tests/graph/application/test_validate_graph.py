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
                # max_attempts is 1 and on_failure is fail_graph throughout this fixture
                # because those are the only values the runtime actually routes; the
                # validator now refuses the rest rather than accepting and ignoring them.
                "budget": {"max_attempts": 1, "max_wallclock_s": 60},
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
                "on_failure": "fail_graph",
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
        # on_failure_unimplemented: a value the SCHEMA declares but the RUNTIME does not
        # route.  GraphRunController sends every failure to fail_graph, so accepting
        # these would return a plan whose declared failure policy is silently discarded.
        # Refusing is the same fail-closed rule this project applies to its connectors.
        (
            lambda g: g["nodes"][0].update({"on_failure": "repair"}),
            "on_failure_unimplemented",
        ),
        (
            lambda g: g["nodes"][0].update({"on_failure": "continue"}),
            "on_failure_unimplemented",
        ),
        (
            lambda g: g["nodes"][0].update({"on_failure": "await_human"}),
            "on_failure_unimplemented",
        ),
        # Spend caps are no longer refused — the runtime meters them (see
        # test_node_spend.py). What IS still refused is a cap below its floor: a node that
        # may not use one single token cannot do anything, so that is a mis-authored graph
        # rather than a policy. A cost cap of 0 is meaningful ("must not cost money") and is
        # accepted, which test_a_spend_cap_is_now_authorable pins.
        (
            lambda g: g["nodes"][0]["budget"].update({"max_tokens": 0}),
            "range",
        ),
        # max_attempts: above the ceiling the controller enforces.  Narrowed from 1000
        # because the retry budget multiplies the gate's per-attempt false-accept
        # probability, so an over-large budget erodes the gate's own guarantee.
        (
            lambda g: g["nodes"][0]["budget"].update({"max_attempts": 101}),
            "range",
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


@pytest.mark.parametrize(
    ("budget", "expected_tokens", "expected_cost"),
    [
        ({"max_tokens": 50_000}, 50_000, None),
        ({"max_cost_microunits": 1_000_000}, None, 1_000_000),
        ({"max_tokens": 1, "max_cost_microunits": 0}, 1, 0),
    ],
)
def test_a_spend_cap_is_now_authorable(budget, expected_tokens, expected_cost):
    """These fields were refused outright ("no component meters it") until spend landed.

    A cost cap of 0 is a real declaration — "this node must not cost money" — so it is
    accepted, not treated as a missing value. The runtime honours it by permitting free work
    and refusing the first attempt that charges anything.
    """
    graph = _graph()
    graph["nodes"][0]["budget"].update(budget)

    spec = validate_authoring_graph(graph)

    assert spec.nodes[0].budget.max_tokens == expected_tokens
    assert spec.nodes[0].budget.max_cost_microunits == expected_cost


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        # The false positive that made max_tokens unauthorable: _SECRET_WORDS is matched as
        # a substring, and the counting vocabulary of an LLM orchestrator is full of it.
        ("max_tokens", 50_000, True),
        ("token_limit", 4_096, True),
        ("secret_count", 3, True),
        ("cost_per_token", 1.5, True),
        # No integer is an API key, but everything else under a secret-shaped name still is
        # one as far as this check is concerned.
        ("api_key", "sk-live-not-a-real-key", False),
        ("auth_token", "ghp_not-a-real-token", False),
        ("tokens", ["one", "two"], False),
        ("credential", {"nested": "value"}, False),
        # bool is an int subclass; a flag named `secret` is worth looking at, not a count.
        ("password", True, False),
    ],
)
def test_the_secret_shaped_field_check_keys_on_the_value_not_only_the_name(
    field, value, accepted,
):
    graph = _graph()
    # ``presentation`` is the graph's open sub-mapping, so an arbitrary key here reaches the
    # secret check without first hitting the closed allowed-set on a node.
    graph["presentation"] = {field: value}

    if accepted:
        assert validate_authoring_graph(graph).presentation[field] == value
        return
    with pytest.raises(GraphValidationError) as raised:
        validate_authoring_graph(graph)
    assert raised.value.code == "secret_field"
