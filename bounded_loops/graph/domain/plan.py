"""Immutable execution-plan values produced from portable graph intent."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from bounded_loops.graph.domain.authoring import Effect, IsolationLevel


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class ResolvedBinding:
    binding_id: str
    slot_id: str
    connector_id: str
    connector_version: str
    connection_id: str
    admission_digest: str
    route_policy_digest: str
    provider_id: str
    model_target: str
    region: str
    fallback: bool
    transport: str


@dataclass(frozen=True)
class PlannedNode:
    node_id: str
    kind: str
    package_digest: str | None
    binding_id: str | None
    required_effects: frozenset[Effect]
    isolation: IsolationLevel
    hard_deadline_ms: int
    budgets: Mapping[str, object]
    approval_policy: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "budgets", _freeze_mapping(self.budgets))
        object.__setattr__(self, "approval_policy", _freeze_mapping(self.approval_policy))


@dataclass(frozen=True)
class PlannedEdge:
    from_node: str
    from_port: str
    to_node: str
    to_port: str
    when: str | None


@dataclass(frozen=True)
class ExecutionPlan:
    api_version: str
    plan_id: str
    source_graph_digest: str
    policy_digest: str
    compiler_version: str
    nodes: tuple[PlannedNode, ...]
    edges: tuple[PlannedEdge, ...]
    levels: tuple[tuple[str, ...], ...]
    package_digests: tuple[str, ...]
    connection_bindings: tuple[ResolvedBinding, ...]
    canonical_json: bytes
