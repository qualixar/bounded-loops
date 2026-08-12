from __future__ import annotations

from dataclasses import asdict

import pytest

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.authoring import DataClass, Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError


def _graph() -> dict[str, object]:
    return {
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "research-brief",
        "version": "1.0.0",
        "nodes": [
            {
                "id": "research", "kind": "loop", "inputs": {}, "outputs": {"evidence": "bundle"},
                "budget": {"max_attempts": 2, "max_wallclock_s": 60}, "effects": ["read_only"],
                "isolation": "process_restricted", "connection_slot": "research-model",
                "on_failure": "fail_graph", "loop_package": "sha256:" + "a" * 64,
            },
            {
                "id": "review", "kind": "approval", "inputs": {"evidence": "bundle"}, "outputs": {},
                "budget": {"max_attempts": 1, "max_wallclock_s": 30}, "effects": ["read_only"],
                "isolation": "workspace_only", "on_failure": "await_human", "required_role": "reviewer",
            },
        ],
        "edges": [{"from_node": "research", "from_port": "evidence", "to_node": "review", "to_port": "evidence", "when": None}],
        "connection_slots": [{"id": "research-model", "requires": ["text_generation"], "data_class_max": "internal"}],
        "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
    }


def _snapshot(**overrides: object) -> CompileSnapshot:
    values: dict[str, object] = {
        "policy_digest": "sha256:" + "b" * 64,
        "package_digests": frozenset({"sha256:" + "a" * 64}),
        "connections": (
            {
                "binding_id": "binding-b",
                "slot_id": "research-model",
                "connector_id": "local-codex",
                "connector_version": "1.0.0",
                "connection_id": "conn-2",
                "admission_digest": "sha256:" + "c" * 64,
                "route_policy_digest": "sha256:" + "d" * 64,
                "provider_id": "openai",
                "model_target": "model-a",
                "region": "in",
                "fallback": False,
                "capabilities": frozenset({"text_generation"}),
                "data_class_max": DataClass.INTERNAL,
                "allowed_effects": frozenset({Effect.READ_ONLY}),
                "isolation": IsolationLevel.PROCESS_RESTRICTED,
                "transport": "local_cli",
                "admitted": True,
            },
            {
                "binding_id": "binding-a",
                "slot_id": "research-model",
                "connector_id": "local-codex",
                "connector_version": "1.0.0",
                "connection_id": "conn-1",
                "admission_digest": "sha256:" + "c" * 64,
                "route_policy_digest": "sha256:" + "d" * 64,
                "provider_id": "openai",
                "model_target": "model-a",
                "region": "in",
                "fallback": False,
                "capabilities": frozenset({"text_generation"}),
                "data_class_max": DataClass.INTERNAL,
                "allowed_effects": frozenset({Effect.READ_ONLY}),
                "isolation": IsolationLevel.PROCESS_RESTRICTED,
                "transport": "local_cli",
                "admitted": True,
            },
        ),
    }
    values.update(overrides)
    return CompileSnapshot(**values)  # type: ignore[arg-type]


def test_compiler_resolves_portable_graph_to_deterministic_immutable_plan():
    graph = validate_authoring_graph(_graph())
    first = compile_graph(graph, _snapshot())
    second = compile_graph(graph, _snapshot(connections=tuple(reversed(_snapshot().connections))))

    assert first.plan_id == second.plan_id
    assert first.levels == (("research",), ("review",))
    assert first.nodes[0].binding_id == "binding-a"
    binding = first.connection_bindings[0]
    assert (binding.provider_id, binding.model_target, binding.region, binding.fallback) == ("openai", "model-a", "in", False)
    assert b'"provider_id":"openai"' in first.canonical_json
    assert "sk-live-secret-canary" not in first.canonical_json.decode("utf-8")
    with pytest.raises(TypeError):
        first.nodes[0].budgets["max_attempts"] = 99  # type: ignore[index]


def test_compiler_binds_the_admitted_transport_into_the_immutable_plan():
    candidate = asdict(_snapshot().connections[0])
    candidate["transport"] = "local_cli"

    plan = compile_graph(
        validate_authoring_graph(_graph()),
        _snapshot(connections=(candidate,)),
    )

    assert plan.connection_bindings[0].transport == "local_cli"
    assert b'"transport":"local_cli"' in plan.canonical_json


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (_snapshot(package_digests=frozenset()), "package_unavailable"),
        (_snapshot(connections=()), "no_admitted_connection"),
        (_snapshot(connections=(_snapshot().connections[0],), policy_digest="not-a-digest"), "policy_digest"),
    ],
)
def test_compiler_fails_closed_when_resolution_snapshot_is_invalid(snapshot, code):
    with pytest.raises(GraphValidationError) as raised:
        compile_graph(validate_authoring_graph(_graph()), snapshot)
    assert raised.value.code == code
