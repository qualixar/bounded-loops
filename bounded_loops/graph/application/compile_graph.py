"""Pure compiler from portable graph intent to an immutable execution plan."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from enum import Enum
from typing import TypeVar, cast

from bounded_loops.graph.domain.authoring import (
    AuthoringGraphSpec,
    AuthoringNode,
    DataClass,
    Effect,
    IsolationLevel,
    NodeKind,
    PortableBindingSlot,
    canonical_json,
    digest,
)
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import (
    ExecutionPlan,
    PlannedEdge,
    PlannedNode,
    ResolvedBinding,
)


_COMPILER_VERSION = "bounded-loops.graph-compiler/v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATA_RANK = {
    DataClass.PUBLIC: 0,
    DataClass.INTERNAL: 1,
    DataClass.CONFIDENTIAL: 2,
    DataClass.RESTRICTED: 3,
}
_ISOLATION_RANK = {
    IsolationLevel.WORKSPACE_ONLY: 0,
    IsolationLevel.PROCESS_RESTRICTED: 1,
    IsolationLevel.CONTAINER_RESTRICTED: 2,
    IsolationLevel.CUSTOMER_MANAGED_WORKER: 3,
}


@dataclass(frozen=True)
class ConnectionCandidate:
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
    capabilities: frozenset[str]
    data_class_max: DataClass
    allowed_effects: frozenset[Effect]
    isolation: IsolationLevel
    transport: str
    admitted: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ConnectionCandidate:
        forbidden = {key for key in raw if any(word in key.lower() for word in ("secret", "token", "password", "credential"))}
        if forbidden:
            raise GraphValidationError("secret_field", "/connections", "connection snapshot contains secret-shaped data")
        required = {
            "binding_id", "slot_id", "connector_id", "connector_version", "connection_id",
            "admission_digest", "route_policy_digest", "provider_id", "model_target", "region", "fallback", "capabilities", "data_class_max",
            "allowed_effects", "isolation", "transport", "admitted",
        }
        if set(raw) != required:
            raise GraphValidationError("connection_snapshot", "/connections", "connection snapshot has an invalid shape")
        return cls(
            binding_id=_string(raw["binding_id"], "/connections/binding_id"),
            slot_id=_string(raw["slot_id"], "/connections/slot_id"),
            connector_id=_string(raw["connector_id"], "/connections/connector_id"),
            connector_version=_string(raw["connector_version"], "/connections/connector_version"),
            connection_id=_string(raw["connection_id"], "/connections/connection_id"),
            admission_digest=_digest(raw["admission_digest"], "/connections/admission_digest"),
            route_policy_digest=_digest(raw["route_policy_digest"], "/connections/route_policy_digest"),
            provider_id=_string(raw["provider_id"], "/connections/provider_id"),
            model_target=_string(raw["model_target"], "/connections/model_target"),
            region=_string(raw["region"], "/connections/region"),
            fallback=_bool(raw["fallback"], "/connections/fallback"),
            capabilities=_strings(raw["capabilities"], "/connections/capabilities"),
            data_class_max=_enum(DataClass, raw["data_class_max"], "/connections/data_class_max"),
            allowed_effects=frozenset(_enum(Effect, item, "/connections/allowed_effects") for item in _strings(raw["allowed_effects"], "/connections/allowed_effects")),
            isolation=_enum(IsolationLevel, raw["isolation"], "/connections/isolation"),
            transport=_string(raw["transport"], "/connections/transport"),
            admitted=_bool(raw["admitted"], "/connections/admitted"),
        )


@dataclass(frozen=True)
class CompileSnapshot:
    policy_digest: str
    package_digests: frozenset[str]
    connections: tuple[ConnectionCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "package_digests", frozenset(self.package_digests))
        raw_connections = cast(tuple[object, ...], self.connections)
        object.__setattr__(
            self,
            "connections",
            tuple(
                value if isinstance(value, ConnectionCandidate) else _candidate_from_raw(value)
                for value in raw_connections
            ),
        )


def compile_graph(graph: AuthoringGraphSpec, snapshot: CompileSnapshot) -> ExecutionPlan:
    """Compile one validated graph against explicit, non-secret snapshots."""
    policy_digest = _digest(snapshot.policy_digest, "/policy_digest")
    _validate_packages(graph, snapshot.package_digests)
    bindings = _resolve_bindings(graph, snapshot)
    nodes = tuple(
        _planned_node(node, bindings, graph.policies.repair_budget) for node in graph.nodes
    )
    edges = tuple(PlannedEdge(edge.from_node, edge.from_port, edge.to_node, edge.to_port, edge.when) for edge in graph.edges)
    levels = _topological_levels(nodes, edges)
    canonical = _canonical_plan(graph, policy_digest, nodes, edges, levels, bindings)
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1",
        plan_id=digest(canonical),
        source_graph_digest=graph.digest,
        policy_digest=policy_digest,
        compiler_version=_COMPILER_VERSION,
        nodes=nodes,
        edges=edges,
        levels=levels,
        package_digests=tuple(sorted(snapshot.package_digests)),
        connection_bindings=tuple(sorted(bindings.values(), key=lambda binding: binding.binding_id)),
        canonical_json=canonical_json(canonical),
    )


def _validate_packages(graph: AuthoringGraphSpec, packages: frozenset[str]) -> None:
    for package in packages:
        _digest(package, "/package_digests")
    for node in graph.nodes:
        if node.kind not in {NodeKind.LOOP, NodeKind.SUBGRAPH}:
            continue
        key = "loop_package" if node.kind is NodeKind.LOOP else "graph_package"
        package = _string(node.details[key], f"/nodes/{node.id}/{key}")
        if package not in packages:
            raise GraphValidationError("package_unavailable", f"/nodes/{node.id}/{key}", "package digest is not admitted")


def _why_no_candidate(
    graph: AuthoringGraphSpec,
    snapshot: CompileSnapshot,
    node: AuthoringNode,
    slot: PortableBindingSlot,
) -> str:
    """Return a human-readable explanation for why no connection candidate was found.

    Walks the filter chain step-by-step (DX-09) so the user sees exactly which
    constraint eliminated all candidates — rather than the generic "no admitted
    connection satisfies policy" that previously offered no fix hint.
    """
    all_conns = list(snapshot.connections)
    if not all_conns:
        return (
            "no connections were provided — pass a connections.json via --connections "
            "or supply connections programmatically"
        )
    slot_matched = [c for c in all_conns if c.slot_id == slot.id]
    if not slot_matched:
        available = sorted({c.slot_id for c in all_conns})
        return (
            f"no connection targets slot '{slot.id}'; "
            f"available slot_ids in connections.json: {available}"
        )
    admitted_only = [c for c in slot_matched if c.admitted]
    if not admitted_only:
        return f"connection for slot '{slot.id}' exists but is not marked admitted"
    caps_ok = [c for c in admitted_only if slot.requires <= c.capabilities]
    if not caps_ok:
        missing = slot.requires - admitted_only[0].capabilities
        return (
            f"slot '{slot.id}' requires capabilities {sorted(missing)} "
            "that the admitted connection does not advertise"
        )
    data_rank = _DATA_RANK[graph.policies.data_class]
    data_ok = [c for c in caps_ok if data_rank <= _DATA_RANK[c.data_class_max]]
    if not data_ok:
        return (
            f"graph data_class '{graph.policies.data_class.value}' exceeds the "
            f"data_class_max '{caps_ok[0].data_class_max.value}' of the admitted connection for slot '{slot.id}'"
        )
    effects_ok = [c for c in data_ok if node.effects <= c.allowed_effects]
    if not effects_ok:
        extra = node.effects - data_ok[0].allowed_effects
        return (
            f"node '{node.id}' declares effects {sorted(e.value for e in extra)} "
            f"that the admitted connection for slot '{slot.id}' does not permit"
        )
    iso_ok = [
        c for c in effects_ok
        if _ISOLATION_RANK[c.isolation] >= _ISOLATION_RANK[node.isolation]
    ]
    if not iso_ok:
        return (
            f"node '{node.id}' requires isolation '{node.isolation.value}' but the "
            f"admitted connection for slot '{slot.id}' only provides '{effects_ok[0].isolation.value}'"
        )
    # Fallback — all known checks passed; something changed upstream.
    return "no admitted connection satisfies policy (unknown constraint)"


def _resolve_bindings(graph: AuthoringGraphSpec, snapshot: CompileSnapshot) -> dict[str, ResolvedBinding]:
    slots = {slot.id: slot for slot in graph.connection_slots}
    bindings: dict[str, ResolvedBinding] = {}
    for node in graph.nodes:
        if node.connection_slot is None:
            continue
        slot = slots[node.connection_slot]
        candidates = [
            candidate for candidate in snapshot.connections
            if candidate.slot_id == slot.id
            and candidate.admitted
            and slot.requires <= candidate.capabilities
            and _DATA_RANK[graph.policies.data_class] <= _DATA_RANK[candidate.data_class_max]
            and node.effects <= candidate.allowed_effects
            and _ISOLATION_RANK[candidate.isolation] >= _ISOLATION_RANK[node.isolation]
        ]
        if not candidates:
            raise GraphValidationError(
                "no_admitted_connection",
                f"/nodes/{node.id}/connection_slot",
                _why_no_candidate(graph, snapshot, node, slot),
            )
        selected = sorted(candidates, key=lambda candidate: (candidate.binding_id, candidate.connection_id))[0]
        bindings[node.id] = ResolvedBinding(
            binding_id=selected.binding_id,
            slot_id=selected.slot_id,
            connector_id=selected.connector_id,
            connector_version=selected.connector_version,
            connection_id=selected.connection_id,
            admission_digest=selected.admission_digest,
            route_policy_digest=selected.route_policy_digest,
            provider_id=selected.provider_id,
            model_target=selected.model_target,
            region=selected.region,
            fallback=selected.fallback,
            transport=selected.transport,
        )
    return bindings


def _planned_node(
    node: AuthoringNode, bindings: Mapping[str, ResolvedBinding], repair_budget: int = 0,
) -> PlannedNode:
    # Kept separate from validation to preserve compiler purity over a frozen graph.
    package = None
    if node.kind is NodeKind.LOOP:
        package = _string(node.details["loop_package"], f"/nodes/{node.id}/loop_package")
    elif node.kind is NodeKind.SUBGRAPH:
        package = _string(node.details["graph_package"], f"/nodes/{node.id}/graph_package")
    binding = bindings.get(node.id)
    approval = {
        "join_mode": node.details.get("mode"),
        "required": node.kind is NodeKind.APPROVAL,
        "required_role": node.details.get("required_role"),
    }
    # Repair reaches the runtime through the node policy map, because the controller and the replay
    # verifier both need it and neither holds the manifest. Added ONLY when declared, so a graph
    # without repair serialises byte-identically and keeps its plan_id — and therefore keeps every
    # existing run directory resumable.
    if node.repair_target is not None:
        approval["repair_target"] = node.repair_target
    if repair_budget:
        approval["repair_budget"] = repair_budget
    return PlannedNode(
        node_id=node.id,
        kind=node.kind.value,
        package_digest=package,
        binding_id=binding.binding_id if binding else None,
        required_effects=node.effects,
        isolation=node.isolation,
        # PER ATTEMPT, not per node: the workers apply this as a subprocess deadline on each
        # attempt, so a node's total wall time is up to max_attempts * max_wallclock_s.  A
        # node-total wallclock ceiling is a separate budget and does not exist yet.
        hard_deadline_ms=node.budget.max_wallclock_s * 1000,
        budgets={
            "max_attempts": node.budget.max_attempts,
            "max_cost_microunits": node.budget.max_cost_microunits,
            "max_tokens": node.budget.max_tokens,
        },
        approval_policy=approval,
    )


def _topological_levels(nodes: tuple[PlannedNode, ...], edges: tuple[PlannedEdge, ...]) -> tuple[tuple[str, ...], ...]:
    incoming = {node.node_id: 0 for node in nodes}
    children: dict[str, list[str]] = {node.node_id: [] for node in nodes}
    for edge in edges:
        incoming[edge.to_node] += 1
        children[edge.from_node].append(edge.to_node)
    ready = sorted(node_id for node_id, count in incoming.items() if count == 0)
    levels: list[tuple[str, ...]] = []
    while ready:
        level = tuple(ready)
        levels.append(level)
        next_ready: list[str] = []
        for node_id in level:
            for child in sorted(children[node_id]):
                incoming[child] -= 1
                if incoming[child] == 0:
                    next_ready.append(child)
        ready = sorted(next_ready)
    if sum(len(level) for level in levels) != len(nodes):
        raise GraphValidationError("cycle", "/edges", "validated graph unexpectedly contains a cycle")
    return tuple(levels)


def _canonical_plan(graph: AuthoringGraphSpec, policy_digest: str, nodes: tuple[PlannedNode, ...], edges: tuple[PlannedEdge, ...], levels: tuple[tuple[str, ...], ...], bindings: Mapping[str, ResolvedBinding]) -> dict[str, object]:
    return {
        "api_version": "bounded-loops.dev/plan/v1",
        "compiler_version": _COMPILER_VERSION,
        "connection_bindings": [
            {"admission_digest": binding.admission_digest, "binding_id": binding.binding_id, "connection_id": binding.connection_id, "connector_id": binding.connector_id, "connector_version": binding.connector_version, "fallback": binding.fallback, "model_target": binding.model_target, "provider_id": binding.provider_id, "region": binding.region, "route_policy_digest": binding.route_policy_digest, "slot_id": binding.slot_id, "transport": binding.transport}
            for binding in sorted(bindings.values(), key=lambda item: item.binding_id)
        ],
        "edges": [{"from_node": edge.from_node, "from_port": edge.from_port, "to_node": edge.to_node, "to_port": edge.to_port, "when": edge.when} for edge in edges],
        "levels": [list(level) for level in levels],
        "nodes": [
            {"approval_policy": dict(node.approval_policy), "binding_id": node.binding_id, "budgets": dict(node.budgets), "hard_deadline_ms": node.hard_deadline_ms, "isolation": node.isolation.value, "kind": node.kind, "node_id": node.node_id, "package_digest": node.package_digest, "required_effects": sorted(effect.value for effect in node.required_effects)}
            for node in nodes
        ],
        "package_digests": sorted({node.package_digest for node in nodes if node.package_digest}),
        "policy_digest": policy_digest,
        "source_graph_digest": graph.digest,
    }


def _string(value: object, pointer: str) -> str:
    if not isinstance(value, str) or not value:
        raise GraphValidationError("connection_snapshot", pointer, "must be a non-empty string")
    return value


def _strings(value: object, pointer: str) -> frozenset[str]:
    if not isinstance(value, (frozenset, set, tuple, list)):
        raise GraphValidationError("connection_snapshot", pointer, "must be a collection of strings")
    values = frozenset(_string(item, pointer) for item in value)
    if not values:
        raise GraphValidationError("connection_snapshot", pointer, "must not be empty")
    return values


def _digest(value: object, pointer: str) -> str:
    text = _string(value, pointer)
    if not _DIGEST.fullmatch(text):
        raise GraphValidationError("policy_digest", pointer, "must be a sha256 digest")
    return text


EnumValue = TypeVar("EnumValue", bound=Enum)


def _enum(enum: type[EnumValue], value: object, pointer: str) -> EnumValue:
    try:
        return enum(value)
    except ValueError as exc:
        raise GraphValidationError("connection_snapshot", pointer, "contains an unsupported enum") from exc


def _candidate_from_raw(value: object) -> ConnectionCandidate:
    if not isinstance(value, Mapping):
        raise GraphValidationError("connection_snapshot", "/connections", "must be an object")
    return ConnectionCandidate.from_mapping(value)


def _bool(value: object, pointer: str) -> bool:
    if not isinstance(value, bool):
        raise GraphValidationError("connection_snapshot", pointer, "must be boolean")
    return value
